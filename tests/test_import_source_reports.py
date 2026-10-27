"""Bounded numeric import diagnostics from public, temporary source fixtures."""

import json
import os
import tempfile
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

from claude_note import importers


class ImportSourceReportTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        for attr, relative in {
            'SEEN_FILE': 'transcripts/seen.json', 'HEARTBEAT': 'logs/import-sweep.ok',
            'CODEX_SESSIONS': 'codex', 'CLAUDE_PROJECTS': 'claude',
            'CURSOR_DB': 'absent.sqlite', 'CURSOR_WORKSPACES': 'workspaces',
            'IMPORTS_DIR': 'imports', 'TRANSCRIPTS_DIR': 'transcripts',
        }.items():
            self.stack.enter_context(mock.patch.object(importers, attr, self.root / relative))
        self.stack.enter_context(mock.patch.object(importers, 'WATCH_DIRS', []))

    def native_failures(self, source, count):
        folder = self.root / ('codex/2026/10/01' if source == 'codex' else 'claude/public-project')
        folder.mkdir(parents=True, exist_ok=True)
        paths = []
        for index in range(count):
            name = f'rollout-public-{index}.jsonl' if source == 'codex' else f'public-{index}.jsonl'
            path = folder / name
            # Missing Codex identity / invalid Claude identity: real metadata
            # validation fails before publication or external work.
            path.write_text('{"sessionId": 7, "public_body": "DO_NOT_REPORT_BODY"}\n')
            old = time.time() - 3600
            os.utime(path, (old, old))
            paths.append(path)
        return paths

    def assert_report(self, report, queued, failed, omitted):
        self.assertEqual(report['queued_sessions'], queued)
        self.assertEqual(report['failed_items'], failed)
        self.assertEqual(report['problem_details_omitted'], omitted)
        self.assertTrue(all(type(value) is int for value in report.values()))

    def test_each_native_source_reports_all_failures_beyond_detail_cap(self):
        for source, function in [('codex', importers.sweep_codex),
                                 ('claude-code', importers.sweep_claude_code)]:
            with self.subTest(source=source):
                paths = self.native_failures(source, 103)
                before = {path: path.read_bytes() for path in paths}
                report, problems, seen = {}, importers.ImportProblems(), {}
                self.assertEqual(function(seen, 0, problems=problems, report=report), 0)
                self.assert_report(report, 0, 103, 3)
                self.assertEqual((len(problems), problems.total, problems.omitted), (100, 103, 3))
                self.assertEqual(seen, {})
                self.assertEqual({path: path.read_bytes() for path in paths}, before)
                self.assertNotIn('DO_NOT_REPORT_BODY', json.dumps(problems))

    def test_direct_native_errors_keep_full_report_without_problem_sink(self):
        for source, function in [('codex', importers.sweep_codex),
                                 ('claude-code', importers.sweep_claude_code)]:
            with self.subTest(source=source):
                self.native_failures(source, 102)
                report = {}
                with self.assertRaises(importers.SourceImportError) as caught:
                    function({}, 0, report=report)
                self.assert_report(report, 0, 102, 2)
                self.assertEqual(caught.exception.report, report)
                self.assertEqual(caught.exception.failed, 102)
                self.assertEqual(caught.exception.queued, 0)

    def test_empty_native_sources_report_explicit_zeroes(self):
        for function in [importers.sweep_codex, importers.sweep_claude_code]:
            report = {}
            self.assertEqual(function({}, 0, report=report), 0)
            self.assert_report(report, 0, 0, 0)

    def test_later_source_aggregates_survive_global_detail_saturation(self):
        self.native_failures('codex', 103)
        self.native_failures('claude-code', 4)
        importers.HEARTBEAT.parent.mkdir(parents=True)
        importers.HEARTBEAT.write_bytes(b'older-clean-success')
        result = importers.sweep()
        self.assertEqual(result['failed'], 107)
        self.assertEqual((len(result['problems']), result['problem_details_omitted']), (100, 7))
        self.assert_report(result['reports']['codex'], 0, 103, 3)
        # No Claude detail fits globally, but its LOCAL report omits none.
        self.assert_report(result['reports']['claude-code'], 0, 4, 0)
        self.assertEqual(importers.HEARTBEAT.read_bytes(), b'older-clean-success')
        status = json.loads(importers.HEARTBEAT.with_suffix('.json').read_text())['counts']
        self.assertEqual(status, result)
        self.assertNotIn('DO_NOT_REPORT_BODY', json.dumps(status))

    def test_each_escaped_source_failure_is_counted_and_later_sources_run(self):
        names = ['zips', 'cursor', 'codex', 'claude-code']
        for failed_source in names:
            with self.subTest(source=failed_source), ExitStack() as stack:
                for source, attr in zip(names, ['sweep_zips', 'sweep_cursor', 'sweep_codex',
                                               'sweep_claude_code']):
                    if source == failed_source:
                        stack.enter_context(mock.patch.object(importers, attr,
                            side_effect=RuntimeError('DO_NOT_REPORT_EXCEPTION')))
                    else:
                        def success(*args, problems=None, report=None, **kwargs):
                            return importers._finish_source('public', 2,
                                                            importers.ImportProblems(), problems, report)
                        stack.enter_context(mock.patch.object(importers, attr, side_effect=success))
                result = importers.sweep()
                self.assertEqual(result['failed'], 1)
                self.assert_report(result['reports'][failed_source], 0, 1, 0)
                self.assertEqual([result[name] for name in names],
                                 [0 if name == failed_source else 2 for name in names])
                self.assertNotIn('DO_NOT_REPORT_EXCEPTION', json.dumps(result))

    def test_escaped_source_import_error_preserves_queued_and_omitted_totals(self):
        errors = importers.ImportProblems()
        errors.extend(['public: fixed authored failure'] * 105)
        error = importers.SourceImportError('codex', 2, errors)
        with mock.patch.object(importers, 'sweep_codex', side_effect=error):
            result = importers.sweep()
        self.assertEqual(result['codex'], 2)
        self.assertEqual(result['failed'], 105)
        self.assertEqual((len(result['problems']), result['problem_details_omitted']), (100, 5))
        self.assert_report(result['reports']['codex'], 2, 105, 5)

    def test_clean_sweep_still_publishes_success_with_zero_native_reports(self):
        result = importers.sweep()
        self.assertTrue(importers.HEARTBEAT.exists())
        self.assertNotIn('failed', result)
        for name in ['codex', 'claude-code']:
            self.assert_report(result['reports'][name], 0, 0, 0)


if __name__ == '__main__':
    unittest.main()
