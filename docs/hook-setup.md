# Hook setup and verification

The bundled hooks capture events for later processing. They do not search knowledge or inject recall into an assistant's prompt.

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

## Recall before answering

A separate Claude Code `UserPromptSubmit` integration can return source-linked leads in `hookSpecificOutput.additionalContext`. That is a custom host integration, not the `claude-note enqueue` hook installed here. It should tell the assistant to open full sources, verify current applicability, search further after a miss, and record durable corrections. Context injection does not enforce those actions or prove that recall runs for every prompt.

For Codex, the bundled hooks enqueue capture events. Use explicit search and source-reading instructions for recall unless a separate recall integration has been installed and exercised in that host. Verify capture and recall independently.

The [demo and operating rule](knowledge-loop-demo.md) show this separation. Its custom-Claude recall receipt uses mocked retrieval and synthetic input; it does not establish real assistant-event delivery, retrieval quality, or latency.
