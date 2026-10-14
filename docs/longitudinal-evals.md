# Longitudinal evaluation controller boundary

`longitudinal_tasks.py` is a config-free, standard-library spec and phase receipt
journal. It imports no application configuration, discovers no tools, and calls
no host, provider, QMD, indexer or worker. It has no CLI or automatic schedule.
It is the first controller boundary for a future own-memory experiment, not an
implemented or exercised production learning loop.

The current independent task harness supplies author-written memories. A future
longitudinal experiment must instead capture each arm's own actual experience,
extract supported changes, persist safely, establish index coverage and deliver
that evidence to its next action. A paraphrase and a later correction or
changing environment must test whether memory helps without preserving stale
mistakes. The existing independent suite remains a separate control/regression
set; giving its phase labels a chronological name does not establish learning.

## Frozen schedule and interventions

`Spec()` declares two public development trajectory IDs, seven arms and four
ordered phases (`acquire`, `transfer`, `correct`, `corrected_transfer`): 56 episode
cells. It contains identifiers, control policy and budgets, not lessons, expected
answers, task code, transcripts or oracle data. Actual trajectory definitions and
trusted executable verifiers are later adapters.

The note-learning arms are scoped lexical, dense-only, pinned legacy hybrid
retrieval and corrected hybrid. No-memory and static controls withhold learned
context; bounded-history uses its own prior raw history. Their extract/mutate/index
stages are explicitly disabled, never reported as successfully exercised. These
stages are also disabled after the terminal phase because there is no scheduled
next action. Every condition still owns its own capture history.

Within each cell, stages are `retrieve → act → capture → extract → mutate → index`.
Every predecessor stage must have a durable resolved outcome before the next is
admitted; a new phase also requires the previous phase's full chain. A failed or
skipped stage is resolved controller work, not evidence of usable memory. The
coordinator must decide which dependent stages to skip instead of claiming that a
failure completed the pipeline.

## Journal API and recovery

Supply a trusted absolute private directory, exact `Spec` and stable `run_id` to
`PhaseJournal`. Use it as a context manager. The immutable manifest binds the
spec. `run_stage(cell, stage, callback, adapter_revision=SHA256, ...)` writes and
fsyncs a checksum-bound intent before callback admission, then publishes a separate
outcome. Intent inputs bind adapter revision, exact references, reservation and
skip decision. A completed/failed/skipped/disabled replay returns the old receipt
without invoking the callback again. Changed replay inputs are rejected.

An interrupted intent without a valid outcome returns `unknown` and never
repeats the callback automatically. It blocks later stages in that arm until an
explicit future recovery/reconciliation procedure is designed. A callback
exception records failure with unknown effects; this does not mean it had no
effects. Torn, altered or incompatible records fail closed; they do not cause a
cursor reset. There is no automatic deletion, rollback, receipt repair or claim
of end-to-end exactly-once external execution.

Records are bounded canonical JSON, singly linked private regular files, written
with exclusive creation and fsync. Final file symlinks are refused. A nonblocking
file lock plus an in-process lock covers admission and callback completion;
concurrent journal users fail busy. `close()` also fails busy while an operation
or callback borrows the directory descriptor; an idle close is idempotent.
The supplied root and its parents are
trusted caller-owned paths; this module is not a general untrusted-path service.
Checksums and chained predecessor digests detect inconsistent/accidental edits;
a same-user adversary capable of rewriting a whole consistent journal can
recompute them. They are not cryptographic authority or hostile-host protection.

## References, cutoffs and immutable views

A completed callback may register bounded `Artifact(source_id, revision)` values.
`reference()` binds a registered artifact to the producer receipt digest.
Before invoking another callback the controller rejects a different run,
trajectory or arm; a future phase; a same-phase stage that has not preceded the
target; a missing/failed producer; and a changed artifact revision or receipt.
Retrieval therefore sees only prior phases; later stages can also reference
completed earlier stages in their current phase.

`StageContext` supplies the previous-phase cutoff, declared references and deeply
immutable prior receipt views. Its `arm_root` is a path hint, not an operating
system capability. An arbitrary Python callback can still read its own globals,
other files or network unless a separate executor prevents that. Reference
checks do not establish OS read isolation or prevent a malicious callback from
inventing observations. Source IDs are opaque; actual adapters must verify file
membership, bytes and scope before registering them. The journal does not expose
`case.lessons` or an oracle interface, and cannot prove that an arbitrary callback
has not obtained an oracle elsewhere.

Source revisions and receipt digests bind recorded evidence. They do not grant
claim authority, approval, an audience or model egress. Host-only derived claims
remain restricted. A future provider adapter must enforce actual source closure
and applicable public-fixture policy; the installed owner's model grant and
configuration remain unchanged.

## Budgets, time and denominators

A journal's declared conservative reservation cap is positive and at most $2.
Reservations use exact cents and remain consumed after failure or unknown
completion; no successful replay reserves again. Admission over that cap records
a skip without invoking the callback. These are caller-declared reservations,
not observed provider charges or a global account ledger. The future run
coordinator must account for all other billable work and other journals against
the existing $2 recurring-run cap. A larger study must be sharded across admitted
runs, not multiple journals used to bypass a run cap. No automation is added or
changed here.

The finite stage time cap is cooperative. An adapter receives an absolute
monotonic deadline, clipped to a supplied shared deadline. Expired admission
skips; expiration during intent persistence records failed/not-invoked without
entering the callback. A late return records failed/unverified effects. The journal cannot
interrupt an arbitrary callback, kill or reap it, or promise a hard wall-clock
bound. The future phase coordinator must pass one shared phase deadline, and
actual external work needs independently reviewed subprocess/native ownership
supervision. No hidden replacement or cancellation is supplied by this module.

`summary()` retains every frozen episode cell, including failed, skipped, unknown,
corrupt, not-started and dependency-blocked rows. Its task rate divides
`reported_task_successes` by all scheduled cells. Success means only that an
`act` callback's completed outcome explicitly reported `reported_success=True`;
it does not attest a trusted executable verifier, completed learning pipeline,
provider transport, native inference or real host delivery. The production task
adapter must add that evidence before any measured-benefit claim.

`callback_admissions` counts durable intents that allowed a callback. A crash can
occur after intent persistence but before callback entry. Thus admissions are
neither actual callback-entry counts nor external-effect/transport counts.
Reservations, callback reports and journal outcomes must stay separate from
later HTTP/model/index/verifier attestations.

## Validation and next slices

Tests use public strings, injected Python callbacks and owned temporary files.
They cover chronological/cross-arm denial before adapter entry, exact artifact
binding, immutable views, completed replay after reopen, interruption before and
after callback entry, receipt corruption and torn publication, exclusive
admission, conservative budget skips and full scheduled denominators. These tests
exercise controller/file contracts only.

The next slices remain separate: fixture-scoped production capture/extraction/
mutation adapters; dynamic per-arm indexed corpus manifests and passage/context
binding; a budget-admitted public provider pilot after quota availability; actual
isolated host hook/delivery evidence; and sealed project/topic/time-separated
confirmatory tasks with paired cluster-level outcome analysis. None follows from
a green journal test or a source checksum.
