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

## Recall and sentinel

Source of truth: [recall.py](../src/claude_note/recall.py) and [sentinel.py](../src/claude_note/sentinel.py).
These sections are read without loading the rest of the config, so hooks work everywhere.

```toml
[recall]
enabled = true
collections = []               # QMD collections to search; empty = all
memory_collections = []        # searched separately, placed first (e.g. ["agentmem"])
inject = 4                     # notes per prompt
memory_slots = 2               # how many of those may be memories
search_n = 10                  # results requested per query
timeout = 6                    # seconds per qmd call (wall clock)
budget = 8                     # seconds for the whole recall
min_prompt = 15                # shorter prompts are not searched
max_terms = 10
snippet_chars = 260
index = ""                     # qmd --index name; "" = default
skip_paths = ["/sessions/", "/claude-session-", "/claude-note-inbox\\.md$"]
rerank_command = ""            # optional judge: JSON on stdin -> {"keep": [...]}
rerank_timeout = 2.5
log = true                     # ~/.local/share/claude-note/recall.jsonl
qmd = ""                       # explicit qmd path

[sentinel]
enabled = true
default_rules = true           # auth, scope, 401, quota/rate limit, disk full, macOS privacy
inject = 2

[[sentinel.rules]]
match = "ECONNREFUSED 127.0.0.1:5432"   # regex, case-insensitive, over the failure output
advice = "Local Postgres is not running."
```

Environment: `CLAUDE_NOTE_RECALL=off` disables recall; `CLAUDE_NOTE_RECALL_INDEX` selects a QMD
index; `CLAUDE_NOTE_RECALL_LOG=0` stops logging (the doctor's probes use it).

The optional push adapter reads its destination and credentials from `~/.foresyn/config.json`; do not put credentials in this repository or notes. See [shared-system boundaries](current-system.md).

## Source egress policy

Version 2.1 defaults to `local` and `host` audiences. Capture, Markdown storage and recall
can run locally; synthesis, optional external judging and staging require explicit grants.
Every source in a derivation must permit the destination. Marked private sources, malformed
privacy controls and denied origins remain local even under a broad grant. Redaction runs
after authorization and does not identify ordinary business confidentiality.

```toml
[egress]
default = ["local", "host"]

[[egress.rules]]
source = "/absolute/approved-project/*"
audiences = ["local", "host", "model"]

[[egress.rules]]
source = "/absolute/approved-project/private/*"
audiences = ["local", "host"]
restrict = true
```

Use actual absolute source paths. Background capture includes the transcript, tool input
paths, working directory, included catalog metadata and retrieved sources. A generated note
also retains restrictive lineage in local protected state. Removing its visible provenance
does not declassify it. Grant `staging` separately only when those source owners authorize the
configured destination. Existing generated notes without known lineage are deferred.

Claim `audiences` further restrict source grants; a missing claim audience means `host`.
Every claim in a payload or source must permit the external destination. Managed writes
persist these restrictions, including existing controls before replacement, and later writes
cannot broaden them. Malformed controls require owner reconciliation. The synthesis catalog
omits notes whose artifact or origin denies model access before including their paths or tags.
Legacy origin-only records remain compatible; controls removed manually before any restriction
was recorded cannot be reconstructed automatically. Inspect and reconcile those files locally.

Normalized chat imports retain the original export ZIP or native database as a restrictive
origin. Granting the generated transcript folder cannot override a deny on that origin.
New ZIP sweeps retain the original file and archive a copy only when new conversations are
imported; deleting an origin pauses external use of its derivatives. Legacy imports with an
unknown origin require reimport or owner reconciliation rather than an inferred grant.
Source inspection supports bounded UTF-8 text/native JSONL and recognized PDF, Word, assistant
export ZIP and SQLite inputs. Unsupported, oversized or linked final sources fail closed.

Model calls run with no tools, MCP servers, skills, project settings or session persistence,
from an empty directory with an allowlisted environment. Older unsupported Claude CLI versions
fail closed. Provider authentication and quota are reported separately from local readiness.

## Claim inspection and attestation

Canonical claim records live in Markdown. `claude-note claim inspect claims/<id>.md` shows
the exact claim digest and current detached authority. After inspecting the claim and local
evidence, an owner can attest that exact digest:

```sh
claude-note claim attest claims/<id>.md --authority verified \
  --evidence checks/result.md --confirm-digest <digest-from-inspect>
```

`approved` records a governing choice; `verified` records inspected support. Neither follows
from the producing assistant's identity or a note saying it was approved. Evidence changes,
deletion, scope, expiry, unresolved contradictions and invalid dependencies prevent admission.
Use `claim revoke` with the inspected digest to withdraw a local attestation. The private
receipt key protects against note-content forgery, not a compromised local account.

Retrieval supports `[recall] repository`, `environment` and `audiences` for claim applicability,
plus `context_token_budget` (a conservative UTF-8-byte bound) and absolute `budget` deadlines.
Unknown required context denies a machine claim; legacy prose remains an unverified lead.

## Evaluation and recovery

`claude-note eval tasks` runs simulated policy contracts. Actual constrained code editing is
separate: `claude-note eval agent-tasks --help` describes public fixtures, provider budgets,
OS sandbox requirements and exact baseline configuration. A missing backend or failed model
call stays in the scheduled denominator. Unknown runtime identities do not join historical
trends. Nightly runs require explicit `[eval.agent_tasks]` settings before provider calls.

Expensive extraction packs and immutable appended source snapshots survive crashes before
effects complete. Eight failed synthesis attempts park the session for inspection; a new
completed turn or explicit resynthesis can resume it. Human-file replacements and malformed
managed blocks become proposals under `.claude-note/mutations/pending`. Staging is reported
separately from an applied canonical change. External upload intents are read back at their
bound destination after unknown outcomes; conflicts and revocation requests await destination
reconciliation instead of silently overwriting reviewed content.
