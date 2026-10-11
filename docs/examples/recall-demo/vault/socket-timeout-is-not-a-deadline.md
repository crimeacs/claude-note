---
title: "A socket timeout is not a deadline"
type: gotcha
created: 2026-09-20
assistant: claude-code
tags: [reliability, python, http]
---
# A socket timeout is not a deadline

`urlopen(timeout=2)` bounds each read, not the whole call. A server that dribbles one byte per
second kept a "2 second" request alive for 20 seconds. For anything unattended, run the call on a
daemon thread and `join(deadline)`, or wrap the process in `timeout -k`.
