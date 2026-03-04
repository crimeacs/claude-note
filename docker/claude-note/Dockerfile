# ── Build stage ───────────────────────────────────────────────────────────────
# Must use python:3.11 to match distroless runtime (Python 3.11.2, Debian 12)
FROM python:3.11-slim AS builder

WORKDIR /build
COPY pyproject.toml README.md ./
COPY src/ src/
RUN pip install --no-cache-dir --target=/deps ".[api]" \
    && find /deps -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null; \
       find /deps \( -name "*.pyc" -o -name "*.pyo" \) -delete 2>/dev/null; \
       rm -rf /deps/pip /deps/pip-*.dist-info \
              /deps/setuptools /deps/setuptools-*.dist-info; \
       true

# ── Runtime stage (distroless — no shell, no apt, no pip) ────────────────────
FROM gcr.io/distroless/python3-debian12:nonroot

COPY --from=builder /deps /app/deps

ENV PYTHONPATH=/app/deps
ENV CLAUDE_NOTE_VAULT_ROOT=/vault

EXPOSE 8000

ENTRYPOINT ["python3", "-m", "uvicorn", "claude_note.api:app", "--host", "0.0.0.0", "--port", "8000"]
