---
type: gotcha
title: A lost response does not prove the write failed
status: verified-in-controlled-example
synthetic: true
assistant: codex
author: demo-builder
source: retry-example.py
verification: retry-result.json
verified_at: 2026-10-10
tags: [retry, idempotency, correction, synthetic]
---

# A lost response does not prove the write failed

This is a synthetic teaching note, verified with an in-memory SQLite example.
It is not a production incident or a claim about automatic recall delivery.

## Failure

The first request commits a job, then its response is deliberately lost. Retrying
with a fresh operation key creates a second row, even though the server enforces
`UNIQUE(operation_key)` and uses an upsert.

## Correction

Replace the unsafe assumption “no response means no write” with “the outcome is
unknown until reconciled.” Create one operation key before the retry loop and
reuse it for retries of the same operation. In this example, the server's unique
constraint and `ON CONFLICT DO NOTHING` preserve the existing job; the retry
reads and returns that job.

## Verification

Run from the repository root; compare stdout with `retry-result.json`:

```sh
python3 docs/examples/knowledge-loop/retry-example.py
```

The script asserts that the first insert is committed before either retry:

- Fresh key per attempt: **2 stored rows** after retry.
- Same key per operation: **1 stored row** after retry; the original job is returned.

Evidence: [executable example](retry-example.py) and [actual result](retry-result.json).

## Scope

This demonstrates one local retry boundary with an unchanged synthetic payload.
Production handlers must also define operation-key scope, payload mismatch
handling, and retention. A stable key alone does not establish exactly-once
behavior across an entire system.
