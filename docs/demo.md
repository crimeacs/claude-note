# The 2.0 demo: what was filmed, and how to check it

[Watch](assets/claude-note-2.mp4) · [captions](assets/claude-note-2.vtt) · 2 min 33 s, 1080p, narrated.

Every terminal line in the film was copied from a real run of this release in a sandbox: a
throwaway vault, a separate QMD index (`--index cn-demo`), and hosts loaded with only the
claude-note hooks. Nothing on screen was typed by hand except the layout. The raw outputs are in
[`examples/recall-demo/runs/`](examples/recall-demo/runs/) and the vault in
[`examples/recall-demo/vault/`](examples/recall-demo/vault/).

| Scene | What ran | Evidence |
|---|---|---|
| Recall in Claude Code | `claude -p` (Claude Code 2.1.296), `--setting-sources project` with `hook recall` and `hook sentinel` | `recorded-runs.json` → `build-question.claude_code_with_recall` |
| Without recall | The same prompt and model, no hooks | `build-question.claude_code_without_recall` |
| Codex | `codex exec` (Codex CLI 0.162.1) with `hook recall` in `hooks.json` | `build-question.codex_with_recall` |
| README GIF and blog example | The `qmd query` question, `claude -p` with and without `hook recall` (recall server over a sandbox index holding one extra note) | [`runs/qmd-query-question.json`](examples/recall-demo/runs/qmd-query-question.json) |
| How recall finds the note | Ladder counts are real `qmd search` results on the demo index; the miss notice is the hook's real output | `recall.py` |
| Sentinel | `claude -p` running [`calendar_sync.py`](examples/recall-demo/calendar_sync.py) against a token without the Calendar scope; the hook context is verbatim | `sentinel.claude_code`, `sentinel.hook_context` |
| Remember, then recall | `claude-note remember …` then `claude-note recall …` | `runs/remember.txt`, `runs/recall-after-remember.txt` |
| Doctor | `claude-note doctor` in a throwaway HOME after `claude-note setup --codex` | `runs/doctor.txt`, `runs/setup.txt` |
| "It caught our own outage" | `claude-note doctor` and `~/.cache/qmd/refresh.log` on the machine this was built on, 10 October 2026 | Five hourly `FAIL qmd update rc=1` lines, 17:28 to 21:28; recovered at 22:24 after the disk was freed |

Presentation choices, so nothing is overstated:

- Terminals are re-typeset for legibility; Claude Code's own UI does not print the hook name and
  timing line shown above the injected context. Model answers are verbatim.
- Paths are shortened to `~/notes/…`. The Codex scene elides a second file Codex read in the same
  `cat` (a globally installed skill unrelated to the demo).
- The OAuth question also got a good answer *without* recall: it is general knowledge. The film
  uses the build question because only the team's own note can answer it.
- The opening heap-out-of-memory loop is illustrative, not a recording.

Narration: ElevenLabs (voice "George", `eleven_v3`), checked line by line with Whisper. Music:
ElevenLabs music, instrumental. Picture: one HTML page rendered frame by frame in Chrome at 30 fps.

## Reproduce the recorded runs

```bash
export DEMO=$(mktemp -d)
cp -r docs/examples/recall-demo/vault "$DEMO/vault"
mkdir -p "$DEMO/config/claude-note"
cat > "$DEMO/config/claude-note/config.toml" <<EOF
vault_root = "$DEMO/vault"
[recall]
index = "cn-demo"
[sentinel]
enabled = true
EOF
qmd --index cn-demo collection add "$DEMO/vault" --name notes --mask '**/*.md'

export XDG_CONFIG_HOME="$DEMO/config"
claude-note recall "can I run npm run build on the 4 GB production box"
echo '{"prompt":"Can I just run npm run build on the 4 GB production box?"}' | claude-note hook recall
```

To repeat the Claude Code runs, put the three hook entries from [hook setup](hook-setup.md) in a
project's `.claude/settings.json` (with `XDG_CONFIG_HOME` set in the command) and run
`claude -p "<prompt>" --setting-sources project --allowedTools Read`.
