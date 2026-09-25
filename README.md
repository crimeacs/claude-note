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

## Codex and the ChatGPT desktop app

The ChatGPT desktop app runs Codex, and Codex supports the same hook events as Claude Code (`UserPromptSubmit`, `Stop`, `SessionEnd`, each with `session_id`, `transcript_path` and `cwd`). `claude-note` reads Codex rollouts (`~/.codex/sessions/**/rollout-*.jsonl`) as well as Claude Code transcripts; context Codex injects itself (environment, AGENTS.md, plugin lists) is not treated as a user prompt.

To capture Codex sessions, add `~/.codex/hooks.json`:

```json
{"hooks": {
  "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "claude-note enqueue", "timeout": 5}]}],
  "Stop":             [{"hooks": [{"type": "command", "command": "claude-note enqueue", "timeout": 5}]}],
  "SessionEnd":       [{"hooks": [{"type": "command", "command": "claude-note enqueue", "timeout": 3}]}]
}}
```

Codex runs a new hooks file only after it has been trusted once: approve it in the app's hooks review (or run `codex` once in a terminal). Until then the hooks are skipped silently.
