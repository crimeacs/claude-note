# Claude Note proof-carrying pilot

This experiment compares source admission with independently checked derivations.
It changes no installed runtime or production service. It supports an adoption
decision for a narrow verification boundary, not a productivity claim.

## Frozen comparison

The baseline is the real current claude_note.verified_claims implementation at
Claude Note main 8fbd0fb7690460637d05a906b71b36d9ce48956b. File SHA256
6077c4ef28078e755b54cea485b0c3506e99c6aec17bdade2ad8220e34cb04ec
also matches the installed 2.1.26 component. The adapter uses real confined
Markdown publication, detached HMAC receipts, evidence hashes, revocations and
applicability checks in constructed temporary public vaults.

| Arm | What it checks | What acceptance establishes |
| --- | --- | --- |
| Current validator | Canonical claim/evidence, attestation, scope/time/audience and claim relations | Source-stable claim admitted under the existing contract |
| Current + Python certificate checker | Same admission plus independent small executable Horn certificate verifier | Exact requested goal follows through submitted steps under frozen facts/rules |
| Current + Lean certificate checker | Same admission and same submitted artifact, checked by pinned upstream Lean executable | Same conditional derivation warrant, backed by a proved checking algorithm |

The proof arms share one untrusted upstream producer. They are independent
verifier implementations, not independent end-to-end engines. A proof-valid
target can still be denied by current source admission. The baseline does not
certify arbitrary domain entailment. An admitted but unsupported target is a
feature boundary, not a discovered violation of its provenance contract.

There are exactly 100 public synthetic scenarios: 80 development cases and 20
independently authored holdouts. Development covers 30 logical mechanisms/
counterexamples, 40 source/applicability mechanisms and 10 artifact adversaries.
These are contract scenarios, not 100 independent real-world workflows.
The implementation author did not inspect holdout labels before implementation
freeze. A preliminary baseline-only preparation processed all 100 fixtures when
the holdout input file arrived; only the 80 development labels were inspected.
That retained preparation is not a preregistered blinded execution.

Cases, manually reasoned answer keys and component hashes are saved. Same case
membership, goal, admitted facts, rules and submitted certificate feed the paired
proof arms. Predictions become durable before semantic answer-key access.
Opaque answer-key hashing occurs before execution.

## Pins and execution

- Open Ontologies: 2ecb50c2ca7e99778d767251ec8ca64146c1669f, MIT.
- Lean: upstream leanprover/lean4:v4.33.1, from the pinned project.
- Pyoxigraph: 0.5.11, Python 3.12 Linux wheel SHA256
  e3097d62e4fb903238ef074744ecf54c4328cf20e7787e925e670f6f7d33d345.
- Elan: v4.2.4, Linux archive SHA256
  42b94d4244e8353142c456ec0e4ca6528fd898a6c604d4059f494e706e431f63.
- Actions pinned to commits; read-only repository permission; no benchmark
  provider credentials or private vault inputs.

CI builds actual oo-horn from pinned Lean source, prints version/hash and the
soundness theorem's axiom dependencies, then executes the pilot. No simulated
Lean verdict is substituted. The supplied Lean API documentation describes a
newer development version; it does not override the checker toolchain pin.

For a prepared Linux environment:

~~~sh
python3 -I -B experiments/proof_pilot/test_pilot.py
python3 -I -B experiments/proof_pilot/run_pilot.py \
  --upstream /absolute/path/to/pinned/open-ontologies \
  --checker /absolute/path/to/pinned/open-ontologies/lean/.lake/build/bin/oo-horn \
  --output /absolute/path/to/a/new/attempt
~~~

Local Lean installation is avoided on the nearly full user disk. No QMD loader,
native recall experiment, model download, provider invocation, source-policy
expansion or private synthesis is part of this pilot. No installed worker flags,
owner configuration, external Python, app bundle, assistant source pin, retained
history or production release is changed.

## Artifact contracts

Each case keeps actual admission, producer identity, submitted certificate,
verifier outcomes and stage timings. Predictions JSONL is append-only with
flush/fsync; JSON views are replaced atomically. Failed/unavailable attempts
remain in the denominator. Unknown is not a successful rejection.

The answer-key name certificate_valid means validity of the **bound submitted
artifact**, including expected snapshot and goal identity, not always raw Lean
acceptance. Injected assertions, swapped rules and changed goal identities are
binding-wrapper refusals before Lean invocation. checker_invoked,
raw_checker_valid, parser diagnostics and wrapper reasons distinguish those
from checked invalid derivations.

A valid certificate need not establish the requested goal. Exact goal membership
is separate. A missing goal is not proof of non-entailment. Original producer
results are compared with authored entailment expectations separately from
corrupted proofs; no producer completeness theorem is claimed.

All supplied rules are fixed experiment assumptions. Successful custom-rule
checks retain entailed_under_supplied_rules. They do not become unconditional
world truth. This corpus does not vary a separate rule-approval policy.

The supported profile is finite positive Horn rules over safe ASCII URNs,
32 rules maximum, eight body atoms per rule, 256 input facts/derivation steps,
and bounded artifact bytes. It does not cover all RDF/OWL, natural-language
extraction, negation, existentials, arbitrary literals or external tenant ACLs.
There is no admission-epoch/ABA validation claim. Checker outage and budget
exhaustion are not among the 100 scenarios; supported accounting is not evidence
those conditions were measured.

## Timing and trust

Admission timing includes public fixture construction and attestation. It is not
comparable to verifier-only latency. The proof checkers report wrapper-inclusive
verification times, and proposal time is separate. No production latency or
total/peak memory bound follows from these fixtures. Binary hashing before/after
use is included in the Lean wrapper measurement.

Lean proves soundness of the checking algorithm under formal assumptions.
Executing its compiled checker is not fresh kernel proof replay per case.
Parser, compiler/runtime, binding wrapper, source admission, supplied rules and
evidence-to-atom mapping remain trust boundaries. Explicit atoms in this pilot
avoid rather than solve extraction error.

Actual-agent longitudinal learning, the six paired memory arms with each arm's
own captured/learned carry-forward, host delivery, cold retrieval reliability
and all original memory architecture gates remain separate open work.

## Development harness scope

Problem ID: none registered for this new pilot. The versioned development
harness was inspected, but this pilot is **uncovered by the development harness**:
its current Python-unittest regression runner has no reviewed task-specific
baseline/RED contract for this additive multi-arm/Lean experiment, and no Lean
adapter. No unrelated Regtime problem, import failure, installation smoke or
invented ACCEPT receipt is used. The reviewed pilot Git revision and source
manifest identify the code tested by the 22 independent repository contracts
and actual pinned GitHub CI run. This is test/build evidence, not certification
of production integration, extraction quality, customer acceptance or release.
