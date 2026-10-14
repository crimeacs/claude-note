"""Config-free longitudinal controller receipts, not a model/host executor.

Only explicit caller-owned paths are read. Callbacks remain arbitrary Python:
reference checks and immutable views are not an OS capability sandbox. Digests
bind recorded evidence; they confer neither authority nor model audience.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import threading
import time
from types import MappingProxyType

ARMS = ("no_memory", "static_instructions", "scoped_lexical", "dense_only",
        "pinned_legacy_hybrid_retrieval", "corrected_hybrid", "bounded_history")
PHASES = ("acquire", "transfer", "correct", "corrected_transfer")
STAGES = ("retrieve", "act", "capture", "extract", "mutate", "index")
LEARNING_ARMS = frozenset(ARMS[2:6])
LIMIT = 262144
ID = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
SHA = re.compile(r"[a-f0-9]{64}\Z")


class JournalError(ValueError):
    pass


class OrderingError(JournalError):
    pass


class ReferenceError(JournalError):
    pass


class JournalBusy(JournalError):
    pass


def _json(value):
    try:
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise JournalError("Invalid bounded JSON") from exc
    if len(raw) > LIMIT:
        raise JournalError("Journal record exceeds bound")
    return raw


def _digest(value):
    return hashlib.sha256(_json(value)).hexdigest()


def _identifier(value):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise JournalError("Invalid identifier")
    return value


def _revision(value):
    if not isinstance(value, str) or not SHA.fullmatch(value):
        raise JournalError("Exact SHA-256 revision required")
    return value


def _cents(value, *, zero=True):
    if type(value) not in (int, float, str):
        raise JournalError("Finite decimal reservation required")
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount < 0 or (not zero and amount == 0):
            raise InvalidOperation
        cents = amount * 100
        if cents != cents.to_integral_value() or cents > 200:
            raise InvalidOperation
        return int(cents)
    except (InvalidOperation, ValueError, OverflowError) as exc:
        raise JournalError("Reservation must be in cents, at most $2") from exc


def _freeze(value):
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


@dataclass(frozen=True)
class Cell:
    trajectory: str
    arm: str
    phase: int

    def __post_init__(self):
        _identifier(self.trajectory)
        if self.arm not in ARMS or type(self.phase) is not int or not 0 <= self.phase < len(PHASES):
            raise JournalError("Unknown arm/phase")

    def to_dict(self):
        return {"trajectory": self.trajectory, "arm": self.arm, "phase": self.phase}


@dataclass(frozen=True)
class Spec:
    trajectories: tuple = ("payment-replay-dev", "retry-recovery-dev")
    arms: tuple = ARMS
    budget_usd: float = 2.0
    stage_seconds: float = 240.0

    def __post_init__(self):
        if (type(self.trajectories) is not tuple or not 1 <= len(self.trajectories) <= 16 or
                len(set(self.trajectories)) != len(self.trajectories)):
            raise JournalError("Unique frozen trajectories required")
        for value in self.trajectories:
            _identifier(value)
        if (type(self.arms) is not tuple or not self.arms or len(set(self.arms)) != len(self.arms) or
                any(arm not in ARMS for arm in self.arms)):
            raise JournalError("Unique known arms required")
        _cents(self.budget_usd, zero=False)
        if (type(self.stage_seconds) not in (int, float) or not math.isfinite(self.stage_seconds)
                or not 0 < self.stage_seconds <= 240):
            raise JournalError("Finite cooperative stage bound required")

    def schedule(self):
        return tuple(Cell(t, a, p) for t in self.trajectories for a in self.arms for p in range(len(PHASES)))

    def disabled(self, cell, stage):
        if stage in ("extract", "mutate", "index"):
            if cell.arm not in LEARNING_ARMS:
                return "disabled_by_arm"
            if cell.phase == len(PHASES) - 1:
                return "terminal_phase_no_next_action"
        return ""

    def to_dict(self):
        return {"version": 1, "trajectories": list(self.trajectories), "arms": list(self.arms),
                "phases": list(PHASES), "stages": list(STAGES),
                "reservation_cap_cents": _cents(self.budget_usd), "stage_seconds": self.stage_seconds,
                "prior_context_characters": 4096, "prior_context_utf8_bytes": 16384}


@dataclass(frozen=True)
class Artifact:
    source_id: str
    revision: str

    def __post_init__(self):
        if (not isinstance(self.source_id, str) or not self.source_id or
                len(self.source_id.encode("utf-8")) > 1024 or
                any(ord(char) < 32 or ord(char) == 127 for char in self.source_id)):
            raise JournalError("Bounded source identifier required")
        _revision(self.revision)

    def to_dict(self):
        return {"source_id": self.source_id, "revision": self.revision}


@dataclass(frozen=True)
class Reference:
    run_id: str
    cell: Cell
    stage: str
    artifact: Artifact
    receipt_digest: str

    def __post_init__(self):
        _identifier(self.run_id)
        if not isinstance(self.cell, Cell) or self.stage not in STAGES or not isinstance(self.artifact, Artifact):
            raise ReferenceError("Invalid reference")
        _revision(self.receipt_digest)

    def to_dict(self):
        return {"run_id": self.run_id, "cell": self.cell.to_dict(), "stage": self.stage,
                "artifact": self.artifact.to_dict(), "receipt_digest": self.receipt_digest}


@dataclass(frozen=True)
class Outcome:
    status: str = "completed"
    details: dict = field(default_factory=dict)
    artifacts: tuple = ()

    def to_dict(self):
        if (self.status not in ("completed", "failed") or type(self.details) is not dict or
                type(self.artifacts) is not tuple or len(self.artifacts) > 32 or
                any(not isinstance(item, Artifact) for item in self.artifacts)):
            raise JournalError("Invalid callback outcome")
        values = [item.to_dict() for item in self.artifacts]
        if len({item.source_id for item in self.artifacts}) != len(values):
            raise JournalError("Duplicate artifact identity")
        result = {"status": self.status, "details": self.details, "artifacts": values}
        return json.loads(_json(result))  # Detach callback-owned mutable dictionaries.


@dataclass(frozen=True)
class StageContext:
    run_id: str
    cell: Cell
    stage: str
    prior_phase_cutoff: int
    references: tuple
    prior_receipts: tuple
    arm_root: Path
    deadline_monotonic: float


@dataclass(frozen=True)
class Result:
    state: str
    digest: str
    outcome: object
    replayed: bool


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise JournalError("Duplicate journal field")
        result[key] = value
    return result


class PhaseJournal:
    """Private append-only receipts in one explicitly supplied run directory.

    A durable intent is callback admission, not proof the callback entered or
    performed an effect. Missing/torn outcomes never authorize automatic repeat.
    The lock covers callbacks; concurrent callers fail busy rather than wait.
    """
    def __init__(self, root, spec: Spec, run_id: str):
        if not isinstance(spec, Spec):
            raise JournalError("Frozen Spec required")
        self.root, self.spec, self.run_id = Path(root), spec, _identifier(run_id)
        if not self.root.is_absolute():
            raise JournalError("Explicit absolute journal root required")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        self._thread_lock = threading.Lock()
        self._manifest = {"version": 1, "kind": "run", "run_id": self.run_id,
                          "spec": spec.to_dict(), "spec_digest": _digest(spec.to_dict())}
        try:
            with self._locked():
                record = self._read("manifest.json")
                if record is None:
                    record = self._write("manifest.json", self._manifest)
                if _json(record["payload"]) != _json(self._manifest):
                    raise JournalError("Run/spec differs from immutable manifest")
                self._manifest_digest = record["sha256"]
        except BaseException:
            self.close()
            raise

    def close(self):
        if not self._thread_lock.acquire(blocking=False):
            raise JournalBusy("Journal in use")
        try:
            if self._fd is not None:
                os.close(self._fd)
                self._fd = None
        finally:
            self._thread_lock.release()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    @contextmanager
    def _locked(self):
        if not self._thread_lock.acquire(blocking=False):
            raise JournalBusy("Journal closed or in use")
        lock = None
        try:
            if self._fd is None:
                raise JournalBusy("Journal closed or in use")
            current, opened = self.root.lstat(), os.fstat(self._fd)
            if (not stat.S_ISDIR(current.st_mode) or current.st_mode & 0o077 or
                    (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)):
                raise JournalError("Journal root changed or is not private")
            lock = os.open(".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                           0o600, dir_fd=self._fd)
            self._regular(lock)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise JournalBusy("Journal in use") from exc
            yield
        finally:
            if lock is not None:
                os.close(lock)
            self._thread_lock.release()

    @staticmethod
    def _regular(fd):
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o077 or info.st_size > LIMIT:
            raise JournalError("Invalid private regular journal file")

    def _read(self, name):
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self._fd)
        except FileNotFoundError:
            return None
        try:
            self._regular(fd)
            with os.fdopen(fd, "rb", closefd=False) as handle:
                raw = handle.read(LIMIT + 1)
        finally:
            os.close(fd)
        try:
            record = json.loads(raw, object_pairs_hook=_unique)
            if (type(record) is not dict or set(record) != {"payload", "sha256"} or
                    type(record["payload"]) is not dict or record["sha256"] != _digest(record["payload"]) or
                    raw != _json(record) + b"\n"):
                raise JournalError("Invalid altered or torn journal record")
            return record
        except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
            raise JournalError("Invalid altered or torn journal record") from exc

    def _write(self, name, payload):
        record = {"payload": payload, "sha256": _digest(payload)}
        raw = _json(record) + b"\n"
        if len(raw) > LIMIT:
            raise JournalError("Journal record exceeds bound")
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=self._fd)
        try:
            remaining = memoryview(raw)
            while remaining:
                count = os.write(fd, remaining)
                if count <= 0:
                    raise OSError("Incomplete journal write")
                remaining = remaining[count:]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.fsync(self._fd)
        return record

    def _check_manifest(self):
        record = self._read("manifest.json")
        if record is None or _json(record["payload"]) != _json(self._manifest) or record["sha256"] != self._manifest_digest:
            raise JournalError("Run manifest changed")

    def _name(self, cell, stage, kind):
        if cell not in self.spec.schedule() or stage not in STAGES:
            raise JournalError("Cell/stage outside frozen schedule")
        return _digest(cell.to_dict()) + "." + stage + "." + kind + ".json"

    def _load(self, cell, stage, previous):
        intent = self._read(self._name(cell, stage, "intent"))
        outcome = self._read(self._name(cell, stage, "outcome"))
        if intent is None:
            if outcome is not None:
                raise JournalError("Outcome lacks intent")
            return None, None
        body = intent["payload"]
        if (set(body) != {"version", "kind", "manifest_digest", "cell", "stage", "previous_digest",
                         "input", "reservation_cents", "callback_admitted"} or
                type(body["version"]) is not int or body["version"] != 1 or body["kind"] != "intent" or
                body["manifest_digest"] != self._manifest_digest or _json(body["cell"]) != _json(cell.to_dict()) or
                body["stage"] != stage or body["previous_digest"] != previous or
                type(body["reservation_cents"]) is not int or not 0 <= body["reservation_cents"] <= 200 or
                type(body["callback_admitted"]) is not bool or type(body["input"]) is not dict):
            raise JournalError("Intent differs from phase chain")
        inputs = body["input"]
        if (set(inputs) != {"adapter_revision", "references", "requested_reservation_cents", "skip_reason"} or
                type(inputs["requested_reservation_cents"]) is not int or
                not 0 <= inputs["requested_reservation_cents"] <= 200 or
                type(inputs["references"]) is not list or len(inputs["references"]) > 64 or
                not isinstance(inputs["skip_reason"], str) or len(inputs["skip_reason"]) > 160):
            raise JournalError("Invalid admitted input schema")
        _revision(inputs["adapter_revision"])
        if body["callback_admitted"]:
            if self.spec.disabled(cell, stage) or body["reservation_cents"] != inputs["requested_reservation_cents"]:
                raise JournalError("Callback violates declared stage/reservation policy")
        elif body["reservation_cents"]:
            raise JournalError("Disabled/skipped callback cannot reserve cost")
        for reference in inputs["references"]:
            try:
                if set(reference) != {"run_id", "cell", "stage", "artifact", "receipt_digest"}:
                    raise JournalError("Invalid recorded reference")
                ref = Reference(reference["run_id"], Cell(**reference["cell"]), reference["stage"],
                                Artifact(**reference["artifact"]), reference["receipt_digest"])
                if (ref.run_id != self.run_id or ref.cell.trajectory != cell.trajectory or ref.cell.arm != cell.arm or
                        ref.cell.phase > cell.phase or
                        (ref.cell.phase == cell.phase and STAGES.index(ref.stage) >= STAGES.index(stage))):
                    raise ReferenceError("Recorded cross-arm/future reference")
            except (TypeError, KeyError) as exc:
                raise JournalError("Invalid recorded reference") from exc
        if outcome is not None:
            value = outcome["payload"]
            if (set(value) != {"version", "kind", "intent_digest", "status", "details", "artifacts"} or
                    type(value["version"]) is not int or value["version"] != 1 or value["kind"] != "outcome" or
                    value["intent_digest"] != intent["sha256"] or
                    value["status"] not in ("completed", "failed", "disabled", "skipped") or
                    type(value["details"]) is not dict or type(value["artifacts"]) is not list):
                raise JournalError("Outcome differs from admitted intent")
            for artifact in value["artifacts"]:
                if type(artifact) is not dict or set(artifact) != {"source_id", "revision"}:
                    raise JournalError("Invalid recorded artifact")
                Artifact(**artifact)
            if (len(value["artifacts"]) > 32 or len({a["source_id"] for a in value["artifacts"]}) != len(value["artifacts"]) or
                    (body["callback_admitted"] and value["status"] not in ("completed", "failed")) or
                    (not body["callback_admitted"] and (value["status"] not in ("disabled", "skipped") or value["artifacts"]))):
                raise JournalError("Outcome violates admission/artifact policy")
        return intent, outcome

    def _previous(self, cell, stage):
        previous, prior = self._manifest_digest, []
        for phase in range(cell.phase + 1):
            current = Cell(cell.trajectory, cell.arm, phase)
            for item in STAGES:
                if current == cell and item == stage:
                    return previous, tuple(prior)
                intent, outcome = self._load(current, item, previous)
                if outcome is None:
                    raise OrderingError("Prior stage unresolved; no automatic repeat")
                previous = outcome["sha256"]
                if phase < cell.phase:
                    prior.append(_freeze(outcome))
        raise OrderingError("Unknown stage")

    def _check_reference(self, target, stage, reference):
        if (not isinstance(reference, Reference) or reference.run_id != self.run_id or
                reference.cell.trajectory != target.trajectory or reference.cell.arm != target.arm or
                reference.cell.phase > target.phase or
                (reference.cell.phase == target.phase and STAGES.index(reference.stage) >= STAGES.index(stage))):
            raise ReferenceError("Cross-arm/run or future reference")
        previous, _ = self._previous(reference.cell, reference.stage)
        _, outcome = self._load(reference.cell, reference.stage, previous)
        if (outcome is None or outcome["sha256"] != reference.receipt_digest or
                outcome["payload"]["status"] != "completed" or
                reference.artifact.to_dict() not in outcome["payload"]["artifacts"]):
            raise ReferenceError("Reference lacks exact completed producer receipt")

    def reference(self, cell, stage, source_id):
        with self._locked():
            self._check_manifest()
            previous, _ = self._previous(cell, stage)
            _, outcome = self._load(cell, stage, previous)
            if outcome is None or outcome["payload"]["status"] != "completed":
                raise ReferenceError("Producer is not completed")
            matches = [a for a in outcome["payload"]["artifacts"] if a["source_id"] == source_id]
            if len(matches) != 1:
                raise ReferenceError("Artifact not uniquely recorded")
            return Reference(self.run_id, cell, stage, Artifact(**matches[0]), outcome["sha256"])

    def _reserved(self):
        total = 0
        for trajectory in self.spec.trajectories:
            for arm in self.spec.arms:
                previous, blocked = self._manifest_digest, False
                for phase in range(len(PHASES)):
                    for stage in STAGES:
                        cell = Cell(trajectory, arm, phase)
                        intent, outcome = self._load(cell, stage, previous)
                        if intent is not None:
                            if blocked:
                                raise JournalError("Recorded work after unresolved predecessor")
                            total += intent["payload"]["reservation_cents"]
                        if outcome is None:
                            blocked = True
                        else:
                            previous = outcome["sha256"]
        if total > _cents(self.spec.budget_usd):
            raise JournalError("Recorded reservations exceed frozen cap")
        return total

    def run_stage(self, cell, stage, callback=None, *, adapter_revision, references=(),
                  reserve_usd=0, skip_reason="", deadline=None):
        """Admit once or replay a receipt. Deadlines are cooperative, not preemption."""
        revision, requested = _revision(adapter_revision), _cents(reserve_usd)
        if type(references) not in (tuple, list) or len(references) > 64:
            raise ReferenceError("Bounded declared references required")
        if not isinstance(skip_reason, str) or len(skip_reason) > 160:
            raise JournalError("Bounded skip reason required")
        if deadline is not None and (type(deadline) not in (int, float) or not math.isfinite(deadline)):
            raise JournalError("Finite monotonic deadline required")
        with self._locked():
            self._check_manifest()
            self._name(cell, stage, "intent")
            previous, prior = self._previous(cell, stage)
            for reference in references:
                self._check_reference(cell, stage, reference)
            inputs = {"adapter_revision": revision, "references": [r.to_dict() for r in references],
                      "requested_reservation_cents": requested, "skip_reason": skip_reason}
            intent, outcome = self._load(cell, stage, previous)
            if intent is not None:
                if _json(intent["payload"]["input"]) != _json(inputs):
                    raise JournalError("Replay input differs from admitted intent")
                if outcome is None:
                    return Result("unknown", intent["sha256"], _freeze({"effects": "unknown"}), True)
                return Result(outcome["payload"]["status"], outcome["sha256"], _freeze(outcome["payload"]), True)
            reserved = self._reserved()
            expires = min(time.monotonic() + self.spec.stage_seconds, deadline) if deadline is not None else time.monotonic() + self.spec.stage_seconds
            disabled = self.spec.disabled(cell, stage)
            decision = disabled or skip_reason or ("reservation_budget" if reserved + requested > _cents(self.spec.budget_usd) else "")
            if expires <= time.monotonic():
                decision = decision or "deadline_before_admission"
            if not decision and not callable(callback):
                raise JournalError("Callable adapter required")
            intent = self._write(self._name(cell, stage, "intent"), {
                "version": 1, "kind": "intent", "manifest_digest": self._manifest_digest,
                "cell": cell.to_dict(), "stage": stage, "previous_digest": previous,
                "input": inputs, "reservation_cents": requested if not decision else 0,
                "callback_admitted": not bool(decision)})
            if decision:
                value = {"status": "disabled" if disabled else "skipped", "details": {"reason": decision}, "artifacts": []}
            elif time.monotonic() >= expires:
                value = {"status": "failed", "details": {"reason": "deadline_after_intent", "effects": "not_invoked"}, "artifacts": []}
            else:
                context = StageContext(self.run_id, cell, stage, cell.phase - 1, tuple(references), prior,
                    self.root / "artifacts" / cell.trajectory / cell.arm, expires)
                try:
                    report = callback(context)
                    if not isinstance(report, Outcome):
                        raise JournalError("Adapter must return Outcome")
                    value = report.to_dict()
                    if time.monotonic() > expires:
                        value = {"status": "failed", "details": {"reason": "deadline_after_return", "effects": "unverified"}, "artifacts": []}
                except Exception as exc:
                    value = {"status": "failed", "details": {"reason": "callback_exception", "error_type": type(exc).__name__, "effects": "unknown"}, "artifacts": []}
            outcome = self._write(self._name(cell, stage, "outcome"), {
                "version": 1, "kind": "outcome", "intent_digest": intent["sha256"], **value})
            return Result(value["status"], outcome["sha256"], _freeze(outcome["payload"]), False)

    def summary(self):
        """All frozen cells, including unknown/corrupt/unstarted outcomes, stay visible."""
        with self._locked():
            self._check_manifest()
            rows, admissions, reserved = [], 0, 0
            for trajectory in self.spec.trajectories:
                for arm in self.spec.arms:
                    previous, blocked = self._manifest_digest, False
                    for phase in range(len(PHASES)):
                        cell, stages, task_success = Cell(trajectory, arm, phase), {}, False
                        for stage in STAGES:
                            try:
                                intent, outcome = self._load(cell, stage, previous)
                                if blocked and intent is not None:
                                    raise JournalError("Work after unresolved predecessor")
                                if intent is not None:
                                    admissions += int(intent["payload"]["callback_admitted"])
                                    reserved += intent["payload"]["reservation_cents"]
                                state = outcome["payload"]["status"] if outcome else "unknown" if intent else "blocked" if blocked else "not_started"
                                if outcome:
                                    previous = outcome["sha256"]
                                    if stage == "act":
                                        task_success = state == "completed" and outcome["payload"]["details"].get("reported_success") is True
                                else:
                                    blocked = True
                            except (JournalError, OSError):
                                state, blocked = "corrupt", True
                            stages[stage] = state
                        states = set(stages.values())
                        state = next((s for s in ("corrupt", "unknown", "failed", "skipped", "not_started", "blocked") if s in states), "resolved")
                        rows.append({**cell.to_dict(), "state": state, "stages": stages,
                                     "reported_task_success": task_success})
            return {"scheduled": len(rows), "reported_task_successes": sum(r["reported_task_success"] for r in rows),
                    "reported_task_success_rate": sum(r["reported_task_success"] for r in rows) / len(rows),
                    "callback_admissions": admissions, "reserved_cents": reserved,
                    "counts": {s: sum(r["state"] == s for r in rows) for s in
                               ("resolved", "failed", "skipped", "unknown", "corrupt", "not_started", "blocked")},
                    "rows": rows, "measurement": "callback_reports_and_controller_receipts_not_transport_effect_or_task_attestation"}
