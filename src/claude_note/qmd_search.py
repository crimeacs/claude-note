"""
QMD semantic search integration for claude-note.

Provides semantic search capabilities via the QMD HTTP API.
Falls back gracefully if the QMD service is not available.
"""

import json
import urllib.request
import urllib.error
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class SearchResult:
    """A semantic search result."""
    path: str
    title: str
    score: float
    snippet: str = ""


def _api_base() -> Optional[str]:
    """Get QMD API base URL from config (lazy import to avoid circular deps)."""
    from claude_note.config import QMD_API_BASE
    return QMD_API_BASE


def _get(path: str, timeout: int = 10) -> Optional[dict]:
    """HTTP GET returning parsed JSON, or None on failure."""
    base = _api_base()
    if not base:
        return None
    try:
        req = urllib.request.Request(f"{base}{path}")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except (urllib.error.URLError, json.JSONDecodeError, OSError):
        return None


def _post(path: str, body: dict, timeout: int = 30) -> Optional[dict]:
    """HTTP POST with JSON body returning parsed JSON, or None on failure."""
    base = _api_base()
    if not base:
        return None
    try:
        data = json.dumps(body).encode()
        req = urllib.request.Request(
            f"{base}{path}",
            data=data,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except (urllib.error.URLError, json.JSONDecodeError, OSError):
        return None


def _parse_results(data: Optional[dict]) -> list[SearchResult]:
    """Extract SearchResult list from QMD API response."""
    if not data:
        return []
    results = []
    for item in data.get("results", []):
        results.append(SearchResult(
            path=item.get("path", ""),
            title=item.get("title", Path(item.get("path", "")).stem),
            score=float(item.get("score", 0)),
            snippet=item.get("snippet", ""),
        ))
    return results


def is_qmd_available() -> bool:
    """Check if the QMD HTTP service is reachable."""
    data = _get("/health", timeout=5)
    return data is not None and data.get("ok", False)


def search_vector(
    query: str,
    limit: int = 10,
    min_score: float = 0.3,
) -> list[SearchResult]:
    """
    Perform semantic (vector) search using QMD vsearch.

    Args:
        query: Natural language query
        limit: Maximum results to return
        min_score: Minimum similarity score (0-1)

    Returns:
        List of SearchResult objects
    """
    data = _post("/vsearch", {"query": query, "limit": limit, "min_score": min_score})
    return _parse_results(data)


def search_keyword(
    query: str,
    limit: int = 10,
) -> list[SearchResult]:
    """
    Perform keyword (BM25) search using QMD search.

    Args:
        query: Keywords to search for
        limit: Maximum results to return

    Returns:
        List of SearchResult objects
    """
    data = _post("/search", {"query": query, "limit": limit})
    return _parse_results(data)


def find_similar_content(
    query: str,
    limit: int = 5,
    min_score: float = 0.6,
) -> list[SearchResult]:
    """
    Find content semantically similar to the given query.

    Used for deduplication checking.

    Args:
        query: Content to find similar matches for
        limit: Maximum results
        min_score: Minimum similarity threshold

    Returns:
        List of SearchResult objects sorted by score descending
    """
    return search_vector(query, limit=limit, min_score=min_score)


def find_related_notes(
    keywords: list[str] = None,
    tags: list[str] = None,
    limit: int = 10,
    use_semantic: bool = True,
) -> list[SearchResult]:
    """
    Find notes related to keywords and tags.

    Combines keyword and semantic search for best results.

    Args:
        keywords: Keywords to search for
        tags: Tags to match (used as additional keywords)
        limit: Maximum results
        use_semantic: Use vector search (slower but better)

    Returns:
        List of SearchResult objects
    """
    query_parts = []
    if keywords:
        query_parts.extend(keywords)
    if tags:
        query_parts.extend(tags)

    if not query_parts:
        return []

    query = " ".join(query_parts)

    if use_semantic:
        return search_vector(query, limit=limit)
    else:
        return search_keyword(query, limit=limit)


def get_document(file_path: str) -> Optional[str]:
    """
    Get the full content of a document by path.

    Args:
        file_path: Path to the document (relative or absolute)

    Returns:
        Document content, or None if not found
    """
    from urllib.parse import quote
    data = _get(f"/document?path={quote(file_path, safe='')}", timeout=10)
    if data and "content" in data:
        return data["content"]
    return None
