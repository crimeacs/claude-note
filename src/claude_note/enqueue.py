#!/usr/bin/env python3
"""
Fast hook handler for claude-note.

Reads hook event from stdin and appends to queue.
Must be FAST - exits immediately, even on errors.
"""

import json
import sys

from . import models
from . import queue_manager


def main() -> int:
    """Main entry point for enqueue command."""
    try:
        # Read JSON from stdin
        raw_input = sys.stdin.read()
        if not raw_input.strip():
            # No input is fine, just exit
            return 0

        hook_data = json.loads(raw_input)

        # Codex subagent threads and `codex exec` automation are not sessions
        # a person had; they would each become a note.
        transcript = hook_data.get("transcript_path") or ""
        if "/.codex/" in transcript:
            from .transcript_reader import codex_session_meta
            if codex_session_meta(transcript).get("background"):
                return 0

        # Create event and enqueue
        event = models.QueuedEvent.from_hook_input(hook_data)
        queue_manager.enqueue_event(event)

        return 0

    except json.JSONDecodeError as e:
        # Log to stderr but don't fail the hook
        print(f"claude-note: JSON decode error: {e}", file=sys.stderr)
        return 0

    except Exception as e:
        # Log to stderr but don't fail the hook
        print(f"claude-note: Error: {e}", file=sys.stderr)
        return 0


if __name__ == "__main__":
    sys.exit(main())
