"""Fresh hook capture stays responsive to older public journal backlogs."""

import hashlib
import logging
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

with tempfile.TemporaryDirectory(prefix="cn-public-priority-import-") as import_root:
    with mock.patch.object(Path, "home", return_value=Path(import_root) / "home"), \
         mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(Path(import_root) / "config"),
                                      "CLAUDE_NOTE_VAULT_ROOT": str(Path(import_root) / "vault")}):
        from claude_note import (config, import_publication, models, note_writer,
                                 queue_manager, session_tracker, worker)


class CapturePriorityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="cn-public-capture-priority-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.logger = logging.getLogger("public-capture-priority")
        self.enterContext(mock.patch.object(Path, "home", return_value=self.root / "home"))
        self.enterContext(mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.root / "config")}))
        self.enterContext(mock.patch.multiple(config, VAULT_ROOT=self.root / "vault",
            CLAUDE_NOTE_DIR=self.root / ".claude-note", STATE_DIR=self.root / "state",
            QUEUE_DIR=self.root / "queue", SYNTH_MODE="route", DEBOUNCE_SECONDS=0))
        self.enterContext(mock.patch.object(worker, "_capture_only", True))
        self.enterContext(mock.patch.object(worker, "_shutdown", False))
        self.enterContext(mock.patch.object(queue_manager, "_import_failures", {}))

    def event(self, identity, *, session=None, day="2026-10-23", padding=0):
        return models.QueuedEvent(identity, day + "T00:00:00Z", "UserPromptSubmit",
            session or identity, "", "", {"prompt": "Public capture priority fixture", "padding": "x" * padding})

    def journal(self, events, day):
        path = config.QUEUE_DIR / (day + ".jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("wb") as handle:
            for event in events:
                handle.write((event.to_json() + "\n").encode())
        return path

    def capture(self, events):
        for session_id in {event.session_id for event in events}:
            state = session_tracker.update_session_from_events(session_id,
                [event for event in events if event.session_id == session_id])
            session_tracker.save_session_state(state)

    def publication(self, session):
        path = self.root / (session + ".jsonl")
        raw = b'{"type":"user","message":{"content":"Public immutable history"}}\n'
        path.write_bytes(raw)
        return import_publication.publish("claude-code", session, str(path), path, raw, "", "Public import")

    def test_capture_worker_delivers_new_prompt_before_large_old_day_and_old_replay_progresses(self):
        older = [self.event(f"old-{number}", session="older-session", day="2026-10-21",
                            padding=1024 * 1024) for number in range(17)]
        fresh = self.event("fresh-prompt", session="fresh-session")
        old_path = self.journal(older, "2026-10-21")
        new_path = self.journal([fresh], "2026-10-23")
        before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in (old_path, new_path)}
        self.assertGreater(old_path.stat().st_size, queue_manager.MAX_PENDING_BYTES)

        default = list(queue_manager.read_pending_events())
        self.assertNotIn(fresh, default)
        selected = list(queue_manager.read_pending_events(recent_first=True))
        self.assertEqual(selected[0], fresh)
        self.assertEqual(list(queue_manager.read_pending_events(recent_first=True)), selected)
        self.assertLessEqual(sum(len((event.to_json() + "\n").encode()) for event in selected),
                             queue_manager.MAX_PENDING_BYTES)
        self.assertFalse((config.STATE_DIR / "older-session.json").exists())
        self.assertFalse((config.STATE_DIR / "queue-offsets").exists(), "Priority does not acknowledge old work")

        order = []
        process = worker.process_session
        def record(session_id, events, logger):
            order.append(session_id)
            return process(session_id, events, logger)

        with mock.patch.object(worker, "process_session", side_effect=record), \
             mock.patch.object(note_writer, "update_session_note", return_value=self.root / "public.md"), \
             mock.patch.object(session_tracker, "get_sessions_ready_for_write", return_value=[]), \
             mock.patch.object(worker.synthesizer, "synthesize_from_state", side_effect=AssertionError("No model")), \
             mock.patch.object(worker.vault_indexer, "get_index", side_effect=AssertionError("No index")):
            self.assertEqual(worker.poll_once(self.logger), 2)
            self.assertEqual(order[0], "fresh-session")
            self.assertIn(fresh.event_id, session_tracker.load_session_state("fresh-session").processed_event_ids)
            captured_old = session_tracker.load_session_state("older-session").processed_event_ids
            self.assertGreater(len(captured_old), 0)
            self.assertLess(len(captured_old), len(older))
            remaining = list(queue_manager.read_pending_events(recent_first=True))
            self.assertEqual([event.event_id for event in remaining],
                             [event.event_id for event in older if event.event_id not in captured_old])
            # Same-timestamp old records add durable capture IDs without
            # requiring another note rewrite.
            self.assertEqual(worker.poll_once(self.logger), 0)
            self.assertEqual(set(session_tracker.load_session_state("older-session").processed_event_ids),
                             {event.event_id for event in older})
        self.assertEqual(list(queue_manager.read_pending_events(recent_first=True)), [])
        self.assertEqual({path: hashlib.sha256(path.read_bytes()).hexdigest() for path in before}, before)

    def test_default_order_unchanged_and_recent_journals_precede_imports(self):
        old, new = self.event("old", day="2026-10-21"), self.event("new")
        self.journal([old], "2026-10-21")
        self.journal([new], "2026-10-23")
        result = self.publication("imported")
        expected_imports = list(queue_manager.read_events(result.queue_path))
        self.assertEqual(list(queue_manager.read_pending_events()), [old, new, *expected_imports])
        self.assertEqual(list(queue_manager.read_pending_events(recent_first=False)), [old, new, *expected_imports])
        self.assertEqual(list(queue_manager.read_pending_events(recent_first=True)), [new, old, *expected_imports])

    def test_all_import_metadata_is_checked_before_first_recent_delivery(self):
        fresh = self.event("fresh")
        self.journal([fresh], "2026-10-23")
        result = self.publication("historical-other-session")
        with mock.patch.object(import_publication, "_validate_batch_metadata",
                               wraps=import_publication._validate_batch_metadata) as metadata:
            delivery = queue_manager.read_pending_events(limit=1, recent_first=True)
            self.assertEqual(next(delivery), fresh)
            metadata.assert_called_once_with(result.queue_path)
            delivery.close()

    def test_corrupt_historical_same_session_still_blocks_recent_hook_and_cursor_ack(self):
        result = self.publication("blocked")
        self.capture(list(queue_manager.read_events(result.queue_path)))
        state_path = config.STATE_DIR / "blocked.json"
        before = state_path.read_bytes()
        fresh = self.event("blocked-fresh", session="blocked")
        fresh.ts = "2035-01-01T00:00:00Z"
        healthy = self.event("independent-fresh")
        path = self.journal([fresh, healthy], "2026-10-23")
        queue_before = path.read_bytes()
        result.snapshot_path.write_bytes(result.snapshot_path.read_bytes().replace(b"Public", b"Broken", 1))

        self.assertEqual(list(queue_manager.read_pending_events(recent_first=True)), [healthy])
        self.assertEqual(state_path.read_bytes(), before)
        self.assertFalse((config.STATE_DIR / "queue-offsets" / "2026-10-23.json").exists())
        self.assertEqual(path.read_bytes(), queue_before)
        self.assertNotIn(fresh.event_id, session_tracker.load_session_state("blocked").processed_event_ids)

    def test_only_capture_worker_opts_into_recent_delivery(self):
        with mock.patch.object(queue_manager, "read_pending_events", return_value=iter(())) as pending, \
             mock.patch.object(session_tracker, "get_sessions_ready_for_write", return_value=[]):
            self.assertEqual(worker.poll_once(self.logger), 0)
            pending.assert_called_once_with(recent_first=True)
        with mock.patch.object(worker, "_capture_only", False), \
             mock.patch.object(queue_manager, "read_pending_events", return_value=iter(())) as pending, \
             mock.patch.object(session_tracker, "get_sessions_ready_for_write", return_value=[]), \
             mock.patch.object(session_tracker, "get_pending_synthesis_sessions", return_value=[]):
            self.assertEqual(worker.poll_once(self.logger), 0)
            pending.assert_called_once_with()

    def test_invalid_priority_fails_before_queue_read(self):
        with mock.patch.object(queue_manager, "read_queue_files", side_effect=AssertionError("No read for invalid priority")):
            for value in (1, None, "yes"):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    list(queue_manager.read_pending_events(recent_first=value))


if __name__ == "__main__":
    unittest.main()
