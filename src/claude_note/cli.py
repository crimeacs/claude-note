#!/usr/bin/env python3
"""
Main CLI entry point for claude-note.

Commands:
    enqueue   - Hook handler (reads stdin)
    worker    - Background daemon
    drain     - One-shot processing
    status    - Show queue/session status
    resynth   - Re-run synthesis for a session
    index     - Rebuild vault index
    clean     - Daily cleanup operations
"""

import argparse
import sys
import time
from datetime import datetime

from . import config
from . import queue_manager
from . import session_tracker
from . import note_router
from . import vault_indexer
from . import cleaner
from . import enqueue as enqueue_module
from . import worker as worker_module
from . import drain as drain_module
from . import synthesizer


def cmd_enqueue(args) -> int:
    """Handle enqueue command."""
    return enqueue_module.main()


def cmd_worker(args) -> int:
    """Handle worker command."""
    return worker_module.run_worker(foreground=args.foreground, verbose=args.verbose)


def cmd_drain(args) -> int:
    """Handle drain command."""
    return drain_module.main()


def cmd_status(args) -> int:
    """Handle status command - show queue and session status."""
    print("=== Claude Note Status ===\n")

    # Synthesis mode
    print(f"Synthesis mode: {config.SYNTH_MODE}")
    print(f"Synthesis model: {config.SYNTH_MODEL}")
    print(f"Vault root: {config.VAULT_ROOT}\n")

    # Queue status
    print("Queue files:")
    if config.QUEUE_DIR.exists():
        queue_files = sorted(config.QUEUE_DIR.glob("*.jsonl"))
        if queue_files:
            for qf in queue_files[-5:]:  # Show last 5
                size = qf.stat().st_size
                print(f"  {qf.name}: {size} bytes")
        else:
            print("  (none)")
    else:
        print("  (queue directory does not exist)")

    # Count events by session
    print("\nSessions:")
    sessions: dict[str, int] = {}
    for event in queue_manager.read_all_events():
        sessions[event.session_id] = sessions.get(event.session_id, 0) + 1

    if sessions:
        for session_id, count in sorted(sessions.items()):
            state = session_tracker.load_session_state(session_id)
            status = "unknown"
            if state:
                if state.last_write_ts:
                    status = f"written at {state.last_write_ts[:19]}"
                else:
                    status = f"pending ({len(state.events)} events in state)"
            print(f"  {session_id[:8]}: {count} queued events, {status}")
    else:
        print("  (none)")

    # State files
    print("\nState files:")
    if config.STATE_DIR.exists():
        state_files = list(config.STATE_DIR.glob("*.json"))
        print(f"  {len(state_files)} session state files")
        lock_files = list(config.STATE_DIR.glob("*.lock"))
        print(f"  {len(lock_files)} lock files")
    else:
        print("  (state directory does not exist)")

    # Vault index status
    print("\nVault index:")
    if config.INDEX_PATH.exists():
        index = vault_indexer.load_index()
        if index:
            age = time.time() - index.last_full_scan
            print(f"  {len(index.notes)} notes indexed")
            print(f"  Last scan: {int(age)}s ago")
        else:
            print("  (could not load index)")
    else:
        print("  (not built yet)")

    # Import sweep heartbeat (Cursor, export zips, Codex rollouts)
    from . import importers
    print("\nImport sweep:")
    if importers.HEARTBEAT.exists():
        age = time.time() - importers.HEARTBEAT.stat().st_mtime
        stale = "  STALE: is the worker running?" if age > 2 * importers.SWEEP_INTERVAL + 600 else ""
        print(f"  last ran {int(age // 60)} min ago{stale}")
        try:
            import json
            for problem in json.loads(importers.HEARTBEAT.read_text())["counts"].get("problems", []):
                print(f"  PROBLEM: {problem}")
        except (OSError, ValueError, KeyError):
            pass
    else:
        print("  (never ran)")

    # Inbox status
    print("\nInbox:")
    if config.INBOX_PATH.exists():
        entries = note_router.get_inbox_entries(limit=3)
        print(f"  {len(entries)} recent entries")
        for e in entries:
            print(f"    - {e['date']}: {e['title']}")
    else:
        print("  (not created yet)")

    return 0


def cmd_import(args) -> int:
    """Sweep Cursor, export zips and Codex rollouts now (the worker also does this)."""
    from . import importers
    since = datetime.strptime(args.since, "%Y-%m-%d") if args.since else None
    counts = importers.sweep(since=since, zips=args.zips)
    for source, count in counts.items():
        print(f"  {source}: {count}")
    print("Queued sessions are written by the worker (or run: claude-note drain).")
    return 0


CODEX_HOOK_EVENTS = {"UserPromptSubmit": 5, "Stop": 5, "SessionEnd": 3}


def install_codex_hooks(hooks_path, command: str) -> bool:
    """Add `claude-note enqueue` to Codex's hooks.json (backup first). Idempotent."""
    import json
    import shutil
    from pathlib import Path

    hooks_path = Path(hooks_path)
    data = json.loads(hooks_path.read_text()) if hooks_path.exists() else {}
    hooks = data.setdefault("hooks", {})
    changed = False
    for event, timeout in CODEX_HOOK_EVENTS.items():
        groups = hooks.setdefault(event, [])
        if any("claude-note enqueue" in h.get("command", "") for g in groups for h in g.get("hooks", [])):
            continue
        groups.append({"hooks": [{"type": "command", "command": command, "timeout": timeout}]})
        changed = True
    if changed:
        if hooks_path.exists():
            shutil.copy2(hooks_path, hooks_path.with_name(f"hooks.json.bak-{datetime.now():%Y%m%d-%H%M%S}"))
        hooks_path.parent.mkdir(parents=True, exist_ok=True)
        hooks_path.write_text(json.dumps(data, indent=2) + "\n")
    return changed


def cmd_install_codex_hooks(args) -> int:
    import shutil
    from pathlib import Path

    exe = shutil.which("claude-note") or "claude-note"
    path = Path(args.hooks_file).expanduser()
    if install_codex_hooks(path, f"{exe} enqueue"):
        print(f"Added claude-note hooks to {path} (any previous file is backed up alongside).")
    else:
        print(f"{path} already has the claude-note hooks.")
    print("Codex skips new hooks until trusted: open the ChatGPT/Codex app and approve them")
    print("in its hooks review, or run `codex` once in a terminal and accept the trust prompt.")
    return 0


def cmd_resynth(args) -> int:
    """Handle resynth command - re-run synthesis for a session."""
    session_id = args.session_id
    model = args.model

    print(f"Re-synthesizing session {session_id[:8]}...")
    if model:
        print(f"Using model: {model}")

    try:
        pack = synthesizer.resynthesize_session(session_id, model=model)

        if pack is None or pack.is_empty():
            print("No knowledge extracted from session.")
            return 0

        print(f"Extracted:")
        print(f"  Title: {pack.title}")
        print(f"  Highlights: {len(pack.highlights)}")
        print(f"  Concepts: {len(pack.concepts)}")
        print(f"  Decisions: {len(pack.decisions)}")
        print(f"  Open questions: {len(pack.open_questions)}")
        print(f"  How-tos: {len(pack.howtos)}")
        print(f"  Note ops: {len(pack.note_ops)}")

        # Apply based on mode (or use explicit mode from args)
        mode = args.mode if args.mode else config.SYNTH_MODE
        if mode == "log":
            mode = "inbox"  # For resynth, at least write to inbox

        print(f"\nApplying with mode: {mode}")
        results = note_router.apply_note_ops(pack, mode=mode)

        if results["inbox_updated"]:
            print("  Updated inbox")
        if results["notes_created"]:
            print(f"  Created: {', '.join(results['notes_created'])}")
        if results["notes_updated"]:
            print(f"  Updated: {', '.join(results['notes_updated'])}")
        if results["errors"]:
            print(f"  Errors: {', '.join(results['errors'])}")

        return 0

    except Exception as e:
        print(f"Error: {e}")
        return 1


def cmd_index(args) -> int:
    """Handle index command - rebuild vault index."""
    print("Rebuilding vault index...")

    index = vault_indexer.build_index()
    vault_indexer.save_index(index)

    summary = vault_indexer.get_index_summary()

    print(f"Indexed {summary['total_notes']} notes")
    print(f"Found {summary['unique_tags']} unique tags")
    print("\nTop tags:")
    for tag, count in summary['top_tags'][:10]:
        print(f"  #{tag}: {count}")

    return 0


def cmd_clean(args) -> int:
    """Handle clean command - daily cleanup operations."""
    # Determine what to clean
    clean_all = args.all
    clean_state = args.state or clean_all
    clean_sessions = args.sessions or clean_all
    clean_inbox = args.inbox or clean_all
    clean_topics = args.topics or clean_all

    # If nothing specified, default to all
    if not any([args.state, args.sessions, args.inbox, args.topics, args.all]):
        clean_state = clean_sessions = clean_inbox = clean_topics = True

    # Run cleanup
    results = cleaner.run_daily_clean(
        date=args.date,
        dry_run=not args.execute,
        clean_state=clean_state,
        clean_sessions=clean_sessions,
        clean_inbox=clean_inbox,
        clean_topics=clean_topics,
    )

    # Format and print results
    output = cleaner.format_clean_results(results)
    print(output)

    if not args.execute:
        print("\nThis was a dry-run. Use --execute (-x) to apply changes.")

    return 0


def cmd_ingest(args) -> int:
    """Handle ingest command - ingest external research documents."""
    from . import ingest
    return ingest.main(args)


def main() -> int:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Claude Note - session logging for Claude Code",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # enqueue command
    enqueue_parser = subparsers.add_parser(
        "enqueue", help="Hook handler (reads JSON from stdin)"
    )
    enqueue_parser.set_defaults(func=cmd_enqueue)

    # worker command
    worker_parser = subparsers.add_parser("worker", help="Background daemon")
    worker_parser.add_argument(
        "--foreground", "-f", action="store_true", help="Run in foreground"
    )
    worker_parser.add_argument(
        "--verbose", "-v", action="store_true", help="Verbose logging"
    )
    worker_parser.set_defaults(func=cmd_worker)

    # drain command
    drain_parser = subparsers.add_parser(
        "drain", help="One-shot processing (ignore debounce)"
    )
    drain_parser.set_defaults(func=cmd_drain)

    # status command
    status_parser = subparsers.add_parser("status", help="Show queue/session status")
    status_parser.set_defaults(func=cmd_status)

    # resynth command
    resynth_parser = subparsers.add_parser(
        "resynth", help="Re-run synthesis for a session"
    )
    resynth_parser.add_argument(
        "session_id", help="Session ID (full or prefix)"
    )
    resynth_parser.add_argument(
        "--mode", "-m", choices=["inbox", "route"],
        help="Override synthesis mode"
    )
    resynth_parser.add_argument(
        "--model", help="Override model (e.g., claude-opus-4-20250514)"
    )
    resynth_parser.set_defaults(func=cmd_resynth)

    # index command
    index_parser = subparsers.add_parser(
        "index", help="Rebuild vault index"
    )
    index_parser.set_defaults(func=cmd_index)

    # clean command
    clean_parser = subparsers.add_parser(
        "clean", help="Daily cleanup (dedupe inbox, compress timelines, etc.)"
    )
    clean_parser.add_argument(
        "--date", "-d",
        help="Date to clean (YYYY-MM-DD, default: today)"
    )
    clean_parser.add_argument(
        "--execute", "-x", action="store_true",
        help="Actually execute changes (default: dry-run)"
    )
    clean_parser.add_argument(
        "--state", action="store_true",
        help="Clean state directory (orphan locks, old sessions)"
    )
    clean_parser.add_argument(
        "--sessions", action="store_true",
        help="Compress session timelines"
    )
    clean_parser.add_argument(
        "--inbox", action="store_true",
        help="Deduplicate inbox entries"
    )
    clean_parser.add_argument(
        "--topics", action="store_true",
        help="Consolidate redundant blocks in topic notes"
    )
    clean_parser.add_argument(
        "--all", "-a", action="store_true",
        help="Run all cleanup types"
    )
    clean_parser.set_defaults(func=cmd_clean)

    # import command
    import_parser = subparsers.add_parser(
        "import", help="Pick up Cursor chats, Claude/ChatGPT export zips, Codex rollouts now"
    )
    import_parser.add_argument("zips", nargs="*", help="Export zip(s) to import explicitly")
    import_parser.add_argument("--since", help="Backfill sessions updated since YYYY-MM-DD")
    import_parser.set_defaults(func=cmd_import)

    # install-codex-hooks command
    codex_parser = subparsers.add_parser(
        "install-codex-hooks", help="Add claude-note to ~/.codex/hooks.json (Codex CLI and ChatGPT app)"
    )
    codex_parser.add_argument("--hooks-file", default="~/.codex/hooks.json")
    codex_parser.set_defaults(func=cmd_install_codex_hooks)

    # ingest command
    ingest_parser = subparsers.add_parser(
        "ingest", help="Ingest external research (papers, docs) as lit-* notes"
    )
    ingest_parser.add_argument(
        "file", help="Document to ingest (.pdf, .docx, .md, .txt)"
    )
    ingest_parser.add_argument(
        "--title", "-t", help="Override title (default: filename)"
    )
    ingest_parser.add_argument(
        "--model", "-m", help="Override Claude model"
    )
    ingest_parser.add_argument(
        "--dry-run", "-n", action="store_true",
        help="Extract knowledge but don't create notes"
    )
    ingest_parser.add_argument(
        "--internal", "-i", action="store_true",
        help="Internal mode: create int-* notes in internal/ (default: lit-* in literature/)"
    )
    ingest_parser.set_defaults(func=cmd_ingest)

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 1

    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
