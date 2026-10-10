# Document ingestion skill

This bundled skill routes document ingestion through `claude-note ingest`.
It creates typed source indexes and atomic concept notes with provenance and
backlinks, preserving human text and project boundaries.

Install and configure claude-note using the [repository guide](../../README.md),
then copy this skill folder to your assistant's supported skills directory.
The CLI requires a configured vault and the local Claude CLI; document parsing
may also use `pdftotext` for PDF or `pandoc` for DOCX.

```text
/ingest ~/Downloads/paper.pdf
/ingest ~/work/runbook.md --internal
/ingest https://example.com/document --dry-run
```

The skill can fetch a URL into a named local source first. The CLI itself accepts
one local file per invocation:

```bash
claude-note ingest "/path/to/paper.pdf" --dry-run
claude-note ingest "/path/to/runbook.md" --internal --title "Service Runbook"
```

External documents produce `type: literature` notes under `literature/`.
Internal documents produce `type: source` notes under `internal/`. These types
describe evidence roles; they do not imply that internal material is publishable.

Optional QMD vector candidate lookup requires a configured vault collection and
`[qmd] ingest_dedup_enabled = true`. Candidate files must exist in the destination
folder; search similarity alone never justifies replacing a note. Default
capture/retrieval does not require semantic ingestion.

See [SKILL.md](SKILL.md) for the workflow and
[sample output](examples/sample-output.md) for the source/concept structure.
