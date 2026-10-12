# Hook setup and verification

Claude Note installs two kinds of hooks. **Capture** hooks (`claude-note enqueue`) queue events for the worker. **Recall** hooks (`claude-note hook recall`, `claude-note hook sentinel`) search your notes and hand the agent prior knowledge before it acts. `claude-note setup` installs both; the commands below install capture only.

Use the idempotent installers instead of replacing the host's settings:

```bash
claude-note install-claude-hooks
claude-note install-codex-hooks
```

Existing unrelated settings remain, and changed files receive a timestamped backup. Claude Code settings default to `~/.claude/settings.json`; Codex hooks default to `~/.codex/hooks.json`. Use `--settings-file` or `--hooks-file` for another location.

| Host | Registered events | Timeout units |
| --- | --- | --- |
| Claude Code | `PostToolUse`, `UserPromptSubmit`, `Stop` | Seconds, 10 each. |
| Codex | `UserPromptSubmit`, `Stop`, `SessionEnd` | Seconds, 5 / 5 / 3. |

Each command invokes the installed `claude-note enqueue` executable. Hook support and trust depend on the installed host. A `Stop` event means the assistant stopped responding; it does not necessarily mean the conversation ended.

## Verify delivery

1. Inspect `claude-note status --json` for hook configuration and worker health.
2. Trust the hook file in the host if prompted.
3. Perform a controlled assistant turn, then inspect `{vault}/.claude-note/queue/` for that session's event.
4. Inspect its session record and synthesis output, and check worker logs for errors.

Do not claim automatic capture from configuration alone. A later import sweep can recover a session even when no hook fired; distinguish recovery from hook delivery. The sweep also handles supported recent Claude Code and Codex history.

The enqueue interface reads host JSON on stdin; it has no positional `test` or `event_type` arguments. Test with a controlled host event or synthetic stdin in a disposable vault. Synthesis disables hooks for its own Claude subprocess to reduce recursive capture.

Cursor uses a read-only local database importer. Claude desktop/web and ChatGPT web use explicit conversation exports. Those are separate adapters, not hooks into their accounts.

## Recall and sentinel hooks

`claude-note setup` adds these, idempotently and with a backup:

| Host | Event | Matcher | Command | Timeout |
| --- | --- | --- | --- | --- |
| Claude Code | `UserPromptSubmit` | | `claude-note hook recall` | 10 s |
| Claude Code | `PostToolUse`, `PostToolUseFailure` | `Bash` | `claude-note hook sentinel` | 10 s |
| Codex | `UserPromptSubmit` | | `claude-note hook recall` | 10 s |

Both print `hookSpecificOutput.additionalContext`, which Claude Code 2.1.296 and Codex 0.162.1 add
to the model's context (verified by recording what each model then said). Both fail open: a
missing QMD, a locked index or a timeout returns normally with nothing worse than a miss notice.
They are answered before the rest of the CLI is imported, so they work, and stay fast, even on a
machine with no vault configured.

Verify with `claude-note doctor`: it runs these exact commands against a canary note in a
throwaway index. Context injection is advice, not enforcement; the recall rule that `setup` adds to
`CLAUDE.md` / `AGENTS.md` tells the agent to open the note, check it still applies, and search
again after a miss. See [recall](recall.md).
