"""Independent public contracts for the pilot certificate checker and receipt IO.

No frozen development/held-out corpus or label file is opened. These are tiny
hand-written logic and artifact contracts, not benchmark outcomes. Application
admission and the upstream Lean engine are not stubbed or invoked here.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from python_checker import verify_certificate, serialize_rules, MAX_BYTES
from run_pilot import append_attempt, error_record, mutate_certificate, write_json


def term(name):
    return "<urn:pilot:" + name + ">"


def triple(name):
    return [term("a"), term("type"), term(name)]


def fixture():
    rules = [
        {"id": "r1", "body": [["?x", term("type"), term("A")]],
         "head": ["?x", term("type"), term("B")]},
        {"id": "r2", "body": [["?x", term("type"), term("B")]],
         "head": ["?x", term("type"), term("C")]},
    ]
    table = serialize_rules(rules)
    asserted = "\t".join(triple("A")) + "\n"
    rows = [
        ["0", "1", "x", term("a"), *triple("B"), *triple("A")],
        ["1", "1", "x", term("a"), *triple("C"), *triple("B")],
    ]
    cert = {
        "rules_tsv": table, "asserted_tsv": asserted,
        "horn_tsv": "".join("\t".join(row) + "\n" for row in rows),
        "goal": triple("C"),
    }
    expected = {"rules_text": table, "facts": [triple("A")], "goal": triple("C")}
    return cert, expected


class CertificateContracts(unittest.TestCase):
    def assert_not_authorized(self, cert, expected):
        result = verify_certificate(cert, expected)
        self.assertFalse(result["accepted"], result)
        self.assertFalse(result["goal_supported"], result)
        return result

    def test_two_hops_need_valid_prior_premises(self):
        cert, expected = fixture()
        result = verify_certificate(cert, expected)
        self.assertTrue(result["checker_valid"])
        self.assertTrue(result["goal_supported"])
        self.assertTrue(result["accepted"])

    def test_forged_conclusion_is_refused(self):
        cert, expected = fixture()
        self.assert_not_authorized(mutate_certificate(cert, "conclusion_changed"), expected)

    def test_forged_premise_is_refused(self):
        cert, expected = fixture()
        self.assert_not_authorized(mutate_certificate(cert, "premise_changed"), expected)

    def test_forward_reference_is_refused_even_when_rule_instantiation_is_valid(self):
        cert, expected = fixture()
        altered = mutate_certificate(cert, "forward_reference")
        result = self.assert_not_authorized(altered, expected)
        self.assertFalse(result["checker_valid"])

    def test_unknown_rule_index_is_refused(self):
        cert, expected = fixture()
        self.assert_not_authorized(mutate_certificate(cert, "rule_index_out_of_range"), expected)

    def test_missing_binding_is_refused(self):
        cert, expected = fixture()
        rows = cert["horn_tsv"].splitlines()
        first = rows[0].split("\t")
        rows[0] = "\t".join([first[0], "0", *first[4:]])
        cert["horn_tsv"] = "\n".join(rows) + "\n"
        self.assert_not_authorized(cert, expected)

    def test_duplicate_binding_is_refused(self):
        cert, expected = fixture()
        rows = cert["horn_tsv"].splitlines()
        first = rows[0].split("\t")
        rows[0] = "\t".join([first[0], "2", *first[2:4], *first[2:4], *first[4:]])
        cert["horn_tsv"] = "\n".join(rows) + "\n"
        self.assert_not_authorized(cert, expected)

    def test_extra_unused_binding_does_not_forge_a_conclusion(self):
        cert, expected = fixture()
        rows = cert["horn_tsv"].splitlines()
        first = rows[0].split("\t")
        rows[0] = "\t".join([first[0], "2", *first[2:4], "unused", term("other"), *first[4:]])
        cert["horn_tsv"] = "\n".join(rows) + "\n"
        result = verify_certificate(cert, expected)
        self.assertTrue(result["accepted"], result)

    def test_partial_valid_certificate_is_not_proof_of_the_goal(self):
        cert, expected = fixture()
        cert["horn_tsv"] = cert["horn_tsv"].splitlines()[0] + "\n"
        result = verify_certificate(cert, expected)
        self.assertTrue(result["checker_valid"], result)
        self.assertFalse(result["goal_supported"])
        self.assertFalse(result["accepted"])
        self.assertEqual(result["reason"], "goal-not-established")

    def test_empty_certificate_can_establish_only_an_asserted_goal(self):
        cert, expected = fixture()
        cert["horn_tsv"] = ""
        self.assert_not_authorized(cert, expected)
        cert["goal"] = expected["goal"] = triple("A")
        result = verify_certificate(cert, expected)
        self.assertTrue(result["checker_valid"])
        self.assertTrue(result["accepted"])

    def test_asserted_snapshot_is_bound_outside_the_certificate(self):
        cert, expected = fixture()
        self.assert_not_authorized(mutate_certificate(cert, "asserted_injection"), expected)

    def test_rule_table_is_bound_outside_the_certificate(self):
        cert, expected = fixture()
        self.assert_not_authorized(mutate_certificate(cert, "rules_replacement"), expected)

    def test_goal_is_bound_outside_the_certificate(self):
        cert, expected = fixture()
        self.assert_not_authorized(mutate_certificate(cert, "goal_binding_changed"), expected)

    def test_truncated_or_extra_step_columns_are_refused(self):
        cert, expected = fixture()
        self.assert_not_authorized(mutate_certificate(cert, "truncated_step"), expected)
        cert, expected = fixture()
        cert["horn_tsv"] += "\tgarbage"
        self.assert_not_authorized(cert, expected)

    def test_oversized_certificate_is_refused(self):
        cert, expected = fixture()
        cert["horn_tsv"] = "x" * (MAX_BYTES + 1)
        self.assert_not_authorized(cert, expected)

    def test_supported_profile_refuses_unicode_line_separators_and_controls(self):
        for separator in ("\u2028", "\u0085", "\v", "\f", "\r"):
            with self.subTest(codepoint=ord(separator)):
                cert, expected = fixture()
                cert["horn_tsv"] = cert["horn_tsv"].replace("\n", separator)
                self.assert_not_authorized(cert, expected)

    def test_ascii_delete_control_inside_bound_term_is_refused(self):
        cert, expected = fixture()
        bad = "<urn:pilot:a" + chr(127) + ">"
        for name in ("asserted_tsv", "horn_tsv"):
            cert[name] = cert[name].replace(term("a"), bad)
        cert["goal"][0] = expected["goal"][0] = bad
        expected["facts"][0][0] = bad
        self.assert_not_authorized(cert, expected)

    def test_literal_backslash_escape_is_not_a_supported_urn(self):
        cert, expected = fixture()
        bad = "<urn:pilot:a" + chr(92) + "x7f>"
        for name in ("asserted_tsv", "horn_tsv"):
            cert[name] = cert[name].replace(term("a"), bad)
        cert["goal"][0] = expected["goal"][0] = bad
        expected["facts"][0][0] = bad
        self.assert_not_authorized(cert, expected)

    def test_negative_or_non_ascii_rule_natural_is_refused(self):
        for value in ("-1", "١", "1.0"):
            with self.subTest(value=value):
                cert, expected = fixture()
                rows = cert["horn_tsv"].splitlines()
                first = rows[0].split("\t")
                first[0] = value
                rows[0] = "\t".join(first)
                cert["horn_tsv"] = "\n".join(rows) + "\n"
                self.assert_not_authorized(cert, expected)


class ReceiptContracts(unittest.TestCase):
    def test_mutations_do_not_modify_the_original_submitted_artifact(self):
        cert, _ = fixture()
        before = copy.deepcopy(cert)
        for kind in ("asserted_injection", "rules_replacement", "goal_binding_changed",
                     "drop_all_steps", "rule_index_out_of_range", "binding_value_changed",
                     "conclusion_changed", "premise_changed", "truncated_step", "forward_reference"):
            mutate_certificate(cert, kind)
            self.assertEqual(cert, before)

    def test_unavailable_record_does_not_claim_a_checked_negative(self):
        value = error_record("checker", TimeoutError())
        self.assertEqual(value["status"], "unavailable")
        self.assertIsNone(value["checker_valid"])
        self.assertFalse(value["accepted"])

    def test_atomic_json_replaces_a_public_snapshot_and_keeps_jsonl_failures(self):
        with tempfile.TemporaryDirectory(prefix="public-proof-receipt-") as temporary:
            root = Path(temporary)
            snapshot = root / "predictions.json"
            journal = root / "attempts.jsonl"
            first = {"id": "public1", "current": error_record("current", ValueError())}
            second = {"id": "public2", "current": {"target_admitted": True}}
            append_attempt(journal, first)
            write_json(snapshot, [first])
            append_attempt(journal, second)
            write_json(snapshot, [first, second])
            self.assertEqual(json.loads(snapshot.read_text()), [first, second])
            self.assertEqual([json.loads(row) for row in journal.read_text().splitlines()],
                             [first, second])
            self.assertFalse(snapshot.with_name(snapshot.name + ".tmp").exists())


if __name__ == "__main__":
    unittest.main()
