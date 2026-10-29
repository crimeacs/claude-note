"""Public child supervision contracts; sampled thresholds are not OS heap caps."""

import logging
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from claude_note import capture_sweep as sweep


class BoundedSweepTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="public-sweep-unit-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.enterContext(mock.patch.object(sweep.shutil, "disk_usage", return_value=mock.Mock(free=8 * 1024 ** 3)))
        self.clock = self.enterContext(mock.patch.object(sweep.time, "monotonic", return_value=0))
        self.spawn = self.enterContext(mock.patch.object(sweep.subprocess, "Popen"))
        self.child = self.spawn.return_value
        self.child.poll.return_value = None
        self.child.pid = 12345
        self.rss = self.enterContext(mock.patch.object(sweep, "rss_bytes", return_value=20 * 1024 ** 2))
        self.job = sweep.Sweep(logging.getLogger("bounded-sweep-test"),
                               status_path=self.root / "import-supervisor.json")

    def test_one_child_until_reaped_then_interval_before_retry(self):
        self.job.tick()
        self.clock.return_value = 1
        self.job.tick()
        self.spawn.assert_called_once()
        self.assertTrue(self.spawn.call_args.kwargs["start_new_session"])
        self.child.poll.return_value = 0
        self.job.tick()
        self.assertIsNone(self.job.child)
        self.clock.return_value = 2
        self.job.tick()
        self.spawn.assert_called_once()
        self.clock.return_value = sweep.importers.SWEEP_INTERVAL + 1
        self.job.tick()
        self.assertEqual(self.spawn.call_count, 2)

    def test_deadline_rss_disk_and_unavailable_measurement_reap_exact_child(self):
        for reason in ("deadline", "rss", "disk", "measurement"):
            with self.subTest(reason=reason):
                self.setUp()
                self.job.tick()
                if reason == "deadline":
                    self.clock.return_value = sweep.MAX_SECONDS
                elif reason == "rss":
                    self.rss.return_value = sweep.MAX_RSS_BYTES + 1
                elif reason == "disk":
                    sweep.shutil.disk_usage.return_value.free = sweep.MIN_FREE_BYTES - 1
                else:
                    self.rss.side_effect = OSError("measurement failed")
                self.job.tick()
                self.child.terminate.assert_called_once()
                self.child.wait.assert_called_once_with(timeout=sweep.STOP_GRACE_SECONDS)
                self.assertIsNone(self.job.child)

    def test_no_start_below_reserve_and_no_busy_retry(self):
        sweep.shutil.disk_usage.return_value.free = sweep.MIN_FREE_BYTES - 1
        self.job.tick()
        sweep.shutil.disk_usage.return_value.free = 8 * 1024 ** 3
        self.clock.return_value = 1
        self.job.tick()
        self.spawn.assert_not_called()

    def test_stubborn_owned_child_is_killed_and_reaped_before_ownership_clears(self):
        self.job.tick()
        self.child.wait.side_effect = [subprocess.TimeoutExpired("public-child", 2), 0]
        self.job.close()
        self.child.terminate.assert_called_once()
        self.child.kill.assert_called_once()
        self.assertEqual(self.child.wait.call_count, 2)
        self.assertIsNone(self.job.child)

    def test_supervisor_shutdown_joins_and_closes_after_failed_tick(self):
        with mock.patch.object(self.job, "tick", side_effect=OSError("public failure")), \
             mock.patch.object(self.job, "close") as close:
            self.job.start()
            self.job.shutdown()
        close.assert_called_once()
        self.assertFalse(self.job.thread.is_alive())


if __name__ == "__main__":
    unittest.main()
