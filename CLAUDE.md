# Repository guidance

Claude Note is a local, multi-assistant knowledge loop. Read [README](README.md), [implementation architecture](docs/architecture.md), and [current system boundaries](docs/current-system.md) before changing it.

## Development

- Python 3.11+, standard-library runtime only. Keep optional external tools optional.
- Run tests: `PYTHONPATH=src python3 -m unittest discover -s tests -v`.
- Run CLI from source: `PYTHONPATH=src python3 -m claude_note --help`.
- JSON health must work without a vault config and without network calls.
- Use synthetic fixtures and temporary vaults. Never run tests against personal notes, account exports, or a production shared destination.
- Version lives in `src/claude_note/__init__.py`; pyproject uses Hatch dynamic versioning.

## Contracts

Capture evidence, curated knowledge, shared staging, canonical promotion, and retrieval indexing are separate stages. Do not claim an external shared merger or tenant authorization server is bundled here.

Set note types explicitly. Preserve assistant/author provenance, user corrections, source backlinks, and human text around managed blocks. Reject model paths that escape the vault, including symlinks. Keep session records separate while respecting existing root session files.

QMD's current JSON is an array with `file`; legacy wrappers may return `results` and `path`. Preserve result order and never present BM25 rank as confidence. Require the intended collection before source injection, resolve local files, and bound every subprocess. Do not add cold hybrid-model downloads to the worker path.

Push is opt-in and curated. Keep privacy exclusions and redaction. Bind receipts to destination and vault; an unknown legacy receipt must not cause automatic mass re-upload. Author metadata is not ownership authorization.

Updates preserve recorded checkout/bundle provenance and use this fork for unmanaged releases. Do not silently switch users to another repository. Installers preserve authored config and existing Obsidian settings.

## Review and releases

Use a `codex/`, `claude/`, or human-owned feature branch and a reviewable PR. Stage explicit paths, run meaningful checks, and record validation. Do not commit personal vault data or environment secrets. Do not install the working branch into the user's running services as a side effect of repository work.

A release is a separate maintainer decision: bump the package version deliberately, review the diff, then tag only an approved commit. The tag workflow publishes release metadata. Repository modernization alone does not authorize a release tag or deployment.
