# Claude Note

Automatic knowledge extraction from Claude Code sessions into your Obsidian vault.

Claude Note runs as a background service, watching your Claude Code sessions and synthesizing key learnings, decisions, and questions into structured notes.

## Features

- **Session Logging**: Automatically captures Claude Code sessions as markdown notes
- **Knowledge Synthesis**: Uses Claude to extract key concepts, code patterns, and learnings
- **Smart Routing**: Routes synthesized knowledge to your inbox, specific notes, or creates new ones
- **Open Questions Tracking**: Detects and tracks questions that come up during sessions
- **Vault Integration**: Understands your existing notes for better context
- **Transcript Upload**: Hook script uploads transcripts on session end — no host filesystem mounts needed in Docker
- **Semantic Search Context**: QMD vector search provides related vault notes during synthesis
- **Lightweight Docker Stack**: Micro images — full stack under 400MB total

| Image | Size |
|-------|------|
| claude-note (API + worker) | 96 MB |
| emb-service (embeddings) | 41 MB |
| rerank-service (reranker) | 41 MB |
| obsidian (Perlite viewer) | 47 MB |
| qmd (semantic search) | 155 MB |

## Requirements

- Python 3.11+ (for built-in `tomllib`)
- [Claude CLI](https://github.com/anthropics/claude-cli) (for knowledge synthesis)
- An Obsidian vault (or any markdown-based notes system)

## Quick Start

```bash
make up
# Follow auth flow to log in to your Claude account
make cliproxy-login
make install-hooks
```

The installer will:
1. Check dependencies
2. Ask for your vault path
3. Set up the background service
4. Print instructions for Claude Code hook configuration

## How It Works

1. **Hook Integration**: A hook script (`claude-note-hook.sh`) posts events to the API on every hook, and uploads the full transcript on Stop
2. **Queue Processing**: Events are queued and processed by the background worker
3. **Synthesis**: When a session ends, Claude analyzes the transcript (read from the local transcript store)
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

## Configuration

Config file: `~/claude-note/docker/claude-note/config.toml`

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

Use the installer to add hooks to `~/.claude/settings.json`:

```bash
make install-hooks    # Install hooks (backs up settings.json first)
make remove-hooks     # Remove hooks
```

This installs `claude-note-hook.sh` which:
- Posts all hook events (PostToolUse, UserPromptSubmit, Stop) to `POST /events`
- On Stop: uploads the session transcript to `POST /transcripts/{session_id}`

The API URL defaults to `http://localhost:8000` and can be overridden with the `CLAUDE_NOTE_API` environment variable.

See [docs/hook-setup.md](docs/hook-setup.md) for detailed instructions.

## Service Management

### macOS (launchd)

```bash
make up                # Start the service
make down              # Stop the service
```

### Linux (systemd)

```bash
make up                # Start the service
make down              # Stop the service
```

## Vault Structure

Claude Note creates/uses these files in your vault:

```
your-vault/
├── .claude-note/           # Internal data (gitignore this)
│   ├── queue/              # Event queue
│   ├── state/              # Session state
│   ├── transcripts/        # Uploaded transcript files
│   ├── logs/               # Worker logs
│   └── vault_index.json    # Note index for context
├── claude-note-inbox.md    # Synthesized knowledge lands here
├── open-questions.md       # Questions tracker
└── claude-session-*.md     # Session logs (optional)
```

## Uninstall

```bash
make down              # Stop the service
make remove-hooks     # Remove hooks from Claude Code settings
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
