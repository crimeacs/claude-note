---
title: "Google OAuth: a new scope needs a fresh consent"
type: gotcha
created: 2026-08-19
assistant: claude-code
tags: [google, oauth, gmail]
---
# Google OAuth: a new scope needs a fresh consent

Adding a scope in the Cloud Console does not upgrade tokens you already hold. Gmail and Calendar
calls keep failing with `403 insufficientPermissions` and re-running the login silently returns the
old cached token.

**Fix:** delete the stored token (every cache: token.json, the DB row, localStorage), then run the
consent flow once with `prompt=consent` and ALL scopes, old and new. Incremental scope grants do not work.

**Verify:** `getTokenInfo(accessToken)` lists the new scope before you retry the real call.
