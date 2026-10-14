"""Pure callbacks + owned temporary receipts; no hosts/models/indexes/providers."""
import ast
from dataclasses import FrozenInstanceError
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock

from claude_note import longitudinal_tasks as tasks

REV = "a" * 64


class Crash(BaseException):
    pass


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cn-longitudinal-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "run"
        self.spec = tasks.Spec(trajectories=("public-project",), arms=("scoped_lexical",))
        self.journal = tasks.PhaseJournal(self.root, self.spec, "public-run")
        self.addCleanup(self.journal.close)
        self.cell = tasks.Cell("public-project", "scoped_lexical", 0)

    def run_stage(self, stage, callback=None, cell=None, **kwargs):
        return self.journal.run_stage(cell or self.cell, stage,
            callback or (lambda context: tasks.Outcome()), adapter_revision=REV, **kwargs)

    def finish_phase(self, cell=None):
        cell = cell or self.cell
        for stage in tasks.STAGES:
            self.run_stage(stage, lambda c: tasks.Outcome(details={"reported_success": c.stage == "act"}), cell)

    def test_default_schedule_has_frozen_phase_order_and_declared_control_cuts(self):
        spec = tasks.Spec()
        self.assertEqual(len(spec.schedule()), 56)
        self.assertEqual(len({(c.trajectory, c.arm, c.phase) for c in spec.schedule()}), 56)
        for arm in tasks.ARMS:
            cell = tasks.Cell(spec.trajectories[0], arm, 0)
            self.assertEqual(bool(spec.disabled(cell, "extract")), arm not in tasks.LEARNING_ARMS)
        self.assertEqual(spec.disabled(tasks.Cell("public-project", "dense_only", 3), "extract"),
                         "terminal_phase_no_next_action")
        with self.assertRaises(FrozenInstanceError):
            spec.arms = ()

    def test_invalid_nonfinite_or_over_cap_spec_never_creates_run(self):
        for options in ({"budget_usd": 2.01}, {"budget_usd": float("nan")}, {"budget_usd": True},
                        {"budget_usd": 0}, {"stage_seconds": float("inf")},
                        {"arms": ("dense_only", "dense_only")}, {"trajectories": ("../escape",)}):
            with self.subTest(options=options), self.assertRaises(tasks.JournalError):
                tasks.Spec(**options)

    def test_ordering_and_future_phase_reject_before_callback(self):
        callback = mock.Mock()
        with self.assertRaises(tasks.OrderingError):
            self.run_stage("act", callback)
        future = tasks.Cell(self.cell.trajectory, self.cell.arm, 1)
        with self.assertRaises(tasks.OrderingError):
            self.run_stage("retrieve", callback, future)
        callback.assert_not_called()
        self.assertEqual(self.journal.summary()["callback_admissions"], 0)

    def test_completed_replay_never_calls_changed_callback_and_is_immutable(self):
        callback = mock.Mock(return_value=tasks.Outcome(details={"nested": {"value": 1}}))
        first = self.run_stage("retrieve", callback, reserve_usd=.10)
        second = self.run_stage("retrieve", mock.Mock(side_effect=AssertionError("repeated effect")), reserve_usd=.10)
        self.assertFalse(first.replayed)
        self.assertTrue(second.replayed)
        self.assertEqual(first.digest, second.digest)
        callback.assert_called_once()
        with self.assertRaises(TypeError):
            second.outcome["details"]["nested"]["value"] = 2
        with self.assertRaises(tasks.JournalError):
            self.run_stage("retrieve", callback, reserve_usd=.11)

    def test_crash_after_intent_becomes_unknown_without_callback_retry(self):
        def crash(context):
            raise Crash("Synthetic interruption after durable admission")
        with self.assertRaises(Crash):
            self.run_stage("retrieve", crash, reserve_usd=1.50)
        callback = mock.Mock(side_effect=AssertionError("unknown effect repeated"))
        result = self.run_stage("retrieve", callback, reserve_usd=1.50)
        self.assertEqual(result.state, "unknown")
        self.assertTrue(result.replayed)
        callback.assert_not_called()
        with self.assertRaises(tasks.OrderingError):
            self.run_stage("act", callback)
        summary = self.journal.summary()
        self.assertEqual(summary["counts"]["unknown"], 1)
        self.assertEqual(summary["scheduled"], 4)
        self.assertEqual(summary["reserved_cents"], 150)
        self.assertEqual(summary["reported_task_success_rate"], 0)

    def test_reopened_completed_stage_replays_without_callback(self):
        first = self.run_stage("retrieve", reserve_usd=.25)
        self.journal.close()
        self.journal = tasks.PhaseJournal(self.root, self.spec, "public-run")
        callback = mock.Mock(side_effect=AssertionError("completed external effect repeated"))
        result = self.run_stage("retrieve", callback, reserve_usd=.25)
        self.assertEqual(first.digest, result.digest)
        self.assertTrue(result.replayed)
        callback.assert_not_called()

    def test_callback_close_is_busy_and_relative_io_retains_directory_descriptor(self):
        descriptor = self.journal._fd
        original_open = tasks.os.open
        relative_calls = []
        def checked_open(path, *args, **kwargs):
            if not Path(path).is_absolute():
                self.assertEqual(kwargs.get("dir_fd"), descriptor)
                relative_calls.append(path)
            return original_open(path, *args, **kwargs)
        def callback(context):
            with self.assertRaises(tasks.JournalBusy):
                self.journal.close()
            self.assertEqual(self.journal._fd, descriptor)
            return tasks.Outcome()
        with mock.patch.object(tasks.os, "open", side_effect=checked_open):
            result = self.run_stage("retrieve", callback)
        self.assertEqual(result.state, "completed")
        self.assertTrue(any(str(name).endswith(".outcome.json") for name in relative_calls))
        self.assertEqual(self.journal.summary()["callback_admissions"], 1)

    def test_concurrent_close_rejects_active_callback_then_is_idempotent(self):
        entered, release = threading.Event(), threading.Event()
        results, errors = [], []
        descriptor = self.journal._fd
        def callback(context):
            entered.set()
            if not release.wait(2):
                raise AssertionError("Synthetic callback was not released")
            self.assertEqual(self.journal._fd, descriptor)
            return tasks.Outcome()
        def invoke():
            try:
                results.append(self.run_stage("retrieve", callback))
            except BaseException as exc:
                errors.append(exc)
        thread = threading.Thread(target=invoke)
        thread.start()
        try:
            self.assertTrue(entered.wait(2))
            with self.assertRaises(tasks.JournalBusy):
                self.journal.close()
            self.assertEqual(self.journal._fd, descriptor)
        finally:
            release.set()
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual([result.state for result in results], ["completed"])
        self.journal.close()
        self.journal.close()
        self.assertIsNone(self.journal._fd)
        with self.assertRaises(tasks.JournalBusy):
            self.journal.summary()

    def test_close_between_operation_request_and_lock_acquisition_is_closed(self):
        actual_lock = self.journal._thread_lock
        journal = self.journal
        class CloseBeforeAcquire:
            def acquire(self, blocking=False):
                journal._thread_lock = actual_lock
                journal.close()
                return actual_lock.acquire(blocking=blocking)
            def release(self):
                actual_lock.release()
        self.journal._thread_lock = CloseBeforeAcquire()
        with self.assertRaises(tasks.JournalBusy):
            self.journal.summary()
        self.assertIsNone(self.journal._fd)
        self.journal.close()

    def test_crash_between_intent_and_callback_is_admission_not_entry_proof(self):
        original = self.journal._write
        def interrupt(name, payload):
            result = original(name, payload)
            if name.endswith(".intent.json"):
                raise Crash("Synthetic interruption before callback entry")
            return result
        callback = mock.Mock(return_value=tasks.Outcome())
        with mock.patch.object(self.journal, "_write", side_effect=interrupt), self.assertRaises(Crash):
            self.run_stage("retrieve", callback, reserve_usd=.25)
        callback.assert_not_called()
        self.assertEqual(self.journal.summary()["callback_admissions"], 1)
        result = self.run_stage("retrieve", callback, reserve_usd=.25)
        self.assertEqual(result.state, "unknown")
        callback.assert_not_called()

    def test_unknown_reservation_cannot_be_refunded_to_another_arm(self):
        self.journal.close()
        spec = tasks.Spec(trajectories=("public-project",), arms=("dense_only", "scoped_lexical"))
        self.journal = tasks.PhaseJournal(Path(self.temp.name)/"shared-budget", spec, "shared-run")
        self.cell = tasks.Cell("public-project", "dense_only", 0)
        def crash(context):
            raise Crash("Unknown prior cost")
        with self.assertRaises(Crash):
            self.run_stage("retrieve", crash, reserve_usd=1.50)
        callback = mock.Mock()
        other = tasks.Cell("public-project", "scoped_lexical", 0)
        result = self.run_stage("retrieve", callback, other, reserve_usd=1.00)
        self.assertEqual(result.state, "skipped")
        callback.assert_not_called()
        self.assertEqual(self.journal.summary()["reserved_cents"], 150)
        self.assertEqual(self.journal.summary()["scheduled"], 8)

    def test_callback_failure_is_durable_failed_and_not_retried(self):
        callback = mock.Mock(side_effect=RuntimeError("Synthetic failure after possible effect"))
        first = self.run_stage("retrieve", callback, reserve_usd=.25)
        second = self.run_stage("retrieve", callback, reserve_usd=.25)
        self.assertEqual(first.state, "failed")
        self.assertEqual(first.outcome["details"]["effects"], "unknown")
        self.assertTrue(second.replayed)
        callback.assert_called_once()
        self.assertEqual(self.journal.summary()["counts"]["failed"], 1)

    def test_declared_reservation_skip_preserves_denominator_and_no_callback(self):
        self.run_stage("retrieve", reserve_usd=1.50)
        callback = mock.Mock()
        result = self.run_stage("act", callback, reserve_usd=1.00)
        self.assertEqual(result.state, "skipped")
        self.assertEqual(result.outcome["details"]["reason"], "reservation_budget")
        callback.assert_not_called()
        summary = self.journal.summary()
        self.assertEqual(summary["reserved_cents"], 150)
        self.assertEqual(summary["scheduled"], 4)
        self.assertEqual(summary["counts"]["skipped"], 1)
        self.assertEqual(summary["reported_task_success_rate"], 0)
        self.assertEqual(sum(summary["counts"].values()), 4)

    def test_disabled_learning_stages_cannot_invoke_callbacks(self):
        self.journal.close()
        spec = tasks.Spec(trajectories=("public-project",), arms=("no_memory",))
        self.journal = tasks.PhaseJournal(Path(self.temp.name)/"control", spec, "public-control")
        self.cell = tasks.Cell("public-project", "no_memory", 0)
        for stage in tasks.STAGES[:3]:
            self.run_stage(stage)
        callback = mock.Mock(side_effect=AssertionError("control learned context"))
        for stage in tasks.STAGES[3:]:
            result = self.run_stage(stage, callback)
            self.assertEqual(result.state, "disabled")
        callback.assert_not_called()
        self.assertEqual(self.journal.summary()["callback_admissions"], 3)

    def test_reference_binds_exact_completed_artifact(self):
        artifact = tasks.Artifact("synthetic://own-observed-event", "b"*64)
        self.run_stage("retrieve", lambda context: tasks.Outcome(artifacts=(artifact,)))
        reference = self.journal.reference(self.cell, "retrieve", artifact.source_id)
        seen = []
        self.run_stage("act", lambda c: seen.append(c) or tasks.Outcome(), references=(reference,))
        self.assertEqual(seen[0].references, (reference,))
        self.assertEqual(seen[0].prior_phase_cutoff, -1)
        with self.assertRaises(FrozenInstanceError):
            reference.artifact = tasks.Artifact("other", REV)

    def test_cross_run_arm_project_and_future_refs_reject_before_adapter(self):
        artifact = tasks.Artifact("synthetic://source", REV)
        self.run_stage("retrieve", lambda c: tasks.Outcome(artifacts=(artifact,)))
        valid = self.journal.reference(self.cell, "retrieve", artifact.source_id)
        references = [
            tasks.Reference("other-run", self.cell, "retrieve", artifact, valid.receipt_digest),
            tasks.Reference("public-run", tasks.Cell("other-project", self.cell.arm, 0), "retrieve", artifact, valid.receipt_digest),
            tasks.Reference("public-run", tasks.Cell(self.cell.trajectory, "dense_only", 0), "retrieve", artifact, valid.receipt_digest),
            tasks.Reference("public-run", tasks.Cell(self.cell.trajectory, self.cell.arm, 1), "retrieve", artifact, valid.receipt_digest),
            tasks.Reference("public-run", self.cell, "capture", artifact, valid.receipt_digest),
        ]
        callback = mock.Mock()
        for reference in references:
            with self.subTest(reference=reference), self.assertRaises(tasks.ReferenceError):
                self.run_stage("act", callback, references=(reference,))
        callback.assert_not_called()

    def test_forged_artifact_revision_cannot_borrow_real_producer_receipt(self):
        artifact = tasks.Artifact("synthetic://source", REV)
        self.run_stage("retrieve", lambda c: tasks.Outcome(artifacts=(artifact,)))
        valid = self.journal.reference(self.cell, "retrieve", artifact.source_id)
        forged = tasks.Reference(valid.run_id, valid.cell, valid.stage,
            tasks.Artifact(artifact.source_id, "b"*64), valid.receipt_digest)
        callback = mock.Mock()
        with self.assertRaises(tasks.ReferenceError):
            self.run_stage("act", callback, references=(forged,))
        callback.assert_not_called()

    def test_prior_receipts_and_cutoff_are_only_this_arm_and_immutable(self):
        self.finish_phase()
        next_cell = tasks.Cell(self.cell.trajectory, self.cell.arm, 1)
        seen = []
        self.run_stage("retrieve", lambda c: seen.append(c) or tasks.Outcome(), next_cell)
        self.assertEqual(seen[0].prior_phase_cutoff, 0)
        self.assertEqual(len(seen[0].prior_receipts), 6)
        self.assertFalse(hasattr(seen[0], "lessons"))
        self.assertFalse(hasattr(seen[0], "oracle"))
        with self.assertRaises(TypeError):
            seen[0].prior_receipts[0]["payload"]["details"]["lesson"] = "future answer"

    def test_failed_producer_artifacts_are_not_visible_references(self):
        self.run_stage("retrieve", lambda c: tasks.Outcome("failed", artifacts=(tasks.Artifact("s", REV),)))
        with self.assertRaises(tasks.ReferenceError):
            self.journal.reference(self.cell, "retrieve", "s")

    def test_altered_receipt_is_rejected_and_reported_without_new_callback(self):
        self.run_stage("retrieve", lambda c: tasks.Outcome(details={"value": 1}))
        target = next(self.root.glob("*.outcome.json"))
        target.write_bytes(target.read_bytes().replace(b'"value":1', b'"value":2'))
        callback = mock.Mock()
        with self.assertRaises(tasks.JournalError):
            self.run_stage("retrieve", callback)
        callback.assert_not_called()
        self.assertEqual(self.journal.summary()["counts"]["corrupt"], 1)

    def test_torn_outcome_after_effect_is_never_automatically_repeated(self):
        original = self.journal._write
        def interrupt(name, payload):
            if name.endswith(".outcome.json"):
                path = self.root/name
                with path.open("xb") as handle:
                    handle.write(b'{"payload":')
                path.chmod(0o600)
                raise Crash("Synthetic crash during outcome publication")
            return original(name, payload)
        callback = mock.Mock(return_value=tasks.Outcome())
        with mock.patch.object(self.journal, "_write", side_effect=interrupt), self.assertRaises(Crash):
            self.run_stage("retrieve", callback)
        with self.assertRaises(tasks.JournalError):
            self.run_stage("retrieve", callback)
        callback.assert_called_once()

    def test_semantic_bool_phase_drift_rejected_even_with_recomputed_checksums(self):
        self.run_stage("retrieve")
        intent_path = next(self.root.glob("*.intent.json"))
        outcome_path = next(self.root.glob("*.outcome.json"))
        intent = json.loads(intent_path.read_text());intent["payload"]["cell"]["phase"] = False
        intent["sha256"] = tasks._digest(intent["payload"])
        outcome = json.loads(outcome_path.read_text());outcome["payload"]["intent_digest"] = intent["sha256"]
        outcome["sha256"] = tasks._digest(outcome["payload"])
        intent_path.write_bytes(tasks._json(intent)+b"\n");outcome_path.write_bytes(tasks._json(outcome)+b"\n")
        with self.assertRaises(tasks.JournalError):
            self.run_stage("retrieve")

    def test_root_manifest_change_or_different_spec_cannot_reset_progress(self):
        before = (self.root/"manifest.json").read_bytes()
        with self.assertRaises(tasks.JournalError):
            tasks.PhaseJournal(self.root, tasks.Spec(trajectories=self.spec.trajectories,
                arms=self.spec.arms, budget_usd=1), "public-run")
        self.assertEqual((self.root/"manifest.json").read_bytes(), before)
        (self.root/"manifest.json").write_bytes(before[:-5])
        with self.assertRaises(tasks.JournalError):
            self.run_stage("retrieve", mock.Mock())

    def test_final_record_link_is_rejected_without_touching_sibling(self):
        self.run_stage("retrieve")
        path = next(self.root.glob("*.outcome.json")); raw = path.read_bytes()
        sibling = Path(self.temp.name)/"sibling";sibling.write_bytes(raw);sibling.chmod(0o600)
        path.unlink();path.symlink_to(sibling)
        callback = mock.Mock()
        with self.assertRaises(OSError):
            self.run_stage("retrieve", callback)
        callback.assert_not_called()
        self.assertEqual(sibling.read_bytes(), raw)

    def test_two_journal_handles_cannot_admit_the_same_concurrent_effect(self):
        other = tasks.PhaseJournal(self.root, self.spec, "public-run")
        self.addCleanup(other.close)
        calls = []
        def callback(context):
            calls.append(1)
            with self.assertRaises(tasks.JournalBusy):
                other.run_stage(self.cell, "retrieve", lambda c: calls.append(2) or tasks.Outcome(), adapter_revision=REV)
            return tasks.Outcome()
        self.run_stage("retrieve", callback)
        self.assertEqual(calls, [1])

    def test_deadline_before_admission_skips_without_callback(self):
        callback = mock.Mock()
        result = self.run_stage("retrieve", callback, deadline=0)
        self.assertEqual(result.state, "skipped")
        self.assertEqual(result.outcome["details"]["reason"], "deadline_before_admission")
        callback.assert_not_called()

    def test_intent_fsync_consuming_deadline_cannot_start_known_expired_callback(self):
        clock = [10.]
        original = self.journal._write
        def slow_intent(name, payload):
            record = original(name, payload)
            if name.endswith(".intent.json"):
                clock[0] = 1000.
            return record
        callback = mock.Mock(return_value=tasks.Outcome())
        with mock.patch.object(tasks.time, "monotonic", side_effect=lambda: clock[0]), \
             mock.patch.object(self.journal, "_write", side_effect=slow_intent):
            result = self.run_stage("retrieve", callback, reserve_usd=.25)
        self.assertEqual(result.state, "failed")
        self.assertEqual(result.outcome["details"]["reason"], "deadline_after_intent")
        self.assertEqual(result.outcome["details"]["effects"], "not_invoked")
        callback.assert_not_called()
        summary = self.journal.summary()
        self.assertEqual(summary["callback_admissions"], 1)
        self.assertEqual(summary["reserved_cents"], 25)

    def test_late_callback_return_is_failed_without_claiming_cancellation(self):
        clock = [10.]
        def callback(context):
            clock[0] = 1000.
            return tasks.Outcome(details={"reported_success": True})
        with mock.patch.object(tasks.time, "monotonic", side_effect=lambda: clock[0]):
            result = self.run_stage("retrieve", callback)
        self.assertEqual(result.state, "failed")
        self.assertEqual(result.outcome["details"]["reason"], "deadline_after_return")
        self.assertEqual(result.outcome["details"]["effects"], "unverified")

    def test_all_scheduled_cells_remain_in_failure_and_skip_denominators(self):
        self.run_stage("retrieve")
        self.run_stage("act", skip_reason="public-test-skip")
        summary = self.journal.summary()
        self.assertEqual(len(summary["rows"]), 4)
        self.assertEqual(summary["scheduled"], 4)
        self.assertEqual(summary["reported_task_successes"], 0)
        self.assertEqual(summary["reported_task_success_rate"], 0)
        self.assertEqual(sum(summary["counts"].values()), 4)


class ConfigFreeImportTests(unittest.TestCase):
    def test_module_import_has_no_ambient_config_home_or_file_reads(self):
        source = Path(tasks.__file__).read_bytes()
        tree = ast.parse(source)
        self.assertFalse(any(isinstance(n, ast.ImportFrom) and n.level for n in tree.body))
        module = types.ModuleType("cn_longitudinal_import_fixture")
        with mock.patch.dict(sys.modules, {module.__name__: module}), \
             mock.patch.object(Path, "home", side_effect=AssertionError("ambient home")), \
             mock.patch.object(Path, "read_text", side_effect=AssertionError("ambient config")), \
             mock.patch("builtins.open", side_effect=AssertionError("ambient file")):
            exec(compile(source, "synthetic_module", "exec"), module.__dict__)
        self.assertEqual(len(module.Spec().schedule()), 56)
