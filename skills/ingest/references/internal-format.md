# Internal source notes

`--internal` writes a document source index and concepts under `internal/`, each
with `type: source`. This distinguishes ingested evidence from independently
curated patterns, decisions or incident lessons. A later curated note can use
its own explicit semantic type and link back to the source.

Source index frontmatter:

```yaml
type: source
tags:
  - source/internal
  - int/process
source_file: service-runbook.md
ingested: 2026-10-10
```

Concept frontmatter:

```yaml
type: source
tags:
  - source/internal
  - int/process
  - recovery
source: "[[internal/int-service-runbook]]"
added: 2026-10-10
```

Capture the process, rationale or convention supported by the document. Include
owners only when identified in the source. Distinguish historical decisions from
current instructions; preserve evidence of superseded behavior. For fetched
internal pages, retain their original URL on the source index without exposing
credentials. Keep the material in its authorized project vault.

Link to actual source and concept files. Existing source indexes update a managed
block while preserving surrounding annotations. Semantic candidates must resolve
to real files in this vault's internal output directory; matching names in an
unrelated collection do not qualify. No search rank proves that two claims agree.
