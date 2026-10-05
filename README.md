# Claude Note

Automatic knowledge extraction from Claude Code sessions into your Obsidian vault.

Claude Note runs as a background service, watching your Claude Code sessions and synthesizing key learnings, decisions, and questions into structured notes.

## Features

- **Session Logging**: Automatically captures Claude Code sessions as markdown notes
- **Knowledge Synthesis**: Uses Claude to extract key concepts, code patterns, and learnings
- **Smart Routing**: Routes synthesized knowledge to your inbox, specific notes, or creates new ones
- **Open Questions Tracking**: Detects and tracks questions that come up during sessions
- **Vault Integration**: Understands your existing notes for better context

## Requirements

- Python 3.11+ (for built-in `tomllib`)
- [Claude CLI](https://github.com/anthropics/claude-cli) (for knowledge synthesis)
- An Obsidian vault (or any markdown-based notes system)

## Quick Start

```bash
# Clone and install
git clone https://github.com/crimeacs/claude-note.git
cd claude-note
./install.sh
```

The installer will:
1. Check dependencies
2. Ask for your vault path
3. Set up the background service
4. Print instructions for Claude Code hook configuration

### Updating an existing install from a checkout

`install.sh` is for first-time setup: it asks questions and rewrites
`config.toml`. To put a checkout's code on a machine that is already set up
(every laptop after the first install, or after `git pull`):

```bash
cd ~/src/claude-note && git pull
scripts/install-from-checkout.sh --push-agent --codex-hooks
```

It installs the checkout with `uv tool install --reinstall`, restarts the
worker if it is loaded, and records the path and commit in
`~/.local/share/claude-note/installed-from.json`. It never touches the config,
the vault or the worker plist. `--push-agent` (re)installs the daily push job,
`--claude-hooks` the Claude Code hooks and `--codex-hooks` the Codex hooks; all
are idempotent. It refuses a checkout under a temp directory (it vanishes on
reboot and nobody can tell what is running) and uncommitted changes in `src/`
(`--allow-temp`, `--allow-dirty` override).

### Installing from another program (no Terminal, no questions)

The same script sets up a new machine when run with `--non-interactive`, from
a git checkout or from a plain copy of the source (the Sweat app bundles one;
a copy records its ref in a `SOURCE_REF` file, or pass `--source-ref`):

```bash
scripts/install-from-checkout.sh --non-interactive --install-uv \
    --vault ~/Documents/claude-notes --author you@company.com \
    --push-agent --claude-hooks
```

It closes stdin and never prompts. It writes `config.toml` only if there is
none, creates the vault from `vault-template/` and installs and starts the
worker LaunchAgent only if missing, and sets `author` in `config.toml` when
`--author` is given. `--install-uv` fetches uv with astral's standalone
installer into `~/.local/bin` (shell profiles untouched) when it is missing;
without it a missing uv exits 3. Exit codes: 0 installed, 1 failed, 2 bad
option, 3 missing prerequisite.

Afterwards, `claude-note status --json` prints one health report (config,
author, worker and push agents, last clean push, import sweep, hooks, which of
`uv`, `claude`, `qmd` and `foresyn` are installed, `notes` (session logs,
knowledge notes, how many the push will share and how many it has sent),
`synthesis` (failures in the newest worker log), and a `problems` list). It
works before anything is configured, needs no network, and exits 1 when
`problems` is not empty.

### Where a note came from

Every note claude-note writes carries `assistant:` (`claude-code`, `codex`,
`cursor`, `claude-app` or `chatgpt`, from where the transcript came from) and
`author:` (`CLAUDE_NOTE_AUTHOR`, else `author` in `config.toml`, else the email
in `~/.foresyn/config.json`, else the global git email). The daily push sends
both in the document metadata.

`claude-note update` installs the latest release of `artemiin/claude-note`,
which does not have the push or the Codex/Cursor importers; use the script
above instead.

## How It Works

1. **Hook Integration**: Claude Code hooks notify claude-note when sessions start/stop
2. **Queue Processing**: Events are queued and processed by the background worker
3. **Synthesis**: When a session ends, Claude analyzes the transcript
4. **Note Routing**: Extracted knowledge is written to your vault

```
Claude Code Session
        │
        ▼
   [Hooks fire]
        │
        ▼
  ┌─────────────┐
  │ Event Queue │
  └─────────────┘
        │
        ▼
  ┌─────────────┐      ┌─────────────┐
  │   Worker    │─────▶│  Synthesize │
  └─────────────┘      └─────────────┘
                              │
                              ▼
                       ┌─────────────┐
                       │    Vault    │
                       │  - inbox.md │
                       │  - notes/   │
                       └─────────────┘
```

## Commands

```bash
claude-note status       # Check worker and queue status
claude-note update       # Check for and apply updates
claude-note drain        # Process all pending sessions now
claude-note clean        # Cleanup duplicate sessions, old locks
claude-note index        # Rebuild vault index for synthesis context
claude-note resynth <id> # Re-synthesize a specific session
claude-note ingest <file> # Ingest PDF/DOCX into literature notes
```

## Configuration

Config file: `~/.config/claude-note/config.toml`

```toml
vault_root = "/path/to/your/vault"

# Optional settings
open_questions_file = "open-questions.md"  # relative to vault

[synthesis]
mode = "route"           # log | inbox | route
model = "claude-sonnet-4-5-20250929"

[qmd]
enabled = false          # Enable qmd semantic search for context
synth_max_notes = 5
```

All settings can be overridden with environment variables:
- `CLAUDE_NOTE_VAULT` - vault path
- `CLAUDE_NOTE_MODE` - synthesis mode
- `CLAUDE_NOTE_MODEL` - Claude model for synthesis

See [docs/configuration.md](docs/configuration.md) for full reference.

## Claude Code Hook Setup

Add to your Claude Code settings (`~/.claude/settings.json`):

```json
{
  "hooks": {
    "PostToolUse": [
      {
        "hooks": [
          { "type": "command", "command": "claude-note enqueue", "timeout": 5000 }
        ]
      }
    ],
    "UserPromptSubmit": [
      {
        "hooks": [
          { "type": "command", "command": "claude-note enqueue", "timeout": 5000 }
        ]
      }
    ],
    "Stop": [
      {
        "hooks": [
          { "type": "command", "command": "claude-note enqueue", "timeout": 5000 }
        ]
      }
    ]
  }
}
```

See [docs/hook-setup.md](docs/hook-setup.md) for detailed instructions.

## Service Management

### macOS (launchd)

```bash
# Status
launchctl list | grep claude-note

# Stop
launchctl unload ~/Library/LaunchAgents/com.claude-note.worker.plist

# Start
launchctl load ~/Library/LaunchAgents/com.claude-note.worker.plist

# Logs
tail -f /path/to/vault/.claude-note/logs/worker-*.log
```

### Linux (systemd)

```bash
# Status
systemctl --user status claude-note

# Stop/Start
systemctl --user stop claude-note
systemctl --user start claude-note

# Logs
journalctl --user -u claude-note -f
```

## Vault Structure

Claude Note creates/uses these files in your vault:

```
your-vault/
├── .claude-note/           # Internal data (gitignore this)
│   ├── queue/              # Event queue
│   ├── state/              # Session state
│   ├── logs/               # Worker logs
│   └── vault_index.json    # Note index for context
├── claude-note-inbox.md    # Synthesized knowledge lands here
├── open-questions.md       # Questions tracker
└── claude-session-*.md     # Session logs (optional)
```

## Uninstall

```bash
./uninstall.sh
```

This removes the service, CLI, and source files. Your vault data is preserved.

## Optional: QMD Integration

If you have [qmd](https://github.com/tobi/qmd) installed for semantic search, enable it in config:

```toml
[qmd]
enabled = true
synth_max_notes = 5  # Include top N relevant notes as context
```

This improves synthesis quality by providing relevant vault context.

## Documentation

| Guide | Description |
|-------|-------------|
| [Getting Started](docs/getting-started.md) | Step-by-step installation walkthrough |
| [Configuration](docs/configuration.md) | Complete config reference |
| [Commands](docs/commands.md) | All CLI commands explained |
| [Synthesis Modes](docs/synthesis-modes.md) | log vs inbox vs route |
| [Hook Setup](docs/hook-setup.md) | Claude Code integration |
| [Service Setup](docs/service-setup.md) | launchd/systemd configuration |
| [QMD Integration](docs/qmd-integration.md) | Semantic search setup |
| [Document Ingestion](docs/document-ingestion.md) | Importing papers and docs |
| [Architecture](docs/architecture.md) | How it works internally |
| [Troubleshooting](docs/troubleshooting.md) | Common issues and fixes |

## License

MIT

## Other assistants: Codex / ChatGPT app, Cursor, Claude.ai and ChatGPT exports

| Assistant | How it is captured |
|---|---|
| Claude Code (terminal) | hooks in `~/.claude/settings.json` (above) |
| Codex CLI and the ChatGPT desktop app (it runs Codex) | hooks in `~/.codex/hooks.json`: `claude-note install-codex-hooks` |
| Cursor | read from Cursor's `state.vscdb` (read-only) by the worker every 30 min |
| Claude desktop / claude.ai, ChatGPT web | Settings → Export data; leave the zip in `~/Downloads` or drop it in `~/Documents/claude-note-imports/` |

The worker sweeps every 30 minutes for sources without a hook: Cursor chats,
export zips (Claude `data-*.zip`, ChatGPT `<hash>-<date>.zip`, or any zip in
`~/Documents/claude-note-imports/` that holds a `conversations.json`), Codex
rollouts the hook did not deliver, and interactive Claude Code sessions in
`~/.claude/projects` the hook did not deliver (sessions from before the hooks were
installed, or from a Claude Code without them; `claude -p` and SDK runs are
skipped, and at most 10 are queued per sweep since each costs one synthesis). Each conversation is converted into a local
transcript under `~/.local/share/claude-note/transcripts/` and processed once
(content hash); a conversation that grows is processed again. Imported zips move
to `~/Documents/claude-note-imports/done/`. Codex subagent threads and
`codex exec` automation are skipped. The first sweep only looks back 7 days;
backfill older history with `claude-note import --since 2025-01-01`. Heartbeat:
`~/Library/Logs/claude-note/import-sweep.ok` (shown in `claude-note status`).

```bash
claude-note install-codex-hooks   # once; backs up ~/.codex/hooks.json
claude-note import                # sweep now instead of waiting
claude-note import ~/Downloads/data-2026-09-25.zip
```

Codex skips a new hooks file until it is trusted once: approve it in the app's
hooks review, or run `codex` in a terminal and accept the prompt. Until then the
30-minute sweep still picks the sessions up, just later.

## Daily push to the shared Foresyn vault

`claude-note push` sends curated notes to the Foresyn vault inbox
(`inbox/<user>/<date>/<path with / as __>.md`) using `~/.foresyn/config.json`.
Only notes whose frontmatter `type` is `pattern`, `gotcha`, `decision`,
`reference`, `project` or `literature` go. A note claude-note synthesized
without a `type` (all of them before 1.6.0; new ones always get one) is typed by
its first tag naming a type, else `reference`; a note a person wrote without a
`type` is left alone. Session notes never go, nor anything
with `share: false` or under a `private/` folder. API keys, tokens, passwords,
private keys and credentialed connection strings are replaced with
`[REDACTED: <kind>]` first; a note the scanner fails on is skipped. A note whose
redacted content is unchanged since its last push sends no request.

```bash
claude-note push --dry-run        # counts only
claude-note push --install-agent  # launchd, daily at 03:00 (log: ~/Library/Logs/claude-note/push.log)
```

A note that fails is named in `push.log` (`error: <path>: HTTP <status>: ...`).
A request with no response, a 5xx or a 409 is retried once after 5 s; a lost
response usually means the server wrote the note, and a repeat PUT of the same
content is a no-op there. 401, 403 and 429 stop the run. Any error leaves the
heartbeat untouched and the job exits 1; the note is retried on the next run.

Heartbeat after a clean run: `~/Library/Logs/claude-note/push.ok`.
