"""Chunk inheritance on public synthetic evidence only; no providers or vaults."""

import hashlib
import json
import os
import tempfile
import threading
import unittest
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from claude_note import egress, source_store, state_io


class SourceChunkPrivacyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='claude-note-chunk-privacy-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.state = self.root / 'state'
        self.state.mkdir()
        self.scope = 'a' * 64
        self.public = json.dumps({'content': 'Public synthetic cited passage.'}) + '\n'

    def chunk(self, body=None, *, scope=None):
        body = self.public.encode() if body is None else body
        revision = hashlib.sha256(body).hexdigest()
        path = self.state / 'sources/objects' / (scope or self.scope) / 'chunks' / (revision + '.jsonl')
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_bytes(body)
        return path, revision

    def capture(self, raw=None, *, origins=(), legacy=False, budgeted=False):
        raw = self.public if raw is None else raw
        revision = hashlib.sha256(raw.encode()).hexdigest()
        if legacy:
            manifest = self.state / 'sources' / (revision + '.jsonl')
        else:
            manifest = self.state / 'sources/objects' / self.scope / (revision + '.source.json')
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(raw if legacy else '{"synthetic_manifest": true}\n')
        egress.capture_restrictions(manifest, origins=list(origins), captured_text=raw,
                                    vault_root=self.state, budgeted=budgeted)
        return manifest, revision

    def inherit(self, captured, targets):
        manifest, revision = captured
        return egress.inherit_capture_restrictions(manifest, source_revision=revision,
                                                  targets=targets, vault_root=self.state)

    def ledger(self, path):
        return state_io.read_json(egress._lineage_path(path, self.state))

    def prepare(self, path, audience='model'):
        return egress.prepare('Public synthetic cited passage', audience=audience, sources=[str(path)],
                              rules={'default': ['local', 'host', 'model', 'shared']})

    def test_private_marker_outside_cited_chunk_restricts_every_chunk(self):
        private = json.dumps({'content': '---\nprivate: true\n---\nPrivate synthetic tail.'}) + '\n'
        original = self.root / 'original.jsonl'
        raw = self.public + private
        original.write_text(raw)
        captured = self.capture(raw, origins=[str(original)])
        targets = [self.chunk(), self.chunk(private.encode())]
        with mock.patch.object(egress, '_source_restrictions', side_effect=AssertionError('No origin rescans')):
            self.inherit(captured, targets)
        for target, revision in targets:
            record = self.ledger(target)
            self.assertEqual(record['revision'], revision)
            self.assertEqual(record['artifact_audiences'], ['host', 'local'])
            self.assertEqual(set(record['origins']), {str(original), str(captured[0])})
            with self.assertRaises(egress.EgressDenied):
                self.prepare(target)
            self.assertEqual(self.prepare(target, 'host'), 'Public synthetic cited passage')

    def test_replay_and_less_restrictive_origins_never_loosen_existing_chunk(self):
        private = self.root / 'private.md'
        private.write_text('---\nprivate: true\n---\nPrivate synthetic origin.')
        target = self.chunk()
        captured = self.capture(origins=[str(private)])
        self.inherit(captured, [target])
        private.write_text('Public-looking replay cannot erase its captured restrictions.')
        replay = self.capture(origins=[])
        self.inherit(replay, [target])
        first = self.ledger(target[0])
        self.inherit(replay, [target, target])
        self.assertEqual(self.ledger(target[0]), first)
        self.assertIn(str(private), first['origins'])
        self.assertEqual(first['artifact_audiences'], ['host', 'local'])
        with self.assertRaises(egress.EgressDenied):
            self.prepare(target[0])

    def test_legacy_source_and_deleted_origin_keep_local_reads_and_external_denial(self):
        original = self.root / 'original.jsonl'
        original.write_text(self.public)
        captured = self.capture(origins=[str(original)], legacy=True)
        original.unlink()
        target = self.chunk()
        with mock.patch.object(egress, '_source_restrictions', side_effect=AssertionError('Deleted origin not reread')):
            self.inherit(captured, [target])
        self.assertEqual(self.prepare(target[0], 'host'), 'Public synthetic cited passage')
        with self.assertRaises(egress.EgressDenied):
            self.prepare(target[0])

    def test_unknown_source_is_copied_to_all_chunks_before_local_capture_exception(self):
        captured = self.capture('<!-- claude-note:claim malformed synthetic control -->\n' + self.public)
        self.assertTrue(self.ledger(captured[0])['unknown'])
        targets = [self.chunk(), self.chunk(b'{"content":"Second public synthetic passage"}\n')]
        with self.assertRaises(egress.UnresolvedSourceControls):
            self.inherit(captured, targets)
        for target, revision in targets:
            self.assertEqual(self.ledger(target)['revision'], revision)
            self.assertTrue(self.ledger(target)['unknown'])
            self.assertEqual(self.prepare(target, 'host'), 'Public synthetic cited passage')
            with self.assertRaises(egress.EgressDenied):
                self.prepare(target)

    def test_existing_unknown_target_remains_unknown_under_public_replay(self):
        captured = self.capture()
        target = self.chunk()
        state_io.atomic_json(egress._lineage_path(target[0], self.state),
            {'version': 1, 'origins': [], 'unknown': True, 'revision': target[1]})
        with self.assertRaises(egress.UnresolvedSourceControls):
            self.inherit(captured, [target])
        self.assertTrue(self.ledger(target[0])['unknown'])

    def test_missing_corrupt_duplicate_and_wrong_revision_source_ledgers_fail_closed(self):
        captured = self.capture()
        ledger = egress._lineage_path(captured[0], self.state)
        original = ledger.read_bytes()
        target = self.chunk()
        for bad in (None, b'{bad', b'{"version":1,"version":1}',
                    json.dumps({'version': 1, 'origins': [], 'unknown': False, 'revision': '0' * 64}).encode(),
                    json.dumps({'version': 1, 'origins': [], 'unknown': 'false', 'revision': captured[1]}).encode()):
            with self.subTest(bad=bad):
                if bad is None:
                    ledger.unlink()
                else:
                    ledger.write_bytes(bad)
                with self.assertRaises(egress.EgressDenied):
                    self.inherit(captured, [target])
                self.assertFalse(egress._lineage_path(target[0], self.state).exists())
                ledger.write_bytes(original)

    def test_symlink_sources_targets_and_lineage_state_cannot_publish_outside(self):
        captured = self.capture()
        target = self.chunk()
        outside = self.root / 'outside'
        outside.write_text('Public synthetic sentinel')
        before = outside.read_bytes()
        for path in (captured[0], target[0], egress._lineage_path(captured[0], self.state)):
            with self.subTest(path=path.name):
                saved = path.read_bytes()
                path.unlink()
                path.symlink_to(outside)
                with self.assertRaises(egress.EgressDenied):
                    self.inherit(captured, [target])
                path.unlink()
                path.write_bytes(saved)
                self.assertEqual(outside.read_bytes(), before)

    def test_namespace_digest_and_regular_file_validation_precede_visibility(self):
        captured = self.capture()
        target = self.chunk()
        outside = self.root / 'outside.jsonl'
        outside.write_text(self.public)
        wrong_namespace = self.state / (target[1] + '.jsonl')
        wrong_namespace.write_text(self.public)
        for request in ([(outside, target[1])], [(wrong_namespace, target[1])],
                        [(captured[0], captured[1])], [(target[0], '0' * 64)]):
            with self.subTest(request=request), self.assertRaises(egress.EgressDenied):
                self.inherit(captured, request)
        target[0].write_text('Forged synthetic chunk bytes')
        visible = []
        def publish():
            self.inherit(captured, [target])
            visible.append(target)
        with self.assertRaises(egress.EgressDenied):
            publish()
        self.assertFalse(visible)
        target[0].unlink()
        os.mkfifo(target[0])
        with self.assertRaises(egress.EgressDenied):
            self.inherit(captured, [target])

    def test_previous_target_restrictions_intersect_and_bad_target_ledger_is_not_reset(self):
        captured = self.capture('<!-- claude-note:claim {"audiences":["host","model"]} -->\n' + self.public)
        target = self.chunk()
        ledger = egress._lineage_path(target[0], self.state)
        state_io.atomic_json(ledger, {'version': 1, 'origins': ['synthetic://previous'], 'unknown': False,
                                     'revision': target[1], 'artifact_audiences': ['host', 'shared']})
        self.inherit(captured, [target])
        self.assertEqual(self.ledger(target[0])['artifact_audiences'], ['host'])
        self.assertIn('synthetic://previous', self.ledger(target[0])['origins'])
        ledger.write_text('{corrupt')
        with self.assertRaises(egress.EgressDenied):
            self.inherit(captured, [target])
        self.assertEqual(ledger.read_text(), '{corrupt')

    def test_concurrent_fanouts_and_target_updates_preserve_all_restrictions(self):
        target = self.chunk()
        captures = []
        for number in range(4):
            raw = self.public + json.dumps({'content': str(number)}) + '\n'
            if number == 0:
                raw += '<!-- claude-note:claim {"audiences":["host"]} -->\n'
            captures.append(self.capture(raw))
        start = threading.Barrier(5)
        def inherit(captured):
            start.wait(timeout=2)
            self.inherit(captured, [target])
        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = [pool.submit(inherit, captured) for captured in captures]
            # A same-identity target publication must share the lineage lock.
            futures.append(pool.submit(lambda: (start.wait(timeout=2),
                egress.record_derivation(target[0], self.public, vault_root=self.state))))
            for future in futures:
                future.result(timeout=10)
        record = self.ledger(target[0])
        self.assertEqual(set(record['origins']), {str(captured[0]) for captured in captures})
        self.assertEqual(record['artifact_audiences'], ['host'])

    def test_one_deadline_bounds_waiting_across_multiple_lineage_locks(self):
        captured, target = self.capture(), self.chunk()
        clock = [0.0]
        attempts = {}
        original = egress.fcntl.flock
        def delayed(descriptor, flags):
            attempts[descriptor] = attempts.get(descriptor, 0) + 1
            if attempts[descriptor] <= 15:
                raise BlockingIOError('Synthetic lock contention')
            return original(descriptor, flags)
        def sleep(_):
            clock[0] += 1.0
        with mock.patch.object(egress.time, 'monotonic', side_effect=lambda: clock[0]), \
                mock.patch.object(egress.time, 'sleep', side_effect=sleep), \
                mock.patch.object(egress.fcntl, 'flock', side_effect=delayed):
            with self.assertRaisesRegex(egress.EgressDenied, 'deadline'):
                self.inherit(captured, [target])
        self.assertEqual(clock[0], 30.0)
        self.assertEqual(len(attempts), 2)
        self.assertFalse(egress._lineage_path(target[0], self.state).exists())

    def test_chunk_hash_reads_are_streaming_and_size_bounds_precede_reads(self):
        body = b'{"content":"' + b'x' * (2 * 1024 * 1024) + b'"}\n'
        captured = self.capture(body.decode())
        target = self.chunk(body)
        reads = []
        original = os.read
        def read(descriptor, count):
            reads.append(count)
            return original(descriptor, count)
        with mock.patch.object(egress.os, 'read', side_effect=read):
            self.inherit(captured, [target])
        self.assertGreater(len(reads), 2)
        self.assertLessEqual(max(reads), 1024 * 1024)
        with mock.patch.object(egress, 'NATIVE_SOURCE_BYTES', 64), \
                mock.patch.object(egress.os, 'read', side_effect=AssertionError('Oversize target read')):
            with self.assertRaises(egress.EgressDenied):
                self.inherit(captured, [target])

    def test_fanout_quota_and_headroom_denial_publish_no_chunk_lineage(self):
        captured = self.capture()
        targets = [self.chunk(), self.chunk(b'{"content":"Another synthetic passage"}\n')]
        before = egress._lineage_path(captured[0], self.state).read_bytes()
        for limitation in (mock.patch.object(source_store, '_free_bytes', return_value=0),
                           mock.patch.object(source_store, 'MAX_STORE_BYTES', 1)):
            with limitation, self.assertRaises(egress.EgressDenied):
                self.inherit(captured, targets)
            self.assertTrue(all(not egress._lineage_path(path, self.state).exists() for path, _ in targets))
            self.assertTrue(all(not egress._lineage_path(path, self.state).with_suffix('.lock').exists()
                                for path, _ in targets))
            self.assertEqual(egress._lineage_path(captured[0], self.state).read_bytes(), before)

    def test_one_reservation_covers_all_fanout_writes_and_replay_allocates_nothing(self):
        captured = self.capture()
        targets = [self.chunk(), self.chunk(b'{"content":"Another synthetic passage"}\n')]
        admissions, writes = [], []
        admit, atomic = source_store.auxiliary_admission, state_io.atomic_json
        @contextmanager
        def admission(sizes, **kwargs):
            admissions.append((sizes, kwargs))
            with admit(sizes, **kwargs):
                yield
        def write(path, record):
            # A second descriptor cannot take the global store lock while any
            # target ledger becomes visible. This is an actual flock check.
            descriptor = state_io.open_lock(source_store._root(self.state) / '.store.lock')
            try:
                with self.assertRaises(BlockingIOError):
                    egress.fcntl.flock(descriptor, egress.fcntl.LOCK_EX | egress.fcntl.LOCK_NB)
            finally:
                os.close(descriptor)
            writes.append(path)
            return atomic(path, record)
        with mock.patch.object(source_store, 'auxiliary_admission', side_effect=admission), \
                mock.patch.object(state_io, 'atomic_json', side_effect=write):
            self.inherit(captured, targets)
        self.assertEqual(len(admissions), 1)
        self.assertEqual(len(admissions[0][0]), 2)
        self.assertEqual(admissions[0][1]['state_dir'], self.state)
        self.assertIsInstance(admissions[0][1]['deadline'], float)
        self.assertEqual(len(writes), 2)
        before = {path: egress._lineage_path(path, self.state).stat().st_mtime_ns for path, _ in targets}
        with mock.patch.object(source_store, 'auxiliary_admission', side_effect=AssertionError('Replay reservation')), \
                mock.patch.object(state_io, 'atomic_json', side_effect=AssertionError('Replay write')), \
                mock.patch.object(source_store, '_free_bytes', return_value=0):
            self.inherit(captured, targets)
        self.assertEqual(before, {path: egress._lineage_path(path, self.state).stat().st_mtime_ns for path, _ in targets})

    def test_budgeted_capture_and_derivation_reserve_exact_records_and_skip_replay(self):
        admissions = []
        admit = source_store.auxiliary_admission
        @contextmanager
        def admission(sizes, **kwargs):
            admissions.append((sizes, kwargs))
            with admit(sizes, **kwargs):
                yield
        derived = self.state / 'derived.source.json'
        derived.write_text('{"synthetic":true}')
        content = '<!-- claude-note:claim {"audiences":["host"]} -->\nPublic synthetic derivation.'
        with mock.patch.object(source_store, 'auxiliary_admission', side_effect=admission):
            captured = self.capture(budgeted=True)
            egress.record_derivation(derived, content, vault_root=self.state, budgeted=True)
        self.assertEqual(len(admissions), 2)
        for path, (sizes, options) in zip((captured[0], derived), admissions):
            self.assertEqual(sizes, [egress._lineage_path(path, self.state).stat().st_size])
            self.assertEqual(options['state_dir'], self.state)
        with mock.patch.object(source_store, 'auxiliary_admission', side_effect=AssertionError('Replay reservation')), \
                mock.patch.object(state_io, 'atomic_json', side_effect=AssertionError('Replay write')):
            self.capture(budgeted=True)
            egress.record_derivation(derived, content, vault_root=self.state, budgeted=True)
        blocked = self.state / 'blocked.source.json'
        blocked.write_text('{"synthetic":true}')
        with mock.patch.object(source_store, '_free_bytes', return_value=0), self.assertRaises(ValueError):
            egress.capture_restrictions(blocked, origins=[], captured_text=self.public,
                                        vault_root=self.state, budgeted=True)
        self.assertFalse(egress._lineage_path(blocked, self.state).exists())
        with mock.patch.object(source_store, 'MAX_STORE_BYTES', 1), self.assertRaises(ValueError):
            egress.record_derivation(blocked, content, vault_root=self.state, budgeted=True)
        self.assertFalse(egress._lineage_path(blocked, self.state).exists())

    def test_budgeted_writers_take_shared_gate_before_their_own_lineage_lock(self):
        captured = self.capture()
        target = self.chunk()[0]
        gate = self.state / '.claude-note/provenance/.capture-inheritance.lock'
        operations = (
            lambda: egress.capture_restrictions(captured[0], origins=[], captured_text=self.public,
                                                vault_root=self.state, budgeted=True),
            lambda: egress.record_derivation(target, '<!-- claude-note:claim {"audiences":["host"]} -->',
                                             vault_root=self.state, budgeted=True),
        )
        for operation in operations:
            with self.subTest(operation=operation):
                owner = state_io.open_lock(gate)
                egress.fcntl.flock(owner, egress.fcntl.LOCK_EX)
                waiting = threading.Event()
                opened = []
                open_lock, flock = state_io.open_lock, egress.fcntl.flock
                descriptors = {}
                def open_traced(path):
                    descriptor = open_lock(path)
                    descriptors[descriptor] = Path(path)
                    opened.append(Path(path))
                    return descriptor
                def lock_traced(descriptor, flags):
                    try:
                        return flock(descriptor, flags)
                    except BlockingIOError:
                        if descriptors.get(descriptor) == gate:
                            waiting.set()
                        raise
                with mock.patch.object(state_io, 'open_lock', side_effect=open_traced), \
                        mock.patch.object(egress.fcntl, 'flock', side_effect=lock_traced), \
                        ThreadPoolExecutor(max_workers=1) as pool:
                    future = pool.submit(operation)
                    try:
                        self.assertTrue(waiting.wait(2))
                        self.assertEqual(opened, [gate])
                    finally:
                        os.close(owner)
                    future.result(timeout=5)
                self.assertGreater(len(opened), 1)

    def test_budgeted_derivation_cannot_hold_target_while_fanout_holds_store(self):
        captured, target = self.capture(), self.chunk()
        entered, release, writer_waiting = threading.Event(), threading.Event(), threading.Event()
        gate = self.state / '.claude-note/provenance/.capture-inheritance.lock'
        target_lock = egress._lineage_path(target[0], self.state).with_suffix('.lock')
        admit, open_lock, flock = source_store.auxiliary_admission, state_io.open_lock, egress.fcntl.flock
        writer_descriptors, writer_paths = {}, []
        @contextmanager
        def admission(sizes, **kwargs):
            with admit(sizes, **kwargs):
                if threading.current_thread().name.startswith('fanout'):
                    entered.set()
                    if not release.wait(3):
                        raise AssertionError('Synthetic fanout release deadline')
                yield
        def open_traced(path):
            descriptor = open_lock(path)
            if threading.current_thread().name.startswith('writer'):
                writer_descriptors[descriptor] = Path(path)
                writer_paths.append(Path(path))
            return descriptor
        def lock_traced(descriptor, flags):
            try:
                return flock(descriptor, flags)
            except BlockingIOError:
                if writer_descriptors.get(descriptor) == gate:
                    writer_waiting.set()
                raise
        with mock.patch.object(source_store, 'auxiliary_admission', side_effect=admission), \
                mock.patch.object(state_io, 'open_lock', side_effect=open_traced), \
                mock.patch.object(egress.fcntl, 'flock', side_effect=lock_traced), \
                ThreadPoolExecutor(max_workers=1, thread_name_prefix='fanout') as fanout, \
                ThreadPoolExecutor(max_workers=1, thread_name_prefix='writer') as writer:
            first = fanout.submit(self.inherit, captured, [target])
            self.assertTrue(entered.wait(2))
            second = writer.submit(egress.record_derivation, target[0], self.public,
                                   vault_root=self.state, budgeted=True)
            try:
                self.assertTrue(writer_waiting.wait(2))
                self.assertNotIn(target_lock, writer_paths)
            finally:
                release.set()
            first.result(timeout=5)
            second.result(timeout=5)
        self.assertIn(str(captured[0]), self.ledger(target[0])['origins'])

    def test_lineage_changed_between_plan_and_admission_fails_without_acknowledgement(self):
        captured, target = self.capture(), self.chunk()
        ledger = egress._lineage_path(target[0], self.state)
        admit = source_store.auxiliary_admission
        @contextmanager
        def changed(sizes, **kwargs):
            with admit(sizes, **kwargs):
                state_io.atomic_json(ledger, {'version': 1, 'origins': ['synthetic://retained'],
                    'unknown': True, 'revision': target[1], 'artifact_audiences': ['host']})
                yield
        visible = []
        with mock.patch.object(source_store, 'auxiliary_admission', side_effect=changed), \
                self.assertRaises(egress.EgressDenied) as raised:
            self.inherit(captured, [target])
            visible.append(True)
        self.assertNotIsInstance(raised.exception, egress.UnresolvedSourceControls)
        self.assertFalse(visible)
        self.assertTrue(self.ledger(target[0])['unknown'])
        self.assertEqual(self.ledger(target[0])['origins'], ['synthetic://retained'])


if __name__ == '__main__':
    unittest.main()
