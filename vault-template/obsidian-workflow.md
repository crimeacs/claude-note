---
type: meta
tags:
  - meta
  - knowledge-management
aliases:
  - vault workflow
  - how to use this vault
---

# Obsidian Workflow

How this vault is organised and how to get something out of the graph.

## The `type` property

Every note carries exactly one `type:` in its frontmatter. It answers *what kind of
thing is this*, not *what is it about* — topic stays in `tags:`. One value per note,
so the graph can colour each node unambiguously.

| `type` | What belongs here | Graph colour |
|---|---|---|
| `pattern` | Reusable approaches, methodologies, principles | teal |
| `gotcha` | Failures, bugs, incidents — the expensive lessons | red |
| `decision` | Architecture and design choices, with rationale | purple |
| `reference` | Concepts, APIs, how-tos, general knowledge | blue |
| `project` | Project trackers and scope notes | orange |
| `log` | Calls, status snapshots, dated assessments | grey |
| `literature` | Ingested external papers (`lit-*`) | magenta |
| `source` | Ingested internal source docs (`int-*`, `reference_*`) | gold |
| `feedback` | How I want to be worked with | green |
| `meta` | Vault infrastructure and maps | white |
| `session` | Auto-generated Claude Code transcripts | dark grey |

Ranking rule when a note could be two things: **reusable knowledge beats context**.
A note tagged both `pattern` and `some-project` is a `pattern` that happened to come
out of that project. `gotcha` outranks everything except the ingest prefixes, because
a failure you already paid for is the most costly thing to re-learn.

Choose `type` explicitly when writing knowledge. claude-note logs a warning and
uses a compatibility fallback for old extraction output that omits it. A fallback
to `reference` cannot distinguish an incident from a reusable pattern.

A type describes the note's role. It does not grant permission to publish the
note; the optional push has its own narrower eligibility and privacy rules.

## Graph view

`.obsidian/graph.json` ships with one colour group per type and a default filter of
`-[type:session] -file:claude-note-inbox -path:templates/`. That is deliberate:

- **Session transcripts** are auto-generated and mostly disconnected. Once there are
  a few hundred, they bury the notes that carry real knowledge.
- **`claude-note-inbox`** accumulates a link to every concept it has ever staged.
  Left in the graph it becomes a single node wired to most of the vault.

Clear the search box in graph settings to see everything.

Also on by default: unresolved links hidden, orphans hidden, tags hidden (a mature
vault has more distinct tags than notes, which swamps the node count).

### Reading it

- Big nodes are hubs — many links in or out. They are the notes worth keeping good.
- Red clusters are where things went wrong repeatedly. Worth a consolidating note.
- An isolated colour island means that topic never got linked to the rest.

## Conventions

- Knowledge notes can live in the root or topic folders. Raw capture lives in
  `sessions/`, ingested documents in `internal/` or `literature/`, and reusable
  starter notes in `templates/`. Legacy session notes in the root stay in place.
- Frontmatter order is `type`, `tags`, `aliases`, `created`.
- Add `aliases:` for alternative names — it makes autocomplete find the note.
- Add links when they express a real relationship. Verify the target exists;
  avoid fabricated links and mandatory link counts that create graph noise.
- `alwaysUpdateLinks` is on, so renaming a note rewrites inbound links automatically.

## Automation

[[CLAUDE]] describes the claude-note integration. Session notes arrive under
`sessions/` as `claude-session-YYYY-MM-DD-XXXXXXXX.md` with `type: session`.
Synthesized knowledge lands in `claude-note-inbox`; route mode also updates
typed knowledge notes, and questions accumulate in [[open-questions]].

The inbox is a staging area, not a destination. Promote anything worth keeping into
its own typed note and link it.

## Maintenance

Treat raw capture, curated knowledge and retrieval as separate layers. Session
logs are evidence; the wiki records reusable understanding; QMD indexes files
for retrieval and is rebuilt from those files. Search results are leads: read
the source before relying on a claim. Keep a correction alongside its evidence
and explain when it supersedes an older claim.

When a topic accumulates several dated investigations, add a hub note linking
the actual sequence. A tag groups topics; a link describes a relationship.
Avoid merging unrelated notes merely because their titles or search ranks are
similar. Check untyped notes, duplicate basenames, missing link targets and
isolated knowledge periodically; fix these with judgment.

## Related

- [[open-questions]] — unresolved threads
- [[CLAUDE]] — instructions for Claude Code working in this vault
