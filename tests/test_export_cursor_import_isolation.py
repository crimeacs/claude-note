"""Export/Cursor failure isolation using only disposable synthetic sources."""

import hashlib
import io
import json
import os
import sqlite3
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

_BOOTSTRAP = tempfile.TemporaryDirectory(prefix='export-cursor-config-')
os.environ.setdefault('CLAUDE_NOTE_VAULT_ROOT', str(Path(_BOOTSTRAP.name) / 'vault'))

from claude_note import config, importers, queue_manager  # noqa: E402


class ExportCursorImportIsolationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='export-cursor-isolation-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        patchers = [mock.patch.multiple(importers, TRANSCRIPTS_DIR=self.root/'transcripts',
            SEEN_FILE=self.root/'transcripts/seen.json', HEARTBEAT=self.root/'logs/import-sweep.ok',
            IMPORTS_DIR=self.root/'imports', WATCH_DIRS=[self.root/'downloads', self.root/'imports'],
            CURSOR_DB=self.root/'cursor.vscdb', CURSOR_WORKSPACES=self.root/'workspaces',
            CODEX_SESSIONS=self.root/'codex', CLAUDE_PROJECTS=self.root/'claude'),
            mock.patch.multiple(config, QUEUE_DIR=self.root/'queue', STATE_DIR=self.root/'state')]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        for directory in importers.WATCH_DIRS:
            directory.mkdir(parents=True)

    def conversation(self, identity):
        return {'uuid': identity, 'chat_messages': [
            {'sender': 'human', 'text': 'Synthetic public question'},
            {'sender': 'assistant', 'text': 'Synthetic public answer'}]}

    def archive(self, members, name='synthetic.zip', directory=None):
        path = (directory or importers.IMPORTS_DIR) / name
        with zipfile.ZipFile(path, 'w') as archive:
            for member, value in members:
                archive.writestr(member, value if isinstance(value, (str, bytes)) else json.dumps(value))
        stamp = time.time() - 120
        os.utime(path, (stamp, stamp))
        return path

    def composer(self, identity, *, bubbles=None, headers=None):
        value = {'composerId': identity, 'lastUpdatedAt': int(time.time()*1000)}
        if headers is not None:
            value['fullConversationHeadersOnly'] = [{'bubbleId': item} for item in headers]
        else:
            value['conversation'] = bubbles if bubbles is not None else [
                {'type': 1, 'text': 'Synthetic public question'},
                {'type': 2, 'text': 'Synthetic public answer'}]
        return value

    def database(self, rows):
        db = sqlite3.connect(importers.CURSOR_DB)
        db.execute('CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value BLOB)')
        db.executemany('INSERT INTO cursorDiskKV VALUES (?, ?)', [
            (key, value if isinstance(value, (str, bytes)) else json.dumps(value)) for key, value in rows])
        db.commit()
        db.close()
        return importers.CURSOR_DB

    def events(self):
        return list(queue_manager.read_all_events())

    def seen_key(self, source, identity):
        return source + ':' + hashlib.sha256(f'{source}:{identity}'.encode()).hexdigest()[:32]

    def test_recognized_member_failure_continues_later_members_and_retains_original(self):
        path = self.archive([('conversations.json', 'SECRET_PAYLOAD_INVALID_JSON'),
            ('conversations-1.json', [self.conversation('later')])])
        original = path.read_bytes()
        seen, problems, report = {}, [], {}
        self.assertEqual(importers.import_zip(path, seen, problems=problems, report=report), 1)
        self.assertEqual(report['members_examined'], 2)
        self.assertEqual(report['member_failures'], 1)
        self.assertEqual(report['queued_sessions'], 1)
        self.assertEqual(len(problems), 1)
        self.assertNotIn('SECRET_PAYLOAD', problems[0])
        self.assertEqual(path.read_bytes(), original)
        self.assertIn(self.seen_key('claude-ai', 'later'), seen)

    def test_failed_conversation_retains_seen_and_does_not_block_later_conversation(self):
        path = self.archive([('conversations.json', [self.conversation('first'),
            {'uuid': 'bad', 'chat_messages': 'invalid'}, self.conversation('last')])])
        bad_key = self.seen_key('claude-ai', 'bad')
        seen, problems, report = {bad_key: 'prior-revision'}, [], {}
        self.assertEqual(importers.import_zip(path, seen, problems=problems, report=report), 2)
        self.assertEqual(seen[bad_key], 'prior-revision')
        self.assertEqual(report['conversations_examined'], 3)
        self.assertEqual(report['conversation_failures'], 1)
        self.assertEqual(len(self.events()), 4)
        self.assertEqual(importers.import_zip(path, seen, problems=[]), 0)
        self.archive([('conversations.json', [self.conversation('first'),
            self.conversation('bad'), self.conversation('last')])])
        self.assertEqual(importers.import_zip(path, seen, problems=[]), 1)
        self.assertNotEqual(seen[bad_key], 'prior-revision')
        self.assertEqual(len(self.events()), 6)

    def test_readable_unrelated_zip_is_a_skip_and_recognized_failure_is_visible(self):
        unrelated = self.archive([('readme.txt', 'unrelated')], 'unrelated.zip')
        report = {}
        self.assertIsNone(importers.import_zip(unrelated, {}, report=report))
        self.assertEqual(report['skipped_unrelated_archives'], 1)
        bad = self.archive([('conversations.json', '{}')], 'broken.zip')
        with self.assertRaises(importers.SourceImportError) as caught:
            importers.import_zip(bad, {})
        self.assertEqual(caught.exception.queued, 0)
        self.assertEqual(caught.exception.failed, 1)
        self.assertEqual(caught.exception.report['member_failures'], 1)
        counts = importers.sweep(zips=[unrelated])
        self.assertEqual(counts['zips'], 0)
        self.assertEqual(counts[str(unrelated)], 0)
        self.assertEqual(counts['failed'], 1)
        self.assertFalse(importers.HEARTBEAT.exists())

    def test_detail_cap_retains_exact_totals_and_later_progress_without_clean_heartbeat(self):
        path = self.archive([('conversations.json', [None]*105 + [self.conversation('after-errors')])])
        importers.HEARTBEAT.parent.mkdir(parents=True)
        importers.HEARTBEAT.write_bytes(b'prior-clean')
        counts = importers.sweep()
        self.assertEqual(counts['zips'], 1)
        self.assertEqual(counts['failed'], 105)
        self.assertEqual(len(counts['problems']), 100)
        self.assertEqual(counts['problem_details_omitted'], 5)
        self.assertEqual(counts['reports']['zips']['conversations_examined'], 106)
        self.assertEqual(counts['reports']['zips']['failed_items'], 105)
        self.assertEqual(importers.HEARTBEAT.read_bytes(), b'prior-clean')
        self.assertTrue(path.exists())
        self.assertEqual(len(self.events()), 2)
        attempt = json.loads(importers.HEARTBEAT.with_suffix('.json').read_text())
        self.assertEqual(attempt['counts']['failed'], 105)

    def test_direct_and_ordinary_list_sinks_report_omitted_details(self):
        path = self.archive([('conversations.json', [None]*103 + [self.conversation('direct')])])
        with self.assertRaises(importers.SourceImportError) as caught:
            importers.import_zip(path, {})
        self.assertEqual(caught.exception.queued, 1)
        self.assertEqual(caught.exception.failed, 103)
        self.assertEqual(caught.exception.omitted_problem_details, 3)
        self.assertEqual(len(caught.exception.problems), 100)
        problems, report = [], {}
        # The prior call published the healthy conversation before raising;
        # reconstructing a missing seen store must not enqueue it again.
        self.assertEqual(importers.import_zip(path, {}, problems=problems, report=report), 0)
        self.assertEqual(len(problems), 101)
        self.assertEqual(problems[-1], 'zips: 3 additional failures; details omitted')
        self.assertEqual(report['failed_items'], 103)
        self.assertEqual(len(self.events()), 2)

    def test_declared_member_limit_rejects_before_decompression(self):
        path = self.archive([('conversations.json', 'x'*33)])
        with mock.patch.object(importers, 'MAX_IMPORT_PAYLOAD_BYTES', 32), \
                mock.patch.object(zipfile.ZipFile, 'open', side_effect=AssertionError('must not open')):
            problems = []
            self.assertEqual(importers.import_zip(path, {}, problems=problems), 0)
        self.assertIn('Export member exceeds 256 MiB', problems[0])

    def test_actual_decompressed_member_limit_is_also_enforced(self):
        path = self.archive([('conversations.json', [])])
        body = io.BytesIO(b'x'*33)
        with mock.patch.object(importers, 'MAX_IMPORT_PAYLOAD_BYTES', 32), \
                mock.patch.object(zipfile.ZipFile, 'open', return_value=body):
            problems = []
            self.assertEqual(importers.import_zip(path, {}, problems=problems), 0)
        self.assertIn('Export member exceeds 256 MiB', problems[0])

    def test_archive_backup_failure_preserves_progress_and_retry_with_zero_new_sessions(self):
        path = self.archive([('conversations.json', [self.conversation('backup')])])
        original = path.read_bytes()
        with mock.patch.object(importers.shutil, 'copy2', side_effect=OSError('SECRET_BACKUP_PAYLOAD')):
            first = importers.sweep()
        self.assertEqual(first['zips'], 1)
        self.assertEqual(first['failed'], 1)
        self.assertEqual(first['reports']['zips']['archive_backup_failures'], 1)
        self.assertNotIn('SECRET_BACKUP_PAYLOAD', ' '.join(first['problems']))
        self.assertFalse(importers.HEARTBEAT.exists())
        seen = json.loads(importers.SEEN_FILE.read_text())
        self.assertIn(self.seen_key('claude-ai', 'backup'), seen)
        second = importers.sweep()
        self.assertEqual(second['zips'], 0)
        self.assertNotIn('problems', second)
        self.assertEqual(second['reports']['zips']['archive_backups_copied'], 1)
        self.assertEqual((importers.IMPORTS_DIR/'done'/path.name).read_bytes(), original)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(len(self.events()), 2)
        self.assertTrue(importers.HEARTBEAT.exists())

    def test_failed_publication_does_not_advance_seen_or_block_independent_conversations(self):
        path = self.archive([('conversations.json', [self.conversation('failure'), self.conversation('healthy')])])
        original = importers.write_session
        def write(seen, source, identity, *args, **kwargs):
            if identity == 'failure':
                raise OSError('SECRET_SOURCE_MESSAGE')
            return original(seen, source, identity, *args, **kwargs)
        seen, problems = {}, []
        with mock.patch.object(importers, 'write_session', side_effect=write):
            self.assertEqual(importers.import_zip(path, seen, problems=problems), 1)
        self.assertNotIn(self.seen_key('claude-ai', 'failure'), seen)
        self.assertIn(self.seen_key('claude-ai', 'healthy'), seen)
        self.assertNotIn('SECRET_SOURCE_MESSAGE', problems[0])

    def test_zip_deferrals_and_noninteractive_exclusions_have_explicit_units(self):
        self.archive([('conversations.json', [self.conversation('unknown')])], 'unknown.zip', self.root/'downloads')
        fresh = self.archive([('conversations.json', [self.conversation('fresh')])], 'data-fresh.zip', self.root/'downloads')
        os.utime(fresh, None)
        self.archive([('conversations.json', [{'uuid': 'empty', 'chat_messages': []}])])
        report = {}
        self.assertEqual(importers.sweep_zips({}, problems=[], report=report), 0)
        self.assertEqual(report['skipped_unrecognized_download_names'], 1)
        self.assertEqual(report['deferred_downloading_archives'], 1)
        self.assertEqual(report['skipped_noninteractive_conversations'], 1)
        self.assertEqual(report['queued_sessions'], 0)

    def test_cursor_missing_required_bubble_blocks_only_its_composer_and_retains_checkpoint(self):
        path = self.database([('composerData:a', self.composer('a')),
            ('composerData:b', self.composer('b', headers=['question', 'answer', 'missing'])),
            ('bubbleId:b:question', {'type': 1, 'text': 'question'}),
            ('bubbleId:b:answer', {'type': 2, 'text': 'answer'}),
            ('composerData:c', self.composer('c'))])
        original = path.read_bytes()
        checkpoint = config.STATE_DIR/'unrelated.json'
        checkpoint.parent.mkdir(parents=True)
        checkpoint.write_bytes(b'{"transcript_checkpoint":{"bytes":17}}')
        seen, problems, report = {self.seen_key('cursor', 'b'): 'prior'}, [], {}
        self.assertEqual(importers.sweep_cursor(seen, 0, problems=problems, report=report), 2)
        self.assertEqual(report['composers_examined'], 3)
        self.assertEqual(report['composer_failures'], 1)
        self.assertEqual(seen[self.seen_key('cursor', 'b')], 'prior')
        self.assertEqual(len(self.events()), 4)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(checkpoint.read_bytes(), b'{"transcript_checkpoint":{"bytes":17}}')
        self.assertIn('required bubble is missing or malformed', problems[0])

    def test_cursor_deleted_composer_tombstones_are_counted_without_false_size_failure(self):
        path = self.database([('composerData:deleted', None), ('composerData:healthy', self.composer('healthy'))])
        # The helper serializes fixtures; set a genuine SQL NULL as Cursor does.
        with sqlite3.connect(path) as db:
            db.execute('UPDATE cursorDiskKV SET value=NULL WHERE key=?', ('composerData:deleted',))
        original = path.read_bytes()
        seen, problems, report = {}, [], {}
        self.assertEqual(importers.sweep_cursor(seen, 0, problems=problems, report=report), 1)
        self.assertEqual(problems, [])
        self.assertEqual(report['skipped_deleted_composers'], 1)
        self.assertEqual(report['failed_items'], 0)
        self.assertNotIn(self.seen_key('cursor', 'deleted'), seen)
        self.assertEqual(path.read_bytes(), original)

    def test_cursor_malformed_bubbles_and_composers_continue_then_repair_retries(self):
        self.database([('composerData:invalid-json', '{SECRET_PAYLOAD'),
            ('composerData:bad-bubble', self.composer('bad-bubble', bubbles=[{'type': 1, 'text': 'q'}, {'type': 9, 'text': 'unsupported'}])),
            ('composerData:malformed-json-bubble', self.composer('malformed-json-bubble', headers=['bad'])),
            ('bubbleId:malformed-json-bubble:bad', '{SECRET_BUBBLE'),
            ('composerData:healthy', self.composer('healthy'))])
        seen, problems, report = {}, [], {}
        self.assertEqual(importers.sweep_cursor(seen, 0, problems=problems, report=report), 1)
        self.assertEqual(report['composer_failures'], 3)
        self.assertEqual(report['failed_items'], 3)
        self.assertEqual(len(problems), 3)
        self.assertNotIn('SECRET', ' '.join(problems))
        self.assertEqual(len(seen), 1)
        db = sqlite3.connect(importers.CURSOR_DB)
        db.execute('UPDATE cursorDiskKV SET value=? WHERE key=?',
            (json.dumps(self.composer('invalid-json')), 'composerData:invalid-json'))
        db.commit()
        db.close()
        self.assertEqual(importers.sweep_cursor(seen, 0, problems=[]), 1)
        self.assertEqual(len(self.events()), 4)

    def test_cursor_tool_output_preserves_full_normalized_evidence(self):
        output = 'FULL_SYNTHETIC_OUTPUT_' + 'x'*6000
        self.database([('composerData:tools', self.composer('tools', bubbles=[
            {'type': 1, 'text': 'q'}, {'type': 2, 'text': 'a', 'toolFormerData': {
                'name': 'read_file', 'params': '{unparsed arguments', 'result': output}}]))])
        self.assertEqual(importers.sweep_cursor({}, 0), 1)
        transcript = Path(self.events()[0].transcript_path)
        rows = [json.loads(line) for line in transcript.read_text().splitlines()]
        result = next(row for row in rows if row['type'] == 'tool_result')
        self.assertEqual(result['content'], output)
        assistant = next(row for row in rows if row['type'] == 'assistant')
        tool = next(block for block in assistant['message']['content'] if block['type'] == 'tool_use')
        self.assertEqual(tool['input'], {'input': '{unparsed arguments'})

    def test_cursor_workspace_failure_is_visible_without_stopping_valid_composers(self):
        self.database([('composerData:a', self.composer('a')), ('composerData:b', self.composer('b'))])
        bad = importers.CURSOR_WORKSPACES/'bad'
        bad.mkdir(parents=True)
        (bad/'workspace.json').write_text('{SECRET_WORKSPACE')
        counts = importers.sweep()
        self.assertEqual(counts['cursor'], 2)
        self.assertEqual(counts['failed'], 1)
        self.assertEqual(counts['reports']['cursor']['workspace_source_failures'], 1)
        self.assertEqual(counts['reports']['cursor'].get('composer_failures', 0), 0)
        self.assertFalse(importers.HEARTBEAT.exists())
        self.assertNotIn('SECRET_WORKSPACE', ' '.join(counts['problems']))
        self.assertEqual({event.cwd for event in self.events()}, {'cursor'})

    def test_late_cursor_scan_failure_preserves_successes_and_visible_direct_error(self):
        self.database([('composerData:first', self.composer('first'))])
        real_connect = sqlite3.connect
        class Connection:
            def __init__(self, *args, **kwargs):
                self.inner = real_connect(*args, **kwargs)
            def execute(self, sql, *args):
                result = self.inner.execute(sql, *args)
                if sql.startswith('select key from cursorDiskKV'):
                    def scan():
                        yield next(iter(result))
                        raise sqlite3.OperationalError('SECRET_SCAN_PAYLOAD')
                    return scan()
                return result
            def setlimit(self, *args):
                return self.inner.setlimit(*args)
            def close(self):
                self.inner.close()
        with mock.patch.object(importers.sqlite3, 'connect', side_effect=Connection), \
                self.assertRaises(importers.SourceImportError) as caught:
            importers.sweep_cursor({}, 0)
        self.assertEqual(caught.exception.queued, 1)
        self.assertEqual(caught.exception.report['database_scan_failures'], 1)
        self.assertNotIn('database_scans_completed', caught.exception.report)
        self.assertNotIn('SECRET_SCAN_PAYLOAD', str(caught.exception))
        self.assertEqual(len(self.events()), 2)

    def test_cursor_close_failure_keeps_count_and_blocks_clean_heartbeat(self):
        self.database([('composerData:close', self.composer('close'))])
        real_connect = sqlite3.connect
        class Connection:
            def __init__(self, *args, **kwargs):
                self.inner = real_connect(*args, **kwargs)
            def execute(self, *args):
                return self.inner.execute(*args)
            def setlimit(self, *args):
                return self.inner.setlimit(*args)
            def close(self):
                self.inner.close()
                raise sqlite3.OperationalError('SECRET_CLOSE_PAYLOAD')
        with mock.patch.object(importers.sqlite3, 'connect', side_effect=Connection):
            counts = importers.sweep()
        self.assertEqual(counts['cursor'], 1)
        self.assertEqual(counts['failed'], 1)
        self.assertEqual(counts['reports']['cursor']['database_close_failures'], 1)
        self.assertFalse(importers.HEARTBEAT.exists())
        self.assertNotIn('SECRET_CLOSE_PAYLOAD', ' '.join(counts['problems']))

    def test_cursor_payload_limit_and_old_composer_deferrals_are_explicit(self):
        huge = self.composer('huge', bubbles=[{'type': 1, 'text': 'x'*1000}])
        old = self.composer('old')
        old['lastUpdatedAt'] = 1
        self.database([('composerData:huge', huge), ('composerData:old', old),
            ('composerData:healthy', self.composer('healthy'))])
        seen, problems, report = {}, [], {}
        with mock.patch.object(importers, 'MAX_IMPORT_PAYLOAD_BYTES', 512):
            self.assertEqual(importers.sweep_cursor(seen, 1, problems=problems, report=report), 1)
        self.assertEqual(report['composer_failures'], 1)
        self.assertEqual(report['skipped_old_composers'], 1)
        self.assertNotIn(self.seen_key('cursor', 'huge'), seen)
        self.assertNotIn(self.seen_key('cursor', 'old'), seen)

    def test_cursor_many_failures_preserve_exact_totals_and_later_valid_composer(self):
        rows = [(f'composerData:bad-{number:03}', 'invalid') for number in range(105)]
        rows.append(('composerData:healthy', self.composer('healthy')))
        self.database(rows)
        problems, report = importers.ImportProblems(), {}
        self.assertEqual(importers.sweep_cursor({}, 0, problems=problems, report=report), 1)
        self.assertEqual(problems.total, 105)
        self.assertEqual(problems.omitted, 5)
        self.assertEqual(report['composers_examined'], 106)
        self.assertEqual(report['failed_items'], 105)

    def test_native_problem_details_are_also_bounded_without_losing_failure_totals(self):
        directory = importers.CLAUDE_PROJECTS/'synthetic'
        directory.mkdir(parents=True)
        for number in range(105):
            path = directory/f'bad-{number:03}.jsonl'
            path.write_text('{SYNTHETIC_INVALID_JSON\n')
            stamp = time.time()-3600
            os.utime(path, (stamp, stamp))
        path = directory/'healthy.jsonl'
        path.write_text(''.join(json.dumps(row)+'\n' for row in [
            {'type': 'user', 'sessionId': 'healthy', 'entrypoint': 'cli', 'message': {'content': 'question'}},
            {'type': 'assistant', 'sessionId': 'healthy', 'message': {'content': [{'type': 'text', 'text': 'answer'}]}}]))
        stamp = time.time()-7200
        os.utime(path, (stamp, stamp))
        counts = importers.sweep()
        self.assertEqual(counts['claude-code'], 1)
        self.assertEqual(counts['failed'], 105)
        self.assertEqual(len(counts['problems']), 100)
        self.assertEqual(counts['problem_details_omitted'], 5)
        self.assertFalse(importers.HEARTBEAT.exists())
        self.assertEqual(len(self.events()), 2)

    def test_cursor_falsey_malformed_required_fields_are_failures(self):
        for field in ('thinking', 'toolFormerData'):
            with self.subTest(field=field), self.assertRaises(ValueError):
                importers._cursor_bubble_message({'type': 2, 'text': 'answer', field: []})

    def test_empty_supported_assistant_export_is_skipped_not_unchanged(self):
        conversation=self.conversation('empty-assistant')
        conversation['chat_messages'][1]['text']=''
        path=self.archive([('conversations.json',[conversation])])
        report={};seen={};problems=[]
        self.assertEqual(importers.import_zip(path,seen,problems=problems,report=report),0)
        self.assertEqual(report.get('skipped_noninteractive_conversations'),1)
        self.assertEqual(report.get('unchanged_conversations',0),0)
        self.assertEqual(report.get('valid_interactive_conversations',0),0)
        self.assertEqual(seen,{})
        self.assertEqual(problems,[])

    def test_empty_supported_assistant_cursor_is_skipped_not_unchanged(self):
        composer=self.composer('empty-assistant',bubbles=[{'type':1,'text':'Synthetic question'},
                                                        {'type':2,'text':''}])
        self.database([('composerData:empty-assistant',json.dumps(composer))])
        report={};seen={};problems=[]
        self.assertEqual(importers.sweep_cursor(seen,0,problems=problems,report=report),0)
        self.assertEqual(report.get('skipped_noninteractive_composers'),1)
        self.assertEqual(report.get('unchanged_composers',0),0)
        self.assertEqual(seen,{})
        self.assertEqual(problems,[])


if __name__ == '__main__':
    unittest.main()
