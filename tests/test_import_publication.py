"""Synthetic publication/crash/replay contracts; no real imports or models."""

import concurrent.futures
import hashlib
import json
import logging
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from claude_note import (config, egress, import_publication as publication,
                         models, queue_manager, session_tracker, state_io,
                         transcript_reader, worker)


def rows(text, origins=None):
    row = {"type": "user", "uuid": "question", "message": {"role": "user", "content": text}}
    if origins is not None:
        row["derivation_sources"] = [str(path) for path in origins]
    return (json.dumps(row) + "\n").encode()


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for name, value in (("STATE_DIR", self.root / "state"), ("QUEUE_DIR", self.root / "queue")):
            patch = mock.patch.object(config, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        patch = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.root / "xdg")})
        patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.object(Path, "home", return_value=self.root / "home")
        patch.start()
        self.addCleanup(patch.stop)
        self.path = self.root / "native.jsonl"
        self.raw = rows("A synthetic question")
        self.path.write_bytes(self.raw)

    def publish(self, raw=None, **kwargs):
        return publication.publish("claude-code", "session1", "native-log", self.path,
                                   self.raw if raw is None else raw, "", "A synthetic question", **kwargs)

    def events(self, result):
        return list(queue_manager.read_events(result.queue_path))

    def capture(self, events):
        state = session_tracker.update_session_from_events("session1", events)
        session_tracker.save_session_state(state)
        return state

    def test_atomic_pair_replay_retains_first_bytes_and_timestamp(self):
        first = self.publish()
        original = first.queue_path.read_bytes()
        with mock.patch.object(publication, "datetime") as clock:
            clock.utcnow.return_value = datetime(2035, 1, 2)
            second = self.publish()
        self.assertEqual(second.status, "replayed")
        self.assertEqual(second.ordinal, first.ordinal)
        self.assertEqual(second.event_ids, first.event_ids)
        self.assertEqual(second.queue_path.read_bytes(), original)
        self.assertEqual(len(self.events(first)), 2)
        self.assertNotIn(b'2035', original)

    def test_queue_failure_reconciles_committed_pair(self):
        real = state_io.create_bytes
        def fail_queue(path, raw):
            if Path(path).parent == config.QUEUE_DIR:
                raise OSError("synthetic interrupted queue publication")
            return real(path, raw)
        with mock.patch.object(state_io, "create_bytes", side_effect=fail_queue):
            with self.assertRaises(OSError):
                self.publish()
        self.assertEqual(list(queue_manager.read_queue_files()), [])
        receipt = next(path for path in (config.STATE_DIR / "import-publications").glob("*.json") if len(path.name) == 69)
        canonical = json.loads(receipt.read_bytes())
        retry = self.publish()
        self.assertEqual(retry.status, "published")
        self.assertEqual(retry.event_ids, tuple(event["event_id"] for event in canonical["events"]))
        self.assertEqual(self.events(retry)[0].ts, canonical["events"][0]["ts"])

    def test_protection_failure_leaves_uncommitted_intent_invisible(self):
        with mock.patch.object(egress, "capture_restrictions", side_effect=ValueError("synthetic denied")):
            with self.assertRaises(ValueError):
                self.publish()
        intent = next(path for path in (config.STATE_DIR / "import-intents").glob("*.json") if len(path.name) == 69)
        pair = json.loads(intent.read_bytes())["events"]
        self.assertFalse((config.STATE_DIR / "import-publications").exists())
        self.assertEqual(list(queue_manager.read_queue_files()), [])
        with self.assertRaises(publication.PublicationError):
            publication.validate_event(models.QueuedEvent(**pair[0]))
        retry = self.publish()
        self.assertEqual(self.events(retry)[0].ts, pair[0]["ts"])

    def test_created_batch_is_complete_even_when_return_is_lost(self):
        real = state_io.create_bytes
        def after_queue(path, raw):
            real(path, raw)
            if Path(path).parent == config.QUEUE_DIR:
                raise OSError("synthetic caller lost completion")
        with mock.patch.object(state_io, "create_bytes", side_effect=after_queue):
            with self.assertRaises(OSError):
                self.publish()
        pending = list(queue_manager.read_pending_events())
        self.assertEqual([event.event for event in pending], ["UserPromptSubmit", "Stop"])
        retry = self.publish()
        self.assertEqual(tuple(event.event_id for event in pending), retry.event_ids)

    def test_existing_intent_missing_snapshot_is_not_reallocated(self):
        with mock.patch.object(egress, "capture_restrictions", side_effect=ValueError("synthetic failure")):
            with self.assertRaises(ValueError):
                self.publish()
        intent = next(path for path in (config.STATE_DIR / "import-intents").glob("*.json") if len(path.name) == 69)
        before = intent.read_bytes()
        record = json.loads(before)
        Path(record["snapshot_path"]).unlink()
        with self.assertRaisesRegex(publication.PublicationError, "missing evidence"):
            self.publish()
        self.assertEqual(intent.read_bytes(), before)
        self.assertEqual(list(queue_manager.read_queue_files()), [])

    def test_concurrent_same_revision_owns_one_pair_and_ordinal(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda _: self.publish(), range(6)))
        self.assertEqual(len({result.publication_id for result in results}), 1)
        self.assertEqual({result.ordinal for result in results}, {1})
        self.assertEqual(len(list(queue_manager.read_all_events())), 2)

    def test_normalized_origin_replay_keeps_first_snapshot_and_intersects_lineage(self):
        origin_a, origin_b = self.root / "export-a.json", self.root / "export-b.json"
        origin_a.write_text("Public synthetic origin")
        origin_b.write_text("---\nprivate: true\n---\nRestricted synthetic origin")
        revision = hashlib.sha256(b"normalized messages").hexdigest()
        first_raw = rows("A synthetic question", [origin_a])
        self.path.write_bytes(first_raw)
        first = self.publish(first_raw, origin_sources=[origin_a], logical_revision=revision)
        changed = rows("A synthetic question", [origin_b])
        self.path.write_bytes(changed)
        retry = self.publish(changed, origin_sources=[origin_b], logical_revision=revision)
        self.assertEqual(retry.snapshot_path, first.snapshot_path)
        self.assertEqual(retry.snapshot_path.read_bytes(), first_raw)
        self.assertEqual(retry.event_ids, first.event_ids)
        origin_b.write_text("Now appears public")
        with self.assertRaises(egress.EgressDenied):
            egress.prepare("Synthetic derived lesson", audience="model", sources=[str(retry.snapshot_path)], rules={"default": ["model"]})

    def test_logical_revision_cannot_hide_different_message(self):
        origin = self.root / "export.json"
        origin.write_text("Synthetic export")
        revision = hashlib.sha256(b"messages").hexdigest()
        self.publish(rows("A synthetic question", [origin]), origin_sources=[origin], logical_revision=revision)
        with self.assertRaises(publication.PublicationError):
            self.publish(rows("A DIFFERENT synthetic question", [origin]), origin_sources=[origin], logical_revision=revision)
        self.assertEqual(len(list(queue_manager.read_all_events())), 2)

    def test_normalized_origin_equivalence_preserves_scalar_types(self):
        for first, second in ((True, 1), (1, 1.0)):
            before = (json.dumps({"tool_value": first, "derivation_sources": ["/synthetic/a"]}) + "\n").encode()
            after = (json.dumps({"tool_value": second, "derivation_sources": ["/synthetic/b"]}) + "\n").encode()
            self.assertFalse(publication._equivalent(before, after))

    def test_protect_existing_does_not_create_or_republish_work(self):
        revision = hashlib.sha256(b"messages").hexdigest()
        self.assertFalse(publication.protect_existing("claude-code", "session1", "native-log", self.raw, logical_revision=revision))
        origin = self.root / "export.json"
        origin.write_text("Synthetic export")
        raw = rows("A synthetic question", [origin])
        result = self.publish(raw, logical_revision=revision, origin_sources=[origin])
        result.queue_path.unlink()
        self.assertTrue(publication.protect_existing("claude-code", "session1", "native-log", raw, [origin], logical_revision=revision))
        self.assertFalse(result.queue_path.exists())

    def test_imported_reader_uses_exact_snapshot_after_mutable_path_changes(self):
        result = self.publish()
        state = self.capture(self.events(result))
        self.path.write_bytes(rows("MUTABLE replacement must not be read"))
        content = transcript_reader.read_transcript_from_state(state)
        self.assertEqual(content.user_prompts, ["A synthetic question"])
        self.assertEqual(content.source_path, str(self.path))
        self.assertEqual(content.source_revision, result.captured_revision)
        self.assertIn(str(result.snapshot_path), content.context_sources)
        self.assertTrue(all(event.source == str(result.snapshot_path.resolve()) for event in content.source_events))
        self.path.unlink()
        self.assertEqual(transcript_reader.read_transcript_from_state(state).user_prompts, ["A synthetic question"])

    def test_modified_snapshot_fails_without_live_path_fallback(self):
        result = self.publish()
        state = self.capture(self.events(result))
        result.snapshot_path.write_bytes(rows("Corrupt captured bytes"))
        with self.assertRaises(publication.PublicationError):
            transcript_reader.read_transcript_from_state(state)

    def test_all_pending_revisions_verified_before_ack_and_newer_view_wins(self):
        first = self.publish()
        self.path.write_bytes(rows("A second question"))
        second = self.publish(self.path.read_bytes())
        all_events = self.events(second) + self.events(first)
        state = self.capture(all_events)
        self.assertEqual(state.import_binding["publication_id"], second.publication_id)
        self.assertEqual(set(state.import_publications), {first.publication_id, second.publication_id})
        self.assertEqual(len(state.processed_event_ids), 4)
        first.snapshot_path.write_bytes(rows("Corrupted old revision"))
        with self.assertRaises(publication.PublicationError):
            session_tracker.update_session_from_events("session1", all_events)
        self.assertEqual(len(session_tracker.load_session_state("session1").processed_event_ids), 4)

    def test_corrupt_older_batch_blocks_newer_same_session_before_delivery(self):
        first = self.publish()
        self.path.write_bytes(rows("A second question"))
        self.publish(self.path.read_bytes())
        first.snapshot_path.write_bytes(rows("Corrupted old revision"))
        self.assertEqual(list(queue_manager.read_pending_events()), [])
        self.assertFalse(session_tracker.get_state_file("session1").exists())

    def test_reader_limit_splits_delivery_but_capture_remains_explicit(self):
        result = self.publish()
        first = list(queue_manager.read_pending_events(limit=1))
        self.assertEqual(first[0].event, "UserPromptSubmit")
        self.assertEqual(list(queue_manager.read_pending_events(limit=1))[0].event_id, first[0].event_id)
        self.capture(first)
        stop = list(queue_manager.read_pending_events(limit=1))
        self.assertEqual(stop[0].event, "Stop")
        self.capture(stop)
        self.assertEqual(list(queue_manager.read_pending_events()), [])
        self.assertEqual(self.publish().status, "captured")

    def test_newer_hook_clears_binding_and_old_import_does_not_restore_it(self):
        result = self.publish()
        imported = self.events(result)
        state = self.capture(imported[:1])
        hook = models.QueuedEvent("new-hook", (datetime.utcnow() + timedelta(seconds=5)).isoformat() + "Z",
                                  "UserPromptSubmit", "session1", "", str(self.path), {"prompt": "New live hook"})
        state = self.capture([hook])
        self.assertEqual(state.import_binding, {})
        state = self.capture(imported[1:])
        self.assertEqual(state.import_binding, {})
        self.assertEqual(state.last_event_ts, hook.ts)
        self.assertEqual(state.filtered_event_ids[imported[1].event_id], "stale-source-binding")

    def test_older_hook_does_not_clear_import_binding(self):
        result = self.publish()
        state = self.capture(self.events(result))
        hook = models.QueuedEvent("old-hook", "2000-01-01T00:00:00Z", "Stop", "session1", "", str(self.root / "old.jsonl"), {})
        state = self.capture([hook])
        self.assertEqual(state.import_binding["publication_id"], result.publication_id)
        self.assertEqual(state.transcript_path, str(self.path))

    def test_higher_ordinal_under_clock_rollback_remains_new_work(self):
        first = self.publish()
        state = self.capture(self.events(first))
        previous_ts = state.last_event_ts
        state.last_write_ts = (datetime.utcnow() + timedelta(seconds=1)).isoformat() + "Z"
        session_tracker.save_session_state(state)
        self.path.write_bytes(rows("A newer revision with earlier wall clock"))
        with mock.patch.object(publication, "datetime") as clock:
            clock.utcnow.return_value = datetime(2000, 1, 1)
            newer = self.publish(self.path.read_bytes())
        state = self.capture(self.events(newer))
        self.assertEqual(state.import_binding["publication_id"], newer.publication_id)
        self.assertEqual(state.last_event_ts, previous_ts)
        self.assertIsNone(state.last_write_ts)

    def test_stop_delivered_after_prompt_note_still_requests_synthesis_once(self):
        result = self.publish()
        prompt, stop = self.events(result)
        state = self.capture([prompt])
        state.last_write_ts = (datetime.utcnow() + timedelta(seconds=1)).isoformat() + "Z"
        session_tracker.save_session_state(state)
        with mock.patch.object(worker, "run_synthesis", return_value=True) as synth, mock.patch.object(worker.note_writer, "update_session_note") as writer:
            self.assertFalse(worker.process_session("session1", [stop], logging.getLogger("publication-test")))
            self.assertEqual(synth.call_count, 1)
            writer.assert_not_called()
            self.assertFalse(worker.process_session("session1", [stop], logging.getLogger("publication-test")))
            self.assertEqual(synth.call_count, 1)

    def test_forged_binding_or_event_payload_is_rejected_before_ack(self):
        result = self.publish()
        event = self.events(result)[0]
        event.data["prompt"] = "Forged prompt"
        with self.assertRaises(publication.PublicationError):
            self.capture([event])
        self.assertFalse(session_tracker.get_state_file("session1").exists())

    def test_corrupt_complete_batch_never_uses_legacy_invalid_line_ack(self):
        result = self.publish()
        result.queue_path.write_bytes(result.queue_path.read_bytes().splitlines(keepends=True)[0] + b"invalid\n")
        self.assertEqual(list(queue_manager.read_pending_events()), [])
        self.assertFalse((config.STATE_DIR / "queue-offsets").exists())
        self.assertFalse(session_tracker.get_state_file("session1").exists())

    def test_acknowledged_batch_cleanup_retains_canonical_evidence_and_no_resurrection(self):
        with mock.patch.object(publication, "datetime") as clock:
            clock.utcnow.return_value = datetime(2000, 1, 1)
            result = self.publish()
        events = self.events(result)
        self.capture(events[:1])
        queue_manager.cleanup_old_queue_files()
        self.assertTrue(result.queue_path.exists())
        self.capture(events[1:])
        queue_manager.cleanup_old_queue_files()
        self.assertFalse(result.queue_path.exists())
        self.assertTrue(result.snapshot_path.exists())
        self.assertTrue(publication._receipt(result.publication_id).exists())
        self.assertEqual(self.publish().status, "captured")
        self.assertFalse(result.queue_path.exists())

    def test_worker_does_not_ack_corrupt_old_revision_when_newer_valid(self):
        first = self.publish()
        self.path.write_bytes(rows("A second question"))
        second = self.publish(self.path.read_bytes())
        events = self.events(first) + self.events(second)
        first.snapshot_path.write_bytes(rows("Corrupted old revision"))
        with mock.patch.object(worker.note_writer, "update_session_note") as writer, mock.patch.object(worker, "run_synthesis") as model:
            self.assertFalse(worker.process_session("session1", events, logging.getLogger("publication-test")))
        writer.assert_not_called()
        model.assert_not_called()
        self.assertFalse(session_tracker.get_state_file("session1").exists())

    def test_backward_session_defaults_and_incompatible_state_is_preserved(self):
        legacy = {"session_id": "session1", "first_event_ts": "2000-01-01T00:00:00Z", "last_event_ts": "2000-01-01T00:00:00Z"}
        state_io.atomic_json(session_tracker.get_state_file("session1"), legacy)
        self.assertEqual(session_tracker.load_session_state("session1").import_binding, {})
        legacy["unknown_future_field"] = True
        state_io.atomic_json(session_tracker.get_state_file("session1"), legacy)
        with self.assertRaises(ValueError):
            session_tracker.load_session_state("session1")
        self.assertTrue(session_tracker.get_state_file("session1").exists())

    def test_stop_ack_crash_keeps_durable_synthesis_request(self):
        result = self.publish()
        prompt, stop = self.events(result)
        state = self.capture([prompt])
        state.last_write_ts = (datetime.utcnow() + timedelta(seconds=1)).isoformat() + 'Z'
        session_tracker.save_session_state(state)
        actual_save = session_tracker.save_session_state
        class Crash(BaseException):
            pass
        def crash_after_capture(state):
            actual_save(state)
            raise Crash('Synthetic crash after durable capture, before action scheduling')
        with mock.patch.object(session_tracker, 'save_session_state', side_effect=crash_after_capture):
            with self.assertRaises(Crash):
                worker.process_session('session1', [stop], logging.getLogger('review-probe'))
        saved = session_tracker.load_session_state('session1')
        self.assertIn(stop.event_id, saved.processed_event_ids)
        self.assertEqual(list(queue_manager.read_pending_events()), [])
        self.assertTrue(saved.synthesis_pending, 'Captured Stop must retain its requested action across crash')

    def test_malformed_older_receipt_cannot_hide_behind_valid_newer_batch(self):
        first = self.publish()
        self.path.write_bytes(rows('Newer synthetic question'))
        self.publish(self.path.read_bytes())
        publication._receipt(first.publication_id).write_text('{bad-json}')
        self.assertEqual(list(queue_manager.read_pending_events()), [],
                         'Older unverified pending import must block same-session newer evidence')

    def test_incompatible_session_state_does_not_starve_independent_batch(self):
        self.publish()
        other_path = self.root / 'other.jsonl'
        other_raw = rows('Independent synthetic question')
        other_path.write_bytes(other_raw)
        publication.publish('claude-code', 'session2', 'other-log', other_path,
                            other_raw, '', 'Independent synthetic question')
        state_io.atomic_json(session_tracker.get_state_file('session1'),
                             {'session_id': 'session1', 'unsupported_future_field': True})
        pending = list(queue_manager.read_pending_events())
        self.assertEqual({event.session_id for event in pending}, {'session2'})

    def test_corrupt_state_is_isolated_through_worker_and_drain_readiness_scans(self):
        from claude_note import drain
        self.publish()
        other_path = self.root / "other.jsonl"
        other_raw = rows("Independent synthetic question")
        other_path.write_bytes(other_raw)
        publication.publish("claude-code", "session2", "other-log", other_path,
                            other_raw, "", "Independent synthetic question")
        invalid = {"session_id": "session1", "unsupported_future_field": True}
        state_io.atomic_json(session_tracker.get_state_file("session1"), invalid)
        before = session_tracker.get_state_file("session1").read_bytes()
        with mock.patch.object(worker.note_writer, "update_session_note", return_value=self.root / "raw.md") as writer, \
                mock.patch.object(worker.open_questions, "promote_session_questions", return_value=0), \
                mock.patch.object(worker, "run_synthesis", return_value=True):
            self.assertEqual(worker.poll_once(logging.getLogger("publication-test")), 1)
            self.assertEqual(writer.call_args.args[0].session_id, "session2")
        session_tracker.get_state_file("session2").unlink()
        with mock.patch.object(drain.note_writer, "update_session_note", return_value=self.root / "raw.md") as writer, \
                mock.patch.object(drain.open_questions, "promote_session_questions", return_value=0), \
                mock.patch.object(drain, "run_synthesis_for_drain", return_value=True):
            self.assertEqual(drain.drain_all(), (1, 1))
            self.assertEqual(writer.call_args.args[0].session_id, "session2")
        self.assertEqual(session_tracker.get_state_file("session1").read_bytes(), before)

    def test_malformed_import_reference_in_hook_journal_does_not_advance_cursor(self):
        result = self.publish()
        event = json.loads(self.events(result)[0].to_json())
        result.queue_path.unlink()
        event["ts"] = "invalid-time"
        # Escaped JSON property names still declare an imported reference.
        raw = json.dumps(event).replace('"_import_publication"', '"\\u005fimport_publication"').encode() + b"\n"
        path = queue_manager.get_queue_file()
        state_io.append_line(path, raw)
        self.assertEqual(list(queue_manager.read_pending_events()), [])
        self.assertFalse((config.STATE_DIR / "queue-offsets" / (path.stem + ".json")).exists())


if __name__ == "__main__":
    unittest.main()
