"""Bounded QMD CLI retrieval. Search scores are rankings, not confidence.

QMD emits a JSON array with ``file`` URIs. Older wrappers emitted
``{"results": [...]}`` and ``path``; accept both without changing result order.
"""

import json
import logging
import math
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import unquote, urlsplit

logger = logging.getLogger(__name__)


@dataclass
class SearchResult:
    path: str
    title: str
    score: float
    snippet: str = ""


def _qmd_bin() -> Optional[str]:
    # The worker may have launchd's minimal PATH.
    from .health import _which
    return _which("qmd")


def is_qmd_available() -> bool:
    """Discover the executable without starting a model or scanning the index."""
    return bool(_qmd_bin())


def _settings(collection: Optional[str], timeout: Optional[float]) -> tuple[str, float]:
    from . import config
    scope = config.QMD_COLLECTION if collection is None else collection
    deadline = config.QMD_TIMEOUT if timeout is None else timeout
    return str(scope or ""), max(0.1, float(deadline))


def _parse_results(output: str) -> list[SearchResult]:
    data = json.loads(output)
    items = data.get("results", []) if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise ValueError("QMD results must be an array")
    results = []
    for item in items:
        if not isinstance(item, dict):
            continue
        path = item.get("file") or item.get("path")
        if not isinstance(path, str) or not path:
            continue
        try:
            score = float(item.get("score", 0))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(score):
            continue
        title = item.get("title")
        snippet = item.get("snippet")
        results.append(SearchResult(
            path=path,
            title=title if isinstance(title, str) else Path(path).stem,
            score=score,
            snippet=snippet if isinstance(snippet, str) else "",
        ))
    return results


def _search(mode: str, query: str, limit: int, collection: Optional[str],
            timeout: Optional[float], min_score: Optional[float] = None) -> list[SearchResult]:
    if not query.strip() or limit <= 0:
        return []
    executable = _qmd_bin()
    if not executable:
        return []
    scope, deadline = _settings(collection, timeout)
    args = [executable, mode, query, "-n", str(limit), "--json"]
    if scope:
        args.extend(["-c", scope])
    # BM25's transformed score is not a similarity threshold.
    if mode == "vsearch" and min_score is not None:
        args.extend(["--min-score", str(min_score)])
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=deadline)
        if result.returncode != 0:
            logger.warning("QMD %s failed (exit %s); using vault index", mode, result.returncode)
            return []
        return _parse_results(result.stdout)[:limit]
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        logger.warning("QMD %s unavailable (%s); using vault index", mode, type(exc).__name__)
        return []


def search_vector(query: str, limit: int = 10, min_score: float = 0.3, *,
                  collection: Optional[str] = None, timeout: Optional[float] = None) -> list[SearchResult]:
    """Explicit vector retrieval; may require QMD's embedding model."""
    return _search("vsearch", query, limit, collection, timeout, min_score)


def search_keyword(query: str, limit: int = 10, *, collection: Optional[str] = None,
                   timeout: Optional[float] = None) -> list[SearchResult]:
    """BM25 retrieval, preserving QMD's order without a score cutoff."""
    return _search("search", query, limit, collection, timeout)


_STOPWORDS = frozenset("a an and are as at be been but by can could do does for from had has have how i if in into is it its lets me my of on or our please should so some than that the their them then there these they this to up us use using was we were what when where which who why will with would you your".split())


def keyword_query(text: str, max_terms: int = 4) -> str:
    """Keep a small set of content words for QMD's conjunctive BM25 query."""
    terms = []
    for token in re.findall(r"[\w]{3,}", text.lower().replace("-", " ")):
        if token not in _STOPWORDS and token not in terms:
            terms.append(token)
        if len(terms) >= max_terms:
            break
    return " ".join(terms)


def find_similar_content(query: str, limit: int = 5, min_score: float = 0.6) -> list[SearchResult]:
    return search_vector(query, limit=limit, min_score=min_score)


def find_related_notes(keywords: Optional[list[str]] = None, tags: Optional[list[str]] = None,
                       limit: int = 10, use_semantic: bool = False) -> list[SearchResult]:
    query = " ".join((keywords or []) + (tags or []))
    if use_semantic:
        return search_vector(query, limit=limit)
    return search_keyword(keyword_query(query), limit=limit)


def resolve_result_path(file_path: str, vault_root: Path, collection: str = "") -> Optional[Path]:
    """Resolve local-vault paths or QMD URIs from the explicit collection.

    Never map a different collection to a same-named note in this vault. Resolve
    symlinks before checking containment so retrieval cannot nominate outside files.
    """
    try:
        if file_path.startswith("qmd://"):
            uri = urlsplit(file_path)
            if not collection or uri.netloc != collection or uri.query or uri.fragment:
                return None
            candidate = vault_root / unquote(uri.path).lstrip("/")
        elif "://" in file_path:
            return None
        else:
            path = Path(file_path)
            candidate = path if path.is_absolute() else vault_root / path
        root = vault_root.resolve()
        candidate = candidate.resolve()
        candidate.relative_to(root)
        if candidate.suffix.lower() != ".md":
            return None
        return candidate
    except (OSError, RuntimeError, ValueError):
        return None


def get_document(file_path: str) -> Optional[str]:
    executable = _qmd_bin()
    if not executable:
        return None
    _, deadline = _settings(None, None)
    try:
        result = subprocess.run([executable, "get", file_path], capture_output=True,
                                text=True, timeout=deadline)
        return result.stdout if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None
