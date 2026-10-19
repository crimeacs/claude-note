"""An explicit provider cooldown keeps capture running without model retries."""

from datetime import datetime, timezone


def remaining(value, now=None):
    """Seconds until an owner-configured resume time; invalid times fail closed."""
    if value is None or value == "":
        return 0
    if not isinstance(value, (str, datetime)):
        raise ValueError("synthesis.resume_after must be an ISO timestamp with timezone")
    try:
        resume = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
        if resume.tzinfo is None:
            raise ValueError("missing timezone")
    except ValueError as error:
        raise ValueError("synthesis.resume_after must be an ISO timestamp with timezone") from error
    now = now or datetime.now(timezone.utc)
    return max(0, (resume - now).total_seconds())


def paused(value, now=None):
    try:
        return remaining(value, now) > 0
    except ValueError:
        return True
