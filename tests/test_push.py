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
        self.assertEqual(set(meta), {"source", "author", "laptop", "source_path", "content_sha256", "note_type", "redactions"})
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
