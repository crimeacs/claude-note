# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

claude-note is a background service that captures Claude Code sessions and synthesizes knowledge into an Obsidian vault. It runs as a daemon, watching for hook events and processing them into structured notes.

## Development Commands

```bash
# Install for development (editable)
uv tool install --force .

# Run CLI directly during development
uv run claude-note status
uv run claude-note worker --foreground --verbose

# Reinstall after changes
uv tool install --force --reinstall .
```

## Release Process

```bash
# 1. Bump version in src/claude_note/__init__.py
# 2. Commit and push
git add -A && git commit -m "Bump to vX.Y.Z" && git push fork main

# 3. Tag and push - triggers auto-release via GitHub Actions
git tag vX.Y.Z && git push fork vX.Y.Z
```

## Architecture

### Data Flow

```
Claude Code Hook → enqueue.py → queue/*.jsonl → worker.py → synthesizer.py → vault notes
```

1. **Hooks** fire on Claude Code events (PostToolUse, UserPromptSubmit, Stop)
2. **enqueue.py** receives JSON via stdin, writes to daily queue files
3. **worker.py** polls queue, groups events by session, applies debounce
4. **synthesizer.py** calls Claude CLI to extract knowledge from transcripts
5. **note_router.py** writes to inbox or routes to specific notes

### Key Data Structures

- **QueuedEvent** (`models.py`): Single hook event with session_id, transcript_path
- **SessionState** (`models.py`): Tracks processing state, debounce timing
- **KnowledgePack** (`knowledge_pack.py`): Structured extraction output (concepts, decisions, questions, how-tos)

### Module Responsibilities

| Module | Purpose |
|--------|---------|
| `cli.py` | Command dispatch, argument parsing |
| `worker.py` | Background daemon, poll loop, synthesis trigger |
| `enqueue.py` | Hook handler, stdin → queue file |
| `synthesizer.py` | Claude API calls for knowledge extraction |
| `note_router.py` | Route KnowledgePack to vault notes |
| `ingest.py` | PDF/DOCX ingestion into literature notes |
| `version_checker.py` | GitHub releases API for update notifications |

### Storage Locations

All state lives in `{vault}/.claude-note/`:
- `queue/YYYY-MM-DD.jsonl` - Daily event queues
- `state/{session_id}.json` - Session processing state
- `state/{session_id}.lock` - File locks for concurrent access
- `logs/worker-*.log` - Worker logs

## Design Decisions

- **Pure stdlib**: No runtime dependencies (except Claude CLI for synthesis)
- **File-based queue**: Simple JSONL files, no database
- **Debounce**: Wait 15s after last event before writing notes
- **Synthesis modes**: `log` (just log), `inbox` (safe), `route` (full)
- **Version from `__init__.py`**: pyproject.toml uses dynamic versioning via hatch
