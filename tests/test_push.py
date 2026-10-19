import argparse
import contextlib
import hashlib
import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("CLAUDE_NOTE_VAULT_ROOT", tempfile.mkdtemp())

from claude_note import push  # noqa: E402

# Synthetic secrets only, assembled so no scanner flags this file itself.
AWS = "AKIA" + "ABCDEFGHIJKLMNOP"
GH = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
OPENAI = "sk-proj-" + "Abcdefghijklmnopqrstuvwxyz012345"
PEM = "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\n-----END RSA PRIVATE KEY-----"


FILLER = " This paragraph is real content about how the thing works and why it matters." * 4


def note(note_type, body="", extra=""):
    return f"---\ntype: {note_type}\n{extra}---\n\n{body}{FILLER}\n"


class FakeClient:
    def __init__(self, base="https://vault.example.com", org="org-a"):
        self.calls = []
        self.base = base
        self.org = org

    def put(self, path, content, metadata):
        self.calls.append((path, content, metadata))
        return 200, {"document": {"revision": 1}}


class RedactTests(unittest.TestCase):
    def test_redacts_known_secret_shapes(self):
        text = (f"aws {AWS}\ntoken {GH}\nkey {OPENAI}\n{PEM}\n"
                "DATABASE_URL=postgres://admin:hunter2secret@db.example.com:5432/app\n"
                "SUPABASE_SERVICE_KEY=abcdefgh12345678\n"
                "Authorization: Bearer abcdefghijklmnopqrstuvwxyz123456\n")
        out, n = push.redact(text)
        for secret in (AWS, GH, OPENAI, "MIIEowIBAAKCAQEA", "hunter2secret", "abcdefgh12345678", "abcdefghijklmnopqrstuvwxyz123456"):
            self.assertNotIn(secret, out)
        self.assertIn("[REDACTED: AWS key]", out)
        self.assertIn("db.example.com", out)
        self.assertGreaterEqual(n, 7)

    def test_leaves_ordinary_prose_and_placeholders(self):
        text = "Mask keys as sk-*** and set API_KEY=${API_KEY}; password: <your password>. The token budget is 4000."
        self.assertEqual(push.redact(text), (text, 0))


class PushTests(unittest.TestCase):
    def setUp(self):
        policy = mock.patch.object(push.egress, "policy", return_value={"default": ["local", "host", "staging"]})
        policy.start()
        self.addCleanup(policy.stop)
        self.vault = Path(tempfile.mkdtemp())
        tmp = Path(tempfile.mkdtemp())
        push.STATE_FILE = tmp / "state.json"
        push.HEARTBEAT = tmp / "push.ok"
        files = {
            "pattern-a.md": note("pattern", f"use {AWS}"),
            "topics/gotcha-b.md": note("gotcha", "plain"),
            "sessions/claude-session-x.md": note("session", "raw"),
            "log-c.md": note("log", "x"),
            "decision-d.md": note("decision", "x", "share: false\n"),
            "private/ref-e.md": note("reference", "x"),
            ".claude-note/ref-f.md": note("reference", "x"),
            "untyped.md": "no frontmatter",
        }
        for rel, text in files.items():
            (self.vault / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.vault / rel).write_text(text)

    def test_only_eligible_notes_are_pushed_redacted_once(self):
        client = FakeClient()
        counts = push.run(vault=self.vault, client=client)
        self.assertEqual((counts["pushed"], counts["redacted"], counts["unchanged"]), (2, 1, 0))
        paths = sorted(c[0] for c in client.calls)
        self.assertTrue(paths[0].startswith("inbox/"))
        self.assertTrue(any(path.endswith("/pattern-a.md") for path in paths))
        self.assertRegex(next(path for path in paths if "/_nested/" in path),
                         r"/_nested/topics__gotcha-b--[0-9a-f]{16}\.md$")
        sent = {c[2]["source_path"]: c for c in client.calls}
        self.assertNotIn(AWS, sent["pattern-a.md"][1])
        meta = sent["pattern-a.md"][2]
        self.assertEqual(set(meta), {"source", "author", "assistant", "laptop", "source_path", "content_sha256", "note_type", "redactions", "operation_id"})
        self.assertEqual((meta["source"], meta["note_type"], meta["redactions"]), ("claude-note", "pattern", 1))

        # Second run: nothing changed, so no request at all.
        again = FakeClient()
        counts = push.run(vault=self.vault, client=again)
        self.assertEqual((counts["pushed"], counts["unchanged"], len(again.calls)), (0, 2, 0))
        self.assertTrue(push.HEARTBEAT.exists())

        # An edit is pushed again.
        (self.vault / "topics/gotcha-b.md").write_text(note("gotcha", "edited"))
        self.assertEqual(push.run(vault=self.vault, client=FakeClient())["pushed"], 1)

    def test_receipts_are_specific_to_organization_and_endpoint(self):
        self.assertEqual(push.run(vault=self.vault, client=FakeClient())["pushed"], 2)
        # Cosmetic endpoint spelling does not create another destination.
        same = FakeClient(base="https://VAULT.EXAMPLE.COM/")
        self.assertEqual(push.run(vault=self.vault, client=same)["unchanged"], 2)
        self.assertEqual(same.calls, [])
        other_org = FakeClient(org="org-b")
        self.assertEqual(push.run(vault=self.vault, client=other_org)["pushed"], 2)
        other_host = FakeClient(base="https://another.example.com", org="org-b")
        self.assertEqual(push.run(vault=self.vault, client=other_host)["pushed"], 2)
        self.assertEqual(push.run(vault=self.vault, client=FakeClient())["unchanged"], 2)
        state = json.loads(push.STATE_FILE.read_text())
        self.assertEqual(state["version"], 2)
        self.assertEqual(len(state["destinations"]), 3)

    def test_receipts_do_not_cross_local_vaults_or_owners(self):
        from unittest import mock
        with mock.patch.object(push, "identity", return_value=("owner-a", "a@example.com")):
            push.run(vault=self.vault, client=FakeClient())
        with mock.patch.object(push, "identity", return_value=("owner-b", "b@example.com")):
            self.assertEqual(push.run(vault=self.vault, client=FakeClient())["pushed"], 2)
        other_vault = Path(tempfile.mkdtemp())
        (other_vault / "pattern-a.md").write_text((self.vault / "pattern-a.md").read_text())
        with mock.patch.object(push, "identity", return_value=("owner-a", "a@example.com")):
            self.assertEqual(push.run(vault=other_vault, client=FakeClient())["pushed"], 1)

    def test_legacy_receipts_stay_unbound_until_explicit_resend(self):
        import contextlib
        import io
        legacy = {rel: hashlib.sha256(push.redact((self.vault / rel).read_text())[0].encode()).hexdigest()
                  for rel in ("pattern-a.md", "topics/gotcha-b.md")}
        push.STATE_FILE.write_text(json.dumps(legacy))
        err = io.StringIO()
        client = FakeClient()
        with contextlib.redirect_stderr(err):
            counts = push.run(vault=self.vault, client=client)
        self.assertEqual((counts["pushed"], counts["unchanged"], counts["legacy_deferred"]), (0, 0, 2))
        self.assertEqual(client.calls, [])
        self.assertIn("--resend-legacy", err.getvalue())
        self.assertFalse(push.HEARTBEAT.exists())
        state = json.loads(push.STATE_FILE.read_text())
        self.assertEqual(state["legacy"], legacy)
        self.assertTrue(all(not record["notes"] for record in state["destinations"].values()))

        self.assertEqual(push.run(vault=self.vault, client=FakeClient(), resend_legacy=True)["pushed"], 2)
        self.assertEqual(push.run(vault=self.vault, client=FakeClient())["unchanged"], 2)
        self.assertTrue(push.HEARTBEAT.exists())
        # Once there is known history, a deliberately changed target receives it.
        self.assertEqual(push.run(vault=self.vault, client=FakeClient(org="org-b"))["pushed"], 2)

    def test_edit_of_legacy_note_is_sent_as_new_content(self):
        original = (self.vault / "pattern-a.md").read_text()
        old_sha = hashlib.sha256(push.redact(original)[0].encode()).hexdigest()
        push.STATE_FILE.write_text(json.dumps({"pattern-a.md": old_sha}))
        (self.vault / "pattern-a.md").write_text(original + "\nA newly verified correction.\n")
        counts = push.run(vault=self.vault, client=FakeClient())
        self.assertEqual((counts["pushed"], counts["legacy_deferred"]), (2, 0))

    def test_legacy_dry_run_never_rebinds_state_or_contacts_destination(self):
        original = (self.vault / "pattern-a.md").read_text()
        old_sha = hashlib.sha256(push.redact(original)[0].encode()).hexdigest()
        before = json.dumps({"pattern-a.md": old_sha})
        push.STATE_FILE.write_text(before)
        client = FakeClient()
        counts = push.run(vault=self.vault, client=client, dry_run=True, resend_legacy=True)
        self.assertEqual(counts["pushed"], 2)
        self.assertEqual(client.calls, [])
        self.assertEqual(push.STATE_FILE.read_text(), before)
        self.assertFalse(push.HEARTBEAT.exists())

    def test_remote_paths_are_unique_without_changing_root_filenames(self):
        prefix = "inbox/person/2026-01-01"
        root = push.remote_path("person", "2026-01-01", Path("a__b.md"))
        nested = push.remote_path("person", "2026-01-01", Path("a/b.md"))
        self.assertEqual(root, f"{prefix}/a__b.md")
        self.assertNotEqual(root, nested)
        self.assertEqual(nested, push.remote_path("person", "2026-01-01", Path("a/b.md")))
        self.assertNotEqual(push.remote_path("person", "2026-01-01", Path("a/b__c.md")),
                            push.remote_path("person", "2026-01-01", Path("a__b/c.md")))

    def test_privacy_controls_match_in_dry_run_and_upload_selection(self):
        # Remove the ordinary eligible fixture notes, then exercise only the
        # privacy cases. A dry run and a real run must nominate the same files.
        (self.vault / "pattern-a.md").unlink()
        (self.vault / "topics/gotcha-b.md").unlink()
        fixtures = {
            "blocked-comment.md": "share: false # keep this private\n",
            "blocked-quoted.md": 'share: "false" # local only\n',
            "blocked-malformed.md": "share: fasle\n",
            "allowed.md": "share: true # consciously share\n",
        }
        for name, control in fixtures.items():
            (self.vault / name).write_text(note("pattern", "A reusable finding.", control))
        (self.vault / "blocked-bom.md").write_text("\ufeff" + note("pattern", extra="share: false\n"))
        (self.vault / "blocked-unterminated.md").write_text(
            "---\ntype: pattern\nshare: false\n" + FILLER)
        (self.vault / "allowed-bom.md").write_text("\ufeff" + note("pattern", extra="share: true\n"))
        dry_client = FakeClient()
        dry = push.run(vault=self.vault, client=dry_client, dry_run=True)
        self.assertEqual((dry["pushed"], dry_client.calls), (2, []))
        client = FakeClient()
        actual = push.run(vault=self.vault, client=client)
        self.assertEqual(actual["pushed"], dry["pushed"])
        self.assertEqual(sorted(metadata["source_path"] for _, _, metadata in client.calls),
                         ["allowed-bom.md", "allowed.md"])

    def test_stub_notes_are_skipped(self):
        stubs = {
            "short.md": "---\ntype: pattern\n---\n\nOne line.\n",
            "headings.md": "---\ntype: gotcha\n---\n\n# Title\n\n## Context\n\n## Fix\n\n## Related\n" + "#" * 0,
            "placeholder.md": "---\ntype: decision\n---\n\n# Decision\n\nThis was never written up; see the session log for details of what happened here.\n" * 2,
        }
        for rel, text in stubs.items():
            (self.vault / rel).write_text(text)
        client = FakeClient()
        counts = push.run(vault=self.vault, client=client)
        self.assertEqual((counts["skipped_stub"], counts["pushed"]), (3, 2))

    def test_run_with_nothing_to_do_touches_heartbeat(self):
        push.run(vault=self.vault, client=FakeClient())
        push.HEARTBEAT.unlink()
        counts = push.run(vault=self.vault, client=FakeClient())
        self.assertEqual(counts["pushed"], 0)
        self.assertTrue(push.HEARTBEAT.exists())

    def test_scanner_error_skips_note(self):
        original = push.redact
        push.redact = lambda text: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            client = FakeClient()
            counts = push.run(vault=self.vault, client=client)
        finally:
            push.redact = original
        self.assertEqual((counts["skipped"], len(client.calls)), (2, 0))
        self.assertFalse(counts["clean"])
        self.assertEqual(counts["skipped_reasons"], {"inspection: RuntimeError": 2})
        self.assertFalse(push.HEARTBEAT.exists())

    def test_revoked_root_scope_records_attempt_without_refreshing_clean_heartbeat(self):
        push.run(vault=self.vault, client=FakeClient())
        clean_before = push.HEARTBEAT.read_text()
        with mock.patch.object(push.egress, "policy", return_value={}):
            counts = push.run(vault=self.vault, client=FakeClient())
        self.assertEqual((counts["errors"], counts["pushed"], counts["policy_denied"], counts["local_only"]), (0, 0, 0, 2))
        self.assertEqual(counts["policy_denied_reasons"], {})
        self.assertEqual(counts["revocation_reasons"], {"audience-revoked": 2})
        self.assertFalse(counts["clean"])
        self.assertEqual(push.HEARTBEAT.read_text(), clean_before)
        attempt = json.loads(push.HEARTBEAT.with_suffix(".json").read_text())
        self.assertEqual(attempt["status"], "blocked")
        self.assertEqual(attempt["counts"], counts)
        self.assertTrue(attempt["started_at"])

    def test_generated_note_missing_sources_stays_local_and_reports_cause(self):
        generated = note("gotcha", extra="assistant: codex\n")
        (self.vault / "generated.md").write_text(generated)
        client = FakeClient()
        counts = push.run(vault=self.vault, client=client)
        self.assertEqual(counts["policy_denied_reasons"], {"Generated note has no bound derivation sources": 1})
        self.assertFalse(push.HEARTBEAT.exists())
        self.assertNotIn("generated.md", [metadata["source_path"] for _, _, metadata in client.calls])
        self.assertEqual((self.vault / "generated.md").read_text(), generated)

    def test_unchanged_reauthorized_source_clears_only_its_exact_revocations(self):
        client = FakeClient()
        push.run(vault=self.vault, client=client)
        with mock.patch.object(push.egress, "policy", return_value={}):
            self.assertEqual(push.run(vault=self.vault, client=client)["revocations_pending"], 2)
        state = json.loads(push.STATE_FILE.read_text())
        destination = next(iter(state["destinations"].values()))["identity"]
        retained = [
            {"source": "pattern-a.md", "destination": dict(destination, organizationId="other"), "reason": "audience-revoked"},
            {"source": "pattern-a.md", "destination": dict(destination, vault="/other/vault"), "reason": "audience-revoked"},
            {"source": "pattern-a.md", "destination": destination, "reason": "unknown"},
            {"source": "missing.md", "destination": destination, "reason": "eligibility-revoked"},
        ]
        state["revocations"].extend(retained)
        push.STATE_FILE.write_text(json.dumps(state))
        again = FakeClient()
        restored = push.run(vault=self.vault, client=again)
        self.assertEqual((restored["unchanged"], restored["revocations_cleared"], restored["revocations_pending"]), (2, 2, 4))
        self.assertEqual(again.calls, [])
        self.assertEqual(json.loads(push.STATE_FILE.read_text())["revocations"], retained)
        self.assertEqual(restored["revocation_reasons"], {"audience-revoked": 2, "unknown": 1, "eligibility-revoked": 1})

    def test_clean_heartbeat_recovers_after_exact_policy_reauthorization(self):
        push.run(vault=self.vault, client=FakeClient())
        with mock.patch.object(push.egress, "policy", return_value={}):
            push.run(vault=self.vault, client=FakeClient())
        push.HEARTBEAT.unlink()
        again = FakeClient()
        restored = push.run(vault=self.vault, client=again)
        self.assertEqual((restored["revocations_cleared"], restored["revocations_pending"]), (2, 0))
        self.assertEqual(again.calls, [])
        self.assertTrue(restored["clean"])
        self.assertTrue(push.HEARTBEAT.exists())

    def test_changed_bytes_do_not_cancel_revocation_of_the_prior_remote_bytes(self):
        push.run(vault=self.vault, client=FakeClient())
        with mock.patch.object(push.egress, "policy", return_value={}):
            push.run(vault=self.vault, client=FakeClient())
        path = self.vault / "pattern-a.md"
        path.write_text(path.read_text() + "\nA newly authorized revision.\n")
        restored = push.run(vault=self.vault, client=FakeClient())
        self.assertEqual((restored["pushed"], restored["revocations_cleared"], restored["revocations_pending"]), (1, 1, 1))
        self.assertEqual(json.loads(push.STATE_FILE.read_text())["revocations"][0]["source"], "pattern-a.md")
        # The new receipt must not erase the withdrawal of older remote bytes on
        # tomorrow's otherwise unchanged run.
        again = push.run(vault=self.vault, client=FakeClient())
        self.assertEqual((again["revocations_cleared"], again["revocations_pending"]), (0, 1))

    def test_restored_source_still_needs_transitive_authored_permission(self):
        origin = self.vault / "origin.txt"
        origin.write_text("Original synthetic source evidence.")
        generated = note("pattern", extra="assistant: codex\nderivation_sources: " + json.dumps([str(origin)]) + "\n")
        (self.vault / "generated.md").write_text(generated)
        self.assertEqual(push.run(vault=self.vault, client=FakeClient())["pushed"], 3)
        with mock.patch.object(push.egress, "policy", return_value={}):
            push.run(vault=self.vault, client=FakeClient())
        origin.write_text("---\nprivate: true\n---\nOriginal synthetic source evidence.")
        restored = push.run(vault=self.vault, client=FakeClient())
        self.assertEqual((restored["revocations_cleared"], restored["revocations_pending"], restored["policy_denied"]), (2, 1, 1))
        self.assertEqual(restored["policy_denied_reasons"], {"Artifact audience does not permit this destination": 1})
        self.assertEqual(json.loads(push.STATE_FILE.read_text())["revocations"][0]["source"], "generated.md")

    def test_unknown_recorded_lineage_remains_blocked_after_policy_restore(self):
        origin = self.vault / "origin.txt"
        origin.write_text("Synthetic origin evidence.")
        path = self.vault / "generated.md"
        path.write_text(note("pattern", extra="assistant: codex\nderivation_sources: " + json.dumps([str(origin)]) + "\n"))
        push.run(vault=self.vault, client=FakeClient())
        with mock.patch.object(push.egress, "policy", return_value={}):
            push.run(vault=self.vault, client=FakeClient())
        from claude_note.state_io import atomic_json
        ledger = push.egress._lineage_path(path, self.vault)
        atomic_json(ledger, {"version": 1, "origins": [str(origin)], "unknown": True})
        restored = push.run(vault=self.vault, client=FakeClient())
        self.assertEqual((restored["revocations_cleared"], restored["revocations_pending"]), (2, 1))
        self.assertEqual(restored["policy_denied_reasons"], {"Source lineage needs owner reconciliation": 1})
        self.assertTrue(json.loads(ledger.read_text())["unknown"])

    def test_restored_exact_eligibility_clears_queue_without_reupload(self):
        path = self.vault / "pattern-a.md"
        original = path.read_text()
        push.run(vault=self.vault, client=FakeClient())
        path.write_text(original.replace("type: pattern", "type: pattern\nshare: false"))
        self.assertEqual(push.run(vault=self.vault, client=FakeClient())["revocation_reasons"], {"eligibility-revoked": 1})
        path.write_text(original)
        client = FakeClient()
        restored = push.run(vault=self.vault, client=client)
        self.assertEqual((restored["revocations_cleared"], restored["revocations_pending"]), (1, 0))
        self.assertEqual(client.calls, [])

    def test_dry_run_never_clears_queued_revocations_or_writes_attempt_receipt(self):
        push.run(vault=self.vault, client=FakeClient())
        with mock.patch.object(push.egress, "policy", return_value={}):
            push.run(vault=self.vault, client=FakeClient())
        before_state = push.STATE_FILE.read_text()
        before_attempt = push.HEARTBEAT.with_suffix(".json").read_text()
        counts = push.run(vault=self.vault, client=FakeClient(), dry_run=True)
        self.assertEqual((counts["revocations_cleared"], counts["revocations_pending"]), (0, 2))
        self.assertEqual(push.STATE_FILE.read_text(), before_state)
        self.assertEqual(push.HEARTBEAT.with_suffix(".json").read_text(), before_attempt)

    def test_ungranted_roots_are_local_without_failed_sync_or_source_invention(self):
        generated = note("pattern", extra="assistant: codex\n")
        path = self.vault / "generated.md"
        path.write_text(generated)
        with mock.patch.object(push.egress, "policy", return_value={}):
            client = FakeClient()
            counts = push.run(vault=self.vault, client=client)
        self.assertEqual((counts["local_only"], counts["policy_denied"], counts["pushed"]), (3, 0, 0))
        self.assertTrue(counts["clean"])
        self.assertEqual(counts["problems"], [])
        self.assertTrue(push.HEARTBEAT.exists())
        self.assertEqual(client.calls, [])
        self.assertEqual(path.read_text(), generated)

    def test_explicit_root_grant_with_ungranted_origin_is_blocked(self):
        origin = self.vault / "origin.txt"
        origin.write_text("Synthetic source evidence stays local.")
        path = self.vault / "generated.md"
        path.write_text(note("pattern", extra="assistant: codex\nderivation_sources: " + json.dumps([str(origin)]) + "\n"))
        policy = {"rules": [{"source": str(path), "audiences": ["host", "local", "staging"]}]}
        with mock.patch.object(push.egress, "policy", return_value=policy):
            client = FakeClient()
            counts = push.run(vault=self.vault, client=client)
        self.assertEqual((counts["local_only"], counts["policy_denied"], counts["pushed"]), (2, 1, 0))
        self.assertEqual(counts["policy_denied_reasons"], {"Source policy does not allow staging egress": 1})
        self.assertFalse(counts["clean"])
        self.assertEqual(client.calls, [])
        self.assertFalse(push.HEARTBEAT.exists())

    def test_cli_exits_nonzero_and_names_a_bound_source_policy_blockage(self):
        from claude_note import cli
        (self.vault / "generated.md").write_text(note("pattern", extra="assistant: codex\n"))
        counts = push.run(vault=self.vault, client=FakeClient())
        output = io.StringIO()
        args = argparse.Namespace(install_agent=False, dry_run=False, limit=0, resend_legacy=False)
        with mock.patch.object(push, "run", return_value=counts), contextlib.redirect_stdout(output):
            self.assertEqual(cli.cmd_push(args), 1)
        self.assertIn("policy denied: 1", output.getvalue())
        self.assertIn("revocations pending: 0", output.getvalue())
        self.assertIn("Generated note has no bound derivation sources", output.getvalue())

    def test_auth_failure_stops_the_run(self):
        class Denied(FakeClient):
            def put(self, path, content, metadata):
                self.calls.append(path)
                return 403, {"error": "forbidden"}
        client = Denied()
        counts = push.run(vault=self.vault, client=client)
        self.assertEqual(len(client.calls), 1)
        self.assertIn("403", counts["stopped"])
        self.assertFalse(push.HEARTBEAT.exists())

    def test_lost_response_is_retried_once_and_counts_as_pushed(self):
        push.RETRY_DELAY = 0

        class Flaky(FakeClient):
            def put(self, path, content, metadata):
                self.calls.append(path)
                if self.calls.count(path) == 1:
                    raise TimeoutError("read timed out")  # server wrote it, reply lost
                return 200, {"document": {"revision": 1}, "noop": True}
        client = Flaky()
        counts = push.run(vault=self.vault, client=client)
        self.assertEqual((counts["pushed"], counts["errors"], len(client.calls)), (2, 0, 4))
        self.assertTrue(push.HEARTBEAT.exists())

    def test_persistent_server_error_is_counted_and_logged(self):
        push.RETRY_DELAY = 0

        class Broken(FakeClient):
            def put(self, path, content, metadata):
                self.calls.append(path)
                return 502, {"error": "bad gateway"}
        import contextlib
        import io
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            counts = push.run(vault=self.vault, client=Broken())
        self.assertEqual((counts["errors"], counts["pushed"]), (2, 0))
        self.assertIn("error: pattern-a.md: HTTP 502: bad gateway", err.getvalue())
        self.assertFalse(push.HEARTBEAT.exists())


if __name__ == "__main__":
    unittest.main()


class InferTypeTests(unittest.TestCase):
    """Synthesized notes had no `type` before 1.6.0, so the push shared none of them."""

    def synth(self, tags, extra="assistant: claude-code\n"):
        tag_block = "".join(f"  - {t}\n" for t in tags)
        return f"---\ntags:\n{tag_block}{extra}---\n\n# Title\n{FILLER}\n"

    def test_untyped_synthesized_note_gets_a_type_from_its_tags(self):
        self.assertEqual(push.eligible(Path("a.md"), self.synth(["automation", "gotcha", "pattern"])), "gotcha")
        self.assertEqual(push.eligible(Path("a.md"), self.synth(["research", "sweat-ai"])), "reference")
        self.assertEqual(push.eligible(Path("a.md"), "---\ntags: [claude-note, decision]\n---\nx"), "decision")

    def test_logs_inbox_sessions_and_unshared_stay_home(self):
        self.assertIsNone(push.eligible(Path("a.md"), self.synth(["log", "claude-note"])))
        self.assertIsNone(push.eligible(Path("claude-note-inbox.md"), self.synth(["log", "claude-note", "inbox"])))
        self.assertIsNone(push.eligible(Path("claude-session-2026-10-05-ab.md"), self.synth(["pattern"])))
        self.assertIsNone(push.eligible(Path("a.md"), self.synth(["pattern"], "assistant: codex\nshare: false\n")))
        self.assertIsNone(push.eligible(Path("private/a.md"), self.synth(["pattern"])))

    def test_a_persons_untyped_note_is_not_inferred(self):
        self.assertIsNone(push.eligible(Path("a.md"), "---\ntags:\n  - pattern\n---\nmine"))
        self.assertIsNone(push.eligible(Path("a.md"), "no frontmatter"))

    def test_explicit_type_still_wins(self):
        self.assertIsNone(push.eligible(Path("a.md"), self.synth(["pattern"], "assistant: codex\ntype: log\n")))
        self.assertEqual(push.eligible(Path("a.md"), self.synth(["pattern"], "assistant: codex\ntype: project\n")), "project")

    def test_false_share_scalars_with_quotes_and_yaml_comments_stay_local(self):
        for control in ("false", "FALSE # privacy", '"false" # privacy', "'false' # privacy",
                        "'FALSE'", '"false # not a boolean"'):
            with self.subTest(control=control):
                self.assertIsNone(push.eligible(Path("a.md"), note("pattern", extra=f"share: {control}\n")))
        self.assertIsNone(push.eligible(Path("a.md"), note("pattern", extra='"share": false # private\n')))
        self.assertIsNone(push.eligible(Path("a.md"), note("pattern", extra="'share' : 'false' # private\n")))

    def test_malformed_or_unsupported_share_values_fail_closed(self):
        for control in ("", "fasle", "no", "yes", "0", "[]", "{}", "null", '"false',
                        "true#not-a-yaml-comment", "true unexpected", "'true' unexpected", "|"):
            with self.subTest(control=control):
                self.assertIsNone(push.eligible(Path("a.md"), note("pattern", extra=f"share: {control}\n")))
        for controls in ("share: false\nshare: true\n", "share: true\nshare: true\n"):
            self.assertIsNone(push.eligible(Path("a.md"), note("pattern", extra=controls)))

    def test_bom_notes_honor_privacy_type_and_session_tags(self):
        for newline in ("\n", "\r\n"):
            for control, expected in (("false # private", None), ("true", "pattern")):
                with self.subTest(newline=newline, control=control):
                    text = "\ufeff" + note("pattern", extra=f"share: {control}\n").replace("\n", newline)
                    self.assertEqual(push.eligible(Path("a.md"), text), expected)
                    self.assertEqual(push.frontmatter(text)["type"], "pattern")
        self.assertEqual(push.eligible(Path("a.md"), "\ufeff" + note("pattern")), "pattern")
        self.assertIsNone(push.eligible(Path("a.md"), "\ufeff" + self.synth(["log", "claude-note"])))

    def test_unterminated_frontmatter_explicitly_fails_closed(self):
        for prefix in ("", "\ufeff"):
            for control in ("share: false\n", "share: true\n", ""):
                for suffix in ("", "---truncated\n"):
                    with self.subTest(prefix=prefix, control=control, suffix=suffix):
                        text = prefix + "---\ntype: pattern\n" + control + suffix + FILLER
                        self.assertFalse(push._share_allowed(text))
                        self.assertIsNone(push.eligible(Path("a.md"), text))
            self.assertFalse(push._share_allowed(prefix + "---"))

    def test_explicit_true_and_absent_share_keep_existing_eligibility(self):
        for control in ("true", "TRUE # approved", '"true" # approved', "'true' # approved"):
            with self.subTest(control=control):
                self.assertEqual(push.eligible(Path("a.md"), note("pattern", extra=f"share: {control}\n")), "pattern")
        self.assertEqual(push.eligible(Path("a.md"), note("pattern")), "pattern")


class WithTypeTests(unittest.TestCase):
    def test_create_frontmatter_always_has_a_type(self):
        from claude_note.note_router import with_type
        self.assertEqual(with_type({"tags": ["x", "decision"]})["type"], "decision")
        self.assertEqual(with_type({"tags": ["x"]})["type"], "reference")
        self.assertEqual(with_type({"type": "Gotcha", "tags": ["pattern"]})["type"], "gotcha")
        self.assertEqual(with_type({"type": "concept", "tags": ["pattern"]})["type"], "pattern")


class NoteCountTests(unittest.TestCase):
    def test_status_counts_sessions_knowledge_and_shareable(self):
        from claude_note import health
        vault = Path(tempfile.mkdtemp())
        (vault / "claude-session-2026-10-05-ab.md").write_text("---\ntags:\n  - log\n---\nx")
        (vault / "synth.md").write_text("---\ntags:\n  - pattern\nassistant: claude-code\n---\nx")
        (vault / "mine.md").write_text("no frontmatter")
        (vault / ".claude-note").mkdir()
        (vault / ".claude-note/q.md").write_text("x")
        health.PUSH_STATE = vault / "none.json"
        got = health._notes(vault)
        self.assertEqual((got["sessions"], got["knowledge"], got["shareable"], got["pushed"]), (1, 2, 1, 0))

    def test_synthesis_failures_are_read_from_the_worker_log(self):
        from claude_note import health
        vault = Path(tempfile.mkdtemp())
        (vault / ".claude-note/logs").mkdir(parents=True)
        (vault / ".claude-note/logs/worker-2026-10-05.log").write_text(
            "2026-10-05 [ERROR] Synthesis failed for session ab: Claude CLI not found. Is it installed?\n")
        got = health._synthesis(vault)
        self.assertEqual(got["failed"], 1)
        self.assertIn("Claude CLI not found", got["last_error"])


class PushHealthTests(unittest.TestCase):
    def test_latest_blocked_attempt_is_distinct_from_last_clean_completion(self):
        from claude_note import health
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "push.ok").write_text(json.dumps({"counts": {"pushed": 5}}))
            old = time.time() - 4 * 86400
            os.utime(root / "push.ok", (old, old))
            latest = {"pushed": 0, "errors": 0, "policy_denied": 2,
                      "policy_denied_reasons": {"Source lineage needs owner reconciliation": 2},
                      "revocations_pending": 1, "problems": ["2 note(s) denied by staging policy",
                      "staging policy: Source lineage needs owner reconciliation (2)"]}
            (root / "push.json").write_text(json.dumps({"status": "blocked", "counts": latest}))
            with mock.patch.object(health, "LOGS", root), \
                    mock.patch.object(health, "LAUNCH_AGENTS", root / "agents"), \
                    mock.patch.object(health, "_launchd_loaded", return_value=False):
                got = health._push_health()
            self.assertTrue(got["stale"])
            self.assertNotEqual(got["last_ok"], got["last_attempt"])
            self.assertEqual(got["attempt_status"], "blocked")
            self.assertEqual(got["counts"], latest)
            self.assertEqual(got["clean_counts"], {"pushed": 5})
            self.assertEqual(got["problems"], latest["problems"])

    def test_incomplete_attempt_is_reported_after_run_deadline(self):
        from claude_note import health
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            attempt = root / "push.json"
            attempt.write_text(json.dumps({"status": "running", "counts": {}}))
            old = time.time() - 46 * 60
            os.utime(attempt, (old, old))
            with mock.patch.object(health, "LOGS", root), \
                    mock.patch.object(health, "LAUNCH_AGENTS", root / "agents"), \
                    mock.patch.object(health, "_launchd_loaded", return_value=False):
                got = health._push_health()
            self.assertIn("latest push attempt did not complete", got["problems"][0])

    def test_report_includes_current_push_blockage_without_waiting_for_stale_success(self):
        from claude_note import health
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "push.ok").write_text(json.dumps({"counts": {"pushed": 1}}))
            (root / "push.json").write_text(json.dumps({"status": "blocked", "counts": {
                "problems": ["63 queued revocation(s) need review"]}}))
            with mock.patch.object(health, "LOGS", root), \
                    mock.patch.object(health, "LAUNCH_AGENTS", root / "agents"), \
                    mock.patch.object(health, "_config", return_value=(root / "config.toml", {})), \
                    mock.patch.object(health, "_launchd_loaded", return_value=False), \
                    mock.patch.object(health, "_notes", return_value={}), \
                    mock.patch.object(health, "_synthesis", return_value={}), \
                    mock.patch.object(health, "_which", return_value="/tool"), \
                    mock.patch.object(health, "freshness_summary", return_value={}):
                got = health.report()
            self.assertFalse(got["push"]["stale"])
            self.assertIn("push: 63 queued revocation(s) need review", got["problems"])
            self.assertFalse(got["ok"])
