"""Real supervised children and public import fixtures, isolated from the owner's sources."""

import hashlib
import json
import logging
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from contextlib import closing
from pathlib import Path
from unittest import mock

from claude_note import capture_sweep, config


CHILD = r'''
import json
import os
import resource
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

root = Path(os.environ["PUBLIC_CAPTURE_ROOT"])
expanduser = os.path.expanduser
def public_expanduser(path):
    value = os.fspath(path)
    if value == "~" or value.startswith("~/"):
        return str(root) + value[1:]
    return expanduser(path)
mock.patch.object(Path, "home", return_value=root).start()
mock.patch.object(os.path, "expanduser", side_effect=public_expanduser).start()
from claude_note import capture_sweep, importers, models, session_tracker, source_store

free = int(os.environ.get("PUBLIC_CAPTURE_FREE", str(8 * 1024 ** 3)))
capture_sweep.shutil.disk_usage = lambda path: SimpleNamespace(free=free)
source_store._free_bytes = lambda state_dir=None: free
if os.environ.get("PUBLIC_CAPTURE_HOOK_STATE"):
    session_tracker.save_session_state(models.SessionState("public-session", "2026-10-23T00:00:00Z", "2026-10-23T00:00:00Z"))
code = capture_sweep.main()
seen = json.loads(importers.SEEN_FILE.read_text()) if importers.SEEN_FILE.exists() else {}
status = importers.HEARTBEAT.with_suffix(".json")
status = json.loads(status.read_text()) if status.exists() else {}
rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
print(json.dumps({"code": code, "seen_keys": sorted(seen), "heartbeat": importers.HEARTBEAT.exists(),
                  "status": status, "max_rss_bytes": rss if sys.platform == "darwin" else rss * 1024}))
sys.exit(code)
'''


class CaptureSweepProfileTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="cn-public-sweep-profile-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.vault = self.root / "vault"
        self.vault.mkdir()
        self.source_root = Path(__file__).resolve().parents[1] / "src"

    def child(self, *, hook_state=False, free=8 * 1024 ** 3):
        environment = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PUBLIC_CAPTURE_ROOT": str(self.root),
                       "XDG_CONFIG_HOME": str(self.root / "config"), "CLAUDE_NOTE_VAULT": str(self.vault),
                       "PYTHONPATH": str(self.source_root), "PUBLIC_CAPTURE_FREE": str(free)}
        if hook_state:
            environment["PUBLIC_CAPTURE_HOOK_STATE"] = "1"
        result = subprocess.run([sys.executable, "-c", CHILD], env=environment,
                                cwd=self.root, capture_output=True, text=True, timeout=15)
        self.assertIn(result.returncode, (0, 1), result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(result.returncode, report["code"])
        self.assertLess(report["max_rss_bytes"], capture_sweep.MAX_RSS_BYTES)
        return report

    def native(self, records, *, tail=b""):
        path = self.root / ".codex/sessions/2026/10/23/rollout-public.jsonl"
        path.parent.mkdir(parents=True)
        meta = {"type": "session_meta", "payload": {"id": "public-session", "cwd": str(self.root)}}
        with path.open("wb") as stream:
            for row in [meta, *records]:
                stream.write(json.dumps(row).encode() + b"\n")
            stream.write(tail)
        os.utime(path, (time.time() - 1200, time.time() - 1200))
        return path

    def message(self, role, text):
        return {"type": "response_item", "payload": {"type": "message", "role": role,
                "content": [{"type": "input_text" if role == "user" else "output_text", "text": text}]}}

    def assert_deferred(self, report):
        self.assertEqual(report["code"], 1)
        self.assertFalse(report["heartbeat"], "An incomplete sweep cannot publish a clean heartbeat")
        self.assertEqual(report["seen_keys"], ["_watermark"])
        self.assertGreater(report["status"]["counts"]["failed"], 0)

    def test_oversized_record_below_payload_cap_cannot_use_hook_seen_shortcut(self):
        path = self.native([self.message("user", "x" * (1024 ** 2)), self.message("assistant", "Public result")])
        self.assertLess(path.stat().st_size, 8 * 1024 ** 2)
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        self.assert_deferred(self.child(hook_state=True))
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)

    def test_native_projection_over_eight_mib_is_visible_failure_without_seen(self):
        path = self.native([self.message("user", "Public question"),
                            *[self.message("assistant", "x" * (128 * 1024)) for _ in range(65)]])
        self.assertGreater(path.stat().st_size, 8 * 1024 ** 2)
        before = path.stat().st_size
        self.assert_deferred(self.child())
        self.assertEqual(path.stat().st_size, before)

    def test_incomplete_native_source_does_not_mark_hook_delivered_session_seen(self):
        path = self.native([self.message("user", "Public question"), self.message("assistant", "Public answer")], tail=b'{"incomplete":')
        before = path.read_bytes()
        self.assert_deferred(self.child(hook_state=True))
        self.assertEqual(path.read_bytes(), before)

    def test_complete_small_native_source_still_captures_and_reports_success(self):
        path = self.native([self.message("user", "Public question"), self.message("assistant", "Public answer")])
        before = path.read_bytes()
        report = self.child()
        self.assertEqual(report["code"], 0, report)
        self.assertTrue(report["heartbeat"])
        self.assertIn("codex:rollout-public.jsonl", report["seen_keys"])
        self.assertEqual(report["status"]["counts"]["codex"], 1)
        self.assertEqual(path.read_bytes(), before)

    def archive(self, *, large_member=False, large_archive=False):
        path = self.root / "Documents/claude-note-imports/data-public.zip"
        path.parent.mkdir(parents=True)
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("conversations.json", b"x" * (8 * 1024 ** 2 + 1) if large_member else b"[]")
            if large_archive:
                archive.writestr("media.bin", b"x" * (8 * 1024 ** 2 + 1), compress_type=zipfile.ZIP_STORED)
        os.utime(path, (time.time() - 1200, time.time() - 1200))
        return path

    def assert_archive_deferred(self, *, large_member):
        path = self.archive(large_member=large_member, large_archive=not large_member)
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        self.assert_deferred(self.child())
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)
        self.assertFalse((path.parent / "done").exists())

    def test_zip_member_limit_does_not_advance_seen_or_copy_backup(self):
        self.assert_archive_deferred(large_member=True)

    def test_whole_archive_limit_does_not_advance_seen_or_copy_backup(self):
        self.assert_archive_deferred(large_member=False)

    def test_zip_copy_reserve_is_checked_before_processing_any_conversation(self):
        path = self.archive()
        self.assert_deferred(self.child(free=capture_sweep.MIN_FREE_BYTES + path.stat().st_size - 1))
        self.assertTrue(path.exists())
        self.assertFalse((path.parent / "done").exists())

    def test_cursor_payload_cap_does_not_mark_oversized_composer_seen(self):
        path = self.root / "Library/Application Support/Cursor/User/globalStorage/state.vscdb"
        path.parent.mkdir(parents=True)
        with closing(sqlite3.connect(path)) as database:
            database.execute("create table cursorDiskKV (key text primary key, value text)")
            database.execute("insert into cursorDiskKV values (?, ?)", ("composerData:public", "x" * (8 * 1024 ** 2 + 1)))
            database.commit()
        before = path.stat().st_size
        self.assert_deferred(self.child())
        self.assertEqual(path.stat().st_size, before)

    def test_normalized_publication_cap_rejects_before_publication_and_failed_count_is_failure(self):
        from claude_note import import_publication
        original_limit = import_publication.MAX_RAW
        invoked = []

        def attempt():
            with self.assertRaises(ValueError):
                import_publication.publish("cursor", "public", "public", self.root / "public.jsonl",
                                           b"x" * (8 * 1024 ** 2 + 1), "", "Public prompt")
            return {"failed": 1}

        with mock.patch.object(capture_sweep.shutil, "disk_usage", return_value=mock.Mock(free=8 * 1024 ** 3)), \
             mock.patch.object(capture_sweep.importers, "sweep", side_effect=attempt), \
             mock.patch.object(import_publication, "publish", side_effect=lambda *args, **kwargs: invoked.append(args)):
            self.assertEqual(capture_sweep.main(), 1)
        self.assertFalse(invoked)
        self.assertEqual(import_publication.MAX_RAW, original_limit, "Historical source verification retains its original bound")

    def test_normalized_unicode_expansion_is_rejected_before_transcript_write_or_seen(self):
        from claude_note import egress, importers
        transcripts = self.root / "transcripts"
        seen = {}
        messages = [{"role": "user", "text": "Public question"},
                    {"role": "assistant", "text": "\u2603" * 1500000}]

        def attempt():
            with self.assertRaises(ValueError):
                importers.write_session(seen, "cursor", "public-expanded", messages)
            return {"failed": 1}

        with mock.patch.object(capture_sweep.shutil, "disk_usage", return_value=mock.Mock(free=8 * 1024 ** 3)), \
             mock.patch.object(importers, "TRANSCRIPTS_DIR", transcripts), \
             mock.patch.object(importers, "sweep", side_effect=attempt), \
             mock.patch.object(egress, "capture_restrictions"):
            self.assertEqual(capture_sweep.main(), 1)
        self.assertEqual(seen, {})
        self.assertEqual(list(transcripts.rglob("*.jsonl")), [])

    def test_transcript_write_reserve_fails_before_any_bytes_are_persisted(self):
        from claude_note import importers, state_io
        transcripts = self.root / "transcripts"
        path = transcripts / "public.jsonl"

        def attempt():
            with self.assertRaises(ValueError):
                state_io.atomic_bytes(path, b"public\n")
            return {"failed": 1}

        with mock.patch.object(capture_sweep.shutil, "disk_usage", return_value=mock.Mock(free=capture_sweep.MIN_FREE_BYTES)), \
             mock.patch.object(importers, "TRANSCRIPTS_DIR", transcripts), \
             mock.patch.object(importers, "sweep", side_effect=attempt):
            self.assertEqual(capture_sweep.main(), 1)
        self.assertFalse(path.exists())

    def test_real_child_rss_is_measured_stopped_and_reaped_without_spawning_worker(self):
        marker = self.root / "ready"
        code = "import pathlib,sys,time; data=bytearray(24*1024*1024); pathlib.Path(sys.argv[1]).write_text('ready'); time.sleep(30)"
        child = subprocess.Popen([sys.executable, "-c", code, str(marker)], stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        def reap():
            if child.poll() is None:
                child.kill()
            child.wait(timeout=3)
        self.addCleanup(reap)
        deadline = time.monotonic() + 5
        while not marker.exists() and child.poll() is None and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertTrue(marker.exists())
        self.assertGreater(capture_sweep.rss_bytes(child.pid), 16 * 1024 ** 2)
        job = capture_sweep.Sweep(logging.getLogger("public-real-child"))
        job.child, job.started = child, time.monotonic()
        with mock.patch.object(config, "VAULT_ROOT", self.vault), \
             mock.patch.object(capture_sweep, "MIN_FREE_BYTES", 0), \
             mock.patch.object(capture_sweep, "MAX_RSS_BYTES", 16 * 1024 ** 2):
            job.tick()
        self.assertIsNone(job.child)
        self.assertIsNotNone(child.poll())


if __name__ == "__main__":
    unittest.main()
