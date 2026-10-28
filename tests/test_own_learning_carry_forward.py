"""Public offline contracts, not task-success or learning-benefit scores."""
import copy
import json
import logging
from pathlib import Path
import unittest
from unittest import mock
from support.public_own_learning import BridgeError, PublicBridge, public_fixture

class OwnLearningCarryForwardTests(unittest.TestCase):

    def setUp(self):
        owned = self.enterContext(public_fixture())
        self.root, self.fixture, self.app = (owned.root, owned.fixture, owned.app)
        self.bridge = PublicBridge(self.root / 'run', copy.deepcopy(self.fixture), arms=('dense_only', 'no_memory', 'static_instructions', 'bounded_history'))
        self.addCleanup(self.bridge.close)
        self.cell = self.bridge.cell('dense_only', 0)

    def completed(self, result):
        self.assertEqual(result.state, 'completed', dict(result.outcome))
        return result

    def learn(self, cell=None, prior=None):
        bridge, cell = (self.bridge, cell or self.cell)
        self.completed(bridge.retrieve(cell, prior))
        self.completed(bridge.act(cell))
        act = bridge.ref(cell, 'act', bridge.area(cell) / 'task-output.jsonl')
        self.completed(bridge.capture(cell, act))
        capture = bridge.ref(cell, 'capture', bridge.area(cell) / 'capture.json')
        self.completed(bridge.extract(cell, capture))
        extraction = bridge.ref(cell, 'extract', bridge.area(cell) / 'extraction.json')
        self.completed(bridge.mutate(cell, capture, extraction))
        change = bridge.ref(cell, 'mutate', bridge.area(cell) / 'mutation.json')
        self.completed(bridge.index(cell, capture, extraction, change))
        return bridge.bundle(cell)

    def until_extract(self):
        bridge, cell = (self.bridge, self.cell)
        self.completed(bridge.retrieve(cell))
        self.completed(bridge.act(cell))
        act = bridge.ref(cell, 'act', bridge.area(cell) / 'task-output.jsonl')
        self.completed(bridge.capture(cell, act))
        return bridge.ref(cell, 'capture', bridge.area(cell) / 'capture.json')

    def test_real_public_chain_delivers_actual_mutated_bytes(self):
        bundle = self.learn()
        result = self.completed(self.bridge.retrieve(self.bridge.cell('dense_only', 1), bundle))
        details = result.outcome['details']
        self.assertIn(self.fixture['phase_outputs'][0], details['context'])
        self.assertFalse(details['measured_learning'])
        self.assertFalse(details['reranked'])
        self.assertEqual(details['coverage']['lexical'], 'not_run')
        self.assertEqual(details['coverage']['rerank'], 'not_run')
        change = json.loads((self.bridge.area(self.cell) / 'mutation.json').read_bytes())
        self.assertEqual(change['effect_record']['revision'], change['learned']['revision'])
        self.assertEqual(self.bridge.delivery_calls, 1)
        self.assertEqual(self.bridge.transport_calls, 1)
        extraction = json.loads((self.bridge.area(self.cell) / 'extraction.json').read_bytes())
        self.assertTrue(extraction['real_cache_reuse_without_transport'])
        self.assertEqual(extraction['extraction_checkpoint']['revision'], extraction['pack']['source_revision'])

    def test_correction_retains_earlier_evidence_and_terminal_stages_disabled(self):
        bundle = None
        for phase in range(3):
            bundle = self.learn(self.bridge.cell('dense_only', phase), bundle)
        initial = self.bridge.vault(self.cell) / 'memory/phase-0.md'
        self.assertIn(self.fixture['phase_outputs'][0], initial.read_text())
        terminal = self.bridge.cell('dense_only', 3)
        result = self.completed(self.bridge.retrieve(terminal, bundle))
        self.assertIn(self.fixture['phase_outputs'][2], result.outcome['details']['context'])
        self.completed(self.bridge.act(terminal))
        act = self.bridge.ref(terminal, 'act', self.bridge.area(terminal) / 'task-output.jsonl')
        self.completed(self.bridge.capture(terminal, act))
        for stage in ('extract', 'mutate', 'index'):
            result = self.bridge.stage(terminal, stage, lambda _: self.fail('disabled callback entered'))
            self.assertEqual(result.state, 'disabled')
        self.assertFalse((self.bridge.vault(terminal) / 'memory/phase-3.md').exists())

    def altered_bundle(self, kind):
        bundle = self.learn()
        old = bundle['capture']
        cell, run_id, receipt = (old.cell, old.run_id, old.receipt_digest)
        if kind == 'cross_arm':
            cell = self.bridge.cell('no_memory', 0)
        if kind == 'future':
            cell = self.bridge.cell('dense_only', 2)
        if kind == 'wrong_run':
            run_id = 'different-run'
        if kind == 'uncaptured':
            receipt = '0' * 64
        bundle['capture'] = self.app.longitudinal.Reference(run_id, cell, old.stage, old.artifact, receipt)
        with self.assertRaises(self.app.longitudinal.ReferenceError):
            self.bridge.retrieve(self.bridge.cell('dense_only', 1), bundle)
        self.assertEqual(self.bridge.delivery_calls, 0)

    def test_cross_arm_denied_before_delivery(self):
        self.altered_bundle('cross_arm')

    def test_future_phase_denied_before_delivery(self):
        self.altered_bundle('future')

    def test_wrong_run_denied_before_delivery(self):
        self.altered_bundle('wrong_run')

    def test_uncaptured_receipt_denied_before_delivery(self):
        self.altered_bundle('uncaptured')

    def test_preauthored_lesson_has_no_real_mutation_producer(self):
        bundle = self.learn()
        path = self.bridge.area(self.cell) / 'preauthored-lesson.md'
        path.write_text('Authored public gold lesson that never passed extraction or mutation.')
        old = bundle['mutate']
        bundle['mutate'] = self.app.longitudinal.Reference(old.run_id, old.cell, old.stage, self.bridge.artifact(path), old.receipt_digest)
        with self.assertRaises(self.app.longitudinal.ReferenceError):
            self.bridge.retrieve(self.bridge.cell('dense_only', 1), bundle)
        self.assertEqual(self.bridge.delivery_calls, 0)

    def test_preauthored_phase_target_is_preserved_and_cannot_be_adopted(self):
        capture = self.until_extract()
        self.completed(self.bridge.extract(self.cell, capture))
        extraction = self.bridge.ref(self.cell, 'extract', self.bridge.area(self.cell) / 'extraction.json')
        path = self.bridge.vault(self.cell) / 'memory/phase-0.md'
        path.parent.mkdir(parents=True, exist_ok=True)
        original = b'Human-authored public oracle that never came from this capture.\n'
        path.write_bytes(original)
        self.assertEqual(self.bridge.mutate(self.cell, capture, extraction).state, 'failed')
        self.assertEqual(path.read_bytes(), original)
        self.assertFalse((self.bridge.area(self.cell) / 'simulated-index.json').exists())
        self.assertEqual(self.bridge.delivery_calls, 0)

    def test_tampered_learned_file_is_unavailable(self):
        bundle = self.learn()
        path = self.bridge.vault(self.cell) / 'memory/phase-0.md'
        path.write_text(path.read_text() + '\nAuthored tamper.\n')
        result = self.bridge.retrieve(self.bridge.cell('dense_only', 1), bundle)
        self.assertEqual(result.state, 'failed')
        self.assertEqual(self.bridge.delivery_calls, 0)

    def test_tampered_capture_snapshot_is_unavailable(self):
        bundle = self.learn()
        capture = json.loads((self.bridge.area(self.cell) / 'capture.json').read_bytes())
        path = self.bridge.root / capture['snapshot']['source_id']
        path.write_bytes(path.read_bytes() + b'{}\n')
        result = self.bridge.retrieve(self.bridge.cell('dense_only', 1), bundle)
        self.assertEqual(result.state, 'failed')
        self.assertEqual(self.bridge.delivery_calls, 0)

    def test_tampered_capture_producer_record_is_unavailable(self):
        bundle = self.learn()
        path = self.bridge.area(self.cell) / 'capture.json'
        value = json.loads(path.read_bytes())
        value['publication_id'] = '0' * 64
        path.write_text(json.dumps(value))
        with self.assertRaises(self.app.longitudinal.ReferenceError):
            self.bridge.retrieve(self.bridge.cell('dense_only', 1), bundle)
        self.assertEqual(self.bridge.delivery_calls, 0)

    def test_tampered_extraction_cache_prevents_mutation(self):
        capture = self.until_extract()
        self.completed(self.bridge.extract(self.cell, capture))
        extraction = self.bridge.ref(self.cell, 'extract', self.bridge.area(self.cell) / 'extraction.json')
        value = json.loads((self.bridge.area(self.cell) / 'extraction.json').read_bytes())
        path = self.bridge.root / value['cache']['source_id']
        path.write_bytes(path.read_bytes() + b' ')
        result = self.bridge.mutate(self.cell, capture, extraction)
        self.assertEqual(result.state, 'failed')
        self.assertFalse((self.bridge.vault(self.cell) / 'memory/phase-0.md').exists())

    def index_denial(self, **kwargs):
        capture = self.until_extract()
        self.completed(self.bridge.extract(self.cell, capture))
        extraction = self.bridge.ref(self.cell, 'extract', self.bridge.area(self.cell) / 'extraction.json')
        self.completed(self.bridge.mutate(self.cell, capture, extraction))
        change = self.bridge.ref(self.cell, 'mutate', self.bridge.area(self.cell) / 'mutation.json')
        self.assertEqual(self.bridge.index(self.cell, capture, extraction, change, **kwargs).state, 'failed')
        with self.bridge.owned_config(self.cell) as vault:
            state = self.app.freshness.snapshot(vault)
            self.assertEqual(state['lexical_generation'], 0)
            self.assertEqual(state['vector_generation'], 0)

    def test_inactive_simulated_index_cannot_acknowledge(self):
        self.index_denial(active=False)

    def test_changed_simulated_revision_cannot_acknowledge(self):
        self.index_denial(revision='0' * 64)

    def test_real_span_validator_denies_uncaptured_extraction(self):
        capture = self.until_extract()
        self.assertEqual(self.bridge.extract(self.cell, capture, wrong_span=True).state, 'failed')
        self.assertFalse((self.bridge.vault(self.cell) / '.claude-note/state/extractions').exists())
        self.assertFalse((self.bridge.vault(self.cell) / 'memory/phase-0.md').exists())

    def test_failed_extraction_replay_does_not_reinvoke_transport(self):
        capture = self.until_extract()
        self.assertEqual(self.bridge.extract(self.cell, capture, fail_transport=True).state, 'failed')
        result = self.bridge.extract(self.cell, capture, fail_transport=True)
        self.assertEqual(result.state, 'failed')
        self.assertTrue(result.replayed)
        self.assertEqual(self.bridge.transport_calls, 1)

    def test_completed_replay_does_not_duplicate_capture_extract_or_mutation(self):
        bundle = self.learn()
        act = self.bridge.ref(self.cell, 'act', self.bridge.area(self.cell) / 'task-output.jsonl')
        before = (self.bridge.vault(self.cell) / 'memory/phase-0.md').read_bytes()
        self.assertTrue(self.bridge.capture(self.cell, act).replayed)
        self.assertTrue(self.bridge.extract(self.cell, bundle['capture']).replayed)
        self.assertTrue(self.bridge.mutate(self.cell, bundle['capture'], bundle['extract']).replayed)
        self.assertTrue(self.bridge.index(self.cell, bundle['capture'], bundle['extract'], bundle['mutate']).replayed)
        self.assertEqual(self.bridge.transport_calls, 1)
        self.assertEqual((self.bridge.vault(self.cell) / 'memory/phase-0.md').read_bytes(), before)

    def test_freshness_changed_before_delivery_is_unavailable(self):
        bundle = self.learn()
        with self.bridge.owned_config(self.cell) as vault:
            self.app.mutation.create_note(vault / 'later-public.md', 'Later public change', vault_root=vault)
        self.assertEqual(self.bridge.retrieve(self.bridge.cell('dense_only', 1), bundle).state, 'failed')
        self.assertEqual(self.bridge.delivery_calls, 0)

    def test_no_memory_static_and_history_controls_never_extract_or_mutate(self):
        for arm in ('no_memory', 'static_instructions', 'bounded_history'):
            cell = self.bridge.cell(arm, 0)
            self.completed(self.bridge.retrieve(cell))
            self.completed(self.bridge.act(cell))
            act = self.bridge.ref(cell, 'act', self.bridge.area(cell) / 'task-output.jsonl')
            self.completed(self.bridge.capture(cell, act))
            capture = self.bridge.ref(cell, 'capture', self.bridge.area(cell) / 'capture.json')
            for stage in ('extract', 'mutate', 'index'):
                self.assertEqual(self.bridge.stage(cell, stage, lambda _: self.fail('control learned')).state, 'disabled')
            prior = {'capture': capture} if arm == 'bounded_history' else None
            result = self.completed(self.bridge.retrieve(self.bridge.cell(arm, 1), prior))
            context = result.outcome['details']['context']
            if arm == 'no_memory':
                self.assertEqual(context, '')
            elif arm == 'static_instructions':
                self.assertEqual(context, self.fixture['static_instruction'])
            else:
                self.assertIn(self.fixture['phase_outputs'][0], context)
            self.assertFalse((self.bridge.vault(cell) / 'memory').exists())
        self.assertEqual(self.bridge.transport_calls, 0)

    def test_symlinked_learned_artifact_cannot_deliver(self):
        bundle = self.learn()
        path = self.bridge.vault(self.cell) / 'memory/phase-0.md'
        other = path.with_name('authored-copy.md')
        other.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(other)
        self.assertEqual(self.bridge.retrieve(self.bridge.cell('dense_only', 1), bundle).state, 'failed')
        self.assertEqual(self.bridge.delivery_calls, 0)

    def test_nested_producers_are_authenticated_before_lock_and_never_reread_inside(self):
        capture = self.until_extract()
        actual_resolver = self.bridge.journal.reference

        def callback(context):
            with mock.patch.object(self.bridge.journal, 'reference', side_effect=AssertionError('nested journal lock')):
                record = self.bridge.captured(self.cell, capture)
                self.assertEqual(record['cell'], self.cell.to_dict())
                self.assertEqual(len(self.bridge._callback_references), 2)
            return self.app.longitudinal.Outcome(details={'nested_producer_bytes_verified_without_journal_reentry': True})
        with mock.patch.object(self.bridge.journal, 'reference', wraps=actual_resolver) as resolver:
            self.completed(self.bridge.stage(self.cell, 'extract', callback, (capture,)))
            self.assertEqual(resolver.call_count, 2)
        self.assertIsNone(self.bridge._callback_references)

    def test_missing_producer_is_rejected_before_callback(self):
        self.completed(self.bridge.retrieve(self.cell))
        self.completed(self.bridge.act(self.cell))
        path = self.bridge.area(self.cell) / 'capture.json'
        missing = self.app.longitudinal.Reference(self.bridge.journal.run_id, self.cell, 'capture', self.app.longitudinal.Artifact(path.relative_to(self.bridge.root).as_posix(), '0' * 64), '0' * 64)
        callback = mock.Mock()
        with self.assertRaises(self.app.longitudinal.ReferenceError):
            self.bridge.stage(self.cell, 'extract', callback, (missing,))
        callback.assert_not_called()

    def test_forged_completed_producer_is_rejected_before_callback(self):
        self.completed(self.bridge.retrieve(self.cell))
        self.completed(self.bridge.act(self.cell))
        reference = self.bridge.ref(self.cell, 'act', self.bridge.area(self.cell) / 'task-output.jsonl')
        forged = self.app.longitudinal.Reference(reference.run_id, reference.cell, reference.stage, reference.artifact, '0' * 64)
        callback = mock.Mock()
        with self.assertRaises(self.app.longitudinal.ReferenceError):
            self.bridge.stage(self.cell, 'capture', callback, (forged,))
        callback.assert_not_called()

    def test_missing_authenticated_snapshot_cannot_validate_current_bytes(self):
        self.completed(self.bridge.retrieve(self.cell))
        self.completed(self.bridge.act(self.cell))
        reference = self.bridge.ref(self.cell, 'act', self.bridge.area(self.cell) / 'task-output.jsonl')

        def callback(context):
            authenticated = self.bridge._callback_references
            try:
                self.bridge._callback_references = frozenset()
                with self.assertRaises(BridgeError):
                    self.bridge.exact(self.cell, 'act', reference)
            finally:
                self.bridge._callback_references = authenticated
            return self.app.longitudinal.Outcome()
        self.completed(self.bridge.stage(self.cell, 'capture', callback, (reference,)))
        self.assertIsNone(self.bridge._callback_references)

    def test_forged_callback_reference_cannot_borrow_authenticated_snapshot(self):
        self.completed(self.bridge.retrieve(self.cell))
        self.completed(self.bridge.act(self.cell))
        reference = self.bridge.ref(self.cell, 'act', self.bridge.area(self.cell) / 'task-output.jsonl')
        forged = self.app.longitudinal.Reference(reference.run_id, reference.cell, reference.stage, reference.artifact, '0' * 64)

        def callback(context):
            with self.assertRaises(BridgeError):
                self.bridge.exact(self.cell, 'act', forged)
            return self.app.longitudinal.Outcome()
        self.completed(self.bridge.stage(self.cell, 'capture', callback, (reference,)))

    def test_unchanged_recursion_policy_still_denies_real_public_self_capture(self):
        with self.bridge.owned_config(self.cell):
            source = self.bridge.area(self.cell) / 'public-recursive-source.jsonl'
            source.parent.mkdir(parents=True, exist_ok=True)
            raw = b'{"type":"user","message":{"content":"Public self-capture probe"}}\n'
            source.write_bytes(raw)
            recursive_cwd = self.bridge.area(self.cell) / 'claude-note-internal'
            recursive_cwd.mkdir()
            publication = self.app.import_publication.publish('claude-code', 'public-recursion-probe', 'public-recursion', str(source), raw, str(recursive_cwd), 'Public recursion probe')
            events = list(self.app.queue_manager.read_events(publication.queue_path))
            self.assertTrue(all((self.app.session_tracker.is_recursive_event(event) for event in events)))
            self.assertFalse(self.app.worker.process_session('public-recursion-probe', events, logging.getLogger('public-bridge')))
            state = self.app.session_tracker.load_session_state('public-recursion-probe')
            self.assertEqual(set(state.filtered_event_ids.values()), {'recursion-policy'})
            self.assertFalse(self.app.note_writer.get_note_path(state).exists())
            diagnostic = self.bridge.capture_diagnostic(state, publication.event_ids)
            self.assertEqual(diagnostic['reason'], 'public_capture_recursion_policy')
            self.assertEqual(diagnostic['recursion_filtered_count'], 2)
            self.assertEqual(source.read_bytes(), raw)
            self.assertEqual(self.bridge.transport_calls, 0)

    def test_unexpected_capture_outcome_records_fixed_public_diagnostic(self):
        self.completed(self.bridge.retrieve(self.cell))
        self.completed(self.bridge.act(self.cell))
        reference = self.bridge.ref(self.cell, 'act', self.bridge.area(self.cell) / 'task-output.jsonl')
        with mock.patch.object(self.app.worker, 'process_session', return_value=False):
            result = self.bridge.capture(self.cell, reference)
        self.assertEqual(result.state, 'failed')
        self.assertEqual(dict(result.outcome['details']), {'reason': 'public_capture_state_unavailable', 'expected_event_count': 2, 'processed_event_count': 0, 'filtered_event_count': 0, 'recursion_filtered_count': 0, 'provider_calls': 0})
        self.assertEqual(self.bridge.transport_calls, 0)
