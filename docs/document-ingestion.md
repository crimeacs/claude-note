# Document Ingestion

Claude Note can ingest external documents (research papers, documentation, technical specs) and convert them into structured atomic notes in your vault.

## Two Modes

| Mode | Flag | Output | Use Case |
|------|------|--------|----------|
| Literature | (default) | `lit-*.md` in `literature/` | Research papers, external docs |
| Internal | `--internal` | `int-*.md` in `internal/` | Team docs, processes, architecture |

## Quick Start

```bash
# Ingest a research paper
claude-note ingest ~/Downloads/paper.pdf

# Preview without creating notes
claude-note ingest paper.pdf --dry-run

# Ingest team documentation
claude-note ingest process.docx --internal

# Custom title
claude-note ingest spec.pdf --title "API Specification v2"
```

## Supported Formats

| Format | Extension | Requirements |
|--------|-----------|--------------|
| PDF | `.pdf` | pymupdf, pdftotext, or pandoc |
| Word | `.docx` | pandoc |
| Markdown | `.md` | None |
| Plain text | `.txt` | None |

### Installing Dependencies

```bash
# Best PDF support (recommended)
pip install pymupdf

# Alternative: pandoc (also handles DOCX)
brew install pandoc        # macOS
sudo apt install pandoc    # Ubuntu/Debian

# Alternative: pdftotext
brew install poppler       # macOS
sudo apt install poppler-utils  # Ubuntu/Debian
```

## How It Works

### 1. Text Extraction

Documents are converted to plain text:
- **PDF:** pymupdf (best) → pdftotext → pandoc (fallback chain)
- **DOCX:** pandoc
- **MD/TXT:** Direct read

### 2. Knowledge Extraction

Claude analyzes the document and extracts:
- **Source summary:** 2-3 sentence overview
- **Key citation:** Author/title for references
- **Interesting takeaways:** Narrative summary of key insights
- **Atomic concepts:** 3-15 standalone notes per document

Each concept includes:
- Title and slug (becomes filename)
- Type: finding, technique, definition, benchmark, open-question
- Summary and optional details
- Fi project relevance (if applicable)
- Tags

### 3. Semantic Deduplication

When qmd is enabled, ingestion checks for similar existing notes:
- **Exact match:** Attempts to merge new source info
- **Similar concept:** Merges if new information adds value
- **No match:** Creates new note

This prevents duplicate notes while accumulating sources.

### 4. Note Creation

**Source note:** `lit-{citation-slug}.md` or `int-{citation-slug}.md`
- Links to all extracted concept notes
- Contains metadata about the ingested document

**Concept notes:** `lit-{concept-slug}.md` or `int-{concept-slug}.md`
- Atomic, standalone notes
- Link back to source
- Include relevance to your project

---

## Command Reference

```bash
claude-note ingest <file> [options]
```

### Arguments

| Argument | Description |
|----------|-------------|
| `file` | Path to document (.pdf, .docx, .md, .txt) |

### Options

| Option | Short | Description |
|--------|-------|-------------|
| `--title` | `-t` | Override note title (default: filename) |
| `--model` | `-m` | Override Claude model for extraction |
| `--dry-run` | `-n` | Extract and display without writing notes |
| `--internal` | `-i` | Create `int-*` notes in `internal/` instead of `lit-*` in `literature/` |

---

## Output Structure

### Source Note

Created at `literature/lit-{slug}.md`:

```markdown
---
tags:
  - source/literature
  - paper
  - project/fi
source_file: "paper.pdf"
ingested: 2024-01-15
---

# Author et al. (2024)

2-3 sentence summary of what this document covers.

## Extracted Concepts

- [[literature/lit-concept-one|Concept One]]
- [[literature/lit-concept-two|Concept Two]]
- [[literature/lit-concept-three|Concept Three]]

## Source

- **File:** `paper.pdf`
- **Ingested:** 2024-01-15
- **Type:** paper

## Related

- [[fi-ml-moc]] - ML project hub
- [[fi-moc]] - Fi project hub
```

### Concept Note

Created at `literature/lit-{concept-slug}.md`:

```markdown
---
tags:
  - source/literature
  - project/fi
  - lit/finding
  - machine-learning
source: "[[literature/lit-author-2024]]"
added: 2024-01-15
---

# Concept Title

2-4 sentence explanation of this concept.

## Details

Longer explanation with specifics, numbers, quotes from the paper.

## Fi Relevance

How this relates to the Fi project - collar sensors, behavior classification, etc.

## Related

- [[fi-ml-moc]]
- [[fi-moc]]

---

*Source: Author et al. (2024)*
```

### Internal Mode

With `--internal`, creates `internal/int-{slug}.md` with:
- `source/internal` tag instead of `source/literature`
- `int/{type}` tags (process, architecture, decision, convention, reference, how-to)
- Owner field when extractable

---

## Examples

### Research Paper

```bash
claude-note ingest ~/papers/accelerometer-dog-behavior.pdf

# Output:
# ✓ Converting accelerometer-dog-behavior.pdf (12s)
#   142,847 characters extracted
# ✓ Extracting knowledge with Claude (literature mode) (45s)
#   8 concepts found
# Creating notes in literature/...
#   Created: lit-accelerometer-dog-behavior.md
#   Created: lit-sliding-window-features.md
#   Created: lit-random-forest-classification.md
#   Merged into: lit-collar-placement-effects.md
#   Created: lit-activity-recognition-accuracy.md
#   ...
```

### Dry Run Preview

```bash
claude-note ingest paper.pdf --dry-run

# === Dry Run Results (literature mode) ===
# Source: Smith et al. (2024)
# Type: paper
# Summary: This paper presents a novel approach to...
#
# Would create 6 concept notes:
#   - lit-novel-approach.md: Novel Approach to Classification
#   - lit-feature-engineering.md: Feature Engineering Pipeline
#   ...
#
# ────────────────────────────────────────────────────────
# 📌 Key Takeaways:
# ────────────────────────────────────────────────────────
# The most interesting finding is that... [narrative summary]
# ────────────────────────────────────────────────────────
```

### Team Documentation

```bash
claude-note ingest deployment-process.md --internal

# Creating notes in internal/...
#   Created: int-deployment-process.md
#   Created: int-staging-environment.md
#   Created: int-rollback-procedure.md
```

---

## Semantic Deduplication

### How It Works

When qmd is enabled, each extracted concept is checked against existing notes:

1. **Query:** Concept title + summary + slug
2. **Search:** Vector similarity search in output directory
3. **Threshold:** Default 0.75 (configurable)
4. **Action:** If similar note found, assess whether to merge

### Merge Assessment

Claude evaluates if the new source adds genuinely new information:
- New techniques, numbers, or findings?
- Different perspective or application?
- Or just restating the same concept?

If new info exists, it's appended under "## Additional Sources".

### Configuration

```toml
[qmd]
enabled = true
ingest_dedup_enabled = true
ingest_dedup_threshold = 0.75  # Similarity threshold

# Merge settings
ingest_merge_enabled = true
max_sources_per_concept = 5    # Stop merging after N sources
```

---

## Configuration

### Output Directories

By default:
- `literature/` for external research (papers, docs)
- `internal/` for team documentation

These are relative to your vault root.

### Model Override

```bash
# Use a specific model for complex documents
claude-note ingest paper.pdf --model claude-opus-4-20250514
```

Or set globally:
```toml
[synthesis]
model = "claude-opus-4-20250514"
```

---

## Best Practices

### 1. Use Descriptive Filenames

The filename becomes the default title:
- Good: `accelerometer-dog-behavior-classification.pdf`
- Bad: `paper.pdf`, `doc1.pdf`

Or use `--title` to override.

### 2. Preview First

Use `--dry-run` to see what would be extracted:
```bash
claude-note ingest paper.pdf --dry-run
```

Review the "Key Takeaways" section to verify extraction quality.

### 3. Organize by Type

- Research papers, external specs → default (`literature/`)
- Team processes, architecture docs → `--internal`

### 4. Enable QMD

For best results, enable qmd semantic search:
```toml
[qmd]
enabled = true
```

This provides:
- Better duplicate detection
- Smarter concept merging
- Relevant vault context during synthesis

### 5. Index After Batch Ingestion

```bash
# Ingest multiple papers
for f in ~/papers/*.pdf; do
    claude-note ingest "$f"
done

# Rebuild qmd index
cd ~/vault && qmd index
```

---

## Troubleshooting

### "Could not convert PDF"

Install a PDF converter:
```bash
pip install pymupdf           # Best option
# or
brew install poppler          # pdftotext
# or
brew install pandoc           # Fallback
```

### Poor extraction quality

- Try `--model claude-opus-4-20250514` for complex documents
- Use DOCX format when available (often better than PDF)
- For scanned PDFs, OCR first with a dedicated tool

### "Claude CLI timed out"

Large documents may exceed the default 180s timeout. The document is automatically truncated to ~100k characters (~25k tokens).

For very large documents, consider splitting:
```bash
# Split large PDF
pdftk large.pdf cat 1-50 output part1.pdf
pdftk large.pdf cat 51-100 output part2.pdf

# Ingest parts
claude-note ingest part1.pdf --title "Large Doc - Part 1"
claude-note ingest part2.pdf --title "Large Doc - Part 2"
```

### Concepts not merging correctly

- Check qmd is enabled and indexed
- Adjust `ingest_dedup_threshold` (lower = more aggressive matching)
- Review existing notes have good titles/summaries

---

## Comparison: Ingestion vs Session Synthesis

| Aspect | Document Ingestion | Session Synthesis |
|--------|-------------------|-------------------|
| **Source** | External PDF/DOCX | Claude Code transcript |
| **Trigger** | Manual `ingest` command | Automatic on session end |
| **Output** | Atomic concept notes | Session log + inbox items |
| **Structure** | Source → Concepts | Timeline → Learnings |
| **Best for** | Research, references | Work knowledge, decisions |
