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
* Rank and deliver the same source-versioned passage. A newer unrelated block
  must never replace the evidence that matched the question.
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
import math
import os
import re
import selectors
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field, replace
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
    min_prompt: int = 15          # legacy setting; meaningful short prompts remain searchable
    max_terms: int = 10
    snippet_chars: int = 260
    context_token_budget: int = 4096  # conservative UTF-8 byte upper bound; no tokenizer dependency
    packet_bytes: int = 8192
    source_bytes: int = 1_000_000
    resolve_scan_limit: int = 1000
    repository: str = ""
    environment: str = ""
    audiences: list[str] = field(default_factory=lambda: ["host"])
    index: str = ""               # qmd --index name; "" = default index
    skip_paths: list[str] = field(default_factory=lambda: [
        r"/sessions/", r"/claude-session-", r"/claude-note-inbox\.md$"])
    single_term_fallback: bool = False  # on a miss, also search the two most specific words alone
    # The warm recall server (recall_server.mjs, QMD 2.x SDK): the keyword ladder AND a search by
    # meaning in one local request, fused here, then QMD's reranker. Falls back to the CLI ladder.
    # Defaults were chosen on the dev half of the eval set; see docs/evals.md.
    engine: str = "auto"          # auto (server if it answers) | server | cli
    server_url: str = "http://127.0.0.1:8182"
    vector: bool = True           # search the whole question by meaning
    vector_weight: float = 2.0    # weight of the meaning list against each keyword rung (RRF)
    vector_gate: float = 0.40     # floor for semantic-only candidates; lexical evidence is independent
    rerank: bool = True           # rerank the fused top `rerank_top` with QMD's reranker
    rerank_top: int = 12
    rerank_chars: int = 1200      # upper bound on the exact passage ranked and delivered
    rerank_clean: bool = True     # retained for config compatibility; passages always exclude metadata
    rerank_blend: float = 0.0     # 0 = order by the reranker alone; >0 also keeps some of the fusion order
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
        s.repository = str(raw.get("repository", "") or "")
        s.environment = str(raw.get("environment", "") or "")
        s.audiences = _as_list(raw.get("audiences", ["host"]))
        for name in ("inject", "memory_slots", "search_n", "min_prompt", "max_terms", "snippet_chars",
                     "context_token_budget", "packet_bytes", "source_bytes", "resolve_scan_limit"):
            if name in raw:
                setattr(s, name, int(raw[name]))
        for name in ("timeout", "budget", "rerank_timeout"):
            if name in raw:
                setattr(s, name, float(raw[name]))
        s.index = str(raw.get("index", "") or os.environ.get("CLAUDE_NOTE_RECALL_INDEX", ""))
        if "skip_paths" in raw:
            s.skip_paths = _as_list(raw["skip_paths"])
        s.single_term_fallback = _as_bool(raw.get("single_term_fallback"), False)
        s.engine = str(raw.get("engine", "auto") or "auto")
        s.server_url = str(raw.get("server_url", s.server_url) or s.server_url).rstrip("/")
        s.vector = _as_bool(raw.get("vector"), True)
        s.rerank = _as_bool(raw.get("rerank"), True)
        for name in ("vector_weight", "vector_gate"):
            if name in raw:
                setattr(s, name, float(raw[name]))
        for name in ("rerank_top", "rerank_chars"):
            if name in raw:
                setattr(s, name, int(raw[name]))
        if "rerank_clean" in raw:
            s.rerank_clean = _as_bool(raw["rerank_clean"], False)
        if "rerank_blend" in raw:
            s.rerank_blend = float(raw["rerank_blend"])
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
lets need really maybe sure find best thing things stuff bit lot ok okay thanks hello
summarise summarize explain describe review look looking stands status
evaluate integrate produce define assess explore create draft build wire ship
""".split())

WORD = re.compile(r"[^\W_][\w./-]*", re.UNICODE)
PATHISH = re.compile(r"^(https?:|/|~|\.{1,2}/)|/")
DATEISH = re.compile(r"^(?:19|20)\d\d(?:[-_/.]\d\d?(?:[-_/.]\d\d?)?)?$|^\d{1,2}[-/]\d{1,2}(?:[-/]\d{2,4})?$")
HYPHENATED = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:-[A-Za-z0-9]+)+")
COMPOUND = re.compile(r"[A-Za-z0-9]+(?:[_.][A-Za-z0-9]+)+")
# Harness-generated events are not questions; searching their ids only prints misses.
AUTOMATED_EVENT = re.compile(r"<task-notification>|\[SYSTEM NOTIFICATION")
# Wrapper tags keep their inner text but lose their markup (and attributes).
WRAPPER_TAG = re.compile(r"</?[A-Za-z_][\w-]*(?:\s[^<>]*)?>")
# These follow-ups depend on the preceding conversation, which a prompt hook
# does not receive. Searching "fix" alone injects arbitrary incident notes.
# Keep this narrow: short subjects such as "codex hooks" and single identifiers
# are still useful queries, and non-English intent still reaches semantic search.
UNRESOLVED_FOLLOWUP = re.compile(
    r"^(?:(?:please\s+)?(?:(?:fix|repair|check|handle|do)\s+(?:it|this|that)"
    r"|get\s+(?:it|this|that)\s+to\s+work)(?:\s+please)?"
    r"(?:[.!?]+\s*|\s+and\s+|\s*;\s*|$))+$", re.IGNORECASE)
SUBJECT_FREE_ACKNOWLEDGEMENT = re.compile(
    r"^(?:(?:yes|no|yep|nope|ok|okay|sure)(?:[\s,]+(?:please|thanks|thank\s+you))?"
    r"|thanks|thank\s+you)[.!?]*$", re.IGNORECASE)


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
    if len(terms) > max_terms:
        # The decisive correction often comes last. Keep both ends rather than
        # silently discarding every term after the first ten.
        head = max_terms // 2
        return terms[:head] + terms[-(max_terms - head):]
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

    if len(terms) == 1:
        return [terms]
    ladder = [rung for rung in (terms, top(5), top(3), top(2)) if len(rung) >= 2]
    return [r for i, r in enumerate(ladder) if i == 0 or r != ladder[i - 1]]


def complementary_pairs(ladder: list[list[str]]) -> list[list[str]]:
    """Bounded leave-one-out alternatives, without guessing a term's spelling.

    Length ranking can keep the same typo in every narrower query. The other
    pairs of the three-term rung let one poisoned term drop out while retaining
    two original subject words. Normal ladders add at most two queries.
    """
    triple = next((rung for rung in reversed(ladder) if len(rung) == 3), [])
    pairs = []
    for omitted in range(len(triple)):
        pair = [word for i, word in enumerate(triple) if i != omitted]
        if pair not in ladder and pair not in pairs:
            pairs.append(pair)
    return pairs


def semantic_intent(prompt: str, limit: int = 2000) -> tuple[str, bool]:
    """Raw Unicode intent, independently of lexical eligibility; disclose sampling."""
    if len(prompt) <= limit:
        return prompt, False
    # Retain both the opening task and the final correction. Include identifiers
    # from the middle rather than blindly truncating the first 2,000 characters.
    identifiers = re.findall(r"\b(?:[A-Z][A-Z0-9_]{3,}|[\w]+[._][\w._]+)\b", prompt[:100_000] + prompt[-100_000:])
    middle = " ".join(dict.fromkeys(identifiers))[-300:]
    head = max(0, (limit - len(middle) - 48) // 2)
    return (prompt[:head] + "\n[omitted middle; identifiers: " + middle + "]\n" + prompt[-head:])[:limit], True


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
_READ_SLOTS = threading.BoundedSemaphore(8)


def _bounded(call, deadline: float):
    """Bound optional filesystem/adapter work, including a misbehaving custom runner.

    Abandoned reads cannot mutate the returned Result. A fixed slot count bounds
    stranded daemon threads if an OS filesystem operation cannot be cancelled.
    """
    left = deadline - time.monotonic()
    if left <= 0 or not _READ_SLOTS.acquire(blocking=False):
        raise TimeoutError("recall deadline or read capacity exhausted")
    done, box = threading.Event(), {}
    def work():
        try:
            box["value"] = call()
        except BaseException as exc:
            box["error"] = exc
        finally:
            _READ_SLOTS.release()
            done.set()
    threading.Thread(target=work, daemon=True).start()
    if not done.wait(max(0, deadline - time.monotonic())):
        raise TimeoutError("recall absolute deadline exhausted")
    if "error" in box:
        raise box["error"]
    return box.get("value")


def _process(cmd, timeout: float, payload: str = "", shell: bool = False, env=None):
    """Kill the entire process group on expiry, with bounded captured output."""
    if timeout <= 0:
        raise TimeoutError("no subprocess budget remains")
    deadline = time.monotonic() + timeout
    proc = subprocess.Popen(cmd, shell=shell, stdin=subprocess.PIPE if payload else subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            start_new_session=(os.name == "posix"), env=env)
    output = bytearray()
    pending = memoryview(payload.encode())
    stderr_bytes = 0
    try:
        with selectors.DefaultSelector() as selector:
            for stream, name in ((proc.stdout, "out"), (proc.stderr, "err")):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, name)
            if proc.stdin:
                os.set_blocking(proc.stdin.fileno(), False)
                selector.register(proc.stdin, selectors.EVENT_WRITE, "in")
            while selector.get_map():
                left = deadline - time.monotonic()
                if left <= 0:
                    raise subprocess.TimeoutExpired(cmd, timeout)
                for key, _ in selector.select(left):
                    if key.data == "in":
                        try:
                            written = os.write(key.fd, pending[:16384])
                            pending = pending[written:]
                        except BrokenPipeError:
                            pending = pending[:0]
                        if not pending:
                            selector.unregister(key.fileobj)
                            key.fileobj.close()
                    else:
                        data = os.read(key.fd, 65536)
                        if not data:
                            selector.unregister(key.fileobj)
                            continue
                        if key.data == "out":
                            output.extend(data)
                        else:
                            stderr_bytes += len(data)
                        if len(output) > 4_000_000 or stderr_bytes > 256_000:
                            raise ValueError("recall adapter output exceeds bound")
            proc.wait(timeout=max(.001, deadline - time.monotonic()))
        return proc.returncode, output.decode("utf-8", "replace")
    finally:
        # Even a successful shell may have left a child running after closing
        # its pipes. This per-call session has no work beyond this response.
        if os.name == "posix":
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        elif proc.poll() is None:
            proc.kill()
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if stream and not stream.closed:
                stream.close()
        try:
            proc.wait(timeout=.2)
        except subprocess.TimeoutExpired:
            pass


def _run_qmd(cmd: list[str], timeout: float) -> Optional[list]:
    """None = qmd unavailable/failed/hung; [] = ran and found nothing."""
    try:
        returncode, output = _process(cmd, timeout)
    except Exception:
        return None
    if returncode != 0:
        # qmd prints "No results found." (exit 0) on a miss; non-zero is a failure.
        return None
    out = output.strip()
    if not out:
        return []
    try:
        data = json.loads(out)
    except ValueError:
        return None
    if isinstance(data, dict):
        data = data.get("results", [])
    return data if isinstance(data, list) else []


def server_post(settings: "Settings", path: str, body: dict, deadline: float) -> Optional[dict]:
    """POST with bounded, payload-free client diagnostics, including on failure.

    The bound is wall clock: the request runs on a daemon thread and is abandoned at the
    deadline (a socket timeout bounds inactivity, not elapsed time)."""
    import urllib.request
    import urllib.error
    started = time.monotonic()
    expires = started + max(0, deadline)
    payload = dict(body)
    payload["deadline_at_ms"] = min(payload.get("deadline_at_ms", float("inf")), int((time.time() + max(0, deadline)) * 1000))
    if not isinstance(payload.get("request_id"), str) or not re.fullmatch(r"[0-9a-f]{24}", payload["request_id"]):
        payload["request_id"] = hashlib.sha256(os.urandom(16)).hexdigest()[:24]
    def receipt(data, outcome, http_status=None):
        return {**data, "client_transport": {
            "route": path if path in ("/search", "/rerank") else "unknown",
            "request_id": payload["request_id"], "outcome": outcome,
            "http_status": http_status, "elapsed_ms": int((time.monotonic() - started) * 1000)}}
    def call():
        req = urllib.request.Request(settings.server_url + path, data=json.dumps(payload).encode(),
                                     headers={"content-type": "application/json"})
        try:
            response = urllib.request.urlopen(req, timeout=max(0.001, expires - time.monotonic()))
        except urllib.error.HTTPError as exc:
            response = exc  # preserve structured queue/deadline/index errors
        except Exception as exc:
            timeout = isinstance(exc, TimeoutError) or isinstance(getattr(exc, "reason", None), TimeoutError)
            code = "socket_timeout" if timeout else "transport_error"
            return receipt({"error": code}, code)
        with response:
            status = response.status
            try:
                raw = response.read(4_000_001)
            except Exception as exc:
                code = "socket_timeout" if isinstance(exc, TimeoutError) else "response_read_error"
                return receipt({"error": code}, code, status)
            if len(raw) > 4_000_000:
                return receipt({"error": "response_too_large"}, "response_too_large", status)
            try:
                data = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeError, RecursionError):
                return receipt({"error": "invalid_response_json"}, "invalid_response_json", status)
            if not isinstance(data, dict):
                return receipt({"error": "invalid_response_object"}, "invalid_response_object", status)
            if not 200 <= status < 300:
                # An HTTP failure cannot supply an apparently successful pool.
                data = {**data, "error": data.get("error") or "http_error"}
            failed = "error" in data
            if failed:
                known = {"adapter_unavailable", "closed", "deadline", "index_mismatch", "invalid_passage",
                         "model_cache_unavailable", "not_found", "poisoned", "queue_full", "startup",
                         "unsupported_schema", "loopback_host_required", "loopback_origin_required", "http_error"}
                error = data["error"]
                error = error if isinstance(error, str) and error in known else "server_error"
                data = {"error": error, "manifest": data.get("manifest") or {}}
            outcome = "http_error" if not 200 <= status < 300 else "server_error" if failed else "response"
            return receipt(data, outcome, status)
    try:
        return _bounded(call, expires)
    except TimeoutError:
        code = "client_deadline" if time.monotonic() >= expires else "client_read_capacity"
        return receipt({"error": code}, code)
    except Exception:
        return receipt({"error": "transport_error"}, "transport_error")


def server_alive(settings: "Settings", timeout: float = 0.4) -> bool:
    return bool(server_capabilities(settings, timeout).get("ok"))


def server_capabilities(settings: "Settings", timeout: float = 0.4) -> dict:
    """Read-only, absolute-deadline capability receipt; never writes an index."""
    import urllib.request
    expires = time.monotonic() + max(0, timeout)
    def get():
        with urllib.request.urlopen(settings.server_url + "/health", timeout=max(0.001, expires - time.monotonic())) as response:
            return json.loads(response.read(256_000).decode())
    try:
        value = _bounded(get, expires)
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _source_key(item: dict) -> tuple[str, str]:
    """Only a declared source ID AND revision establish a mirror equivalence."""
    revision = str(item.get("revision") or item.get("hash") or "")
    source = str(item.get("source_id") or "") if revision else ""
    return source or str(item.get("file") or item.get("path") or ""), revision


def fuse(lex: list[list[dict]], vec: list[dict], settings: "Settings") -> tuple[list[dict], float]:
    """Reciprocal-rank fusion of the keyword rungs and the meaning list (weight `vector_weight`),
    within configured collections. Equal titles are never identity evidence."""
    # `collections = []` means every collection. The memory collections only widen an explicit
    # list; they must never narrow "everything" down to themselves.
    cols = set(settings.collections or [])
    if cols:
        cols |= set(settings.memory_collections)
    skip = [re.compile(p) for p in settings.skip_paths]

    def keep(h: dict) -> bool:
        f = h.get("file") or ""
        c = (re.match(r"qmd://([^/]+)/", f) or [None, ""])[1]
        return bool(f) and (not cols or c in cols) and not any(p.search(f) for p in skip)
    score: dict[tuple, float] = {}
    meta: dict[tuple, dict] = {}
    for ri, rung in enumerate(lex):
        for pos, h in enumerate(x for x in rung if keep(x)):
            key = _source_key(h)
            score[key] = score.get(key, 0.0) + 1 / (60 + pos + 3 * ri)
            old = meta.get(key, {})
            meta[key] = {**h, "lexical_votes": old.get("lexical_votes", 0) + 1}
    kept_vec = [h for h in vec if keep(h)]
    for pos, h in enumerate(kept_vec):
        key = _source_key(h)
        score[key] = score.get(key, 0.0) + settings.vector_weight / (60 + pos)
        meta.setdefault(key, dict(h))
        meta[key]["vector_score"] = max(float(h.get("score") or 0), float(meta[key].get("vector_score") or 0))
    out = [meta[key] for key, _ in sorted(score.items(), key=lambda kv: -kv[1])]
    return out, max((float(h.get("score") or 0) for h in kept_vec), default=0.0)


def _server_pool(prompt: str, ladder: list[list[str]], settings: "Settings", started: float,
                 result: "Result", request_id: str = "") -> Optional[dict]:
    left = lambda: settings.budget - (time.monotonic() - started)
    question, query_partial = semantic_intent(prompt)
    cols = list(dict.fromkeys(settings.collections + settings.memory_collections)) if settings.collections else []
    got = server_post(settings, "/search", {"request_id": request_id, "rungs": [" ".join(r) for r in ladder],
                      "question": question if settings.vector else "", "collections": cols,
                      "index_id": settings.index or "default", "passage_chars": settings.snippet_chars,
                      "lex_limit": settings.search_n, "vec_limit": max(settings.search_n, settings.rerank_top)},
                      min(left(), settings.timeout))
    if got is None:
        return None
    if isinstance(got.get("client_transport"), dict):
        result.server_calls.append(got["client_transport"])
    result.manifest = got.get("manifest") or {}
    if got.get("error"):
        result.coverage = {"search": str(got["error"])[:80]}
        result.reason = f"recall server unavailable ({str(got['error'])[:80]})"
        return None
    result.coverage = {**(got.get("coverage") or {"lexical": "unknown_legacy_server"}),
                       "query": "sampled_head_tail_identifiers" if query_partial else "complete"}
    if got.get("partial") is True:
        result.coverage["partial"] = True
    if result.manifest.get("index", {}).get("id", settings.index or "default") != (settings.index or "default"):
        result.reason = "server index identity does not match request"
        return None
    fused, best_vec = fuse(got.get("lex") or [], got.get("vec") or [], settings)
    top = []
    query_terms = {x.casefold() for x in content_terms(prompt, 64)}
    for h in fused:
        text_terms = {x.casefold() for x in WORD.findall(str(h.get("title", "")) + " " + str(h.get("snippet", "")) + " " + str(h.get("scored_text", "")))}
        lexical = h.get("lexical_votes", 0) and (h.get("lexical_evidence") or bool(query_terms & text_terms))
        if not settings.vector or lexical or h.get("vector_score", 0) >= settings.vector_gate:
            top.append(h)
    if not top and fused:
        result.reason = f"no admissible lexical evidence or close meaning (best {best_vec:.2f} < {settings.vector_gate})"
    pool: dict[str, dict] = {}
    for pos, h in enumerate(top):
        coll = (re.match(r"qmd://([^/]+)/", h["file"]) or [None, ""])[1]
        pool[h["file"]] = {"item": h,
                           "votes": 1, "best": pos, "memory": coll in settings.memory_collections}
    return pool


def _collection_dirs(index: str = "") -> dict[str, Path]:
    """Collection name -> directory, read from QMD's own config."""
    name = f"{index or 'index'}.yml"
    # Explicit configuration is an isolation boundary, not a hint to fall back
    # to the user's unrelated default index.
    if os.environ.get("QMD_CONFIG_DIR"):
        candidates = [Path(os.environ["QMD_CONFIG_DIR"]) / name]
    elif os.environ.get("XDG_CONFIG_HOME"):
        candidates = [Path(os.environ["XDG_CONFIG_HOME"]) / "qmd" / name]
    else:
        candidates = [HOME / ".config" / "qmd" / name]
    dirs: dict[str, Path] = {}
    text = ""
    for cfg in candidates:
        try:
            with cfg.open(encoding="utf-8") as fh:
                text = fh.read(256_000)
            break
        except OSError:
            continue
    # QMD accepts JSON as YAML. Synthetic fixtures and generated configs use
    # that form; failing to parse it would silently lose real source anchors.
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    if isinstance(data, dict) and isinstance(data.get("collections"), dict):
        for name, value in data["collections"].items():
            if isinstance(value, dict) and isinstance(value.get("path"), str):
                dirs[str(name)] = Path(value["path"]).expanduser()
        return dirs
    for m in re.finditer(r"^  ([A-Za-z0-9_.-]+):\s*\n\s+path:\s*[\"']?([^\"'\n]+)", text, re.M):
        dirs[m.group(1)] = Path(m.group(2).strip()).expanduser()
    return dirs


def local_path(uri: str, index: str = "", *, deadline: float = float("inf"), scan_limit: int = 1000) -> Optional[Path]:
    """The file behind a qmd:// URI, if it lives inside its collection directory."""
    from urllib.parse import unquote, urlsplit
    parsed = urlsplit(uri or "")
    if parsed.query or parsed.fragment:
        return None
    m = re.match(r"qmd://([^/]+)/(.+)$", uri or "")
    if not m:
        p = Path(uri)
        if not p.is_absolute() or not p.is_file():
            return None
        for root in _collection_dirs(index).values():
            try:
                p.resolve().relative_to(root.resolve())
                return p.resolve()
            except (ValueError, OSError):
                continue
        return None
    base = _collection_dirs(index).get(m.group(1))
    if not base:
        return None
    try:
        root = base.resolve()
        cand = (base / unquote(m.group(2))).resolve()
        cand.relative_to(root)
    except (OSError, ValueError):
        return None
    if cand.is_file():
        return cand
    # QMD lower-cases and slugs paths in URIs; fall back to a case-insensitive match.
    want = unquote(m.group(2)).lower()
    try:
        for i, p in enumerate(root.rglob("*.md")):
            if i >= scan_limit or time.monotonic() >= deadline:
                return None
            if p.is_symlink():
                continue
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


def select_passage(text: str, prompt: str, limit: int = 260, *, deadline: float = float("inf")) -> dict:
    """Select a bounded matching passage with stable source offsets, never newest-by-date.

    The exact cleaned text returned here is both ranked and delivered. Anchors
    refer to the original source span, even when Markdown markers are omitted.
    """
    limit = max(40, min(int(limit), 4000))
    terms = {w.casefold() for w in content_terms(prompt, 64)}
    candidates, start, offset, current = [], 0, 0, []
    in_frontmatter = text.startswith("---\n")
    for line_no, line in enumerate(text.splitlines(keepends=True), 1):
        if time.monotonic() >= deadline:
            raise TimeoutError("passage selection deadline")
        begin = offset
        offset += len(line)
        if in_frontmatter:
            if line_no > 1 and line.strip() == "---":
                in_frontmatter = False
            continue
        if not line.strip() or line.lstrip().startswith("<!--"):
            if current:
                candidates.append((start, begin, "".join(current)))
                current = []
            continue
        if not current:
            start = begin
        current.append(line)
    if current:
        candidates.append((start, len(text), "".join(current)))
    windows = []
    stride = max(20, limit * 3 // 4)
    for begin, end, paragraph in candidates:
        for position in range(0, max(1, len(paragraph)), stride):
            if time.monotonic() >= deadline:
                raise TimeoutError("passage window deadline")
            raw = paragraph[position:position + limit]
            cleaned = clean(raw, limit)
            if len(cleaned) < 40 or FRONTMATTER_ONLY.match(cleaned):
                continue
            words = {w.casefold() for w in content_terms(raw, 128)}
            overlap = terms & words
            score = sum(1 + min(len(w), 20) / 20 for w in overlap)
            # Later *matching* evidence can contain a correction. Date/recency
            # alone never substitutes an unrelated passage.
            windows.append((score, begin + position, cleaned, min(end, begin + position + limit)))
    if not windows:
        return {}
    score, begin, excerpt, end = max(windows, key=lambda row: (row[0], row[1]))
    return {"text": excerpt, "anchor": {"start_char": begin, "end_char": end,
            "start_line": text.count("\n", 0, begin) + 1, "end_line": text.count("\n", 0, end) + 1},
            "lexical_overlap": score > 0, "lexical_score": score}


def _passage_identity(source_id: str, revision: str, anchor: dict, text: str) -> str:
    return hashlib.sha256(json.dumps([source_id, revision, anchor, text], sort_keys=True, ensure_ascii=False).encode()).hexdigest()


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
    score: float = 0.0             # the engine's own score (QMD), for fitting a floor offline
    source_id: str = ""
    revision: str = ""
    revision_kind: str = ""
    anchor: dict = field(default_factory=dict)
    passage_id: str = ""
    scored_text: str = ""
    packet: dict = field(default_factory=dict)

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
    engine: str = "cli"
    attempted_engines: list[str] = field(default_factory=list)
    server_calls: list[dict] = field(default_factory=list)
    reranked: bool = False
    coverage: dict = field(default_factory=dict)
    manifest: dict = field(default_factory=dict)
    deadline_exhausted: bool = False
    evidence_tokens: int = 0
    context_tokens: int = 0
    token_count_method: str = "utf8_bytes_upper_bound"

    def to_dict(self) -> dict:
        return {"status": self.status, "reason": self.reason, "query": " ".join(self.query),
                "narrowed": " ".join(self.narrowed), "pool": self.pool, "ms": self.ms,
                "hits": [h.to_dict() for h in self.hits], "engine": self.engine,
                "attempted_engines": self.attempted_engines, "server_calls": self.server_calls,
                "candidates": self.candidates, "reranked": self.reranked,
                "coverage": self.coverage, "manifest": self.manifest,
                "deadline_exhausted": self.deadline_exhausted,
                "evidence_tokens": self.evidence_tokens, "context_tokens": self.context_tokens,
                "token_count_method": self.token_count_method}


def _rerank(prompt: str, hits: list[Hit], settings: Settings, deadline: float = float("inf"), result=None) -> Optional[list[Hit]]:
    """Optional external judge: JSON on stdin -> {"keep": [ids, best first]}.

    None means "judge unavailable, keep pool order": a judge must never be able to
    break recall. [] means the judge looked and nothing was relevant.
    """
    if not settings.rerank_command or not hits:
        return None
    if any(hit.packet.get("source_private") for hit in hits):
        if result is not None:
            result.coverage["external_judge"] = "private_source_denied"
        return None
    if any("model" not in claim.get("audiences", ["host"]) for hit in hits
           for claim in hit.packet.get("applicability", [])):
        if result is not None:
            result.coverage["external_judge"] = "claim_audience_denied"
        return None
    payload = json.dumps({"request": semantic_intent(prompt)[0], "candidates": [
        {"id": f"c{i}", "title": h.title[:160], "excerpt": h.scored_text or h.snippet, "uri": h.uri,
         "passage_id": h.passage_id, "revision": h.revision, "anchor": h.anchor}
        for i, h in enumerate(hits)]})
    try:
        from .egress import prepare, model_env, EgressDenied
        prepared = _bounded(lambda: prepare(payload, audience="model", sources=[h.path or h.uri for h in hits]), deadline)
        if prepared != payload:
            if result is not None:
                result.coverage["external_judge"] = "redacted_payload_requires_separate_passage"
            return None
        code, output = _process(settings.rerank_command,
                                min(settings.rerank_timeout, max(0, deadline - time.monotonic())), payload, shell=True,
                                env=model_env())
        keep = json.loads(output).get("keep") if code == 0 else None
    except EgressDenied:
        if result is not None:
            result.coverage["external_judge"] = "source_policy_denied"
        return None
    except Exception:
        return None
    if not isinstance(keep, list):
        return None
    by_id = {f"c{i}": h for i, h in enumerate(hits)}
    return [by_id[k] for k in dict.fromkeys(k for k in keep if isinstance(k, str)) if k in by_id]


def recall(prompt: str, settings: Optional[Settings] = None, runner: Runner = _run_qmd,
           *, prefer_types: tuple[str, ...] = (), extra_rungs: Optional[list[list[str]]] = None,
           _started: Optional[float] = None) -> Result:
    """Search the index for prior knowledge about `prompt`. Never raises."""
    started = time.monotonic() if _started is None else _started
    result = Result(status="skipped")
    try:
        settings = settings or _bounded(Settings.load, started + 8.0)
        _recall(prompt, settings, runner, result, started, prefer_types, extra_rungs or [])
    except TimeoutError:
        result.deadline_exhausted = True
        result.coverage["deadline"] = "exhausted"
        if not result.hits:
            result.status, result.reason = "unavailable", "recall absolute deadline exhausted"
    except Exception as exc:  # noqa: BLE001 - recall fails open
        result.status, result.reason = "unavailable", f"recall error: {type(exc).__name__}"
    result.ms = int((time.monotonic() - started) * 1000)
    return result


def _claim_view(text: str, collection: str, settings: Settings, deadline: float) -> tuple[str, dict]:
    """Claims require detached authority; authored status never grants trust."""
    marker_pattern = re.compile(r"(?m)^<!-- claude-note:claim (\{[^\n]*\}) -->[^\S\n]*$")
    markers = marker_pattern.findall(text)
    if not markers:
        return text, {}
    try:
        from .verified_claims import applicability
        roots = _bounded(lambda: _collection_dirs(settings.index), deadline)
        root = roots.get(collection)
        if not root:
            raise ValueError("claim collection root unavailable")
        view = _bounded(lambda: applicability(text, vault_root=root, repository=settings.repository,
                         environment=settings.environment, audiences=tuple(settings.audiences)), deadline)
    except TimeoutError:
        raise
    except Exception:
        # Preserve legacy prose, but remove machine claims we cannot validate.
        claims = []
        for raw in markers:
            try:
                claims.append(json.loads(raw))
            except ValueError:
                pass
        view = {"accepted": [], "observed": [], "proposed": [],
                "denied": [{**c, "reason_codes": ["claim_validation_unavailable"]} for c in claims]}
    protected = {str(c.get("text") or "") for c in view.get("accepted", [])}
    # Preserve character/line coordinates while excluding machine metadata from
    # ordinary prose. Applicable claims are rendered separately from parsed JSON.
    blank = lambda value: "".join("\n" if c == "\n" else " " for c in value)
    filtered = marker_pattern.sub(lambda match: blank(match[0]), text)
    for claim in view.get("denied", []):
        value = str(claim.get("text") or "")
        if value and value not in protected:
            filtered = filtered.replace(value, blank(value))
    return filtered, view


def _claim_passages(text: str, view: dict, prompt: str, limit: int, deadline: float) -> list[dict]:
    from .verified_claims import Claim
    records = {}
    for kind in ("accepted", "observed", "proposed"):
        for record in view.get(kind, []):
            try:
                claim = Claim(**{key: record[key] for key in Claim.__dataclass_fields__ if key in record}).validate()
                records[(claim.claim_id, claim.digest())] = record
            except (ValueError, TypeError):
                continue
    out = []
    for match in re.finditer(r"(?m)^<!-- claude-note:claim (\{[^\n]*\}) -->[^\S\n]*$", text):
        if time.monotonic() >= deadline:
            raise TimeoutError("claim passage deadline")
        try:
            claim = Claim(**json.loads(match[1])).validate()
            record = records.get((claim.claim_id, claim.digest()))
        except (ValueError, TypeError):
            continue
        if not record:
            continue
        selected = select_passage(record["text"], prompt, limit, deadline=deadline)
        if selected:
            selected["anchor"] = {"kind": "claim_json", "claim_id": record["claim_id"],
                                  "start_char": match.start(), "end_char": match.end(),
                                  "start_line": text.count("\n", 0, match.start()) + 1,
                                  "end_line": text.count("\n", 0, match.end()) + 1,
                                  "claim_text_start_char": selected["anchor"]["start_char"],
                                  "claim_text_end_char": selected["anchor"]["end_char"]}
            selected["claim_record"] = record
            out.append(selected)
    return out


def _prepare_hit(item: dict, c: dict, prompt: str, settings: Settings, deadline: float) -> Optional[Hit]:
    uri = item.get("file") or item.get("path")
    if not isinstance(uri, str) or len(uri.encode()) > 4096:
        return None
    collection = (re.match(r"qmd://([^/]+)/", uri) or [None, ""])[1]
    cols = set(settings.collections) | set(settings.memory_collections)
    if settings.collections and collection not in cols:
        return None
    hit = Hit(uri=uri, title=str(item.get("title") or Path(uri).stem)[:160], snippet="", collection=collection,
              memory=c["memory"], votes=c["votes"], score=float(item.get("score") or 0.0),
              source_id=str(item.get("source_id") or uri), revision=str(item.get("revision") or ""),
              revision_kind=str(item.get("revision_kind") or "qmd_content_sha256"))
    passage = {"text": item.get("scored_text") or clean(item.get("snippet", ""), settings.snippet_chars),
               "anchor": item.get("anchor") or {}}
    path = _bounded(lambda: local_path(uri, settings.index, deadline=deadline,
                                      scan_limit=settings.resolve_scan_limit), deadline)
    claims, source_private, stale_index = {}, False, False
    if path:
        def read():
            with path.open("rb") as fh:
                return fh.read(settings.source_bytes + 1)
        raw = _bounded(read, deadline)
        if len(raw) > settings.source_bytes:
            return None  # partial source cannot impersonate a complete revision
        text = raw.decode("utf-8", "replace")
        hit.path = str(path)
        hit.revision = hashlib.sha256(raw).hexdigest()
        indexed_revision = str(item.get("revision") or "")
        stale_index = bool(indexed_revision and item.get("revision_kind") in {"source_file_sha256", "indexed_body_sha256"}
                           and indexed_revision != hit.revision)
        hit.revision_kind = "source_file_sha256"
        hit.type = note_type(text)
        from .egress import external_allowed
        source_private = not external_allowed(text)
        filtered, claims = _claim_view(text, collection, settings, deadline)
        limit = min(settings.snippet_chars, settings.rerank_chars)
        ordinary = select_passage(filtered, prompt, limit, deadline=deadline)
        passages = ([ordinary] if ordinary else []) + _claim_passages(text, claims, prompt, limit, deadline)
        passage = max(passages, key=lambda p: (p.get("lexical_score", 0), p["anchor"].get("start_char", 0)), default={})
        if claims and not passage.get("lexical_overlap") and float(item.get("vector_score") or 0) < settings.vector_gate:
            return None  # JSON status/scope/provenance alone is not lexical evidence
        if stale_index and not passage.get("lexical_overlap"):
            return None  # an old vector score cannot admit an unrelated new version
    elif item.get("has_machine_claims"):
        return None  # applicability needs the actual source and detached receipts
    elif not hit.revision:
        # Legacy adapters offer only a snippet; retain its limitation explicitly.
        hit.revision = hashlib.sha256(str(passage["text"]).encode()).hexdigest()
        hit.revision_kind = "snippet_sha256"
    if hit.type == "session" or len(passage.get("text", "")) < 40 or FRONTMATTER_ONLY.match(passage.get("text", "")):
        return None
    hit.snippet = hit.scored_text = str(passage["text"])
    allowed_anchor = {"start_char", "end_char", "start_line", "end_line", "claim_text_start_char", "claim_text_end_char", "kind", "claim_id"}
    hit.anchor = {key: value for key, value in (passage.get("anchor") or {}).items() if key in allowed_anchor and
                  ((isinstance(value, int) and 0 <= value <= settings.source_bytes) or (isinstance(value, str) and len(value) <= 128))}
    hit.anchor = hit.anchor or {"kind": "adapter_excerpt_unanchored"}
    hit.passage_id = _passage_identity(hit.source_id, hit.revision, hit.anchor, hit.scored_text)
    selected_claims = [passage["claim_record"]] if passage.get("claim_record") else [c for kind in ("accepted", "observed", "proposed") for c in claims.get(kind, [])
                       if c.get("text") and str(c["text"]) in hit.scored_text]
    hit.packet = {"claim": hit.scored_text, "verification": "unverified", "scope": {"collection": collection},
                  "source_private": source_private,
                  "index_version_changed": stale_index,
                  "source": {"id": hit.source_id, "revision": hit.revision, "revision_kind": hit.revision_kind,
                             "anchor": hit.anchor}, "applicability": selected_claims,
                  "denied_claims": [{"claim_id": x.get("claim_id"), "reason_codes": x.get("reason_codes", [])}
                                    for x in claims.get("denied", [])]}
    if passage.get("claim_record") in claims.get("accepted", []):
        hit.packet["verification"] = "receipt_verified_claim"
    # Packets include detached authority/provenance, but cannot smuggle an
    # unbounded source span or JSON metadata blob around the context budget.
    if len(json.dumps(hit.packet, ensure_ascii=False).encode()) > max(0, settings.packet_bytes):
        hit.packet["applicability"] = [{**{key: record.get(key) for key in
            ("claim_id", "verification", "status", "valid_from", "valid_until", "source_revision")},
            "scope": {key: str(value)[:160] for key, value in record.get("scope", {}).items()
                      if key in {"repository", "environment"}},
            "audiences": [str(value)[:40] for value in record.get("audiences", [])[:8]]}
            for record in selected_claims[:1]]
        hit.packet["denied_claims"] = hit.packet["denied_claims"][:8]
        hit.packet["metadata_partial"] = True
        if len(json.dumps(hit.packet, ensure_ascii=False).encode()) > max(0, settings.packet_bytes):
            return None
    return hit


def _rank_passages(prompt: str, candidates: list[Hit], settings: Settings, result: Result, deadline: float) -> list[Hit]:
    if result.engine != "server" or not settings.rerank or len(candidates) < 2:
        return candidates
    if deadline - time.monotonic() <= 0.05:
        result.coverage["rerank"] = "deadline_skipped"
        return candidates
    top, tail = candidates[:settings.rerank_top], candidates[settings.rerank_top:]
    body = {"question": semantic_intent(prompt)[0], "index_id": settings.index or "default",
            "collections": list(dict.fromkeys(settings.collections + settings.memory_collections)) if settings.collections else [],
            "passages": [{"file": h.uri, "source_id": h.source_id, "revision": h.revision,
                          "passage_id": h.passage_id, "anchor": h.anchor, "title": h.title,
                          "text": h.scored_text} for h in top]}
    rr = _server_attempt(result, "/rerank", deadline, lambda request_id: server_post(
        settings, "/rerank", {**body, "request_id": request_id}, max(0, deadline - time.monotonic())))
    result.reranked = False
    response = rr if isinstance(rr, dict) else {}
    if isinstance(response.get("client_transport"), dict):
        result.server_calls[-1] = response["client_transport"]
    status, coverage = response.get("rerank_status"), response.get("coverage")
    if response.get("error"):
        result.coverage["rerank"] = "unavailable_or_unverified_passage"
        return candidates
    if status == "sdk_fallback" or response.get("model") == "fallback" or coverage == "sdk_fallback_no_rerank":
        result.coverage["rerank"] = "sdk_fallback_no_rerank"
        return candidates
    degraded = {"unverified_model": "unverified_model", "partial_scores": "partial_passage_scores",
                "invalid_scores": "invalid_scores", "no_valid_scores": "no_valid_scores",
                "model_cache_unavailable": "model_cache_unavailable", "not_requested": "no_passages"}
    if status != "scored" or coverage != "exact_passage":
        result.coverage["rerank"] = (degraded[status] if isinstance(status, str) and status in degraded and
                                     degraded[status] == coverage else "unverified_rerank_execution")
        return candidates

    def model_metadata(manifest):
        models = manifest.get("models") if isinstance(manifest, dict) else None
        metadata = models.get("rerank") if isinstance(models, dict) else None
        return metadata if isinstance(metadata, dict) else {}

    def valid_model(value):
        if (not isinstance(value, str) or not value or len(value) > 1024 or value.strip() != value or
                value in ("fallback", "unknown") or any(ord(char) < 32 or ord(char) == 127 for char in value)):
            return False
        try:
            return len(value.encode("utf-8")) <= 1024
        except UnicodeError:
            return False

    manifest = response.get("manifest")
    metadata = model_metadata(manifest)
    model, expected = response.get("model"), metadata.get("id")
    previous = model_metadata(result.manifest).get("id")
    if (not valid_model(model) or not valid_model(expected) or model != expected or
            (previous is not None and (not valid_model(previous) or previous != expected)) or
            manifest.get("rerank_ready") is not True or metadata.get("ready") is not True):
        result.coverage["rerank"] = "unverified_model"
        return candidates
    # A score map alone is insufficient: old SDKs can produce finite placeholder
    # scores without running their configured ranker. Require a complete positive
    # model-bound observation and exact IDs before changing the fusion order.
    scores, confirmed = response.get("passage_scores"), response.get("scored_passage_ids")
    ids = [h.passage_id for h in top]
    if (not all(isinstance(value, str) and value for value in ids) or len(set(ids)) != len(ids) or
            not isinstance(scores, dict) or set(scores) != set(ids) or
            not isinstance(confirmed, list) or not all(isinstance(value, str) for value in confirmed) or
            len(confirmed) != len(ids) or set(confirmed) != set(ids)):
        result.coverage["rerank"] = "invalid_scores"
        return candidates
    numeric = {}
    try:
        for identity, value in scores.items():
            if type(value) not in (int, float) or not math.isfinite(value):
                raise ValueError("Invalid passage score")
            numeric[identity] = float(value)
    except (ValueError, TypeError, OverflowError):
        result.coverage["rerank"] = "invalid_scores"
        return candidates
    positions = {h.passage_id: i for i, h in enumerate(top)}
    top.sort(key=lambda h: -(numeric[h.passage_id] + settings.rerank_blend / (1 + positions[h.passage_id])))
    result.reranked = True
    result.coverage["rerank"] = "exact_passage"
    return top + tail


def _server_attempt(result: Result, route: str, deadline: float, call):
    """The caller owns this receipt; an abandoned adapter cannot mutate it."""
    started = time.monotonic()
    request_id = hashlib.sha256(os.urandom(16)).hexdigest()[:24]
    entry = {"route": route, "request_id": request_id, "outcome": "adapter_returned_without_receipt",
             "http_status": None, "elapsed_ms": 0}
    result.server_calls.append(entry)
    try:
        return _bounded(lambda: call(request_id), deadline)
    except TimeoutError:
        entry["outcome"] = "recall_deadline" if time.monotonic() >= deadline else "recall_read_capacity"
        raise
    except Exception:
        entry["outcome"] = "adapter_error"
        raise
    finally:
        entry["elapsed_ms"] = int((time.monotonic() - started) * 1000)


def _pool_keyword_rows(pool, rows, rung_i, is_memory, limit):
    for pos, item in enumerate(rows[:max(1, limit)]):
        if not isinstance(item, dict):
            continue
        uri = item.get("file") or item.get("path")
        if not isinstance(uri, str) or not uri:
            continue
        candidate = pool.setdefault(uri, {"item": item, "votes": 0, "best": 1e9, "memory": False})
        candidate["votes"] += 1
        candidate["best"] = min(candidate["best"], pos + 3 * rung_i)
        candidate["memory"] |= is_memory


def _recall(prompt, settings, runner, result, started, prefer_types, extra_rungs):
    deadline = started + max(0, settings.budget)
    if not settings.enabled:
        result.reason = "recall disabled"
        return
    prompt = prompt or ""
    raw_partial = len(prompt) > 200_000
    if raw_partial:
        prompt = prompt[:100_000] + "\n[omitted source middle]\n" + prompt[-100_000:]
    prompt = prompt.strip()
    if AUTOMATED_EVENT.search(prompt):
        result.reason = "automated event"
        return
    prompt = WRAPPER_TAG.sub(" ", prompt).strip()
    if not prompt or re.match(r"^/[A-Za-z][\w-]*(?:\s|$)", prompt):
        result.reason = "prompt too short or a slash command"
        return
    if not extra_rungs and (UNRESOLVED_FOLLOWUP.fullmatch(prompt) or
                            SUBJECT_FREE_ACKNOWLEDGEMENT.fullmatch(prompt)):
        result.reason = "follow-up has no retrieval subject; use the preceding conversation"
        return
    # Bound lexical CPU as well as semantic input. Huge wrappers cannot consume
    # the entire hook budget before the first deadline check.
    terms = _bounded(lambda: content_terms(prompt, max(1, settings.max_terms)), deadline)
    meaningful = any(w.casefold() not in STOPWORDS and not DATEISH.match(w)
                     for w in WORD.findall(prompt))
    if not terms and not extra_rungs and not meaningful:
        result.reason = "no lexical or semantic intent"
        return
    first = (WORD.findall(prompt) or [""])[0]
    ladder = query_ladder(terms, first)
    ladder += [r for r in extra_rungs if r and r not in ladder]
    result.query = ladder[0] if ladder else ["[semantic-only]"]
    result.narrowed = ladder[-1] if ladder else []
    result.coverage = {"local_resolution": "bounded", "lexical": "not_run"}
    if raw_partial:
        result.coverage["raw_prompt"] = "sampled_head_tail"
    pool, ran, server_failed, server_partial, cli_partial = {}, False, False, False, False
    # Search results still need local source resolution, revision checks and
    # passage selection. Reserve that work before either adapter starts. In
    # auto mode a slow warm server also has to leave time for a real CLI search.
    # These are slices of the existing absolute budget, never extra timeouts.
    left = max(0, deadline - time.monotonic())
    source_reserve = min(1.0, left * .2)
    search_deadline = deadline - source_reserve
    cli_reserve = min(2.0, left * .3) if settings.engine == "auto" else 0
    server_deadline = search_deadline - cli_reserve
    use_server = settings.engine == "server" or (settings.engine == "auto" and not settings.index)
    if use_server and not extra_rungs:
        result.engine = "server"
        result.attempted_engines.append("server")
        server_result = Result(status="skipped")
        # Leave a small scheduling margin between the transport's timeout and
        # its outer guard so the transport can retain its failure receipt.
        server_settings = replace(settings, timeout=min(settings.timeout,
            max(0, server_deadline - time.monotonic() - min(.01, left * .01))))
        try:
            got = _server_attempt(result, "/search", server_deadline, lambda request_id: _server_pool(
                prompt, ladder, server_settings, started, server_result, request_id))
        except TimeoutError:
            got = None
            server_result.reason = "recall server search deadline exhausted"
            server_result.coverage = {"search": "partial_server_deadline"}
            if settings.engine == "server":
                result.deadline_exhausted = True
        result.manifest, result.coverage = server_result.manifest, server_result.coverage or result.coverage
        if server_result.server_calls:
            result.server_calls[-1] = server_result.server_calls[0]
        if raw_partial:
            result.coverage["raw_prompt"] = "sampled_head_tail"
        result.reason = server_result.reason
        if got is not None:
            pool, ran, result.engine = got, True, "server"
            server_partial = server_result.coverage.get("partial") is True
        elif settings.engine == "server":
            result.status, result.reason = "unavailable", result.reason or "the recall server did not answer"
            return
        else:
            server_failed = True
    pairs = complementary_pairs(ladder)
    cli_rungs = [*ladder, *pairs]
    if settings.single_term_fallback and ladder:
        cli_rungs += [[word] for word in ladder[-1][:2] if [word] not in cli_rungs]
    scheduled_rungs = list(enumerate(cli_rungs))
    if "server" in result.attempted_engines and len(ladder) > 1:
        narrow = len(ladder) - 1
        preferred = [narrow, *range(len(ladder), len(ladder) + len(pairs))]
        scheduled_rungs = [*(scheduled_rungs[i] for i in preferred),
                           *(entry for entry in scheduled_rungs if entry[0] not in preferred)]
    if not ran and ladder and settings.engine == "auto" and runner is _run_qmd:
        # A cold vector scan can spend the server slice and restart its owner.
        # Read the same keyword index in one bounded, read-only connection;
        # spawning a QMD CLI for every rung can exhaust the remaining slice.
        from .recall_lexical import search_batch
        result.engine = "sqlite"
        result.attempted_engines.append("sqlite")
        sqlite_deadline = min(search_deadline - .03, time.monotonic() + 2.0)
        try:
            batch = _bounded(lambda: search_batch(
                [(i, " ".join(rung)) for i, rung in scheduled_rungs], settings.collections,
                settings.memory_collections, settings.index, sqlite_deadline,
                settings.search_n), min(search_deadline, sqlite_deadline + .02))
        except Exception:
            batch = None
        if batch is not None:
            ran = True
            cli_partial = not batch["complete"]
            result.reason = ""
            if not cli_partial:
                lexical_coverage = "complete_sqlite_scoped_before_limit"
            elif batch.get("reason") == "deadline":
                lexical_coverage = "partial_sqlite_deadline"
            else:
                lexical_coverage = "partial_sqlite_failure"
            result.coverage.update(lexical=lexical_coverage,
                                   semantic="unavailable_sqlite", partial=server_failed or cli_partial)
            for _scope, is_memory, rung_i, rows in batch["calls"]:
                _pool_keyword_rows(pool, rows, rung_i, is_memory, settings.search_n)
    if not ran:
        exe = _bounded(lambda: qmd_executable(settings), deadline)
        if not exe:
            result.status, result.reason = "unavailable", "qmd is not installed"
            return
        base = [exe] + (["--index", settings.index] if settings.index else [])
        scopes = [(c, c in settings.memory_collections) for c in dict.fromkeys(settings.collections + settings.memory_collections)] if settings.collections else [(None, False)] + [(c, True) for c in settings.memory_collections]
        result.coverage.update(lexical="partial_scoped_cli_sdk_overfetch" if settings.collections or settings.memory_collections
                               else "cli_topk", semantic="unavailable_cli")
        if server_failed:
            result.coverage["partial"] = True
        # A transport failure is the reason for fallback, not the outcome of a
        # completed keyword search. Preserve its receipt/coverage separately.
        result.reason = ""
        calls = [(scope, is_memory, rung_i, rung) for scope, is_memory in scopes
                 for rung_i, rung in scheduled_rungs]
        attempt_cap = max(0, search_deadline - time.monotonic()) / min(2, max(1, len(calls)))
        for scope, is_memory, rung_i, rung in calls:
            left = search_deadline - time.monotonic()
            if left <= 0:
                result.deadline_exhausted = True
                cli_partial = True
                result.coverage.update(partial=True, lexical="partial_cli_deadline")
                break
            cmd = base + ["search", " ".join(rung), "-n", str(settings.search_n), "--json"]
            if scope:
                cmd += ["-c", scope]
            result.engine = "cli"
            if "cli" not in result.attempted_engines:
                result.attempted_engines.append("cli")
            # One hung rung must not consume the entire search slice. The
            # absolute per-attempt guard also bounds custom runners that
            # ignore the timeout argument.
            timeout = min(settings.timeout, left, attempt_cap)
            call_deadline = min(search_deadline, time.monotonic() + timeout)
            try:
                got = _bounded(lambda cmd=cmd, timeout=timeout: runner(cmd, timeout), call_deadline)
            except TimeoutError:
                got = None
                result.deadline_exhausted = True
            if got is None:
                cli_partial = True
                result.coverage.update(partial=True, lexical="partial_cli_failure")
                continue
            ran = True
            _pool_keyword_rows(pool, got, rung_i, is_memory, settings.search_n)
    if not ran:
        result.status, result.reason = "unavailable", "qmd failed, hung, or its index was locked"
        return
    result.pool = len(pool)
    if not pool:
        if cli_partial:
            result.status, result.reason = "unavailable", ("SQLite keyword" if result.engine == "sqlite" else "CLI") + " search coverage incomplete; no verified match returned"
        elif server_partial:
            result.status, result.reason = "unavailable", "recall server search coverage incomplete; no verified match returned"
        elif server_failed:
            result.status, result.reason = "unavailable", "no keyword match; semantic search unavailable"
        else:
            result.status, result.reason = "miss", result.reason or "no keyword or semantic match"
        return
    skip = [re.compile(p) for p in settings.skip_paths]
    candidates = []
    for c in sorted(pool.values(), key=lambda x: (-x["votes"], x["best"])):
        item = c["item"]
        uri = item.get("file") or item.get("path")
        if not isinstance(uri, str) or any(p.search(uri) for p in skip):
            continue
        try:
            hit = _prepare_hit(item, c, prompt, settings, deadline)
        except TimeoutError:
            result.deadline_exhausted = True
            result.coverage.update(partial=True, local_resolution="partial_deadline")
            break
        if hit:
            candidates.append(hit)
        if len(candidates) >= 16:
            break
    if prefer_types:
        candidates.sort(key=lambda h: h.type not in prefer_types)
    result.candidates = [h.path or h.uri for h in candidates]
    try:
        candidates = _rank_passages(prompt, candidates, settings, result, deadline)
        # The external judge sees these same anchored texts, not another prefix.
        judge_result = Result(status="skipped")
        judged = _bounded(lambda: _rerank(prompt, candidates, settings, deadline, judge_result), deadline) if settings.rerank_command else None
        result.coverage.update(judge_result.coverage)
        if judged is not None:
            candidates = judged
    except TimeoutError:
        result.deadline_exhausted = True
    if not candidates:
        if result.coverage.get("local_resolution") == "partial_deadline":
            result.status, result.reason = "unavailable", "source verification deadline exhausted"
        elif cli_partial:
            result.status, result.reason = "unavailable", "search coverage incomplete; no eligible source verified"
        elif server_partial:
            result.status, result.reason = "unavailable", "search coverage incomplete; no eligible source verified"
        elif server_failed:
            result.status, result.reason = "unavailable", "no eligible keyword evidence; semantic search unavailable"
        else:
            result.status, result.reason = "miss", "no eligible evidence passages"
        return
    result.candidates = [h.path or h.uri for h in candidates]
    memories = [h for h in candidates if h.memory][:max(0, settings.memory_slots)]
    ordered = memories + [h for h in candidates if h not in memories]
    result.status, result.hits = "hit", []
    for hit in ordered:
        if len(result.hits) >= max(0, settings.inject):
            break
        result.hits.append(hit)
        # A UTF-8 byte count is a conservative token upper bound, including the
        # rendered instructions, source anchors and evidence labels.
        if len(format_context(result).encode()) > max(0, settings.context_token_budget):
            result.hits.pop()
            result.coverage["context"] = "token_budget_limited"
    if not result.hits:
        result.status, result.reason = "miss", "evidence exceeds configured context token budget"
    result.evidence_tokens = sum(len(h.scored_text.encode()) for h in result.hits)
    result.context_tokens = len(format_context(result).encode()) if result.hits else 0


# --------------------------------------------------------------------------- #
# Presentation
# --------------------------------------------------------------------------- #


def format_context(result: Result) -> str:
    if result.status == "hit":
        lines = [
            "claude-note recall: source-linked evidence; legacy prose and proposals are UNVERIFIED leads.",
            "Open a note before relying on it (read the file shown), check that it still applies, then act.",
            "If none fit, this lookup missed; it does not mean nothing is known. Search yourself with",
            "other words (`claude-note recall \"<content words>\"` or `qmd vsearch \"<question>\"`).",
            "",
        ]
        for h in result.hits:
            meta = ", ".join(x for x in (h.type, "memory" if h.memory else "") if x)
            lines.append(f"- {h.title}" + (f" ({meta})" if meta else "") + f"  [{h.path or h.uri}]")
            anchor = f"lines {h.anchor.get('start_line')}–{h.anchor.get('end_line')}" if h.anchor.get("start_line") else "unanchored adapter excerpt"
            lines.append(f"    Evidence: {h.packet.get('verification', 'unverified')}; {anchor}; revision {h.revision[:16]} ({h.revision_kind}).")
            lines.append(f"    {(h.fresh_label + ' ') if h.fresh_label else ''}{h.snippet}")
        if result.coverage.get("partial") or any("partial" in str(v) for v in result.coverage.values()):
            lines.append("Coverage is partial; missing candidates are not evidence that nothing is known.")
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
        out.append(f"   Evidence: {h.packet.get('verification', 'unverified')}; revision {h.revision[:16]} ({h.revision_kind or 'unknown'})")
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
               "engine": result.engine, "attempted_engines": result.attempted_engines,
               "server_calls": result.server_calls, "reranked": result.reranked,
               "deadline_exhausted": result.deadline_exhausted,
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
    started = time.monotonic()
    try:
        settings = _bounded(Settings.load, started + 8.0)
    except Exception:
        emit(event, format_context(Result(status="unavailable", reason="configuration unavailable within hook deadline")))
        return 0
    deadline = started + settings.budget
    result = recall(payload.get("prompt") or "", settings, _started=started)
    if settings.log and result.status != "skipped":
        # Session and file paths let `eval live` check whether the agent opened what it was shown.
        try:
            _bounded(lambda: log_event("prompt", result, {"session": payload.get("session_id") or "",
                     "transcript": payload.get("transcript_path") or "", "paths": [h.path for h in result.hits if h.path]}), deadline)
        except TimeoutError:
            pass
    try:
        warning = _bounded(lambda: memory_md_warning(payload.get("cwd") or ""), deadline)
    except TimeoutError:
        warning = ""
    if len((warning + format_context(result)).encode()) > settings.context_token_budget:
        warning = ""
    emit(event, warning + format_context(result))
    return 0
