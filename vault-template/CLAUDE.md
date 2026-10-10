# Vault instructions

This vault holds technical knowledge and the evidence behind it. Preserve
human-written material and project boundaries when adding or consolidating notes.

## Structure

Knowledge may live in the root or topic folders. `sessions/` holds generated
session logs, `internal/` holds ingested internal documents, `literature/` holds
external research, and `templates/` holds starter notes. Existing session files
in the vault root remain compatible and are updated in place.

Every note has one explicit `type:` describing its role. Topic belongs in
`tags:`, alternative names in `aliases:`, and relationships in `[[wiki-links]]`.

| Type | Contents |
|---|---|
| `pattern` | Reusable approaches and principles |
| `gotcha` | Failures, bugs, incidents and their fixes |
| `decision` | Choices, rationale and consequences |
| `reference` | Concepts, procedures and general technical knowledge |
| `project` | Project scope and trackers |
| `log` | Dated calls, status snapshots and assessments |
| `literature` | Ingested external research |
| `source` | Ingested internal source documents |
| `feedback` | Explicit user corrections and working preferences |
| `meta` | Vault infrastructure, maps, inbox and question index |
| `session` | Generated session capture |

Choose the primary role explicitly: a reusable pattern learned in a project
is a `pattern`, and an incident report is a `gotcha`. Do not use a generic
fallback to hide the expensive lesson. A note's type does not determine whether
it is safe or eligible to publish.

## Writing and retrieval

- Search existing knowledge before deriving a familiar answer. Read the actual
  source: a search snippet or ranking is not verified evidence.
- Start frontmatter with `type`, then `tags`, `aliases`, `created` where useful.
- Preserve source references and distinguish verified facts from hypotheses.
- Keep one durable idea per note. Link to existing, relevant targets; add no
  placeholder or fabricated edges to meet a link count.
- Record a correction with its reason and evidence. Explain when it supersedes
  an earlier decision rather than silently rewriting the historical record.
- Keep unrelated projects and customer material within their proper vault.
- Exclude credentials and private customer contents from shared deliverables.

## Templates

`templates/pattern.md` captures reusable methods, `decision.md` choices with
rationale, `topic.md` references, `project.md` project scope, `debug-session.md`
incidents, and `til.md` short learnings. Use the appropriate explicit type.

## Capture and curation

claude-note generates `type: session` logs under `sessions/`. Synthesis stages
knowledge in [[claude-note-inbox]], and route mode also writes typed notes using
managed blocks that preserve the rest of an existing note. [[open-questions]]
tracks unresolved questions. Promote useful inbox entries into independent
knowledge notes; do not treat the inbox as the final wiki.

Claude Code capture depends on installed hooks. Other assistant capture may use
supported imports or local sweeps; verify the configured path with health/status
before claiming automatic capture is active.

```bash
claude-note status
claude-note health
claude-note drain
claude-note index
```

[[obsidian-workflow]] explains graph filters, note roles and maintenance. QMD is
a derived retrieval index, not the canonical source of the vault's knowledge.
