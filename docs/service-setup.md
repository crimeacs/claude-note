# Worker and service operation

The worker consumes queued events and sweeps supported import sources every 30 minutes. Capture, synthesis, push, and external merge are independent stages.

## macOS

The preferred setup is `scripts/install-from-checkout.sh --non-interactive --vault PATH`; it preserves authored config and creates the worker LaunchAgent only when needed. Existing installs use the same script without `--non-interactive` to reinstall source and restart the loaded worker.

The worker label is `com.claude-note.worker`; the plist is `~/Library/LaunchAgents/com.claude-note.worker.plist`. Use the generated file or [template](../service/com.claude-note.worker.plist.template), with absolute executable paths.

```bash
launchctl print gui/$(id -u)/com.claude-note.worker
claude-note status --json
```

For direct diagnosis, stop the background worker before launching a foreground copy. Avoid two independent writers over the same vault.

```bash
claude-note worker --foreground --verbose
```

## Linux

Install from a permanent checkout with `uv tool install --reinstall .`. Adapt [claude-note.service.template](../service/claude-note.service.template) with absolute executable and vault paths, save as `~/.config/systemd/user/claude-note.service`, then:

```bash
systemctl --user daemon-reload
systemctl --user enable --now claude-note
systemctl --user status claude-note
journalctl --user -u claude-note -f
```

The checkout bootstrap creates macOS LaunchAgents; it is not a Linux systemd installer. JSON health currently includes macOS-oriented agent probes, so inspect systemd separately on Linux.

## Logs and refresh jobs

Worker logs: `{vault}/.claude-note/logs/worker-*.log`. macOS import and push operational files: `~/Library/Logs/claude-note/`. Source receipt: `~/.local/share/claude-note/installed-from.json`.

`claude-note push --install-agent` separately installs the optional daily 03:00 local-time push job. It needs an explicitly configured shared destination. QMD refresh scheduling and shared-vault pull/merge services are external integrations; Claude Note does not install a global memory scheduler.

Use `qmd update` for text and `qmd embed` for vectors if needed. Ensure tools are discoverable by the service, not only an interactive shell. Monitor stale success heartbeats outside the worker; a running PID can coexist with synthesis failures or missing downstream progress.
