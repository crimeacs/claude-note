---
title: "Mint the idempotency key once per operation"
type: pattern
created: 2026-06-02
assistant: codex
tags: [payments, retries]
---
# Mint the idempotency key once per operation

A lost response is an unknown outcome, not a failure. Create the key before the retry loop and
reuse it on every attempt, and enforce uniqueness on the server, or a retry charges twice.
