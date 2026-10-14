# Verified memory implementation and rollout

Base: Claude Note 2.0.2, `5c1ec7e079eae7d6fe2f82b149a2f22b9bfc073a`.
This implements the balanced design from the October 13 architecture study.
A passing unit test establishes a contract, not an agent productivity gain.

| Workstream | Implementation and acceptance gate | State |
|---|---|---|
| Capture | Source/revision/line anchors, original message/time/parent IDs, explicit unknown outcomes, lossless local outputs; decisive correction/exit fixtures | Implemented; rollout checks below |
| Worker | Persist extraction before effects, stable generations and operation receipts, append-only checkpoints, bounded retries with parked work; crash replay | Implemented; rollout checks below |
| Synthesis | Bounded candidate catalog, strict schema/citation rejection, assigned provenance, delta evidence; important correction/no-memory fixtures | Implemented; rollout checks below |
| Claims | Canonical Markdown records, source/evidence/applicability, detached revision-bound attestations; forged approval/stale version tests | Implemented; rollout checks below |
| Mutation | One confined descriptor boundary for every writer, physical append or staged human-file replacement, stable operation identities; process concurrency and crash tests | Implemented; rollout checks below |
| Documents | Complete bounded anchored sections, resumable section cache, preserve late qualifications/tables, explicit blank extraction error | Implemented; rollout checks below |
| Consolidation | Exact duplicate caution, preserve overflow as suggestions, typed claim relations, no similarity deletion; correction retention fixtures | Implemented; rollout checks below |
| Freshness | Saved/text/vector/eligible states and dirty generation journal; fresh lexical evidence without vectors | Implemented; rollout checks below |
| Retrieval | Raw semantic intent, Unicode/identifiers, scope before top-k, exact source/version identity; multilingual and scoped starvation fixtures | Implemented; rollout checks below |
| Context | Same anchored passage ranked and delivered, token budget, authored metadata distinguished from attestations | Implemented; rollout checks below |
| Runtime | Absolute deadlines across calls/read/judge, bounded expiring queue, observable degradation/capabilities; overload tests | Implemented; rollout checks below |
| Sentinel | Structured codes, conservative auth/quota distinction, pre-search bounded dedup; misleading log and transient rate-limit fixtures | Implemented; rollout checks below |
| Privacy | Source audience intersection before every external call, redaction, minimal subprocess environment, bound staging intents and revocations | Implemented; rollout checks below |
| Health | Public indexed default-server canary distinct from isolated index and actual host delivery; break-one-stage fixtures | Implemented; rollout checks below |
| Evaluation | Exact revision oracle, full fingerprints, all-attempt denominators, executable longitudinal traces, paired strong baselines, frozen holdouts, scheduled reconciliation | Implemented; Claude outcome run quota-blocked |

## Rollout gates

1. Synthetic temporary fixtures only; never use personal transcripts as test data.
2. Run the full Python regression suite, Node syntax/server tests, concurrency and crash fixtures.
3. Run actual isolated agent tasks with executable checks and no-memory/static/scoped lexical/hybrid baselines. Report failures and missing attempts. Holdout scores remain separate from development fixtures.
4. Independent review, fix findings, merge through a PR. Main is the sole release source.
5. Build signed/checksummed release artifacts; preserve installed bundle provenance. Bump the Sweat Assistant pin on reviewed main, build/deploy through its documented release path.
6. Exercise installed hooks/server/worker on the public fixture. Do not infer host delivery from process existence or an isolated named-index probe.
7. Schedule recurring checks with actionable notifications. Subsequent changes get their own regressions and paired evaluation; no automatic baseline replacement on a regression.

## Authority and migration

Markdown remains canonical. A claim's authored `status` is a request; a detached private attestation binds the exact claim JSON and unchanged evidence. Approval and verification are separate from source authorship and egress authorization. A compromised local account can access the key; these receipts prevent note-content forgery, not host compromise.

Supersession retains historical demotions only while the attested issuer's premises remain applicable. Dependency invalidation and unresolved contradictions remove governing effects transitively; circular dependency or succession relations confer no governing effects. An independently evidenced supersession can resolve a historical contradiction, while a correction relying on a conflicting premise cannot. A same-ID marker with different bytes cannot borrow a canonical claim's receipt or replace its relation graph; passage anchors bind the exact claim digest.

Existing prose remains an unverified lead. Machine claim scope/validity is enforced without granting legacy notes authority. Unattended replacements of human or unknown files become immutable proposals. Inspect `notes_staged` and `.claude-note/mutations/pending`; staging is not a successful canonical update.

External model/staging use requires source audience configuration. The default is local/host; existing background processing can capture locally while external synthesis awaits an explicit owner policy. No automatic consent is inferred from a note saying it is approved. Example private owner configuration:

```toml
[egress]
default = ["local", "host"]
[[egress.rules]]
source = "/absolute/authorized-source/*"
audiences = ["local", "host", "model"]
```

Secret redaction is an additional boundary and cannot identify ordinary business confidentiality. Staging revocation records are surfaced for reconciliation; the external destination must acknowledge removal before a revocation is considered complete. No organizational authorization or remote canonical consolidation service is claimed.

## Deliberately deferred designs

A graph database, hosted memory framework, learned utility policy, general OCR/layout model and distributed queue are not prerequisites. Adopt them only after the controlled experiments demonstrate a benefit against these corrected baselines. The local journal does not use SQLite WAL.

## Exercised evidence and limits (October 13, 2026)

The settled source passed 341 isolated Python 3.11 regressions and all 12 Node server contracts using the installed QMD SDK. The Sweat Assistant release checkout passed both TypeScript checks, 8,625 frontend tests and 714 Swift tests. Independent review fixes include source-origin privacy, claim dependency/conflict propagation, canonical passage anchors, index-generation acknowledgement and failed-answer denominators.

The final corrected public synthetic hybrid preflight returned the current contract source in 16/16 cases versus 14/16 for the pinned 2.0.2 baseline. All 32 queries were available. On this warm, small corpus, median latency was 262 ms versus 43.5 ms and nearest-rank p95 was 379 ms versus 84 ms. The corrected route exercised actual local embeddings/reranking and returned exact source revisions and passage anchors. This is source reach, not completed-task correctness: ordinary proposed prose appeared in three of four poison cases, and other-environment prose could appear as an unverified lead. Only structured claims with earned authority receive enforced governing applicability.

Six fresh-context Codex responses, one public development task across six baselines, passed the trusted parent-side executable verifier (four generated inputs per response). All six baselines passed, including no memory; this smoke test establishes no measured memory benefit. Its provider/model identity and cost are unknown, so it is kept separate from Claude outcome history and cannot support trend comparisons.

Two real Claude CLI pilots failed at the account's weekly quota before producing a structured answer. Those failures remain in the scheduled denominator. No unchanged provider retries will run before the reported October 17 reset. Full development/holdout task outcomes, real extraction, and actual host delivery remain rollout checks; simulated policy fixtures and direct process invocation do not establish them. A daily continuation checks the release, runs isolated regressions, and resumes the bounded public Claude evaluation when the account is available.
