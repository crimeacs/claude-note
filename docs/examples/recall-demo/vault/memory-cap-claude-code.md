---
title: "Claude Code loads only the first 200 lines of MEMORY.md"
type: gotcha
created: 2026-09-20
assistant: claude-code
tags: [claude-code, memory]
---
# Claude Code loads only the first 200 lines of MEMORY.md

Past 200 lines or 25 KB the rest is dropped silently, newest entries first. Keep MEMORY.md an
index of topics and put the detail in topic files.
