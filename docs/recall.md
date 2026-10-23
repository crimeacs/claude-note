# Recall, sentinel and remember

Capture writes knowledge down. This is the other half of the loop: putting it back in front of an
agent at the moment it matters, and writing new lessons back while they are fresh.

| Piece | Runs | Gives the agent |
|---|---|---|
| `claude-note hook recall` | Every prompt (Claude Code and Codex `UserPromptSubmit`) | Up to `inject` notes about the same subject, with file paths, or an explicit "found nothing" |
| `claude-note hook sentinel` | After a failed shell command (Claude Code `PostToolUse` / `PostToolUseFailure`, matcher `Bash`) | Notes about that error, and "do not retry" advice for deterministic failures |
| `claude-note remember` | When you or the agent decide a lesson is durable | A typed note, indexed immediately |
| `claude-note recall` | By hand, or by the agent when the hook missed | The same search, in the terminal or as JSON |
| `claude-note doctor` | After setup and after any change | Proof that the hooks work, not only that they are configured |

`claude-note setup` installs all of it; see the [README](../README.md#install).

## What the agent sees

On a hit, the hook returns `hookSpecificOutput.additionalContext`, which Claude Code and Codex add
to the model's context before it answers:

```text
claude-note recall: possibly relevant prior knowledge (keyword matches, NOT verified answers).
Open a note before relying on it (read the file shown), check that it still applies, then act.
If none fit, this lookup missed; it does not mean nothing is known. Search yourself with
other words (`claude-note recall "<content words>"` or `qmd vsearch "<question>"`).

- Google OAuth: a new scope needs a fresh consent  [/Users/you/notes/google-oauth-new-scope-needs-reconsent.md]
    Adding a scope in the Cloud Console does not upgrade tokens you already hold. Gmail and Calendar
    calls keep failing with `403 insufficientPermissions` and re-running the login silently returns
    the old cached token.

If this task teaches something durable, write it back: `claude-note remember --type gotcha "<what broke, why, the fix>"`.
```

The path is the note's real file, so the agent can open it with its ordinary file-reading tool and
no extra permission. In the recorded demo, Claude Code read the note before answering and said
where its answer came from.

On a miss:

```text
claude-note recall ran and found nothing (no keyword match on any rung of the query).
Query tried: 'deploy failed gate source unchanged', narrowed to 'deploy unchanged'

This is NOT evidence that nothing is known: keyword search is conjunctive, so one
rare or mis-guessed word zeroes a query. ...
```

## How a prompt becomes a search

QMD's keyword search (BM25) is fast enough to run on every prompt, about 0.3 s per query, but
behaves conjunctively: one word that no note contains returns zero results. Each rule below fixed
a measured miss on a 15,000-note index.

1. **Content words only.** Stopwords, imperative verbs ("summarise", "evaluate", "build"), paths,
   URLs and dates are dropped. A path in a prompt says *where* to look, not *what*; as one long
   token it matched nothing. Dates made 7 of 8 title-lookups miss.
2. **Compounds are split.** `task-backed`, `source_unchanged` and `config.toml` are each one
   literal token to BM25, and each matched nothing. Recall searches their parts.
3. **A ladder of queries.** The full query, then the 5, 3 and 2 most discriminating words, in the
   prompt's own order. A capitalized word (except the first) and a word with digits rank as
   specific; then longer words.
4. **Every rung is pooled.** Taking the first rung that answered let a narrow query with a worse
   note beat a wide one with the right note. Notes that several rungs agree on rank first.
5. **Noise is skipped.** Session transcripts and the inbox dominate the index by count and their
   snippets are frontmatter. When a match landed in frontmatter, the note's first prose is shown.
6. **The newest update wins the snippet.** Notes that grow by appended managed blocks keep their
   current truth at the bottom, while BM25 serves the snippet from wherever it matched (usually the
   oldest text). Recall shows the newest dated block and says how much history is behind it.
7. **Agent memories get reserved slots.** Short, hand-written memories (Claude Code's
   `~/.claude/projects/*/memory/*.md`) rank low in BM25 against long notes, yet are the
   highest-signal entries. `memory_collections` are searched separately and placed first.
8. **Misses are announced.** A hook that prints nothing on a miss is indistinguishable from one that
   never ran; agents read the silence as "nothing is known" and stop looking.
9. **It fails open, on a deadline.** Each search has a wall-clock `timeout`, the whole recall a
   `budget`, and every failure path returns normally. Harness events (task notifications), slash
   commands and subject-free follow-ups such as "fix it" are not searched. Specific short
   subjects remain searchable. Search leaves time for source verification, and automatic mode
   reserves a CLI fallback slice before calling the server. Incomplete coverage is reported as
   partial or unavailable instead of an ordinary no-match result.

Optional: `rerank_command` receives `{"request": …, "candidates": [{"id", "title", "excerpt", "uri"}]}`
on stdin and returns `{"keep": ["c3", "c0"]}`, best first. An empty list means "nothing relevant"
(recall reports a miss); any failure or timeout keeps the pool order. Fit any threshold your judge
uses on labelled examples, not by eye.

## Server failure diagnostics

JSON results and the local recall log retain `attempted_engines` and
`server_calls`. Each call records its route, random client request ID, observed
HTTP status, elapsed time and a bounded outcome such as `socket_timeout`,
`client_deadline`, `client_read_capacity`, `http_error` or `response`.
Transport diagnostics contain no prompt, URL, response body or exception message.
The existing local log still contains query terms and note URIs.

After a search attempt, `engine` identifies its last attempted route: a strict server failure is
labelled `server`; automatic fallback records both attempts and switches to
`cli` when a CLI search is scheduled. An empty `attempted_engines` means no
search was attempted; `engine` then retains its legacy `cli` default.
The client ID correlates the HTTP request
and its receipt; the runtime proxy uses a separate internal job ID. These fields
do not expose queue wait versus native execution time or prove a timeout cause.
A search's adapter subdeadline can expire before the whole eight-second
recall budget. Automatic mode shortens the server slice to leave time for a CLI search and
source verification; `timeout` remains an upper bound. Read-capacity exhaustion is distinguished in the call outcome;
the legacy overall `deadline_exhausted` flag also covers that bounded-read failure.

The daemon warms existing cached embedding and rerank models during bounded startup, before
admitting hook requests, and retains them across idle periods. The SDK store configures its
five-minute idle cleanup to release both inference contexts and model objects, making a later
prompt recreate native state inside its search deadline.
Startup never downloads a model or builds native dependencies. Warm-up metadata records model
loading separately from retrieval readiness: a successful real search and exact-passage rerank
are still needed to establish those capabilities. Model work that exceeds its deadline retains
the existing whole-process retirement behavior.

Startup also scans vector pages once before accepting requests. On a large, cold index this
file I/O can exceed a hook's entire budget even when the models are already warm. The service
has a separate 45-second startup watchdog; each hook keeps its eight-second total deadline.
Its SQLite connection uses a bounded 128-MiB page cache and up to about 2 GiB of file mapping.
The pinned QMD vector adapter hydrates matched `(hash, seq)` keys through existing indexes,
preserving collection filters, active-document joins, source bodies and scores. These settings
and query changes do not add indexes, rewrite the database or change other QMD connections.

On macOS the recall LaunchAgent uses `ProcessType=Interactive` because prompt responsiveness
depends on it. The previous default process class applies background CPU and I/O throttles.
Upgrades migrate the installer's former `Nice=5` default to normal priority while preserving
explicit custom priorities and process classes.
The runtime worker also limits QMD to one embedding context and one rerank context, matching
its serialized request queue and avoiding the SDK's larger batch-processing pools. Other QMD
processes keep their own settings.

Health metadata and search operations now share one lexical-worker lifetime.
An already pending health refresh settles before a search starts; it cannot
close the worker while that later search is using it. This preserves deadlines
and may consume part of a queued search's budget. It is separate from the index
writer's update/embedding refresh.

## Reranking requires a configured-model observation

The server accepts scores only when the SDK reports the configured rerank model
and returns unique finite scores for the requested passage identities. The
client additionally requires `rerank_status=scored`, `coverage=exact_passage`,
both current readiness flags and complete, exact score/identity coverage. If the
model differs from the search manifest, it preserves the original fusion order.

QMD can return `model=fallback` and constant finite scores without invoking its
ranker. Those results have empty accepted score maps, `sdk_fallback` status and
`sdk_fallback_no_rerank` coverage. Missing or conflicting model identities,
invalid scores and partial results also keep fusion order and `reranked=false`.
Older servers lacking this positive execution contract still deliver retrieval
results; their reranking is labelled `unverified_rerank_execution`. A legitimate
configured-model score of `0.5` is accepted; score values do not identify fallback.

Each unsuccessful SDK scoring result clears current rerank readiness. A later
complete, validated result can restore readiness, while historical fallback
degradation remains visible. Readiness means a model-bound SDK scoring response
was observed. It does not independently prove native hardware execution,
calibrated relevance, or task benefit. Passage identity binds the text sent over
the wire; SDK-internal token truncation is not traced by this contract.

The result's `manifest` remains the search-stage snapshot. Its `reranked` and
`coverage.rerank` fields describe this query's later scoring result. The doctor
checks that scoring result separately from a fresh global capability read;
another request's readiness cannot make this query's fallback pass.

## The sentinel

The sentinel reads the **output** of a failed command, never the command text. An earlier version
matched error words anywhere in the hook payload and fired whenever an agent ran `grep` or `cat`
on a file that merely mentioned `401` or `invalid_grant`. A zero exit with error-looking stdout
(a file being read) does not count either; error-looking stderr does.

From the output it takes the most specific error line, strips URLs, module paths, exception class
names and exit-code boilerplate, then searches twice over:

- the remaining words, as a ladder like the prompt hook;
- each error *identifier* on its own (`insufficientPermissions`, `invalid_grant`, `ENOSPC`,
  `ERR_SSL_PROTOCOL_ERROR`), because they are specific alone and keyword search has no OR.

Notes typed `gotcha`, `pattern`, `decision`, `reference` or `feedback` are preferred. Built-in rules
flag failures that never fix themselves on retry: expired grants, missing OAuth scopes, 401s,
quota and rate limits, a full disk, and macOS privacy denials. Add your own:

```toml
[[sentinel.rules]]
match = "ECONNREFUSED 127.0.0.1:5432"
advice = "Local Postgres is not running: `brew services start postgresql@16`."
```

Each distinct error is reported once per session.

## Remember

```bash
claude-note remember --type gotcha --title "A full disk silently stops the index refresh" \
  --tags qmd,disk --why "Recall only finds what the index has seen." \
  "When the disk fills, the hourly qmd update fails every run. Free space, then run claude-note refresh."
```

```text
Created a-full-disk-silently-stops-the-index-refresh.md  (type: gotcha)
Indexed in 0.5s: the next prompt can recall it.
```

- Types: `gotcha`, `pattern`, `decision`, `reference`, `feedback`, `project`.
- Frontmatter records `type`, `created`, `assistant` (detected: `claude-code`, `codex` or
  `human`) and `author`.
- An existing note is never overwritten: the lesson is appended as a dated managed block.
- Writes stay inside the vault and outside hidden folders; creation is exclusive.
- `-` reads the lesson from stdin; `--no-index` skips the reindex.

The worker still extracts lessons from finished sessions. `remember` is for the lesson you know
matters now, so the very next prompt (yours, or another agent's) starts from it.

## Doctor

```text
configured
  ✓ qmd installed
  ✓ collections indexed                                  notes, agentmem
  ✓ Claude Code hooks                                    recall, sentinel, capture
  ✓ Codex hooks                                          recall
  ✓ index refreshed recently                             0.0 h ago

exercised
  ✓ recall hook injects a matching note                  1366 ms end to end
  ✓ recall announces a miss instead of staying silent    miss notice present
  ✓ sentinel surfaces the known fix for a failed command fix recalled
  ✓ sentinel ignores error words in successful output    quiet

in use
  ✓ recall on real prompts, 7 days                       4 prompts, 2 with notes (50%), p50 1953 ms
```

*Exercised* plants a canary note in a throwaway QMD index (`--index claude-note-doctor-<pid>`),
runs the installed hook commands against it with real hook payloads, and deletes the index. Your
own index and logs are untouched. *In use* reads `~/.local/share/claude-note/recall.jsonl`, which
the hooks append to (hashed queries, statuses, latencies and note URIs only).

The first run on the machine this was built on found its hourly index refresh failing for eight
hours (the disk had filled). Every note written in that time was unfindable while search still
looked healthy. That is why "index refreshed recently" is a check, and why `claude-note refresh`
writes a heartbeat.

## Refresh

`claude-note refresh` runs `qmd update` (text) and then `qmd embed` (vectors) with a lock, hard
timeouts, a non-interactive environment and a heartbeat at
`~/.local/share/claude-note/refresh.ok`. A bare `qmd update` never embeds: keyword search stays
current while semantic search quietly goes stale. `setup` schedules it hourly on macOS; elsewhere,
run it from cron or a systemd timer.

## Verified hosts

| Host | Version tested | Recall context reaches the model | Sentinel |
|---|---|---|---|
| Claude Code | 2.1.296 | Yes | Yes (`PostToolUse`, `PostToolUseFailure`) |
| Codex CLI | 0.162.1 | Yes, once the hook is trusted | Not installed by `setup` (untested on Codex) |

Older Codex builds may run hooks without passing their output to the model; the recall rule that
`setup` writes to `AGENTS.md` covers that case, since it tells the agent to run `claude-note recall`
itself.
