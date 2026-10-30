# Public learning-loop study

This experiment measures whether an explicit Claude Note learning loop helps later tasks under a fixed memory-context budget. It uses actual model actor and extractor calls on fictional public workflows. It does not enable installed capture/synthesis workers or use personal history.

The reusable API is `claude_note.learning_loop.LearningLoop`. An explicit caller supplies an owned root, run/arm/scope identity and an extractor callback. Call `observe(task_id, task, actual_response, verifier_feedback)` after a real task, `learn(observation)` to extract proposed lessons, and `retrieve(next_task)` before the next task. The real capture parser, restrictive source lineage and mutation writer retain evidence. Consolidation records current capture and prior supplied memory revisions; superseded versions remain immutable. Scope, temporal cutoffs and exact evidence anchors constrain reuse. Proposed text is not verified semantic truth or authority.

## Frozen comparison

Five independently permuted fictional companies each supply twelve tasks to three arms (180 scheduled cells, sixty paired tasks). All use the same actor model, at most two actions and 4096 memory characters.

- **Learning:** model extraction and consolidation from its own actual outcomes after episodes1–11.
- **Frozen:** the same algorithm learns from its own first three outcomes, then uses that snapshot.
- **Growing raw history:** its own captured journals grow; fixed512-character windows are selected by lexical overlap and recency.

Each arm owns its experience. The initial learned and frozen notes can differ because they are extracted separately. This estimates end-to-end trajectories, rather than updating an identical shared snapshot.

New conditional procedures, a scoped correction, precedence and irrelevant duplicated notifications appear after acquisition. Backend feedback explains only a rule applied to an actual current action. No future answer or authored gold lesson is supplied to actors/extractors. This is learning from operational explanations, not discovery from labels alone.

The primary endpoint is first-action success on transfer episodes5,8,9,10,12. Exposure tasks remain separate. Report early/middle/late results and all five paired company differences against both controls. Final success can saturate because a corrective retry receives the current item's required queue. Five company configurations are not180 independent samples or five unrelated domains. A positive result does not establish indefinite, monotonic or production-wide improvement.

## Execution and evidence

Before real calls, freeze task/evaluator source, analyzer, prompts, provider, loop and controller at a Git commit and obtain independent exact-source/profile review. The runner requires a separate approval JSON matching its source manifest and commit. Analysis files/results stay outside the checkout during execution.

```sh
python3 -I -B experiments/learning_curve/test_tasks.py
python3 -I -B experiments/learning_curve/test_provider.py
python3 -I -B experiments/learning_curve/test_study_runner.py
python3 -I -B experiments/learning_curve/test_analyze_study.py
python3 -I -B experiments/learning_curve/analyze_study.py --self-test
python3 -I -B experiments/learning_curve/run_study.py --execute --approval /absolute/review.json --output /absolute/new-attempt
python3 -I -B experiments/learning_curve/analyze_study.py --input /absolute/new-attempt --output /absolute/new-analysis.json
```

Real generation uses pinned `gpt-4.1-mini-2025-04-14` Responses API, temperature0 and `store:false`. The existing environment credential reaches only an owned transport child; redirects and proxies are disabled. No key is written to requests or artifacts. Public requests/responses, usage, source hashes, captures, citations, actions, unavailable attempts and budget settlements remain inspectable.

All actor and extractor calls share a $2 maximum reservation ledger. The curriculum requires at most430 generations; the transport additionally caps500. Each request is at most32KiB, HTTP response256KiB, actor output256 tokens, extraction1536 tokens. Unknown usage retains its reservation and stops further calls. Auth/quota/protocol failures are retained without automatic retry. Reported uncached token cost is a conservative estimate, not a billing statement.

This small public profile has a512MiB free-space floor plus64MiB projected inflight allowance, checked before cells. A16MiB logical output threshold is a cooperative pre-cell stop threshold, not a hard cap or filesystem reservation. At most three already-admitted chains can finish a burst of two actor calls, capture and one extraction each. The overall1800-second admission lifetime can likewise be followed by the final bounded in-flight transport/cleanup. These guards do not change the production import6GiB reserve and do not bound total RSS.

The pre-results analyzer independently regrades actual actions, checks capture/own-arm temporal provenance, reports missing outcomes separately, rejects source drift, audits the budget and uses company-level paired uncertainty. Raw fixed windows can split JSON; this is a limited lexical raw-history comparator, not an optimal history agent. Learning receives extra explicitly reported extraction compute.

## Verification scope

The independently frozen `claude-note-learning-lineage` development contract at trusted Git revision `1f91b8d557c019aeee21eb3a333a61c74eab3533` produced genuine26-pass/4-assertion-failure baseline RED, then30-pass GREEN at `48e9a9cc06fcbee55f26b251fffb61ca9ef1673c`. Accepted source hash: `aad1bb4ed413e33e449d250685eeca5a017751dc7579c9614355d50a1c582515`. This certifies the declared lineage/ownership contract only. Actual learning benefit, provider behavior, workflow generalization, production native retrieval, private-data operation and installed host delivery require their own evidence. No baseline is promoted from deterministic fixture scores.
