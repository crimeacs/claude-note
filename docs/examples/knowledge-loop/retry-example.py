#!/usr/bin/env python3
"""Controlled synthetic replay: a committed write can outlive its response.

Run: python3 docs/examples/knowledge-loop/retry-example.py
Uses only an in-memory SQLite database and Python's standard library.
"""

import json
import sqlite3


class ResponseLost(Exception):
    """The server committed the write, but the client received no result."""


def submit_job(db, operation_key, lose_response=False):
    # The server supports idempotency; the caller must reuse the operation key.
    with db:
        db.execute(
            "INSERT INTO jobs (operation_key, payload) VALUES (?, ?) "
            "ON CONFLICT(operation_key) DO NOTHING",
            (operation_key, "synthetic-job"),
        )
    job_id = db.execute(
        "SELECT id FROM jobs WHERE operation_key = ?", (operation_key,)
    ).fetchone()[0]
    # Failure occurs AFTER the transaction above has committed.
    if lose_response:
        raise ResponseLost("Insert committed; response lost.")
    return job_id


def exercise(reuse_key):
    db = sqlite3.connect(":memory:")
    try:
        db.execute(
            "CREATE TABLE jobs (id INTEGER PRIMARY KEY, "
            "operation_key TEXT NOT NULL UNIQUE, payload TEXT NOT NULL)"
        )
        first_key = "demo-operation-1"
        try:
            submit_job(db, first_key, lose_response=True)
        except ResponseLost:
            pass
        else:
            raise AssertionError("The controlled response loss did not occur.")

        rows_before_retry = db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        assert rows_before_retry == 1, "The initial insert must already be committed."
        original_id = db.execute("SELECT id FROM jobs").fetchone()[0]
        retry_key = first_key if reuse_key else "demo-operation-2"
        returned_id = submit_job(db, retry_key)
        rows_after_retry = db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        assert rows_after_retry == (1 if reuse_key else 2)
        assert (returned_id == original_id) == reuse_key
        return {
            "key_strategy": "same key per operation" if reuse_key else "fresh key per attempt",
            "rows_after_lost_response": rows_before_retry,
            "rows_after_retry": rows_after_retry,
            "retry_returned_original": returned_id == original_id,
        }
    finally:
        db.close()


def main():
    broken = exercise(reuse_key=False)
    fixed = exercise(reuse_key=True)
    result = {
        "synthetic": True,
        "scenario": "Lost response after committed SQLite insert",
        "server_behavior": "UNIQUE(operation_key) + upsert ON CONFLICT DO NOTHING",
        "broken": broken,
        "fixed": fixed,
        "assertions": "passed",
        "clips": [
            "First insert committed; response lost.",
            f"Fresh key retry: {broken['rows_after_retry']} rows.",
            f"Same operation key + UNIQUE + upsert: {fixed['rows_after_retry']} row.",
            "Retry returned the existing job.",
        ],
    }
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
