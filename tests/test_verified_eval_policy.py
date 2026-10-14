"""Synthetic boundaries for score integrity, conservative recovery and honest health."""
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from claude_note import evals, evals_cli, health, recall, sentinel, setup_recall


class ScoreIntegrityTests(unittest.TestCase):
    def test_scope_title_and_unscoped_names_cannot_supply_identity(self):
        self.assertFalse(evals.matches("qmd://a/runbook.md", ["b/runbook.md"]))
        self.assertFalse(evals.matches("qmd://a/runbook.md", ["runbook"]))
        self.assertIsNone(evals._rank(["qmd://a/unrelated.md"], ["b/expected.md"], ["Same title"], "Same title"))
        self.assertTrue(evals.matches("qmd://a/日本語.md", ["a/日本語.md"]))

    def test_mirrors_and_revision_kinds_are_explicit(self):
        labels = [{"source_id": "qmd://a/x.md", "revision": "r1", "mirrors": [
            {"source_id": "qmd://b/copy.md", "revision": "copy1"}]}]
        self.assertTrue(evals.matches("qmd://b/copy.md", [], revision="copy1", expected_sources=labels))
        self.assertFalse(evals.matches("qmd://b/copy.md", [], revision="old", expected_sources=labels))
        self.assertFalse(evals.matches("qmd://a/x.md", [], revision="r1", revision_kind="snippet_sha256", expected_sources=labels))
        self.assertFalse(evals.matches("qmd://a/x.md", [], expected_sources=labels))

    def test_labels_splits_and_runtime_fingerprints_separate_trends(self):
        first = evals.Case("same", "same question", ["a/x.md"], "dev")
        changed = evals.Case("same", "same question", ["b/x.md"], "test")
        self.assertNotEqual(evals._set_id([first]), evals._set_id([changed]))
        s = recall.Settings(qmd="/synthetic/qmd", engine="cli", log=False)
        with mock.patch.object(recall, "recall", return_value=recall.Result(status="miss")):
            one = evals.run_retrieval([first], s, lambda *args: [], identity={"complete": True, "corpus": "one"})
            two = evals.run_retrieval([first], s, lambda *args: [], identity={"complete": True, "corpus": "two"})
        one["t"], two["t"] = "before", "after"
        self.assertEqual(evals.comparable(two, [one]), [])
        legacy = {"kind": "retrieval", "set": one["set"], "settings": one["settings"], "t": "old"}
        self.assertEqual(evals.comparable(one, [legacy]), [])

    def test_scoped_lexical_baseline_rejects_out_of_scope_results(self):
        s = recall.Settings(qmd="/synthetic/qmd", collections=["a"], engine="cli")
        with mock.patch.object(recall, "qmd_executable", return_value="/synthetic/qmd"):
            result = evals.scoped_baseline("deployment memory constraints", s, lambda *args: [
                {"file": "qmd://b/runbook.md"}, {"file": "qmd://a/runbook.md"}])
        self.assertEqual(result, ["qmd://a/runbook.md"])

    def test_server_cache_writes_do_not_hide_real_regressions_or_index_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            notes = root/'notes'
            notes.mkdir()
            (notes/'public.md').write_text('Public synthetic indexed evidence.')
            executable = root/'qmd'
            executable.write_text('public synthetic executable identity')
            database = root/'cache/qmd/index.sqlite'
            database.parent.mkdir(parents=True)
            database.write_bytes(b'initial physical cache bytes')
            manifest = {'protocol_version': 2, 'index': {'id': 'default', 'corpus_digest': 'c'*64,
                        'generation': 'g'*64, 'vector_rows': 1, 'vector_models': ['embed'],
                        'collections': [{'name': 'public', 'documents': 1, 'vector_rows': 1}]},
                        'qmd': {'version': '2.8.3', 'sdk_revision': 's'*64},
                        'models': {name: {'id': name, 'revision': name[0]*64, 'ready': False}
                                   for name in ('embed', 'rerank')}}
            settings = recall.Settings(qmd=str(executable), engine='server', collections=['public'])
            with mock.patch.dict(os.environ, {'XDG_CACHE_HOME': str(root/'cache')}), \
                 mock.patch.object(recall, '_collection_dirs', return_value={'public': notes}), \
                 mock.patch.object(recall, 'qmd_executable', return_value=str(executable)), \
                 mock.patch.object(recall, 'server_capabilities', return_value=manifest), \
                 mock.patch.object(evals.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout='qmd2.8.3')):
                before = evals.runtime_identity(settings, [])
                ready = json.loads(json.dumps(manifest))
                for model in ready['models'].values():
                    model['ready'] = True
                database.write_bytes(b'reranking wrote unrelated cache entries')
                Path(str(database)+'-wal').write_bytes(b'different WAL cache bytes')
                after = evals.runtime_identity(settings, [{'engine': 'server', 'manifest': manifest},
                                                          {'engine': 'server', 'manifest': ready}])
                self.assertTrue(before['complete'] and after['complete'])
                self.assertEqual(before, after)
                def run(identity, timestamp, hit):
                    return {'kind': 'retrieval', 't': timestamp, 'scorer_version': evals.SCORER_VERSION,
                            'comparable': identity['complete'], 'fingerprint': {'runtime': identity},
                            'set': 'public', 'settings': {}, 'metrics': {'all': {'hit_at_k': hit}}}
                previous = run(before, 'before', 1)
                current = run(after, 'after', .5)
                self.assertEqual(len(evals.comparable(current, [previous])), 1)
                self.assertTrue(evals.regressions(current, [previous]))
                ready['index']['generation'] = 'new-generation'
                changed = evals.runtime_identity(settings, [{'engine': 'server', 'manifest': ready}])
                self.assertNotEqual(changed['index_digest'], after['index_digest'])
                self.assertEqual(evals.comparable(run(changed, 'changed', 0), [previous]), [])
                ready['index'].pop('corpus_digest')
                unknown = evals.runtime_identity(settings, [{'engine': 'server', 'manifest': ready}])
                self.assertFalse(unknown['complete'])

    def test_policy_contracts_are_longitudinal_and_not_agent_claims(self):
        run = evals.run_policy_contracts()
        self.assertEqual(run["metrics"]["verified_policy"]["attempted"], 16)
        self.assertEqual(run["metrics"]["verified_policy"]["contract_pass_rate"], 1)
        self.assertLess(run["metrics"]["static_instructions"]["contract_pass_rate"], 1)
        self.assertGreater(run["metrics"]["scoped_lexical"]["harmful_actions"], 0)
        self.assertIn("not_agent", run["measurement"])
        failed = evals.run_policy_contracts({"broken": lambda task: (_ for _ in ()).throw(RuntimeError("failed"))})
        self.assertEqual(failed["metrics"]["broken"]["failed"], 16)

    def test_captured_traces_include_missing_and_failed_denominators(self):
        records = [{"task_id": "a", "baseline": "hybrid", "completed": True,
                    "checks": [{"name": "tests pass", "passed": True, "verifier": "pytest"}]}]
        run = evals.score_task_traces(records, ["a", "b"], ["no_memory", "hybrid"], suite="safe-tasks-v1")
        self.assertEqual(run["metrics"]["hybrid"], {"scheduled": 2, "attempted": 1, "completed": 1, "success_rate": .5})
        self.assertEqual(run["metrics"]["no_memory"]["success_rate"], 0)
        with self.assertRaises(ValueError):
            evals.score_task_traces(records * 2, ["a"], ["hybrid"], suite="x")


class SentinelPolicyTests(unittest.TestCase):
    def test_valid_token_mention_does_not_authorize_credential_removal(self):
        text = "ERROR: refresh token is valid; upstream connection timed out"
        result = sentinel.classify_failure({}, text)
        self.assertEqual(result["class"], "transient")
        self.assertNotIn("clear the cached", result["advice"])
        self.assertEqual(sentinel.classify_failure({}, "not invalid_grant; connection timed out")["class"], "transient")

    def test_budget_and_transient_rate_limit_have_different_policies(self):
        transient = sentinel.classify_failure({}, "HTTP 429 rate_limit; Retry-After: 1")
        budget = sentinel.classify_failure({}, "HTTP 429 insufficient_quota credit balance exhausted")
        self.assertEqual(transient["class"], "transient")
        self.assertEqual(transient["retry_after"], 1)
        self.assertEqual(budget["class"], "budget")
        self.assertEqual(sentinel.classify_failure({}, "You've hit your weekly limit; resets tomorrow")["class"], "budget")
        self.assertNotEqual(transient["advice"], budget["advice"])

    def test_structured_provider_error_and_expected_nonzero(self):
        payload = {"tool_response": {"error": {"code": "invalid_grant", "message": "revoked"}}}
        self.assertEqual(sentinel.classify_failure(payload, sentinel.failure_text(payload))["class"], "authentication")
        grep = {"tool_input": {"command": "rg pattern file"}, "tool_response": {"exit_code": 1, "stdout": "", "stderr": ""}}
        self.assertEqual(sentinel.classify_failure(grep, "")["class"], "expected_nonzero")

    def test_repeat_detection_precedes_lookup_and_expires_or_invalidates(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Path(temporary)
            payload = {"session_id": "s", "error": "HTTP 401 Unauthorized"}
            found = {"line": "HTTP 401 Unauthorized", "advice": ["inspect"], "hits": [], "result": None}
            clock = [100.0]
            with mock.patch.object(sentinel, "_seen_path", return_value=fixture / "seen.json"), \
                 mock.patch.object(sentinel, "_settings", return_value={}), \
                 mock.patch.object(recall, "_config_path", return_value=fixture / "config.toml"), \
                 mock.patch.object(sentinel.time, "time", side_effect=lambda: clock[0]), \
                 mock.patch.object(sentinel, "analyse", return_value=found) as lookup, \
                 mock.patch.object(recall, "emit"):
                sentinel.hook_main(io.StringIO(json.dumps(payload)))
                sentinel.hook_main(io.StringIO(json.dumps(payload)))
                self.assertEqual(lookup.call_count, 1)
                payload["credential_revision"] = "changed"
                sentinel.hook_main(io.StringIO(json.dumps(payload)))
                self.assertEqual(lookup.call_count, 2)
                clock[0] += 301
                sentinel.hook_main(io.StringIO(json.dumps(payload)))
                self.assertEqual(lookup.call_count, 3)


class CapabilityHealthTests(unittest.TestCase):
    def test_default_server_probe_requires_exact_source_and_model_readiness(self):
        manifest = {"lexical_ready": True, "vector_ready": False, "rerank_ready": True}
        result = recall.Result(status="hit", engine="server", hits=[recall.Hit(
            uri="qmd://wrong/doctor-canary.md", title="Doctor canary", snippet="synthetic")])
        with mock.patch.object(recall, "server_capabilities", return_value=manifest), \
             mock.patch.object(recall, "recall", return_value=result) as search:
            out = setup_recall.probe_default_server(recall.Settings(), save=False)
        self.assertFalse(out["ok"])
        self.assertEqual(search.call_args.args[1].engine, "server")
        self.assertEqual(search.call_args.args[1].index, "")
        self.assertEqual(search.call_args.args[1].collections, [setup_recall.PUBLIC_CANARY_COLLECTION])

    def test_setup_canary_dry_run_does_not_write(self):
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(setup_recall, "HOME", Path(temporary)), \
             mock.patch.object(setup_recall, "add_collection", return_value="would add"):
            self.assertEqual(setup_recall.install_public_canary(dry_run=True), "would add")
            self.assertEqual(list(Path(temporary).iterdir()), [])

    def test_health_capability_receipt_is_offline_and_honest_when_absent(self):
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(health, "STATE_DIR", Path(temporary)), \
             mock.patch.object(recall, "server_capabilities") as network:
            self.assertFalse(health.recall_capabilities()["ok"])
            self.assertEqual(network.call_count, 0)

    def test_nightly_does_not_read_private_probes_without_opt_in(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Path(temporary)
            (fixture / "probes.jsonl").write_text("this would fail if read")
            with mock.patch.multiple(evals, EVAL_DIR=fixture, PROBES=fixture / "probes.jsonl",
                                     NIGHTLY=fixture / "nightly.json", NIGHTLY_OK=fixture / "nightly.ok"), \
                 mock.patch.object(recall, "_collection_dirs", return_value={}), \
                 mock.patch.object(recall, "_load_toml", return_value={}), \
                 mock.patch.object(recall.Settings, "load", return_value=recall.Settings()), \
                 mock.patch("sys.stdout", new=io.StringIO()):
                self.assertEqual(evals_cli.nightly(SimpleNamespace(heartbeat=None, json=True, private_probes=False)), 0)
            status = json.loads((fixture / "nightly.json").read_text())
            self.assertIn("policy_contracts", status)
            self.assertNotIn("private_probes", status)

    def test_model_calls_require_source_authorization(self):
        from claude_note import egress
        with mock.patch.object(egress, "policy", return_value={}), mock.patch.object(evals.subprocess, "run") as call:
            with self.assertRaises(egress.EgressDenied):
                evals.claude_json("private source", sources=["/synthetic/private.md"])
            self.assertEqual(call.call_count, 0)

    def test_explicit_nightly_agent_plan_reports_unavailable_all_attempts(self):
        outcome = {"measurement": "real_model_invocation_attempts_no_executable_outcomes", "t": "now",
                   "comparable": False, "fingerprint": {"fixture": "public"}, "model_calls": 0, "model_responses": 0,
                   "metrics": {"corrected_hybrid": {"scheduled": 1, "attempted": 1, "completed": 0, "success_rate": 0}},
                   "records": [{"error": "unavailable: synthetic model backend"}], "rows": []}
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Path(temporary)
            with mock.patch.multiple(evals, EVAL_DIR=fixture, PROBES=fixture/'missing.jsonl', NIGHTLY=fixture/'nightly.json',
                                     NIGHTLY_OK=fixture/'nightly.ok'), \
                 mock.patch.object(recall, '_load_toml', return_value={'eval': {'agent_tasks': {'enabled': True, 'model': 'fixed', 'budget_usd': .1}}}), \
                 mock.patch.object(recall, '_collection_dirs', return_value={}), \
                 mock.patch.object(recall.Settings, 'load', return_value=recall.Settings()), \
                 mock.patch.object(evals_cli, 'run_agent_tasks', return_value=outcome) as runner, \
                 mock.patch('sys.stdout', new=io.StringIO()):
                self.assertEqual(evals_cli.nightly(SimpleNamespace(heartbeat=None, json=True, private_probes=False)), 3)
            self.assertEqual(runner.call_count, 1)
            status = json.loads((fixture/'nightly.json').read_text())
            self.assertFalse(status['ok'])
            self.assertEqual(status['agent_tasks']['metrics']['corrected_hybrid']['scheduled'], 1)
            self.assertTrue((fixture/'agent-task-runs.jsonl').exists())


if __name__ == "__main__":
    unittest.main()
