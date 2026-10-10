# Memory and vault modernization — 10 October 2026

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

These are documented design lessons. Claude Note does not bundle the external authorization server, shared consolidation worker, task context-pack service, hosted relevance scorer, or fleet sync scheduler.

## Validation boundary

The repository test suite uses synthetic transcripts, disposable vaults, mocked model/API calls, and fake installer executables. Local CLI help and the installed QMD keyword interface are checked separately. Repository validation does not exercise private accounts, upload personal notes, install this branch over a running service, or publish a release tag.
