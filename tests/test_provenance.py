import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("CLAUDE_NOTE_VAULT_ROOT", tempfile.mkdtemp())

from claude_note import health, knowledge_pack, models, note_router, note_writer, provenance, push  # noqa: E402

FILLER = " This paragraph is real content about how the thing works and why it matters." * 4


class AssistantTests(unittest.TestCase):
    def test_transcript_locations(self):
        cases = {
            "/Users/a/.claude/projects/-Users-a-x/abc.jsonl": "claude-code",
            "/Users/a/.codex/sessions/2026/09/29/rollout-1.jsonl": "codex",
            "/Users/a/.local/share/claude-note/transcripts/cursor/c1.jsonl": "cursor",
            "/Users/a/.local/share/claude-note/transcripts/claude-ai/u1.jsonl": "claude-app",
            "/Users/a/.local/share/claude-note/transcripts/chatgpt/g1.jsonl": "chatgpt",
            "": "claude-code",
            "/somewhere/else.jsonl": "",
        }
        for path, want in cases.items():
            self.assertEqual(provenance.assistant_for(path), want, path)

    def test_importer_source_wins(self):
        self.assertEqual(provenance.assistant_for("/x/y.jsonl", source="claude-ai"), "claude-app")

    def test_every_value_is_in_the_vocabulary(self):
        for value in provenance._IMPORTER_SOURCES.values():
            self.assertIn(value, provenance.ASSISTANTS)


class AuthorTests(unittest.TestCase):
    def setUp(self):
        provenance._author_cache = None
        self.addCleanup(setattr, provenance, "_author_cache", None)

    def test_env_then_config_then_foresyn_then_git(self):
        with tempfile.TemporaryDirectory() as tmp:
            xdg = Path(tmp) / "xdg"
            (xdg / "claude-note").mkdir(parents=True)
            foresyn = Path(tmp) / "config.json"
            foresyn.write_text(json.dumps({"email": "ws@example.com"}))
            env = {"XDG_CONFIG_HOME": str(xdg)}
            with mock.patch.dict(os.environ, env), mock.patch.object(provenance, "FORESYN_CONFIG", foresyn), \
                    mock.patch.object(provenance, "_git_email", return_value="git@example.com"):
                os.environ.pop("CLAUDE_NOTE_AUTHOR", None)
                self.assertEqual(provenance.author_email(), "ws@example.com")
                provenance._author_cache = None
                (xdg / "claude-note/config.toml").write_text('author = "cfg@example.com"\nvault_root = "/v"\n')
                self.assertEqual(provenance.author_email(), "cfg@example.com")
                provenance._author_cache = None
                with mock.patch.dict(os.environ, {"CLAUDE_NOTE_AUTHOR": "env@example.com"}):
                    self.assertEqual(provenance.author_email(), "env@example.com")
                provenance._author_cache = None
                (xdg / "claude-note/config.toml").write_text('vault_root = "/v"\n')
                foresyn.write_text("{}")
                self.assertEqual(provenance.author_email(), "git@example.com")

    def test_stamp_keeps_existing_values(self):
        with mock.patch.object(provenance, "author_email", return_value="me@example.com"):
            self.assertEqual(provenance.stamp({"type": "pattern"}, "codex"),
                             {"type": "pattern", "assistant": "codex", "author": "me@example.com"})
            self.assertEqual(provenance.stamp({"assistant": "cursor", "author": "x@example.com"}, "codex"),
                             {"assistant": "cursor", "author": "x@example.com"})
            self.assertNotIn("assistant", provenance.stamp({}, ""))


class NoteStampingTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(provenance, "author_email", return_value="me@example.com")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_session_note_frontmatter(self):
        state = models.SessionState(session_id="s1", first_event_ts="2026-09-29T10:00:00Z",
                                    last_event_ts="2026-09-29T10:05:00Z",
                                    transcript_path="/Users/a/.codex/sessions/rollout.jsonl")
        fm = push.frontmatter(note_writer.generate_note_content(state))
        self.assertEqual(fm["assistant"], "codex")
        self.assertEqual(fm["author"], "me@example.com")

    def test_created_knowledge_note_frontmatter(self):
        with tempfile.TemporaryDirectory() as vault:
            op = knowledge_pack.NoteOp(op="create", path="retry-lost-response",
                                       frontmatter={"type": "gotcha"}, body_markdown="body")
            self.assertTrue(note_router.apply_note_op(op, Path(vault), session_id="s1", assistant="cursor"))
            fm = push.frontmatter((Path(vault) / "retry-lost-response.md").read_text())
            self.assertEqual((fm["type"], fm["assistant"], fm["author"]), ("gotcha", "cursor", "me@example.com"))


class FakeClient:
    def __init__(self):
        self.calls = []

    def put(self, path, content, metadata):
        self.calls.append((path, content, metadata))
        return 200, {"document": {"revision": 1}}


class PushCarriesProvenanceTests(unittest.TestCase):
    def test_metadata_has_assistant_and_author(self):
        with tempfile.TemporaryDirectory() as vault, tempfile.TemporaryDirectory() as state:
            Path(vault, "a.md").write_text(f"---\ntype: pattern\nassistant: codex\n---\n\n{FILLER}\n")
            Path(vault, "b.md").write_text(f"---\ntype: pattern\nassistant: not-a-thing\n---\n\n{FILLER}\n")
            client = FakeClient()
            with mock.patch.object(push, "STATE_FILE", Path(state) / "s.json"), \
                    mock.patch.object(push, "HEARTBEAT", Path(state) / "push.ok"), \
                    mock.patch.object(provenance, "author_email", return_value="me@example.com"):
                push.run(vault=Path(vault), client=client)
            meta = {Path(m["source_path"]).stem: m for _, _, m in client.calls}
            self.assertEqual(meta["a"]["assistant"], "codex")
            self.assertEqual(meta["b"]["assistant"], "")
            self.assertEqual(meta["a"]["author"], "me@example.com")


class HealthTests(unittest.TestCase):
    def test_unconfigured_machine_reports_instead_of_exiting(self):
        with tempfile.TemporaryDirectory() as home:
            env = {"XDG_CONFIG_HOME": str(Path(home) / "xdg")}
            with mock.patch.dict(os.environ, env), \
                    mock.patch.object(health, "HOME", Path(home)), \
                    mock.patch.object(health, "LOGS", Path(home) / "logs"), \
                    mock.patch.object(health, "STATE_DIR", Path(home) / "state"), \
                    mock.patch.object(health, "LAUNCH_AGENTS", Path(home) / "LaunchAgents"), \
                    mock.patch.object(health, "_launchd_loaded", return_value=False), \
                    mock.patch.object(provenance, "author_email", return_value=""):
                saved = os.environ.pop("CLAUDE_NOTE_VAULT_ROOT", None)
                try:
                    out = health.report()
                finally:
                    if saved is not None:
                        os.environ["CLAUDE_NOTE_VAULT_ROOT"] = saved
        self.assertFalse(out["configured"])
        self.assertFalse(out["ok"])
        self.assertTrue(any("not configured" in p for p in out["problems"]))
        json.dumps(out)  # serializable

    mode = "route"

    def test_log_mode_with_claude_present_is_a_problem(self):
        self.mode = "log"
        with self.assertRaises(AssertionError) as caught:
            self.test_healthy_machine()
        self.assertIn("synthesis is off", str(caught.exception))

    def test_healthy_machine(self):
        with tempfile.TemporaryDirectory() as home:
            home = Path(home)
            vault = home / "vault"
            vault.mkdir()
            (home / "xdg/claude-note").mkdir(parents=True)
            (home / "xdg/claude-note/config.toml").write_text(f'vault_root = "{vault}"\n[synthesis]\nmode = "{self.mode}"\n')
            (home / "LaunchAgents").mkdir()
            (home / "LaunchAgents/com.claude-note.worker.plist").write_text("x")
            (home / "LaunchAgents/com.claude-note.push.plist").write_text("x")
            (home / ".claude").mkdir()
            (home / ".claude/settings.json").write_text(json.dumps({"hooks": {"Stop": [
                {"hooks": [{"type": "command", "command": "/x/claude-note enqueue"}]}]}}))
            with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(home / "xdg"),
                                              "CLAUDE_NOTE_VAULT_ROOT": str(vault)}), \
                    mock.patch.object(health, "HOME", home), \
                    mock.patch.object(health, "LOGS", home / "logs"), \
                    mock.patch.object(health, "STATE_DIR", home / "state"), \
                    mock.patch.object(health, "LAUNCH_AGENTS", home / "LaunchAgents"), \
                    mock.patch.object(health, "_launchd_loaded", return_value=True), \
                    mock.patch.object(health, "_which", return_value="/usr/local/bin/tool"), \
                    mock.patch.object(provenance, "author_email", return_value="me@example.com"):
                out = health.report()
        self.assertEqual(out["problems"], [])
        self.assertEqual(out["notes"]["knowledge"], 0)
        self.assertTrue(out["ok"])
        self.assertTrue(out["hooks"]["claude_code"])


if __name__ == "__main__":
    unittest.main()
