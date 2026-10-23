"""Bounded retention inspection of synthetic journals in temporary vaults."""

import fcntl
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from claude_note import config, models, queue_manager, session_tracker


class QueueRetentionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="cn-public-retention-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.enterContext(mock.patch.multiple(config, STATE_DIR=self.root / "state",
                                              QUEUE_DIR=self.root / "queue"))
        config.QUEUE_DIR.mkdir()
        self.event = models.QueuedEvent("public-event", "2000-01-01T00:00:00Z",
                                        "UserPromptSubmit", "public-session", "", "",
                                        {"prompt": "Synthetic retained evidence"})
        self.raw = (self.event.to_json() + "\n").encode()

    def journal(self, raw=None, *, name="2000-01-01.jsonl"):
        path = config.QUEUE_DIR / name
        path.write_bytes(self.raw if raw is None else raw)
        return path

    def capture(self):
        state = models.SessionState(self.event.session_id, self.event.ts, self.event.ts,
                                     processed_event_ids=[self.event.event_id])
        session_tracker.save_session_state(state)

    def test_complete_captured_old_journal_retires_but_recent_and_unrecognized_stay(self):
        self.capture()
        old = self.journal()
        boundary = (datetime.utcnow().date() - timedelta(days=7)).isoformat()
        recent = self.journal(name=f"{boundary}.jsonl")
        unrecognized = self.journal(name="retained-history.jsonl")
        queue_manager.cleanup_old_queue_files()
        self.assertFalse(old.exists())
        self.assertEqual(recent.read_bytes(), self.raw)
        self.assertEqual(unrecognized.read_bytes(), self.raw)

    def test_empty_complete_old_journal_retains_existing_retirement_semantics(self):
        path = self.journal(b"")
        queue_manager.cleanup_old_queue_files()
        self.assertFalse(path.exists())

    def test_uncaptured_or_unreadable_capture_state_keeps_exact_journal(self):
        path = self.journal()
        queue_manager.cleanup_old_queue_files()
        self.assertEqual(path.read_bytes(), self.raw)
        self.capture()
        session_tracker.get_state_file(self.event.session_id).write_bytes(b"not-json\n")
        queue_manager.cleanup_old_queue_files()
        self.assertEqual(path.read_bytes(), self.raw)

    def test_partial_malformed_and_oversized_records_are_retained(self):
        self.capture()
        for suffix in (self.raw[:-1], b"not-json\n", b"\xff\n", b"x" * 513 + b"\n"):
            with self.subTest(suffix=suffix[:20]):
                original = self.raw + suffix
                path = self.journal(original)
                with mock.patch.object(queue_manager, "MAX_EVENT_BYTES", 512):
                    queue_manager.cleanup_old_queue_files()
                self.assertEqual(path.read_bytes(), original)

    def test_streams_many_records_with_bounded_readline_and_no_whole_file_read(self):
        self.capture()
        path = self.journal(self.raw * 2000)
        self.assertGreater(path.stat().st_size, 512 * 100)
        inode = path.stat().st_ino
        fdopen = os.fdopen
        reads = []
        test = self

        class BoundedJournal:
            def __init__(self, handle):
                self.handle = handle

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return self.handle.__exit__(*args)

            def fileno(self):
                return self.handle.fileno()

            def readline(self, size=-1):
                test.assertEqual(size, 513)
                reads.append(size)
                return self.handle.readline(size)

            def read(self, *args):
                raise AssertionError("Journal retention must not read the entire file")

            def __iter__(self):
                raise AssertionError("Journal retention must bound individual records")

        def open_bounded(descriptor, *args, **kwargs):
            handle = fdopen(descriptor, *args, **kwargs)
            return BoundedJournal(handle) if os.fstat(descriptor).st_ino == inode else handle

        with mock.patch.object(queue_manager, "MAX_EVENT_BYTES", 512), \
             mock.patch.object(queue_manager.os, "fdopen", side_effect=open_bounded), \
             mock.patch.object(Path, "read_bytes", side_effect=AssertionError("No whole-file journal reads")):
            queue_manager.cleanup_old_queue_files()
        self.assertEqual(len(reads), 2001)
        self.assertFalse(path.exists())

    def test_record_over_limit_is_not_parsed_or_acknowledged(self):
        path = self.journal(b"x" * 513 + b"\n")
        with mock.patch.object(queue_manager, "MAX_EVENT_BYTES", 512), \
             mock.patch.object(models.QueuedEvent, "from_json", side_effect=AssertionError("Oversized record parsed")):
            queue_manager.cleanup_old_queue_files()
        self.assertEqual(path.stat().st_size, 514)

    def test_writer_lock_keeps_journal_until_later_complete_inspection(self):
        self.capture()
        path = self.journal()
        with path.open("rb") as writer:
            fcntl.flock(writer.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            queue_manager.cleanup_old_queue_files()
            self.assertEqual(path.read_bytes(), self.raw)
        queue_manager.cleanup_old_queue_files()
        self.assertFalse(path.exists())

    def test_changed_file_and_replaced_path_are_retained(self):
        self.capture()
        original_parser = models.QueuedEvent.from_json
        for replace in (False, True):
            with self.subTest(replace=replace):
                path = self.journal()
                changed = False

                def change_after_parse(line):
                    nonlocal changed
                    if not changed:
                        changed = True
                        if replace:
                            replacement = self.root / "replacement"
                            replacement.write_bytes(self.raw)
                            replacement.replace(path)
                        else:
                            with path.open("ab") as stream:
                                stream.write(self.raw)
                    return original_parser(line)

                with mock.patch.object(models.QueuedEvent, "from_json", side_effect=change_after_parse):
                    queue_manager.cleanup_old_queue_files()
                self.assertTrue(path.exists())
                self.assertEqual(path.read_bytes(), self.raw if replace else self.raw * 2)

    def test_symlink_or_hardlinked_journal_never_retires_source(self):
        self.capture()
        source = self.root / "original-history.jsonl"
        source.write_bytes(self.raw)
        path = config.QUEUE_DIR / "2000-01-01.jsonl"
        path.symlink_to(source)
        queue_manager.cleanup_old_queue_files()
        self.assertTrue(path.is_symlink())
        self.assertEqual(source.read_bytes(), self.raw)
        path.unlink()
        os.link(source, path)
        queue_manager.cleanup_old_queue_files()
        self.assertTrue(path.exists())
        self.assertEqual(source.read_bytes(), self.raw)


if __name__ == "__main__":
    unittest.main()
