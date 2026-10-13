# Evals: is recall finding the right notes?

`claude-note eval` answers three questions, cheapest first, and keeps a history so you can see
whether a change helped:

| Question | Command | Costs |
|---|---|---|
| Does recall inject the note that answers a question? | `eval run` | free (local QMD searches) |
| In real use, does the agent open what recall injected? | `eval live` | free (your recall log and transcripts) |
| Does recall change the answer? | `eval answers` | Claude CLI calls (opt-in) |

`eval dashboard` draws all three into one HTML file, and `eval report --json` gives an agent the
same numbers plus what to fix.

```bash
claude-note eval probes -n 80        # write questions from your own notes (Claude CLI, ~8 calls, once)
claude-note eval run                 # score recall on them; appends to the history
claude-note eval dashboard           # open the dashboard
claude-note eval report --json       # for an agent: metrics, failures, advice
```

## Retrieval: labelled questions

Each case is a question and the note that answers it:

```json
{"question": "Gmail calls fail with 403 since I added the Calendar scope", "expect": ["google-oauth-new-scope-needs-reconsent"], "split": "test"}
```

`eval run` asks recall exactly as the hook would and records, per question, whether the expected
note was injected, at which rank, whether it was found but cut before injection, and how long it
took. It reports:

- **right note injected (hit@k)**: the expected note was among the `inject` notes;
- **MRR**: 1.0 when it was always first;
- **found nothing**: recall injected no note;
- **ranked out**: found among the candidates, cut by the `inject` limit (a ranking problem, not a
  search one);
- **notes for off-topic questions**: cases with `"expect": []` that still got notes injected;
- **plain search**: the same questions as one keyword search of the whole prompt, no ladder. If
  recall does not beat it, something is wrong before anything else is.

### Where the labels come from

A score is only as honest as its labels, and recall must never grade itself.

- **The fixture** (`--fixture`) is hand-written: 20 questions over the ten notes of the public
  demo vault, two per note (one in the asker's words, one worded unlike the note), plus four
  off-topic questions. It runs anywhere and is the regression check for changes to recall itself.
  Index the demo vault first:
  `qmd --index cn-eval-demo collection add docs/examples/recall-demo/vault --name notes --mask '**/*.md'`
  then `claude-note eval run --fixture --index cn-eval-demo`.
- **Probes** (`eval probes`) are written from your own notes. A sample of typed notes, stratified
  by type, goes to the Claude CLI, which writes the question a teammate would ask *before* knowing
  the note existed, in their own words. The label is the note the question was written from, so
  it is known by construction. Each probe records how much it borrows from the note's title
  (`overlap`); the dashboard reports the low-overlap "hard" cases separately, because a question
  that repeats the title is easy for keyword search and flatters it.

Probes are frozen once written. Rebuilding them (`--yes`) starts a new, incomparable series.

### Dev and test

Cases are split into **dev** and **test** by the expected note, never by row, so two questions
about one note always land on the same side. Tune settings looking at dev; report test. A
setting chosen because it looked good on test has not been tested.

### Comparing settings

Every run records its settings. To try one, run with it and a label, then compare on the dashboard:

```bash
claude-note eval run --label "inject 6" --inject 6
claude-note eval run --label "single-term fallback" --single-term-fallback
```

## Live: what the real hook did

The recall hook logs each prompt it searched (`~/.local/share/claude-note/recall.jsonl`): status,
latency, the query's content words (never the prompt itself), and the injected file paths, with the
session's transcript path. `eval live` joins that to the transcript: did the agent read an injected
note (a `Read` of it, or a shell command naming it) before the next prompt you typed? That is the
one usefulness signal that needs no judge. It reports:

- miss rate and latency of real prompts;
- **opened rate**: turns where the agent opened at least one injected note;
- **keeps finding nothing**: the queries that miss most often; write a note for them;
- **injected, never opened**: notes shown repeatedly and never read; they are matching the wrong
  questions (retitle, tag, or merge them).

## Answers: does recall change what the agent says?

`eval answers -n 10` takes test-split probes about team-specific knowledge (decisions, gotchas,
feedback), asks each question twice through the Claude CLI (no project hooks, empty directory),
once plain and once with the recall context the hook would inject, and gives a judge the team's
note and both answers in random order, unlabelled. The judge says which answer follows the note.

Two guards, because a model judge is not trusted by default:

- **a control**: the same pair is judged against an unrelated note, where the right answer is
  "neither". If the judge says "neither" less than 80% of the time, the run is marked untrusted
  and the dashboard says so;
- **a different judge** if you have one: `--judge-model`. The same model family grading its own
  answers is a known weakness; the control exists because of it.

## Measured on our own vault

12 October 2026, QMD 1.0, default settings (`inject` 4), collections `notes`, `foresyn`, `agentmem`
(15,052 notes):

| Set | Right note injected | Right note first | Plain search | Found nothing | p95 |
|---|---|---|---|---|---|
| Demo fixture, all 20 answerable | 40% | 40% | 0% | 60% | 1.9 s |
| Our probes, test half (36) | 28% | 22% | 8% | 6% | 11 s |
| Our probes, dev half (44) | 34% | 18% | 7% | 2% | 12 s |

On our vault recall rarely comes back empty; it loses to other notes. Across the 80 probes the right
note was found but cut by `inject` 16 times and lost outright 36 times. Only 1 of the misses was a
pushed copy of the expected note (title matches count as the same note since then), so the strict
score is close to fair; some winning notes are neighbours that partly answer the question, so it is
a floor. Median probe-to-title word overlap was 8%: these are hard, realistic questions.

Answers, 10 test-half probes about decisions, gotchas and feedback: the judge found the answer
followed the team's note 5 times with recall and 3 times without; on the control pairs it said
"neither" 10 times out of 10. Ten questions do not separate the two; it is the series to watch.

`[recall] single_term_fallback` on the demo fixture: right note injected 40% -> 90%, but off-topic
questions that got notes 0% -> 25% and p95 ~2 s -> ~6 s. On our vault it can only affect the few
empty results, so it stays off by default.

### After: QMD 2.8.3, the recall server, hybrid search and the reranker (12 October 2026, later)

Same 80 probes, plus 30 hand-written off-topic questions (`--negatives`). Every setting below was
chosen on the dev half; the test half was looked at once, at the end.

| Test half (36 answerable, 15 off-topic) | Before (QMD 1.0, CLI ladder) | Hybrid, no rerank | Hybrid + rerank (default) |
|---|---|---|---|
| Right note injected | 28% | 58% | **78%** |
| Right note first | 22% | 39% | **56%** |
| MRR | 0.25 | 0.46 | **0.66** |
| Off-topic questions that got notes | not measured | 1 of 15 | 1 of 15 |
| Latency p50 / p95 | not measured / 11 s | 0.36 s / 1.3 s | 3.3 s / 5.3 s |

A repeat run of the default setting gave 81% right note injected and 58% first on test: expect a few
points of run-to-run variation (reranker, timing).

What changed, and how each choice was made:

- **Clean index.** QMD 2.8.3 would not adopt 1.0's vectors (no fingerprint), and 87% of the 370k
  were stale copies of edited notes. `qmd embed --force` rebuilt 61,896 current vectors in about
  1.5 hours. (It needs room: clearing vectors grows the WAL first; check free space.)
- **The recall server** (`recall_server.mjs`, QMD's SDK, models warm, 127.0.0.1:8182): the whole
  keyword ladder in ~46 ms and a search by meaning in ~290 ms, against ~1.5 s per `qmd search`
  process. `claude-note setup` installs it; recall falls back to the CLI ladder when it is down.
- **Fusion.** Reciprocal-rank fusion of the keyword rungs and the meaning list. Vector weight swept
  on dev over 0.5-6: 3 was best (73% right note on dev; keyword ladder alone 45%, meaning alone 61%).
- **Gate.** On dev, the best meaning score of every answerable question was >= 0.455 and of every
  off-topic question <= 0.36. Any gate in 0.38-0.45 gave 0% junk with no answerable question lost;
  0.40 (the middle) is the default. On test it let 1 of 15 off-topic questions through.
- **Reranker** (QMD's Qwen3-Reranker-0.6B, top 12, first 2000 characters of each note; 1200 and title-first since 2.0.1): on dev,
  right note first 32% -> 59%. Costs ~2.7 s; `rerank = false` keeps the 0.36 s path.

### Continuous: `claude-note eval nightly` (13 October 2026)

`claude-note setup` now schedules `eval nightly` (04:30, capped at an hour). Each night it scores the
frozen probes plus the 30 off-topic questions with whatever settings recall uses that day, reads the
real-use log, rebuilds the dashboard and writes `evals/nightly.json`. It compares the run with the
median of the last five runs on the same set and settings, and exits 3 (and `doctor` turns red) when
"right note injected" on the test half falls 8 points or more, or off-topic injections rise 10. A
wobble of a question or two does not trip it. Wire the exit code or `nightly.json` to your alerts.

The first night found a real bug: with the default config (`collections = []`), the server path kept
only the memory collection, so recall missed every vault note. Right note injected fell to 7%. The
earlier 78% runs had passed explicit collections, so they never hit it. Fixed in 2.0.1.

### Tuning, the same night

15 settings on the dev half (44 questions), one at a time against the warm server:

| Dev half | Right note in top 4 | First | MRR | p50 / p95 |
|---|---|---|---|---|
| 2.0 defaults (rerank top 12, raw file, 2000 chars, vector weight 3) | 75% | 30% | 0.49 | 3.5 s / 5.4 s |
| No reranker | 70% | 27% | 0.43 | 0.5 s / 1.8 s |
| Reranker reads title + body without frontmatter (`rerank_clean`), 1200 chars | 82% | 32% | 0.53 | 2.6 s / 4.7 s |
| ...and vector weight 2 (chosen) | 82% | 36% | 0.55 | 2.3 s / 5.5 s |

Checked once on the test half (36 questions): the right note in the top four for 29 of 36 with both
the old and the new defaults (no question gained or lost); first place 16 versus 14, within noise at
this size; median 3.7 s versus 2.3 s, slowest one in twenty 5.8 s versus 3.1 s. The dev gain in
accuracy did not replicate; the speed did. The new defaults are those settings: same accuracy, about
40% faster.

Where first place goes is also clearer now: Claude Code memory files take it for about half the dev
questions, often a real memory of the same incident. Probes are written only from vault notes, so a
memory file can never count as the right answer, and "first" is a floor for that reason.

## What we tried and dropped

- **QMD's own HTTP daemon (`qmd mcp --http`, `POST /query` with lex + vec searches).** 4-5 s per
  query warm on our index, with worse top results than the same searches through the SDK; the time
  is in its full search pipeline, not the vector lookup (~180 ms through the SDK).
- **Search by meaning on a miss.** On QMD 1.0, `qmd vsearch` expands the query with a 1.3 GB
  model before searching. It took 27 to 67 seconds on a 15,000-note index, against an 8 second
  budget for the whole hook, and loading that model inside an agent turn has taken a server down
  before. Not usable in a prompt hook; revisit when vector search can run without expansion.
