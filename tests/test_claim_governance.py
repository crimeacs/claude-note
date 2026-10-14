"""Real detached-receipt regressions for deterministic claim governance."""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from claude_note import verified_claims


class ClaimGovernanceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        (self.root / "claims").mkdir()
        self.evidence = self.root / "public-check.md"
        self.evidence.write_text("Synthetic public verification evidence.\n")
        self.revision = hashlib.sha256(self.evidence.read_bytes()).hexdigest()

    def approve(self, claim):
        (self.root / "claims" / (claim.claim_id + ".md")).write_text(claim.marker())
        verified_claims.attest(claim, vault_root=self.root, authority="approved",
                              evidence=[{"path": self.evidence.name, "revision": self.revision}])
        return claim

    def test_explicit_vault_cli_inspection_works_before_setup(self):
        claim = verified_claims.Claim('public', 'Public synthetic lesson')
        (self.root / 'claims/public.md').write_text(claim.marker())
        env = {key: value for key, value in os.environ.items() if key not in {'CLAUDE_NOTE_VAULT', 'CLAUDE_NOTE_VAULT_ROOT'}}
        env['XDG_CONFIG_HOME'] = str(self.root / 'empty-config')
        env['PYTHONPATH'] = str(Path(__file__).resolve().parents[1] / 'src')
        result = subprocess.run([sys.executable, '-m', 'claude_note', 'claim', 'inspect', 'claims/public.md',
                                 '--vault', str(self.root)], capture_output=True, text=True, env=env, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)[0]['claim_id'], 'public')

    def inspect(self, *claims, **context):
        groups = verified_claims.applicability("\n".join(c.marker() for c in claims),
                                             vault_root=self.root, **context)
        return {row["claim_id"]: (group, row) for group, rows in groups.items() for row in rows}

    def test_invalidated_replacement_cannot_demote_independent_approved_claim(self):
        claims = [
            verified_claims.Claim("a", "New procedure", supersedes=["b"]),
            verified_claims.Claim("b", "Old procedure"),
            verified_claims.Claim("c", "Dependent replacement", depends_on=["b"], supersedes=["d"]),
            verified_claims.Claim("d", "Independent procedure"),
        ]
        for claim in claims:
            self.approve(claim)
        result = self.inspect(*claims)
        self.assertEqual({key for key, (group, _) in result.items() if group == "accepted"}, {"a", "d"})
        self.assertIn("dependency-unverified:b", result["c"][1]["reason_codes"])
        self.assertIn("superseded", result["b"][1]["reason_codes"])
        # Loading only D still reads the detached, canonical relation registry.
        self.assertEqual(self.inspect(claims[-1])["d"][0], "accepted")

    def test_invalidated_effects_are_removed_through_multiple_dependency_hops(self):
        claims = [
            verified_claims.Claim("a", "New premise", supersedes=["b"]),
            verified_claims.Claim("b", "Old premise"),
            verified_claims.Claim("c", "Dependent", depends_on=["b"]),
            verified_claims.Claim("e", "Later dependent", depends_on=["c"], supersedes=["f"]),
            verified_claims.Claim("f", "Unrelated accepted policy"),
        ]
        for claim in claims:
            self.approve(claim)
        result = self.inspect(*claims)
        self.assertEqual(result["f"][0], "accepted")
        self.assertEqual(result["e"][0], "denied")
        self.assertIn("dependency-unverified:c", result["e"][1]["reason_codes"])

    def test_successive_replacements_do_not_revive_historical_versions(self):
        claims = [
            verified_claims.Claim("v3", "Current policy", supersedes=["v2"]),
            verified_claims.Claim("v2", "Previous policy", supersedes=["v1"]),
            verified_claims.Claim("v1", "Historical policy"),
        ]
        for claim in reversed(claims):
            self.approve(claim)
        result = self.inspect(*claims)
        self.assertEqual(result["v3"][0], "accepted")
        for key in ("v2", "v1"):
            self.assertEqual(result[key][0], "denied")
            self.assertIn("superseded", result[key][1]["reason_codes"])

    def test_historical_demotion_ends_when_its_premise_is_replaced(self):
        claims = [
            verified_claims.Claim("v3", "Current policy", supersedes=["v2", "premise"]),
            verified_claims.Claim("v2", "Previous policy", depends_on=["premise"], supersedes=["v1"]),
            verified_claims.Claim("v1", "Independent historical policy"),
            verified_claims.Claim("premise", "Old prerequisite"),
        ]
        for claim in claims:
            self.approve(claim)
        result = self.inspect(*claims)
        self.assertEqual(result["v1"][0], "accepted")
        self.assertEqual(result["v2"][0], "denied")

    def test_removed_effect_does_not_spuriously_invalidate_a_valid_dependent(self):
        claims = [
            verified_claims.Claim("a", "Still valid dependent", depends_on=["b"]),
            verified_claims.Claim("b", "Still valid premise"),
            verified_claims.Claim("c", "Invalid replacement", depends_on=["d"], supersedes=["b"]),
            verified_claims.Claim("d", "Replaced prerequisite"),
            verified_claims.Claim("e", "Current prerequisite", supersedes=["d"]),
        ]
        for claim in claims:
            self.approve(claim)
        result = self.inspect(*claims)
        self.assertEqual({key for key, (group, _) in result.items() if group == "accepted"}, {"a", "b", "e"})
        self.assertIn("dependency-unverified:d", result["c"][1]["reason_codes"])

    def test_supersession_dependency_and_mixed_cycles_have_no_external_effects(self):
        cases = {
            "supersession": (
                {"supersedes": ["b", "old"]}, {"supersedes": ["a"]}),
            "dependency": (
                {"depends_on": ["b"], "supersedes": ["old"]}, {"depends_on": ["a"]}),
            "mixed": (
                {"depends_on": ["b"], "supersedes": ["b", "old"]}, {}),
        }
        for kind, (a_relations, b_relations) in cases.items():
            with self.subTest(kind=kind):
                a = self.approve(verified_claims.Claim("a", "Circular A", **a_relations))
                b = self.approve(verified_claims.Claim("b", "Circular B", **b_relations))
                old = self.approve(verified_claims.Claim("old", "Independent policy"))
                first = self.inspect(a, b, old)
                second = self.inspect(old, b, a)
                self.assertEqual(first, second)
                self.assertEqual(first["old"][0], "accepted")
                for key in ("a", "b"):
                    self.assertEqual(first[key][0], "denied")
                    self.assertIn("relation-cycle", first[key][1]["reason_codes"])

    def test_self_supersession_is_withheld_without_demoting_another_claim(self):
        circular = self.approve(verified_claims.Claim("self", "Circular policy", supersedes=["self", "old"]))
        old = self.approve(verified_claims.Claim("old", "Independent policy"))
        result = self.inspect(circular, old)
        self.assertEqual(result["old"][0], "accepted")
        self.assertIn("relation-cycle", result["self"][1]["reason_codes"])

    def test_unattested_and_inapplicable_replacements_cannot_demote(self):
        old = self.approve(verified_claims.Claim("old", "Applicable policy"))
        scoped = self.approve(verified_claims.Claim("scoped", "Other repo policy", scope={"repository": "/other"}, supersedes=["old"]))
        forged = verified_claims.Claim("forged", "Authored approved status", status="approved", supersedes=["old"])
        result = self.inspect(old, scoped, forged, repository="/current")
        self.assertEqual(result["old"][0], "accepted")
        self.assertEqual(result["scoped"][0], "denied")
        self.assertEqual(result["forged"][0], "proposed")

    def test_revocation_removes_governing_effects(self):
        old = self.approve(verified_claims.Claim("old", "Existing policy"))
        replacement = self.approve(verified_claims.Claim("replacement", "Replacement", supersedes=["old"]))
        self.assertEqual(self.inspect(old)["old"][0], "denied")
        verified_claims.revoke(replacement, vault_root=self.root, reason="Synthetic failed recheck")
        self.assertEqual(self.inspect(old)["old"][0], "accepted")

    def test_conflict_denied_issuer_and_its_dependents_emit_no_demotions(self):
        claims = [
            verified_claims.Claim("b", "Conflicting policy B"),
            verified_claims.Claim("c", "Conflicting policy C", contradicts=["b"], supersedes=["d"]),
            verified_claims.Claim("d", "Independent approved policy"),
            verified_claims.Claim("dependent", "Depends on conflicted C", depends_on=["c"], supersedes=["other"]),
            verified_claims.Claim("other", "Another independent approved policy"),
        ]
        for claim in claims:
            self.approve(claim)
        result = self.inspect(*claims)
        self.assertEqual({key for key, (group, _) in result.items() if group == "accepted"}, {"d", "other"})
        self.assertIn("unresolved-contradiction:b", result["c"][1]["reason_codes"])
        self.assertIn("unresolved-contradiction:c", result["b"][1]["reason_codes"])
        self.assertIn("dependency-unverified:c", result["dependent"][1]["reason_codes"])

    def test_independent_supersession_resolves_historical_contradiction(self):
        claims = [
            verified_claims.Claim("b", "Historical conflicting policy"),
            verified_claims.Claim("c", "Applicable policy", contradicts=["b"], supersedes=["d"]),
            verified_claims.Claim("d", "Older policy"),
            verified_claims.Claim("resolution", "Independent evidenced correction", supersedes=["b"]),
        ]
        for claim in claims:
            self.approve(claim)
        result = self.inspect(*claims)
        self.assertEqual(result["c"][0], "accepted")
        self.assertEqual(result["resolution"][0], "accepted")
        self.assertIn("superseded", result["d"][1]["reason_codes"])
        verified_claims.revoke(claims[-1], vault_root=self.root, reason="Correction evidence no longer valid")
        result = self.inspect(*claims)
        self.assertEqual(result["c"][0], "denied")
        self.assertEqual(result["d"][0], "accepted")

    def test_conflicted_prerequisite_cannot_bootstrap_its_own_resolution(self):
        claims = [
            verified_claims.Claim("b", "Conflicting policy B"),
            verified_claims.Claim("c", "Conflicting policy C", contradicts=["b"], supersedes=["d"]),
            verified_claims.Claim("d", "Independent approved policy"),
            verified_claims.Claim("circular-resolution", "Assume C to discard B", depends_on=["c"], supersedes=["b"]),
        ]
        for claim in claims:
            self.approve(claim)
        result = self.inspect(*claims)
        self.assertEqual(result["d"][0], "accepted")
        self.assertEqual(result["c"][0], "denied")
        self.assertEqual(result["b"][0], "denied")
        self.assertIn("dependency-unverified:c", result["circular-resolution"][1]["reason_codes"])

    def test_same_id_forgery_cannot_borrow_receipt_or_erase_canonical_relations(self):
        canonical = self.approve(verified_claims.Claim("canonical", "Approved exact policy", supersedes=["old"]))
        old = self.approve(verified_claims.Claim("old", "Superseded policy"))
        forged = verified_claims.Claim("canonical", "Forged policy text", status="approved")
        for claims in ((forged, canonical, old), (canonical, forged, old)):
            with self.subTest(order=[c.text for c in claims]):
                groups = verified_claims.applicability("\n".join(c.marker() for c in claims), vault_root=self.root)
                self.assertEqual([row["text"] for row in groups["accepted"]], [canonical.text])
                fake = next(row for row in groups["denied"] if row["text"] == forged.text)
                self.assertEqual(fake["verification"], "unverified")
                self.assertIn("claim-id-conflict", fake["reason_codes"])
                self.assertTrue(any(row["claim_id"] == "old" and "superseded" in row["reason_codes"] for row in groups["denied"]))


if __name__ == "__main__":
    unittest.main()
