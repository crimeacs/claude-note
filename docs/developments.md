# Developments

## 2.1.6 — retain the installed Python on upgrades (14 October 2026)

Both installers retain the existing tool's external Python by default. The
checkout installer accepts `--python VERSION_OR_PATH`; either installer also
honors `UV_PYTHON`. These explicit requests take precedence over reuse. Explicit
`UV_MANAGED_PYTHON`, `UV_NO_MANAGED_PYTHON` or `UV_PYTHON_PREFERENCE` environment
settings opt out of reuse and leave selection to uv with the default 3.11
request. No new selector is combined with those settings. Fresh installs keep
the normal 3.11 discovery and download behavior; offline, index and download
settings stay with uv. The interactive installer no longer preemptively downloads
Python before it knows whether it needs one.

This is an upgrade continuity policy, analogous to `uv tool upgrade`: changing a
persistent uv discovery preference does not silently migrate an existing tool's
interpreter. Pass `--python 3.11` or set `UV_PYTHON=3.11` to reselect using current
uv settings, or give an explicit executable. An exact executable request bypasses
uv's managed/system discovery filters; reuse is not a proof that every current
discovery preference permits that interpreter. Owner configuration is neither
parsed nor rewritten by the installers.

Selection reads uv's tool directory and resolves at most 40 symlinks from the
installed `claude-note/bin/python`. The chosen executable must be outside the
entire tool directory, because uv can remove an environment while reinstalling
it. No Python code runs during this selection. uv remains responsible for
validating the executable and the package's Python requirement. Broken, copied,
cyclic, internal or newline-ambiguous runtimes fail before installation, with an
explicit `--python`/`UV_PYTHON` recovery instruction; they do not trigger an ambiguous
fallback. A missing tool uses fresh-install discovery. Receipt publication still
requires the package and existing worker/recall upgrade steps to finish.
Resolving the existing executable also retains its concrete patch version;
explicit reselection is the path to a different Python release.

The observed failure was an unrelated Conda 3.10 startup probe during a 3.11
upgrade; the existing managed 3.11 interpreter was healthy. Reusing its exact
external executable avoids that search. This change does not add process-tree
supervision or claim that every Python startup and fresh-install discovery is
bounded independently of the caller's installation deadline.

## 2.1.5 — revision-bound import publication and recovery (14 October 2026)

Imports now own immutable captured revisions and publish a complete, stable
prompt/stop batch after the protected source restrictions and canonical receipt
are durable. Retries reuse the first pair, IDs and timestamps, including after
seen-store failure or rediscovery in another export. The worker verifies every
reference before durable capture acknowledgement; older publications cannot
regress a newer binding or hook. Queue cleanup retains canonical receipts and
evidence. Source identities remain available for current policy checks, while
authored restrictions are retained against later metadata removal. See
[ownership and validity](MEMORY_OWNERSHIP.md). These are local capture guarantees;
model execution and external effects are not exactly-once. Older releases cannot
safely read the added session-state fields; preserve state during rollback.

ZIP imports distinguish readable unrelated archives from failed selected or
recognized exports. Malformed or oversized members and failed conversations
produce explicit problems while independent members and conversations continue.
Cursor recovery isolates composers and rejects a conversation with a required
missing, malformed or unsupported bubble rather than publishing incomplete
normalized evidence. Tool results retain their full value within the 256 MiB
source bound instead of the previous 2,000-character projection. Workspace
association errors remain visible while valid composers can use a generic
workspace. Late database-scan, close or archive-backup failures preserve already
completed publications and integer queued counts. Failed items retain prior seen
values and session checkpoints; originals are retained. Version-aware archive
backups retry even when all valid conversations are already unchanged, and are
not a receipt of complete archive import.

Reports retain exact problem totals while storing at most 100 payload-safe
details, with an explicit omitted count. This bound also applies to native
transcript problems. `failed` counts reported problems, not all malformed records
or all files in a category. Counters name their units and separate source items,
queued sessions, failures, unchanged items, skips and deferrals; subsets overlap.
Any problem leaves the last-clean heartbeat unchanged. Direct helper errors
retain partial progress and exact/omitted failure counts. Synthetic regressions
exercise more than 100 failures followed by healthy input, independent member
and composer continuation, required bubbles, backup retries and late DB failures.

The 256 MiB limit remains per decompressed export member or Cursor composer plus
required bubbles; native transcript and metadata limits are unchanged. Recovery
still uses normalized supported content, including only ChatGPT's selected
visible text branch. Optional absent sources, recent downloading archives,
unrecognized Downloads names, old composers and noninteractive conversations
can be skipped or deferred. A clean eligible sweep does not assert complete
historical or media capture. Lossless oversized-source streaming and unsupported
format recovery remain pending; no lossy compaction or cursor reset is performed.

## 2.1.4 — native transcript import failure isolation (14 October 2026)

Claude Code and Codex recovery sweeps continue past individual unreadable,
oversized, malformed or incomplete transcripts. Category counts remain the
number of successfully queued sessions; each failed file produces a bounded
problem entry and stays retryable without advancing its seen value or session
checkpoint. Partial progress is saved, while any problem keeps the last-clean
sweep heartbeat unchanged. Direct helper callers without a problem accumulator
receive an exception carrying the partial queued count and all bounded problems.
Malformed records and incomplete trailing bytes now prevent native recovery
from marking that file seen, even when earlier records were readable. The
`failed` count is the number of reported problems, not every malformed record
or all files in a category. Queue publication retains its existing two-event
mechanism; this patch does not provide transactional or exactly-once enqueue.

The full-source limit remains 256 MiB, checked before metadata and again while
reading. Metadata reads have a 1 MiB prefix limit. Original sources remain
untouched; this release does not implement lossless streaming recovery for
larger transcripts. Preserve originals and existing cursors rather than replacing
them with lossy summaries or treating split files as new independent sessions.
ZIP/export and Cursor per-conversation isolation is outside this change.

## 2.0 — recall before action (10 October 2026)

1.x wrote knowledge down. Nothing put it back in front of an agent: the docs said prompt-time
recall needed "a separate host integration". 2.0 ships that integration, ported from the recall
hook that has run on every prompt of a working memory system since September 2026, and adds the
pieces that make the loop provable.

| Added | What it does | Evidence |
| --- | --- | --- |
| `hook recall` | Prompt-time recall for Claude Code and Codex: content-word ladder, pooled rungs, compound splitting, session skipping, newest-block snippets, memory slots, announced misses, wall-clock budget. | `recall.py`, `tests/test_recall.py`; real runs in [demo](demo.md) |
| `hook sentinel` | Known fixes for failed shell commands; "do not retry" for auth, quota, disk-full; reads output only. | `sentinel.py`, tests |
| `remember` | Typed lesson now, append-not-overwrite, reindexed in ~0.5 s. | `remember.py`, tests |
| `setup` | One idempotent command: collections (vault + Claude Code memories), hooks, agent rule, hourly refresh. | `setup_recall.py`, tests |
| `doctor` | Configured / exercised / in use; canary in a throwaway QMD index. | Found a real 8-hour refresh outage on its first run |
| `refresh` | Text index then vectors, locked, bounded, heartbeat. | `setup_recall.py` |
| `recall` | The hook's search by hand, `--json`, `--stats`. | `entry.py` |

Corrections to 1.x documentation: the bundled hooks now *do* inject prior knowledge; Codex's
`UserPromptSubmit` hook output *does* reach the model (Codex 0.162.1, after trust).

Bugs found while building it, each now covered by a test: underscore and dotted tokens zeroed
queries; exception class names outranked the words that mattered; a frontmatter-only snippet
dropped a correct hit; QMD's own config lives in `~/.config` even when `XDG_CONFIG_HOME` is set;
the doctor's probes were counted as real usage; and the earlier hand-written sentinel fired on
`grep` output three times in one afternoon.

## 1.6 — memory and vault modernization (10 October 2026)

This update consolidates verified operating lessons from the wider Sweat AI memory system into the public Claude Note fork. It compares current code with local unpublished improvements and current memory/vault implementations, rather than treating old notes as current state. Private notes, customer records, credentials, and machine configuration are not part of this repository update.

## Starting point

The latest baseline already included multi-assistant capture, provenance, the checkout installer, JSON health, curated push, and interactive Claude Code recovery. These features were retained. The old local checkout held useful unpublished typed-note and Obsidian changes; those were generalized and tested against the current baseline.

## Changes implemented here

| Area | Concrete improvement | Evidence / validation |
| --- | --- | --- |
| Transcript fidelity | Preserve nested Claude tool outcomes, genuine XML/HTML user requests, and the originating Codex fork identity. | `transcript_reader.py`, transcript regression tests. |
| Import identity | Source-aware stable IDs, unchanged legacy import dedupe, serialized sweeps, and visible failures. | `importers.py`, importer tests. |
| Synthesis evidence | Include bounded recent assistant conclusions and tool outcomes; mark claims and proposals distinctly; honor configured timeout. | `synthesizer.py`, retrieval/prompt tests. |
| Retry lifecycle | Persist synthesis retries independently of session-note writes; bounded backoff and queue-retention recovery; repeated drain does not repeat successful work. | `models.py`, `session_tracker.py`, `worker.py`, `drain.py`, retry tests. |
| Knowledge roles | Explicit note taxonomy, observable fallback, typed ingestion, separate new session records, and legacy file compatibility. | `knowledge_pack.py`, `note_writer.py`, knowledge tests. |
| Safe writes | Vault-confined Markdown routes, protected instruction/settings/template/session paths, exclusive creation, and stable retry markers. | `note_router.py`, knowledge tests. |
| Corrections | Exact extraction dedupe preserves different findings on a familiar topic; cleanup follows the same rule. | Inbox and cleaner regression tests. |
| Ingestion | Generic extraction prompts, sanitized slugs, source backlinks to actual outputs, preserved human text, scoped existing merge candidates. | `ingest.py`, knowledge tests, refreshed ingest skill. |
| Retrieval | Current QMD array/file JSON and legacy object/path support, actual CLI flags, executable discovery, deadlines, explicit collection, local source reads, mode-aware scores. | `qmd_search.py`, QMD tests and installed CLI contract check. |
| Model preparation | Keyword synthesis and link retrieval by default; vector synthesis or semantic ingestion explicitly enabled. No hybrid cold-start path in the worker. | Config and routing tests. |
| Sharing | Destination-and-vault-scoped receipts, nested filename collision protection, conservative legacy receipt migration, and privacy exclusion parsing. | `push.py`, push tests. |
| Installation | Preserve checkout/bundle provenance and authored settings; use this fork for unmanaged releases; populate only missing vault starter files. | Installer/update tests. |
| Health | Surface stale/failed import capture and current receipt counts, without network calls in JSON health. | Health/provenance tests. |
| Human knowledge use | Generic typed templates and Obsidian graph defaults that separate sessions, staging and durable knowledge. | Vault template JSON and workflow guide. |

## Wider developments captured as integration guidance

[Current system](current-system.md) records the reusable contracts learned outside this package:

- Canonical shared knowledge, observations, session evidence, and QMD indexes have different authorities.
- Personal staging, tenant commit/pull, reviewed promotion, consolidation, and indexing need separate receipts and success signals.
- Current-state projection, standing preferences, supersession, staleness, and reviewer corrections improve task context beyond flat note search.
- Shared mergers need bounded work, single-writer discipline, optimistic revisions, replay protection, tombstone-aware ledgers, and conflict preservation.
- Audience scopes form reader sets; cross-tenant or wider-audience synthesis is not justified by a similar title or service-role key.
- Relevance thresholds need human labels, task-level evaluation splits, and pinned models. A per-note floor does not certify the whole retrieved block.
- Hook configuration, exercised delivery, and operational health are distinct claims.
- Recall before action needs explicit agent search or a custom host integration that returns source-linked leads. Capture, synthesis-time retrieval, and prompt recall are separate paths.

These are documented design lessons. Claude Note does not bundle the external authorization server, shared consolidation worker, task context-pack service, hosted relevance scorer, or fleet sync scheduler.

## Knowledge-loop demo

[Remember. Then act.](knowledge-loop-demo.md) (superseded by the [2.0 demo](demo.md)) was a 112-second walkthrough for technical builders, with captions, a controlled retry example, and an inspectable media manifest. It shows recall, source verification, a tested change, capture, correction, and reindexing. The custom prompt-recall integration is labeled separately from bundled capture hooks.

The film and its receipts preserve the reviewed source snapshot from PR #8, including on-screen proposal labels. Repository merges do not change that historical evidence or update installed services. Install from the intended checkout and compare its recorded commit when verifying an update; the package version alone cannot identify the installed source revision.

## Validation boundary

The repository test suite uses synthetic transcripts, disposable vaults, mocked model/API calls, and fake installer executables. Local CLI help and the installed QMD keyword interface are checked separately. Repository validation does not exercise private accounts, upload personal notes, install this branch over a running service, or publish a release tag.

The demo's offline recall probe checks context formatting with synthetic input and mocked retrieval; it does not test host delivery, retrieval quality, or latency. Its SQLite replay verifies one controlled retry boundary, not a production backend or customer outcome. The delivered MP4 passed full decode, playback review, and audio/video checks described in its manifest (retired in 2.0; see git history at `61d1415`).
