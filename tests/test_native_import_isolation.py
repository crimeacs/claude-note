"""Native recovery failures use synthetic files, never real imports/providers."""

import hashlib
import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

_BOOTSTRAP = tempfile.TemporaryDirectory(prefix='native-import-config-')
os.environ.setdefault('CLAUDE_NOTE_VAULT_ROOT', str(Path(_BOOTSTRAP.name) / 'vault'))

from claude_note import cli, config, importers, queue_manager, transcript_reader  # noqa: E402


class NativeImportIsolationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='native-import-isolation-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        patchers = [mock.patch.multiple(importers, TRANSCRIPTS_DIR=self.root/'transcripts',
            SEEN_FILE=self.root/'transcripts/seen.json', HEARTBEAT=self.root/'logs/import-sweep.ok',
            WATCH_DIRS=[], CURSOR_DB=self.root/'absent.vscdb', CURSOR_WORKSPACES=self.root/'cursor',
            CODEX_SESSIONS=self.root/'codex', CLAUDE_PROJECTS=self.root/'claude'),
            mock.patch.multiple(config, QUEUE_DIR=self.root/'queue', STATE_DIR=self.root/'state')]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def native(self, source, sid, age=3600):
        if source == 'codex':
            path = importers.CODEX_SESSIONS / '2026/10/14' / ('rollout-' + sid + '.jsonl')
            rows = [{'type':'session_meta','payload':{'id':sid,'cwd':'/synthetic','source':'vscode'}},
                {'type':'response_item','payload':{'type':'message','role':'user','content':[{'type':'input_text','text':'Public question'}]}},
                {'type':'response_item','payload':{'type':'message','role':'assistant','content':[{'type':'output_text','text':'Public answer'}]}}]
        else:
            path = importers.CLAUDE_PROJECTS / 'synthetic' / (sid + '.jsonl')
            rows = [{'type':'user','sessionId':sid,'cwd':'/synthetic','entrypoint':'cli','message':{'content':'Public question'}},
                {'type':'assistant','sessionId':sid,'message':{'content':[{'type':'text','text':'Public answer'}]}}]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
        self.age(path, age)
        return path

    def age(self, path, age=3600):
        stamp = time.time() - age
        os.utime(path, (stamp, stamp))

    def oversize(self, path):
        with path.open('r+b') as handle:
            handle.truncate(transcript_reader.MAX_TRANSCRIPT_BYTES + 1)
        self.age(path, 1800)

    def sweep_source(self, source, seen, problems, paths=None, limit=10):
        if source == 'codex':
            directory = mock.Mock()
            directory.glob.return_value = paths or list(importers.CODEX_SESSIONS.glob('*/*/*/rollout-*.jsonl'))
            return importers.sweep_codex(seen, 0, directory, problems=problems)
        return importers.sweep_claude_code(seen, 0, limit=limit, problems=problems)

    def key(self, source, path):
        return 'codex:' + path.name if source == 'codex' else 'claude-code:' + path.stem

    def events(self):
        return list(queue_manager.read_all_events())

    def test_oversized_first_source_does_not_starve_later_source_or_advance_failed_seen(self):
        for source in ('claude-code', 'codex'):
            with self.subTest(source=source):
                bad = self.native(source, source + '-bad')
                good = self.native(source, source + '-good', age=7200)
                self.oversize(bad)
                old_seen = 17.0
                seen = {self.key(source,bad):old_seen}
                checkpoint = config.STATE_DIR / (source + '-bad.json')
                checkpoint.parent.mkdir(parents=True,exist_ok=True)
                original_state = b'{"transcript_checkpoint":{"bytes":42,"lines":2,"revision":"original"}}'
                checkpoint.write_bytes(original_state)
                initial = bad.stat()
                problems = []
                meta = importers._claude_code_meta if source == 'claude-code' else transcript_reader.codex_session_meta
                target = importers if source == 'claude-code' else transcript_reader
                name = '_claude_code_meta' if source == 'claude-code' else 'codex_session_meta'
                def guarded(path, *args, **kwargs):
                    self.assertNotEqual(Path(path),bad,'Oversize must reject before metadata')
                    return meta(path,*args,**kwargs)
                with mock.patch.object(target,name,side_effect=guarded):
                    self.assertEqual(self.sweep_source(source,seen,problems,[bad,good]),1)
                self.assertEqual(len(problems),1)
                self.assertIn('Transcript exceeds 256 MB',problems[0])
                self.assertEqual(seen[self.key(source,bad)],old_seen)
                self.assertEqual(seen[self.key(source,good)],good.stat().st_mtime)
                self.assertEqual(checkpoint.read_bytes(),original_state)
                self.assertEqual((bad.stat().st_size,bad.stat().st_mtime),(initial.st_size,initial.st_mtime))
                self.assertNotIn(source+'-bad',{event.session_id for event in self.events()})

    def test_multiple_failures_preserve_integer_partial_count_and_each_problem(self):
        for source in ('claude-code','codex'):
            with self.subTest(source=source):
                huge = self.native(source,source+'-huge')
                self.oversize(huge)
                malformed = self.native(source,source+'-malformed',age=3000)
                malformed.write_text('{"sensitive_fixture":"DO_NOT_INCLUDE_PAYLOAD",\n')
                self.age(malformed,3000)
                good = self.native(source,source+'-ok',age=7200)
                seen,problems = {},[]
                self.assertEqual(self.sweep_source(source,seen,problems,[huge,malformed,good]),1)
                self.assertEqual(len(problems),2)
                self.assertTrue(all(len(item)<=importers.MAX_IMPORT_PROBLEM_CHARS for item in problems))
                self.assertNotIn('DO_NOT_INCLUDE_PAYLOAD',' '.join(problems))
                self.assertNotIn(self.key(source,huge),seen)
                self.assertNotIn(self.key(source,malformed),seen)

    def test_direct_call_without_problem_sink_raises_with_partial_count(self):
        for source in ('claude-code','codex'):
            bad = self.native(source,source+'-direct-bad')
            self.oversize(bad)
            good = self.native(source,source+'-direct-good',age=7200)
            with self.subTest(source=source),self.assertRaises(importers.SourceImportError) as caught:
                self.sweep_source(source,{},None,[bad,good])
            self.assertEqual(caught.exception.queued,1)
            self.assertEqual(len(caught.exception.problems),1)
            self.assertLessEqual(len(str(caught.exception)),1024)

    def test_failed_files_do_not_consume_claude_queued_limit(self):
        bad = self.native('claude-code','limit-bad')
        self.oversize(bad)
        first = self.native('claude-code','limit-first',age=3600)
        second = self.native('claude-code','limit-second',age=7200)
        seen,problems = {},[]
        self.assertEqual(self.sweep_source('claude-code',seen,problems,limit=1),1)
        self.assertEqual(len(problems),1)
        self.assertIn(self.key('claude-code',first),seen)
        self.assertNotIn(self.key('claude-code',second),seen)
        self.assertEqual(self.sweep_source('claude-code',seen,[],limit=1),1)
        self.assertIn(self.key('claude-code',second),seen)
        self.assertNotIn(self.key('claude-code',bad),seen)

    def test_enqueue_failure_stays_retryable_and_later_source_continues(self):
        original = importers._enqueue
        for source in ('claude-code','codex'):
            bad = self.native(source,source+'-enqueue-bad',age=1800)
            good = self.native(source,source+'-enqueue-good',age=7200)
            def enqueue(sid,*args,**kwargs):
                if sid.endswith('-enqueue-bad'):
                    raise OSError('SYNTHETIC_PAYLOAD_NOT_FOR_DIAGNOSTICS')
                return original(sid,*args,**kwargs)
            seen,problems={},[]
            with mock.patch.object(importers,'_enqueue',side_effect=enqueue):
                self.assertEqual(self.sweep_source(source,seen,problems,[bad,good]),1)
            self.assertNotIn(self.key(source,bad),seen)
            self.assertEqual(len(problems),1)
            self.assertNotIn('SYNTHETIC_PAYLOAD',problems[0])
            self.assertNotIn(source+'-enqueue-bad',{event.session_id for event in self.events()})

    def test_cli_reports_integer_partial_counts_and_returns_failure(self):
        bad=self.native('codex','cli-bad')
        self.oversize(bad)
        self.native('codex','cli-good')
        with mock.patch('sys.stdout',new_callable=io.StringIO) as stdout:
            result=cli.cmd_import(SimpleNamespace(since=None,zips=[]))
        self.assertEqual(result,1)
        self.assertIn('codex: 1',stdout.getvalue())
        self.assertIn('failed: 1',stdout.getvalue())
        self.assertIn('problems:',stdout.getvalue())
        self.assertNotIn('Queued sessions are written',stdout.getvalue())

    def test_stat_failure_is_visible_without_blocking_later_codex_source(self):
        missing=importers.CODEX_SESSIONS/'2026/10/14/rollout-missing.jsonl'
        good=self.native('codex','stat-good')
        seen={self.key('codex',missing):17.0}
        problems=[]
        self.assertEqual(self.sweep_source('codex',seen,problems,[missing,good]),1)
        self.assertEqual(len(problems),1)
        self.assertIn('FileNotFoundError',problems[0])
        self.assertEqual(seen[self.key('codex',missing)],17.0)

    def test_failed_category_file_does_not_hide_other_category_progress_or_refresh_heartbeat(self):
        bad = self.native('codex','heartbeat-bad')
        self.oversize(bad)
        self.native('codex','heartbeat-codex-good')
        self.native('claude-code','heartbeat-claude-good')
        importers.HEARTBEAT.parent.mkdir(parents=True)
        baseline = b'{"ts":"2026-01-01T00:00:00","counts":{}}'
        importers.HEARTBEAT.write_bytes(baseline)
        counts = importers.sweep()
        self.assertEqual((counts['codex'],counts['claude-code']),(1,1))
        self.assertEqual(counts['failed'],1)
        self.assertEqual(len(counts['problems']),1)
        self.assertEqual(importers.HEARTBEAT.read_bytes(),baseline)
        seen = json.loads(importers.SEEN_FILE.read_text())
        self.assertNotIn(self.key('codex',bad),seen)
        attempt = json.loads(importers.HEARTBEAT.with_suffix('.json').read_text())
        self.assertEqual(attempt['counts'],counts)
        self.assertEqual(len(self.events()),4)
        repeated = importers.sweep()
        self.assertEqual((repeated['codex'],repeated['claude-code']),(0,0))
        self.assertEqual(repeated['failed'],1)
        self.assertEqual(len(self.events()),4)
        self.assertEqual(importers.HEARTBEAT.read_bytes(),baseline)
        # A real source repair can be retried normally; no manual seen/cursor edit.
        repaired = self.native('codex','heartbeat-bad',age=1800)
        self.assertEqual(repaired,bad)
        clean = importers.sweep()
        self.assertEqual(clean['codex'],1)
        self.assertNotIn('problems',clean)
        self.assertNotEqual(importers.HEARTBEAT.read_bytes(),baseline)

    def test_metadata_enormous_first_record_is_bounded_and_retryable_for_both_formats(self):
        for source in ('claude-code','codex'):
            bad = self.native(source,source+'-metadata-bad')
            bad.write_bytes(b'x'*(transcript_reader.MAX_METADATA_BYTES+1))
            self.age(bad,1800)
            good = self.native(source,source+'-metadata-good',age=7200)
            seen,problems = {},[]
            self.assertEqual(self.sweep_source(source,seen,problems,[bad,good]),1)
            self.assertEqual(len(problems),1)
            self.assertIn('metadata exceeds 1 MB',problems[0])
            self.assertNotIn(self.key(source,bad),seen)

    def test_incomplete_and_malformed_body_records_remain_retryable(self):
        for source in ('claude-code','codex'):
            for suffix in (b'{"incomplete":',b'not valid JSON\n'):
                bad = self.native(source,source+'-body-bad')
                with bad.open('ab') as handle:handle.write(suffix)
                self.age(bad,1800)
                problems=[]
                seen={}
                self.assertEqual(self.sweep_source(source,seen,problems,[bad]),0)
                self.assertEqual(len(problems),1)
                self.assertNotIn(self.key(source,bad),seen)
        self.assertFalse(self.events())

    def test_malformed_native_metadata_types_are_not_queued_or_marked_seen(self):
        for source in ('claude-code','codex'):
            for field in ('id','cwd'):
                bad=self.native(source,source+'-type-bad')
                rows=[json.loads(line) for line in bad.read_text().splitlines()]
                if source=='codex':
                    rows[0]['payload'][field]=['malformed']
                else:
                    rows[0]['sessionId' if field=='id' else field]=['malformed']
                bad.write_text(''.join(json.dumps(row)+'\n' for row in rows))
                self.age(bad,1800)
                seen,problems={},[]
                self.assertEqual(self.sweep_source(source,seen,problems,[bad]),0)
                self.assertEqual(len(problems),1)
                self.assertNotIn(self.key(source,bad),seen)
        self.assertFalse(self.events())

    def test_error_details_escape_paths_and_never_include_exception_payload(self):
        path=Path('/synthetic/'+('long\nname'*200)+'.jsonl')
        problem=importers._source_problem('codex',path,RuntimeError('PRIVATE_PAYLOAD\n'+'secret'*1000))
        self.assertLessEqual(len(problem),importers.MAX_IMPORT_PROBLEM_CHARS)
        self.assertNotIn('\n',problem)
        self.assertNotIn('PRIVATE_PAYLOAD',problem)
        self.assertIn('RuntimeError',problem)

    def test_metadata_prefix_budget_covers_all_lines_not_only_one_line(self):
        path=self.root/'prefix.jsonl'
        path.write_bytes((b'{"padding":"'+b'x'*24000+b'"}\n')*51)
        with self.assertRaisesRegex(ValueError,'metadata exceeds'):
            list(transcript_reader.metadata_lines(path,max_lines=51))

    def test_existing_checkpoint_does_not_bypass_full_source_cap_or_change_on_failure(self):
        path=self.native('claude-code','checkpoint')
        checkpoint={'bytes':10,'lines':1,'revision':hashlib.sha256(b'prefix').hexdigest()}
        state=SimpleNamespace(transcript_path=str(path),transcript_checkpoint=checkpoint.copy())
        with mock.patch.object(transcript_reader,'MAX_TRANSCRIPT_BYTES',32), \
             mock.patch.object(transcript_reader,'_snapshot') as snapshot:
            with self.assertRaisesRegex(ValueError,'Transcript exceeds'):
                transcript_reader.read_transcript_from_state(state)
        self.assertEqual(state.transcript_checkpoint,checkpoint)
        snapshot.assert_not_called()

    def test_reader_retains_post_stat_growth_guard(self):
        path=self.root/'growth.jsonl'
        path.write_bytes(b'x'*65)
        with mock.patch.object(transcript_reader,'MAX_TRANSCRIPT_BYTES',64), \
             mock.patch.object(transcript_reader,'check_transcript_size'):
            with self.assertRaisesRegex(ValueError,'Transcript exceeds'):
                transcript_reader.read_transcript(path)


if __name__=='__main__':
    unittest.main()
