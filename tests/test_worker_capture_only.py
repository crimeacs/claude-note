"""Capture-only worker uses authored public events and temporary local notes."""

import logging
import os
import tempfile
import unittest
from contextlib import ExitStack
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from claude_note import cli, config, models, note_writer, session_tracker, worker


class CaptureOnlyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="cn-capture-public-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.logger = logging.getLogger("test-worker-capture-only")
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        for name, value in (("VAULT_ROOT", self.root), ("CLAUDE_NOTE_DIR", self.root/".claude-note"),
                            ("STATE_DIR", self.root/"state"), ("QUEUE_DIR", self.root/"queue"),
                            ("SYNTH_MODE", "route"), ("SYNTH_RESUME_AFTER", ""), ("DEBOUNCE_SECONDS", 0)):
            self.stack.enter_context(mock.patch.object(config, name, value))
        self.stack.enter_context(mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.root/"config")}))
        self.stack.enter_context(mock.patch.object(Path,"home",return_value=self.root/"home"))
        self.stack.enter_context(mock.patch("claude_note.provenance.author_email",return_value="public@example.invalid"))
        self.stack.enter_context(mock.patch.object(worker,"_capture_only",False))
        self.transcript = self.root/"public.jsonl"
        self.transcript.write_text('{"type":"user","message":{"content":"Public retry policy question"}}\n')

    def event(self, kind):
        return models.QueuedEvent.from_hook_input({"session_id":"capture-public", "hook_event_name":kind,
            "cwd":str(self.root), "transcript_path":str(self.transcript),
            "prompt":"Which public retry policy preserves idempotency across repeated attempts?"})

    def no_models(self):
        stack=ExitStack(); forbidden=[]
        for owner,name in ((session_tracker,"run_pending_synthesis"),(worker.vault_indexer,"get_index"),
                           (worker.synthesizer,"synthesize_from_state"),(worker.open_questions,"promote_session_questions")):
            forbidden.append(stack.enter_context(mock.patch.object(owner,name,side_effect=AssertionError("Model/index path forbidden"))))
        forbidden.append(stack.enter_context(mock.patch("subprocess.run",side_effect=AssertionError("No model/QMD command"))))
        return stack,forbidden

    def test_capture_writes_real_note_and_retains_new_stop_without_model_or_index(self):
        guards,forbidden=self.no_models()
        with guards,mock.patch.object(worker,"_capture_only",True):
            self.assertTrue(worker.process_session("capture-public",[self.event("UserPromptSubmit"),self.event("Stop")],self.logger))
            state=session_tracker.load_session_state("capture-public")
            self.assertTrue(state.synthesis_pending)
            self.assertEqual(state.synthesis_attempts,0)
            self.assertIsNone(state.last_synthesis_attempt_ts)
            self.assertFalse(state.synthesis_parked)
            path=note_writer.get_note_path(state)
            self.assertTrue(path.is_file())
            self.assertIn("retry policy",path.read_text())
            self.assertEqual(len(state.processed_event_ids),2)
            self.assertFalse(worker.run_synthesis(state,self.logger))
        for call in forbidden:call.assert_not_called()
        self.assertEqual(config.SYNTH_MODE,"route")

    def test_existing_attempt_backoff_park_and_pending_survive_new_stop_and_idle_poll(self):
        guards,forbidden=self.no_models()
        with guards,mock.patch.object(worker,"_capture_only",True):
            self.assertTrue(worker.process_session("capture-public",[self.event("UserPromptSubmit")],self.logger))
            state=session_tracker.load_session_state("capture-public")
            state.synthesis_pending=True;state.synthesis_attempts=7
            state.last_synthesis_attempt_ts="2026-01-02T03:04:05Z"
            state.synthesis_parked=True;state.synthesis_park_reason="retained retry reason"
            # Split prompt/Stop delivery can reach the already-written branch.
            state.last_write_ts=(datetime.utcnow()+timedelta(seconds=60)).isoformat()+"Z"
            session_tracker.save_session_state(state)
            expected=(True,7,state.last_synthesis_attempt_ts,True,"retained retry reason")
            self.assertFalse(worker.process_session("capture-public",[self.event("Stop")],self.logger))
            after=session_tracker.load_session_state("capture-public")
            self.assertEqual((after.synthesis_pending,after.synthesis_attempts,after.last_synthesis_attempt_ts,
                              after.synthesis_parked,after.synthesis_park_reason),expected)
            filename=config.STATE_DIR/"capture-public.json";before=filename.read_bytes()
            with mock.patch.object(session_tracker,"get_pending_synthesis_sessions",side_effect=AssertionError("No pending-only scan")), \
                 mock.patch.object(session_tracker,"get_sessions_ready_for_write",return_value=[]):
                self.assertEqual(worker.poll_once(self.logger),0)
            self.assertEqual(filename.read_bytes(),before)
        for call in forbidden:call.assert_not_called()

    def test_capture_only_does_not_clear_pending_even_if_authored_mode_is_log(self):
        state=models.SessionState("public","2026-01-01T00:00:00Z","2026-01-01T00:00:00Z",transcript_path=str(self.transcript))
        state.synthesis_pending=True;state.synthesis_attempts=4
        with mock.patch.object(worker,"_capture_only",True),mock.patch.object(config,"SYNTH_MODE","log"), \
             mock.patch.object(session_tracker,"run_pending_synthesis") as dispatch:
            worker._schedule_synthesis(state)
            self.assertIsNone(worker._run_pending_synthesis(state,self.logger))
            self.assertTrue(state.synthesis_pending);self.assertEqual(state.synthesis_attempts,4)
            dispatch.assert_not_called()

    def test_normal_mode_still_dispatches_and_completes_synthesis(self):
        with mock.patch.object(worker,"run_synthesis",return_value=True) as synth, \
             mock.patch.object(worker.open_questions,"promote_session_questions",return_value=0) as questions:
            self.assertTrue(worker.process_session("capture-public",[self.event("UserPromptSubmit"),self.event("Stop")],self.logger))
        synth.assert_called_once();questions.assert_called_once()
        state=session_tracker.load_session_state("capture-public")
        self.assertFalse(state.synthesis_pending);self.assertEqual(state.synthesis_attempts,1)

    def test_process_only_flag_is_visible_during_worker_and_restored_on_return_or_error(self):
        seen=[]
        def body(**kwargs):
            seen.append(worker._capture_only);return 3
        with mock.patch.object(worker,"_run_worker",side_effect=body):
            self.assertEqual(worker.run_worker(capture_only=True),3)
            self.assertFalse(worker._capture_only)
            self.assertEqual(worker.run_worker(),3)
        self.assertEqual(seen,[True,False]);self.assertEqual(config.SYNTH_MODE,"route")
        with mock.patch.object(worker,"_run_worker",side_effect=RuntimeError("authored failure")),self.assertRaises(RuntimeError):
            worker.run_worker(capture_only=True)
        self.assertFalse(worker._capture_only)

    def test_both_cli_entrypoints_forward_flag_and_defaults(self):
        for entry,args,expected in ((cli.main,["claude-note","worker","--capture-only","-f","-v"],
                                     {"foreground":True,"verbose":True,"capture_only":True}),
                                    (worker.main,["worker","--capture-only"],
                                     {"foreground":False,"verbose":False,"capture_only":True}),
                                    (cli.main,["claude-note","worker"],
                                     {"foreground":False,"verbose":False,"capture_only":False})):
            with mock.patch.object(worker,"run_worker",return_value=9) as run,mock.patch("sys.argv",args):
                self.assertEqual(entry(),9)
                run.assert_called_once_with(**expected)

    def test_slow_live_sweeper_keeps_ownership_warns_once_and_never_overlaps(self):
        threads=[];polls=[]
        class FakeThread:
            def __init__(self,**kwargs):self.started=0;threads.append(self)
            def start(self):self.started+=1
            def is_alive(self):return True
        logger=mock.Mock()
        def poll(_):
            polls.append(worker._capture_only)
            if len(polls)==4:worker._shutdown=True
            return 0
        with mock.patch.object(worker,"_shutdown",False),mock.patch.object(worker,"setup_logging",return_value=logger), \
             mock.patch.object(worker,"use_private_cwd"),mock.patch.object(worker.version_checker,"check_for_update") as version, \
             mock.patch.object(worker.signal,"signal"),mock.patch.object(worker.threading,"Thread",FakeThread), \
             mock.patch.object(worker.time,"time",side_effect=[1000,1701,1802,1903]),mock.patch.object(worker.time,"sleep"), \
             mock.patch.object(worker.importers,"SWEEP_INTERVAL",100),mock.patch.object(worker.importers,"sweep") as importer, \
             mock.patch.object(worker,"poll_once",side_effect=poll),mock.patch.object(worker.queue_manager,"cleanup_old_queue_files"):
            self.assertEqual(worker.run_worker(capture_only=True),0)
        self.assertEqual(len(threads),1);self.assertEqual(threads[0].started,1)
        self.assertEqual(polls,[True]*4);self.assertEqual(logger.warning.call_count,1)
        self.assertIn("retaining ownership",logger.warning.call_args[0][0])
        importer.assert_not_called();version.assert_not_called()
        self.assertFalse(worker._capture_only)


if __name__ == "__main__":
    unittest.main()
