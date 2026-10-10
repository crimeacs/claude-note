# Commands reference

The executable is `claude-note`. `python3 -m claude_note` uses the same entry point with `PYTHONPATH=src` in a checkout. Use `<command> --help` for the precise parser.

| Command | Supported options / behavior |
| --- | --- |
| `status` | Human-readable queue/index view. `--json` provides bounded local health and works before configuration; exit 1 on problems. |
| `enqueue` | Reads the host's event JSON from stdin. No positional event or session arguments. |
| `worker` | `--foreground`, `--verbose`; background event processing and periodic import sweep. |
| `drain` | Process pending sessions without waiting for debounce. No dry-run or verbose flags. |
| `import [zip ...]` | Run recovery/export sweep now. `--since YYYY-MM-DD` permits older backfill. |
| `index` | Rebuild Claude Note's local routing index; does not refresh QMD. |
| `resynth SESSION` | Full ID or prefix; `--mode inbox|route`, `--model MODEL`. In log mode explicit resynthesis uses inbox. |
| `clean` | Preview by default; `--execute` applies. Select `--state`, `--sessions`, `--inbox`, `--topics`, or `--all`; optional `--date YYYY-MM-DD`. |
| `ingest FILE` | `.pdf`, `.docx`, `.md`, `.txt`; `--title`, `--model`, `--dry-run`, `--internal`. |
| `install-claude-hooks` | Merge hooks into settings with backup; `--settings-file PATH`. |
| `install-codex-hooks` | Merge hooks with backup; `--hooks-file PATH`. Host trust may still be required. |
| `push` | `--dry-run`, `--limit N`, `--resend-legacy`, `--install-agent`. Requires a configured shared destination. |
| `update` | Unmanaged release update from this fork; `--no-restart`. Source-managed installs report their original update path instead. |

## Import and capture

```bash
claude-note install-claude-hooks
claude-note install-codex-hooks
claude-note import
claude-note import ~/Downloads/export.zip
claude-note import --since 2026-01-01
```

Automatic sweeps run every 30 minutes. Supported export zips in `~/Documents/claude-note-imports/` or recognized download patterns are normalized; processed exports move to `done/`. Backfills may queue model work. Automation/subagent runs are excluded from interactive-session recovery.

## Shared push and receipts

```bash
claude-note push --dry-run
claude-note push --limit 10
claude-note push --install-agent
```

The adapter stages curated redacted notes to its configured shared inbox. It skips private/session/stub notes and unchanged successful receipts. API keys identify the configured destination; a local username or author field is not a server-side ownership guarantee.

Receipts are keyed by destination and vault. Nested paths use a stable hash suffix to avoid `folder/note.md` colliding with `folder__note.md`. Unknown-destination legacy receipts remain unbound; the CLI warns and defers unchanged content. After reviewing the target, `--resend-legacy` explicitly sends those notes once to that destination. It does not bypass redaction or sharing exclusions.

A clean push heartbeat records completion of the local push operation; it does not prove external merge or promotion. Inspect deferred and failed counts separately.
