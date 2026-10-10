# Document ingestion

Ingest external research or internal team documents into linked source and concept notes.

```bash
claude-note ingest paper.pdf --dry-run
claude-note ingest paper.pdf --title "Useful research"
claude-note ingest team-notes.docx --internal
```

Supported inputs: `.pdf`, `.docx`, `.md`, `.txt`. `--model` overrides the configured Claude model. `--dry-run` extracts knowledge without writing notes, but still invokes the model service.

Markdown and text need no converter. DOCX uses pandoc. PDF tries PyMuPDF if installed, then `pdftotext`, then a converter fallback; install an appropriate converter when extraction is unavailable. The package itself retains no Python runtime dependencies. A scanned PDF may need OCR before ingestion.

## Source and concept roles

External notes go under `literature/`; `--internal` uses `internal/`. A source note records source metadata and links to the concepts actually written or merged. Concept notes capture one reusable finding with a source backlink. Source records and curated findings have distinct types; missing/invalid types should not silently classify failure lessons as references.

Model-provided slugs are sanitized and output stays in the intended directory. Existing source notes receive managed updates that preserve manual text. Extraction prompts are generic; they do not assume a particular employer, customer, product, or industry.

## Similarity and merging

Optional QMD vector search nominates related local concepts only when an explicit collection is configured. Candidate paths must resolve to existing files in the expected output directory. A same-named file in another collection is not a merge target.

The merge assessment compares existing and incoming content before updating source-backed findings. Vector similarity is not duplicate proof. Corrections and new contradictory evidence must remain visible rather than being discarded because their titles resemble old notes.

Root merge controls belong before any TOML section:

```toml
ingest_merge_enabled = true
max_sources_per_concept = 5

[qmd]
collection = "notes"
ingest_dedup_enabled = true
ingest_dedup_threshold = 0.75
```

After importing, refresh the relevant indexes:

```bash
claude-note index
qmd update
qmd embed   # only when semantic retrieval is wanted
```

Ingestion does not automatically publish confidential documents. Shared push applies its own curated-type, private-path and redaction rules; choose the intended audience before sharing.
