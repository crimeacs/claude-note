"""Real owned children, public evidence and temporary homes only; no models."""

import json
import logging
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

# Standalone discovery must not load the owner's configuration at import time.
with tempfile.TemporaryDirectory(prefix="cn-public-sweep-import-") as import_root:
    with mock.patch.object(Path, "home", return_value=Path(import_root) / "home"), \
         mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(Path(import_root) / "config"),
                                      "CLAUDE_NOTE_VAULT_ROOT": str(Path(import_root) / "vault")}):
        from claude_note import capture_sweep, config, import_publication, queue_manager


PUBLIC_RAW = b'{"type":"user","uuid":"public-question","message":{"role":"user","content":"Public lifecycle fixture"}}\n'

CHILD_FIXTURE = r'''
import os
import signal
import sys
import time
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True
root, mode, source = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
sys.path.insert(0, str(source))
with mock.patch.object(Path, "home", return_value=root / "home"), mock.patch.dict(os.environ, {
    "XDG_CONFIG_HOME": str(root / "config"), "CLAUDE_NOTE_VAULT_ROOT": str(root / "vault")
}):
    from claude_note import config, importers, import_publication, state_io
    config.STATE_DIR = root / "state"
    config.QUEUE_DIR = root / "queue"
    config.CLAUDE_NOTE_DIR = root / ".claude-note"
    importers.TRANSCRIPTS_DIR = root / "transcripts"
    importers.SEEN_FILE = root / "transcripts/seen.json"
    importers.HEARTBEAT = root / "heartbeat/import-sweep.ok"
    signal.signal(signal.SIGTERM, signal.SIG_IGN)

    def pause():
        (root / "ready").write_text("public child ready\n")
        while True:
            time.sleep(1)

    if mode == "sleep":
        pause()
    else:
        real_create = state_io.create_bytes
        def pause_after_intent(path, raw):
            real_create(path, raw)
            if Path(path).parent == config.STATE_DIR / "import-intents":
                pause()

        def public_import(*args, **kwargs):
            path = root / "native.jsonl"
            import_publication.publish("claude-code", "public-session", "native-log", path,
                                       path.read_bytes(), "", "Public lifecycle fixture")
            return 1

        with mock.patch.object(state_io, "create_bytes", side_effect=pause_after_intent), \
             mock.patch.object(importers, "sweep_zips", side_effect=public_import), \
             mock.patch.object(importers, "sweep_cursor", return_value=0), \
             mock.patch.object(importers, "sweep_codex", return_value=0), \
             mock.patch.object(importers, "sweep_claude_code", return_value=0):
            importers.sweep()
'''


@unittest.skipUnless(os.name == "posix", "requires POSIX child signals")
class SweepLifecycleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="cn-public-sweep-lifecycle-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        (self.root / "vault").mkdir()
        self.source = Path(__file__).resolve().parents[1] / "src"
        self.fixture = self.root / "public_child.py"
        self.fixture.write_text(CHILD_FIXTURE)
        (self.root / "native.jsonl").write_bytes(PUBLIC_RAW)
        self.enterContext(mock.patch.object(Path, "home", return_value=self.root / "home"))
        self.enterContext(mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.root / "config")}))
        for name, value in (("VAULT_ROOT", self.root / "vault"), ("STATE_DIR", self.root / "state"),
                            ("QUEUE_DIR", self.root / "queue"), ("CLAUDE_NOTE_DIR", self.root / ".claude-note")):
            self.enterContext(mock.patch.object(config, name, value))
        self.enterContext(mock.patch.object(capture_sweep.shutil, "disk_usage",
                                            return_value=mock.Mock(free=8 * 1024 ** 3)))
        self.enterContext(mock.patch.object(capture_sweep, "STOP_GRACE_SECONDS", .1))
        self.real_popen = subprocess.Popen
        self.children = []
        self.mode = "sleep"
        self.enterContext(mock.patch.object(capture_sweep.subprocess, "Popen", side_effect=self.spawn_public_child))
        self.job = capture_sweep.Sweep(logging.getLogger("public-sweep-lifecycle"),
            status_path=self.root / "heartbeat/import-supervisor.json")
        self.addCleanup(self.cleanup_children)

    def spawn_public_child(self, command, **kwargs):
        if command == [sys.executable, "-m", "claude_note.capture_sweep"]:
            child = self.real_popen([sys.executable, "-I", "-S", "-B", str(self.fixture), str(self.root), self.mode,
                                     str(self.source)], cwd=self.root, **kwargs)
            self.children.append(child)
            return child
        # rss_bytes uses the real OS measurement against this test's owned PID.
        if command[:2] == ["/bin/ps", "-p"] and command[2] in {str(child.pid) for child in self.children}:
            return self.real_popen(command, **kwargs)
        raise AssertionError(f"Unexpected subprocess in public fixture: {command}")

    def cleanup_children(self):
        self.job.stopping.set()
        for child in self.children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)
        if self.job.thread is not None:
            self.job.thread.join(timeout=5)

    def wait_for_ready(self):
        until = time.monotonic() + 10
        while time.monotonic() < until:
            if (self.root / "ready").exists():
                return
            if self.children and self.children[0].poll() is not None:
                self.fail(f"Public child exited before checkpoint: {self.children[0].returncode}")
            time.sleep(.01)
        self.fail("Public child did not reach its temporary checkpoint")

    def assert_reaped(self, child):
        self.assertEqual(child.returncode, -signal.SIGKILL)
        self.assertIsNone(self.job.child)
        with self.assertRaises(ChildProcessError):
            os.waitpid(child.pid, os.WNOHANG)

    def test_supervisor_shutdown_kills_and_reaps_real_stubborn_child(self):
        self.job.start()
        self.wait_for_ready()
        child = self.children[0]
        started = time.monotonic()
        self.job.shutdown()
        self.assertLess(time.monotonic() - started, 3)
        self.assertFalse(self.job.thread.is_alive())
        self.assert_reaped(child)
        from claude_note import capture_sweep_status
        report = capture_sweep_status.read(self.root / "heartbeat/import-supervisor.json")
        self.assertEqual(report["owner_state"], "stopped")
        self.assertTrue(report["attempt"]["child_reaped"])
        self.assertEqual(report["attempt"]["reason"], "shutdown")

    def test_deadline_interrupts_import_intent_and_exact_replay_recovers(self):
        self.mode = "publication"
        self.job.tick()
        self.wait_for_ready()
        child = self.children[0]
        intent = next(path for path in (config.STATE_DIR / "import-intents").glob("*.json")
                      if len(path.name) == 69)
        intent_bytes = intent.read_bytes()
        record = json.loads(intent_bytes)
        snapshot = Path(record["snapshot_path"])
        first_snapshot = snapshot.read_bytes()
        self.assertEqual(list(queue_manager.read_queue_files()), [])
        self.assertFalse((config.STATE_DIR / "import-publications").exists())
        self.assertFalse((self.root / "heartbeat/import-sweep.ok").exists())

        self.job.started = time.monotonic() - capture_sweep.MAX_SECONDS - 1
        started = time.monotonic()
        self.job.tick()
        self.assertLess(time.monotonic() - started, 3)
        self.assert_reaped(child)
        from claude_note import capture_sweep_status
        report = capture_sweep_status.read(self.root / "heartbeat/import-supervisor.json")
        self.assertEqual(report["attempt"]["reason"], "deadline")
        self.assertTrue(report["attempt"]["child_reaped"])
        self.assertEqual(intent.read_bytes(), intent_bytes)
        self.assertEqual(snapshot.read_bytes(), first_snapshot)
        self.assertEqual(list(queue_manager.read_pending_events()), [])
        self.assertFalse((self.root / "heartbeat/import-sweep.ok").exists())

        result = import_publication.publish("claude-code", "public-session", "native-log",
            self.root / "native.jsonl", PUBLIC_RAW, "", "Public lifecycle fixture")
        self.assertEqual(result.status, "published")
        self.assertEqual(result.event_ids, tuple(event["event_id"] for event in record["events"]))
        self.assertEqual(result.ordinal, record["ordinal"])
        self.assertEqual(result.snapshot_path, snapshot)
        self.assertEqual(snapshot.read_bytes(), first_snapshot)
        events = list(queue_manager.read_pending_events())
        self.assertEqual([event.event for event in events], ["UserPromptSubmit", "Stop"])
        self.assertEqual(tuple(event.event_id for event in events), result.event_ids)
        retry = import_publication.publish("claude-code", "public-session", "native-log",
            self.root / "native.jsonl", PUBLIC_RAW, "", "Public lifecycle fixture")
        self.assertEqual(retry.status, "replayed")
        self.assertEqual(retry.event_ids, result.event_ids)
        self.assertEqual(len(list(queue_manager.read_all_events())), 2)


if __name__ == "__main__":
    unittest.main()
