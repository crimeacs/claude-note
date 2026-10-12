---
title: "NEXT_PUBLIC_ variables are baked in at build time"
type: gotcha
created: 2026-05-31
assistant: claude-code
tags: [nextjs, deploy, env]
---
# NEXT_PUBLIC_ variables are baked in at build time

Editing the env file on the server and restarting changes nothing the browser sees. Change the
value where the build runs, rebuild and redeploy, then check `/api/health` echoes it.
