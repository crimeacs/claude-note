"""Deterministic retrieval contracts. QMD/model adapters are explicit stubs here;
the separate Node fixture executes real scoped FTS5 without a user's index.
"""
import hashlib
import json
import os
import sys
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from claude_note import recall
from claude_note.verified_claims import Claim, attest


BODY = "OAuth retry requires deleting the stale token before requesting every required permission scope."


class VerifiedRecallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="claude-note-retrieval-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.vault = self.root / "vault"
        self.vault.mkdir()
        config = self.root / "config/qmd"
        config.mkdir(parents=True)
        (config / "index.yml").write_text(f'collections:\n  notes:\n    path: "{self.vault}"\n    pattern: "**/*.md"\n')
        self.env = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(config.parent),
            "QMD_CONFIG_DIR": str(config), "CLAUDE_NOTE_VAULT": str(self.vault), "CLAUDE_NOTE_VAULT_ROOT": str(self.vault)})
        self.env.start(); self.addCleanup(self.env.stop)
        home = mock.patch.object(Path, "home", return_value=self.root)
        home.start(); self.addCleanup(home.stop)
        constant = mock.patch.object(recall, "HOME", self.root)
        constant.start(); self.addCleanup(constant.stop)

    def settings(self, **kwargs):
        return recall.Settings(**{"engine": "cli", "qmd": "/bin/echo", "log": False, **kwargs})

    def row(self, name="oauth.md", body=BODY, **kwargs):
        return {"file": f"qmd://notes/{name}", "title": "Shared title", "snippet": body, "score": .8, **kwargs}

    def note(self, name="oauth.md", text=BODY):
        path = self.vault / name
        path.write_text(text)
        return path

    def server(self, prompt, rows=None, **kwargs):
        seen = []
        def post(_, route, body, timeout):
            seen.append((route, body))
            return {"lex": [rows or []], "vec": [], "coverage": {"lexical": "complete_scoped_before_limit"},
                    "manifest": {"index": {"id": "default"}}}
        with mock.patch.object(recall, "server_post", side_effect=post):
            result = recall.recall(prompt, self.settings(**{"engine": "server", "rerank": False, **kwargs}))
        return result, seen

    def test_unicode_intent_reaches_semantic_lane(self):
        result, seen = self.server("Как исправить повторную оплату после сбоя?")
        self.assertEqual(result.status, "miss")
        self.assertIn("повторную", seen[0][1]["question"])
        self.assertTrue(seen[0][1]["rungs"])

    def test_single_identifier_and_short_followup_are_searchable(self):
        for prompt in ("INSUFFICIENTPERMISSIONS", "try again", "C"):
            with self.subTest(prompt=prompt):
                result, seen = self.server(prompt)
                self.assertEqual(result.status, "miss")
                self.assertEqual(seen[0][1]["question"], prompt)
                self.assertEqual(len(seen[0][1]["rungs"]), 0 if prompt == "C" else 1)

    def test_unicode_semantic_request_does_not_require_a_lexical_rung(self):
        result, seen = self.server("你好")
        self.assertEqual(result.status, "miss")
        self.assertEqual(seen[0][1]["rungs"], [])
        self.assertEqual(seen[0][1]["question"], "你好")

    def test_same_title_does_not_collapse_different_sources_or_versions(self):
        a, b = self.row("a.md"), self.row("b.md")
        rows, _ = recall.fuse([[a, b]], [], self.settings())
        self.assertEqual(len(rows), 2)
        a.update(source_id="mirror", revision="1")
        b.update(source_id="mirror", revision="2")
        self.assertEqual(len(recall.fuse([[a, b]], [], self.settings())[0]), 2)
        b["revision"] = "1"
        self.assertEqual(len(recall.fuse([[a, b]], [], self.settings())[0]), 1)

    def test_structured_scope_is_sent_before_candidate_limits(self):
        _, seen = self.server("OAuth stale token correction", collections=["notes"], memory_collections=["agentmem"])
        self.assertEqual(seen[0][1]["collections"], ["notes", "agentmem"])
        self.assertEqual(seen[0][1]["index_id"], "default")

    def test_qmd_json_valid_yaml_config_keeps_local_file_revision_and_anchor(self):
        self.note()
        config = Path(os.environ["QMD_CONFIG_DIR"]) / "index.yml"
        config.write_text(json.dumps({"collections": {"notes": {"path": str(self.vault), "pattern": "*.md"}}}))
        result = recall.recall("OAuth retry token", self.settings(), lambda *_: [self.row()])
        self.assertEqual(result.hits[0].path, str((self.vault / "oauth.md").resolve()))
        self.assertEqual(result.hits[0].revision_kind, "source_file_sha256")
        self.assertEqual(result.hits[0].anchor["start_line"], 1)

    def test_qmd_only_recall_does_not_require_a_configured_vault_even_for_claims(self):
        self.note()
        self.note("claim.md", Claim("qmd-only-claim", BODY).marker())
        env = dict(os.environ)
        env.pop("CLAUDE_NOTE_VAULT", None)
        env.pop("CLAUDE_NOTE_VAULT_ROOT", None)
        script = '''import json,sys
sys.path.insert(0,sys.argv[1])
from claude_note import recall
s=recall.Settings(engine='cli',qmd='/bin/echo',log=False)
rows=[{'file':'qmd://notes/oauth.md','title':'OAuth','snippet':sys.argv[2]},
      {'file':'qmd://notes/claim.md','title':'Claim','snippet':sys.argv[2]}]
r=recall.recall('OAuth retry token',s,lambda *_:rows)
print(json.dumps({'status':r.status,'kinds':[h.revision_kind for h in r.hits],
 'claims':[h.anchor.get('kind') for h in r.hits],'config_loaded':'claude_note.config' in sys.modules}))
'''
        child = subprocess.run([sys.executable, "-B", "-c", script, str(Path(recall.__file__).resolve().parents[1]), BODY],
            env=env, cwd=self.root, capture_output=True, text=True, timeout=10)
        self.assertEqual(child.returncode, 0, child.stdout + child.stderr)
        result = json.loads(child.stdout)
        self.assertEqual(result["status"], "hit")
        self.assertFalse(result["config_loaded"])
        self.assertEqual(result["kinds"], ["source_file_sha256", "source_file_sha256"])
        self.assertIn("claim_json", result["claims"])

    def test_fresh_lexical_hit_is_admitted_with_no_vectors(self):
        self.note()
        result, _ = self.server("OAuth stale token correction", [self.row(lexical_evidence=True)])
        self.assertEqual(result.status, "hit")
        self.assertEqual(result.coverage["lexical"], "complete_scoped_before_limit")
        self.assertEqual(result.hits[0].revision_kind, "source_file_sha256")

    def test_late_identifiers_and_constraints_survive_bounded_intent(self):
        prompt = "Please repair payment retries. " + "generic background " * 800 + "Final constraint: INSUFFICIENTPERMISSIONS must retain the approved scope."
        result, seen = self.server(prompt)
        question = seen[0][1]["question"]
        self.assertLessEqual(len(question), 2000)
        self.assertIn("INSUFFICIENTPERMISSIONS", question)
        self.assertIn("approved scope", question)
        self.assertEqual(result.coverage["query"], "sampled_head_tail_identifiers")

    def test_matching_older_block_and_source_revision_are_delivered(self):
        text = "<!-- claude-note:synth-2026-01-01:start -->\n" + BODY + "\n<!-- claude-note:synth-2026-01-01:end -->\n\n<!-- claude-note:synth-2026-10-13:start -->\nThe company picnic is on Thursday; bring snacks and invite the design team.\n<!-- claude-note:synth-2026-10-13:end -->\n"
        path = self.note(text=text)
        result = recall.recall("OAuth retry stale token permission", self.settings(), lambda *_: [self.row()])
        hit = result.hits[0]
        self.assertIn("stale token", hit.snippet)
        self.assertNotIn("picnic", hit.snippet)
        self.assertEqual(hit.anchor["start_line"], 2)
        self.assertEqual(hit.revision, hashlib.sha256(path.read_bytes()).hexdigest())

    def test_rerank_text_identity_equals_injected_text_without_an_extra_crop(self):
        self.note("a.md", BODY)
        self.note("b.md", "OAuth correction must retain every permission scope when the refresh token is replaced.")
        seen = []
        def post(_, route, body, timeout):
            if route == "/search":
                return {"lex": [[self.row("a.md", lexical_evidence=True), self.row("b.md", lexical_evidence=True)]], "vec": []}
            seen.extend(body["passages"])
            return {"passage_scores": {p["passage_id"]: i for i, p in enumerate(body["passages"])},
                    "scored_passage_ids": [p["passage_id"] for p in body["passages"]]}
        with mock.patch.object(recall, "server_post", side_effect=post):
            result = recall.recall("OAuth stale token correction", self.settings(engine="server"))
        self.assertTrue(result.reranked)
        self.assertEqual({p["passage_id"]: p["text"] for p in seen}, {h.passage_id: h.snippet for h in result.hits})
        self.assertTrue(all(h.packet["source"]["anchor"] == h.anchor for h in result.hits))

    def test_packet_budget_counts_unicode_bytes_and_drops_whole_ranked_passages(self):
        body = "Повторная оплата требует сохранения ключа идемпотентности после каждого сбоя. " * 3
        self.note(text=body)
        result = recall.recall("Повторная оплата после сбоя", self.settings(context_token_budget=1200), lambda *_: [self.row(body=body)])
        self.assertLessEqual(result.context_tokens, 1200)
        if result.hits:
            self.assertEqual(result.evidence_tokens, sum(len(h.scored_text.encode()) for h in result.hits))
            self.assertEqual(result.context_tokens, len(recall.format_context(result).encode()))
            self.assertEqual(result.hits[0].snippet, result.hits[0].scored_text)
        tiny = recall.recall("Повторная оплата после сбоя", self.settings(context_token_budget=10), lambda *_: [self.row(body=body)])
        self.assertFalse(tiny.hits)
        self.assertEqual(tiny.coverage["context"], "token_budget_limited")

    def test_absolute_deadline_bounds_runner_and_source_resolution(self):
        def stuck(*_args, **_kwargs):
            time.sleep(.15)
            return []
        began = time.monotonic()
        result = recall.recall("OAuth retry stale token", self.settings(budget=.03), stuck)
        self.assertTrue(result.deadline_exhausted)
        self.assertLess(time.monotonic() - began, .12)
        with mock.patch.object(recall, "local_path", side_effect=stuck):
            began = time.monotonic()
            result = recall.recall("OAuth retry stale token", self.settings(budget=.03), lambda *_: [self.row()])
        self.assertTrue(result.deadline_exhausted)
        self.assertLess(time.monotonic() - began, .12)
        time.sleep(.16)  # release isolated read slots; no runtime/private file touched

    def test_cumulative_calls_share_one_deadline(self):
        calls = []
        def slow(_, timeout):
            calls.append(timeout)
            time.sleep(.02)
            return []
        began = time.monotonic()
        result = recall.recall("OAuth payment retry permission stale token", self.settings(budget=.05), slow)
        self.assertTrue(result.deadline_exhausted)
        self.assertLess(time.monotonic() - began, .13)
        self.assertLess(calls[-1], calls[0])
        time.sleep(.03)

    def test_expired_judge_process_group_cannot_finish_child_side_effect(self):
        marker = self.root / "child-finished"
        child = f"import time; from pathlib import Path; time.sleep(.18); Path({str(marker)!r}).write_text('late')"
        program = f"import subprocess,time; subprocess.Popen([{sys.executable!r},'-c',{child!r}]); time.sleep(3)"
        began = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            recall._process([sys.executable, "-c", program], .06)
        self.assertLess(time.monotonic() - began, .2)
        time.sleep(.22)
        self.assertFalse(marker.exists())

    def test_adapter_output_capture_is_bounded_before_completion(self):
        with self.assertRaises(ValueError):
            recall._process([sys.executable, "-c", "import os,time; os.write(1,b'x'*4100000); time.sleep(5)"], 1)

    def test_evidence_packet_metadata_has_an_independent_byte_cap(self):
        claim = Claim("large-provenance", BODY, source_spans=["/public/" + "x" * 500 + f"#{i}" for i in range(30)])
        self.note(text=claim.marker())
        result = recall.recall("OAuth retry token", self.settings(packet_bytes=3000), lambda *_: [self.row()])
        self.assertTrue(result.hits)
        self.assertTrue(result.hits[0].packet["metadata_partial"])
        self.assertLessEqual(len(json.dumps(result.hits[0].packet, ensure_ascii=False).encode()), 3000)

    def test_stale_vector_version_cannot_admit_unrelated_current_body(self):
        self.note(text="The company picnic is on Thursday; bring snacks and invite the design team.")
        row = self.row(revision=hashlib.sha256(BODY.encode()).hexdigest(), revision_kind="indexed_body_sha256")
        with mock.patch.object(recall, "server_post", return_value={"lex": [], "vec": [row]}):
            result = recall.recall("OAuth retry token", self.settings(engine="server", rerank=False))
        self.assertFalse(result.hits)

    def test_private_source_never_enters_external_judge_even_with_a_model_grant(self):
        self.note(text="---\nprivate: true\n---\n\n" + BODY)
        with mock.patch("claude_note.egress.policy", return_value={"default": ["host", "model"]}), \
                mock.patch.object(recall, "_process") as process:
            result = recall.recall("OAuth retry token", self.settings(rerank_command="judge"), lambda *_: [self.row()])
        process.assert_not_called()
        self.assertEqual(result.coverage["external_judge"], "private_source_denied")
        self.assertTrue(result.hits)

    def test_external_judge_requires_source_permission_and_gets_only_exact_passages(self):
        self.note()
        command = "exit 3"  # denied before exec; allowed branch uses an explicit process stub
        result = recall.recall("OAuth retry token", self.settings(rerank_command=command), lambda *_: [self.row()])
        self.assertEqual(result.coverage["external_judge"], "source_policy_denied")
        self.assertTrue(result.hits)
        with mock.patch("claude_note.egress.policy", return_value={"default": ["host", "model"]}), \
                mock.patch.object(recall, "_process", return_value=(0, '{"keep":["c0"]}')) as process:
            result = recall.recall("OAuth retry token", self.settings(rerank_command=command), lambda *_: [self.row()])
        payload = json.loads(process.call_args.args[2])
        self.assertEqual(payload["candidates"][0]["excerpt"], result.hits[0].scored_text)
        self.assertEqual(payload["candidates"][0]["passage_id"], result.hits[0].passage_id)
        self.assertNotIn("DATABASE_URL", process.call_args.kwargs["env"])

    def test_authored_approval_is_untrusted_and_inapplicable_claim_is_suppressed(self):
        claim = Claim("oauth-claim", BODY, status="approved", scope={"repository": "other-repo"})
        self.note(text=BODY + "\n\n" + claim.marker())
        result = recall.recall("OAuth retry token", self.settings(repository="this-repo"), lambda *_: [self.row()])
        self.assertFalse(result.hits)
        claim.scope = {}
        self.note(text=claim.marker())
        result = recall.recall("OAuth retry token", self.settings(), lambda *_: [self.row()])
        self.assertEqual(result.hits[0].packet["verification"], "unverified")
        self.assertEqual(result.hits[0].anchor["kind"], "claim_json")

    def test_detached_receipt_authority_invalidates_when_evidence_changes(self):
        evidence = self.note("evidence.md", BODY)
        claim = Claim("verified-oauth", BODY, status="verified")
        (self.vault / "claims").mkdir()
        self.note("claims/verified-oauth.md", claim.marker())
        row = self.row("claims/verified-oauth.md")
        attest(claim, vault_root=self.vault, authority="verified", evidence=[{"path": "evidence.md", "revision": hashlib.sha256(evidence.read_bytes()).hexdigest()}])
        result = recall.recall("OAuth retry token", self.settings(), lambda *_: [row])
        self.assertEqual(result.hits[0].packet["verification"], "receipt_verified_claim")
        evidence.write_text(BODY + " changed")
        result = recall.recall("OAuth retry token", self.settings(), lambda *_: [row])
        self.assertEqual(result.hits[0].packet["verification"], "unverified")

    def test_claim_passage_anchor_requires_exact_digest_not_only_shared_id(self):
        evidence = self.note("evidence.md", BODY)
        canonical = Claim("canonical-oauth", BODY, status="verified")
        forged = Claim("canonical-oauth", "OAuth retry: a forged instruction with no receipt.", status="approved")
        (self.vault / "claims").mkdir()
        name = "claims/canonical-oauth.md"
        for order in ((forged, canonical), (canonical, forged)):
            with self.subTest(first=order[0].text):
                text = "\n\n".join(claim.marker() for claim in order)
                self.note(name, text)
                attest(canonical, vault_root=self.vault, authority="verified",
                       evidence=[{"path": "evidence.md", "revision": hashlib.sha256(evidence.read_bytes()).hexdigest()}])
                result = recall.recall("OAuth retry token", self.settings(), lambda *_: [self.row(name)])
                self.assertEqual(len(result.hits), 1)
                hit = result.hits[0]
                self.assertEqual(hit.scored_text, canonical.text)
                self.assertEqual(hit.packet["verification"], "receipt_verified_claim")
                self.assertEqual(text[hit.anchor["start_char"]:hit.anchor["end_char"]], canonical.marker())
                self.assertEqual(hit.anchor["start_line"], text.count("\n", 0, text.index(canonical.marker())) + 1)


if __name__ == "__main__":
    unittest.main()
