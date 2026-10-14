"""Import preflight IO and integrity contracts using only synthetic local state.

The test runner supplies isolated configuration; each test scopes its sources,
queue and session state. No models, private sources, indexes or HOME overrides.
"""
import json
import os
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from unittest import mock

from claude_note import config, import_publication as publication, models
from claude_note import queue_manager, session_tracker, transcript_reader



class ImportPreflightTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='cn-preflight-contract-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.enterContext(mock.patch.multiple(config, STATE_DIR=self.root/'state', QUEUE_DIR=self.root/'queue'))
        self.enterContext(mock.patch.object(Path, 'home', return_value=self.root/'home'))
        self.enterContext(mock.patch.dict(os.environ, {'XDG_CONFIG_HOME': str(self.root/'xdg')}))

    def publish(self, session, revision=1, old=False):
        path = self.root/(session+'.jsonl')
        raw = (json.dumps({'type': 'user', 'message': {'content': f'Public {session} revision{revision} '+('x'*65536)}})+'\n').encode()
        path.write_bytes(raw)
        arguments = ('claude-code', session, str(path), path, raw, '', 'Public synthetic task')
        if old:
            with mock.patch.object(publication, 'datetime') as clock:
                clock.utcnow.return_value = datetime(2000, 1, 1)
                return publication.publish(*arguments, origin_sources=[str(path)])
        return publication.publish(*arguments, origin_sources=[str(path)])

    def events(self, result):
        return list(queue_manager.read_events(result.queue_path))

    def capture(self, result, events=None):
        values = self.events(result) if events is None else events
        state = session_tracker.update_session_from_events(values[0].session_id, values)
        session_tracker.save_session_state(state)
        return state

    @contextmanager
    def count_sources(self):
        calls = []
        read = publication._read
        def inspect(path, limit):
            if Path(path).parent == config.STATE_DIR/'sources':
                calls.append(str(path))
            return read(path, limit)
        with mock.patch.object(publication, '_read', side_effect=inspect):
            yield calls

    def corrupt(self, result):
        before = result.snapshot_path.stat()
        raw = result.snapshot_path.read_bytes().replace(b'Public', b'Broken', 1)
        result.snapshot_path.write_bytes(raw)
        os.utime(result.snapshot_path, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertEqual(result.snapshot_path.stat().st_size, before.st_size)
        self.assertEqual(result.snapshot_path.stat().st_mtime_ns, before.st_mtime_ns)

    def test_captured_retained_backlog_requires_no_snapshot_body_reads(self):
        retained = [self.publish(f'captured-{number}') for number in range(10)]
        for result in retained:
            self.capture(result)
        with self.count_sources() as calls:
            self.assertEqual(list(queue_manager.read_pending_events(limit=1)), [])
        self.assertEqual(calls, [])
        self.assertTrue(all(result.queue_path.exists() for result in retained), 'No lifetime/retirement change')

    def test_limit_one_hashes_only_the_selected_session_not_unrelated_backlog(self):
        pending = [self.publish(f'pending-{number}') for number in range(10)]
        by_session = {self.events(result)[0].session_id: result for result in pending}
        with self.count_sources() as calls:
            events = list(queue_manager.read_pending_events(limit=1))
        self.assertEqual(len(events), 1)
        self.assertEqual(calls, [str(by_session[events[0].session_id].snapshot_path)])

    def test_same_session_all_visible_revisions_hash_before_first_delivery(self):
        first, second = self.publish('same', 1), self.publish('same', 2)
        with self.count_sources() as calls:
            self.assertEqual(len(list(queue_manager.read_pending_events(limit=1))), 1)
        self.assertEqual(set(calls), {str(first.snapshot_path), str(second.snapshot_path)})

    def test_corrupt_pending_older_revision_blocks_newer_same_session(self):
        first = self.publish('same', 1)
        self.publish('same', 2)
        healthy = self.publish('unrelated')
        self.corrupt(first)
        events = list(queue_manager.read_pending_events())
        self.assertEqual({event.session_id for event in events}, {'unrelated'})
        self.assertTrue(first.queue_path.exists() and healthy.queue_path.exists())
        self.assertFalse(session_tracker.get_state_file('same').exists())

    def test_captured_older_revision_still_blocks_pending_newer_same_session(self):
        first = self.publish('same', 1)
        self.capture(first)
        self.publish('same', 2)
        self.corrupt(first)
        self.assertEqual(list(queue_manager.read_pending_events(limit=1)), [])
        self.assertEqual(len(session_tracker.load_session_state('same').processed_event_ids), 2)
        self.assertTrue(first.queue_path.exists())

    def test_pending_ordinary_hook_cannot_bypass_visible_import_corruption(self):
        first = self.publish('same')
        self.capture(first)
        self.corrupt(first)
        queue_manager.enqueue_event(models.QueuedEvent('new-hook', '2035-01-01T00:00:00Z',
            'UserPromptSubmit', 'same', '', str(self.root/'live.jsonl'), {'prompt': 'Public hook'}))
        self.assertEqual(list(queue_manager.read_pending_events()), [])

    def test_copied_ordinary_journal_reference_cannot_bypass_visible_history(self):
        first = self.publish('same', 1)
        self.capture(first)
        second = self.publish('same', 2)
        copied = self.events(second)[0]
        queue_manager.enqueue_event(copied)
        self.corrupt(first)
        self.assertEqual(list(queue_manager.read_pending_events()), [])
        self.assertEqual(len(session_tracker.load_session_state('same').processed_event_ids), 2)

    def test_captured_ordinary_hook_cursor_ack_still_checks_visible_imports(self):
        result = self.publish('same')
        self.capture(result)
        hook = models.QueuedEvent('captured-hook', '2035-01-01T00:00:00Z',
            'UserPromptSubmit', 'same', '', str(self.root/'live.jsonl'), {'prompt': 'Public hook'})
        queue_manager.enqueue_event(hook)
        state = session_tracker.update_session_from_events('same', [hook])
        session_tracker.save_session_state(state)
        self.corrupt(result)
        with self.count_sources() as calls:
            self.assertEqual(list(queue_manager.read_pending_events()), [])
        self.assertIn(str(result.snapshot_path), calls)
        cursor = config.STATE_DIR/'queue-offsets'/(queue_manager.get_queue_file().stem+'.json')
        self.assertTrue(not cursor.exists() or json.loads(cursor.read_bytes()).get('offset', 0) == 0)

    def test_bad_queue_metadata_is_checked_even_for_captured_backlog(self):
        result = self.publish('captured')
        self.capture(result)
        before = result.queue_path.read_bytes()
        result.queue_path.write_bytes(before+b'invalid\n')
        self.publish('captured', 2)
        self.assertEqual(list(queue_manager.read_pending_events()), [])
        self.assertTrue(result.queue_path.exists())

    def test_deeply_malformed_receipt_isolates_affected_session(self):
        result = self.publish('affected')
        healthy = self.publish('healthy')
        receipt = publication._receipt(result.publication_id)
        malformed = '{"nested":' + '['*1500 + '0' + ']'*1500 + '}'
        receipt.write_text(malformed)
        events = list(queue_manager.read_pending_events())
        self.assertEqual({event.session_id for event in events}, {'healthy'})
        self.assertEqual({event.event_id for event in events}, set(healthy.event_ids))
        self.assertTrue(result.queue_path.exists())
        self.assertEqual(receipt.read_text(), malformed)
        self.assertFalse(session_tracker.get_state_file('affected').exists())

    def test_deep_first_hint_line_preserves_later_deny_only_session_hint(self):
        result = self.publish('affected', 1)
        hint = self.events(result)[1].to_json()
        newer = self.publish('affected', 2)
        healthy = self.publish('healthy')
        # Invalid canonical metadata supplies no trusted session identity. The
        # second queue line may only deny delivery; it cannot authorize capture.
        publication._receipt(result.publication_id).write_text('{}')
        deep_line = '['*1500 + '0' + ']'*1500
        result.queue_path.write_text(deep_line + '\n' + hint + '\n')
        self.assertEqual(publication.batch_session_hints(result.queue_path), {'affected'})
        events = list(queue_manager.read_pending_events())
        self.assertEqual({event.session_id for event in events}, {'healthy'})
        self.assertEqual({event.event_id for event in events}, set(healthy.event_ids))
        self.assertTrue(result.queue_path.exists() and newer.queue_path.exists())
        self.assertFalse(session_tracker.get_state_file('affected').exists())

    def test_corrupt_unrelated_session_state_does_not_stop_healthy_delivery(self):
        self.publish('broken-state')
        session_tracker.get_state_file('broken-state').write_bytes(b'{invalid')
        self.publish('healthy')
        self.assertEqual({event.session_id for event in queue_manager.read_pending_events()}, {'healthy'})

    def test_shared_snapshot_corruption_blocks_every_referencing_session(self):
        first = self.publish('shared-first')
        path = self.root/'shared-second.jsonl'
        raw = first.snapshot_path.read_bytes()
        path.write_bytes(raw)
        second = publication.publish('claude-code', 'shared-second', str(path), path, raw,
            '', 'Public synthetic task', origin_sources=[str(path)])
        self.assertEqual(first.snapshot_path, second.snapshot_path)
        self.publish('healthy-independent')
        self.corrupt(first)
        events = list(queue_manager.read_pending_events())
        self.assertEqual({event.session_id for event in events}, {'healthy-independent'})

    def test_source_changed_after_state_lookup_is_checked_before_delivery(self):
        result = self.publish('same')
        load = session_tracker.load_session_state
        changed = False
        def lookup(session_id):
            nonlocal changed
            state = load(session_id)
            if session_id == 'same' and not changed:
                self.corrupt(result)
                changed = True
            return state
        with mock.patch.object(session_tracker, 'load_session_state', side_effect=lookup):
            self.assertEqual(list(queue_manager.read_pending_events()), [])
        self.assertTrue(changed)
        self.assertFalse(session_tracker.get_state_file('same').exists())

    def test_valid_metadata_change_between_passes_cannot_rebind_delivery(self):
        result = self.publish('same')
        receipt = publication._receipt(result.publication_id)
        load = session_tracker.load_session_state
        changed = False
        def lookup(session_id):
            nonlocal changed
            state = load(session_id)
            if session_id == 'same' and not changed:
                record = json.loads(receipt.read_bytes())
                # This remains valid canonical metadata, with unchanged IDs and
                # source bytes. It must still differ from the first-pass view.
                record['ordinal'] += 1
                receipt.write_text(json.dumps(record))
                changed = True
            return state
        with mock.patch.object(session_tracker, 'load_session_state', side_effect=lookup):
            self.assertEqual(list(queue_manager.read_pending_events()), [])
        self.assertTrue(changed)
        self.assertTrue(result.queue_path.exists())
        self.assertFalse(session_tracker.get_state_file('same').exists())

    def test_scalar_type_change_between_metadata_passes_is_not_equal(self):
        result = self.publish('same')
        receipt = publication._receipt(result.publication_id)
        load = session_tracker.load_session_state
        changed = False
        def lookup(session_id):
            nonlocal changed
            state = load(session_id)
            if session_id == 'same' and not changed:
                record = json.loads(receipt.read_bytes())
                record['version'] = True  # Python True == 1; canonical JSON differs.
                receipt.write_text(json.dumps(record))
                changed = True
            return state
        with mock.patch.object(session_tracker, 'load_session_state', side_effect=lookup):
            self.assertEqual(list(queue_manager.read_pending_events()), [])
        self.assertTrue(changed)
        self.assertTrue(result.queue_path.exists())
        self.assertFalse(session_tracker.get_state_file('same').exists())

    def test_valid_extra_scalar_type_drift_cannot_rebind_metadata_view(self):
        result = self.publish('same')
        receipt = publication._receipt(result.publication_id)
        def rewrite(value):
            record = json.loads(receipt.read_bytes())
            for event in record['events']:
                event['data']['synthetic_scalar'] = value
            receipt.write_text(json.dumps(record))
            result.queue_path.write_bytes(publication._batch_bytes(record))
        rewrite(1)
        initial = publication.validate_batch(result.queue_path)
        self.assertIs(type(initial['events'][0]['data']['synthetic_scalar']), int)
        load = session_tracker.load_session_state
        changed = False
        def lookup(session_id):
            nonlocal changed
            state = load(session_id)
            if session_id == 'same' and not changed:
                rewrite(True)
                changed = True
            return state
        with mock.patch.object(session_tracker, 'load_session_state', side_effect=lookup):
            self.assertEqual(list(queue_manager.read_pending_events()), [])
        # The new canonical view itself passes strict version and source checks;
        # rejecting delivery requires detecting its JSON type drift from 1.
        current = publication.validate_batch(result.queue_path)
        self.assertIs(current['events'][0]['data']['synthetic_scalar'], True)
        self.assertTrue(changed)
        self.assertFalse(session_tracker.get_state_file('same').exists())

    def test_metadata_only_view_never_relaxes_public_source_verification(self):
        result = self.publish('captured')
        self.capture(result)
        self.corrupt(result)
        with self.count_sources() as calls:
            record = publication._validate_batch_metadata(result.queue_path)
            self.assertEqual(record['publication_id'], result.publication_id)
            self.assertEqual(list(queue_manager.read_pending_events()), [])
        self.assertEqual(calls, [])
        with self.assertRaises(publication.PublicationError):
            publication.validate_batch(result.queue_path)
        with self.assertRaises(publication.PublicationError):
            self.events(result)

    def test_same_size_same_mtime_edit_is_not_a_verified_cache_hit(self):
        result = self.publish('same')
        self.assertEqual(len(list(queue_manager.read_pending_events())), 2)
        self.corrupt(result)
        self.assertEqual(list(queue_manager.read_pending_events()), [])

    def test_source_change_after_delivery_fails_before_capture_ack(self):
        result = self.publish('same')
        events = list(queue_manager.read_pending_events())
        self.corrupt(result)
        with self.assertRaises(publication.PublicationError):
            session_tracker.update_session_from_events('same', events)
        self.assertFalse(session_tracker.get_state_file('same').exists())

    def test_actual_binding_borrow_still_hashes_source_and_rejects_corruption(self):
        result = self.publish('same')
        state = self.capture(result)
        with self.count_sources() as calls:
            transcript_reader.read_transcript_from_state(state)
        self.assertIn(str(result.snapshot_path), calls)
        self.corrupt(result)
        with self.assertRaises(publication.PublicationError):
            transcript_reader.read_transcript_from_state(state)

    def test_partial_capture_cannot_retire_or_skip_the_remaining_stop(self):
        result = self.publish('same')
        first = list(queue_manager.read_pending_events(limit=1))
        self.capture(result, first)
        self.assertTrue(result.queue_path.exists())
        with self.count_sources() as calls:
            pending = list(queue_manager.read_pending_events(limit=1))
        self.assertEqual([event.event for event in pending], ['Stop'])
        self.assertIn(str(result.snapshot_path), calls)

    def test_recent_captured_batch_cleanup_reads_no_snapshot_and_preserves_batch(self):
        result = self.publish('recent')
        self.capture(result)
        with self.count_sources() as calls:
            queue_manager.cleanup_old_queue_files(keep_days=7)
        self.assertEqual(calls, [])
        self.assertTrue(result.queue_path.exists())

    def test_old_uncaptured_batch_is_retained(self):
        result = self.publish('old-unacked', old=True)
        with self.count_sources() as calls:
            queue_manager.cleanup_old_queue_files(keep_days=7)
        self.assertEqual(calls, [])
        self.assertTrue(result.queue_path.exists())

    def test_old_captured_batch_cleanup_verifies_then_retains_canonical_evidence(self):
        result = self.publish('old-acked', old=True)
        self.capture(result)
        with self.count_sources() as calls:
            queue_manager.cleanup_old_queue_files(keep_days=7)
        self.assertIn(str(result.snapshot_path), calls)
        self.assertFalse(result.queue_path.exists())
        self.assertTrue(result.snapshot_path.exists())
        self.assertTrue(publication._receipt(result.publication_id).exists())
        self.assertEqual(self.publish('old-acked', old=True).status, 'captured')
        self.assertFalse(result.queue_path.exists())

    def test_old_captured_corrupt_snapshot_remains_visible_for_reconciliation(self):
        result = self.publish('old-corrupt', old=True)
        self.capture(result)
        self.corrupt(result)
        queue_manager.cleanup_old_queue_files(keep_days=7)
        self.assertTrue(result.queue_path.exists())

    def test_cleanup_rechecks_unreadable_ack_after_full_source_validation(self):
        result = self.publish('old-ack-corrupt', old=True)
        self.capture(result)
        verify = publication.validate_batch
        changed = False
        def inspect(path):
            nonlocal changed
            record = verify(path)
            session_tracker.get_state_file(record['session_id']).write_bytes(b'{invalid durable state')
            changed = True
            return record
        with mock.patch.object(publication, 'validate_batch', side_effect=inspect):
            queue_manager.cleanup_old_queue_files(keep_days=7)
        self.assertTrue(changed)
        self.assertTrue(result.queue_path.exists(), 'Unreadable current ACK must retain the delivery copy')

    def test_cleanup_rechecks_current_ack_ids_after_full_source_validation(self):
        result = self.publish('old-ack-lost', old=True)
        self.capture(result)
        verify = publication.validate_batch
        changed = False
        def inspect(path):
            nonlocal changed
            record = verify(path)
            state = session_tracker.load_session_state(record['session_id'])
            state.processed_event_ids = []
            session_tracker.save_session_state(state)
            changed = True
            return record
        with mock.patch.object(publication, 'validate_batch', side_effect=inspect):
            queue_manager.cleanup_old_queue_files(keep_days=7)
        self.assertTrue(changed)
        self.assertTrue(result.queue_path.exists(), 'Missing current capture IDs must retain the delivery copy')


if __name__ == '__main__':
    unittest.main()
