"""Public supervisor lifecycle/telemetry contracts; no private sources or models."""

import copy
import hashlib
import json
import logging
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from claude_note import capture_sweep_status as status


def value(*, state="started", reason="thread_start", attempt=None, now=100):
    return {"owner_state": state, "owner_reason": reason, "owner_updated_at": now,
            "attempt": attempt, "counters": {name: 0 for name in status.COUNTERS}}


def attempt(*, state="running", reason="attempt_start", now=100):
    return {"number": 1, "state": state, "reason": reason, "started_at": 100,
            "updated_at": now, "elapsed_ms": 0, "child_started": True,
            "child_reaped": False, "exit_code": None, "cleanup": "not_requested"}


class StatusFileTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="public-sweep-status-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.path = self.root / "import-supervisor.json"
        self.writer = status.Writer(self.path)

    def raw_record(self, record):
        self.path.write_bytes(status._json({"record": record,
            "checksum": hashlib.sha256(status._json(record)).hexdigest()}))

    def record(self):
        self.writer.write(value(attempt=attempt()))
        return status.read(self.path)

    def test_roundtrip_has_only_bounded_safe_fields_and_no_positive_liveness(self):
        record = self.record()
        self.assertLess(self.path.stat().st_size, status.MAX_BYTES)
        out = status.snapshot(self.path, now=100)
        self.assertTrue(out["available"])
        self.assertTrue(out["observation_only"])
        self.assertEqual(out["counter_scope"], "reported_owner_instance")
        self.assertEqual(out["latest_attempt"], record["attempt"])
        self.assertNotIn("live", out)
        self.assertNotIn("pid", out)

    def test_missing_metadata_is_neutral_for_older_installations(self):
        out = status.snapshot(self.path, now=100)
        self.assertFalse(out["available"])
        self.assertEqual(out["state"], "not_recorded")
        self.assertEqual(out["problems"], [])

    def test_unknown_file_and_ordinary_edits_are_preserved(self):
        original = b"Public human-authored diagnostic; preserve these bytes.\n"
        self.path.write_bytes(original)
        with self.assertRaises(ValueError):
            self.writer.write(value())
        self.assertEqual(self.path.read_bytes(), original)
        self.path.unlink()  # This test's authored temporary file only.
        record = self.record()
        envelope = json.loads(self.path.read_bytes())
        envelope["record"]["counters"]["failed"] = 9
        self.path.write_text(json.dumps(envelope))
        edited = self.path.read_bytes()
        with self.assertRaises(ValueError):
            self.writer.write(value())
        self.assertEqual(self.path.read_bytes(), edited)
        self.assertNotEqual(record["counters"]["failed"], 9)

    def test_owner_replacement_prevents_old_writer_from_overwriting(self):
        self.record()
        replacement = status.Writer(self.path)
        replacement.write(value(state="starting", reason="thread_start", now=101))
        newest = self.path.read_bytes()
        self.assertTrue(status.read(self.path)["owner_replaced"])
        with self.assertRaises(ValueError):
            self.writer.write(value(now=102))
        self.assertEqual(self.path.read_bytes(), newest)

    def test_uncertain_replace_cannot_reclaim_replacement_owner(self):
        real = self.writer._replace_snapshot
        def replace_then_fail(*args, **kwargs):
            real(*args, **kwargs)
            raise OSError("Public simulated final fsync failure")
        with mock.patch.object(self.writer, "_replace_snapshot", side_effect=replace_then_fail), self.assertRaises(OSError):
            self.writer.write(value())
        replacement = status.Writer(self.path)
        replacement.write(value(now=101))
        newest = self.path.read_bytes()
        with self.assertRaises(ValueError):
            self.writer.write(value(now=102))
        self.assertEqual(self.path.read_bytes(), newest)

    def test_exact_own_uncertain_replace_can_resume_without_reset(self):
        real = self.writer._replace_snapshot
        def replace_then_fail(*args, **kwargs):
            real(*args, **kwargs)
            raise OSError("Public simulated final fsync failure")
        with mock.patch.object(self.writer, "_replace_snapshot", side_effect=replace_then_fail), self.assertRaises(OSError):
            self.writer.write(value())
        self.writer.write(value(now=101))
        record = status.read(self.path)
        self.assertEqual(record["sequence"], 2)
        self.assertFalse(record["owner_replaced"])

    def test_lock_inode_replaced_after_flock_denies_status_write(self):
        self.record()
        before = self.path.read_bytes()
        real = status.fcntl.flock
        def replace_lock(descriptor, flags):
            real(descriptor, flags)
            lock = self.path.with_name(self.path.name + ".lock")
            lock.unlink()
            lock.write_bytes(b"Public replacement lock\n")
        with mock.patch.object(status.fcntl, "flock", side_effect=replace_lock), self.assertRaises(ValueError):
            self.writer.write(value(now=101))
        self.assertEqual(self.path.read_bytes(), before)

    def test_fixed_pending_crash_residue_is_preserved_without_more_files(self):
        pending = self.path.with_name(self.path.name + ".pending")
        residue = b"Public unknown interrupted pending publication; preserve bytes.\n"
        pending.write_bytes(residue)
        for number in range(3):
            with self.assertRaises(FileExistsError):
                self.writer.write(value(now=100 + number))
        self.assertEqual(pending.read_bytes(), residue)
        self.assertEqual({p.name for p in self.root.iterdir()},
                         {pending.name, self.path.name + ".lock"})
        out = status.snapshot(self.path, now=103)
        self.assertEqual(out["state"], "publication_pending")
        self.assertFalse(out["available"])
        self.assertNotIn("preserve bytes", json.dumps(out))

    def test_failed_write_cleans_only_its_own_exact_pending_file(self):
        pending = self.path.with_name(self.path.name + ".pending")
        with mock.patch.object(status.os, "write", side_effect=OSError("public write failure")):
            with self.assertRaises(OSError):
                self.writer.write(value())
        self.assertFalse(pending.exists())
        self.assertFalse(self.path.exists())
        self.writer.write(value(now=101))
        self.assertEqual(status.read(self.path)["sequence"], 1)
        self.assertFalse(pending.exists())

    def test_failed_write_does_not_delete_replaced_human_pending_path(self):
        pending = self.path.with_name(self.path.name + ".pending")
        replacement = b"Public human replacement of pending path.\n"
        def replace_then_fail(descriptor):
            pending.unlink()
            pending.write_bytes(replacement)
            raise OSError("public simulated failure after replacement")
        with mock.patch.object(status.os, "fsync", side_effect=replace_then_fail):
            with self.assertRaises(OSError):
                self.writer.write(value())
        self.assertEqual(pending.read_bytes(), replacement)
        self.assertFalse(self.path.exists())
        self.assertEqual(status.snapshot(self.path)["state"], "publication_pending")

    def test_symlink_hardlink_fifo_and_oversize_are_unavailable(self):
        target = self.root / "public-original"
        target.write_bytes(b"Public fixture original\n")
        self.path.symlink_to(target)
        self.assertEqual(status.snapshot(self.path)["state"], "unavailable")
        self.path.unlink()
        os.link(target, self.path)
        self.assertEqual(status.snapshot(self.path)["state"], "unavailable")
        self.path.unlink()
        os.mkfifo(self.path)
        self.assertEqual(status.snapshot(self.path)["state"], "unavailable")
        self.path.unlink()
        self.path.write_bytes(b"x" * (status.MAX_BYTES + 1))
        self.assertEqual(status.snapshot(self.path)["state"], "unavailable")
        self.assertEqual(target.read_bytes(), b"Public fixture original\n")

    def test_malformed_duplicate_nonfinite_and_extra_fields_never_escape(self):
        record = self.record()
        malformed = [b"{", b'{"record":{},"record":{},"checksum":"x"}',
                     b'{"record":NaN,"checksum":"x"}']
        for raw in malformed:
            self.path.write_bytes(raw)
            self.assertEqual(status.snapshot(self.path)["state"], "unavailable")
        record["private_source"] = "PUBLIC_SENTINEL_NEVER_EXPORT"
        self.raw_record(record)
        out = status.snapshot(self.path)
        self.assertEqual(out["state"], "unavailable")
        self.assertNotIn("PUBLIC_SENTINEL", json.dumps(out))

    def test_large_numeric_timestamp_invalid_types_and_unknown_states_are_safe(self):
        original = self.record()
        for name, bad in (("owner_updated_at", 10 ** 1000), ("version", True),
                          ("owner_state", ["public-invalid"]), ("owner_state", "unknown")):
            record = copy.deepcopy(original)
            record[name] = bad
            self.raw_record(record)
            self.assertEqual(status.snapshot(self.path)["state"], "unavailable")
        record = copy.deepcopy(original)
        record["counters"]["failed"] = True
        self.raw_record(record)
        self.assertEqual(status.snapshot(self.path)["state"], "unavailable")

    def test_future_or_inconsistent_attempt_times_are_unavailable(self):
        original = self.record()
        for field in ("started_at", "updated_at"):
            record = copy.deepcopy(original)
            record["attempt"][field] = 200
            self.raw_record(record)
            self.assertEqual(status.snapshot(self.path, now=100)["state"], "unavailable")
        record = copy.deepcopy(original)
        record["owner_updated_at"] = 200
        self.raw_record(record)
        self.assertEqual(status.snapshot(self.path, now=100)["state"], "unavailable")

    def test_owner_shutdown_does_not_freshen_unfinished_attempt_age(self):
        self.writer.write(value(attempt=attempt()))
        self.writer.write(value(state="stopped", reason="shutdown", attempt=attempt(), now=200))
        out = status.snapshot(self.path, now=200)
        self.assertEqual(out["owner_age_seconds"], 0)
        self.assertEqual(out["attempt_age_seconds"], 100)
        self.assertIn("latest attempt is unfinished and unconfirmed", out["problems"])

    def test_owner_start_without_an_attempt_stays_unconfirmed_when_old(self):
        self.writer.write(value(state="starting", reason="thread_start"))
        out = status.snapshot(self.path, now=200)
        self.assertIsNone(out["latest_attempt"])
        self.assertIn("supervisor observation is stale and unconfirmed", out["problems"])


class SupervisorStatusTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="public-supervisor-attempt-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.enterContext(mock.patch.object(Path, "home", return_value=self.root / "home"))
        self.enterContext(mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.root / "config"),
            "CLAUDE_NOTE_VAULT": str(self.root / "vault")}))
        from claude_note import capture_sweep, config
        self.sweep = capture_sweep
        self.enterContext(mock.patch.object(config, "VAULT_ROOT", self.root / "vault"))
        self.disk = self.enterContext(mock.patch.object(capture_sweep.shutil, "disk_usage",
            return_value=mock.Mock(free=8 * 1024 ** 3)))
        self.clock = self.enterContext(mock.patch.object(capture_sweep.time, "monotonic", return_value=0))
        self.spawn = self.enterContext(mock.patch.object(capture_sweep.subprocess, "Popen"))
        self.child = self.spawn.return_value
        self.child.pid = 12345
        self.child.poll.return_value = None
        self.child.wait.return_value = 0
        self.enterContext(mock.patch.object(capture_sweep, "rss_bytes", return_value=20 * 1024 ** 2))
        self.path = self.root / "import-supervisor.json"
        self.job = capture_sweep.Sweep(logging.getLogger("public-supervisor-test"), status_path=self.path)

    def latest(self):
        return status.read(self.path)

    def test_disk_defer_is_durable_without_touching_completion_or_clean(self):
        clean, completion = self.root / "import-sweep.ok", self.root / "import-sweep.json"
        clean.write_bytes(b"public historical clean\n")
        completion.write_bytes(b"public historical partial\n")
        before = [(p.read_bytes(), p.stat().st_mtime_ns) for p in (clean, completion)]
        self.disk.return_value.free = self.sweep.MIN_FREE_BYTES - 1
        self.job.tick()
        self.assertEqual(self.latest()["attempt"]["reason"], "disk_reserve")
        self.assertFalse(self.latest()["attempt"]["child_started"])
        self.clock.return_value = 1
        self.job.tick()
        self.spawn.assert_not_called()
        self.assertEqual(before, [(p.read_bytes(), p.stat().st_mtime_ns) for p in (clean, completion)])

    def test_expected_disk_measurement_failure_recovers_only_after_interval(self):
        self.disk.side_effect = OSError("PUBLIC_SENTINEL_NEVER_EXPORT")
        self.job.tick()
        self.assertEqual(self.latest()["attempt"]["reason"], "resource_measurement_unavailable")
        self.disk.side_effect = None
        self.clock.return_value = 1
        self.job.tick()
        self.spawn.assert_not_called()
        self.clock.return_value = self.sweep.importers.SWEEP_INTERVAL + 1
        self.job.tick()
        self.spawn.assert_called_once()
        self.assertEqual(self.latest()["attempt"]["state"], "running")
        self.assertEqual(self.latest()["counters"]["failed"], 1)
        self.assertNotIn("PUBLIC_SENTINEL", self.path.read_text())

    def test_expected_spawn_failure_does_not_end_supervision_or_busy_retry(self):
        self.spawn.side_effect = OSError("PUBLIC_SENTINEL_NEVER_EXPORT")
        self.job.tick()
        self.assertEqual(self.latest()["attempt"]["reason"], "spawn_failed")
        self.spawn.side_effect = None
        self.clock.return_value = 1
        self.job.tick()
        self.assertEqual(self.spawn.call_count, 1)
        self.clock.return_value = self.sweep.importers.SWEEP_INTERVAL + 1
        self.job.tick()
        self.assertEqual(self.spawn.call_count, 2)
        self.assertEqual(self.latest()["attempt"]["state"], "running")
        self.assertNotIn("PUBLIC_SENTINEL", self.path.read_text())

    def test_supervisor_loop_continues_after_expected_spawn_failure(self):
        self.spawn.side_effect = [OSError("public transient spawn"), self.child]
        waits = []
        def wait(_):
            waits.append(True)
            if len(waits) == 1:
                self.clock.return_value = self.sweep.importers.SWEEP_INTERVAL + 1
            else:
                self.job.stopping.set()
        with mock.patch.object(self.job.stopping, "wait", side_effect=wait):
            self.job._supervise()
        self.assertEqual(self.spawn.call_count, 2)
        self.assertEqual(self.latest()["counters"]["attempted"], 2)
        self.assertEqual(self.latest()["counters"]["failed"], 1)
        self.assertEqual(self.latest()["owner_state"], "stopped")

    def test_each_resource_stop_is_distinct_and_direct_child_reaped(self):
        for reason in ("deadline", "disk_reserve", "rss_limit", "resource_measurement_unavailable"):
            with self.subTest(reason=reason):
                job = self.sweep.Sweep(logging.getLogger("public-stop"), status_path=self.root / (reason + ".json"))
                self.clock.return_value = 0
                self.disk.side_effect = None
                self.disk.return_value.free = 8 * 1024 ** 3
                self.child.poll.return_value = None
                job.tick()
                self.clock.return_value = self.sweep.MAX_SECONDS if reason == "deadline" else 1
                with mock.patch.object(self.sweep, "rss_bytes", return_value=self.sweep.MAX_RSS_BYTES + 1
                        if reason == "rss_limit" else 0,
                        side_effect=OSError("public measurement") if reason == "resource_measurement_unavailable" else None):
                    if reason == "disk_reserve":
                        self.disk.return_value.free = self.sweep.MIN_FREE_BYTES - 1
                    job.tick()
                record = status.read(self.root / (reason + ".json"))
                self.assertEqual(record["attempt"]["reason"], reason)
                self.assertTrue(record["attempt"]["child_reaped"])
                self.assertIsNone(job.child)

    def test_failed_cleanup_keeps_handle_reason_and_no_overlap_until_reap(self):
        self.job.tick()
        self.child.wait.side_effect = [subprocess.TimeoutExpired("public-child", 2),
                                       subprocess.TimeoutExpired("public-child", 2), 0]
        self.job.close(reason="rss_limit")
        record = self.latest()
        self.assertIs(self.job.child, self.child)
        self.assertEqual(record["attempt"]["reason"], "rss_limit")
        self.assertEqual(record["attempt"]["cleanup"], "cleanup_unconfirmed")
        self.assertFalse(record["attempt"]["child_reaped"])
        self.clock.return_value = self.sweep.importers.SWEEP_INTERVAL + 1
        self.job.tick()
        self.spawn.assert_called_once()
        self.assertIsNone(self.job.child)
        self.assertEqual(self.latest()["attempt"]["reason"], "rss_limit")
        self.assertTrue(self.latest()["attempt"]["child_reaped"])

    def test_repeated_unconfirmed_cleanup_is_not_fsynced_every_tick(self):
        self.job.tick()
        self.child.terminate.side_effect = OSError("public termination failure")
        self.job.close(reason="deadline")
        before = self.path.read_bytes(), self.path.stat().st_mtime_ns
        self.clock.return_value = 1
        self.job.tick()
        self.assertEqual(before, (self.path.read_bytes(), self.path.stat().st_mtime_ns))
        self.assertIs(self.job.child, self.child)

    def test_shutdown_retries_exact_child_retained_by_stopped_supervisor(self):
        self.job.tick()
        self.child.wait.side_effect = [subprocess.TimeoutExpired("public-child", 2),
                                       subprocess.TimeoutExpired("public-child", 2), 0]
        self.job.close(reason="rss_limit")
        self.job.thread = mock.Mock()
        self.job.shutdown()
        self.job.thread.join.assert_called_once()
        self.assertIsNone(self.job.child)
        self.assertEqual(self.latest()["attempt"]["reason"], "rss_limit")
        self.assertTrue(self.latest()["attempt"]["child_reaped"])

    def test_observer_failure_cannot_prevent_terminate_wait_or_ownership_clear(self):
        with mock.patch.object(self.job.observer, "write", side_effect=OSError("PUBLIC_SENTINEL_NEVER_EXPORT")):
            with self.assertLogs("public-supervisor-test", level="WARNING") as log:
                self.job.tick()
                self.job.close(reason="deadline")
        self.child.terminate.assert_called_once()
        self.child.wait.assert_called_once_with(timeout=self.sweep.STOP_GRACE_SECONDS)
        self.assertIsNone(self.job.child)
        self.assertNotIn("PUBLIC_SENTINEL", "\n".join(log.output))

    def test_observed_child_exit_is_not_a_clean_completion(self):
        for code in (0, 1):
            with self.subTest(code=code):
                job = self.sweep.Sweep(logging.getLogger("public-exit"), status_path=self.root / (str(code) + ".json"))
                self.child.poll.return_value = None
                job.tick()
                self.child.poll.return_value = code
                job.tick()
                record = status.read(self.root / (str(code) + ".json"))
                self.assertEqual(record["attempt"]["state"], "exited")
                self.assertEqual(record["attempt"]["exit_code"], code)
                self.assertTrue(record["attempt"]["child_reaped"])
        self.assertFalse((self.root / "import-sweep.ok").exists())
        self.assertFalse((self.root / "import-sweep.json").exists())

    def test_shutdown_preserves_last_actual_attempt_and_counter_scope(self):
        self.disk.return_value.free = 0
        self.job.tick()
        before = copy.deepcopy(self.latest()["attempt"])
        self.job.shutdown()
        record = self.latest()
        self.assertEqual(record["owner_state"], "stopped")
        self.assertEqual(record["attempt"], before)
        self.assertEqual(record["counters"]["deferred"], 1)

    def test_thread_start_and_unexpected_loop_failure_are_fixed_and_visible(self):
        with mock.patch.object(self.sweep.threading, "Thread") as thread:
            thread.return_value.start.side_effect = OSError("PUBLIC_SENTINEL_NEVER_EXPORT")
            self.job.start()
        self.assertIsNone(self.job.thread)
        self.assertEqual(self.latest()["owner_reason"], "thread_start_failed")
        with mock.patch.object(self.job, "tick", side_effect=RuntimeError("PUBLIC_SENTINEL_NEVER_EXPORT")):
            self.job._supervise()
        self.assertEqual(self.latest()["owner_reason"], "supervisor_failed")
        self.assertIsNone(self.latest()["attempt"])
        self.assertNotIn("PUBLIC_SENTINEL", self.path.read_text())

    def test_hostile_lifecycle_log_sink_cannot_break_expected_recovery_or_cleanup(self):
        logger = mock.Mock()
        for level in ("warning", "error", "info"):
            getattr(logger, level).side_effect = RuntimeError("PUBLIC_LOG_SENTINEL")
        path = self.root / "hostile-log.json"
        job = self.sweep.Sweep(logger, status_path=path)
        self.disk.side_effect = OSError("public disk lookup failure")
        job.tick()
        self.disk.side_effect = None
        self.clock.return_value = self.sweep.importers.SWEEP_INTERVAL + 1
        self.spawn.side_effect = OSError("public child startup failure")
        job.tick()
        self.spawn.side_effect = None
        self.clock.return_value = 2 * self.sweep.importers.SWEEP_INTERVAL + 2
        job.tick()
        job.close(reason="rss_limit")
        self.assertIsNone(job.child)
        self.assertTrue(status.read(path)["attempt"]["child_reaped"])
        self.assertEqual(status.read(path)["counters"]["failed"], 2)
        startup = self.sweep.Sweep(logger, status_path=self.root / "hostile-start.json")
        with mock.patch.object(self.sweep.threading, "Thread") as thread:
            thread.return_value.start.side_effect = OSError("public thread startup failure")
            startup.start()
        self.assertIsNone(startup.thread)
        self.assertEqual(status.read(startup.observer.path)["owner_reason"], "thread_start_failed")
        self.assertNotIn("PUBLIC_LOG_SENTINEL", path.read_text() + startup.observer.path.read_text())

    def test_counters_saturate_without_changing_resource_policy(self):
        self.job.counters["attempted"] = status.MAX_COUNTER
        self.disk.return_value.free = 0
        self.job.tick()
        self.assertEqual(self.latest()["counters"]["attempted"], status.MAX_COUNTER)
        self.assertEqual(self.sweep.MIN_FREE_BYTES, 6 * 1024 ** 3)
        self.assertEqual(self.sweep.MAX_RSS_BYTES, 256 * 1024 ** 2)
        self.assertEqual((self.sweep.MAX_SECONDS, self.sweep.STOP_GRACE_SECONDS), (30, 2))


class HealthSupervisorTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="public-supervisor-health-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.logs = self.root / "logs"
        self.logs.mkdir()
        vault = self.root / "vault"
        vault.mkdir()
        from claude_note import health, provenance
        self.health = health
        self.enterContext(mock.patch.dict(os.environ, {"CLAUDE_NOTE_VAULT": str(vault),
            "CLAUDE_NOTE_VAULT_ROOT": str(vault), "CLAUDE_NOTE_RESUME_AFTER": ""}))
        for name, target in (("HOME", self.root / "home"), ("LOGS", self.logs),
                             ("STATE_DIR", self.root / "state"), ("LAUNCH_AGENTS", self.root / "agents")):
            self.enterContext(mock.patch.object(health, name, target))
        for name, result in (("_config", (self.root / "config.toml", {"vault_root": str(vault)})),
                ("_launchd_loaded", True), ("_which", "/public/tool"), ("_notes", {}),
                ("_synthesis", {}), ("freshness_summary", {}),
                ("recall_capabilities", {"ok": False, "stale": True}),
                ("_push_health", {"agent_installed": False, "last_ok": None, "problems": []})):
            self.enterContext(mock.patch.object(health, name, return_value=result))
        self.enterContext(mock.patch.object(provenance, "author_email", return_value="public@example.invalid"))

    def test_new_supervisor_observation_does_not_clear_partial_completion_or_stale_clean(self):
        now = status.time.time()
        clean = self.logs / "import-sweep.ok"
        clean.write_text(json.dumps({"counts": {}}))
        old = now - self.health.IMPORT_STALE_SECONDS - 60
        os.utime(clean, (old, old))
        completion = self.logs / "import-sweep.json"
        completion.write_text(json.dumps({"counts": {"failed": 1, "problems": ["public source failed"]}}))
        before = [(p.read_bytes(), p.stat().st_mtime_ns) for p in (clean, completion)]
        last = attempt(state="deferred", reason="disk_reserve", now=now)
        last.update(started_at=now, child_started=False, child_reaped=None, cleanup="not_started")
        status.Writer(self.logs / "import-supervisor.json").write(value(attempt=last, now=now))
        report = self.health.report()
        sweep = report["import_sweep"]
        self.assertTrue(sweep["stale"])
        self.assertIsNotNone(sweep["last_completed_at"])
        self.assertNotEqual(sweep["last_ok"], sweep["last_completed_at"])
        self.assertIn("import: public source failed", report["problems"])
        self.assertIn("import supervisor: latest attempt reported disk_reserve", report["problems"])
        self.assertFalse(report["ok"])
        self.assertFalse(report["knowledge_loop_ready"])
        self.assertEqual(before, [(p.read_bytes(), p.stat().st_mtime_ns) for p in (clean, completion)])

    def test_missing_or_invalid_metadata_is_offline_and_never_echoes_raw_fields(self):
        report = self.health.report()
        self.assertEqual(report["import_sweep"]["supervisor"]["state"], "not_recorded")
        (self.logs / "import-supervisor.json").write_text('{"private":"PUBLIC_SENTINEL_NEVER_EXPORT"}')
        report = self.health.report()
        self.assertEqual(report["import_sweep"]["supervisor"]["state"], "unavailable")
        self.assertNotIn("PUBLIC_SENTINEL", json.dumps(report))


if __name__ == "__main__":
    unittest.main()
