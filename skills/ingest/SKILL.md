---
name: ingest
description: Ingest PDFs, DOCX, and URLs into your knowledge vault as atomic, interconnected notes. Extracts concepts with semantic deduplication. Use when you want to turn research papers, documentation, or team docs into permanent knowledge.
argument-hint: [file-or-url] [--internal] [--dry-run]
allowed-tools: Read, Write, Bash, WebFetch
---

# Document Ingestion Skill

Turn external documents into structured, linkable knowledge notes with semantic deduplication.

## Quick Start

```
/ingest ~/Downloads/attention-paper.pdf
/ingest https://example.com/api-docs --internal
/ingest spec.docx --title "API Specification v2"
/ingest paper.pdf --dry-run  # Preview without writing
```

## Features

- **Semantic deduplication**: Merges similar concepts instead of creating duplicates
- **Atomic notes**: Extracts 3-15 typed concepts per document
- **Multi-source accumulation**: Concepts grow richer with each new source
- **Dual mode**: Literature (research) vs Internal (team docs)

## Modes

- **Literature** (default): Creates `literature/lit-{slug}.md` for external research, papers, articles
- **Internal** (`--internal`): Creates `internal/int-{slug}.md` for team docs, internal specs, processes

## Arguments

| Argument | Description |
|----------|-------------|
| `[file-or-url]` | Path to document (.pdf, .docx, .md, .txt) or URL |
| `--internal` | Create internal note instead of literature note |
| `--title "..."` | Override the note title (default: derived from filename/URL) |
| `--dry-run` | Preview what would be extracted without writing files |

## Prerequisites

This skill requires `claude-note` CLI for full functionality (deduplication, PDF parsing).

Check if installed:
```bash
claude-note --version
```

Install if needed:
```bash
# Using uv (recommended)
uv tool install git+https://github.com/crimeacs/claude-note.git

# Or using pipx
pipx install git+https://github.com/crimeacs/claude-note.git
```

**Without CLI**: The skill still works for basic ingestion using built-in tools (pdftotext, pandoc), but advanced features like semantic deduplication require the CLI.

## Process

When the user invokes `/ingest`, follow these steps:

### 1. Read the Document

**For files:**
- Use the Read tool for `.md` and `.txt` files
- For `.pdf` files: use Bash with `pdftotext` or `pandoc` to extract text
- For `.docx` files: use Bash with `pandoc -t plain` to extract text

**For URLs:**
- Use WebFetch to retrieve the content

### 2. Get Configuration

Read the vault path from `~/.config/claude-note/config.toml`:
```bash
cat ~/.config/claude-note/config.toml
```

Look for `vault_root = "..."` to find where notes should be created.

If no config exists, ask the user where to create notes.

### 3. Extract Knowledge

Analyze the content and extract:
- **Summary**: 2-3 sentence overview of what the document covers
- **Key Concepts**: Main ideas, terminology, and definitions (3-7 concepts)
- **Highlights**: Important findings, quotes, or actionable insights
- **Questions**: Open questions or things worth exploring further
- **Related Topics**: Connections to other knowledge areas

### 4. Check for Duplicates

Before creating a new note, check if a similar note exists:
```bash
ls {vault_path}/literature/ 2>/dev/null | grep -i "{keywords}"
ls {vault_path}/internal/ 2>/dev/null | grep -i "{keywords}"
```

If a similar note exists, ask the user if they want to:
1. Merge new content into the existing note
2. Create a separate note anyway
3. Cancel

### 5. Create the Note

Write a structured note using the appropriate format:
- Literature notes: See [literature-format.md](references/literature-format.md)
- Internal notes: See [internal-format.md](references/internal-format.md)

**Filename conventions:**
- Literature: `literature/lit-{slug}.md`
- Internal: `internal/int-{slug}.md`

Where `{slug}` is a kebab-case version of the title (max 50 chars).

### 6. Confirm

Tell the user what was created and offer to open the note.

## Example Workflow

User: `/ingest ~/Downloads/transformer-paper.pdf`

1. Extract text from PDF:
   ```bash
   pdftotext ~/Downloads/transformer-paper.pdf - 2>/dev/null || pandoc ~/Downloads/transformer-paper.pdf -t plain
   ```

2. Read config:
   ```bash
   cat ~/.config/claude-note/config.toml
   ```

3. Analyze the content and extract knowledge

4. Create note at `{vault_root}/literature/lit-attention-is-all-you-need.md`

5. Confirm to user what was created

## URL Ingestion

For URLs, use WebFetch to get the content:

```
/ingest https://docs.example.com/api-guide --internal
```

WebFetch will retrieve the content, then follow the same extraction process.

## Dry Run

Preview what would be extracted without writing files:

```
/ingest paper.pdf --dry-run
```

This shows the extracted knowledge and where it would be saved, but doesn't create any files.

## Tips

- Use descriptive filenames or `--title` for better slugs
- Review created notes and add manual annotations
- Link to existing topic notes in your vault using `[[note-name]]` syntax
- For large documents, focus on the most important 3-5 concepts
- Use `--dry-run` to preview before committing

## See Also

- [literature-format.md](references/literature-format.md) - Template for literature notes
- [internal-format.md](references/internal-format.md) - Template for internal notes
- [sample-output.md](examples/sample-output.md) - Example of a created note

## CLI Alternative

For batch processing or automation, use the CLI command instead:
```bash
claude-note ingest ~/papers/*.pdf
```

The CLI calls Claude API programmatically and is better for processing multiple documents.
