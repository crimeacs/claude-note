import io
import logging
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

os.environ.setdefault("CLAUDE_NOTE_VAULT_ROOT", tempfile.mkdtemp())

from claude_note import config, drain, models, note_writer, queue_manager, session_tracker, worker  # noqa: E402


class SynthesisRetryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name, value in (("VAULT_ROOT", self.root), ("STATE_DIR", self.root / "state"),
                            ("QUEUE_DIR", self.root / "queue"), ("SYNTH_MODE", "route")):
            patcher = mock.patch.object(config, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(worker.open_questions, "promote_session_questions", return_value=0)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.logger = logging.getLogger("test-worker-retries")
        self.events = [models.QueuedEvent.from_hook_input({
            "session_id": "session1", "hook_event_name": kind,
            "cwd": "/repo", "transcript_path": "/repo/transcript.jsonl", "prompt": "Fix the check",
        }) for kind in ("UserPromptSubmit", "Stop")]

    def test_worker_failure_retries_after_backoff_without_rewriting_note(self):
        with mock.patch.object(worker, "run_synthesis", side_effect=[False, True]) as synthesize, \
                mock.patch.object(note_writer, "update_session_note", wraps=note_writer.update_session_note) as write:
            self.assertTrue(worker.process_session("session1", self.events, self.logger))
            state = session_tracker.load_session_state("session1")
            self.assertTrue(state.synthesis_pending)
            self.assertEqual(state.synthesis_attempts, 1)
            self.assertIsNotNone(state.last_write_ts)
            self.assertFalse(worker.process_session("session1", self.events, self.logger))
            self.assertEqual(synthesize.call_count, 1)
            state.last_synthesis_attempt_ts = (datetime.utcnow() - timedelta(seconds=61)).isoformat() + "Z"
            session_tracker.save_session_state(state)
            # Old queue files may have expired: the pending state still retries.
            self.assertEqual(worker.poll_once(self.logger), 0)
            self.assertEqual(synthesize.call_count, 2)
            self.assertEqual(write.call_count, 1)
            self.assertFalse(session_tracker.load_session_state("session1").synthesis_pending)
            self.assertEqual(len(list(self.root.rglob("claude-session-*.md"))), 1)

    def test_drain_completion_is_idempotent_and_failed_synthesis_can_be_forced(self):
        for event in self.events:
            queue_manager.enqueue_event(event)
        with mock.patch.object(drain, "run_synthesis_for_drain", side_effect=[False, True]) as synthesize, \
                mock.patch.object(note_writer, "update_session_note", wraps=note_writer.update_session_note) as write, \
                redirect_stdout(io.StringIO()):
            self.assertEqual(drain.drain_all(), (1, 1))
            self.assertEqual(drain.drain_all(), (1, 0))
            self.assertEqual(drain.drain_all(), (0, 0))
        self.assertEqual(write.call_count, 1)
        self.assertEqual(synthesize.call_count, 2)
        self.assertIsNotNone(session_tracker.load_session_state("session1").last_write_ts)

    def test_old_stop_does_not_bypass_debounce_for_a_new_prompt(self):
        with mock.patch.object(worker, "run_synthesis", return_value=True) as synthesize, \
                mock.patch.object(note_writer, "update_session_note", wraps=note_writer.update_session_note) as write:
            self.assertTrue(worker.process_session("session1", self.events, self.logger))
            new = models.QueuedEvent.from_hook_input({"session_id": "session1", "hook_event_name": "UserPromptSubmit",
                                                     "prompt": "Next turn", "transcript_path": "/repo/transcript.jsonl"})
            self.assertFalse(worker.process_session("session1", [*self.events, new], self.logger))
            self.assertEqual(write.call_count, 1)
            self.assertEqual(synthesize.call_count, 1)

    def test_empty_extraction_completes_without_endless_retries(self):
        empty = mock.Mock()
        empty.is_empty.return_value = True
        state = models.SessionState("s", "2026-10-10T00:00:00Z", "2026-10-10T00:00:00Z",
                                    transcript_path="/repo/transcript.jsonl")
        with mock.patch.object(worker.vault_indexer, "get_index"), \
                mock.patch.object(worker.synthesizer, "synthesize_from_state", return_value=empty):
            self.assertTrue(worker.run_synthesis(state, self.logger))
            self.assertTrue(drain.run_synthesis_for_drain(state))

    def test_backoff_is_capped_and_older_state_remains_readable(self):
        state = models.SessionState.from_json('{"session_id":"old","first_event_ts":"2026-10-10T00:00:00Z",'
                                               '"last_event_ts":"2026-10-10T00:00:00Z"}')
        self.assertFalse(state.synthesis_pending)
        state.synthesis_pending = True
        state.synthesis_attempts = 100
        state.last_synthesis_attempt_ts = (datetime.utcnow() - timedelta(seconds=3601)).isoformat() + "Z"
        self.assertTrue(session_tracker.synthesis_retry_due(state))


if __name__ == "__main__":
    unittest.main()
