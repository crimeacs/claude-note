# Knowledge-loop demo (1.6, superseded)

The 1.6 film "Remember. Then act." illustrated the loop with a synthetic retry bug and labelled
prompt-time recall as an external integration. 2.0 ships that integration, so the film was
replaced by [the 2.0 demo](demo.md), which is cut from recorded runs of Claude Code and Codex.
The 1.6 film, its captions and its evidence files remain in git history at commit `61d1415`
(`docs/assets/knowledge-loop.mp4`, `docs/examples/knowledge-loop/`).

The operating rule from that page now ships with the package: `claude-note setup` writes it to
`~/.claude/CLAUDE.md` and `~/.codex/AGENTS.md`. See [recall](recall.md).
