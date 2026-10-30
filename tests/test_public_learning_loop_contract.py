"""Frozen independent public learning-loop behavior, not productivity scores.

The extractor below is deliberately deterministic test transport. It cannot
establish model extraction quality, semantic truth, native retrieval or host
delivery. Root/arm isolation and no external transport use the existing public
fixture. The new module's absence is not a valid development-harness RED.
"""
from collections.abc import Mapping
import copy
import hashlib
import importlib
import json
import os
from pathlib import Path
import unittest
from unittest import mock

from support.public_own_learning import public_fixture


def detached(value):
    if isinstance(value, Mapping):
        return {key: detached(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [detached(item) for item in value]
    return value


class PublicLearningLoopContractTests(unittest.TestCase):
    def setUp(self):
        self.original_home = os.environ.get("HOME")
        self.owned = self.enterContext(public_fixture())
        self.module = importlib.import_module("claude_note.learning_loop")
        self.error = self.module.LearningLoopError
        self.root = self.owned.root / "actual-public-loop"
        self.requests = []
        self.loops = []

    def tearDown(self):
        for loop in reversed(self.loops):
            loop.close()
        self.assertEqual(os.environ.get("HOME"), self.original_home)
        self.assertEqual(self.owned.calls["transport"], 0)

    def extractor(self, request):
        self.requests.append(detached(request))
        return {"lessons": [{"key": "retry", "text": request["experience"]["actual_response"],
                             "source_spans": list(request["source_spans"])}]}

    def loop(self, name="a", **kwargs):
        defaults = {"run_id": "public-run", "arm_id": "learning", "public_only": True,
                    "repository": "public-demo", "environment": "test", "extractor": self.extractor,
                    "max_memory_chars": 4096}
        defaults.update(kwargs)
        loop = self.module.LearningLoop(self.root/name, **defaults)
        self.loops.append(loop)
        return loop

    def observe(self, loop, task_id="step-0", response="Keep the original retry key.", **kwargs):
        return loop.observe(task_id, "Use this public retry observation.", response,
                            {"passed": True, "check": "public-executable-contract"}, **kwargs)

    def learn(self, loop, task_id="step-0", response="Keep the original retry key."):
        observation = self.observe(loop, task_id, response)
        outcome = loop.learn(observation)
        self.assertEqual(outcome["status"], "completed", outcome)
        return observation, outcome

    def entries(self, loop, task="public retry key"):
        result = loop.retrieve(task)
        self.assertLessEqual(len(result["context"]), 4096)
        self.assertEqual(result["characters"], len(result["context"]))
        self.assertEqual(result["utf8_bytes"], len(result["context"].encode()))
        return result["entries"]

    def matching_files(self, root, predicate):
        return [(path, path.read_bytes()) for path in root.rglob("*")
                if path.is_file() and not path.is_symlink() and predicate(path.read_bytes())]

    def test_requires_explicit_true_public_opt_in(self):
        for value in (False, None, 1, "true"):
            with self.subTest(value=value), self.assertRaises(self.error):
                self.loop(name="denied-"+str(value), public_only=value)

    def test_root_identity_cannot_be_rebound_to_another_run_or_arm(self):
        loop = self.loop()
        self.learn(loop)
        loop.close()
        for changed in ({"run_id": "other-run"}, {"arm_id": "frozen"},
                        {"repository": "other-project"}, {"environment": "production"}):
            with self.subTest(changed=changed), self.assertRaises(self.error):
                self.loop(**changed)

    def test_observation_is_real_revision_bound_source_before_extraction(self):
        loop = self.loop()
        observed = self.observe(loop)
        self.assertEqual(observed["kind"], "observation")
        self.assertEqual(observed["sequence"], 0)
        self.assertRegex(observed["source_revision"], r"\A[0-9a-f]{64}\Z")
        self.assertTrue(observed["source_spans"])
        for span in observed["source_spans"]:
            self.assertRegex(span, r"#L[1-9][0-9]*@[0-9a-f]{64}\Z")
            source = Path(span.rsplit("#L", 1)[0])
            self.assertTrue(source.is_absolute() and source.is_relative_to(self.root))
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), observed["source_revision"])
        self.assertEqual(self.requests, [])
        self.assertEqual(loop.learn(observed)["status"], "completed")
        request = self.requests[0]
        self.assertEqual(request["experience"]["actual_response"], "Keep the original retry key.")
        self.assertEqual(request["source_spans"], observed["source_spans"])
        self.assertEqual(request["source_revision"], observed["source_revision"])

    def test_real_mutation_is_used_and_returned_entry_is_exact_revision(self):
        loop = self.loop()
        observed = self.observe(loop)
        mutation = self.owned.app.mutation
        with mock.patch.object(mutation, "mutate", wraps=mutation.mutate) as actual:
            self.assertEqual(loop.learn(observed)["status"], "completed")
        self.assertGreater(actual.call_count, 0)
        entry = self.entries(loop)[0]
        self.assertEqual(entry["run_id"], "public-run")
        self.assertEqual(entry["arm_id"], "learning")
        self.assertEqual(entry["source_revision"], observed["source_revision"])
        self.assertEqual(entry["source_spans"], observed["source_spans"])
        self.assertEqual(entry["sequence"], 0)
        self.assertTrue(self.matching_files(self.root, lambda raw: hashlib.sha256(raw).hexdigest() == entry["revision"]))

    def test_extractor_receives_nested_immutable_detached_request(self):
        def immutable(request):
            with self.assertRaises(TypeError):
                request["arm_id"] = "foreign"
            with self.assertRaises(TypeError):
                request["experience"]["actual_response"] = "future answer key"
            with self.assertRaises((TypeError, AttributeError)):
                request["source_spans"].append("uncaptured")
            return self.extractor(request)
        loop = self.loop(extractor=immutable)
        self.learn(loop)

    def test_cross_run_or_arm_receipt_is_denied_before_extractor(self):
        loop = self.loop()
        foreign = self.loop(name="b", arm_id="other-learning")
        observed = self.observe(foreign)
        with self.assertRaises(self.error):
            loop.learn(observed)
        self.assertEqual(self.requests, [])

    def test_forged_revision_span_sequence_or_digest_is_denied(self):
        loop = self.loop()
        observed = self.observe(loop)
        changes = ({"source_revision": "0"*64}, {"source_spans": ["/public/gold#L1@"+"0"*64]},
                   {"sequence": 99}, {"receipt_digest": "0"*64}, {"run_id": "different-run"})
        for change in changes:
            with self.subTest(change=change), self.assertRaises(self.error):
                loop.learn({**copy.deepcopy(observed), **change})
        self.assertEqual(self.requests, [])

    def test_current_capture_bytes_tamper_denies_extraction(self):
        loop = self.loop()
        observed = self.observe(loop)
        source = Path(observed["source_spans"][0].rsplit("#L", 1)[0])
        source.write_bytes(source.read_bytes()+b"\n{}\n")
        with self.assertRaises(self.error):
            loop.learn(observed)
        self.assertEqual(self.requests, [])

    def test_uncaptured_or_empty_extraction_spans_fail_without_memory(self):
        for number, spans in enumerate(([], ["/public/gold#L1@"+"0"*64])):
            def bad(request, spans=spans):
                return {"lessons": [{"key": "retry", "text": "Do not adopt an oracle.", "source_spans": spans}]}
            loop = self.loop(name="invalid-spans-"+str(number), extractor=bad)
            observed = self.observe(loop)
            result = loop.learn(observed)
            self.assertEqual(result["status"], "failed")
            self.assertEqual(loop.retrieve("retry")["entries"], [])
            self.assertEqual(loop.learn(observed), result)

    def test_duplicate_keys_and_authority_claims_are_schema_failures(self):
        for kind in ("duplicate", "authority"):
            def invalid(request, kind=kind):
                lesson = {"key": "retry", "text": "Public proposed lesson.", "source_spans": list(request["source_spans"])}
                if kind == "authority":
                    lesson["authority"] = "verified"
                return {"lessons": [lesson, dict(lesson)] if kind == "duplicate" else [lesson]}
            loop = self.loop(name=kind, extractor=invalid)
            self.assertEqual(loop.learn(self.observe(loop))["status"], "failed")
            self.assertEqual(loop.retrieve("retry")["entries"], [])

    def test_stable_key_correction_replaces_effective_text_and_keeps_old_revision(self):
        loop = self.loop()
        self.learn(loop, response="Retry key is global.")
        old_entry = self.entries(loop)[0]
        old_files = self.matching_files(self.root, lambda raw: hashlib.sha256(raw).hexdigest() == old_entry["revision"])
        self.assertTrue(old_files)
        self.learn(loop, task_id="step-1", response="Retry key is scoped to the operation payload.")
        entries = self.entries(loop)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["text"], "Retry key is scoped to the operation payload.")
        self.assertNotEqual(entries[0]["revision"], old_entry["revision"])
        for path, before in old_files:
            self.assertEqual(path.read_bytes(), before)

    def test_explicit_supersession_keeps_unrelated_lessons(self):
        def propose(request):
            phase = request["sequence"]
            spans = list(request["source_spans"])
            if phase == 0:
                return {"lessons": [{"key": "old-retry", "text": "Old retry rule.", "source_spans": spans},
                                    {"key": "logging", "text": "Retain correlation logs.", "source_spans": spans}]}
            return {"lessons": [{"key": "corrected-retry", "text": "Corrected retry rule.",
                                 "source_spans": spans, "supersedes": ["old-retry"]}]}
        loop = self.loop(extractor=propose)
        self.learn(loop)
        self.learn(loop, "step-1", "Correction observed in a public executable check.")
        keys = {entry["key"] for entry in self.entries(loop, "retry logging")}
        self.assertIn("corrected-retry", keys)
        self.assertIn("logging", keys)
        self.assertNotIn("old-retry", keys)

    def test_foreign_scope_is_not_delivered_as_current_context(self):
        def foreign_scope(request):
            return {"lessons": [{"key": "foreign", "text": "Production-only retry rule.",
                "source_spans": list(request["source_spans"]),
                "scope": {"repository": "other-project", "environment": "production"}}]}
        loop = self.loop(extractor=foreign_scope)
        loop.learn(self.observe(loop))
        self.assertEqual(loop.retrieve("retry")["entries"], [])

    def test_constructor_company_and_workflow_constraints_cannot_be_widened(self):
        scope = {"company_id": "public-company-a", "workflow": "invoice-correction"}
        loop = self.loop(scope=scope)
        observed = self.observe(loop)
        self.assertEqual(loop.learn(observed)["status"], "completed")
        entry = loop.retrieve("retry")["entries"][0]
        self.assertEqual(entry["scope"]["company_id"], scope["company_id"])
        self.assertEqual(entry["scope"]["workflow"], scope["workflow"])
        self.assertEqual(self.requests[0]["scope"]["company_id"], scope["company_id"])
        self.assertEqual(self.requests[0]["scope"]["workflow"], scope["workflow"])
        with self.assertRaises(self.error):
            self.observe(loop, "step-1", scope={"company_id": "public-company-b"})
        with self.assertRaises(self.error):
            self.observe(loop, "step-1", scope={"workflow": "unrelated-workflow"})

    def test_absent_company_context_cannot_deliver_company_scoped_observation(self):
        loop = self.loop()
        observed = self.observe(loop, scope={"company_id": "public-company-a", "workflow": "retry"})
        self.assertEqual(loop.learn(observed)["status"], "completed")
        self.assertEqual(loop.retrieve("retry")["entries"], [])
        matching = loop.retrieve("retry", scope={"company_id": "public-company-a", "workflow": "retry"})
        self.assertEqual(len(matching["entries"]), 1)
        foreign = loop.retrieve("retry", scope={"company_id": "public-company-b", "workflow": "retry"})
        self.assertEqual(foreign["entries"], [])

    def test_company_scope_is_part_of_reopen_identity(self):
        loop = self.loop(scope={"company_id": "public-company-a", "workflow": "retry"})
        self.learn(loop)
        loop.close()
        with self.assertRaises(self.error):
            self.loop(scope={"company_id": "public-company-b", "workflow": "retry"})

    def test_context_uses_whole_entries_and_excludes_raw_noise(self):
        concise = "Keep the operation retry key."
        def extractor(request):
            return {"lessons": [{"key": "retry", "text": concise, "source_spans": list(request["source_spans"])}]}
        loop = self.loop(max_memory_chars=96, extractor=extractor)
        noisy = "NOISE-PUBLIC-ONLY "*50 + concise
        self.learn(loop, response=noisy)
        result = loop.retrieve("operation retry key")
        self.assertLessEqual(result["characters"], 96)
        self.assertIn(concise, result["context"])
        self.assertNotIn("NOISE-PUBLIC-ONLY", result["context"])
        self.assertTrue(all(entry["text"] in result["context"] for entry in result["entries"]))
        self.assertIn("NOISE-PUBLIC-ONLY", loop.raw_history())

    def test_oversized_single_lesson_is_not_truncated_into_a_false_whole_entry(self):
        loop = self.loop(max_memory_chars=40)
        self.learn(loop, response="PUBLIC-VERY-LONG-LESSON "*20)
        result = loop.retrieve("PUBLIC-VERY-LONG-LESSON")
        self.assertLessEqual(result["characters"], 40)
        self.assertEqual(result["entries"], [])

    def test_repeated_observations_consolidate_one_effective_stable_key(self):
        loop = self.loop()
        for phase in range(3):
            self.learn(loop, "step-"+str(phase), "Public repeated retry observation "+str(phase))
        self.assertEqual(len(self.entries(loop)), 1)
        self.assertIn("Public repeated retry observation 0", loop.raw_history())
        self.assertIn("Public repeated retry observation 1", loop.raw_history())
        self.assertIn("Public repeated retry observation 2", loop.raw_history())

    def test_replay_and_reopen_do_not_reextract_or_duplicate_effects(self):
        loop = self.loop()
        observation, learned = self.learn(loop)
        first = loop.retrieve("retry")
        self.assertEqual(loop.learn(observation), learned)
        self.assertEqual(self.observe(loop), observation)
        self.assertEqual(len(self.requests), 1)
        loop.close()
        reopened = self.loop()
        self.assertEqual(reopened.retrieve("retry")["context"], first["context"])
        self.assertEqual(reopened.learn(observation), learned)
        self.assertEqual(len(self.requests), 1)

    def test_changed_task_replay_preserves_original_evidence(self):
        loop = self.loop()
        observed = self.observe(loop)
        before = loop.raw_history()
        with self.assertRaises(self.error):
            self.observe(loop, response="A future or corrected response under an old identity.")
        self.assertEqual(loop.raw_history(), before)
        self.assertEqual(self.observe(loop), observed)

    def test_failed_extractor_is_durable_and_not_automatically_reinvoked(self):
        calls = []
        def fail(request):
            calls.append(request["task_id"])
            raise RuntimeError("public controlled extractor failure")
        loop = self.loop(extractor=fail)
        observed = self.observe(loop)
        first = loop.learn(observed)
        self.assertEqual(first["status"], "failed")
        self.assertEqual(loop.learn(observed), first)
        loop.close()
        reopened = self.loop(extractor=fail)
        self.assertEqual(reopened.learn(observed), first)
        self.assertEqual(calls, ["step-0"])

    def test_future_cutoff_never_exposes_later_capture_or_memory(self):
        loop = self.loop()
        self.learn(loop, response="Earlier retry observation.")
        self.learn(loop, "step-1", "Later retry correction.")
        earlier = loop.retrieve("retry", before_sequence=1)
        self.assertIn("Earlier retry observation.", earlier["context"])
        self.assertNotIn("Later retry correction.", earlier["context"])
        with self.assertRaises(self.error):
            loop.retrieve("retry", before_sequence=3)
        with self.assertRaises(self.error):
            loop.retrieve("retry", before_sequence=-1)

    def test_preauthored_oracle_file_is_not_implicitly_adopted(self):
        loop = self.loop()
        path = self.root/"a"/"authored-oracle.md"
        path.write_text("PUBLIC-GOLD-ORACLE future target answer.\n")
        self.assertEqual(loop.retrieve("PUBLIC-GOLD-ORACLE")["entries"], [])
        self.assertNotIn("PUBLIC-GOLD-ORACLE", loop.raw_history())
        self.assertEqual(path.read_text(), "PUBLIC-GOLD-ORACLE future target answer.\n")

    def test_tampered_learned_revision_is_not_delivered(self):
        loop = self.loop()
        self.learn(loop)
        entry = self.entries(loop)[0]
        matched = self.matching_files(self.root, lambda raw: hashlib.sha256(raw).hexdigest() == entry["revision"])
        self.assertTrue(matched)
        for path, raw in matched:
            path.write_bytes(raw+b"\nPUBLIC-TAMPER\n")
        with self.assertRaises(self.error):
            loop.retrieve("retry")

    def test_raw_history_is_own_completed_capture_under_a_budget(self):
        loop = self.loop()
        foreign = self.loop(name="b", arm_id="raw-history-control")
        self.observe(loop, response="OWN-PUBLIC-HISTORY")
        self.observe(foreign, response="FOREIGN-PUBLIC-HISTORY")
        history = loop.raw_history()
        self.assertIn("OWN-PUBLIC-HISTORY", history)
        self.assertNotIn("FOREIGN-PUBLIC-HISTORY", history)
        self.assertLessEqual(len(loop.raw_history(max_chars=48)), 48)
        self.assertEqual(loop.retrieve("OWN-PUBLIC-HISTORY")["entries"], [])


if __name__ == "__main__":
    unittest.main()
