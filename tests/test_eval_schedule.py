"""Nightly evaluation catches up after login without restarting other jobs."""

import plistlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from claude_note import setup_recall


class NightlyScheduleTests(unittest.TestCase):
    def test_loaded_job_runs_once_and_retains_bounded_daily_schedule(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            share = home / ".local/share/claude-note"
            share.mkdir(parents=True)
            agents = home / "Library/LaunchAgents"
            agents.mkdir(parents=True)
            other_job = agents / "com.claude-note.recall-server.plist"
            other_job.write_bytes(b"unrelated authored job")
            exe = str(home / ".local/bin/claude-note")
            timeout = str(home / "tools/timeout")
            with mock.patch.multiple(setup_recall, HOME=home, SHARE=share), \
                    mock.patch.object(setup_recall.sys, "platform", "darwin"), \
                    mock.patch.object(setup_recall.shutil, "which", return_value=timeout), \
                    mock.patch.object(setup_recall.os, "getuid", return_value=503), \
                    mock.patch.object(setup_recall.subprocess, "run", return_value=
                                      subprocess.CompletedProcess([], 0)) as run:
                setup_recall.install_eval_agent(exe)

            plist = agents / "com.claude-note.eval-nightly.plist"
            job = plistlib.loads(plist.read_bytes())
            # A login after 04:30 must start one evaluation instead of waiting
            # until the following night; completion must not start a retry loop.
            self.assertIs(job.get("RunAtLoad"), True)
            self.assertNotIn("KeepAlive", job)
            self.assertEqual(job["StartCalendarInterval"], {"Hour": 4, "Minute": 30})
            self.assertEqual(job["ProgramArguments"],
                             [timeout, "-k", "60", "3600", exe, "eval", "nightly"])
            self.assertEqual(job["WorkingDirectory"], str(share))
            self.assertEqual(job["StandardOutPath"], str(share / "eval-nightly.log"))
            self.assertEqual(job["StandardErrorPath"], str(share / "eval-nightly.log"))
            self.assertEqual(other_job.read_bytes(), b"unrelated authored job")
            self.assertEqual(run.call_args_list, [
                mock.call(["launchctl", "bootout", "gui/503", str(plist)],
                          capture_output=True, timeout=30),
                mock.call(["launchctl", "bootstrap", "gui/503", str(plist)],
                          capture_output=True, timeout=30),
            ])
