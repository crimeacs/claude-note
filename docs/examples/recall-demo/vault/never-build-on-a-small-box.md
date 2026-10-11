---
title: "Never run the production build on the 4 GB box"
type: decision
created: 2026-05-31
assistant: codex
tags: [deploy, nextjs, infra]
---
# Never run the production build on the 4 GB box

`npm run build` on the t3.medium runs out of memory and takes the web app down with it.
Build on a laptop and ship the standalone artifact; the box only swaps a symlink and restarts.
