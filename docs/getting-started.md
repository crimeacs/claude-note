# Getting started

Use a permanent checkout so you can identify and reproduce the running version.

```bash
git clone https://github.com/crimeacs/claude-note.git ~/src/claude-note
cd ~/src/claude-note
scripts/install-from-checkout.sh --non-interactive \
  --vault ~/Documents/claude-notes --author you@example.com --claude-hooks
```

Python 3.11+ and uv are required. `--install-uv` explicitly permits fetching uv. Claude CLI must be installed and authenticated for synthesis; the installer chooses `log` if it is absent. Existing authored configuration is preserved. The checkout installer creates a macOS LaunchAgent; Linux setup is in [service setup](service-setup.md). `./install.sh` remains the interactive installation path.

1. Inspect `~/.config/claude-note/config.toml`. Choose `inbox` initially to review extractions. The built-in mode is `route`.
2. Run `claude-note status --json`. It reports configuration and operational problems even before setup; exit 1 means inspect its `problems` list.
3. Start a short assistant session that establishes one useful synthetic lesson. End the turn and wait for processing. Check the event queue, session note and curated output separately.
4. Inspect `{vault}/.claude-note/logs/worker-*.log`. A session note proves capture, not necessarily successful synthesis.
5. Enable [QMD](qmd-integration.md) with an explicit collection for this vault if you want source context.

For Codex hosts that support hooks, run `claude-note install-codex-hooks` and trust the hook file in the host. Presence of `~/.codex/hooks.json` does not prove delivery. The recovery sweep can pick up missed rollouts. Cursor import and supported exported conversations use the same worker pipeline; see [commands](commands.md).

```bash
claude-note import
claude-note ingest paper.pdf --dry-run
claude-note clean --all           # preview only
```

Keep `.claude-note/` out of version control. Keep confidential notes local or explicitly excluded from sharing. Optional shared push is a separate configured action; see the [README](../README.md#optional-shared-vault-push).

Update from the same checkout:

```bash
cd ~/src/claude-note
git pull --ff-only
scripts/install-from-checkout.sh --claude-hooks --codex-hooks
```

Do not use a temporary checkout or an unrelated fork to update an existing install. Source-managed `claude-note update` explains the recorded source instead of replacing it.

## Existing vaults

The installer preserves existing settings, so review these changes explicitly:

- Set `qmd.collection` to the registered collection for this vault. Retrieval now defaults to keyword search; choose `search_mode = "vector"` and prepare embeddings if you want vector search. Semantic ingestion candidates require `ingest_dedup_enabled = true`.
- New session records live in `sessions/`; existing root session records remain usable. A changed imported conversation receives a new source-aware identity, preserving the older capture as historical evidence.
- Push receipts now bind to the destination and local vault. Unbound legacy receipts are deferred. Inspect `push --dry-run` before deliberately using `push --resend-legacy` for the configured target.

No vault move or mass re-upload is required to install the update. See [development notes](developments.md) for the implementation map.
