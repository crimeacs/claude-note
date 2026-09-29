# claude-ingest-skill

A Claude Code skill that turns documents (PDF, DOCX, URLs) into atomic, interconnected knowledge notes with semantic deduplication.

## Features

- **Semantic deduplication** - Merges similar concepts instead of creating duplicates
- **Atomic notes** - Extracts 3-15 typed concepts per document, not monolithic summaries
- **Multi-source accumulation** - One concept grows richer with each new source
- **Dual mode** - Literature (research papers) vs Internal (team docs)
- **Dry-run preview** - See what would be extracted before committing

## Installation

### Claude Code (recommended)

Add the marketplace and install:
```
/plugin marketplace add crimeacs/claude-ingest-skill
/plugin install ingest@crimeacs-claude-ingest-skill
```

Or use the interactive plugin manager:
```
/plugin
```
Then navigate to **Marketplaces** → Add `crimeacs/claude-ingest-skill`

### Manual Installation

Clone to your skills directory:

```bash
git clone https://github.com/crimeacs/claude-ingest-skill.git ~/.claude/skills/ingest
```

## Usage

```
/ingest ~/Downloads/attention-paper.pdf
/ingest https://example.com/api-docs --internal
/ingest spec.docx --title "API Specification v2"
/ingest paper.pdf --dry-run
```

### Arguments

| Argument | Description |
|----------|-------------|
| `[file-or-url]` | Path to document (.pdf, .docx, .md, .txt) or URL |
| `--internal` | Create internal note instead of literature note |
| `--title "..."` | Override the note title |
| `--dry-run` | Preview extraction without writing files |

### Output Modes

- **Literature** (default): Creates `literature/lit-{slug}.md` for external research
- **Internal** (`--internal`): Creates `internal/int-{slug}.md` for team docs

## Prerequisites

For full functionality (semantic deduplication, batch processing), install the `claude-note` CLI:

```bash
# Using uv (recommended)
uv tool install git+https://github.com/crimeacs/claude-note.git

# Or using pipx
pipx install git+https://github.com/crimeacs/claude-note.git
```

Configure your vault:

```bash
claude-note setup
```

**Without CLI**: Basic ingestion still works using built-in tools (pdftotext, pandoc).

## How It Works

1. **Extract** - Reads content from PDF, DOCX, Markdown, or URLs
2. **Analyze** - Extracts key concepts, highlights, questions, and relationships
3. **Deduplicate** - Checks for existing similar notes in your vault
4. **Create** - Writes structured Markdown with frontmatter and wiki-links

## Example Output

```markdown
---
tags:
  - literature
  - machine-learning
  - transformers
source: "attention-is-all-you-need.pdf"
author: "Vaswani et al."
year: 2017
ingested: 2024-01-15
---

# Attention Is All You Need

## Summary

Introduces the Transformer architecture, which relies entirely on attention
mechanisms without recurrence or convolution. Achieves state-of-the-art
results on machine translation.

## Key Concepts

- **Self-attention**: Compute representations by attending to different positions
- **Multi-head attention**: Run attention multiple times in parallel
- **Positional encoding**: Inject sequence order using sinusoidal functions

## Highlights

- Training takes only 3.5 days on 8 GPUs (vs weeks for RNNs)
- Achieves 28.4 BLEU on EN-DE translation

## Questions

- How do positional encodings compare to learned embeddings?
- What are the memory requirements for very long sequences?

## Related

- [[transformers]] - Architecture family
- [[attention-mechanisms]] - Core concept
```

## Vault Structure

Notes are created in your configured vault:

```
vault/
├── literature/
│   ├── lit-attention-is-all-you-need.md
│   └── lit-rest-api-design.md
└── internal/
    ├── int-deployment-process.md
    └── int-auth-service-spec.md
```

## Configuration

Configure via `~/.config/claude-note/config.toml`:

```toml
vault_root = "~/Documents/my-vault"
```

## Related Projects

- [claude-note](https://github.com/crimeacs/claude-note) - Full knowledge synthesis daemon
- [Obsidian](https://obsidian.md) - Knowledge base that works on local Markdown files

## License

MIT
