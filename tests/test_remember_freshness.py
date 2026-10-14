"""Immediate indexing must respect captured generations and failed stages."""

import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from claude_note import freshness, index_refresh, mutation, remember


class RememberFreshnessTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='remember-freshness-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        mutation.create_note(self.root / 'first.md', 'Synthetic first lesson',
                             vault_root=self.root, operation_id='first')
        self.captured = {}

    def begin(self, *, index):
        self.assertEqual(index, 'synthetic-index')
        self.captured['generation'] = freshness.snapshot(self.root)['saved_generation']
        return {'token': 'synthetic-token'}

    def finish(self, token, *, lexical_ok):
        self.assertEqual(token, 'synthetic-token')
        if lexical_ok:
            freshness.acknowledge(self.captured['generation'], capability='lexical', vault_root=self.root)
        return {}

    def test_success_cannot_acknowledge_a_write_arriving_during_update(self):
        def update(*args, **kwargs):
            mutation.create_note(self.root / 'later.md', 'Synthetic later lesson',
                                 vault_root=self.root, operation_id='later')
            return SimpleNamespace(returncode=0)

        with mock.patch.object(remember.recall_mod.Settings, 'load', return_value=SimpleNamespace()), \
             mock.patch.object(remember.recall_mod, 'qmd_executable', return_value='/synthetic/qmd'), \
             mock.patch.object(index_refresh, 'begin', side_effect=self.begin), \
             mock.patch.object(index_refresh, 'finish', side_effect=self.finish) as finish, \
             mock.patch.object(remember.subprocess, 'run', side_effect=update):
            ok, _ = remember.reindex('synthetic-index')
        self.assertTrue(ok)
        finish.assert_called_once_with('synthetic-token', lexical_ok=True)
        state = freshness.snapshot(self.root)
        self.assertEqual((state['saved_generation'], state['lexical_generation'], state['vector_generation']), (2, 1, 0))
        self.assertIn('later.md', state['dirty'])

    def test_failed_or_timed_out_update_never_acknowledges_text(self):
        for outcome in (SimpleNamespace(returncode=7), subprocess.TimeoutExpired('synthetic-qmd', 1)):
            with self.subTest(outcome=type(outcome).__name__), \
                 mock.patch.object(remember.recall_mod.Settings, 'load', return_value=SimpleNamespace()), \
                 mock.patch.object(remember.recall_mod, 'qmd_executable', return_value='/synthetic/qmd'), \
                 mock.patch.object(index_refresh, 'begin', side_effect=self.begin), \
                 mock.patch.object(index_refresh, 'finish', side_effect=self.finish) as finish, \
                 mock.patch.object(remember.subprocess, 'run', **({'side_effect': outcome} if isinstance(outcome, Exception) else {'return_value': outcome})):
                ok, _ = remember.reindex('synthetic-index')
            self.assertFalse(ok)
            finish.assert_called_once_with('synthetic-token', lexical_ok=False)
            self.assertEqual(freshness.snapshot(self.root)['lexical_generation'], 0)
            self.assertIn('first.md', freshness.snapshot(self.root)['dirty'])


if __name__ == '__main__':
    unittest.main()
