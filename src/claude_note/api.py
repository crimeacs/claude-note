"""FastAPI wrapper for claude-note.

Exposes the enqueue/status/drain/health functionality as an HTTP API
and runs the background worker in a daemon thread.

Install with: pip install "claude-note[api]"
"""

import logging
import threading
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import __version__
from . import config
from . import models
from . import queue_manager
from . import session_tracker
from . import drain as drain_module
from . import worker as worker_module


# ---------------------------------------------------------------------------
# Worker thread management
# ---------------------------------------------------------------------------

_worker_thread: threading.Thread | None = None
_shutdown = threading.Event()


def _worker_loop() -> None:
    """Poll loop that runs in a daemon thread.

    This reimplements the core of worker.run_worker() but skips
    signal.signal() which only works on the main thread.
    """
    logger = worker_module.setup_logging(verbose=False)
    logger.info("Worker starting (API thread)")

    while not _shutdown.is_set():
        try:
            notes_written = worker_module.poll_once(logger)
            if notes_written > 0:
                logger.debug(f"Poll cycle: wrote {notes_written} notes")
            queue_manager.cleanup_old_queue_files(keep_days=7)
        except Exception as e:
            logger.error(f"Error in poll cycle: {e}")

        _shutdown.wait(timeout=config.POLL_INTERVAL)

    logger.info("Worker shutting down (API thread)")


def _start_worker() -> threading.Thread:
    """Start the poll-loop worker in a daemon thread."""
    _shutdown.clear()
    t = threading.Thread(
        target=_worker_loop,
        daemon=True,
        name="claude-note-worker",
    )
    t.start()
    return t


def _stop_worker(t: threading.Thread, timeout: float = 5.0) -> None:
    """Signal the worker to stop and wait for it to finish."""
    _shutdown.set()
    t.join(timeout=timeout)


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start worker on startup, stop on shutdown."""
    global _worker_thread
    config.ensure_dirs()
    _worker_thread = _start_worker()
    yield
    if _worker_thread is not None:
        _stop_worker(_worker_thread)


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="claude-note",
    version=__version__,
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    """Liveness check."""
    worker_alive = _worker_thread is not None and _worker_thread.is_alive()
    return {"status": "ok", "version": __version__, "worker_alive": worker_alive}


@app.post("/transcripts/{session_id}")
async def upload_transcript(session_id: str, request: Request):
    """Store transcript content uploaded by the Stop hook."""
    body = await request.body()
    dest = config.TRANSCRIPTS_DIR / f"{session_id}.jsonl"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(body)
    return {"stored": True, "size": len(body)}


@app.post("/events")
async def post_event(request: Request):
    """Enqueue a hook event (replaces stdin-based enqueue)."""
    body = await request.json()

    # Validate minimum required fields
    if "session_id" not in body:
        return JSONResponse(
            status_code=422,
            content={"detail": "session_id is required"},
        )

    event = models.QueuedEvent.from_hook_input(body)
    queue_manager.enqueue_event(event)

    return {"event_id": event.event_id, "session_id": event.session_id}


@app.get("/status")
async def status():
    """Queue / session / vault info as JSON."""
    total_events = 0
    queue_files = []
    total_size = 0

    if config.QUEUE_DIR.exists():
        queue_files = sorted(config.QUEUE_DIR.glob("*.jsonl"))
        for qf in queue_files:
            total_size += qf.stat().st_size

    sessions: dict[str, int] = {}
    for event in queue_manager.read_all_events():
        sessions[event.session_id] = sessions.get(event.session_id, 0) + 1
        total_events += 1

    written = 0
    pending = 0
    for sid in sessions:
        state = session_tracker.load_session_state(sid)
        if state and state.last_write_ts:
            written += 1
        else:
            pending += 1

    return {
        "version": __version__,
        "vault_root": str(config.VAULT_ROOT),
        "synth_mode": config.SYNTH_MODE,
        "queue": {
            "files": len(queue_files),
            "total_bytes": total_size,
            "events": total_events,
        },
        "sessions": {
            "total": len(sessions),
            "written": written,
            "pending": pending,
        },
        "worker_alive": _worker_thread is not None and _worker_thread.is_alive(),
    }


@app.post("/drain")
async def drain():
    """One-shot: process all pending sessions immediately."""
    sessions_processed, notes_written = drain_module.drain_all()
    return {
        "sessions_processed": sessions_processed,
        "notes_written": notes_written,
    }
