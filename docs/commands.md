# Commands reference

The executable is `claude-note`. `python3 -m claude_note` uses the same entry point with `PYTHONPATH=src` in a checkout. Use `<command> --help` for the precise parser.

| Command | Supported options / behavior |
| --- | --- |
| `setup` | Index the vault and agent memories, install recall + sentinel hooks, add the recall rule to `CLAUDE.md`/`AGENTS.md`, schedule hourly refresh. `--codex`, `--dry-run`, `--no-claude-memory`, `--no-rules`, `--no-schedule`, `--add DIR`. |
| `doctor` | Configured / exercised / in use. Plants a canary in a throwaway QMD index and runs the real hooks. `--json`, `--skip-probe`. Exit 1 if a configured or exercised check fails. |
| `recall QUERY` | The recall hook's search, by hand. `-n N`, `-c COLLECTION` (repeatable), `--json`; `--stats [--days N]` for hook hit rate and latency. Exit 1 on a miss. |
| `remember TEXT` | Write a typed lesson and reindex. `--type gotcha|pattern|decision|reference|feedback|project`, `--title`, `--tags a,b`, `--why`, `--how`, `--folder`, `--no-index`; `-` reads stdin. Appends to an existing note. |
| `refresh` | `qmd update` then `qmd embed`, locked, bounded, with a heartbeat. `--no-embed`. |
| `hook recall` / `hook sentinel` | Hook handlers; read the host's JSON on stdin and print `additionalContext`. |
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

Automatic sweeps run every 30 minutes. Supported export zips in
`~/Documents/claude-note-imports/` or recognized download patterns are normalized.
Original archives remain in place; version-aware backup copies go to `done/`,
including a retry after a previous backup failure when no new sessions are queued.
A backup is separate from import completion and does not certify that all archive
members or conversations were captured. Backfills may queue model work.
Automation/subagent runs are excluded from interactive-session recovery.

Each newly published import binds immutable source bytes and a stable two-event
batch. Retrying an already published or captured revision queues zero new
sessions. Its canonical receipt, original source identities and protected
restrictions remain after acknowledged queue cleanup. The worker verifies every
imported reference before acknowledging local capture. A newer native hook can
resume the live transcript; replaying older imported work cannot replace it.
These guarantees cover local publication and capture, not exactly-once model or
external execution. Authored controls can only narrow captured restrictions;
current owner policy is still checked at egress. Legacy seen records migrate
without automatically reimporting history. New session-state fields are not
compatible with older readers; preserve state if rolling back. Text `status`
reports incomplete queue/state inspection and returns nonzero while retaining
unreadable records; healthy session processing continues. Snapshot verification
reads evidence before delivery, so local I/O grows with the pending backlog.
Session publication history is capped at 10,000 revisions without eviction.
Origin-only normalized replay comparisons have an 8 MiB per-record bound;
unsupported comparisons fail visibly while originals remain. Hook/import
ordering uses local event times and cannot resolve clock rollback across hooks.

Claude Code and Codex recovery isolate individual file failures: integer category
counts show successfully queued sessions, `problems` lists bounded failures, and
`failed` counts reported problems exactly. Other eligible files continue; failed
files retain their prior seen values for retry. Any problem leaves the last-clean
sweep heartbeat unchanged, so partial progress does not claim full success.
The source limit is 256 MiB and metadata prefix limit is 1 MiB. Sources and session
checkpoints are preserved; oversized transcripts need explicit lossless recovery,
which this command does not yet provide.

ZIP imports isolate each recognized `conversations*.json` member and each
conversation. A readable archive without a recognized member is an unrelated
skip; an unreadable selected archive or failed recognized member is a problem.
Each member has a 256 MiB declared and decompressed byte limit. Cursor isolates
each composer, with a 256 MiB bound on its JSON plus required bubble JSON. A
missing, malformed or unsupported required bubble prevents publication of that
composer, leaving it retryable while later composers continue. Full tool results
are retained in normalized evidence within this bound. Workspace association
failures are reported separately; otherwise valid composers can use the generic
`cursor` workspace and the attempt remains unclean. Later database or backup
failures preserve earlier successful publications and their seen values.

At most 100 payload-safe problem details are displayed per sweep;
`problem_details_omitted` gives the exact number omitted. `failed` counts problems,
which may concern files, members, conversations, composers, workspace metadata,
database scans or backups; it is not a count of every malformed record or a
complete source inventory. `reports` supplies counters with explicit units:
`archives_examined`, `members_examined`, `conversations_examined`,
`composers_examined`, and `queued_sessions`, with separate failed, unchanged,
skipped and deferred counters. Subsets overlap and are not additive denominators.
Direct helper callers without a problems sink receive `SourceImportError` with
the partial queued count, exact failure total and omitted-detail count; ordinary
list sinks receive an explicit omitted-details summary.

Capture remains a normalized view: ChatGPT keeps supported text on the selected
visible branch; Claude exports keep supported message text, thinking and tool
inputs; unsupported media and other branches are not normalized. Unknown
Downloads filenames, very recent archives, old composers, noninteractive
conversations and optional absent sources are reported as skips or deferrals
when examined. A clean heartbeat means no reported problem among attempted
eligible inputs, not that excluded or deferred history was captured. Lossless
streaming of oversized sources and recovery across every unsupported export
format remain pending; preserve originals and existing cursors.

## Shared push and receipts

```bash
claude-note push --dry-run
claude-note push --limit 10
claude-note push --install-agent
```

The adapter stages curated redacted notes to its configured shared inbox. It skips private/session/stub notes and unchanged successful receipts. API keys identify the configured destination; a local username or author field is not a server-side ownership guarantee.

Receipts are keyed by destination and vault. Nested paths use a stable hash suffix to avoid `folder/note.md` colliding with `folder__note.md`. Unknown-destination legacy receipts remain unbound; the CLI warns and defers unchanged content. After reviewing the target, `--resend-legacy` explicitly sends those notes once to that destination. It does not bypass redaction or sharing exclusions.

A clean push heartbeat records completion of the local push operation; it does not prove external merge or promotion. Inspect deferred and failed counts separately.
