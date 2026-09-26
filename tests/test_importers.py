import json
import os
import sqlite3
import tempfile
import time
import unittest
import zipfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp())
os.environ.setdefault("CLAUDE_NOTE_VAULT", str(_TMP / "vault"))

from claude_note import cli, config, importers, queue_manager  # noqa: E402
from claude_note.transcript_reader import read_transcript  # noqa: E402

CLAUDE_EXPORT = [{
    "uuid": "c1a0d0e0-0000-0000-0000-000000000001", "name": "Mixing",
    "chat_messages": [
        {"sender": "human", "text": "How do I compress vocals?", "content": [{"type": "text", "text": "How do I compress vocals?"}]},
        {"sender": "assistant", "text": "", "content": [{"type": "text", "text": "Use a 4:1 ratio."}]},
    ],
}]
CHATGPT_EXPORT = [{
    "conversation_id": "6650aaaa-0000-0000-0000-000000000002", "title": "Trip", "current_node": "n3",
    "mapping": {
        "n0": {"message": None, "parent": None},
        "n1": {"message": {"author": {"role": "user"}, "content": {"content_type": "text", "parts": ["Plan a trip"]}}, "parent": "n0"},
        "n2": {"message": {"author": {"role": "assistant"}, "content": {"content_type": "text", "parts": ["Go to Lisbon."]}}, "parent": "n1"},
        "n9": {"message": {"author": {"role": "assistant"}, "content": {"content_type": "text", "parts": ["Abandoned branch"]}}, "parent": "n1"},
        "n3": {"message": {"author": {"role": "user"}, "content": {"content_type": "text", "parts": ["Thanks"]}}, "parent": "n2"},
    },
}]


class ImporterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        importers.TRANSCRIPTS_DIR = self.tmp / "transcripts"
        importers.SEEN_FILE = importers.TRANSCRIPTS_DIR / "seen.json"
        importers.HEARTBEAT = self.tmp / "logs/import-sweep.ok"
        importers.IMPORTS_DIR = self.tmp / "imports"
        importers.WATCH_DIRS = [self.tmp / "downloads", importers.IMPORTS_DIR]
        importers.CURSOR_DB = self.tmp / "none.vscdb"
        importers.CURSOR_WORKSPACES = self.tmp / "ws"
        importers.CODEX_SESSIONS = self.tmp / "codex"
        config.QUEUE_DIR = self.tmp / "queue"
        config.STATE_DIR = self.tmp / "state"
        for d in importers.WATCH_DIRS:
            d.mkdir(parents=True)

    def _events(self):
        return list(queue_manager.read_all_events())

    def _zip(self, folder, name, convs, extra="chat.html"):
        path = folder / name
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("conversations.json", json.dumps(convs))
            zf.writestr(extra, "x")
        old = time.time() - 120
        os.utime(path, (old, old))
        return path

    def test_export_zips_import_once_and_move_to_done(self):
        self._zip(self.tmp / "downloads", "data-2026-09-25.zip", CLAUDE_EXPORT, "users.json")
        self._zip(importers.IMPORTS_DIR, "anything.zip", CHATGPT_EXPORT)
        (self.tmp / "downloads" / "unrelated.zip").write_bytes(b"not a zip")

        counts = importers.sweep()
        self.assertEqual(counts["zips"], 2)
        self.assertTrue((importers.IMPORTS_DIR / "done/data-2026-09-25.zip").exists())
        self.assertTrue((importers.IMPORTS_DIR / "done/anything.zip").exists())
        self.assertTrue((self.tmp / "downloads/unrelated.zip").exists())
        self.assertTrue(importers.HEARTBEAT.exists())

        events = self._events()
        self.assertEqual(sorted(e.event for e in events), ["Stop", "Stop", "UserPromptSubmit", "UserPromptSubmit"])
        gpt = next(e for e in events if e.cwd == "chatgpt.com")
        content = read_transcript(gpt.transcript_path)
        self.assertEqual(content.user_prompts, ["Plan a trip", "Thanks"])
        self.assertEqual(content.assistant_texts, ["Go to Lisbon."])

        # Same export again (re-downloaded): nothing new is queued.
        self._zip(importers.IMPORTS_DIR, "again.zip", CHATGPT_EXPORT)
        self.assertEqual(importers.sweep()["zips"], 0)
        self.assertEqual(len(self._events()), 4)

    def test_cursor_composer_bubbles(self):
        db = sqlite3.connect(importers.CURSOR_DB)
        db.execute("create table cursorDiskKV (key text primary key, value blob)")
        now = int(time.time() * 1000)
        comp = {"composerId": "comp1", "lastUpdatedAt": now,
                "fullConversationHeadersOnly": [{"bubbleId": "b1", "type": 1}, {"bubbleId": "b2", "type": 2}]}
        rows = [
            ("composerData:comp1", json.dumps(comp)),
            ("composerData:old", json.dumps({"composerId": "old", "lastUpdatedAt": 1000})),
            ("bubbleId:comp1:b1", json.dumps({"type": 1, "text": "Fix the build"})),
            ("bubbleId:comp1:b2", json.dumps({"type": 2, "text": "Fixed it.", "toolFormerData": {
                "name": "edit_file", "params": json.dumps({"relativeWorkspacePath": "/repo/a.py"}), "result": "ok"}})),
        ]
        db.executemany("insert into cursorDiskKV values (?, ?)", rows)
        db.commit()
        db.close()

        self.assertEqual(importers.sweep()["cursor"], 1)
        self.assertEqual(importers.sweep()["cursor"], 0)  # unchanged: not re-queued
        content = read_transcript(self._events()[0].transcript_path)
        self.assertEqual(content.user_prompts, ["Fix the build"])
        self.assertEqual(content.assistant_texts, ["Fixed it."])
        self.assertEqual(content.files_touched, ["/repo/a.py"])

    def test_codex_sweep_skips_subagents_and_hook_delivered(self):
        day = importers.CODEX_SESSIONS / "2026/09/25"
        day.mkdir(parents=True)

        def rollout(name, sid, source):
            path = day / name
            path.write_text("\n".join(json.dumps(r) for r in [
                {"type": "session_meta", "payload": {"id": sid, "cwd": "/repo", "source": source}},
                {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]}},
                {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "hello"}]}},
            ]) + "\n")
            old = time.time() - 3600
            os.utime(path, (old, old))

        rollout("rollout-a.jsonl", "sess-a", "vscode")
        rollout("rollout-b.jsonl", "sess-b", {"subagent": {"thread_spawn": {}}})
        rollout("rollout-c.jsonl", "sess-c", "exec")
        rollout("rollout-d.jsonl", "sess-d", "vscode")
        config.STATE_DIR.mkdir(parents=True)
        (config.STATE_DIR / "sess-d.json").write_text("{}")  # the hook already has it

        self.assertEqual(importers.sweep()["codex"], 1)
        self.assertEqual({e.session_id for e in self._events()}, {"sess-a"})
        self.assertEqual(importers.sweep()["codex"], 0)

    def test_install_codex_hooks_is_idempotent_and_keeps_existing(self):
        path = self.tmp / "hooks.json"
        path.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "other"}]}]}}))
        self.assertTrue(cli.install_codex_hooks(path, "/bin/claude-note enqueue"))
        self.assertFalse(cli.install_codex_hooks(path, "/bin/claude-note enqueue"))
        data = json.loads(path.read_text())["hooks"]
        self.assertEqual(len(data["Stop"]), 2)
        self.assertEqual(set(data), {"Stop", "UserPromptSubmit", "SessionEnd"})
        self.assertEqual(len(list(self.tmp.glob("hooks.json.bak-*"))), 1)


if __name__ == "__main__":
    unittest.main()
