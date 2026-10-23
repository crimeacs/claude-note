"""Resource-supervised import sweep, separate from hook capture's heap."""

import shutil
import subprocess
import sys
import threading
import time

from . import config, importers

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
    def __init__(self, logger):
        self.logger = logger
        self.child = None
        self.started = 0.0
        self.last_attempt = None
        self.stopping = threading.Event()
        self.thread = None

    def start(self):
        self.thread = threading.Thread(target=self._supervise, name="bounded-import-supervisor")
        self.thread.start()

    def _supervise(self):
        try:
            while not self.stopping.is_set():
                self.tick()
                self.stopping.wait(.25)
        except Exception:
            self.logger.exception("Import supervisor failed; hook capture continues")
        finally:
            self.close()

    def shutdown(self):
        self.stopping.set()
        if self.thread is not None:
            self.thread.join()

    def tick(self):
        now = time.monotonic()
        if self.child is not None:
            code = self.child.poll()
            if code is not None:
                self.logger.info("Bounded import sweep exited: %s", code)
                self.child = None
                return
            try:
                reason = ("deadline" if now - self.started >= MAX_SECONDS else
                          "disk reserve" if shutil.disk_usage(config.VAULT_ROOT).free < MIN_FREE_BYTES else
                          "RSS limit" if rss_bytes(self.child.pid) > MAX_RSS_BYTES else "")
            except (OSError, ValueError, subprocess.TimeoutExpired):
                reason = "resource measurement unavailable"
            if reason:
                self.logger.warning("Stopping import sweep: %s; unfinished evidence retained", reason)
                self.close()
            return
        if self.last_attempt is not None and now - self.last_attempt < importers.SWEEP_INTERVAL:
            return
        self.last_attempt = now
        if shutil.disk_usage(config.VAULT_ROOT).free < MIN_FREE_BYTES:
            self.logger.warning("Import sweep deferred: preserving 6 GiB disk reserve")
            return
        self.child = subprocess.Popen([sys.executable, "-m", "claude_note.capture_sweep"],
                                      stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL, start_new_session=True)
        self.started = now

    def close(self):
        child = self.child
        if child is None:
            return
        # Keep ownership until wait() reaps this exact child. Atomic publication
        # leaves an interrupted intent replayable, without a false heartbeat.
        if child.poll() is None:
            try:
                child.terminate()
            except ProcessLookupError:
                pass
            try:
                child.wait(timeout=STOP_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=STOP_GRACE_SECONDS)
        else:
            child.wait()
        self.child = None


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
