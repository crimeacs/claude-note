---
name: ingest
description: Turn a supplied research paper, article, or internal document into source-backed atomic notes in the configured claude-note vault. Use for document ingestion, including URLs fetched into a local file first.
argument-hint: [file-or-url] [--internal] [--dry-run]
allowed-tools: Read, Write, Bash, WebFetch
---

# Document ingestion

Use the installed `claude-note ingest` command to preserve the vault's writing,
provenance and merge behavior. The CLI accepts one local `.pdf`, `.docx`, `.md`,
or `.txt` file per invocation. It calls the local Claude CLI for extraction.

```bash
claude-note ingest "/path/to/paper.pdf"
claude-note ingest "/path/to/runbook.md" --internal --title "Service Runbook"
claude-note ingest "/path/to/paper.pdf" --dry-run
```

Resolve the configured vault before writing. `CLAUDE_NOTE_VAULT_ROOT` overrides
`vault_root` in `${XDG_CONFIG_HOME:-~/.config}/claude-note/config.toml`.
`claude-note health` reports configuration; `claude-note ingest --help` lists
supported flags. If no destination is configured or inferable from the user's
request, ask for the destination. Routine ingestion and evidence-backed merges
within the authorized vault do not require another permission prompt.

## Sources and output

For a URL, fetch its readable content, save an explicitly named local document,
and include the original URL in that source. Then pass the saved file to the CLI.
Retain the URL on the resulting source note. Do not pass a URL directly to the
CLI or claim the CLI fetches web pages. For several files, invoke the command
once per file.

Literature mode writes `literature/lit-*.md` with `type: literature`; internal
mode writes `internal/int-*.md` with `type: source`. Each document has a source
index and source-linked concept notes. Use the amount of knowledge the document
supports; no fixed concept count is a quality target. Keep source findings,
interpretation and unanswered questions distinguishable. Add only verified
existing relationships, preserving project boundaries and human annotations.

Read [literature-format.md](references/literature-format.md) for external sources
or [internal-format.md](references/internal-format.md) for internal material.
[Sample output](examples/sample-output.md) illustrates the source/concept pair.

## Existing knowledge

Exact filenames are checked first. Semantic ingestion candidates require QMD
`enabled = true`, an explicit collection for this vault, and
`ingest_dedup_enabled = true` in `[qmd]`. This optional feature uses vector
retrieval and may load an embedding model. A search rank is a candidate signal,
not proof of equivalence: the CLI resolves the result to a real file in the
configured output directory and asks Claude whether source-backed information
is worth adding. An unrelated collection or a source index is never a concept
merge target. When QMD is disabled or unavailable, exact filename handling and
new-note creation still work.

The source index links to actual created or merged concepts. Existing source
indexes update a managed block; other human text is preserved. If a filename is
already owned by another source or an unrelated note, report the conflict and
choose an appropriate distinct source title within the user's scope. Never
replace human content merely because titles or scores are similar.

## Finish

Inspect the returned paths and source backlinks. Summarize what was created,
merged or skipped, and any extraction/merge failures. A dry run extracts and
previews without writing vault notes; it still invokes the extraction model.
Do not claim semantic candidates were searched during a dry run: merging happens
only during the write phase. Publication or external sharing is a separate act.
