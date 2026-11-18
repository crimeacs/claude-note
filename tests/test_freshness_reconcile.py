"""Owner-run reconciliation of tracked notes changed or deleted outside claude-note.

Without it, a note edited by another program or deleted keeps every freshness lease
unacknowledged forever (by design: proofs never prune references automatically). The
owner runs `claude-note index-refresh reconcile`, reviews the list, and `--apply`
records the current on-disk state. Note content is never changed.
"""

import hashlib
import json
import os
import unittest

from claude_note import freshness, index_refresh
import test_index_refresh as base


class ReconcileTests(unittest.TestCase):
    configure = base.IndexRefreshTests.configure
    add_note = base.IndexRefreshTests.add_note
    begin = base.IndexRefreshTests.begin

    def setUp(self):
        base.IndexRefreshTests.setUp(self)
        for name in ('kept', 'edited', 'deleted'):
            self.add_note(name)
        self.edited_text = 'changed by another program\n'
        (self.vault / 'edited.md').write_text(self.edited_text)
        os.remove(self.vault / 'deleted.md')

    def manifest(self):
        return json.dumps(freshness.snapshot(self.vault), sort_keys=True)

    def test_dry_run_lists_both_kinds_and_changes_nothing(self):
        before = self.manifest()
        report = freshness.reconcile_external(vault_root=self.vault)
        self.assertEqual([i['path'] for i in report['edited']], ['edited.md'])
        self.assertEqual([i['path'] for i in report['deleted']], ['deleted.md'])
        self.assertFalse(report['applied'])
        self.assertEqual(self.manifest(), before)

    def test_apply_records_the_current_state_without_touching_notes(self):
        report = freshness.reconcile_external(vault_root=self.vault, apply=True)
        self.assertTrue(report['applied'])
        snap = freshness.snapshot(self.vault)
        current = hashlib.sha256(self.edited_text.encode()).hexdigest()
        self.assertEqual(snap['retained_revisions']['edited.md']['revision'], current)
        self.assertEqual(snap['dirty']['edited.md']['revision'], current)      # must be proved again
        self.assertNotIn('deleted.md', snap['retained_revisions'])
        self.assertIn('kept.md', snap['retained_revisions'])
        self.assertEqual((self.vault / 'edited.md').read_text(), self.edited_text)
        audit = self.vault / '.claude-note/mutations/freshness' / (report['operation_id'] + '.json')
        self.assertTrue(audit.exists())
        self.assertEqual(json.loads(audit.read_text())['deleted'][0]['path'], 'deleted.md')

    def test_after_apply_a_lease_proves_the_current_revision(self):
        freshness.reconcile_external(vault_root=self.vault, apply=True)
        token = self.begin()
        result = index_refresh.finish(token, lexical_ok=True, vector_ok=True)
        refs = {r['path']: r['revision'] for c in self.proof.call_args_list for r in c.kwargs['references']}
        self.assertEqual(refs['edited.md'], hashlib.sha256(self.edited_text.encode()).hexdigest())
        self.assertNotIn('deleted.md', refs)
        self.assertEqual(result['acknowledged'], {'lexical': 1, 'vector': 1})
        self.assertEqual(freshness.reconcile_external(vault_root=self.vault),
                         {'edited': [], 'deleted': [], 'skipped': [], 'applied': False})

    def test_symlinked_paths_are_reported_not_reconciled(self):
        target = self.root / 'outside.md'
        target.write_text('outside\n')
        os.remove(self.vault / 'kept.md')
        os.symlink(target, self.vault / 'kept.md')
        report = freshness.reconcile_external(vault_root=self.vault, apply=True)
        self.assertIn({'path': 'kept.md', 'reason': 'not a regular file'}, report['skipped'])
        self.assertIn('kept.md', freshness.snapshot(self.vault)['retained_revisions'])


if __name__ == '__main__':
    unittest.main()
