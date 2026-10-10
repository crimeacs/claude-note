# Example: source index and concept

This is an illustrative structure, not a record of a real document or model run.
Suppose `example-report.pdf` contains a documented experiment named "Retry
Experiment" and supports one reusable finding. Actual filenames depend on the
extracted citation and concept slug.

```text
literature/
  lit-retry-experiment.md
  lit-retry-idempotency.md
```

`lit-retry-experiment.md`:

```markdown
---
type: literature
tags:
  - source/literature
  - report
source_file: example-report.pdf
ingested: 2026-10-10
---

<!-- claude-note:ingested-source:start -->
# Retry Experiment

The supplied report compares repeated requests with and without a stable request
identifier. This summary would record its actual setup and limitations.

## Extracted Concepts

- [[literature/lit-retry-idempotency]]

## Source

- **File:** `example-report.pdf`
- **Ingested:** 2026-10-10
- **Type:** report
<!-- claude-note:ingested-source:end -->
```

`lit-retry-idempotency.md`:

```markdown
---
type: literature
tags:
  - source/literature
  - retries
source: "[[literature/lit-retry-experiment]]"
added: 2026-10-10
---

# Retry Idempotency

The source's observed finding belongs here, with its conditions and evidence.
Do not infer an unconditional retry guarantee from the note's title.

## Source

[[literature/lit-retry-experiment]] — Retry Experiment
```

For `--internal`, the same shape uses `internal/int-*.md`, `type: source` and
`source/internal` tags. A semantic merge can point the source index at an
existing concept with a different filename. A skipped or nonexistent file is
not added as a speculative link.
