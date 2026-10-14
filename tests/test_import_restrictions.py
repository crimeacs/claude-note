"""Captured restriction contracts, using only disposable synthetic sources."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from claude_note import egress, state_io


class CapturedRestrictionsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='cn-captured-restrictions-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.source = self.root / 'origin.md'
        self.source.write_text('Public synthetic evidence.')
        self.snapshot = self.root / 'snapshots' / 'fixed.jsonl'
        self.snapshot.parent.mkdir()
        self.snapshot.write_text('Captured synthetic evidence.')

    def capture(self, *, origins=None, text=None):
        egress.capture_restrictions(self.snapshot, origins=origins or [str(self.source)],
            captured_text=self.snapshot.read_text() if text is None else text, vault_root=self.root)

    def prepare(self, rules=None, audience='model'):
        return egress.prepare('Synthetic derived prompt.', audience=audience,
            sources=[str(self.snapshot)], rules=rules or {'default': ['local', 'host', 'model', 'staging']})

    def marker(self, audience):
        return '<!-- claude-note:claim ' + json.dumps({'audiences': audience}) + ' -->'

    def test_private_origin_restriction_survives_metadata_removal(self):
        self.source.write_text('---\nprivate: true\n---\nPrivate synthetic evidence.')
        self.capture()
        self.source.write_text('Synthetic source with metadata removed.')
        self.capture()
        for audience in ('model', 'staging'):
            with self.subTest(audience=audience), self.assertRaises(egress.EgressDenied):
                self.prepare(audience=audience)
        self.assertEqual(self.prepare(audience='host'), 'Synthetic derived prompt.')

    def test_captured_text_controls_survive_origin_change(self):
        self.capture(text='---\nprivate: true\n---\nCaptured synthetic private text.')
        self.source.write_text('Replacement with no restrictions.')
        with self.assertRaises(egress.EgressDenied):
            self.prepare()

    def test_transitive_claim_and_protected_ledger_are_frozen(self):
        upstream = self.root / 'upstream.md'
        upstream.write_text(self.marker(['host']))
        self.source.write_text('---\nderivation_sources: ' + json.dumps([str(upstream)]) + '\n---\nDerived facts.')
        self.capture()
        self.source.write_text('Metadata removed.')
        upstream.write_text('Claim marker removed.')
        with self.assertRaises(egress.EgressDenied):
            self.prepare()
        other = self.root / 'other.md'
        other.write_text('No visible controls.')
        egress.record_derivation(other, self.marker(['host']), vault_root=self.root)
        self.capture(origins=[str(other)])
        self.assertIn(str(other), egress.recorded_derivations(self.snapshot))
        with self.assertRaises(egress.EgressDenied):
            self.prepare()

    def test_multiple_origins_and_replays_only_narrow_audiences(self):
        self.source.write_text(self.marker(['host', 'model']))
        self.capture()
        other = self.root / 'other.md'
        other.write_text(self.marker(['host', 'staging']))
        self.capture(origins=[str(other)])
        self.source.write_text('Restriction removed.')
        other.write_text('Restriction removed.')
        self.capture()
        ledger = state_io.read_json(egress._lineage_path(self.snapshot, self.root))
        self.assertEqual(ledger['artifact_audiences'], ['host'])
        self.assertEqual(set(ledger['origins']), {str(self.source), str(other)})
        for audience in ('model', 'staging'):
            with self.assertRaises(egress.EgressDenied):
                self.prepare(audience=audience)

    def test_default_policy_is_live_and_never_frozen_or_granted(self):
        self.capture()
        ledger = state_io.read_json(egress._lineage_path(self.snapshot, self.root))
        self.assertNotIn('artifact_audiences', ledger)
        with self.assertRaises(egress.EgressDenied):
            self.prepare(rules={'default': ['local', 'host']})
        self.assertEqual(self.prepare(rules={'default': ['model']}), 'Synthetic derived prompt.')
        with self.assertRaises(egress.EgressDenied):
            self.prepare(rules={'default': ['model'], 'rules': [
                {'source': str(self.source), 'restrict': True, 'audiences': ['host']}]})

    def test_resolved_origin_policy_survives_parent_alias_retarget_and_replay(self):
        original, replacement = self.root / 'original', self.root / 'replacement'
        original.mkdir()
        replacement.mkdir()
        first, second = original / 'source.md', replacement / 'source.md'
        first.write_text('Original synthetic public-looking bytes.')
        second.write_text('Replacement synthetic public-looking bytes.')
        alias = self.root / 'alias'
        alias.symlink_to(original, target_is_directory=True)
        supplied = alias / 'source.md'
        self.capture(origins=[str(supplied)])
        rules = {'default': ['model'], 'rules': [
            {'source': str(first), 'restrict': True, 'audiences': ['host']}]}
        with self.assertRaises(egress.EgressDenied):
            self.prepare(rules=rules)
        alias.unlink()
        alias.symlink_to(replacement, target_is_directory=True)
        # The live alias now resolves to a public replacement. Its original
        # resolved identity must still restrict the already captured evidence.
        with self.assertRaises(egress.EgressDenied):
            self.prepare(rules=rules)
        self.capture(origins=[str(supplied)])
        ledger = state_io.read_json(egress._lineage_path(self.snapshot, self.root))
        self.assertTrue({str(supplied), str(first), str(second)} <= set(ledger['origins']))
        with self.assertRaises(egress.EgressDenied):
            self.prepare(rules=rules)

    def test_snapshot_ancestor_lineage_is_frozen_in_local_capture_ledger(self):
        upstream = self.root / 'upstream.md'
        upstream.write_text('Public-looking upstream synthetic source.')
        content = ('---\nderivation_sources: ' + json.dumps([str(upstream)]) +
                   '\n---\n' + self.marker(['host']))
        egress.record_derivation(self.snapshot, content, vault_root=self.root)
        ancestor_ledger = egress._lineage_path(self.snapshot, self.root)
        egress.capture_restrictions(self.snapshot, origins=[str(self.source)],
            captured_text=self.snapshot.read_text(), vault_root=self.snapshot.parent)
        local_ledger = egress._lineage_path(self.snapshot, self.snapshot.parent)
        self.assertNotEqual(ancestor_ledger, local_ledger)
        frozen = state_io.read_json(local_ledger)
        self.assertEqual(frozen['artifact_audiences'], ['host'])
        self.assertIn(str(upstream), frozen['origins'])
        ancestor_ledger.unlink()
        # Removing broader-root metadata cannot widen the captured bytes, and
        # its upstream source identity still participates in live owner policy.
        for audience in ('model', 'staging'):
            with self.subTest(audience=audience), self.assertRaises(egress.EgressDenied):
                self.prepare(audience=audience)
        self.assertIn(str(upstream), egress.recorded_derivations(self.snapshot))

    def test_malformed_snapshot_ancestor_lineage_remains_durable_unknown(self):
        ancestor_ledger = egress._lineage_path(self.snapshot, self.root)
        state_io.atomic_json(ancestor_ledger, {'version': 1, 'origins': 'invalid'})
        with self.assertRaises(egress.EgressDenied):
            egress.capture_restrictions(self.snapshot, origins=[str(self.source)],
                captured_text=self.snapshot.read_text(), vault_root=self.snapshot.parent)
        local_ledger = egress._lineage_path(self.snapshot, self.snapshot.parent)
        self.assertTrue(state_io.read_json(local_ledger)['unknown'])
        ancestor_ledger.unlink()
        with self.assertRaises(egress.EgressDenied):
            self.prepare()

    def test_failed_inspection_is_durable_unknown_and_does_not_modify_snapshot(self):
        before = self.snapshot.read_bytes()
        self.source.write_text('<!-- claude-note:claim {bad-json} -->')
        with self.assertRaises(egress.EgressDenied):
            self.capture()
        ledger = state_io.read_json(egress._lineage_path(self.snapshot, self.root))
        self.assertTrue(ledger['unknown'])
        self.assertEqual(self.snapshot.read_bytes(), before)
        self.source.write_text('Malformed control removed.')
        with self.assertRaises(egress.EgressDenied):
            self.capture()
        with self.assertRaises(egress.EgressDenied):
            self.prepare()

    def test_missing_and_linked_origins_fail_closed(self):
        for source in (self.root / 'absent.md', self.root / 'linked.md'):
            if source.name == 'linked.md':
                source.symlink_to(self.source)
            with self.subTest(source=source.name), self.assertRaises(egress.EgressDenied):
                self.capture(origins=[str(source)])

    def test_closure_bound_remains_unknown_instead_of_trusted_truncation(self):
        many = [str(self.root / ('source-' + str(i))) for i in range(2001)]
        # Already captured input metadata is subject to the same closure bound.
        text = '---\nderivation_sources: ' + json.dumps(many) + '\n---\nSynthetic facts.'
        with self.assertRaises(egress.EgressDenied):
            self.capture(text=text)
        self.assertTrue(state_io.read_json(egress._lineage_path(self.snapshot, self.root))['unknown'])


if __name__ == '__main__':
    unittest.main()
