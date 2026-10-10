# Configuration reference

Source of truth: [config.py](../src/claude_note/config.py), [provenance.py](../src/claude_note/provenance.py), and [push.py](../src/claude_note/push.py).

The config lives at `$XDG_CONFIG_HOME/claude-note/config.toml`, defaulting to `~/.config/claude-note/config.toml`. Environment overrides win over TOML, then built-in defaults apply.

```toml
vault_root = "/absolute/path/to/notes"
author = "you@example.com"
inbox_file = "claude-note-inbox.md"
open_questions_file = "open-questions.md"
debounce_seconds = 15
poll_interval = 2
lock_timeout = 30
index_refresh_interval = 300
timeline_max_entries = 100
inbox_dedup_enabled = true
inbox_dedup_lookback = 50
ingest_merge_enabled = true
max_sources_per_concept = 5

[synthesis]
mode = "route"
model = "claude-sonnet-4-5-20250929"
timeout = 300

[qmd]
enabled = true
collection = "notes"
search_mode = "keyword"
synth_max_notes = 5
qmd_timeout = 10
min_score = 0.3
link_enhance_enabled = true
ingest_dedup_enabled = false
ingest_dedup_threshold = 0.75
```

The model string above is the existing code default, not a claim about the newest available model. Set an identifier your installed Claude CLI supports. Synthesis uses `claude -p --model`; its authentication and provider settings belong to that CLI.

| Setting | Default | Meaning |
| --- | --- | --- |
| `vault_root` | Required | Markdown vault; `~` is expanded. |
| `author` | Resolved | Human provenance. Resolution: environment → TOML → optional Foresyn config → global git email. |
| `synthesis.mode` | `route` | `log` records sessions; `inbox` synthesizes into review inbox; `route` applies structured note operations. |
| `synthesis.timeout` | `300` | Timeout for the synthesis subprocess. |
| `qmd.enabled` | `true` | Enables optional retrieval when QMD is available. |
| `qmd.collection` | Empty | Exact registered collection for this vault. Empty disables QMD prompt injection and semantic ingestion merges. |
| `qmd.search_mode` | `keyword` | BM25 synthesis retrieval; `vector` explicitly enables embedding search. |
| `qmd.qmd_timeout` | `10` | Timeout per QMD subprocess. |
| `qmd.min_score` | `0.3` | Vector similarity threshold only; never a BM25 confidence threshold. |
| `qmd.ingest_dedup_enabled` | `false` | Explicit opt-in to vector candidate search during document ingestion. |
| `qmd.ingest_dedup_threshold` | `0.75` | Vector shortlist threshold for ingestion merge candidates; not proof of duplicate content. |
| `inbox_dedup_enabled` | `true` | Suppress repeated identical extractions, preserving different findings on the same topic. |

Environment variables use the setting's uppercase key, without its TOML section: `CLAUDE_NOTE_MODE`, `CLAUDE_NOTE_MODEL`, `CLAUDE_NOTE_TIMEOUT`, `CLAUDE_NOTE_COLLECTION`, `CLAUDE_NOTE_SEARCH_MODE`, `CLAUDE_NOTE_QMD_TIMEOUT`, `CLAUDE_NOTE_INBOX_FILE`, `CLAUDE_NOTE_OPEN_QUESTIONS_FILE`. `CLAUDE_NOTE_VAULT` is the documented vault override; `CLAUDE_NOTE_VAULT_ROOT` is retained for compatibility. `CLAUDE_NOTE_AUTHOR` sets human provenance.

Some legacy keys remain accepted by config but are not active controls: `synthesis.max_tokens` is not a CLI output-token limit; title-similarity dedup thresholds do not govern exact extraction deduplication. Older guides' `route_threshold`, `ingest_dupe_threshold`, `max_transcript_tokens`, and `session_timeout_minutes` are not supported settings. Check source before relying on a setting.

QMD's collection must point to the same vault. Inspect `qmd collection list`; merely assigning a name does not register it. Synthesis also requires the retrieved note to be in the package's local vault index and to exist on disk. Retrieved text remains evidence, not instructions or approval.

The optional push adapter reads its destination and credentials from `~/.foresyn/config.json`; do not put credentials in this repository or notes. See [shared-system boundaries](current-system.md).
