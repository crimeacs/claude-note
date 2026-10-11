---
title: "API credits exhausted: reroute bulk calls, never retry"
type: gotcha
created: 2026-08-06
assistant: claude-code
tags: [llm, billing]
---
# API credits exhausted: reroute bulk calls, never retry

"credit balance is too low" fails every call after it. Stop the batch, run one canary on the other
funded backend, then continue there. Retrying the identical call only burns time.
