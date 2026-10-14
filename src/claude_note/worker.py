#!/usr/bin/env python3
"""
Background worker daemon for claude-note.

Polls the queue and processes sessions when debounce expires.
"""

import argparse
import logging
import signal
import sys
import threading
import time
from datetime import datetime

from . import config
from . import models
from . import mutation
from . import queue_manager
from . import session_tracker
from . import note_writer
from . import open_questions
from . import synthesizer
from . import note_router
from . import vault_indexer
from . import importers
from . import version_checker


# Global flag for graceful shutdown
_shutdown = False


def setup_logging(verbose: bool = False) -> logging.Logger:
    """Configure logging for the worker."""
    config.LOGS_DIR.mkdir(parents=True, exist_ok=True)

    log_file = config.LOGS_DIR / f"worker-{datetime.utcnow().strftime('%Y-%m-%d')}.log"

    level = logging.DEBUG if verbose else logging.INFO

    # Configure root logger
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(log_file),
            logging.StreamHandler() if verbose else logging.NullHandler(),
        ],
    )

    return logging.getLogger("claude-note")


def handle_signal(signum, frame):
    """Handle shutdown signals gracefully."""
    global _shutdown
    _shutdown = True


def update_session_summary(state: models.SessionState, pack, logger: logging.Logger) -> bool:
    """Update generated summaries or preserve a review proposal on human edits."""
    import re

    try:
        note_path = note_writer.get_note_path(state)
        summary_lines = [f"**{pack.title}**", ""]
        if pack.highlights:
            summary_lines.extend(["Key outcomes:", *(f"- {item}" for item in pack.highlights), ""])
        if pack.concepts:
            summary_lines.append(f"Concepts: {', '.join(c.name for c in pack.concepts[:5])}")
        if pack.decisions:
            summary_lines.append(f"Decisions: {len(pack.decisions)}")
        if pack.open_questions:
            summary_lines.append(f"Open questions: {len(pack.open_questions)}")
        summary_text = "\n".join(summary_lines)
        def update(content):
            if content is None:
                return None
            placeholder = "(Updated on Stop/SessionEnd with session highlights)"
            if placeholder in content:
                return content.replace(placeholder, summary_text, 1)
            match = re.search(r"(## Summary\n\n).*?(\n\n## )", content, re.DOTALL)
            if match:
                return content[:match.start()] + match.group(1) + summary_text + match.group(2) + content[match.end():]
            return None
        result = mutation.mutate(note_path, update, vault_root=config.VAULT_ROOT, allow_protected=True,
                                 operation_id=mutation.operation_id('session-summary', state.session_id,
                                                                    getattr(pack, 'generation', ''), summary_text))
        if not result.applied:
            logger.warning("Session summary staged for review: %s", result.staged_path)
            return False
        if result.status != 'unchanged':
            logger.info("Updated session summary: %s", pack.title)
            return True
        return False
    except Exception as exc:
        logger.error("Failed to update session summary: %s", exc)
        return False


def run_synthesis(state: models.SessionState, logger: logging.Logger) -> bool:
    """
    Run synthesis for a session.

    Returns True if synthesis succeeded.
    """
    # Skip if mode is just logging
    if config.SYNTH_MODE == "log":
        return False

    # Skip if no transcript
    if not state.transcript_path:
        logger.debug(f"Session {state.session_id[:8]}: no transcript, skipping synthesis")
        return False

    try:
        # Get vault index
        vault_index = vault_indexer.get_index()

        # Run synthesis
        logger.info(f"Synthesizing session {state.session_id[:8]}...")
        pack = synthesizer.synthesize_from_state(state, vault_index)

        if pack is None:
            logger.warning(f"Session {state.session_id[:8]}: synthesis returned no result")
            return False
        if pack.is_empty():
            logger.info(f"Session {state.session_id[:8]}: no knowledge extracted")
            return True

        # Update session note summary with synthesis results
        update_session_summary(state, pack, logger)

        # Apply note ops
        results = note_router.apply_note_ops(pack, mode=config.SYNTH_MODE)

        # Log results
        if results["inbox_updated"]:
            logger.info(f"Updated inbox with {len(pack.concepts)} concepts, {len(pack.decisions)} decisions")
        if results["notes_created"]:
            logger.info(f"Created notes: {', '.join(results['notes_created'])}")
        if results["notes_updated"]:
            logger.info(f"Updated notes: {', '.join(results['notes_updated'])}")
        for staged in results.get("notes_staged", []):
            logger.warning("Note staged for review: %s (%s)", staged["pending_path"], staged["reason"])
        if results["errors"]:
            for err in results["errors"]:
                logger.warning(f"Synthesis error: {err}")

        return not results["errors"]

    except Exception as e:
        logger.error(f"Synthesis failed for session {state.session_id[:8]}: {e}")
        return False


def process_session(session_id: str, events: list, logger: logging.Logger) -> bool:
    """
    Process a single session.

    Returns True if note was written, False otherwise.
    """
    with session_tracker.session_lock(session_id) as acquired:
        if not acquired:
            logger.debug(f"Could not acquire lock for session {session_id[:8]}")
            return False

        try:
            previous = session_tracker.load_session_state(session_id)
            processed = set(previous.processed_event_ids) if previous else set()
            new_events = [event for event in events if event.event_id not in processed]
            # Update session state from events
            state = session_tracker.update_session_from_events(session_id, events)
            # Capture acknowledgement is independent of note/model completion.
            session_tracker.save_session_state(state)
            new_events = [event for event in new_events if event.event_id not in state.filtered_event_ids]

            # Skip synthesis/empty sessions (no user prompts)
            has_user_prompt = any(
                e.get("event") == "UserPromptSubmit"
                for e in state.events
            )
            if not has_user_prompt:
                session_tracker.save_session_state(state)
                logger.debug(f"Session {session_id[:8]}: no user prompt, skipping")
                return False

            # Check if we should write now
            immediate = session_tracker.should_flush_immediately(new_events)
            debounce_ok = state.should_write(config.DEBOUNCE_SECONDS)

            # Skip if already written after last event (even for immediate events)
            already_written = session_tracker.is_session_written(state)
            if already_written:
                if immediate:
                    # Queue delivery limits may separate Prompt and Stop. A
                    # note written between them does not consume Stop's request
                    # for synthesis; replayed/filtered Stops are excluded above.
                    session_tracker.schedule_synthesis(state)
                    session_tracker.save_session_state(state)
                session_tracker.run_pending_synthesis(state, lambda s: run_synthesis(s, logger))
                logger.debug(f"Session {session_id[:8]}: already written, skipping")
                return False

            if not immediate and not debounce_ok:
                # Save state but don't write note yet
                session_tracker.save_session_state(state)
                logger.debug(f"Session {session_id[:8]}: debounce not ready")
                return False

            # A proposal is durable work awaiting review, not a successful raw
            # note write. New events may supersede it; idle polls do not recreate
            # it or resynthesize a pack that already completed into proposals.
            if state.note_pending_path and not new_events:
                session_tracker.run_pending_synthesis(state, lambda s: run_synthesis(s, logger))
                return False

            # Write the note
            try:
                note_path = note_writer.update_session_note(state)
            except mutation.MutationConflict as conflict:
                state.note_pending_path = str(conflict.result.staged_path)
                state.note_pending_revision = conflict.result.revision or ""
                if immediate:
                    session_tracker.schedule_synthesis(state)
                session_tracker.save_session_state(state)
                logger.warning("Session note staged for review: %s", conflict.result.staged_path)
                session_tracker.run_pending_synthesis(state, lambda s: run_synthesis(s, logger))
                return False
            state.note_pending_path = ""
            state.note_pending_revision = ""
            logger.info(f"Wrote note: {note_path.name}")

            # Promote questions on Stop/SessionEnd
            if immediate:
                count = open_questions.promote_session_questions(state)
                if count > 0:
                    logger.info(f"Promoted {count} questions to open-questions.md")

                session_tracker.schedule_synthesis(state)

            # Mark as written (update state object directly, then save once)
            state.last_write_ts = datetime.utcnow().isoformat() + "Z"
            session_tracker.save_session_state(state)
            session_tracker.run_pending_synthesis(state, lambda s: run_synthesis(s, logger))

            return True

        except Exception as e:
            logger.error(f"Error processing session {session_id[:8]}: {e}")
            return False


def poll_once(logger: logging.Logger) -> int:
    """
    Process all pending sessions once.

    Returns number of notes written.
    """
    # Group events by session
    sessions: dict[str, list] = {}
    for event in queue_manager.read_pending_events():
        if event.session_id not in sessions:
            sessions[event.session_id] = []
        sessions[event.session_id].append(event)
    for session_id in set(session_tracker.get_pending_synthesis_sessions() + session_tracker.get_sessions_ready_for_write()):
        sessions.setdefault(session_id, [])

    notes_written = 0
    for session_id, events in sessions.items():
        if process_session(session_id, events, logger):
            notes_written += 1

    return notes_written


def use_private_cwd(logger=None):
    """Run from an empty directory under the vault, never from `/` (issue #3).

    launchd starts agents in `/`, and every `claude -p` the worker spawns inherits the working
    directory, so relative paths would resolve from the root of the disk. This covers launchd,
    systemd and old plists that were written before they set WorkingDirectory.
    """
    import os
    work = config.CLAUDE_NOTE_DIR / "tmp"
    try:
        work.mkdir(parents=True, exist_ok=True)
        os.chdir(work)
    except OSError as exc:  # never let this stop the worker
        if logger:
            logger.warning(f"could not change to {work}: {exc}")
        return None
    if logger:
        logger.info(f"Working directory: {work}")
    return work


def run_worker(foreground: bool = False, verbose: bool = False) -> int:
    """
    Run the worker daemon.

    Args:
        foreground: If True, log to stdout as well
        verbose: If True, enable debug logging
    """
    logger = setup_logging(verbose=verbose or foreground)
    logger.info("Worker starting")
    use_private_cwd(logger)

    # Check for updates (non-blocking)
    try:
        version_checker.check_for_update(logger)
    except Exception:
        pass  # Never let version check break worker

    # Set up signal handlers
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    # Sources without a hook (Cursor, export zips, untrusted Codex hooks) are
    # swept on a background thread so a slow disk never stalls the queue.
    sweeper = None
    sweep_started = 0.0

    # Main poll loop
    while not _shutdown:
        now = time.time()
        if sweeper is not None and sweeper.is_alive():
            if now - sweep_started > 600:
                logger.warning("Import sweep running for >10 min; abandoning it")
                sweeper = None
        elif now - sweep_started >= importers.SWEEP_INTERVAL:
            def _sweep():
                try:
                    counts = importers.sweep()
                    logger.info(f"Import sweep: {counts}")
                except Exception as e:
                    logger.error(f"Import sweep failed: {e}")
            sweeper = threading.Thread(target=_sweep, name="import-sweep", daemon=True)
            sweeper.start()
            sweep_started = now

        try:
            notes_written = poll_once(logger)
            if notes_written > 0:
                logger.debug(f"Poll cycle: wrote {notes_written} notes")

            # Clean up old queue files periodically (once per poll)
            queue_manager.cleanup_old_queue_files(keep_days=7)

        except Exception as e:
            logger.error(f"Error in poll cycle: {e}")

        # Sleep until next poll
        time.sleep(config.POLL_INTERVAL)

    logger.info("Worker shutting down")
    return 0


def main() -> int:
    """Main entry point for worker command."""
    parser = argparse.ArgumentParser(description="Claude Note background worker")
    parser.add_argument(
        "--foreground", "-f", action="store_true", help="Run in foreground with output"
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Enable verbose logging"
    )
    args = parser.parse_args()

    return run_worker(foreground=args.foreground, verbose=args.verbose)


if __name__ == "__main__":
    sys.exit(main())
