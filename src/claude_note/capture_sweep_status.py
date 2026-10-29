"""Bounded, self-reported supervisor telemetry; not capture evidence or liveness.

This module is config-free so offline health works before vault setup. A checksum
detects accidental edits, not a same-user actor recomputing a valid envelope.
"""

import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import time
import uuid

from . import state_io

MAX_BYTES = 8192
MAX_COUNTER = 2 ** 31 - 1
MAX_EPOCH = 4102444800  # Bounded UTC representation through 2100.
UNFINISHED_STALE_SECONDS = 60
OWNER_STATES = {"created", "starting", "started", "failed", "stopped"}
OWNER_REASONS = {"none", "thread_start", "thread_start_failed", "supervisor_failed", "shutdown"}
ATTEMPT_STATES = {"starting", "running", "deferred", "exited", "stopped", "failed"}
REASONS = {"attempt_start", "disk_reserve", "deadline", "rss_limit",
           "resource_measurement_unavailable", "child_exit", "spawn_failed", "shutdown",
           "supervisor_failed"}
CLEANUP = {"not_started", "not_requested", "reaped", "cleanup_unconfirmed"}
COUNTERS = {"attempted", "started", "deferred", "exited", "stopped", "failed"}
HEX = re.compile(r"[a-f0-9]{32}\Z")


def _json(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("ascii")


def _keys(value, expected):
    if type(value) is not dict or set(value) != set(expected):
        raise ValueError("Invalid supervisor record")


def _integer(value, minimum=0):
    if type(value) is not int or not minimum <= value <= MAX_COUNTER:
        raise ValueError("Invalid supervisor counter")


def _epoch(value):
    if (type(value) not in (int, float) or not 0 <= value <= MAX_EPOCH
            or type(value) is float and not math.isfinite(value)):
        raise ValueError("Invalid supervisor time")


def validate(record):
    _keys(record, {"version", "kind", "owner", "owner_replaced", "sequence", "owner_state",
                   "owner_reason", "owner_updated_at", "attempt", "counters"})
    if (type(record["version"]) is not int or record["version"] != 1
            or record["kind"] != "claude-note.import-supervisor"
            or type(record["owner"]) is not str or not HEX.fullmatch(record["owner"])
            or type(record["owner_replaced"]) is not bool
            or record["owner_state"] not in OWNER_STATES
            or record["owner_reason"] not in OWNER_REASONS):
        raise ValueError("Invalid supervisor identity")
    _integer(record["sequence"], 1)
    _epoch(record["owner_updated_at"])
    _keys(record["counters"], COUNTERS)
    for value in record["counters"].values():
        _integer(value)
    attempt = record["attempt"]
    if attempt is not None:
        _keys(attempt, {"number", "state", "reason", "started_at", "updated_at", "elapsed_ms",
                        "child_started", "child_reaped", "exit_code", "cleanup"})
        _integer(attempt["number"], 1)
        _integer(attempt["elapsed_ms"])
        _epoch(attempt["started_at"])
        _epoch(attempt["updated_at"])
        if not attempt["started_at"] <= attempt["updated_at"] <= record["owner_updated_at"]:
            raise ValueError("Inconsistent supervisor times")
        if (attempt["state"] not in ATTEMPT_STATES or attempt["reason"] not in REASONS
                or attempt["cleanup"] not in CLEANUP or type(attempt["child_started"]) is not bool
                or attempt["child_reaped"] is not None and type(attempt["child_reaped"]) is not bool
                or attempt["exit_code"] is not None and
                (type(attempt["exit_code"]) is not int or not -255 <= attempt["exit_code"] <= 255)):
            raise ValueError("Invalid supervisor attempt")
        if (attempt["child_reaped"] is True and
                (not attempt["child_started"] or attempt["cleanup"] != "reaped")):
            raise ValueError("Invalid supervisor cleanup")
        if (not attempt["child_started"] and (attempt["child_reaped"] is not None
                or attempt["exit_code"] is not None or attempt["cleanup"] != "not_started")):
            raise ValueError("Invalid unstarted child")
        if attempt["state"] in {"starting", "deferred"} and attempt["child_started"]:
            raise ValueError("Invalid admission state")
        if attempt["state"] in {"exited", "stopped"} and attempt["child_reaped"] is not True:
            raise ValueError("Unconfirmed child cannot be reported reaped")
        if attempt["state"] == "running" and (not attempt["child_started"]
                or attempt["child_reaped"] is not False or attempt["cleanup"] != "not_requested"
                or attempt["exit_code"] is not None):
            raise ValueError("Invalid running observation")
    return record


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate supervisor field")
        value[key] = item
    return value


def _decode(raw):
    if len(raw) > MAX_BYTES:
        raise ValueError("Supervisor record exceeds bound")
    envelope = json.loads(raw, object_pairs_hook=_unique,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Invalid number")))
    _keys(envelope, {"record", "checksum"})
    record = validate(envelope["record"])
    if envelope["checksum"] != hashlib.sha256(_json(record)).hexdigest():
        raise ValueError("Supervisor checksum differs")
    return record


def read(path):
    """Read no more than MAX_BYTES+1; refuse links, FIFOs and unknown formats."""
    with state_io._parent(path) as (parent, name):
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(descriptor, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_BYTES:
                raise ValueError("Invalid supervisor file")
            return _decode(handle.read(MAX_BYTES + 1))


class Writer:
    """Serialize generated latest-view updates and refuse ordinary human edits.

    A different writer can replace a valid older machine envelope. Once this
    instance has published, it cannot overwrite a replacement owner's record.
    This is diagnostic serialization, not an import lease or authority system.
    """
    def __init__(self, path):
        self.path = Path(path)
        self.owner = uuid.uuid4().hex
        self.sequence = 0
        self.owner_replaced = False
        self.pending = None
        self.claimed = False
        self.expected = None

    def _locked_inode(self, descriptor):
        with state_io._parent(self.path.with_name(self.path.name + ".lock")) as (parent, name):
            current = os.stat(name, dir_fd=parent, follow_symlinks=False)
        owned = os.fstat(descriptor)
        if (not stat.S_ISREG(current.st_mode) or current.st_nlink != 1 or owned.st_nlink != 1
                or (current.st_dev, current.st_ino) != (owned.st_dev, owned.st_ino)):
            raise ValueError("Supervisor lock changed")

    def _replace_snapshot(self, raw, lock_descriptor, expected):
        """Use one exclusive slot; unknown crash residue blocks new publication.

        Only the descriptor/inode and exact bytes created by this call may be
        cleaned after a failed write. A replaced or edited pending path remains
        untouched. No per-attempt temporary names or automatic recovery/pruning.
        """
        if not isinstance(raw, bytes) or len(raw) > MAX_BYTES:
            raise ValueError("Supervisor record exceeds bound")
        with state_io._parent(self.path, True) as (parent, name):
            pending = name + ".pending"
            descriptor = os.open(pending, os.O_RDWR | os.O_CREAT | os.O_EXCL |
                                 os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=parent)
            written = 0
            try:
                while written < len(raw):
                    count = os.write(descriptor, raw[written:])
                    if count <= 0:
                        raise OSError("Supervisor write did not progress")
                    written += count
                os.fsync(descriptor)
                owned = os.fstat(descriptor)
                named = os.stat(pending, dir_fd=parent, follow_symlinks=False)
                if (not stat.S_ISREG(named.st_mode) or named.st_nlink != 1 or owned.st_nlink != 1
                        or (owned.st_dev, owned.st_ino) != (named.st_dev, named.st_ino)
                        or os.pread(descriptor, MAX_BYTES + 1, 0) != raw):
                    raise ValueError("Supervisor pending file changed")
                self._locked_inode(lock_descriptor)
                try:
                    current = hashlib.sha256(_json(read(self.path))).hexdigest()
                except FileNotFoundError:
                    current = None
                if current != expected:
                    raise ValueError("Supervisor record changed")
                os.replace(pending, name, src_dir_fd=parent, dst_dir_fd=parent)
                os.fsync(parent)
            finally:
                try:
                    owned = os.fstat(descriptor)
                    try:
                        named = os.stat(pending, dir_fd=parent, follow_symlinks=False)
                    except FileNotFoundError:
                        named = None
                    if (named is not None and stat.S_ISREG(named.st_mode) and named.st_nlink == 1
                            and owned.st_nlink == 1
                            and (owned.st_dev, owned.st_ino) == (named.st_dev, named.st_ino)
                            and os.pread(descriptor, MAX_BYTES + 1, 0) == raw[:written]):
                        os.unlink(pending, dir_fd=parent)
                        os.fsync(parent)
                finally:
                    os.close(descriptor)

    def write(self, value):
        descriptor = state_io.open_lock(self.path.with_name(self.path.name + ".lock"))
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._locked_inode(descriptor)
            try:
                previous = read(self.path)
            except FileNotFoundError:
                previous = None
            previous_hash = hashlib.sha256(_json(previous)).hexdigest() if previous is not None else None
            if self.claimed:
                # A replace may have succeeded before a final directory fsync
                # failed. Recognize only this instance's exact pending bytes.
                if self.pending and self.pending == previous_hash:
                    self.sequence = previous["sequence"]
                    self.expected = previous_hash
                    self.pending = None
                elif previous_hash != self.expected:
                    raise ValueError("Supervisor owner changed")
            else:
                self.expected = previous_hash
            if previous is not None:
                self.owner_replaced |= previous["owner"] != self.owner
            record = {**value, "version": 1, "kind": "claude-note.import-supervisor",
                      "owner": self.owner, "owner_replaced": self.owner_replaced,
                      "sequence": min(self.sequence + 1, MAX_COUNTER)}
            validate(record)
            checksum = hashlib.sha256(_json(record)).hexdigest()
            raw = _json({"record": record, "checksum": checksum}) + b"\n"
            if len(raw) > MAX_BYTES:
                raise ValueError("Supervisor record exceeds bound")
            self._locked_inode(descriptor)
            try:
                current_hash = hashlib.sha256(_json(read(self.path))).hexdigest()
            except FileNotFoundError:
                current_hash = None
            if current_hash != previous_hash:
                raise ValueError("Supervisor record changed")
            self.claimed = True
            self.pending = checksum
            self._replace_snapshot(raw, descriptor, previous_hash)
            self._locked_inode(descriptor)
            self.sequence = record["sequence"]
            self.expected = checksum
            self.pending = None
        finally:
            os.close(descriptor)


def snapshot(path, *, now=None, stale_after=4000):
    """Safe offline projection. File age never establishes current owner liveness."""
    out = {"available": False, "observation_only": True, "state": "not_recorded", "problems": []}
    try:
        path = Path(path)
        with state_io._parent(path) as (parent, name):
            try:
                os.stat(name + ".pending", dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                out.update(state="publication_pending",
                           problems=["supervisor telemetry publication is unconfirmed"])
                return out
        record = read(path)
        now = time.time() if now is None else now
        _epoch(now)
        latest = record["attempt"]
        stamps = [record["owner_updated_at"]] + ([latest["started_at"], latest["updated_at"]] if latest else [])
        if any(value > now + 5 for value in stamps):
            raise ValueError("Supervisor time is in the future")
        out.update({"available": True, "state": "recorded", "owner": record["owner"],
                    "owner_replaced": record["owner_replaced"], "owner_state": record["owner_state"],
                    "owner_reason": record["owner_reason"],
                    "owner_age_seconds": int(max(0, now - record["owner_updated_at"])),
                    "counter_scope": "reported_owner_instance", "counters": record["counters"],
                    "latest_attempt": latest})
        if record["owner_state"] == "failed":
            out["problems"].append("supervisor reported a failure")
        if latest is None and out["owner_age_seconds"] > UNFINISHED_STALE_SECONDS:
            out["problems"].append("supervisor observation is stale and unconfirmed")
        if latest:
            age = int(max(0, now - latest["updated_at"]))
            out["attempt_age_seconds"] = age
            out["stale"] = age > stale_after
            if latest["state"] in {"deferred", "stopped", "failed"}:
                out["problems"].append("latest attempt reported " + latest["reason"])
            elif latest["state"] in {"starting", "running"} and age > UNFINISHED_STALE_SECONDS:
                out["problems"].append("latest attempt is unfinished and unconfirmed")
            elif latest["state"] == "exited" and latest["exit_code"] not in (None, 0):
                out["problems"].append("latest child exit was unsuccessful")
            if latest["cleanup"] == "cleanup_unconfirmed":
                out["problems"].append("owned child cleanup is unconfirmed")
        return out
    except FileNotFoundError:
        return out  # Older installations have no supervisor telemetry.
    except (OSError, ValueError, TypeError, RecursionError):
        out.update(state="unavailable", problems=["cannot inspect supervisor telemetry"])
        return out
