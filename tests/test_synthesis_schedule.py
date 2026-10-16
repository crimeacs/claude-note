import os
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

os.environ.setdefault("CLAUDE_NOTE_VAULT_ROOT", tempfile.mkdtemp())

from claude_note import config, health, models, session_tracker, synthesis_schedule, synthesizer


class SynthesisScheduleTests(unittest.TestCase):
    def test_timezone_and_automatic_expiry(self):
        reset = datetime(2026, 10, 17, 5, tzinfo=timezone.utc)
        setting = "2026-10-17T01:00:00-04:00"
        self.assertEqual(synthesis_schedule.remaining(setting, reset - timedelta(seconds=1)), 1)
        self.assertEqual(synthesis_schedule.remaining(setting, reset), 0)
        self.assertFalse(synthesis_schedule.paused(setting, reset + timedelta(seconds=1)))
        self.assertEqual(synthesis_schedule.remaining(reset, reset - timedelta(seconds=1)), 1)

    def test_malformed_or_naive_timestamp_fails_closed(self):
        for value in ("tomorrow", "2026-10-17T01:00:00", 123, 0, False):
            with self.subTest(value=value):
                self.assertTrue(synthesis_schedule.paused(value))
                with self.assertRaises(ValueError):
                    synthesis_schedule.remaining(value)
        self.assertFalse(synthesis_schedule.paused(""))

    def test_cooldown_preserves_pending_retry_and_prevents_forced_model_call(self):
        state = models.SessionState(session_id="cooldown", first_event_ts="2026-10-16T00:00:00Z",
                                    last_event_ts="2026-10-16T00:00:00Z")
        state.synthesis_pending = True
        state.synthesis_attempts = 3
        resume = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        callback = mock.Mock()
        with mock.patch.object(config, "SYNTH_RESUME_AFTER", resume), \
                mock.patch.object(session_tracker, "save_session_state") as save:
            self.assertFalse(session_tracker.synthesis_retry_due(state))
            self.assertIsNone(session_tracker.run_pending_synthesis(state, callback, force=True))
            callback.assert_not_called()
            save.assert_not_called()
            self.assertEqual(state.synthesis_attempts, 3)
            self.assertTrue(state.synthesis_pending)

    def test_direct_synthesis_obeys_cooldown_before_prompt_or_process(self):
        resume = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        with mock.patch.object(config, "SYNTH_RESUME_AFTER", resume), \
                mock.patch.object(synthesizer, "build_synthesis_prompt") as prompt, \
                mock.patch.object(synthesizer.subprocess, "run") as process:
            with self.assertRaisesRegex(RuntimeError, "Synthesis deferred"):
                synthesizer.synthesize_session(None, None)
            prompt.assert_not_called()
            process.assert_not_called()

    def test_health_serializes_toml_datetime_and_honors_environment_override(self):
        resume = datetime.now(timezone.utc) + timedelta(days=1)
        with tempfile.TemporaryDirectory() as temporary, \
                mock.patch.object(health, "LOGS", Path(temporary)), \
                mock.patch.object(health, "_config", return_value=(Path(temporary) / "config.toml",
                    {"synthesis": {"mode": "route", "resume_after": resume}})), \
                mock.patch.object(health, "_notes", return_value={}), \
                mock.patch.object(health, "_synthesis", return_value={}), \
                mock.patch.object(health, "_launchd_loaded", return_value=False), \
                mock.patch.object(health, "_which", return_value="/tool"), \
                mock.patch.dict(os.environ, {}, clear=True):
            report = health.report()
            json.dumps(report)
            self.assertTrue(any("provider cooldown" in problem for problem in report['problems']))
            with mock.patch.dict(os.environ, {"CLAUDE_NOTE_RESUME_AFTER": "2000-01-01T00:00:00Z"}):
                report = health.report()
                self.assertFalse(any("provider cooldown" in problem for problem in report['problems']))
                self.assertEqual(report['synthesis_resume_after'], "2000-01-01T00:00:00Z")
