"""Resource-supervised import sweep, separate from hook capture's heap.

The owner supervises and reaps its exact direct child. Current importers do not
spawn subprocesses; no empty process-group/tree guarantee is asserted. Resource
checks are periodic stop thresholds, with additional cleanup grace, not OS caps.
"""

import shutil
import subprocess
import sys
import threading
import time

from . import config, importers, capture_sweep_status

MIN_FREE_BYTES = 6 * 1024 ** 3
MAX_RSS_BYTES = 256 * 1024 ** 2
MAX_SECONDS = 30
STOP_GRACE_SECONDS = 2


def rss_bytes(pid):
    """Read only the child we own; a failed measurement stops the sweep."""
    result = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "rss="],
                            capture_output=True, text=True, timeout=1)
    if result.returncode or not result.stdout.strip().isdigit():
        raise OSError("Cannot measure import child RSS")
    return int(result.stdout.strip()) * 1024


class Sweep:
    def __init__(self, logger, *, status_path=None):
        self.logger = logger
        self.child = None
        self.started = 0.0
        self.last_attempt = None
        self.stopping = threading.Event()
        self.thread = None
        self.observer = capture_sweep_status.Writer(status_path or
            importers.HEARTBEAT.with_name("import-supervisor.json"))
        self.owner_state, self.owner_reason = "created", "none"
        self.attempt = None
        self.counters = {name: 0 for name in capture_sweep_status.COUNTERS}
        self.stop_reason = None
        self.failed_counted = False

    def _count(self, name):
        self.counters[name] = min(self.counters[name] + 1, capture_sweep_status.MAX_COUNTER)

    def _failed(self):
        if not self.failed_counted:
            self._count("failed")
            self.failed_counted = True

    def _log(self, level, message, *args):
        """A failed diagnostic sink must not change lifecycle control flow."""
        try:
            getattr(self.logger, level)(message, *args)
        except Exception:
            pass

    def _publish(self):
        # Telemetry is best effort. It cannot change capture admission, replace
        # .ok/completion evidence, or raise past resource cleanup.
        try:
            self.observer.write({"owner_state": self.owner_state,
                "owner_reason": self.owner_reason, "owner_updated_at": time.time(),
                "attempt": self.attempt, "counters": self.counters.copy()})
        except Exception:
            self._log("warning", "Import supervisor telemetry unavailable; prior evidence retained")

    def _owner(self, state, reason):
        self.owner_state, self.owner_reason = state, reason
        self._publish()

    def _record(self, state, reason, *, child_started=False, child_reaped=None,
                cleanup="not_started", exit_code=None):
        if self.attempt is None:
            return
        transition = {"state": state, "reason": reason, "child_started": child_started,
            "child_reaped": child_reaped, "cleanup": cleanup,
            "exit_code": exit_code if type(exit_code) is int and -255 <= exit_code <= 255 else None}
        if all(self.attempt.get(key) == value for key, value in transition.items()):
            return  # Repeated unconfirmed cleanup is not a new lifecycle event.
        self.attempt = {**self.attempt, **transition,
            "updated_at": time.time(), "elapsed_ms": min(capture_sweep_status.MAX_COUNTER,
                int(max(0, time.monotonic() - self.started) * 1000))}
        self._publish()

    def start(self):
        self._owner("starting", "thread_start")
        try:
            self.thread = threading.Thread(target=self._supervise, name="bounded-import-supervisor")
            self.thread.start()
        except (OSError, RuntimeError):
            self.thread = None
            self._owner("failed", "thread_start_failed")
            self._log("error", "Import supervisor could not start; hook capture continues")

    def _supervise(self):
        self._owner("started", "thread_start")
        try:
            while not self.stopping.is_set():
                self.tick()
                self.stopping.wait(.25)
        except Exception:
            self.owner_state, self.owner_reason = "failed", "supervisor_failed"
            if self.child is not None:
                self._failed()
            elif self.attempt and self.attempt["state"] in {"starting", "running"}:
                self._failed()
                self._record("failed", "supervisor_failed")
            self._log("error", "Import supervisor failed; hook capture continues")
        finally:
            # Child cleanup precedes final telemetry; observer errors cannot
            # prevent terminate/kill/wait or clear an unreaped child handle.
            reason = "supervisor_failed" if self.owner_state == "failed" else "shutdown"
            self.close(reason=self.stop_reason or reason)
            if self.owner_state == "failed":
                self._publish()
            else:
                self._owner("stopped", "shutdown")

    def shutdown(self):
        self.stopping.set()
        if self.thread is not None:
            self.thread.join()
        # A stopped supervisor may still own a child whose earlier cleanup was
        # unconfirmed. Make one bounded final cleanup attempt after joining.
        if self.child is not None:
            self.close(reason=self.stop_reason or "shutdown")
        if self.owner_state != "failed":
            self._owner("stopped", "shutdown")

    def tick(self):
        now = time.monotonic()
        if self.child is not None:
            if self.stop_reason:
                self.close(reason=self.stop_reason)
                return
            code = self.child.poll()
            if code is not None:
                child = self.child
                try:
                    child.wait(timeout=STOP_GRACE_SECONDS)
                except (OSError, ValueError, subprocess.SubprocessError):
                    self.stop_reason = "child_exit"
                    self._failed()
                    self._record("failed", "child_exit", child_started=True, child_reaped=False,
                                 cleanup="cleanup_unconfirmed", exit_code=code)
                    return
                self.child = None
                self._count("exited")
                self._record("exited", "child_exit", child_started=True, child_reaped=True,
                             cleanup="reaped", exit_code=code)
                self._log("info", "Bounded import sweep exited: %s", code)
                return
            try:
                reason = ("deadline" if now - self.started >= MAX_SECONDS else
                          "disk_reserve" if shutil.disk_usage(config.VAULT_ROOT).free < MIN_FREE_BYTES else
                          "rss_limit" if rss_bytes(self.child.pid) > MAX_RSS_BYTES else "")
            except (OSError, ValueError, subprocess.TimeoutExpired):
                reason = "resource_measurement_unavailable"
            if reason:
                self.stop_reason = self.stop_reason or reason
                self._log("warning", "Stopping import sweep: %s; unfinished evidence retained", reason)
                self.close(reason=reason)
            return
        if self.last_attempt is not None and now - self.last_attempt < importers.SWEEP_INTERVAL:
            return
        self.last_attempt = now
        self.started = now
        self.stop_reason = None
        self.failed_counted = False
        self._count("attempted")
        self.attempt = {"number": self.counters["attempted"], "started_at": time.time()}
        self._record("starting", "attempt_start")
        try:
            free = shutil.disk_usage(config.VAULT_ROOT).free
        except (OSError, ValueError):
            self._failed()
            self._record("failed", "resource_measurement_unavailable")
            self._log("warning", "Import sweep deferred: resource measurement unavailable")
            return
        if free < MIN_FREE_BYTES:
            self._count("deferred")
            self._record("deferred", "disk_reserve")
            self._log("warning", "Import sweep deferred: preserving 6 GiB disk reserve")
            return
        try:
            self.child = subprocess.Popen([sys.executable, "-m", "claude_note.capture_sweep"],
                                          stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                          stderr=subprocess.DEVNULL, start_new_session=True)
        except (OSError, ValueError, subprocess.SubprocessError):
            self._failed()
            self._record("failed", "spawn_failed")
            self._log("warning", "Import sweep deferred: child startup failed")
            return
        self._count("started")
        self._record("running", "attempt_start", child_started=True, child_reaped=False,
                     cleanup="not_requested")

    def close(self, *, reason="shutdown"):
        child = self.child
        if child is None:
            return
        self.stop_reason = self.stop_reason or reason
        # Keep direct-child ownership until wait() reaps this exact PID. Atomic
        # publication leaves interrupted import intents replayable. Status never
        # acknowledges them and never establishes an empty process tree/group.
        reaped, code = False, None
        try:
            if child.poll() is None:
                try:
                    child.terminate()
                except ProcessLookupError:
                    pass
                try:
                    code = child.wait(timeout=STOP_GRACE_SECONDS)
                except subprocess.TimeoutExpired:
                    child.kill()
                    code = child.wait(timeout=STOP_GRACE_SECONDS)
            else:
                code = child.wait(timeout=STOP_GRACE_SECONDS)
            reaped = True
            self.child = None
            self._count("stopped")
        except (OSError, ValueError, subprocess.SubprocessError):
            self._failed()
            self._log("warning", "Import sweep cleanup unconfirmed; exact child ownership retained")
        finally:
            # All observer IO is after this cleanup attempt. Failure to persist
            # cannot bypass cleanup or make child_reaped true.
            self._record("stopped" if reaped else "failed", self.stop_reason,
                         child_started=True, child_reaped=reaped,
                         cleanup="reaped" if reaped else "cleanup_unconfirmed", exit_code=code)


def main():
    # Bound each store admission as well as the supervisor's periodic check.
    import io
    from pathlib import Path
    from . import import_publication, source_store, state_io, transcript_reader

    if shutil.disk_usage(config.VAULT_ROOT).free < MIN_FREE_BYTES:
        return 2
    payload_limit, record_limit = 8 * 1024 ** 2, 1024 ** 2
    limit_error = "Capture sweep exceeds 8 MiB payload or 1 MiB record; preserve the original for lossless recovery"
    overrides = []

    def override(module, name, value):
        overrides.append((module, name, getattr(module, name)))
        setattr(module, name, value)

    capture_native = transcript_reader.capture_native_transcript
    read_native = transcript_reader.read_transcript
    publish = import_publication.publish
    import_zip = importers.import_zip
    archive_backup = importers._archive_backup
    atomic_bytes = state_io.atomic_bytes

    def checked_native_bytes(path, raw=None):
        if raw is None:
            transcript_reader.check_transcript_size(path)
            with Path(path).open("rb") as source:
                raw = source.read(payload_limit + 1)
        if len(raw) > payload_limit:
            raise ValueError(limit_error)
        with io.BytesIO(raw) as source:
            while True:
                line = source.readline(record_limit + 1)
                if not line:
                    break
                if len(line) > record_limit:
                    raise ValueError(limit_error)
                if not line.endswith(b"\n"):
                    raise ValueError("Transcript contains malformed or incomplete JSONL records")
        return raw

    def bounded_capture(path):
        raw = capture_native(path)
        # The ordinary small-source path otherwise bypasses the native record
        # cap and can mark hook-delivered sources seen before parsing them.
        checked_native_bytes(path, raw)
        return raw

    def bounded_read(path, *, captured_bytes=None):
        return read_native(path, captured_bytes=checked_native_bytes(path, captured_bytes))

    def bounded_publish(source, session_id, logical_id, transcript_path, raw, *args, **kwargs):
        if not isinstance(raw, bytes) or len(raw) > payload_limit:
            raise ValueError(limit_error)
        return publish(source, session_id, logical_id, transcript_path, raw, *args, **kwargs)

    def check_zip(path):
        size = Path(path).stat().st_size
        if size > payload_limit or shutil.disk_usage(config.VAULT_ROOT).free < MIN_FREE_BYTES + size:
            raise ValueError(limit_error)

    def bounded_import_zip(path, seen, **kwargs):
        # Reject before processing conversations, so oversized archives never
        # advance their seen watermarks or start an unbudgeted backup copy.
        check_zip(path)
        return import_zip(path, seen, **kwargs)

    def bounded_archive_backup(path, done_dir, report):
        check_zip(path)
        return archive_backup(path, done_dir, report)

    def bounded_atomic_bytes(path, body):
        # Normalization writes its transcript before publication admission.
        # Apply the same payload/reserve limits at that earlier disk boundary.
        if Path(path).absolute().is_relative_to(importers.TRANSCRIPTS_DIR.absolute()):
            if len(body) > payload_limit or shutil.disk_usage(config.VAULT_ROOT).free < MIN_FREE_BYTES + len(body):
                raise ValueError(limit_error)
        return atomic_bytes(path, body)

    try:
        override(source_store, "MIN_FREE_BYTES", max(source_store.MIN_FREE_BYTES, MIN_FREE_BYTES))
        override(transcript_reader, "MAX_TRANSCRIPT_BYTES", payload_limit)
        override(transcript_reader, "MAX_NATIVE_RECORD_BYTES", record_limit)
        override(transcript_reader, "TRANSCRIPT_LIMIT_ERROR", limit_error)
        override(transcript_reader, "capture_native_transcript", bounded_capture)
        override(transcript_reader, "read_transcript", bounded_read)
        override(importers, "MAX_IMPORT_PAYLOAD_BYTES", payload_limit)
        override(import_publication, "publish", bounded_publish)
        override(importers, "import_zip", bounded_import_zip)
        override(importers, "_archive_backup", bounded_archive_backup)
        override(state_io, "atomic_bytes", bounded_atomic_bytes)
        result = importers.sweep()
        return 1 if result.get("problems") or result.get("failed") else 0
    finally:
        for module, name, previous in reversed(overrides):
            setattr(module, name, previous)


if __name__ == "__main__":
    sys.exit(main())
