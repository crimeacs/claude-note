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
    def __init__(self):
        self.calls = []

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
        self.assertTrue(paths[0].endswith("/pattern-a.md"))
        self.assertTrue(paths[1].endswith("/topics__gotcha-b.md"))
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
