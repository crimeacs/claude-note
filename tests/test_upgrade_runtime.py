"""Existing-service upgrade contracts with synthetic paths and launchd stubs."""
import io
import plistlib
import subprocess
import tempfile
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from claude_note import entry, setup_recall


class UpgradeRecallRuntimeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for name, value in [('HOME', self.root), ('SHARE', self.root / 'share')]:
            self.stack.enter_context(mock.patch.object(setup_recall, name, value))
        self.stack.enter_context(mock.patch.object(setup_recall.sys, 'platform', 'darwin'))
        self.stack.enter_context(mock.patch.object(setup_recall, 'qmd_major', return_value=2))
        self.sdk = self.root / 'SDK & tools/dist/index.js'
        self.sdk.parent.mkdir(parents=True)
        (self.sdk.parent.parent / 'package.json').write_text('{"version":"2.8.3"}')
        self.stack.enter_context(mock.patch.object(setup_recall, 'qmd_sdk', return_value=self.sdk))
        self.stack.enter_context(mock.patch.object(setup_recall, 'real_node', return_value=str(self.root / 'Node & tools/node')))

    def done(self, code=0, error=''):
        return subprocess.CompletedProcess([], code, stdout='', stderr=error)

    def test_bootstrap_failure_is_reported_instead_of_installed(self):
        with mock.patch.object(setup_recall.subprocess, 'run', side_effect=[self.done(), self.done(5, 'denied by launchd')]) as run:
            verdict = setup_recall.install_server_agent('/synthetic/qmd')
        self.assertTrue(verdict.startswith('failed:'), verdict)
        self.assertIn('denied by launchd', verdict)
        self.assertEqual([call.args[0][1] for call in run.call_args_list], ['bootout', 'bootstrap'])

    def test_failed_bootout_of_loaded_job_does_not_claim_restart(self):
        with mock.patch.object(setup_recall.subprocess, 'run', side_effect=[self.done(5, 'old process remains'), self.done()]) as run:
            verdict = setup_recall.install_server_agent('/synthetic/qmd')
        self.assertTrue(verdict.startswith('failed:'), verdict)
        self.assertIn('old process remains', verdict)
        self.assertEqual([call.args[0][1] for call in run.call_args_list], ['bootout', 'print'])

    def test_successful_bootstrap_must_actually_load_job_and_escape_plist(self):
        with mock.patch.object(setup_recall.subprocess, 'run', side_effect=[self.done(), self.done(), self.done(3)]):
            verdict = setup_recall.install_server_agent('/synthetic/qmd')
        self.assertIn('not loaded after bootstrap', verdict)
        path = self.root / 'Library/LaunchAgents' / (setup_recall.SERVER_LABEL + '.plist')
        data = plistlib.loads(path.read_bytes())
        self.assertEqual(data['ProgramArguments'][0], str(self.root / 'Node & tools/node'))
        self.assertEqual(data['ProgramArguments'][1], str(Path(setup_recall.__file__).with_name('recall_server.mjs')))
        self.assertEqual(data['ProgramArguments'][2], str(self.sdk))
        self.assertEqual(list(path.parent.glob('.com.claude-note.recall-server.*')), [])

    def test_absent_job_never_probes_qmd_or_installs_agent(self):
        missing = self.done(113, 'Could not find service "com.claude-note.recall-server" in domain for user gui')
        with mock.patch.object(setup_recall.subprocess, 'run', return_value=missing), \
                mock.patch.object(setup_recall, 'qmd_exe') as qmd, \
                mock.patch.object(setup_recall, 'install_server_agent') as install, redirect_stdout(io.StringIO()):
            self.assertEqual(setup_recall.upgrade_existing_server(), 0)
        qmd.assert_not_called()
        install.assert_not_called()
        self.assertFalse((self.root / 'Library').exists())

    def test_latency_service_uses_interactive_policy_and_migrates_old_default(self):
        previous = {'Label': setup_recall.SERVER_LABEL,
                    'ProgramArguments': ['/old/node', '/old/recall_server.mjs', '/old/sdk.js', '8182'],
                    'Nice': 5, 'EnvironmentVariables': {'QMD_METAL_KEEP_RESIDENCY': '1'}}
        with mock.patch.object(setup_recall.subprocess, 'run', return_value=self.done()):
            verdict = setup_recall.install_server_agent('/synthetic/qmd', previous=previous)
        self.assertTrue(verdict.startswith('installed'), verdict)
        path = self.root / 'Library/LaunchAgents' / (setup_recall.SERVER_LABEL + '.plist')
        data = plistlib.loads(path.read_bytes())
        self.assertEqual(data['ProcessType'], 'Interactive')
        self.assertEqual(data['Nice'], 0)
        previous.update(ProcessType='Background', Nice=10)
        with mock.patch.object(setup_recall.subprocess, 'run', return_value=self.done()):
            setup_recall.install_server_agent('/synthetic/qmd', previous=previous)
        data = plistlib.loads(path.read_bytes())
        self.assertEqual(data['ProcessType'], 'Background')
        self.assertEqual(data['Nice'], 10)

    def test_existing_job_with_missing_prerequisite_fails_for_retry(self):
        agent = self.root / 'Library/LaunchAgents' / (setup_recall.SERVER_LABEL + '.plist')
        agent.parent.mkdir(parents=True)
        agent.write_bytes(plistlib.dumps({'Label': setup_recall.SERVER_LABEL,
            'ProgramArguments': ['/old/node', '/old/recall_server.mjs', '/old/sdk.js', '8182']}))
        with mock.patch.object(setup_recall.subprocess, 'run', return_value=self.done()), \
                mock.patch.object(setup_recall, 'qmd_exe', return_value=None), redirect_stdout(io.StringIO()):
            self.assertEqual(setup_recall.upgrade_existing_server(), 1)
        self.assertIn('/old/node', agent.read_text())

    def test_loaded_job_without_plist_fails_closed_without_guessing_corpus(self):
        with mock.patch.object(setup_recall.subprocess, 'run', return_value=self.done()), \
                mock.patch.object(setup_recall, 'qmd_exe', return_value='/synthetic/qmd'), \
                mock.patch.object(setup_recall, 'install_server_agent') as install, \
                redirect_stderr(io.StringIO()) as error:
            self.assertEqual(setup_recall.upgrade_existing_server(), 1)
        install.assert_not_called()
        self.assertIn('restore its configuration', error.getvalue())

    def test_query_permission_failure_is_not_an_absent_job(self):
        with mock.patch.object(setup_recall.subprocess, 'run', return_value=self.done(1, 'Operation not permitted')), \
                mock.patch.object(setup_recall, 'install_server_agent') as install, redirect_stderr(io.StringIO()) as error:
            self.assertEqual(setup_recall.upgrade_existing_server(), 1)
        install.assert_not_called()
        self.assertIn('Operation not permitted', error.getvalue())

    def test_unsupported_argument_shape_does_not_replace_or_stop_job(self):
        previous = {'Label': setup_recall.SERVER_LABEL,
                    'ProgramArguments': ['/old/node', '--unsupported', '/old/recall_server.mjs', '/old/sdk.js']}
        agent = self.root / 'Library/LaunchAgents' / (setup_recall.SERVER_LABEL + '.plist')
        agent.parent.mkdir(parents=True)
        agent.write_bytes(plistlib.dumps(previous))
        with mock.patch.object(setup_recall.subprocess, 'run', return_value=self.done()) as run, \
                redirect_stderr(io.StringIO()):
            self.assertEqual(setup_recall.upgrade_existing_server(), 1)
        self.assertEqual(plistlib.loads(agent.read_bytes()), previous)
        self.assertEqual([call.args[0][1] for call in run.call_args_list], ['print'])

    def test_unusable_launchctl_is_visible_and_internal_entry_forwards_status(self):
        with mock.patch.object(setup_recall.subprocess, 'run', side_effect=OSError('launchctl unavailable')), \
                redirect_stderr(io.StringIO()) as error:
            self.assertEqual(setup_recall.upgrade_existing_server(), 1)
        self.assertIn('launchctl unavailable', error.getvalue())
        with mock.patch.object(entry.sys, 'argv', ['claude-note', '_upgrade-recall-server']), \
                mock.patch.object(setup_recall, 'upgrade_existing_server', return_value=7) as upgrade:
            self.assertEqual(entry.main(), 7)
        upgrade.assert_called_once_with()
