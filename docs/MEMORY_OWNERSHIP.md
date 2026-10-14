# Ownership and validity of memory

Rust's ownership, borrowing and lifetime rules provide a useful discipline for
Claude Note's Python and Markdown boundaries. A memory consumer needs a valid
reference to evidence; access to that reference must not manufacture authority
or a second right to commit an operation.

## Canonical owners and immutable readers

Human-readable records and immutable evidence snapshots are canonical. Search
indexes, summaries and prompt passages are derived views. Extraction, ranking
and delivery must identify the same source revision and passage rather than
independently copy text and lose its provenance. A captured snapshot can remain
valid when the live transcript changes.

Rust permits shared immutable references or an exclusive mutable reference.
Claude Note applies that discipline through confined writes, canonical path
identity, process-shared locks and revision checks. Human editors can bypass
cooperative locks, so a writer must still detect changed content and stage a
conflict. [Rust borrowing rules](https://doc.rust-lang.org/book/ch04-02-references-and-borrowing.html)

## Validity belongs to the checked revision and context

Claim authority binds an exact digest and unchanged supporting evidence.
Applicability also depends on environment, repository, validity dates,
dependencies and audience. A prior successful check cannot authorize a later
operation after those inputs change. Delayed mutation and egress must validate
their own current preconditions.

A historical lesson remains available after it stops governing. Rust lifetime
annotations describe validity relationships; they do not extend the underlying
value's lifetime. Similarly, retaining a note does not extend the applicability
of its advice. Context already delivered to a model cannot be recalled by this
mechanism. [Rust lifetimes](https://doc.rust-lang.org/book/ch10-03-lifetime-syntax.html)

## Import publication owns one captured revision

An import publication binds a source, logical session, exact captured digest,
immutable snapshot, stable event IDs and a per-session ordinal. A durable intent
retains its first event pair and timestamps. Only after restrictions and the
canonical receipt are durable does a complete queue batch become visible.
Retrying reuses that pair, including after the seen store was not saved. A
normalized conversation from another export may add origins but cannot change
canonical captured bytes under the same logical revision; all other differences
fail as identity conflicts.

The worker verifies every imported reference before acknowledging capture,
including older revisions that will not become the current view. It reads the
bound snapshot while preserving the original transcript identity. An older
publication cannot replace a later imported revision or a newer native hook.
A completion event stores its pending synthesis intent in the same durable
capture save, so a crash cannot acknowledge away unscheduled work. Capture
acknowledgement and synthesis completion are separate durable states;
this provides idempotent local capture, not exactly-once model execution or
external effects. Hook/import ordering uses local event times; wall-clock
rollback across hooks remains a limitation.

Queue polling inspects canonical metadata globally, then verifies all visible
import snapshots for a session before delivering its events or acknowledging
an ordinary journal cursor. That check includes older, already captured
revisions, so they cannot hide behind a newer binding. The verified session set
is local to one reader invocation; capture and actual source use perform their
own current checks. Idle captured batches need no repeated source read.
Snapshot-only corruption in idle history can consequently be detected later,
when that session needs work or the evidence is used. Metadata checks still
scale with queue size, and all visible revisions of a selected session must be
read. This is neither a stat-based digest cache nor a historical corpus audit.

Authored private and claim audiences, including protected transitive lineage,
are conservatively retained with the snapshot. Later source edits cannot loosen
those restrictions. Configurable owner policy stays live and original supplied
and canonical source identities remain subject to it. Cleanup may remove an old,
fully acknowledged queue batch; canonical receipts, restrictions and evidence
remain for replay and recovery. Publication history is bounded at 10,000
revisions per session and fails closed without evicting history. Origin-only
normalized replay comparison permits individual records up to 8 MiB; larger
comparisons remain explicit recovery failures. New state fields load older records with defaults;
older releases cannot safely read the new fields. Rollback requires preserving
and reconciling state, rather than resetting capture history.

## Retrieval workers have one operation owner

A pending health metadata refresh owns the lexical worker until completion or
its original deadline. Later searches wait for that operation to settle before
borrowing the worker; simultaneous refresh callers share it. A shorter waiting
caller cannot extend or cancel the shared operation. Searches still use their
original deadlines, so waiting can leave too little time to run. Refresh failure
remains visible as historical degradation even when a later search succeeds.

Native model operations remain serialized. Cancelling queued work prevents its
admission; cancelling an active caller does not prove that native execution
stopped. An active native deadline poisons the owned runtime rather than admitting
another model operation beside it. Search and reranking are separate queue jobs,
so another search can consume the remaining rerank budget. These boundaries do
not establish cold-process or concurrent-request latency.

The rerank operation also owns its observation: only a complete, finite scoring
response naming the configured model may establish current readiness. SDK
fallback, unknown model, partial scores and malformed results cannot borrow a
previous operation's successful state. They preserve retrieval order and clear
current readiness; a later valid result may restore it without erasing failure
history. This is a runtime response contract, not a trace of native inference or
SDK-internal text truncation.

## Index refreshes borrow a captured generation

`IndexRefreshLease` is a frozen, validated metadata object for an existing index
writer. Its `IndexedRevision` references capture the collection, relative path
and exact saved revision, together with generations and index/configuration
identity before indexing. Each tracked journal has one canonical index owner;
another index cannot clear its freshness or automatically take ownership.
The caller retains ownership of its commands, lock,
checksum checks, timeouts and heartbeat.

Each capability receipt also binds the configuration and model selection that
was checked. Changing those inputs invalidates its current applicability even
when saved generations are unchanged. Latest tracked revision metadata survives
dirty-queue cleanup so the new scope can be checked again without a new write.

On first binding, unscoped capability counters are reset and any earlier pruned
generations become an explicit legacy baseline. Freshness describes retained
tracked mutations after that baseline; it cannot reconstruct lost historical
receipts or prove preexisting corpus coverage.

The lease progresses through `open`, `finishing` and `complete`. Finish checks
indexed revisions through a bounded read-only observer, then records the reported
stages and verified results before acknowledging anything; replay can recover
partial acknowledgement with the same flags and saved proof. QMD exiting zero
alone is insufficient: skipped reads, skipped embedding and partial chunks must
not manufacture freshness. A different completion, expired
lease or changed scope cannot broaden the acknowledgement. Ambiguous project
configuration and unsupported database overrides leave freshness unacknowledged.

For example, a refresh captures generation 12, then a writer saves generation
13. If text indexing succeeds and embedding fails, completion may acknowledge
lexical generation 12 only. It cannot claim vector freshness or generation 13.
A job that skips a contended lock never begins a lease. A crash leaves durable
state for reconciliation rather than an invented success.

These completion flags are reports from a cooperating local wrapper. The observer
checks indexed hashes and vector chunks against the pinned writer's recorded
expected totals for the selected model URI and pipeline fingerprint. That
metadata does not attest the historical weight bytes
used to produce a vector or establish semantic truth. Neither the reports nor
the local database are a security boundary against a compromised host.
Copying an evidence reference
also does not copy a right to approve a claim or repeat an effect. Rust's
`Clone` can share underlying state; reference copying and independent knowledge
creation must remain distinct. [Rust Clone](https://doc.rust-lang.org/std/clone/trait.Clone.html)

## Scoped cleanup and durable recovery

Descriptors, locks, temporary files, timers and subprocesses have scoped owners.
Context managers and `finally` release them on ordinary exits. Persistent
intents and receipts handle abrupt termination, when cleanup may not run.
Ending a query or dropping a reference never authorizes deletion of canonical
notes, corrections or supporting evidence. Retention is a separate owner policy.
[Rust Drop](https://doc.rust-lang.org/std/ops/trait.Drop.html),
[destructor limits](https://doc.rust-lang.org/std/mem/fn.forget.html)

## Acceptance contracts and next boundaries

- Changed or deleted evidence invalidates authority without deleting history.
- Concurrent writers serialize the complete read, check and commit operation.
- New writes during indexing remain dirty; failed stages never acknowledge them.
- A completion replay is idempotent; changed flags, expired leases and changed
  index scope cannot reuse the earlier capability.
- Cancellation releases ordinary resources; crash recovery preserves canonical
  records and reconciles only known intents.

Other cross-module boundaries should adopt the same small frozen evidence
references and validated operation outcome variants. Frozen Python objects improve API
discipline; runtime validation, detached attestations and filesystem boundaries
remain necessary. Ownership here describes access and cleanup responsibility,
not legal ownership, semantic truth or human approval.
