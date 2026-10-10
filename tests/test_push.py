import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

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
        self.assertEqual(set(meta), {"source", "author", "assistant", "laptop", "source_path", "content_sha256", "note_type", "redactions"})
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
