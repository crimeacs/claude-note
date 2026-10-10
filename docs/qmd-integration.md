# QMD integration

QMD is an optional retrieval index over Markdown, separate from Claude Note's local note index and any canonical shared vault. Commands below were checked against the installed CLI and [QMD's current documentation](https://github.com/tobi/qmd).

## Register and refresh

```bash
# Optional installation; follow QMD's requirements for your platform:
npm install -g @tobilu/qmd
qmd collection add ~/Documents/claude-notes --name notes
qmd collection list
qmd update
qmd search "retry timeout" -c notes -n 5 --json
```

For semantic search, prepare the embedding model and vectors deliberately:

```bash
qmd embed
qmd vsearch "recover from a lost response" -c notes -n 5 --json
```

`qmd update` refreshes indexed text. `qmd embed` handles missing/changed vectors. `claude-note index` builds Claude Note's separate routing index. There is no `qmd index` command in the verified interface.

## Configure the worker

```toml
[qmd]
enabled = true
collection = "notes"
search_mode = "keyword"
ingest_dedup_enabled = false  # opt in explicitly for semantic ingestion
synth_max_notes = 5
qmd_timeout = 10
min_score = 0.3
```

The collection must refer to the configured vault. With no collection, synthesis uses its local note index and injects no global QMD results. The adapter discovers QMD in common executable locations even under a minimal service PATH. It uses `-n`, `-c`, `--json`, and a subprocess timeout; accepts current `[{"file": "qmd://notes/path.md", ...}]` and legacy `{"results": [{"path": ...}]}` output; and preserves the engine's ordering.

Synthesis checks collection and filesystem containment, requires a current local indexed file, and reads a bounded source excerpt. A snippet nominates a source; it does not establish the whole claim. Inspect the full document before relying on a dated decision, scope, or exception.

## Choose the retrieval mode

| Mode | Use | Operational limit |
| --- | --- | --- |
| `search` | Default BM25 synthesis context; no model inference. | Lexical overlap is not relevance. |
| `vsearch` | Explicit semantic synthesis and ingestion candidate search. | Needs prepared models/vectors; threshold applies only to this mode. |
| `query` | Optional manual hybrid expansion and reranking. | Can trigger cold model downloads and inference; Claude Note does not call it. |

Do not interpret BM25 scores as confidence percentages or filter them using a vector threshold. Score transforms differ by mode and version. The installed BM25 implementation used lower numeric scores for stronger matches; engine ordering was correct. Claude Note preserves order and omits score percentages from its synthesis prompt.

BM25 queries can be conjunctive. Claude Note strips common instruction words, removes duplicates, and bounds the query to a few content words. For manual search, shorten a zero-result query before concluding no memory exists:

```bash
qmd search "vault graph" -c notes -n 5
qmd get "qmd://notes/a-note.md"
qmd status
```

Check collection paths, text freshness, and vector coverage separately. A successful text refresh does not prove semantic coverage. Avoid unprepared `query` in unattended work. Missing QMD, invalid JSON, nonzero exits, and timeouts fall back to the local note index; failures are logged without breaking synthesis.

Advanced relevance judging, current-fact context packs, and tenant authorization belong to external integrations; see [current system](current-system.md).
