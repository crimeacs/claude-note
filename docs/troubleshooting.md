# Troubleshooting

Start with `claude-note status --json`, then inspect the failing stage rather than assuming one healthy process means the whole loop works.

| Symptom | Check |
| --- | --- |
| Configuration missing | Set `vault_root` in the XDG config or `CLAUDE_NOTE_VAULT`. JSON health still works before setup. |
| No queued hook events | Installed executable path, host hook support and trust, hook JSON, and a controlled event. Recovery imports are separate. |
| Import sweep stale or failed | Worker service, `import-sweep.ok`, and last sweep errors. Run `claude-note import` to inspect supported sources. |
| Session notes but no durable knowledge | Synthesis mode, Claude authentication, worker error logs, timeout and model identifier. Capture success is not synthesis success. |
| QMD results missing | Explicit collection path, `qmd collection list`, `qmd status`, and a shorter keyword query. Rebuild local index with `claude-note index`. |
| Vector search misses current text | `qmd update` refreshes text; run `qmd embed` for changed/missing vectors. |
| Repeated synthesis timeout | Inspect context and model latency; `[synthesis].timeout` is honored. Do not hide repeated errors by only increasing it. |
| Note operation rejected | Markdown extension, vault containment, unsafe symlink/traversal, and operation schema. Inspect returned routing errors. |
| Similar-topic correction disappeared | Current exact extraction dedup preserves different content; update an older install and inspect historical inbox entries. |
| Shared push skips a note | Curated type, session/private/share exclusions, placeholder filter, redacted content receipt, and deferred legacy receipt count. |
| New shared destination receives nothing | Current receipts include destination and vault. Unknown legacy receipts require explicit review and `--resend-legacy`. |
| Update wants to replace the source | Reinstall from the recorded checkout or bundled app; preserve source-managed install provenance. |

Worker logs live under `{vault}/.claude-note/logs/`; macOS operational logs/heartbeats under `~/Library/Logs/claude-note/`. `installed-from.json` records the source; a release version alone does not establish which fork or commit is installed.

```bash
claude-note status --json
claude-note import
claude-note index
qmd search "vault graph" -c notes -n 5 --json
claude-note push --dry-run
claude-note clean --all           # preview, no mutation
```

`drain` writes pending work and has no dry-run flag. `clean` previews unless `--execute` is passed. Before a destructive manual reset, preserve queues, notes, receipts, and source evidence. Deleting state can create replay and duplicate-sharing work.

Secret-pattern redaction does not classify confidential prose. Hook configuration does not prove exercised capture. Local push success does not prove external shared merge. These distinctions are operational checks, not interchangeable success indicators.
