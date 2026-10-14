"""Synthetic evidence, claim authority, egress and extraction crash contracts."""
import hashlib
import json
import os
import tempfile
import unittest
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault('CLAUDE_NOTE_VAULT_ROOT', tempfile.mkdtemp())
from claude_note import config, document_sections, egress, knowledge_pack, synthesizer, transcript_reader, verified_claims
from claude_note import ingest, models, session_tracker, vault_indexer


class EvidenceContracts(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for name, value in [('VAULT_ROOT', self.root), ('STATE_DIR', self.root / 'state')]:
            patcher = mock.patch.object(config, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def transcript(self, records):
        path = self.root / 'session.jsonl'
        path.write_text(''.join(json.dumps(r) + '\n' for r in records))
        return path

    def test_decisive_read_and_exit_tail_and_unknown_are_preserved(self):
        path = self.transcript([
            {'type': 'session_meta', 'payload': {'id': 's'}},
            {'type': 'response_item', 'payload': {'type': 'function_call', 'name': 'Read', 'call_id': 'r', 'arguments': '{}'}},
            {'type': 'response_item', 'payload': {'type': 'function_call_output', 'call_id': 'r', 'output': 'Do not retry unknown payment outcomes.'}},
            {'type': 'response_item', 'payload': {'type': 'function_call', 'name': 'exec_command', 'call_id': 'b', 'arguments': '{}'}},
            {'type': 'response_item', 'payload': {'type': 'function_call_output', 'call_id': 'b', 'output': 'x' * 8000 + '\nProcess exited with code 1\n'}},
            {'type': 'response_item', 'payload': {'type': 'function_call', 'name': 'exec_command', 'call_id': 'missing', 'arguments': '{}'}},
        ])
        content = transcript_reader.read_transcript(path)
        self.assertIn('unknown payment', content.tool_uses[0].output_summary)
        self.assertEqual(content.tool_uses[0].outcome, 'unknown')
        self.assertFalse(content.tool_uses[1].success)
        self.assertIn('code 1', content.tool_uses[1].output_summary)
        self.assertIsNone(content.tool_uses[2].success)
        self.assertIn('#L5@', content.tool_uses[1].source_span)
        self.assertEqual(len(content.tool_uses[1].output), 8028)

    def test_attachment_wrapper_keeps_request_and_record_identity(self):
        path = self.transcript([
            {'type': 'session_meta', 'payload': {'id': 's'}},
            {'timestamp': '2026-01-01T00:00:00Z', 'type': 'response_item', 'payload': {'id': 'm1', 'parent_id': 'p1',
                'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': '# Files mentioned by the user:\nsource.md\n## My request:\nKeep the late exception.'}]}},
        ])
        content = transcript_reader.read_transcript(path)
        self.assertEqual(content.user_prompts, ['Keep the late exception.'])
        self.assertEqual((content.source_events[1].event_id, content.source_events[1].parent_id), ('m1', 'p1'))
        self.assertEqual(content.source_events[1].observed_at, '2026-01-01T00:00:00Z')

    def test_middle_correction_survives_prompt_view(self):
        request = 'a' * 350 + ' CORRECTION: do not rotate credentials ' + 'z' * 350
        self.assertIn('do not rotate credentials', synthesizer._format_user_prompts([request]))

    def test_append_checkpoint_keeps_only_delta_and_revised_source_replays(self):
        path = self.transcript([{'type': 'user', 'message': {'content': 'old turn'}}])
        raw = path.read_bytes()
        state = SimpleNamespace(transcript_path=str(path), transcript_checkpoint={'bytes': len(raw), 'lines': 1,
            'revision': hashlib.sha256(raw).hexdigest()})
        with path.open('a') as handle:
            handle.write(json.dumps({'type': 'user', 'message': {'content': 'new correction'}}) + '\n')
        content = transcript_reader.read_transcript_from_state(state)
        self.assertEqual(content.user_prompts, ['new correction'])
        self.assertEqual((content.source_events[0].line, content.source_events[0].origin_line), (1, 2))
        path.write_text(path.read_text().replace('old turn', 'edit old'))
        self.assertEqual(transcript_reader.read_transcript_from_state(state).coverage['checkpoint'], 'source-changed-full-replay')

    def test_catalog_is_bounded(self):
        index = vault_indexer.VaultIndex(notes={str(i): vault_indexer.NoteIndex(f'note-{i}.md', f'note {i}', tags=[str(i)]) for i in range(10000)})
        with mock.patch.object(egress, 'audiences', return_value={'model'}), \
             mock.patch.object(egress, 'prepare', return_value='') as prepare:
            view = synthesizer._format_vault_summary(index, 'note 9999')
        self.assertLessEqual(len(view), 8000)
        self.assertIn('note-9999.md', view)
        self.assertEqual(prepare.call_count, 100)

    def test_catalog_filters_artifact_and_lineage_controls_before_metadata(self):
        public = self.root / 'public.md'
        public.write_text('Synthetic public evidence')
        host = self.root / 'host.md'
        host.write_text(verified_claims.Claim('host', 'Synthetic host-only fact').marker())
        private = self.root / 'private.md'
        private.write_text('---\nprivate: true\n---\nSynthetic private evidence')
        derived = self.root / 'derived.md'
        derived.write_text('---\nderivation_sources:\n  - ' + json.dumps(str(private)) + '\n---\nSynthetic derivative')
        index = vault_indexer.VaultIndex(notes={path.name: vault_indexer.NoteIndex(
            path.name, path.stem, tags=[path.stem + '-tag']) for path in (public, host, private, derived)})
        sources = set()
        with mock.patch.object(egress, 'policy', return_value={'default': ['host', 'model']}):
            view = synthesizer._format_vault_summary(index, sources=sources)
            self.assertEqual(egress.prepare(view, audience='model', sources=sources), view)
        self.assertIn('public.md', view)
        self.assertIn('public-tag', view)
        self.assertIn('Eligible notes in this bounded catalog: 1.', view)
        for name in ('host', 'private', 'derived'):
            self.assertNotIn(name + '.md', view)
            self.assertNotIn(name + '-tag', view)
        self.assertEqual(sources, {str(public)})

    def test_schema_and_source_support_reject_before_mutation(self):
        content = transcript_reader.TranscriptContent('real', source_path='/public/transcript')
        result = SimpleNamespace(returncode=0, stdout=json.dumps({'session_id': 'invented', 'date': 'bad', 'title': 't',
            'note_ops': [{'op': 'delete', 'path': '../outside.md', 'body_markdown': 'bad'}]}), stderr='')
        with mock.patch.object(synthesizer, 'build_synthesis_prompt', return_value='public'), \
             mock.patch.object(egress, 'prepare', side_effect=lambda text, **kw: text), \
             mock.patch.object(synthesizer.subprocess, 'run', return_value=result):
            with self.assertRaises(ValueError):
                synthesizer.synthesize_session(content, vault_indexer.VaultIndex())

    def test_cached_extraction_survives_post_model_crash(self):
        path = self.transcript([{'type': 'user', 'message': {'content': 'public question'}}])
        state = SimpleNamespace(transcript_path=str(path), cwd='/public', transcript_checkpoint={})
        pack = knowledge_pack.KnowledgePack('session', '2026-01-01', 'public lesson', highlights=['observed'])
        pack.source_revision = hashlib.sha256(path.read_bytes()).hexdigest()
        pack.coverage = {'snapshot_revision': pack.source_revision, 'snapshot_bytes': path.stat().st_size, 'snapshot_lines': 1}
        with mock.patch.object(synthesizer, 'synthesize_session', return_value=pack) as call:
            first = synthesizer.synthesize_from_state(state, vault_indexer.VaultIndex())
            second = synthesizer.synthesize_from_state(state, vault_indexer.VaultIndex())
        self.assertEqual(call.call_count, 1)
        self.assertEqual(first.generation, second.generation)

    def test_cache_binds_source_and_session_even_for_identical_bytes(self):
        first = self.transcript([{'type': 'user', 'message': {'content': 'same bytes'}}])
        second = self.root / 'another.jsonl'
        second.write_bytes(first.read_bytes())
        def extract(content, *args, **kwargs):
            return knowledge_pack.KnowledgePack(content.session_id, '2026-01-01', 'public', source_revision=content.source_revision)
        with mock.patch.object(synthesizer, 'synthesize_session', side_effect=extract) as call:
            a = synthesizer.synthesize_from_state(SimpleNamespace(transcript_path=str(first), cwd='/a', transcript_checkpoint={}), vault_indexer.VaultIndex())
            b = synthesizer.synthesize_from_state(SimpleNamespace(transcript_path=str(second), cwd='/b', transcript_checkpoint={}), vault_indexer.VaultIndex())
        self.assertEqual(call.call_count, 2)
        self.assertEqual((a.session_id, b.session_id), ('session', 'another'))
        self.assertNotEqual(a.generation, b.generation)

    def test_partial_jsonl_tail_is_not_checkpointed_or_lost(self):
        path = self.transcript([{'type': 'user', 'message': {'content': 'old'}}])
        complete = path.read_bytes()
        tail = json.dumps({'type': 'user', 'message': {'content': 'late correction'}}).encode()
        path.write_bytes(complete + tail[:25])
        content = transcript_reader.read_transcript(path)
        self.assertEqual(content.coverage['snapshot_bytes'], len(complete))
        state = SimpleNamespace(transcript_path=str(path), transcript_checkpoint={'bytes': len(complete), 'lines': 1,
            'revision': hashlib.sha256(complete).hexdigest()})
        path.write_bytes(complete + tail + b'\n')
        self.assertEqual(transcript_reader.read_transcript_from_state(state).user_prompts, ['late correction'])

    def test_generated_prompt_intersects_tool_input_source_policy(self):
        private = self.root / 'private.md'
        private.write_text('ordinary confidential business facts')
        content = transcript_reader.TranscriptContent('s', source_path='/allowed/transcript',
            tool_uses=[transcript_reader.ToolUse('Read', {'file_path': str(private)}, private.read_text())])
        rules = {'rules': [{'source': '/allowed/*', 'audiences': ['host', 'model']}]}
        with mock.patch.object(egress, 'policy', return_value=rules), \
             mock.patch.object(synthesizer, 'build_synthesis_prompt', return_value=private.read_text()), \
             mock.patch.object(synthesizer.subprocess, 'run') as call:
            with self.assertRaises(egress.EgressDenied):
                synthesizer.synthesize_session(content, vault_indexer.VaultIndex())
        call.assert_not_called()

    def test_recursive_queue_records_are_durably_filtered(self):
        from claude_note import queue_manager
        with mock.patch.object(config, 'QUEUE_DIR', self.root / 'queue'):
            event = models.QueuedEvent.from_hook_input({'session_id': 's', 'cwd': str(config.CLAUDE_NOTE_DIR), 'hook_event_name': 'Stop'})
            # Use an actual configured recursion marker independent of vault.
            event.cwd = config.RECURSION_MARKERS[0]
            queue_manager.enqueue_event(event)
            state = session_tracker.update_session_from_events('s', [event])
            session_tracker.save_session_state(state)
            self.assertEqual(list(queue_manager.read_pending_events()), [])
            self.assertEqual(state.filtered_event_ids[event.event_id], 'recursion-policy')

    def test_malformed_typed_queue_record_does_not_starve_next_record(self):
        from claude_note import queue_manager
        with mock.patch.object(config, 'QUEUE_DIR', self.root / 'queue'):
            self.root.joinpath('queue').mkdir()
            bad = {'event_id': 'bad', 'ts': '2026-01-01T00:00:00Z', 'event': 'Stop', 'session_id': [], 'cwd': '/', 'transcript_path': '', 'data': {}}
            good = models.QueuedEvent.from_hook_input({'session_id': 'good', 'hook_event_name': 'Stop'})
            queue_manager.get_queue_file().write_text(json.dumps(bad) + '\n' + good.to_json() + '\n')
            self.assertEqual([e.session_id for e in queue_manager.read_pending_events()], ['good'])

    def test_exhausted_extraction_retries_park(self):
        state = models.SessionState('s', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', synthesis_pending=True)
        for _ in range(8):
            session_tracker.run_pending_synthesis(state, lambda state: False, force=True)
        self.assertTrue(state.synthesis_parked)
        self.assertEqual(session_tracker.get_pending_synthesis_sessions(), [])

    def test_sections_are_lossless_and_keep_late_qualification(self):
        text = ('early statement\n' * 10000) + '\n# Qualification\nOnly works on version 7, not version 8.'
        parts = document_sections.sections(text)
        self.assertEqual(''.join(p['text'] for p in parts), text)
        self.assertTrue(all(len(p['text']) <= 24000 for p in parts))
        self.assertIn('not version 8', parts[-1]['text'])

    def test_section_extraction_resumes_and_preserves_coverage(self):
        text = 'x' * 30000 + ' late qualifier'
        def extract(content, *args, **kwargs):
            return {'source_summary': content[-20:], 'notes': [{'slug': 'same', 'summary': content[-20:]}]}
        with mock.patch.object(ingest, '_extract_section_knowledge', side_effect=extract) as call:
            first = ingest.extract_knowledge(text, 'Public', 'public.md')
            second = ingest.extract_knowledge(text, 'Public', 'public.md')
        self.assertEqual(call.call_count, 2)
        self.assertTrue(first['coverage']['complete'])
        self.assertEqual(first['coverage']['covered_characters'], len(text))
        self.assertIn('late qualifier', second['notes'][-1]['summary'])

    def test_claim_approval_text_cannot_forge_authority_or_supersede(self):
        approved = verified_claims.Claim('old', 'Use version 7', status='approved', scope={'repository': '/repo'})
        proposed = verified_claims.Claim('new', 'Use version 8', status='approved', supersedes=['old'])
        evidence = self.root / 'check.md'
        evidence.write_text('verified exit 0')
        (self.root / 'claims').mkdir()
        (self.root / 'claims/old.md').write_text(approved.marker())
        verified_claims.attest(approved, vault_root=self.root, authority='approved', evidence=[{'path': 'check.md',
            'revision': hashlib.sha256(evidence.read_bytes()).hexdigest()}])
        groups = verified_claims.applicability(approved.marker() + '\n' + proposed.marker(), vault_root=self.root, repository='/repo')
        self.assertEqual([c['claim_id'] for c in groups['accepted']], ['old'])
        self.assertEqual([c['claim_id'] for c in groups['proposed']], ['new'])
        evidence.write_text('changed environment; old evidence invalid')
        self.assertEqual(verified_claims.authority(approved, self.root), 'unverified')
        evidence.unlink()
        self.assertEqual(verified_claims.authority(approved, self.root), 'unverified')

    def test_cross_note_dependencies_and_supersession_require_current_authority(self):
        (self.root / 'claims').mkdir()
        evidence = self.root / 'check.md'
        evidence.write_text('verified')
        def attest(claim):
            (self.root / 'claims' / (claim.claim_id + '.md')).write_text(claim.marker())
            verified_claims.attest(claim, vault_root=self.root, authority='verified', evidence=[{'path': 'check.md',
                'revision': hashlib.sha256(evidence.read_bytes()).hexdigest()}])
        old = verified_claims.Claim('old', 'old procedure')
        broken = verified_claims.Claim('broken', 'new procedure', depends_on=['missing'], supersedes=['old'])
        dependent = verified_claims.Claim('dependent', 'use new procedure', depends_on=['broken'])
        for claim in (old, broken, dependent):
            attest(claim)
        self.assertEqual([r['claim_id'] for r in verified_claims.applicability(old.marker(), vault_root=self.root)['accepted']], ['old'])
        self.assertTrue(verified_claims.applicability(dependent.marker(), vault_root=self.root)['denied'])

    def test_derived_note_cannot_publish_when_original_source_denies(self):
        note = '---\nderivation_sources:\n  - "/private/original"\n---\nDerived confidential business information'
        rules = {'rules': [{'source': '/generated/*', 'audiences': ['host', 'staging']}]}
        with self.assertRaises(egress.EgressDenied):
            egress.prepare(note, audience='staging', sources=['/generated/a.md'], rules=rules)

    def test_transitive_and_recorded_lineage_survive_authored_metadata_removal(self):
        root = self.root.resolve()
        origin, derived = root / 'private.md', root / 'derived.md'
        origin.write_text('ordinary confidential business facts')
        derived.write_text('---\nderivation_sources:\n  - ' + json.dumps(str(origin)) + '\n---\nDerived facts')
        rules = {'rules': [{'source': str(derived), 'audiences': ['model']}]}
        with self.assertRaises(egress.EgressDenied):
            egress.prepare('wrapped prompt', audience='model', sources=[str(derived)], rules=rules)
        egress.record_derivation(derived, derived.read_text(), vault_root=root)
        derived.write_text('same derived facts with the authored metadata removed')
        with self.assertRaises(egress.EgressDenied):
            egress.prepare('wrapped prompt', audience='model', sources=[str(derived)], rules=rules)

    def test_private_control_variants_fail_closed(self):
        root = self.root.resolve()
        note = root / 'private.md'
        for front in ('private: true # local only', '"private": true', 'private: true\nprivate: false',
                      '  private: true', 'private: *unknown', '"pri\\vate": false'):
            note.write_text('---\n' + front + '\n---\nConfidential facts')
            with self.subTest(front=front), self.assertRaises(egress.EgressDenied):
                egress.prepare('wrapped prompt', audience='model', sources=[str(note)], rules={'default': ['model']})

    def test_deleted_private_origin_does_not_declassify_derivation(self):
        origin, derived = self.root / 'origin.md', self.root / 'derived.md'
        origin.write_text('---\nprivate: true\n---\nConfidential facts')
        derived.write_text('---\nderivation_sources: ' + json.dumps([str(origin)]) + '\n---\nDerived facts')
        for deleted in (False, True):
            if deleted:
                origin.unlink()
            with self.subTest(deleted=deleted), self.assertRaises(egress.EgressDenied):
                egress.prepare(derived.read_text(), audience='model', sources=[str(derived)], rules={'default': ['model']})

    def test_empty_nested_ledger_cannot_shadow_outer_lineage(self):
        root = self.root.resolve()
        nested = root / 'nested'
        nested.mkdir()
        origin, derived = root / 'origin.md', nested / 'derived.md'
        origin.write_text('Confidential facts')
        derived.write_text('---\nderivation_sources: ' + json.dumps([str(origin)]) + '\n---\nDerived facts')
        egress.record_derivation(derived, derived.read_text(), vault_root=root)
        derived.write_text('Derived facts without authored metadata')
        (nested / '.claude-note').mkdir()
        rules = {'default': ['model'], 'rules': [{'source': str(origin), 'audiences': ['host'], 'restrict': True}]}
        self.assertEqual(egress.recorded_derivations(derived), [str(origin)])
        with self.assertRaises(egress.EgressDenied):
            egress.prepare(derived.read_text(), audience='model', sources=[str(derived)], rules=rules)

    def test_quoted_or_nested_derivation_key_cannot_hide_origin(self):
        root = self.root.resolve()
        origin, derived = root / 'origin.md', root / 'derived.md'
        origin.write_text('Confidential facts')
        rules = {'default': ['model'], 'rules': [{'source': str(origin), 'audiences': ['host'], 'restrict': True}]}
        for key in ('derivation_sources', '"derivation_sources"', "'derivation_sources'", 'DERIVATION_SOURCES', '  derivation_sources'):
            derived.write_text('---\n' + key + ': ' + json.dumps([str(origin)]) + '\n---\nDerived facts')
            with self.subTest(key=key), self.assertRaises(egress.EgressDenied):
                egress.prepare('Wrapped prompt', audience='model', sources=[str(derived)], rules=rules)

    def test_source_alias_cannot_bypass_canonical_restriction(self):
        origin = self.root.resolve() / 'confidential.md'
        origin.write_text('Confidential facts')
        alias = origin.with_name('CONFIDENTIAL.md')
        if not alias.exists():
            self.skipTest('Case-sensitive filesystem')
        rules = {'default': ['model'], 'rules': [{'source': str(origin), 'audiences': ['host'], 'restrict': True}]}
        with self.assertRaises(egress.EgressDenied):
            egress.prepare('Derived facts', audience='model', sources=[str(alias)], rules=rules)

    def test_immutable_delta_snapshots_survive_original_rewrite_without_quadratic_copies(self):
        path = self.transcript([{'type': 'user', 'message': {'content': 'original evidence'}}])
        state = SimpleNamespace(transcript_path=str(path), transcript_checkpoint={})
        first = transcript_reader.read_transcript_from_state(state)
        raw = path.read_bytes()
        state.transcript_checkpoint = {'bytes': len(raw), 'lines': 1, 'revision': hashlib.sha256(raw).hexdigest()}
        addition = json.dumps({'type': 'user', 'message': {'content': 'new correction'}}).encode() + b'\n'
        path.write_bytes(raw + addition)
        second = transcript_reader.read_transcript_from_state(state)
        self.assertEqual(Path(second.snapshot_path).read_bytes(), addition)
        self.assertEqual(second.source_events[0].origin_line, 2)
        path.write_text('source has compacted or been replaced')
        self.assertEqual(Path(first.snapshot_path).read_bytes(), raw)
        self.assertEqual(Path(second.snapshot_path).read_bytes(), addition)

    def test_distinct_section_operations_get_distinct_semantic_identities(self):
        path = self.transcript([{'type': 'user', 'message': {'content': 'append both sections'}}])
        content = transcript_reader.read_transcript_from_state(SimpleNamespace(transcript_path=str(path), transcript_checkpoint={}))
        anchor = content.source_events[0].anchor
        body = {'session_id': 'fake', 'date': '2026-01-01', 'title': 'public', 'note_ops': [
            {'op': 'append', 'path': 'note.md', 'body_markdown': 'same evidence', 'section': section, 'source_spans': [anchor]}
            for section in ('## A', '## B')]}
        result = SimpleNamespace(returncode=0, stdout=json.dumps(body), stderr='')
        with mock.patch.object(egress, 'policy', return_value={'default': ['model']}), \
             mock.patch.object(synthesizer, 'build_synthesis_prompt', return_value='public evidence'), \
             mock.patch.object(synthesizer.subprocess, 'run', return_value=result) as run:
            pack = synthesizer.synthesize_session(content, vault_indexer.VaultIndex())
        self.assertNotEqual(pack.note_ops[0].operation_id, pack.note_ops[1].operation_id)
        self.assertIn('--tools', run.call_args.args[0])
        self.assertEqual(run.call_args.args[0][run.call_args.args[0].index('--tools')+1], '')

    def test_snapshot_concurrent_publication_is_idempotent(self):
        from claude_note.state_io import create_bytes
        for index in range(10):
            path = self.root / 'snapshots' / f'{index}.jsonl'
            with ThreadPoolExecutor(max_workers=6) as pool:
                list(pool.map(lambda _: create_bytes(path, b'public evidence\n'), range(6)))
            self.assertEqual(path.read_bytes(), b'public evidence\n')
            self.assertEqual(path.stat().st_nlink, 1)

    def test_snapshot_post_link_crash_recovers_only_saved_publication_intent(self):
        from claude_note.state_io import create_bytes
        path = self.root / 'snapshots' / 'crash.jsonl'
        script = '''import os,sys
from pathlib import Path
from claude_note import state_io
link=os.link
def crash(*args,**kwargs):
    link(*args,**kwargs)
    os._exit(91)
state_io.os.link=crash
state_io.create_bytes(Path(sys.argv[1]),b'public evidence\\n')
'''
        env = {**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')}
        result = subprocess.run([sys.executable, '-c', script, str(path)], env=env, timeout=10)
        self.assertEqual(result.returncode, 91)
        self.assertEqual(path.stat().st_nlink, 2)
        create_bytes(path, b'public evidence\n')
        self.assertEqual(path.stat().st_nlink, 1)
        other = self.root / 'unknown.jsonl'
        os.link(path, other)
        with self.assertRaises(ValueError):
            create_bytes(other, b'public evidence\n')

    def test_claim_scope_and_expiry_deny_without_authorizing(self):
        claim = verified_claims.Claim('scoped', 'recovery', status='verified', scope={'repository': '/other'}, valid_until='2020-01-01')
        groups = verified_claims.applicability(claim.marker(), vault_root=self.root, repository='/repo')
        self.assertEqual(set(groups['denied'][0]['reason_codes']), {'scope-repository', 'expired'})
        self.assertEqual(groups['denied'][0]['verification'], 'unverified')

    def test_egress_intersects_all_sources_and_redacts_before_call(self):
        rules = {'default': ['host'], 'rules': [{'source': '/public/*', 'audiences': ['host', 'model']},
            {'source': '/public/private/*', 'audiences': ['host'], 'restrict': True}]}
        with self.assertRaises(egress.EgressDenied):
            egress.prepare('confidential merger details', audience='model', sources=['/public/a', '/private/b'], rules=rules)
        with self.assertRaises(egress.EgressDenied):
            egress.prepare('secret', audience='model', sources=['/public/private/a'], rules=rules)
        secret = 'sk-ant-' + 'A' * 32
        public = self.root.resolve() / 'public.md'
        public.write_text(secret)
        self.assertNotIn(secret, egress.prepare(secret, audience='model', sources=[str(public)], rules={'default': ['model']}))
        with mock.patch.dict(os.environ, {'DATABASE_URL': 'private', 'AWS_SECRET_ACCESS_KEY': 'private'}):
            env = egress.model_env()
        self.assertNotIn('DATABASE_URL', env)
        self.assertNotIn('AWS_SECRET_ACCESS_KEY', env)


if __name__ == '__main__':
    unittest.main()
