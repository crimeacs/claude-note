# Claude Note

A shared knowledge loop for your AI assistants: capture work, extract reusable lessons, keep typed Markdown notes, and retrieve them in the next session.

Claude Note began as a Claude Code → Obsidian logger. This fork now includes multi-assistant imports, author and assistant provenance, safe note routing, bounded QMD retrieval, health reporting, and optional curated sharing with a Sweat AI vault. The package and command remain `claude-note`; existing `foresyn` API and configuration names remain technical identifiers.

It runs locally with Python's standard library. Synthesis invokes your installed Claude CLI; selected transcript content and retrieved source excerpts go to that CLI's configured model service. Obsidian is optional.

## What it does

| Stage | Behavior |
| --- | --- |
| Capture | Claude Code hooks, Codex hooks and rollout recovery, read-only Cursor import, Claude and ChatGPT export imports. |
| Synthesize | Extract conclusions, tool outcomes, concepts, decisions, questions, and procedures; keep proposals and verified results distinct. |
| Organize | Typed durable notes, separate `sessions/` evidence, source backlinks, managed updates, exact-content deduplication, and vault-confined Markdown paths. |
| Retrieve | Optional collection-scoped QMD search, current and legacy JSON support, bounded subprocesses, and local source verification. BM25 is the synthesis default. |
| Share | Optional push of eligible, redacted notes into a configured shared vault inbox. Upload is staging; remote consolidation and approval are separate. |
| Operate | JSON health report, import and push heartbeats, installed-source provenance, and reproducible checkout updates. |

[Current system architecture](docs/current-system.md) explains the wider memory system, including integrations that are **external** to this package. [Development notes](docs/developments.md) records the changes in this modernization.

## Install

Requirements: Python 3.11+, git, and [uv](https://docs.astral.sh/uv/). Install and authenticate the [Claude CLI](https://code.claude.com/docs/en/overview) for synthesis, or use `log` mode. QMD and document converters are optional.

```bash
git clone https://github.com/crimeacs/claude-note.git ~/src/claude-note
cd ~/src/claude-note
scripts/install-from-checkout.sh --non-interactive \
  --vault ~/Documents/claude-notes --author you@example.com --claude-hooks
```

`--install-uv` explicitly allows the installer to fetch uv if absent. Existing configuration and vault files are preserved. The checkout installer sets up the macOS worker; Linux users install the package with uv and use the [systemd template](docs/service-setup.md), or use the interactive `./install.sh`.

Optional setup:

```bash
claude-note install-codex-hooks
claude-note status --json
```

A hook file is configuration evidence. Verify that the host trusts it and that an event reaches the queue before claiming automatic capture. Import sweeps can recover missed sessions independently. See [hook setup](docs/hook-setup.md).

### Update an existing installation

```bash
cd ~/src/claude-note
git pull --ff-only
scripts/install-from-checkout.sh --claude-hooks --codex-hooks
```

The installer records the checkout path and commit in `~/.local/share/claude-note/installed-from.json`. Source-managed installations keep that source: `claude-note update` directs you to the original checkout or bundled app rather than replacing it with another fork. Unmanaged release installs use this repository's releases.

## Capture sources

| Source | Capture path |
| --- | --- |
| Claude Code | `PostToolUse`, `UserPromptSubmit`, and `Stop` hooks; recovery sweep of recent interactive sessions. |
| Codex CLI / desktop | Installed hooks where the host supports and trusts them; rollout recovery sweep. Automation and subagent threads are excluded. |
| Cursor | Worker reads supported local `state.vscdb` chat formats without writing the database. |
| Claude desktop / web, ChatGPT web | Export conversation data and supply the zip explicitly or place it in the import folder. No account scraping. |

The worker sweeps every 30 minutes. The initial sweep looks back seven days; recent Claude Code recovery is capped at ten sessions per sweep. Each synthesized session invokes the configured model. A changed conversation may be imported again; unchanged content is skipped.

```bash
claude-note import
claude-note import ~/Downloads/export.zip
claude-note import --since 2026-01-01
```

## Configuration

`~/.config/claude-note/config.toml` (or `$XDG_CONFIG_HOME/claude-note/config.toml`):

```toml
vault_root = "/absolute/path/to/notes"
author = "you@example.com"

[synthesis]
mode = "inbox"             # log | inbox | route
timeout = 300              # seconds per synthesis process

[qmd]
enabled = true
collection = "notes"       # register this exact vault in QMD first
search_mode = "keyword"    # vector is explicit opt-in
ingest_dedup_enabled = false # opt in for semantic ingestion candidates
synth_max_notes = 5
qmd_timeout = 10
```

Start with `inbox` to inspect extraction quality, then choose `route` for automatic note operations. The code default is `route`. Without a configured QMD collection, synthesis uses the local note index and injects no global QMD results.

```bash
qmd collection add ~/Documents/claude-notes --name notes
qmd update
qmd search "retry timeout" -c notes -n 5 --json
# Optional, needed for vector mode:
qmd embed
```

`claude-note index` rebuilds the package's local note index. `qmd update` refreshes QMD text search; `qmd embed` refreshes vectors. These are separate operations. See [configuration](docs/configuration.md) and [QMD integration](docs/qmd-integration.md).

## Notes and provenance

```yaml
---
title: Retry after a lost response
type: gotcha
assistant: codex
author: you@example.com
tags: [reliability, retries]
---
```

Set `type` explicitly: a failure lesson is a `gotcha`, a reusable method a `pattern`, and an accepted choice a `decision`. Missing or invalid synthesis types receive an observable fallback; tags are topical metadata, not a substitute for meaning. [Vault workflow](vault-template/obsidian-workflow.md) describes types, sources, and graph use.

`assistant` records the producer (`claude-code`, `codex`, `cursor`, `claude-app`, or `chatgpt`). `author` resolves from `CLAUDE_NOTE_AUTHOR`, config, the optional Foresyn config email, then global git email. Neither is an access-control or approval claim.

```text
notes/
├── .claude-note/           # local queues, session state, logs, local note index
├── sessions/              # new session records; old root records stay usable
├── literature/            # external source and concept notes
├── internal/              # internal document ingestion
├── templates/             # typed writing templates
├── claude-note-inbox.md
└── open-questions.md
```

## Optional shared-vault push

Push requires an explicitly configured `~/.foresyn/config.json`. It stages supported curated types (`pattern`, `gotcha`, `decision`, `reference`, `project`, `literature`), excluding sessions, private folders, `share: false`, and empty placeholders. Recognized secrets are redacted before sending. Redaction does not decide whether business information is confidential.

```bash
claude-note push --dry-run
claude-note push --limit 10
claude-note push --install-agent   # optional macOS daily job at 03:00 local time
```

Receipts are scoped to the destination and local vault. Nested source paths have collision-resistant remote names. Old receipts whose destination is unknown are retained without automatic resending; review the configured target and use `claude-note push --resend-legacy` explicitly when appropriate. See [commands](docs/commands.md).

## Operate and develop

```bash
claude-note status --json
claude-note drain
claude-note clean --all             # dry run
claude-note resynth SESSION_PREFIX --mode inbox
claude-note ingest paper.pdf --dry-run
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

The JSON health report works before configuration, makes bounded local probes without network calls, and exits 1 if its `problems` list is nonempty. A running worker alone does not prove synthesis, sharing, or external consolidation succeeded.

| Guide | Scope |
| --- | --- |
| [Getting started](docs/getting-started.md) | Install and verify the first capture. |
| [Commands](docs/commands.md) / [configuration](docs/configuration.md) | Actual supported interfaces. |
| [Architecture](docs/architecture.md) / [current system](docs/current-system.md) | Implementation and integration boundaries. |
| [Hooks](docs/hook-setup.md) / [services](docs/service-setup.md) | Capture and worker operation. |
| [Synthesis modes](docs/synthesis-modes.md) / [ingestion](docs/document-ingestion.md) | Knowledge production and review. |
| [QMD](docs/qmd-integration.md) / [troubleshooting](docs/troubleshooting.md) | Retrieval and failure diagnosis. |

MIT licensed. Original project by [artemiin](https://github.com/artemiin/claude-note); this fork maintains the developments described here.
