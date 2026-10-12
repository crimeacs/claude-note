---
title: "A recall hook must say when it found nothing"
type: decision
created: 2026-09-20
assistant: claude-code
tags: [recall, hooks]
---
# A recall hook must say when it found nothing

A hook that prints nothing on a miss looks exactly like a hook that never ran, and agents read the
silence as "nothing is known". Announce every miss with the query that was tried.
