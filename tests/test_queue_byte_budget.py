"""Pending delivery bounds with authored public JSONL and temporary state only."""

import json
import logging
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from claude_note import config, import_publication, models, queue_manager, session_tracker, worker


class PendingByteBudgetTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="cn-public-queue-budget-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.enterContext(mock.patch.multiple(config, STATE_DIR=self.root / "state", QUEUE_DIR=self.root / "queue"))
        self.enterContext(mock.patch.object(Path, "home", return_value=self.root / "home"))
        self.enterContext(mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.root / "config")}))

    def event(self, number, *, size=1024, session=None, unicode=False):
        return models.QueuedEvent(f"public-{number}", "2026-10-23T00:00:00Z", "UserPromptSubmit",
                                  session or f"session-{number}", "", "", {"prompt": ("🌍" if unicode else "x") * size})

    def journal(self, events, day="2026-10-23", *, ensure_ascii=True):
        path = config.QUEUE_DIR / f"{day}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = b"".join((json.dumps(json.loads(event.to_json()), ensure_ascii=ensure_ascii) + "\n").encode() for event in events)
        path.write_bytes(raw)
        return path

    def capture(self, events):
        for session in {event.session_id for event in events}:
            values = [event for event in events if event.session_id == session]
            state = session_tracker.update_session_from_events(session, values)
            session_tracker.save_session_state(state)

    def test_default_delivery_and_actual_poll_grouping_are_bounded(self):
        # Seventeen ordinary <=4MiB records exceed the intended16MiB poll budget.
        events = [self.event(number, size=1024 * 1024) for number in range(17)]
        path = self.journal(events)
        self.assertGreater(path.stat().st_size, 16 * 1024 * 1024)
        grouped = []
        with mock.patch.object(worker, "process_session", side_effect=lambda session, values, logger: grouped.extend(values) or False), \
             mock.patch.object(session_tracker, "get_sessions_ready_for_write", return_value=[]), \
             mock.patch.object(session_tracker, "get_pending_synthesis_sessions", return_value=[]):
            self.assertEqual(worker.poll_once(logging.getLogger("public-byte-budget")), 0)
        self.assertLessEqual(sum(len(event.to_json().encode()) + 1 for event in grouped), 16 * 1024 * 1024)
        self.assertGreater(len(grouped), 0)
        self.assertLess(len(grouped), len(events))
        self.assertEqual(path.read_bytes().count(b"\n"), len(events), "Delivery never removes evidence")

    def test_bounded_poll_replays_until_capture_and_later_journal_makes_progress(self):
        first = [self.event(number, size=1024 * 1024) for number in range(4)]
        last = self.event("later", size=1024 * 1024)
        left = self.journal(first, "2026-10-21")
        right = self.journal([last], "2026-10-22")
        before = {path: path.read_bytes() for path in (left, right)}
        selected = list(queue_manager.read_pending_events(max_bytes=4 * 1024 * 1024))
        self.assertEqual([event.event_id for event in selected], [event.event_id for event in first[:3]])
        self.assertEqual(list(queue_manager.read_pending_events(max_bytes=4 * 1024 * 1024)), selected)
        self.assertFalse((config.STATE_DIR / "queue-offsets" / "2026-10-21.json").exists())
        self.capture(selected)
        remaining = list(queue_manager.read_pending_events(max_bytes=4 * 1024 * 1024))
        self.assertEqual([event.event_id for event in remaining], [first[3].event_id, last.event_id])
        self.capture(remaining)
        self.assertEqual(list(queue_manager.read_pending_events(max_bytes=4 * 1024 * 1024)), [])
        self.assertEqual({path: path.read_bytes() for path in before}, before)
        self.assertEqual(json.loads((config.STATE_DIR / "queue-offsets" / "2026-10-21.json").read_bytes())["offset"], len(before[left]))
        self.assertEqual(json.loads((config.STATE_DIR / "queue-offsets" / "2026-10-22.json").read_bytes())["offset"], len(before[right]))

    def test_captured_prefix_ack_persists_before_next_record_exceeds_budget(self):
        captured = self.event("captured", size=20)
        events = [captured, *[self.event(number, size=1024 * 1024) for number in range(4)]]
        path = self.journal(events)
        self.capture([captured])
        prefix_size = len((captured.to_json() + "\n").encode())
        selected = list(queue_manager.read_pending_events(max_bytes=4 * 1024 * 1024))
        self.assertEqual(len(selected), 3)
        cursor = json.loads((config.STATE_DIR / "queue-offsets" / "2026-10-23.json").read_bytes())
        self.assertEqual(cursor["offset"], prefix_size)
        self.assertEqual(path.read_bytes()[prefix_size:], b"".join((event.to_json() + "\n").encode() for event in events[1:]))
        self.assertEqual(list(queue_manager.read_pending_events(limit=1, max_bytes=4 * 1024 * 1024)), selected[:1])

    def test_invalid_limits_fail_before_any_queue_read_and_zero_count_delivers_none(self):
        with mock.patch.object(queue_manager, "read_queue_files", side_effect=AssertionError("Invalid limits must not read state")):
            for kwargs in ({"max_bytes": 4 * 1024 * 1024 - 1}, {"max_bytes": True}, {"max_bytes": "16777216"},
                           {"limit": -1}, {"limit": True}, {"limit": 1.5}):
                with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                    list(queue_manager.read_pending_events(**kwargs))
            self.assertEqual(list(queue_manager.read_pending_events(limit=0)), [])

    def test_utf8_and_escaped_records_charge_original_encoded_bytes_and_remain_whole(self):
        events = [self.event(number, size=256 * 1024, unicode=True) for number in range(4)]
        escaped = self.event("escaped", size=0)
        escaped.data["prompt"] = "\\u2603" * (128 * 1024)
        path = self.journal([*events, escaped], ensure_ascii=False)
        original = path.read_bytes()
        lines = original.splitlines(keepends=True)
        wire_sizes = {models.QueuedEvent.from_json(line).event_id: len(line) for line in lines}
        delivered = list(queue_manager.read_pending_events(max_bytes=4 * 1024 * 1024))
        self.assertEqual(delivered, events[:3])
        self.assertLessEqual(sum(wire_sizes[event.event_id] for event in delivered), 4 * 1024 * 1024)
        self.assertGreater(sum(wire_sizes[event.event_id] for event in events), 4 * 1024 * 1024)
        self.capture(delivered)
        rest = list(queue_manager.read_pending_events(max_bytes=4 * 1024 * 1024))
        self.assertEqual(rest, [events[3], escaped])
        self.assertEqual(rest[1].data["prompt"], "\\u2603" * (128 * 1024))
        self.assertEqual(path.read_bytes(), original)

    def test_record_over_existing_record_cap_is_retained_unacknowledged(self):
        oversized = self.event("too-large", size=4 * 1024 * 1024)
        path = self.journal([oversized], "2026-10-21")
        healthy = self.event("healthy", size=20)
        self.journal([healthy], "2026-10-22")
        before = path.read_bytes()
        self.assertGreater(len(before), queue_manager.MAX_EVENT_BYTES)
        self.assertEqual(list(queue_manager.read_pending_events()), [healthy])
        self.assertFalse((config.STATE_DIR / "queue-offsets" / "2026-10-21.json").exists())
        self.assertEqual(path.read_bytes(), before)

    def publication(self, session="imported", prompt="Public imported task"):
        path = self.root / f"{session}.jsonl"
        raw = b'{"type":"user","message":{"content":"Public capture evidence"}}\n'
        path.write_bytes(raw)
        return import_publication.publish("claude-code", session, str(path), path, raw, "", prompt,
                                          origin_sources=[str(path)])

    def test_exact_byte_boundary_splits_import_prompt_stop_without_consuming_stop(self):
        result = self.publication(prompt="Public " + "🌍" * 1024)
        record = import_publication._validate_batch_metadata(result.queue_path)
        prompt_bytes = len(import_publication._json(record["events"][0])) + 1
        budget = 4 * 1024 * 1024
        ordinary = self.event("before-import", size=0)
        ordinary.data["prompt"] = "x" * (budget - prompt_bytes - len((ordinary.to_json() + "\n").encode()))
        self.journal([ordinary])
        batch_before = result.queue_path.read_bytes()
        first = list(queue_manager.read_pending_events(max_bytes=budget))
        self.assertEqual([event.event for event in first], ["UserPromptSubmit", "UserPromptSubmit"])
        self.assertEqual(first[1].event_id, result.event_ids[0])
        self.assertEqual(len((ordinary.to_json() + "\n").encode()) + prompt_bytes, budget)
        self.assertEqual(list(queue_manager.read_pending_events(max_bytes=budget)), first)
        self.capture(first)
        second = list(queue_manager.read_pending_events(max_bytes=budget))
        self.assertEqual([event.event_id for event in second], [result.event_ids[1]])
        self.assertEqual(second[0].event, "Stop")
        self.capture(second)
        self.assertEqual(list(queue_manager.read_pending_events(max_bytes=budget)), [])
        self.assertEqual(result.queue_path.read_bytes(), batch_before)

    def test_corrupt_import_still_blocks_ordinary_hook_capture_and_cursor_ack(self):
        result = self.publication("blocked")
        self.capture(list(queue_manager.read_events(result.queue_path)))
        hook = self.event("captured-hook", session="blocked")
        self.capture([hook])
        healthy = self.event("healthy", size=1024 * 1024)
        path = self.journal([hook, healthy])
        before = path.read_bytes()
        result.snapshot_path.write_bytes(result.snapshot_path.read_bytes().replace(b"Public", b"Broken", 1))
        selected = list(queue_manager.read_pending_events(max_bytes=4 * 1024 * 1024))
        self.assertEqual(selected, [healthy])
        self.assertFalse((config.STATE_DIR / "queue-offsets" / "2026-10-23.json").exists())
        self.assertEqual(path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
