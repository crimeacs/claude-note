# Synthesis modes

Set `[synthesis].mode` in config or `CLAUDE_NOTE_MODE` in the service environment.

| Mode | Session evidence | Claude synthesis | Curated output |
| --- | --- | --- | --- |
| `log` | Written locally. | Disabled. | No synthesis output. |
| `inbox` | Written locally. | Enabled. | Extraction appended to the review inbox. |
| `route` | Written locally. | Enabled. | Structured note operations plus inbox evidence. |

The code default is `route`; a first-time checkout installer chooses `log` when Claude CLI is absent. Use `inbox` to inspect extraction quality before enabling automatic note operations.

Synthesis receives bounded user prompts, recent assistant conclusions, notable tool inputs and outcomes, errors, file paths, local vault metadata, and optional retrieved local sources. An assistant's statement is an attributed claim. Proposals, failed attempts, accepted choices, and verified results should remain distinguishable.

The subprocess uses the configured Claude CLI model and timeout. Synthesis sends selected context through that CLI's model service; `log` avoids this synthesis call. Optional push and other tools have their own external data paths.

## Structured routing

The current schema is defined in [knowledge_pack.py](../src/claude_note/knowledge_pack.py). Note operations include `create`, `upsert_block`, `append`, and `add_links`. Paths must resolve inside the vault and name Markdown files. Managed updates preserve surrounding human text; retry markers suppress the same applied extraction without suppressing new findings on the same topic.

Set semantic note types explicitly. The writer warns and chooses a fallback for invalid or missing synthesis types; it does not treat topical tags as authoritative meaning. Inbox deduplication compares the extraction rather than dropping every subsequent entry with a similar title.

QMD supplies optional scoped context. It does not independently approve a note, decide an organization audience, or prove two claims are duplicates. Similarity is a candidate cue; current source text and scope still matter.

```bash
claude-note resynth SESSION_PREFIX --mode inbox
claude-note resynth SESSION_PREFIX --mode route --model YOUR_SUPPORTED_MODEL
```

Explicit resynthesis can revise knowledge; inspect the result and errors. A session note's existence alone does not establish successful synthesis. See [troubleshooting](troubleshooting.md).
