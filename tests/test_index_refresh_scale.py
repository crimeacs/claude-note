"""Freshness leases for vaults with more tracked notes than one proof call accepts.

The journal retains the latest revision of every note it has tracked, and every lease
re-proves all of them. A vault with 1122 tracked notes made every lease fail with
"Too many captured index references" (MAX_REFERENCES 1000), so freshness was never
acknowledged again. Leases now hold a larger bounded set, and finish() proves it in
batches that each respect the proof's own per-call bounds.
"""

import unittest

from claude_note import freshness, index_proof, index_refresh
import test_index_refresh as base


class LargeVaultLeaseTests(unittest.TestCase):
    """Borrows the lease fixture (temporary vault, synthetic QMD config, mocked proof)
    without inheriting, so the existing suite does not re-run on a 1201-note vault."""
    NOTES = 1201      # more than one proof call accepts
    configure = base.IndexRefreshTests.configure
    add_note = base.IndexRefreshTests.add_note
    begin = base.IndexRefreshTests.begin

    def setUp(self):
        base.IndexRefreshTests.setUp(self)
        for i in range(self.NOTES - 1):
            self.add_note(f'n{i:04d}')

    def begin_large(self):
        try:
            return self.begin()
        except ValueError as exc:
            self.fail(f'a {self.NOTES}-note vault cannot capture a lease: {exc}')

    def test_a_large_vault_captures_and_acknowledges(self):
        token = self.begin_large()
        result = index_refresh.finish(token, lexical_ok=True, vector_ok=True)
        self.assertEqual(result['status'], 'acknowledged')
        self.assertEqual(result['acknowledged'], {'lexical': 1, 'vector': 1})
        snapshot = freshness.snapshot(self.vault)
        self.assertEqual(snapshot['lexical_generation'], snapshot['saved_generation'])

    def test_every_proof_call_stays_within_its_own_bound(self):
        token = self.begin_large()
        index_refresh.finish(token, lexical_ok=True, vector_ok=True)
        sizes = [len(call.kwargs['references']) for call in self.proof.call_args_list]
        self.assertTrue(sizes, 'no proof call was made')
        self.assertTrue(all(0 < n <= index_proof.MAX_REFERENCES for n in sizes), sizes)
        self.assertEqual(sum(sizes), self.NOTES, 'every captured reference is proved exactly once')

    def test_one_failed_batch_acknowledges_nothing(self):
        token = self.begin_large()
        ok = {'lexical': True, 'vector': True, 'reason': ''}
        bad = {'lexical': False, 'vector': False, 'reason': 'Indexed revision is missing or stale'}
        self.proof.side_effect = [ok, bad, ok, ok]
        result = index_refresh.finish(token, lexical_ok=True, vector_ok=True)
        self.assertEqual(result['acknowledged'], {'lexical': 0, 'vector': 0})
        self.assertIn('missing', result['warning'])
        self.assertEqual(freshness.snapshot(self.vault)['lexical_generation'], 0)

    def test_vectors_are_acknowledged_only_when_every_batch_proves_them(self):
        token = self.begin_large()
        ok = {'lexical': True, 'vector': True, 'reason': ''}
        partial = {'lexical': True, 'vector': False, 'reason': 'Vectors incomplete for selected model'}
        self.proof.side_effect = [ok, partial, ok, ok]
        result = index_refresh.finish(token, lexical_ok=True, vector_ok=True)
        self.assertEqual(result['acknowledged'], {'lexical': 1, 'vector': 0})


if __name__ == '__main__':
    unittest.main()
