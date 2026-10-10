# Literature notes

The CLI creates a document source index and separate concept notes. Both carry
`type: literature`; `tags` carry topics. Use only findings present in the source,
and label interpretations. Cite results in their original context.

Source index frontmatter includes the original filename and ingestion date:

```yaml
type: literature
tags:
  - source/literature
  - report
source_file: example-report.pdf
ingested: 2026-10-10
```

Its body contains the document summary, links to actual concept files, and source
details. The generated body is managed; annotations outside that block survive
re-ingestion. If the skill fetched a URL, retain `source_url` on this index as
well. `author`, when present, identifies the vault writer; put publication
bylines in the citation or a distinct `source_authors` field rather than changing
writer provenance.

A concept carries a backlink to the actual source index:

```yaml
type: literature
tags:
  - source/literature
  - evaluation
source: "[[literature/lit-example-report]]"
added: 2026-10-10
```

Write one self-contained concept with its summary, supported details and source.
Additional sources may accumulate after an evidence-backed merge. Link to other
existing notes when the relationship is useful. Avoid empty placeholder links,
mandatory concept counts and invented project affiliations.
