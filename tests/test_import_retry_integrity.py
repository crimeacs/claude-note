"""Synthetic import failures retry cheaply without weakening evidence gates."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from claude_note import config, import_publication as publication, models
from claude_note import queue_manager, session_tracker, source_store


class ImportRetryIntegrityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='cn-retry-contract-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.enterContext(mock.patch.multiple(config, STATE_DIR=self.root/'state', QUEUE_DIR=self.root/'queue'))
        self.enterContext(mock.patch.object(Path, 'home', return_value=self.root/'home'))
        self.enterContext(mock.patch.dict(os.environ, {'XDG_CONFIG_HOME': str(self.root/'xdg')}))
        self.enterContext(mock.patch.object(queue_manager, '_import_failures', {}))

    def publish(self, session='same', revision=1, size=65536):
        path = self.root/(session+'.jsonl')
        raw = (json.dumps({'type': 'user', 'message': {'content': f'Public revision {revision} '+('x'*size)}})+'\n').encode()
        path.write_bytes(raw)
        result = publication.publish('claude-code', session, str(path), path, raw, '', 'Public synthetic task',
                                     origin_sources=[str(path)])
        return result, raw

    def corrupt(self, result):
        before = result.snapshot_path.stat()
        original = result.snapshot_path.read_bytes()
        result.snapshot_path.write_bytes(original.replace(b'Public', b'Broken', 1))
        os.utime(result.snapshot_path, ns=(before.st_atime_ns, before.st_mtime_ns))
        return original, before

    def test_legacy_verification_reads_fixed_size_buffers_without_materializing_source(self):
        result, _ = self.publish()
        inode = result.snapshot_path.stat().st_ino
        fdopen, calls = os.fdopen, []

        class GuardedHandle:
            def __init__(self, wrapped):
                self.wrapped = wrapped
            def __getattr__(self, name):
                return getattr(self.wrapped, name)
            def __enter__(self):
                self.wrapped.__enter__()
                return self
            def __exit__(self, *args):
                return self.wrapped.__exit__(*args)
            def read(self, size=-1):
                if os.fstat(self.wrapped.fileno()).st_ino == inode:
                    calls.append(size)
                    if not 0 < size <= 4096:
                        raise AssertionError('unbounded source verification read')
                return self.wrapped.read(size)

        with mock.patch.object(publication, 'VERIFY_READ_BYTES', 4096), \
                mock.patch.object(publication.os, 'fdopen', side_effect=lambda *a, **kw: GuardedHandle(fdopen(*a, **kw))), \
                mock.patch.object(publication, '_read_source', side_effect=AssertionError('materialized source')):
            publication.validate_batch(result.queue_path)
        self.assertGreater(len(calls), 2)

    def test_streaming_legacy_verification_rejects_mid_read_change(self):
        result, _ = self.publish()
        original = publication.os.fstat
        inode, calls = result.snapshot_path.stat().st_ino, 0
        def changing(fd):
            nonlocal calls
            value = original(fd)
            if value.st_ino == inode:
                calls += 1
                if calls == 2:
                    with result.snapshot_path.open('ab') as handle:
                        handle.write(b'changed')
                    return original(fd)
            return value
        with mock.patch.object(publication.os, 'fstat', side_effect=changing):
            with self.assertRaises(publication.PublicationError):
                publication.validate_batch(result.queue_path)

    def test_legacy_verification_accepts_only_exact_interrupted_publication_alias(self):
        result, _ = self.publish()
        intent_path = result.snapshot_path.with_name(result.snapshot_path.name+'.intent.json')
        intent = json.loads(intent_path.read_bytes())
        temporary = result.snapshot_path.parent/intent['temporary']
        os.link(result.snapshot_path, temporary)
        publication.validate_batch(result.queue_path)
        self.assertTrue(temporary.exists(), 'Verification does not mutate evidence')
        temporary.rename(temporary.with_name('unrecognized-alias'))
        with self.assertRaises(publication.PublicationError):
            publication.validate_batch(result.queue_path)

    def test_missing_historical_dependency_prevents_other_hashes_and_hook_ack(self):
        first, _ = self.publish(revision=1)
        events = list(queue_manager.read_events(first.queue_path))
        state = session_tracker.update_session_from_events('same', events)
        session_tracker.save_session_state(state)
        second, _ = self.publish(revision=2)
        third, _ = self.publish(revision=3)
        third.snapshot_path.unlink()
        hook = models.QueuedEvent('captured-hook', '2035-01-01T00:00:00Z', 'UserPromptSubmit',
                                  'same', '', str(self.root/'same.jsonl'), {'prompt': 'Public hook'})
        queue_manager.enqueue_event(hook)
        state = session_tracker.update_session_from_events('same', [hook])
        session_tracker.save_session_state(state)
        healthy, _ = self.publish('healthy')
        verify = publication._verify_source
        with mock.patch.object(publication, '_verify_source', wraps=verify) as sources:
            pending = list(queue_manager.read_pending_events())
        self.assertEqual({event.session_id for event in pending}, {'healthy'})
        self.assertEqual([call.args[0]['session_id'] for call in sources.call_args_list], ['healthy'])
        self.assertTrue(first.queue_path.exists() and second.queue_path.exists() and third.queue_path.exists())
        self.assertTrue(healthy.snapshot_path.exists())
        self.assertEqual(set(session_tracker.load_session_state('same').processed_event_ids),
                         {event.event_id for event in events} | {hook.event_id})
        cursor = config.STATE_DIR/'queue-offsets'/(queue_manager.get_queue_file().stem+'.json')
        self.assertTrue(not cursor.exists() or json.loads(cursor.read_bytes()).get('offset', 0) == 0)

    def test_unchanged_corruption_is_not_rehashed_until_retry_due(self):
        result, _ = self.publish()
        self.corrupt(result)
        verify = publication._verify_source
        with mock.patch.object(queue_manager.time, 'monotonic', return_value=100) as clock, \
                mock.patch.object(publication, '_verify_source', wraps=verify) as sources:
            self.assertEqual(list(queue_manager.read_pending_events()), [])
            self.assertEqual(sources.call_count, 1)
            self.assertEqual(list(queue_manager.read_pending_events()), [])
            self.assertEqual(sources.call_count, 1)
            clock.return_value = 100 + queue_manager.IMPORT_RETRY_SECONDS
            self.assertEqual(list(queue_manager.read_pending_events()), [])
            self.assertEqual(sources.call_count, 2)
        self.assertFalse(session_tracker.get_state_file('same').exists())

    def test_repaired_source_with_same_size_and_mtime_retries_immediately(self):
        result, _ = self.publish()
        raw, before = self.corrupt(result)
        with mock.patch.object(queue_manager.time, 'monotonic', return_value=100):
            self.assertEqual(list(queue_manager.read_pending_events()), [])
            result.snapshot_path.write_bytes(raw)
            os.utime(result.snapshot_path, ns=(before.st_atime_ns, before.st_mtime_ns))
            self.assertEqual(len(list(queue_manager.read_pending_events())), 2)
        self.assertFalse(queue_manager._import_failures)

    def test_missing_chunk_repair_invalidates_failed_retry_delay(self):
        result, _ = self.publish(size=300000)
        record = publication.load(result.publication_id)
        self.assertEqual(record['version'], 2)
        scope = publication.source_scope(record['transcript_path'])
        manifest = source_store._manifest(result.snapshot_path, record['captured_revision'], scope)
        chunk = source_store._chunk_path(manifest['chunks'][0]['revision'], scope)
        body = chunk.read_bytes()
        chunk.unlink()
        with mock.patch.object(queue_manager.time, 'monotonic', return_value=100), \
                mock.patch.object(publication, '_verify_source', wraps=publication._verify_source) as sources:
            self.assertEqual(list(queue_manager.read_pending_events()), [])
            self.assertEqual(list(queue_manager.read_pending_events()), [])
            self.assertEqual(sources.call_count, 0)
            chunk.write_bytes(body)
            self.assertEqual(len(list(queue_manager.read_pending_events())), 2)
            self.assertEqual(sources.call_count, 1)

    def test_failure_cache_has_fixed_entry_bound_and_never_caches_success(self):
        with mock.patch.object(queue_manager, 'MAX_IMPORT_FAILURES', 2):
            for number in range(4):
                queue_manager._defer_import_session(('state', 'queue', str(number)), 'fingerprint')
        self.assertEqual(len(queue_manager._import_failures), 2)
        self.assertNotIn(('state', 'queue', '0'), queue_manager._import_failures)
        queue_manager._import_failures.clear()
        self.publish()
        self.assertEqual(len(list(queue_manager.read_pending_events())), 2)
        self.assertEqual(queue_manager._import_failures, {})


if __name__ == '__main__':
    unittest.main()
