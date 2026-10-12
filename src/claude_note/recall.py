"""Recall before action: put prior knowledge in front of an agent before it works.

Capture (hooks -> queue -> worker -> notes) writes knowledge down. Recall is the
other half: when a prompt arrives, search the QMD index for notes about the same
subject and hand the agent a short, source-linked shortlist *before* it answers.

This is a packaged version of a hook that has run on every prompt of a working
memory system since 2026-09. Each rule below exists because the simple version
missed real knowledge in that system:

* QMD's BM25 is effectively conjunctive. A whole natural-language prompt returns
  nothing; one rare word zeroes a good query. So the prompt is reduced to content
  words, ranked by how much each can discriminate, and searched on a ladder of
  progressively narrower queries.
* Every rung is searched and the hits are pooled. Taking the first non-empty rung
  let a narrow query with a worse note beat a wide one that found the right note.
  A note that several rungs agree on ranks first.
* Paths, URLs and dates are dropped. A path says where to look, never what; it is
  one long token that matched nothing and poisoned every rung it survived to.
* Imperative verbs ("summarise", "evaluate") are dropped. They look like content
  and demand a literal match.
* Hyphenated compounds are split: BM25 keeps "task-backed" as one literal token.
* Session transcripts and the inbox are skipped: they dominate the index by count
  and their snippets are frontmatter.
* Append-log notes show their *newest* managed block, labelled, because BM25
  serves the snippet from wherever the query matched (usually the oldest text).
* A miss is announced, never silent. A hook that prints nothing on a miss is
  indistinguishable from one that never ran, and agents read silence as "the
  vault has nothing on this".
* Everything fails open and is bounded by a wall-clock deadline: recall must
  never block or break a prompt.

Nothing here imports `config` (which exits when no vault is configured); hooks
must run on any machine.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

HOME = Path.home()

# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #


def _config_path() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME", str(HOME / ".config"))
    return Path(xdg) / "claude-note" / "config.toml"


def _load_toml() -> dict:
    path = _config_path()
    try:
        import tomllib
        with open(path, "rb") as fh:
            return tomllib.load(fh)
    except Exception:
        return {}


def _as_list(value) -> list[str]:
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if str(v).strip()]
    return []


def _as_bool(value, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


@dataclass
class Settings:
    enabled: bool = True
    collections: list[str] = field(default_factory=list)      # [] = every collection
    memory_collections: list[str] = field(default_factory=list)
    inject: int = 4
    memory_slots: int = 2
    search_n: int = 10
    timeout: float = 6.0          # per qmd call, wall clock (subprocess.run)
    budget: float = 8.0           # whole recall, wall clock
    min_prompt: int = 15
    max_terms: int = 10
    snippet_chars: int = 260
    index: str = ""               # qmd --index name; "" = default index
    skip_paths: list[str] = field(default_factory=lambda: [
        r"/sessions/", r"/claude-session-", r"/claude-note-inbox\.md$"])
    single_term_fallback: bool = False  # on a miss, also search the two most specific words alone
    rerank_command: str = ""      # optional external relevance judge
    rerank_timeout: float = 2.5
    log: bool = True
    qmd: str = ""                 # explicit qmd executable

    @classmethod
    def load(cls, overrides: Optional[dict] = None) -> "Settings":
        raw = _load_toml().get("recall", {}) or {}
        raw = {**raw, **(overrides or {})}
        s = cls()
        s.enabled = _as_bool(raw.get("enabled"), True)
        if os.environ.get("CLAUDE_NOTE_RECALL", "").lower() in {"off", "0", "false"}:
            s.enabled = False
        s.collections = _as_list(raw.get("collections", []))
        s.memory_collections = _as_list(raw.get("memory_collections", []))
        for name in ("inject", "memory_slots", "search_n", "min_prompt", "max_terms", "snippet_chars"):
            if name in raw:
                setattr(s, name, int(raw[name]))
        for name in ("timeout", "budget", "rerank_timeout"):
            if name in raw:
                setattr(s, name, float(raw[name]))
        s.index = str(raw.get("index", "") or os.environ.get("CLAUDE_NOTE_RECALL_INDEX", ""))
        if "skip_paths" in raw:
            s.skip_paths = _as_list(raw["skip_paths"])
        s.single_term_fallback = _as_bool(raw.get("single_term_fallback"), False)
        s.rerank_command = str(raw.get("rerank_command", "") or "")
        s.log = _as_bool(raw.get("log"), True) and os.environ.get("CLAUDE_NOTE_RECALL_LOG") != "0"
        s.qmd = str(raw.get("qmd", "") or "")
        return s


# --------------------------------------------------------------------------- #
# Query construction
# --------------------------------------------------------------------------- #

STOPWORDS = frozenset("""
a about after all also am an and any are as at be because been before being
but by can cant could did do does doing dont down during each few for from
further get got had has have having he her here hers him his how i if in into
is it its just let like make me more most my no nor not now of off on once
only or other our out over own please same she should so some such than that
the their them then there these they this those through to too under until up
us use used using very want was way we were what when where which while who
whom why will with would you your
lets need really maybe sure find best thing things stuff bit lot ok okay
summarise summarize explain describe review look looking stands status
evaluate integrate produce define assess explore create draft build wire ship
""".split())

WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*")
PATHISH = re.compile(r"^(https?:|/|~|\.{1,2}/)|/")
DATEISH = re.compile(r"^(?:19|20)\d\d(?:[-_/.]\d\d?(?:[-_/.]\d\d?)?)?$|^\d{1,2}[-/]\d{1,2}(?:[-/]\d{2,4})?$")
HYPHENATED = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:-[A-Za-z0-9]+)+")
COMPOUND = re.compile(r"[A-Za-z0-9]+(?:[_.][A-Za-z0-9]+)+")
# Harness-generated events are not questions; searching their ids only prints misses.
AUTOMATED_EVENT = re.compile(r"<task-notification>|\[SYSTEM NOTIFICATION")
# Wrapper tags keep their inner text but lose their markup (and attributes).
WRAPPER_TAG = re.compile(r"</?[A-Za-z_][\w-]*(?:\s[^<>]*)?>")


def content_terms(text: str, max_terms: int = 10) -> list[str]:
    """Content words of `text`, in order, de-duplicated, at most `max_terms`."""
    text = WRAPPER_TAG.sub(" ", text or "")
    words: list[str] = []
    for w in WORD.findall(text):
        w = w.strip("._")
        if not w:
            continue
        if PATHISH.search(w) or DATEISH.match(w):
            words.append(w)
        elif HYPHENATED.fullmatch(w) or COMPOUND.fullmatch(w):
            # BM25 keeps "task-backed", "source_unchanged" and "config.toml" as ONE
            # literal token: each matched nothing and zeroed every rung it reached.
            words.extend(p for p in re.split(r"[-_.]", w) if p)
        else:
            words.append(w)
    terms, seen = [], set()
    for w in words:
        lw = w.lower()
        if len(lw) < 3 or lw in STOPWORDS or lw in seen:
            continue
        if DATEISH.match(w) or PATHISH.search(w):
            continue
        seen.add(lw)
        terms.append(w)
        if len(terms) >= max_terms:
            break
    return terms


def query_ladder(terms: list[str], first_word: str = "") -> list[list[str]]:
    """The full query, then the top 5, 3 and 2 most discriminating terms.

    A capital is proper-noun evidence except on the prompt's first word, which is
    capitalised by grammar. Digits and length mark rare, specific terms. Narrower
    rungs keep the prompt's own word order.
    """
    def weight(t: str):
        return (t[:1].isupper() and t != first_word, any(c.isdigit() for c in t), len(t))

    ranked = sorted(terms, key=weight, reverse=True)

    def top(k: int) -> list[str]:
        keep = set(ranked[:k])
        return [t for t in terms if t in keep]

    ladder = [rung for rung in (terms, top(5), top(3), top(2)) if len(rung) >= 2]
    return [r for i, r in enumerate(ladder) if i == 0 or r != ladder[i - 1]]


# --------------------------------------------------------------------------- #
# QMD access
# --------------------------------------------------------------------------- #


def qmd_executable(settings: Settings) -> Optional[str]:
    if settings.qmd:
        return settings.qmd if os.access(settings.qmd, os.X_OK) else None
    try:
        from .health import _which
        found = _which("qmd")
        return found or None
    except Exception:
        import shutil
        return shutil.which("qmd")


Runner = Callable[[list[str], float], Optional[list]]


def _run_qmd(cmd: list[str], timeout: float) -> Optional[list]:
    """None = qmd unavailable/failed/hung; [] = ran and found nothing."""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL)
    except Exception:
        return None
    if proc.returncode != 0:
        # qmd prints "No results found." (exit 0) on a miss; non-zero is a failure.
        return None
    out = proc.stdout.strip()
    if not out:
        return []
    try:
        data = json.loads(out)
    except ValueError:
        return []
    if isinstance(data, dict):
        data = data.get("results", [])
    return data if isinstance(data, list) else []


def _collection_dirs(index: str = "") -> dict[str, Path]:
    """Collection name -> directory, read from QMD's own config."""
    # QMD reads ~/.config/qmd even when XDG_CONFIG_HOME points elsewhere; try both.
    name = f"{index or 'index'}.yml"
    candidates = [Path(os.environ["XDG_CONFIG_HOME"]) / "qmd" / name] if os.environ.get("XDG_CONFIG_HOME") else []
    candidates.append(HOME / ".config" / "qmd" / name)
    dirs: dict[str, Path] = {}
    text = ""
    for cfg in candidates:
        try:
            text = cfg.read_text()
            break
        except OSError:
            continue
    for m in re.finditer(r"^  ([A-Za-z0-9_.-]+):\s*\n\s+path:\s*[\"']?([^\"'\n]+)", text, re.M):
        dirs[m.group(1)] = Path(m.group(2).strip()).expanduser()
    return dirs


def local_path(uri: str, index: str = "") -> Optional[Path]:
    """The file behind a qmd:// URI, if it lives inside its collection directory."""
    m = re.match(r"qmd://([^/]+)/(.+)$", uri or "")
    if not m:
        p = Path(uri)
        return p if p.is_absolute() and p.is_file() else None
    base = _collection_dirs(index).get(m.group(1))
    if not base:
        return None
    try:
        root = base.resolve()
        cand = (base / m.group(2)).resolve()
        cand.relative_to(root)
    except (OSError, ValueError):
        return None
    if cand.is_file():
        return cand
    # QMD lower-cases and slugs paths in URIs; fall back to a case-insensitive match.
    want = m.group(2).lower()
    try:
        for p in root.rglob("*.md"):
            rel = str(p.relative_to(root)).lower().replace(" ", "-")
            if rel == want:
                return p
    except OSError:
        pass
    return None


# --------------------------------------------------------------------------- #
# Snippets
# --------------------------------------------------------------------------- #

DIFF_HEADER = re.compile(r"@@[^@]*@@\s*(\([^)]*\))?")
FRONTMATTER_ONLY = re.compile(r"^[\s-]*(tags|aliases|created|type|session_id|date|status|title|name)\s*:", re.I)
SYNTH_BLOCK = re.compile(r"<!--\s*claude-note:([^:]+?):start\s*-->(.*?)<!--\s*claude-note:\1:end\s*-->", re.S)
ISO_DATE = re.compile(r"20\d\d-\d\d-\d\d")
TYPE_LINE = re.compile(r"^type:\s*[\"']?([A-Za-z-]+)", re.M)


FM_LINE = re.compile(r"^\s*(title|type|created|updated|assistant|author|tags|aliases|date|status|name|"
                     r"description|session_id|share|metadata|source)\s*:.*$", re.I | re.M)


def clean(snippet: str, limit: int) -> str:
    s = DIFF_HEADER.sub("", snippet or "")
    s = re.sub(r"^---\s*$", " ", s, flags=re.M)
    s = FM_LINE.sub(" ", s)
    s = re.sub(r"^\s*#{1,6}\s+", "", s, flags=re.M)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:limit].strip()


def freshest_block(text: str, limit: int) -> Optional[tuple[str, str]]:
    """(label, body) of the newest dated managed block when a note has >= 2."""
    dated = []
    for b in SYNTH_BLOCK.finditer(text):
        d = ISO_DATE.findall(b.group(1)) or ISO_DATE.findall(b.group(2)[:240])
        if d:
            dated.append((max(d), b.group(2)))
    if len(dated) < 2:
        return None
    dated.sort(key=lambda x: x[0])
    newest, body = dated[-1]
    body = re.sub(r"<!--.*?-->", " ", body.strip(), flags=re.S)
    body = re.sub(r"^\s*#+\s*[^\n]*\n", "", body.strip(), count=1)
    body = re.sub(r"^\s*\*\*(Session|Source|Context)\*\*:[^\n]*\n", "", body.strip())
    label = f"[newest of {len(dated)} updates, {newest}; history from {dated[0][0]} in the file]"
    return label, clean(body, limit)


def body_excerpt(text: str, limit: int) -> str:
    """First real prose of a note: after frontmatter, headings and comments."""
    body = re.sub(r"\A---\n.*?\n---\n", "", text, count=1, flags=re.S)
    body = re.sub(r"<!--.*?-->", " ", body, flags=re.S)
    lines = [l for l in body.splitlines() if l.strip() and not l.lstrip().startswith("#")]
    return clean("\n".join(lines), limit)


def note_type(text: str) -> str:
    head = text[:1500]
    if not head.lstrip().startswith("---"):
        return ""
    m = TYPE_LINE.search(head)
    return m.group(1).lower() if m else ""


# --------------------------------------------------------------------------- #
# Recall
# --------------------------------------------------------------------------- #


@dataclass
class Hit:
    uri: str
    title: str
    snippet: str
    collection: str = ""
    type: str = ""
    memory: bool = False
    votes: int = 0
    fresh_label: str = ""
    path: str = ""                 # local file, when it resolves: readable without qmd

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v not in ("", None)}


@dataclass
class Result:
    status: str                    # hit | miss | unavailable | skipped
    reason: str = ""
    query: list[str] = field(default_factory=list)
    narrowed: list[str] = field(default_factory=list)
    hits: list[Hit] = field(default_factory=list)
    ms: int = 0
    pool: int = 0
    candidates: list[str] = field(default_factory=list)   # usable matches before the inject cut (evals)

    def to_dict(self) -> dict:
        return {"status": self.status, "reason": self.reason, "query": " ".join(self.query),
                "narrowed": " ".join(self.narrowed), "pool": self.pool, "ms": self.ms,
                "hits": [h.to_dict() for h in self.hits]}


def _rerank(prompt: str, hits: list[Hit], settings: Settings) -> Optional[list[Hit]]:
    """Optional external judge: JSON on stdin -> {"keep": [ids, best first]}.

    None means "judge unavailable, keep pool order": a judge must never be able to
    break recall. [] means the judge looked and nothing was relevant.
    """
    if not settings.rerank_command or not hits:
        return None
    payload = json.dumps({"request": prompt[:600], "candidates": [
        {"id": f"c{i}", "title": h.title[:120], "excerpt": h.snippet[:360], "uri": h.uri}
        for i, h in enumerate(hits)]})
    box: dict = {}

    def call():
        try:
            proc = subprocess.run(settings.rerank_command, shell=True, input=payload,
                                  capture_output=True, text=True, timeout=settings.rerank_timeout)
            if proc.returncode == 0:
                box["keep"] = json.loads(proc.stdout).get("keep")
        except Exception as exc:  # noqa: BLE001 - judge failures are optional
            box["err"] = exc

    t = threading.Thread(target=call, daemon=True)
    t.start()
    t.join(settings.rerank_timeout + 0.5)  # a real deadline, not a socket timeout
    keep = box.get("keep")
    if t.is_alive() or not isinstance(keep, list):
        return None
    by_id = {f"c{i}": h for i, h in enumerate(hits)}
    return [by_id[k] for k in keep if k in by_id]


def recall(prompt: str, settings: Optional[Settings] = None, runner: Runner = _run_qmd,
           *, prefer_types: tuple[str, ...] = (), extra_rungs: Optional[list[list[str]]] = None) -> Result:
    """Search the index for prior knowledge about `prompt`. Never raises."""
    settings = settings or Settings.load()
    started = time.monotonic()
    result = Result(status="skipped")
    try:
        _recall(prompt, settings, runner, result, started, prefer_types, extra_rungs or [])
    except Exception as exc:  # noqa: BLE001 - recall fails open
        result.status, result.reason = "unavailable", f"recall error: {type(exc).__name__}"
    result.ms = int((time.monotonic() - started) * 1000)
    return result


def _recall(prompt, settings, runner, result, started, prefer_types, extra_rungs):
    if not settings.enabled:
        result.reason = "recall disabled"
        return
    prompt = (prompt or "").strip()
    if AUTOMATED_EVENT.search(prompt):
        result.reason = "automated event"
        return
    prompt = WRAPPER_TAG.sub(" ", prompt).strip()
    if len(prompt) < settings.min_prompt or prompt.startswith("/"):
        result.reason = "prompt too short or a slash command"
        return
    terms = content_terms(prompt, settings.max_terms)
    if len(terms) < 2 and not extra_rungs:
        result.reason = "fewer than two content words"
        return
    first = (WORD.findall(prompt) or [""])[0]
    ladder = query_ladder(terms, first) if len(terms) >= 2 else []
    # Callers may add rungs, e.g. single error identifiers, which are specific alone.
    ladder += [r for r in extra_rungs if r and r not in ladder]
    if not ladder:
        result.reason = "nothing searchable"
        return
    result.query, result.narrowed = ladder[0], ladder[-1]

    exe = qmd_executable(settings)
    if not exe:
        result.status, result.reason = "unavailable", "qmd is not installed"
        return
    base = [exe] + (["--index", settings.index] if settings.index else [])
    scopes: list[tuple[Optional[str], bool]] = [(c, False) for c in settings.collections] or [(None, False)]
    scopes += [(c, True) for c in settings.memory_collections]

    pool: dict[str, dict] = {}
    ran = False
    for scope, is_memory in scopes:
        for rung_i, rung in enumerate(ladder):
            if time.monotonic() - started > settings.budget:
                break
            cmd = base + ["search", " ".join(rung), "-n", str(settings.search_n), "--json"]
            if scope:
                cmd += ["-c", scope]
            got = runner(cmd, settings.timeout)
            if got is None:
                continue
            ran = True
            for pos, item in enumerate(got):
                if not isinstance(item, dict):
                    continue
                uri = item.get("file") or item.get("path")
                if not isinstance(uri, str) or not uri:
                    continue
                c = pool.setdefault(uri, {"item": item, "votes": 0, "best": 1e9, "memory": False})
                c["votes"] += 1
                c["best"] = min(c["best"], pos + 3 * rung_i)
                c["memory"] = c["memory"] or is_memory
    if not pool and ran and settings.single_term_fallback:
        # Every rung asked for all its words. One mis-guessed word ("generated" for a note that
        # says "minted") still zeroes the narrowest rung, so try its two words one at a time.
        for rung_i, word in enumerate(ladder[-1][:2], start=len(ladder)):
            if time.monotonic() - started > settings.budget:
                break
            for scope, is_memory in scopes:
                cmd = base + ["search", word, "-n", str(settings.search_n), "--json"]
                if scope:
                    cmd += ["-c", scope]
                for pos, item in enumerate(runner(cmd, settings.timeout) or []):
                    uri = isinstance(item, dict) and (item.get("file") or item.get("path"))
                    if isinstance(uri, str) and uri:
                        c = pool.setdefault(uri, {"item": item, "votes": 0, "best": 1e9, "memory": False})
                        c["votes"] += 1
                        c["best"] = min(c["best"], pos + 3 * rung_i)
                        c["memory"] = c["memory"] or is_memory
    if not ran:
        result.status, result.reason = "unavailable", "qmd failed, hung, or its index was locked"
        return
    result.pool = len(pool)
    if not pool:
        result.status, result.reason = "miss", "no keyword match on any rung of the query"
        return

    skip = [re.compile(p) for p in settings.skip_paths]
    ordered = sorted(pool.values(), key=lambda c: (-c["votes"], c["best"]))
    candidates: list[Hit] = []
    for c in ordered:
        item, uri = c["item"], c["item"].get("file") or c["item"].get("path")
        if any(p.search(uri) for p in skip):
            continue
        body = clean(item.get("snippet", ""), settings.snippet_chars)
        hit = Hit(uri=uri, title=str(item.get("title") or Path(uri).stem), snippet=body,
                  collection=(re.match(r"qmd://([^/]+)/", uri) or [None, ""])[1],
                  memory=c["memory"], votes=c["votes"])
        path = local_path(uri, settings.index)
        if path:
            hit.path = str(path)
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
                hit.type = note_type(text)
                fresh = freshest_block(text, settings.snippet_chars)
                if fresh:
                    hit.fresh_label, hit.snippet = fresh
                elif len(hit.snippet) < 40:
                    # The match was in the frontmatter or a heading; show the prose.
                    hit.snippet = body_excerpt(text, settings.snippet_chars)
            except OSError:
                pass
        if hit.type == "session":
            continue
        if len(hit.snippet) < 40 or FRONTMATTER_ONLY.match(hit.snippet):
            continue
        candidates.append(hit)
        if len(candidates) >= 16:
            break
    if prefer_types:
        candidates.sort(key=lambda h: h.type not in prefer_types)  # stable: keeps vote order
    result.candidates = [h.path or h.uri for h in candidates]

    judged = _rerank(prompt, candidates, settings)
    if judged is not None:
        if not judged:
            result.status, result.reason = "miss", "candidates were found but the relevance judge kept none"
            return
        candidates = judged

    # Reserve slots for hand-written memories: short, so BM25 under-ranks them,
    # and the highest-signal entries in the index.
    memories = [h for h in candidates if h.memory][: settings.memory_slots]
    rest = [h for h in candidates if h not in memories]
    picked = (memories + rest)[: settings.inject] if memories else rest[: settings.inject]
    if not picked:
        result.status, result.reason = "miss", "matches were all session records or bare frontmatter"
        return
    result.status, result.hits = "hit", picked


# --------------------------------------------------------------------------- #
# Presentation
# --------------------------------------------------------------------------- #


def format_context(result: Result) -> str:
    if result.status == "hit":
        lines = [
            "claude-note recall: possibly relevant prior knowledge (keyword matches, NOT verified answers).",
            "Open a note before relying on it (read the file shown), check that it still applies, then act.",
            "If none fit, this lookup missed; it does not mean nothing is known. Search yourself with",
            "other words (`claude-note recall \"<content words>\"` or `qmd vsearch \"<question>\"`).",
            "",
        ]
        for h in result.hits:
            meta = ", ".join(x for x in (h.type, "memory" if h.memory else "") if x)
            lines.append(f"- {h.title}" + (f" ({meta})" if meta else "") + f"  [{h.path or h.uri}]")
            lines.append(f"    {(h.fresh_label + ' ') if h.fresh_label else ''}{h.snippet}")
        lines += ["", "If this task teaches something durable, write it back: "
                      "`claude-note remember --type gotcha \"<what broke, why, the fix>\"`."]
        return "\n".join(lines)
    if result.status in ("miss", "unavailable"):
        q = " ".join(result.query)
        narrowed = " ".join(result.narrowed)
        return "\n".join([
            f"claude-note recall ran and found nothing ({result.reason}).",
            f"Query tried: {q!r}" + (f", narrowed to {narrowed!r}" if narrowed and narrowed != q else ""),
            "",
            "This is NOT evidence that nothing is known: keyword search is conjunctive, so one",
            "rare or mis-guessed word zeroes a query. If this task touches prior work, an incident,",
            "a recurring error or a past decision, search yourself before acting:",
            "`claude-note recall \"<two or three content words>\"`, or `qmd vsearch \"<question>\"`.",
        ])
    return ""


def format_terminal(result: Result) -> str:
    if result.status != "hit":
        return format_context(result) or f"recall skipped: {result.reason}"
    out = [f"{len(result.hits)} note(s) in {result.ms} ms  (query: {' '.join(result.query)})", ""]
    for i, h in enumerate(result.hits, 1):
        tag = f"[{h.type}]" if h.type else ""
        out.append(f"{i}. {h.title} {tag}{'  memory' if h.memory else ''}")
        out.append(f"   {h.path or h.uri}")
        if h.fresh_label:
            out.append(f"   {h.fresh_label}")
        out.append(f"   {h.snippet}")
        out.append("")
    return "\n".join(out).rstrip()


# --------------------------------------------------------------------------- #
# Logging: so "is recall working?" has an answer
# --------------------------------------------------------------------------- #


def log_path() -> Path:
    return HOME / ".local" / "share" / "claude-note" / "recall.jsonl"


def log_event(kind: str, result: Result, extra: Optional[dict] = None) -> None:
    try:
        path = log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        # `ts` (UTC) joins a row to transcript timestamps; `q` is the query's content words only,
        # kept locally so `claude-note eval live` can list what keeps missing.
        row = {"t": time.strftime("%Y-%m-%dT%H:%M:%S"), "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               "kind": kind, "status": result.status, "q": " ".join(result.query),
               "reason": result.reason, "ms": result.ms, "pool": result.pool,
               "query_sha": hashlib.sha1(" ".join(result.query).encode()).hexdigest()[:10],
               "hits": [h.uri for h in result.hits]}
        row.update(extra or {})
        with open(path, "a") as fh:
            fh.write(json.dumps(row) + "\n")
        # Keep the log bounded: trim to the newest 5000 rows past ~2 MB.
        if path.stat().st_size > 2_000_000:
            rows = path.read_text().splitlines()[-5000:]
            path.write_text("\n".join(rows) + "\n")
    except Exception:
        pass


def stats(days: int = 7) -> dict:
    cutoff = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - days * 86400))
    counts: dict[str, dict[str, int]] = {}
    ms: list[int] = []
    try:
        for line in log_path().read_text().splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("t", "") < cutoff:
                continue
            k = counts.setdefault(row.get("kind", "?"), {})
            k[row.get("status", "?")] = k.get(row.get("status", "?"), 0) + 1
            if row.get("status") in ("hit", "miss"):
                ms.append(int(row.get("ms", 0)))
    except OSError:
        pass
    ms.sort()
    return {"days": days, "by_kind": counts,
            "p50_ms": ms[len(ms) // 2] if ms else None,
            "p95_ms": ms[int(len(ms) * 0.95)] if ms else None}


# --------------------------------------------------------------------------- #
# Claude Code MEMORY.md load cap
# --------------------------------------------------------------------------- #

MEMORY_CAP_BYTES = 25 * 1024
MEMORY_CAP_LINES = 200


def memory_md_warning(cwd: str) -> str:
    """Claude Code loads only the first 200 lines / 25 KB of a project's MEMORY.md
    and silently drops the rest. Warn at 90% so an index is split before entries
    (the newest, since memories are appended) disappear."""
    if not cwd:
        return ""
    proj = re.sub(r"[^A-Za-z0-9]", "-", cwd)
    path = HOME / ".claude" / "projects" / proj / "memory" / "MEMORY.md"
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    size, lines = len(data), data.count(b"\n")
    if size < MEMORY_CAP_BYTES * 0.9 and lines < MEMORY_CAP_LINES * 0.9:
        return ""
    state = "OVER" if size > MEMORY_CAP_BYTES or lines > MEMORY_CAP_LINES else "near"
    return (f"MEMORY.md is {state} Claude Code's load cap ({size:,} B, {lines} lines; cap "
            f"{MEMORY_CAP_BYTES:,} B or {MEMORY_CAP_LINES} lines). Entries past the cap are silently "
            "dropped. Move detail into topic files and keep MEMORY.md an index.\n\n")


# --------------------------------------------------------------------------- #
# Hook entry point (UserPromptSubmit, Claude Code and Codex)
# --------------------------------------------------------------------------- #


def emit(event: str, context: str) -> None:
    if not context:
        return
    json.dump({"hookSpecificOutput": {"hookEventName": event, "additionalContext": context}}, sys.stdout)
    sys.stdout.write("\n")


def hook_main(stdin=None) -> int:
    """`claude-note hook recall`: reads the hook payload, prints additionalContext."""
    if os.environ.get("CLAUDE_CODE_HOOKS_ENABLED") == "false":
        return 0
    try:
        payload = json.load(stdin or sys.stdin)
    except Exception:
        return 0
    event = payload.get("hook_event_name") or "UserPromptSubmit"
    settings = Settings.load()
    result = recall(payload.get("prompt") or "", settings)
    if settings.log and result.status != "skipped":
        # Session and file paths let `eval live` check whether the agent opened what it was shown.
        log_event("prompt", result, {"session": payload.get("session_id") or "",
                                     "transcript": payload.get("transcript_path") or "",
                                     "paths": [h.path for h in result.hits if h.path]})
    warning = memory_md_warning(payload.get("cwd") or "")
    emit(event, warning + format_context(result))
    return 0
