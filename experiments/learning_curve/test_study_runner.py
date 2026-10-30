"""Offline controller plumbing contracts, never benchmark or model outcomes.

Public invented rows/spans below test serialization, cutoff, citation binding and
failure retention only. No actor/provider runs, real task scores, private vault,
native retrieval, or production configuration are used. Domain scoring is
explicitly stubbed only when inspecting the controller's partial-row projection.
The independent task/verifier contracts qualify the actual public curriculum.
"""
from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
from types import MappingProxyType, SimpleNamespace
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
spec = importlib.util.spec_from_file_location("public_study_runner_contract", HERE / "run_study.py")
study = importlib.util.module_from_spec(spec)
spec.loader.exec_module(study)


class PublicRunnerContracts(unittest.TestCase):
    def setUp(self):
        self.home = os.environ.get("HOME")
        self.temporary = self.enterContext(tempfile.TemporaryDirectory(prefix="public-runner-plumbing-"))
        self.root = Path(self.temporary).resolve()
        self.enterContext(mock.patch.object(study, "BudgetedProvider",
            side_effect=AssertionError("No provider in controller plumbing tests")))
        self.enterContext(mock.patch.object(socket, "socket",
            side_effect=AssertionError("No socket in controller plumbing tests")))
        self.enterContext(mock.patch.object(study.subprocess, "Popen",
            side_effect=AssertionError("No subprocess in controller plumbing tests")))

    def tearDown(self):
        self.assertEqual(os.environ.get("HOME"), self.home)

    def request(self):
        revision = "1" * 64
        return {"run_id": "public-plumbing", "arm_id": "learned", "sequence": 1,
            "task_id": "plumbing-only-not-a-corpus-cell", "source_revision": revision,
            "source_spans": [f"/public-owned/capture.jsonl#L{n}@{revision}" for n in range(1, 4)],
            "scope": {"repository": "public-learning-study", "environment": "public-study",
                      "company_id": "public-controller-only", "workflow": "routing"},
            "experience": {"task": "Public controller fixture.", "actual_response": "Public action bytes.",
                           "verifier_feedback": {"attempts": [{"fixture_only": True}]}},
            "current_memory": [{"key": "public-rule", "text": "An authored controller fixture.",
                "scope": {"company_id": "public-controller-only"},
                "relative_path": "/public-owned/prior.md", "source_spans": ["host-path-not-for-provider"],
                "revision": "2" * 64, "observation_receipt_digest": "3" * 64}]}

    def test_cutoff_uses_only_prior_acquisition_then_keeps_snapshot_three(self):
        self.assertEqual([study.frozen_cutoff("frozen", n) for n in range(1, 13)],
                         [0, 1, 2, 3, 3, 3, 3, 3, 3, 3, 3, 3])
        for arm in ("learned", "raw_history"):
            self.assertEqual([study.frozen_cutoff(arm, n) for n in range(1, 13)], [None] * 12)

    def test_future_or_unknown_control_is_not_silently_accepted(self):
        for arm, ordinal in (("foreign", 1), ("learned", 0), ("learned", 13),
                             ("learned", True), ("learned", "1")):
            with self.subTest(arm=arm, ordinal=ordinal), self.assertRaises(ValueError):
                study.frozen_cutoff(arm, ordinal)

    def test_only_learning_arms_extract_and_terminal_extraction_is_disabled(self):
        self.assertEqual([n for n in range(1, 13) if study.should_learn("frozen", n)], [1, 2, 3])
        self.assertEqual([n for n in range(1, 13) if study.should_learn("learned", n)], list(range(1, 12)))
        self.assertFalse(any(study.should_learn("raw_history", n) for n in range(1, 13)))

    def test_extraction_view_strips_local_paths_and_receipts_but_retains_own_experience(self):
        request = self.request()
        safe, aliases, feedback = study.extraction_request(request)
        self.assertEqual(safe["experience"], request["experience"])
        self.assertEqual(safe["scope"], request["scope"])
        self.assertEqual(safe["source_revision"], request["source_revision"])
        self.assertEqual(aliases, dict(zip(("s0", "s1", "s2"), request["source_spans"])))
        self.assertEqual(feedback, "s2")
        self.assertEqual(safe["source_spans"], ["s0", "s1", "s2"])
        self.assertEqual(set(safe["current_memory"][0]), {"key", "text", "scope"})
        self.assertNotIn("/public-owned", json.dumps(safe))
        self.assertNotIn("host-path-not-for-provider", json.dumps(safe))
        safe["experience"]["actual_response"] = "Changed detached view"
        self.assertEqual(request["experience"]["actual_response"], "Public action bytes.")

    def test_nested_immutable_request_is_detached_without_changing_source_aliases(self):
        request = self.request()
        request["experience"] = MappingProxyType(request["experience"])
        request["current_memory"] = tuple(MappingProxyType(item) for item in request["current_memory"])
        safe, aliases, feedback = study.extraction_request(MappingProxyType(request))
        self.assertEqual(feedback, "s2")
        self.assertEqual(aliases["s2"], request["source_spans"][-1])
        self.assertIsInstance(safe["experience"], dict)

    def test_mapped_citations_are_exact_current_sources_and_leave_raw_proposal_unchanged(self):
        _, aliases, feedback = study.extraction_request(self.request())
        proposal = {"lessons": [{"key": "rule", "text": "Public proposal.",
                                  "source_spans": ["s0", "s2"], "supersedes": []}]}
        mapped = study.mapped_extraction(proposal, aliases, feedback)
        self.assertEqual(mapped["lessons"][0]["source_spans"], [aliases["s0"], aliases["s2"]])
        self.assertEqual(proposal["lessons"][0]["source_spans"], ["s0", "s2"])

    def test_missing_feedback_or_foreign_alias_is_refused(self):
        _, aliases, feedback = study.extraction_request(self.request())
        for spans in (["s0"], ["foreign", "s2"], []):
            proposal = {"lessons": [{"key": "rule", "text": "Fixture.",
                                      "source_spans": spans, "supersedes": []}]}
            with self.subTest(spans=spans), self.assertRaises((ValueError, KeyError)):
                study.mapped_extraction(proposal, aliases, feedback)
        self.assertEqual(study.mapped_extraction({"lessons": []}, aliases, feedback), {"lessons": []})

    def test_empty_actual_source_set_cannot_be_aliased(self):
        request = self.request()
        request["source_spans"] = []
        with self.assertRaises(ValueError):
            study.extraction_request(request)

    def test_query_projects_only_current_public_request_fields(self):
        task = {"task": {"request": {"operation": "route", "visibility": "restricted",
                    "team": "ops", "region": "internal", "id": "DO-NOT-QUERY-ID"}},
                "scope": {"workflow": "customer-records"}, "policy_revision": "r2",
                "ambient_observations": ["NOT-QUERY-NOISE"], "evaluator_fixture": "NOT-QUERY-TRUTH"}
        self.assertEqual(study.query_for(task), "route restricted ops internal customer-records r2")

    def test_raw_view_preserves_whole_hashed_windows_under_same_character_budget(self):
        lines = ["UNRELATED-PUBLIC-NOISE " * 500, "retry payload keep stable " * 60]
        loop = SimpleNamespace(raw_history=lambda: "\n".join(lines))
        result = study.raw_context(loop, "retry payload")
        self.assertLessEqual(len(result["context"]), study.MEMORY_CHARS)
        self.assertEqual(result["characters"], len(result["context"]))
        self.assertTrue(result["context"].startswith("[raw observation 1,"))
        self.assertEqual(result["receipt"]["context_sha256"], hashlib.sha256(result["context"].encode()).hexdigest())
        for entry in result["entries"]:
            body = lines[entry["sequence"]][entry["offset"]:entry["offset"] + 512]
            part = f"[raw observation {entry['sequence']}, characters {entry['offset']}:{entry['offset'] + len(body)}]\n" + body
            self.assertIn(part, result["context"])
            self.assertEqual(entry["sha256"], hashlib.sha256(part.encode()).hexdigest())
        empty = study.raw_context(SimpleNamespace(raw_history=lambda: ""), "anything")
        self.assertEqual((empty["context"], empty["entries"], empty["characters"]), ("", [], 0))

    def test_source_manifest_covers_declared_runtime_and_direct_experiment_inputs(self):
        source = self.root / "src/claude_note"
        here = self.root / "experiments/learning_curve"
        source.mkdir(parents=True)
        here.mkdir(parents=True)
        files = {source / "core.py": b"# public core\n", here / "run.py": b"# public runner\n",
                 here / "corpus.json": b'{"public_fixture":true}\n'}
        for path, body in files.items():
            path.write_bytes(body)
        (here / "ignored.txt").write_text("Coverage intentionally excludes this suffix.")
        with mock.patch.object(study, "REPO", self.root), mock.patch.object(study, "HERE", here):
            actual = study.source_manifest()
        self.assertEqual(actual, {str(path.relative_to(self.root)): hashlib.sha256(body).hexdigest()
                                  for path, body in files.items()})

    def test_public_disk_admission_is_exact_profile_threshold_not_filesystem_reservation(self):
        threshold = (512 + 64) * 1024 ** 2
        for free, expected in ((threshold - 1, False), (threshold, True), (threshold + 1, True)):
            with self.subTest(free=free), mock.patch.object(study.os, "statvfs",
                    return_value=SimpleNamespace(f_bavail=free, f_frsize=1)):
                self.assertIs(study.disk_admitted(self.root), expected)

    def test_owned_byte_scan_refuses_links_without_following_outside_artifact(self):
        owned = self.root / "owned"
        owned.mkdir()
        (owned / "a.json").write_bytes(b"123")
        (owned / "b.json").write_bytes(b"45")
        self.assertEqual(study.owned_bytes(owned), 5)
        outside = self.root / "outside-public.json"
        outside.write_bytes(b"PUBLIC-DO-NOT-READ")
        (owned / "linked.json").symlink_to(outside)
        with self.assertRaises(ValueError):
            study.owned_bytes(owned)
        self.assertEqual(outside.read_bytes(), b"PUBLIC-DO-NOT-READ")

    def test_partial_score_projection_uses_observed_result_and_keeps_unknown_final_null(self):
        self.assertIsNone(study.observed_scores("plumbing-only", [], False))
        def fixture_score(task_id, actions):
            return {"available": True, "first_action_success": False,
                    "final_task_success": False, "action_count": len(actions)}
        action = {"fixture_only": True}
        with mock.patch.object(study, "score_episode", side_effect=fixture_score):
            partial = study.observed_scores("plumbing-only", [action], False)
            complete = study.observed_scores("plumbing-only", [action, action], True)
        self.assertIsNone(partial["final_task_success"])
        self.assertIs(partial["first_action_success"], False)
        self.assertIs(complete["final_task_success"], False)

    def cells(self):
        return {"company-00": [{"id": "plumbing-only-cell", "ordinal": 1, "phase": "fixture"}]}

    def test_reconciliation_keeps_completed_cell_and_recovers_partial_journal_once(self):
        directory = self.root / "chains/company-00/frozen"
        directory.mkdir(parents=True)
        row = {"company": "company-00", "arm": "frozen", "task_id": "plumbing-only-cell",
               "status": "completed", "scores": None, "controller_fixture_only": True}
        (directory / "plumbing-only-cell.json").write_text(json.dumps(row))
        raw = self.root / "chains/company-00/raw_history"
        raw.mkdir(parents=True)
        event = {"action": {"fixture_only": True}, "feedback": {"success": False},
                 "provider": {"fixture_only": True}}
        (raw / "plumbing-only-cell.actions.jsonl").write_text(json.dumps(event) + "\n")
        with mock.patch.object(study, "observed_scores", return_value=None) as projector:
            result = study.reconcile_rows(self.root, self.cells())
        self.assertEqual(len(result), 3)
        self.assertEqual(len({(r["company"], r["arm"], r["task_id"]) for r in result}), 3)
        self.assertEqual(next(r for r in result if r["arm"] == "frozen"), row)
        partial = next(r for r in result if r["arm"] == "raw_history")
        self.assertEqual(partial["actions"], [event["action"]])
        self.assertEqual(partial["provider_calls"], [event["provider"]])
        self.assertFalse(partial["action_stage_completed"])
        self.assertEqual(partial["status"], "unavailable")
        self.assertIsNone(partial["scores"])
        projector.assert_called_once_with("plumbing-only-cell", [event["action"]], False)

    def test_reconciliation_rejects_foreign_cell_identity_instead_of_relabeling_it(self):
        directory = self.root / "chains/company-00/frozen"
        directory.mkdir(parents=True)
        (directory / "plumbing-only-cell.json").write_text(json.dumps({
            "company": "company-01", "arm": "frozen", "task_id": "plumbing-only-cell"}))
        with self.assertRaises(ValueError):
            study.reconcile_rows(self.root, self.cells())

    def test_overall_deadline_guards_every_paid_admission_stage(self):
        """Static wiring only; no fake actor response or real provider invocation."""
        self.assertEqual(study.STUDY_SECONDS, 1800)
        tree = ast.parse((HERE / "run_study.py").read_text())
        main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
        chain = next(n for n in main.body if isinstance(n, ast.FunctionDef) and n.name == "run_chain")
        extract = next(n for n in chain.body if isinstance(n, ast.FunctionDef) and n.name == "extract")

        def deadline_guard(node):
            self.assertIsInstance(node, ast.If)
            self.assertEqual(ast.dump(node.test, include_attributes=False),
                ast.dump(ast.parse("time.monotonic() >= study_deadline", mode="eval").body,
                         include_attributes=False))
            self.assertIsInstance(node.body[0], ast.Raise)
            self.assertEqual(ast.unparse(node.body[0].exc), "ProviderUnavailable('overall-study-deadline')")

        deadline_guard(extract.body[0])
        task_loop = next(n for n in ast.walk(chain) if isinstance(n, ast.For)
                         and isinstance(n.target, ast.Name) and n.target.id == "task")
        guarded = next(n for n in task_loop.body if isinstance(n, ast.Try))
        deadline_guard(guarded.body[0])
        attempts = next(n for n in guarded.body if isinstance(n, ast.For)
                        and isinstance(n.target, ast.Name) and n.target.id == "attempt")
        deadline_guard(attempts.body[0])
        paid = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name)
                and n.func.value.id == "provider" and n.func.attr == "call"]
        self.assertEqual(len(paid), 2)
        self.assertTrue(all(n in list(ast.walk(extract)) or n in list(ast.walk(attempts)) for n in paid))
        assignment = next(n for n in main.body if isinstance(n, ast.Assign)
                          and any(isinstance(t, ast.Name) and t.id == "study_deadline" for t in n.targets))
        self.assertEqual(ast.unparse(assignment.value), "time.monotonic() + STUDY_SECONDS")
        self.assertLess(assignment.lineno, min(n.lineno for n in paid))


if __name__ == "__main__":
    unittest.main()
