"""Real filesystem boundary regressions; synthetic vaults and no external tools."""
import json
import os
import subprocess
import sys
import tempfile
import threading
import traceback
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

os.environ.setdefault('CLAUDE_NOTE_VAULT_ROOT', tempfile.mkdtemp(prefix='mutation-import-'))

from claude_note import config, knowledge_pack, managed_blocks, mutation, note_router, provenance, remember


class MutationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.vault = self.root / 'vault'
        self.vault.mkdir()
        for name, value in [('VAULT_ROOT', self.vault), ('STATE_DIR', self.vault / '.claude-note/state'),
                            ('INBOX_PATH', self.vault / 'claude-note-inbox.md')]:
            patch = mock.patch.object(config, name, value)
            patch.start(); self.addCleanup(patch.stop)
        patch = mock.patch.object(provenance, 'author_email', return_value='research@example.invalid')
        patch.start(); self.addCleanup(patch.stop)

    def pending(self):
        return [path for path in (self.vault / '.claude-note/mutations/pending').glob('*.json')
                if len(path.stem) == 64 and all(char in '0123456789abcdef' for char in path.stem)]

    def test_final_parent_and_dangling_symlinks_never_escape(self):
        outside = self.root / 'outside'; outside.mkdir()
        external = outside / 'rule.md'; external.write_text('HUMAN OUTSIDE')
        (self.vault / 'rule.md').symlink_to(external)
        (self.vault / 'linked').symlink_to(outside, target_is_directory=True)
        (self.vault / 'dangling.md').symlink_to(outside / 'absent.md')
        for title, folder in [('Rule', ''), ('Fresh', 'linked'), ('Dangling', '')]:
            with self.subTest(title=title), self.assertRaises(ValueError):
                remember.write_note(self.vault, 'model addition', title=title, folder=folder)
        self.assertEqual(external.read_text(), 'HUMAN OUTSIDE')
        self.assertFalse((outside / 'fresh.md').exists())
        self.assertFalse((outside / 'absent.md').exists())

    def test_metadata_directory_and_hard_links_are_rejected(self):
        outside = self.root / 'outside'; outside.mkdir()
        (self.vault / '.claude-note').symlink_to(outside, target_is_directory=True)
        with self.assertRaises((ValueError, OSError)):
            mutation.create_note(self.vault / 'fresh.md', 'text', vault_root=self.vault)
        self.assertEqual(list(outside.iterdir()), [])
        (self.vault / '.claude-note').unlink()
        source = outside / 'source.md'; source.write_text('outside')
        os.link(source, self.vault / 'hard.md')
        with self.assertRaises(ValueError):
            mutation.mutate('hard.md', lambda old: old + 'append', vault_root=self.vault)
        self.assertEqual(source.read_text(), 'outside')

    def test_predictable_temporary_symlink_is_never_used(self):
        external = self.root / 'external.md'; external.write_text('outside')
        note = self.vault / 'rule.md'; note.write_text('# Human\n')
        (self.vault / 'rule.md.tmp').symlink_to(external)
        self.assertTrue(managed_blocks.write_managed_block(note, 'new', 'evidence', True))
        self.assertEqual(external.read_text(), 'outside')
        self.assertFalse(note.is_symlink())
        self.assertIn('evidence', note.read_text())

    def test_threaded_remember_additions_are_all_retained(self):
        note = self.vault / 'parallel.md'; note.write_text('# Human text\n')
        failures = []
        def write(index):
            try:
                remember.write_note(self.vault, f'LESSON_{index:02d}', title='Parallel', assistant='human')
            except Exception: failures.append(traceback.format_exc())
        writers = [threading.Thread(target=write, args=(i,)) for i in range(12)]
        for thread in writers: thread.start()
        for thread in writers: thread.join(10)
        self.assertFalse(any(t.is_alive() for t in writers))
        self.assertEqual(failures, [])
        content = note.read_text()
        self.assertTrue(content.startswith('# Human text\n'))
        for index in range(12): self.assertEqual(content.count(f'LESSON_{index:02d}'), 1)

    def test_process_writers_share_the_canonical_note_lock(self):
        note = self.vault / 'process.md'; note.write_text('# Human\n')
        source = str(Path(mutation.__file__).parents[1])
        script = '''import os,sys
from pathlib import Path
from unittest import mock
sys.dont_write_bytecode=True
sys.path.insert(0,sys.argv[1])
vault=Path(sys.argv[2])
with mock.patch.object(Path,'home',return_value=vault.parent/'synthetic-home'):
 from claude_note import remember
 remember.write_note(vault,sys.argv[3],title='Process',assistant='human')
'''
        env = dict(os.environ, CLAUDE_NOTE_VAULT=str(self.vault),
                   XDG_CONFIG_HOME=str(self.root / 'config'), CLAUDE_NOTE_AUTHOR='synthetic@example.invalid')
        children = [subprocess.Popen([sys.executable, '-B', '-c', script, source, str(self.vault), f'PROCESS_{i}'],
                                    env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE) for i in range(6)]
        for child in children:
            stdout, stderr = child.communicate(timeout=15)
            self.assertEqual(child.returncode, 0, (stdout, stderr))
        content = note.read_text()
        for index in range(6): self.assertEqual(content.count(f'PROCESS_{index}'), 1)

    def test_concurrent_router_replay_is_one_effect(self):
        note = note_router.create_note('replay', {'type': 'gotcha'}, '# Original\n\n## Lessons\n', self.vault)
        op = knowledge_pack.NoteOp('append', 'replay', 'UNIQUE_EFFECT', section='## Lessons')
        outcomes = []
        threads = [threading.Thread(target=lambda: outcomes.append(note_router.apply_note_op(op, self.vault, 'session')))
                   for _ in range(10)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(10)
        self.assertEqual(outcomes, [True] * 10)
        self.assertEqual(note.read_text().count('UNIQUE_EFFECT'), 1)

    def test_same_second_ids_distinguish_lessons_and_replays(self):
        now = datetime(2026, 10, 13, 12, 0, 0)
        note, _ = remember.write_note(self.vault, 'First', title='Timing', now=now, assistant='human')
        remember.write_note(self.vault, 'Second', title='Timing', now=now, assistant='human')
        remember.write_note(self.vault, 'Second', title='Timing', now=now, assistant='human')
        ids = managed_blocks.list_managed_blocks(note)
        self.assertEqual(len(ids), 2)
        self.assertEqual(len(set(ids)), 2)
        self.assertEqual(note.read_text().count('Second'), 1)

    def test_unreceipted_remember_marker_requires_complete_matching_lesson(self):
        note = self.vault / 'foo.md'
        body = 'A complete synthetic lesson that has not been recorded.'
        start = '<!-- claude-note:remember-replay-id:start -->'
        end = '<!-- claude-note:remember-replay-id:end -->'
        for existing in ('Human prose\n' + start + '\npartial text\n',
                         'Human prose\n' + start + '\nChanged human correction\n' + end + '\n'):
            note.write_text(existing)
            result = remember.write_note_result(self.vault, body, title='Foo',
                                                operation_id='replay-id', assistant='codex')
            self.assertEqual(result.status, 'staged')
            self.assertEqual(note.read_text(), existing)
            self.assertIn(body, json.loads(result.staged_path.read_text())['content'])
        note.write_text('Human prose\n' + start + '\n' + body + '\n' + end + '\n')
        self.assertEqual(remember.write_note_result(self.vault, body, title='Foo',
                         operation_id='replay-id', assistant='codex').status, 'unchanged')

    def test_unknown_human_replacement_stages_even_with_matching_hash(self):
        note = self.vault / 'human.md'; note.write_text('# Human decision\n')
        result = mutation.mutate(note, lambda old: '# Model decision\n', vault_root=self.vault,
                                 expected_revision=mutation.revision(note.read_text()))
        self.assertEqual(result.status, 'staged')
        self.assertEqual(note.read_text(), '# Human decision\n')
        self.assertIn('# Model decision', json.loads(result.staged_path.read_text())['content'])

    def test_changed_managed_block_never_erases_a_correction_by_default(self):
        note = self.vault / 'human.md'; note.write_text('# Human prose\n')
        self.assertTrue(managed_blocks.write_managed_block(note, 'synth-findings', 'original', True))
        note.write_text(note.read_text().replace('original', 'HUMAN CORRECTION'))
        self.assertFalse(managed_blocks.write_managed_block(note, 'synth-findings', 'new model claim', True))
        self.assertIn('HUMAN CORRECTION', note.read_text())
        self.assertNotIn('new model claim', note.read_text())
        self.assertEqual(len(self.pending()), 1)

    def test_owned_file_replacement_requires_explicit_block_base(self):
        note = note_router.create_note('owned', {'type': 'gotcha'}, '# Owned\n', self.vault)
        self.assertTrue(managed_blocks.write_managed_block(note, 'machine', 'old', True))
        result = managed_blocks.write_managed_block_result(note, 'machine', 'new', True)
        self.assertEqual(result.status, 'staged')
        base = mutation.revision(note.read_text())
        self.assertTrue(managed_blocks.write_managed_block(note, 'machine', 'new', True, expected_revision=base))
        self.assertEqual(managed_blocks.read_managed_block(note, 'machine'), 'new')

    def test_external_edit_during_transform_is_preserved_and_conflicted(self):
        note = note_router.create_note('owned', {'type': 'gotcha'}, '# Original\n', self.vault)
        def transform(old):
            note.write_text(old + 'HUMAN EDIT\n')  # deterministic external edit interleaving
            return '# Model replacement\n'
        result = mutation.mutate(note, transform, vault_root=self.vault)
        self.assertEqual(result.status, 'staged')
        self.assertIn('HUMAN EDIT', note.read_text())
        self.assertNotIn('Model replacement', note.read_text())

    def test_human_edit_invalidates_machine_ownership(self):
        note = note_router.create_note('owned', {'type': 'gotcha'}, '# Original\n', self.vault)
        note.write_text(note.read_text() + 'Human correction\n')
        result = mutation.mutate(note, lambda old: '# Replacement\n', vault_root=self.vault)
        self.assertEqual(result.status, 'staged')
        self.assertIn('Human correction', note.read_text())

    def test_post_commit_crash_recovers_without_reapplying_callback(self):
        original = mutation._metadata
        fired = False
        def fail_receipt(root, category, key, value=None):
            nonlocal fired
            if category == 'effects' and value is not None and not fired:
                fired = True
                raise OSError('injected crash after file commit')
            return original(root, category, key, value)
        with mock.patch.object(mutation, '_metadata', fail_receipt), self.assertRaises(OSError):
            mutation.create_note('crash.md', '# Committed\n', vault_root=self.vault, operation_id='stable-op')
        callback = mock.Mock(side_effect=AssertionError('must not rerun after recovered commit'))
        result = mutation.mutate('crash.md', callback, vault_root=self.vault, operation_id='stable-op')
        self.assertEqual(result.status, 'unchanged')
        callback.assert_not_called()
        self.assertEqual((self.vault / 'crash.md').read_text(), '# Committed\n')

    def test_partial_append_stages_saved_intent_instead_of_repeating_effect(self):
        note = self.vault / 'partial.md'; note.write_text('# Human original\n')
        original_write = mutation._write_all
        def partial_write(fd, data):
            if os.fstat(fd).st_ino == note.stat().st_ino:
                os.write(fd, data[:5])
                raise OSError('injected interrupted append')
            return original_write(fd, data)
        transform = lambda old: old + 'COMPLETE ADDITION\n'
        with mock.patch.object(mutation, '_write_all', partial_write), self.assertRaises(OSError):
            mutation.mutate(note, transform, vault_root=self.vault, operation_id='append-operation')
        result = mutation.mutate(note, transform, vault_root=self.vault, operation_id='append-operation')
        self.assertEqual(result.status, 'staged')
        self.assertEqual(note.read_text(), '# Human original\nCOMPL')
        self.assertIn('COMPLETE ADDITION', json.loads(result.staged_path.read_text())['content'])

    def test_router_reports_staging_separately_from_successful_updates(self):
        note = self.vault / 'human.md'; note.write_text('# Human\n\n## Evidence\n\nOld\n\n## End\n')
        op = knowledge_pack.NoteOp('append', 'human', 'candidate', section='## Evidence')
        pack = knowledge_pack.KnowledgePack('session', '2026-10-13', 'test', note_ops=[op])
        result = note_router.apply_note_ops(pack, 'route', self.vault)
        self.assertEqual(result['notes_updated'], [])
        self.assertEqual(result['notes_created'], [])
        self.assertEqual(result['errors'], [])
        self.assertEqual(len(result['notes_staged']), 1)
        self.assertTrue(Path(result['notes_staged'][0]['pending_path']).is_file())
        self.assertNotIn('candidate', note.read_text())

    def test_protected_remember_routes_and_multiline_metadata(self):
        for folder, title in [('templates', 'Fresh'), ('sessions', 'Fresh'), ('.hidden', 'Fresh'), ('../outside', 'Fresh'), ('', 'CLAUDE')]:
            with self.subTest(folder=folder, title=title), self.assertRaises(ValueError):
                remember.write_note(self.vault, 'lesson', title=title, folder=folder)
        note, _ = remember.write_note(self.vault, 'lesson', title='Normal\nstatus: approved',
                                      tags=['good', 'bad\nshare: true'], assistant='human\napproved: true')
        fm = note.read_text().split('\n---', 1)[0]
        self.assertNotIn('\nstatus: approved', fm)
        self.assertNotIn('\nshare: true', fm)
        self.assertNotIn('\napproved: true', fm)

    def test_bounded_regular_file_contract(self):
        note = self.vault / 'large.md'; note.write_text('x' * 2048)
        with mock.patch.object(mutation, 'MAX_NOTE_BYTES', 1024), self.assertRaises(ValueError):
            mutation.mutate(note, lambda old: old + 'new', vault_root=self.vault)
        fifo = self.vault / 'fifo.md'; os.mkfifo(fifo)
        with self.assertRaises(ValueError): mutation.read_text(fifo, vault_root=self.vault)

    def test_freshness_snapshot_is_read_only_and_acknowledges_captured_generation(self):
        from claude_note import freshness
        self.assertEqual(freshness.snapshot(self.vault)['saved_generation'], 0)
        self.assertFalse((self.vault / '.claude-note').exists())
        mutation.create_note('first.md', 'first', vault_root=self.vault, operation_id='first')
        captured = freshness.snapshot(self.vault)['saved_generation']
        mutation.create_note('second.md', 'second', vault_root=self.vault, operation_id='second')
        freshness.acknowledge(captured, capability='lexical', vault_root=self.vault)
        result = freshness.acknowledge(captured, capability='vector', vault_root=self.vault)
        self.assertNotIn('first.md', result['dirty'])
        self.assertIn('second.md', result['dirty'])
        self.assertGreater(result['saved_generation'], result['lexical_generation'])
        with self.assertRaises(ValueError):
            freshness.acknowledge(result['saved_generation'] + 1, vault_root=self.vault)

    def test_interrupted_physical_write_remains_dirty(self):
        from claude_note import freshness
        note = self.vault / 'crash-dirty.md'; note.write_text('old')
        original = mutation._write_all
        def crash(fd, data):
            if os.fstat(fd).st_ino == note.stat().st_ino:
                raise OSError('interrupted physical append')
            return original(fd, data)
        with mock.patch.object(mutation, '_write_all', crash), self.assertRaises(OSError):
            mutation.mutate(note, lambda old: old + ' addition', vault_root=self.vault, operation_id='dirty-crash')
        state = freshness.snapshot(self.vault)
        self.assertEqual(state['dirty']['crash-dirty.md']['status'], 'pending')
        self.assertEqual(state['saved_generation'], 0)

    def test_session_owned_updates_and_manual_corrections_stage(self):
        from claude_note import models, note_writer
        state = models.SessionState('session1', '2026-10-13T00:00:00Z', '2026-10-13T00:01:00Z')
        note = note_writer.write_session_note(state)
        state.events = [{'ts': '2026-10-13T00:01:00Z', 'event': 'Stop', 'description': 'NEW TIMELINE'}]
        note_writer.update_session_note(state)
        self.assertIn('NEW TIMELINE', note.read_text())
        note.write_text(note.read_text().replace('NEW TIMELINE', 'HUMAN TIMELINE CORRECTION'))
        state.events[0]['description'] = 'MODEL SECOND VERSION'
        with self.assertRaises(mutation.MutationConflict) as staged:
            note_writer.update_session_note(state)
        self.assertIn('HUMAN TIMELINE CORRECTION', note.read_text())
        self.assertIn('MODEL SECOND VERSION', json.loads(staged.exception.result.staged_path.read_text())['content'])

    def test_concurrent_question_additions_deduplicate_without_rewriting_human_text(self):
        from claude_note import models, open_questions
        path = self.vault / 'open-questions.md'; path.write_text('Human question header.\n')
        state = models.SessionState('questions', '2026-10-13T00:00:00Z', '2026-10-13T00:00:00Z')
        with mock.patch.object(config, 'OPEN_QUESTIONS_FILE', path):
            failures = []
            barrier = threading.Barrier(6)
            def add(index):
                try:
                    barrier.wait(timeout=10)
                    open_questions.append_questions_to_open_questions(state, [f'Unique question {index}?', 'Shared question?'])
                except Exception:
                    failures.append(traceback.format_exc())
            threads = [threading.Thread(target=add, args=(index,)) for index in range(6)]
            for thread in threads: thread.start()
            for thread in threads: thread.join(10)
        self.assertEqual(failures, [])
        text = path.read_text()
        self.assertTrue(text.startswith('Human question header.\n'))
        self.assertEqual(text.count('Shared question?'), 1)
        for index in range(6): self.assertEqual(text.count(f'Unique question {index}?'), 1)

    def test_question_classifier_does_not_egress_without_source_authorization(self):
        from claude_note import egress, open_questions
        with mock.patch.object(egress, 'policy', return_value={}), mock.patch.object(open_questions.subprocess, 'run') as run:
            self.assertEqual(open_questions.filter_questions_with_llm(['What makes a webhook retry idempotent?']),
                             ['What makes a webhook retry idempotent?'])
        run.assert_not_called()

    def test_capped_concept_source_is_preserved_linked_and_replay_safe(self):
        from claude_note import ingest
        note = self.vault / 'lit-retries.md'
        note.write_text('---\ntype: literature\nsource: "[[original]]"\n---\nHuman correction: retries are unsafe.\n')
        concept = {'slug': 'retries', 'summary': 'Fresh conflicting evidence', 'details': 'Observed failed retry',
                   'source_revision': 'source-hash', 'source_spans': ['section:2']}
        with mock.patch.object(config, 'INGEST_MAX_SOURCES_PER_CONCEPT', 1), mock.patch.object(ingest.subprocess, 'run') as run:
            output = ingest._merge_concept_sources(note, concept, 'Study B', source_note=self.vault / 'lit-study-b.md')
            replay = ingest._merge_concept_sources(note, concept, 'Study B', source_note=self.vault / 'lit-study-b.md')
        run.assert_not_called()
        self.assertEqual(output, replay)
        self.assertEqual(len(list(self.vault.glob('lit-retries-source-*.md'))), 1)
        self.assertIn('Fresh conflicting evidence', output.read_text())
        self.assertIn('section:2', output.read_text())
        self.assertIn('Human correction: retries are unsafe.', note.read_text())
        self.assertEqual(note.read_text().count(output.stem), 1)

    def test_merge_assessment_checks_every_source_and_retains_original_evidence(self):
        from claude_note import egress, ingest
        existing = self.vault / 'lit-rule.md'; existing.write_text('---\ntype: literature\n---\nHUMAN RULE\n')
        source = self.vault / 'lit-study.md'
        source.write_text('Original source qualification and exact boundary condition.')
        concept = {'slug': 'rule', 'summary': 'ORIGINAL SOURCE QUALIFICATION', 'details': 'Exact boundary condition',
                   'source_spans': ['section:1'], 'source_revision': 'hash'}
        result = mock.Mock(returncode=0, stdout='{"has_new_info": true, "new_info_summary": "Proposed assessment"}')
        rules = {'rules': [{'source': str(existing), 'audiences': ['model']}]}
        with mock.patch.object(egress, 'policy', return_value=rules), mock.patch.object(ingest.subprocess, 'run') as run:
            preserved = ingest._merge_concept_sources(existing, concept, 'New study', source_note=source)
        run.assert_not_called()
        self.assertNotEqual(preserved, existing)
        rules['rules'].append({'source': str(source), 'audiences': ['model']})
        concept['details'] = 'Exact boundary condition 2'
        with mock.patch.object(egress, 'policy', return_value=rules), mock.patch.object(ingest.subprocess, 'run', return_value=result) as run:
            output = ingest._merge_concept_sources(existing, concept, 'New study', source_note=source)
        self.assertEqual(output, existing)
        self.assertTrue(existing.read_text().startswith('---\ntype: literature\n---\nHUMAN RULE\n'))
        self.assertIn('ORIGINAL SOURCE QUALIFICATION', existing.read_text())
        self.assertIn('Exact boundary condition 2', existing.read_text())
        self.assertNotIn('DATABASE_URL', run.call_args.kwargs['env'])

    def test_merge_authorizes_actual_origin_before_source_index_is_created(self):
        from claude_note import egress, ingest
        existing = self.vault / 'lit-rule.md'
        existing.write_text('---\ntype: literature\n---\nHUMAN RULE\n')
        origin = self.root / 'original-study.txt'
        origin.write_text('New observed boundary condition.')
        pending_index = self.vault / 'lit-new-study.md'
        concept = {'slug': 'rule', 'summary': 'New observed boundary condition',
                   'details': 'Original qualification', 'source_origin': str(origin),
                   'derivation_sources': [str(origin)], 'source_revision': 'source-hash',
                   'source_spans': ['section:1']}
        rules = {'rules': [{'source': str(path), 'audiences': ['model']}
                           for path in (existing, origin)]}
        result = mock.Mock(returncode=0, stdout='{"has_new_info": true, "new_info_summary": "Proposed assessment"}')
        with mock.patch.object(egress, 'policy', return_value=rules), \
                mock.patch.object(ingest.subprocess, 'run', return_value=result) as run:
            output = ingest._merge_concept_sources(existing, concept, 'New study', source_note=pending_index)
        self.assertEqual(output, existing)
        run.assert_called_once()
        self.assertFalse(pending_index.exists())
        self.assertIn('[[lit-new-study]]', existing.read_text())
        self.assertEqual(set(egress.derivations(existing.read_text())), {str(existing), str(origin)})

    def test_owned_source_metadata_refreshes_without_erasing_annotations(self):
        from claude_note import ingest, push
        output_dir = self.vault / 'literature'
        extraction = {'key_citation': 'Study', 'source_summary': 'First summary', 'source_type': 'paper',
                      'source_revision': 'first', 'source_spans': ['section:1']}
        path = ingest.create_source_note(extraction, self.root / 'study.txt', output_dir, '2026-10-13')
        extraction.update(source_revision='second', source_spans=['section:2'], source_summary='Second summary')
        ingest.create_source_note(extraction, self.root / 'study.txt', output_dir, '2026-10-13')
        text = path.read_text()
        self.assertEqual(push.frontmatter(text)['source_revision'], 'second')
        self.assertIn('section:2', text)
        self.assertNotIn('First summary', text)
        self.assertNotIn('source_revision: "first"', text)

    def test_owned_cleanup_applies_while_human_cleanup_stages(self):
        from claude_note import cleaner
        content = '# Inbox\n\n## 2026-10-09 - Old\n\nSame evidence.\n\n## 2026-10-10 - New\n\nSame evidence.\n'
        mutation.create_note(config.INBOX_PATH, content, vault_root=self.vault)
        result = cleaner.dedupe_inbox(dry_run=False)
        self.assertEqual(result['entries_removed'], 1)
        self.assertEqual(result['status'], 'updated')
        self.assertNotIn('Old', config.INBOX_PATH.read_text())


    def test_worker_and_drain_record_review_status_without_advancing_write_time(self):
        import io
        import logging
        from contextlib import redirect_stdout
        from claude_note import drain, models, note_writer, queue_manager, session_tracker, worker
        events = [models.QueuedEvent.from_hook_input({'session_id': 'stage-session', 'hook_event_name': kind,
                                                     'prompt': 'Capture this correction', 'transcript_path': '/synthetic/transcript.jsonl'})
                  for kind in ('UserPromptSubmit', 'Stop')]
        # Select the exact legacy path without relying on date/short-id guesses.
        state = models.SessionState('stage-session', events[0].ts, events[-1].ts)
        note_path = self.vault / note_writer.get_note_filename(state)
        note_path.write_text('## Timeline\n\nHUMAN ORIGINAL\n\n## Summary\n\nHuman summary.\n')
        with mock.patch.object(config, 'QUEUE_DIR', self.vault / '.claude-note/queue'), \
                mock.patch.object(config, 'SYNTH_MODE', 'route'), mock.patch.object(worker, 'run_synthesis', return_value=True) as synthesize:
            self.assertFalse(worker.process_session('stage-session', events, logging.getLogger('stage-test')))
            saved = session_tracker.load_session_state('stage-session')
            self.assertIsNone(saved.last_write_ts)
            self.assertTrue(Path(saved.note_pending_path).is_file())
            for event in events: queue_manager.enqueue_event(event)
            queue_manager.enqueue_event(models.QueuedEvent.from_hook_input({
                'session_id': 'stage-session', 'hook_event_name': 'Stop',
                'transcript_path': '/synthetic/transcript.jsonl'}))
            with mock.patch.object(drain, 'run_synthesis_for_drain', return_value=True), redirect_stdout(io.StringIO()) as output:
                self.assertEqual(drain.drain_all(), (1, 0))
            self.assertIn('staged for review', output.getvalue())
            synthesize.assert_called_once()
        self.assertIn('HUMAN ORIGINAL', note_path.read_text())
        self.assertIsNone(session_tracker.load_session_state('stage-session').last_write_ts)

    def test_staged_synthesis_pack_is_durable_completion_not_repeated_model_work(self):
        import logging
        from claude_note import models, worker, drain
        state = models.SessionState('synthesis', '2026-10-13T00:00:00Z', '2026-10-13T00:00:00Z',
                                     transcript_path='/synthetic/transcript.jsonl')
        pack = knowledge_pack.KnowledgePack('synthesis', '2026-10-13', 'Correction',
                note_ops=[knowledge_pack.NoteOp('append', 'human', 'candidate', section='## Evidence')])
        (self.vault / 'human.md').write_text('## Evidence\n\nHuman text.\n\n## Next\n')
        with mock.patch.object(config, 'SYNTH_MODE', 'route'), mock.patch.object(worker.vault_indexer, 'get_index'), \
                mock.patch.object(worker.synthesizer, 'synthesize_from_state', return_value=pack), \
                mock.patch.object(worker, 'update_session_summary', return_value=False):
            self.assertTrue(worker.run_synthesis(state, logging.getLogger('stage-test')))
            self.assertTrue(drain.run_synthesis_for_drain(state))
        self.assertNotIn('candidate', (self.vault / 'human.md').read_text())
        self.assertEqual(len(self.pending()), 1)

    def test_distinct_section_operations_with_identical_content_both_apply(self):
        note = note_router.create_note('two-sections', {'type': 'reference'},
                                       '## First\n\n## Second\n', self.vault)
        for heading, identity in [('## First', 'append-first'), ('## Second', 'append-second')]:
            operation = knowledge_pack.NoteOp('append', 'two-sections.md', 'Same useful evidence',
                                              section=heading, operation_id=identity)
            self.assertTrue(note_router.apply_note_op(operation, self.vault, 'same-session'))
        text = note.read_text()
        self.assertEqual(text.count('Same useful evidence'), 2)
        self.assertIn('Same useful evidence', text.split('## Second')[0])
        self.assertIn('Same useful evidence', text.split('## Second')[1])

    def test_simultaneous_first_lock_creation_is_stable(self):
        # Darwin can return ENOENT for concurrent nonexclusive
        # O_CREAT|O_NOFOLLOW, even while the parent descriptor remains valid.
        # Exercise the real first-create path, not only preexisting lock files.
        failures = []
        for round_number in range(20):
            barrier = threading.Barrier(12)
            def acquire():
                try:
                    barrier.wait(timeout=10)
                    with mutation.note_lock(self.vault / f'first-lock-{round_number}.md', vault_root=self.vault):
                        pass
                except Exception:
                    failures.append(traceback.format_exc())
            threads = [threading.Thread(target=acquire) for _ in range(12)]
            for thread in threads: thread.start()
            for thread in threads: thread.join(10)
            self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(failures, [])

    def test_filesystem_aliases_share_effects_but_distinct_files_do_not(self):
        for index, (name, alias) in enumerate([('foo.md', 'FOO.md'), ('é.md', 'e\u0301.md')]):
            path, other = self.vault / name, self.vault / alias
            mutation.create_note(path, 'original\n', vault_root=self.vault, operation_id=f'create-alias-{index}')
            if other.exists() and path.stat().st_ino == other.stat().st_ino:
                for spelling in [path, other]:
                    mutation.mutate(spelling, lambda old: old + 'ONE EFFECT\n', vault_root=self.vault,
                                    operation_id='same-alias-effect')
                self.assertEqual(path.read_text().count('ONE EFFECT'), 1)
            else:
                result = mutation.create_note(other, 'distinct file\n', vault_root=self.vault,
                                              operation_id=f'create-alias-{index}')
                self.assertEqual(result.status, 'created')
                self.assertNotEqual(path.stat().st_ino, other.stat().st_ino)

    def test_exclusive_create_crash_recovers_private_link_and_machine_ownership(self):
        source = str(Path(mutation.__file__).parents[1])
        script = """import os,sys
from pathlib import Path
from unittest import mock
sys.dont_write_bytecode=True
sys.path.insert(0,sys.argv[1])
from claude_note import mutation
vault=Path(sys.argv[2]); original=os.link
def crash(*args,**kwargs):
 original(*args,**kwargs);os._exit(91)
with mock.patch.object(mutation.os,'link',side_effect=crash):
 mutation.create_note(vault/'created-crash.md','one effect\\n',vault_root=vault,operation_id='create-crash')
"""
        child = subprocess.run([sys.executable, '-B', '-c', script, source, str(self.vault)],
                               capture_output=True, text=True, timeout=10)
        self.assertEqual(child.returncode, 91, child.stderr)
        path = self.vault / 'created-crash.md'
        self.assertEqual(path.stat().st_nlink, 2)
        result = mutation.create_note(path, 'one effect\n', vault_root=self.vault, operation_id='create-crash')
        self.assertEqual(result.status, 'unchanged')
        self.assertEqual(path.stat().st_nlink, 1)
        self.assertEqual(list(self.vault.glob('.claude-note-*.tmp')), [])
        replacement = mutation.mutate(path, lambda old: 'new generated revision\n', vault_root=self.vault,
                                      operation_id='after-create-crash')
        self.assertEqual(replacement.status, 'updated')

    def test_malformed_existing_managed_markers_stage_instead_of_false_replay(self):
        path = self.vault / 'partial-block.md'
        original = '# Human\n\n## Evidence\n\n<!-- claude-note:same:start -->\npartial\n'
        path.write_text(original)
        block = '<!-- claude-note:same:start -->\ncomplete\n<!-- claude-note:same:end -->'
        result = managed_blocks.append_to_section_result(path, '## Evidence', block, vault_root=self.vault)
        self.assertEqual(result.status, 'staged')
        self.assertEqual(path.read_text(), original)
        self.assertIn('complete', json.loads(result.staged_path.read_text())['content'])
        nested = '<!-- claude-note:outer:start -->\n' + block + '\n<!-- claude-note:outer:end -->'
        with self.assertRaises(ValueError):
            managed_blocks.append_to_section_result(path, '## Evidence', nested, vault_root=self.vault)

    def test_partial_multi_block_replay_only_adds_the_missing_block(self):
        block_a = '<!-- claude-note:a:start -->\nEvidence A\n<!-- claude-note:a:end -->'
        block_b = '<!-- claude-note:b:start -->\nEvidence B\n<!-- claude-note:b:end -->'
        path = note_router.create_note('partial-multi', {'type': 'reference'}, '## Evidence\n\n' + block_a, self.vault)
        result = managed_blocks.append_to_section_result(path, '## Evidence', block_a + '\n' + block_b,
                                                         vault_root=self.vault, operation_id='multi-block')
        self.assertEqual(result.status, 'updated')
        self.assertEqual(path.read_text().count('Evidence A'), 1)
        self.assertEqual(path.read_text().count('Evidence B'), 1)

    def test_enriched_links_do_not_duplicate_a_stable_extraction(self):
        pack = knowledge_pack.KnowledgePack('stable-session', '2026-10-13', 'Stable extraction',
                generation='stable-generation', source_revision='stable-source',
                concepts=[knowledge_pack.Concept('Retries', 'Idempotency is required')])
        def enhance(value):
            value.concepts[0].links_suggested.append('new-neighbor-' + str(len(value.concepts[0].links_suggested)))
        with mock.patch.object(config, 'QMD_SYNTH_ENABLED', True), \
                mock.patch.object(note_router, '_enhance_concept_links', side_effect=enhance):
            self.assertTrue(note_router.apply_note_ops(pack, mode='inbox')['inbox_updated'])
            self.assertFalse(note_router.apply_note_ops(pack, mode='inbox')['inbox_updated'])
        self.assertEqual(config.INBOX_PATH.read_text().count('Stable extraction'), 1)

    def test_absolute_platform_alias_stays_confined_to_canonical_vault(self):
        raw_path = Path(self.temp.name) / 'vault' / 'absolute-alias.md'
        result = mutation.create_note(raw_path, 'confined absolute route', vault_root=self.vault,
                                      operation_id='absolute-platform-alias')
        self.assertEqual(result.path, self.vault / 'absolute-alias.md')
        self.assertEqual(result.path.read_text(), 'confined absolute route')
        outside = self.root / 'outside.md'; outside.write_text('outside')
        (self.vault / 'escape.md').symlink_to(outside)
        with self.assertRaises(ValueError):
            mutation.mutate(Path(self.temp.name) / 'vault' / 'escape.md', lambda old: old + 'addition',
                            vault_root=self.vault)
        self.assertEqual(outside.read_text(), 'outside')


if __name__ == '__main__':
    unittest.main()
