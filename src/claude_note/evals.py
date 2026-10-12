"""Evals: is recall putting the right notes in front of the agent, and does the agent use them?

Three layers, cheapest first:

* retrieval: labelled questions -> the note that answers them. Measures whether that note is
  among the notes recall injects (hit@k), how high (MRR), how often recall finds nothing, and
  how often it injects notes for a question the vault cannot answer. Every run is compared with
  the free alternative: one keyword search of the raw prompt, with no ladder.
* live: the recall log plus Claude Code transcripts. Miss rate and latency of the real hook,
  and whether the agent opened a note recall injected: the only signal that a note was useful,
  and it needs no judge.
* answers (opt-in, uses the Claude CLI): the same question with and without recall; a blind
  judge says which answer follows the team's note, and a control pair checks the judge.

Labels never come from recall itself. A probe's expected note is the note it was written from;
the fixture set is hand-written. Probes are split into dev and test by note, so a setting tuned
on dev is reported on test. Every run appends to `evals/runs.jsonl`; `eval dashboard` draws them.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from . import recall as recall_mod

HOME = Path.home()
EVAL_DIR = HOME / ".local" / "share" / "claude-note" / "evals"
RUNS = EVAL_DIR / "runs.jsonl"
PROBES = EVAL_DIR / "probes.jsonl"
LAST_RUN = EVAL_DIR / "last-run.json"
FIXTURE = Path(__file__).parent / "eval_fixtures" / "demo.jsonl"

PROBE_TYPES = ("gotcha", "pattern", "decision", "reference", "feedback", "project")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _split(key: str) -> str:
    """Deterministic dev/test split by the grouping unit (the expected note), never by row."""
    return "test" if int(hashlib.sha1(key.encode()).hexdigest(), 16) % 2 else "dev"


# --------------------------------------------------------------------------- #
# Eval cases
# --------------------------------------------------------------------------- #


@dataclass
class Case:
    id: str
    question: str
    expect: list[str] = field(default_factory=list)   # note paths/stems that answer it; [] = off-topic
    split: str = "test"
    source: str = "fixture"
    note_type: str = ""
    overlap: float = 0.0           # share of the question's content words found in the note's title
    note_title: str = ""           # probes: a hit with this exact title is the same note (pushed copies)

    @classmethod
    def from_dict(cls, d: dict) -> "Case":
        return cls(id=str(d.get("id") or hashlib.sha1(d["question"].encode()).hexdigest()[:8]),
                   question=d["question"], expect=list(d.get("expect") or []),
                   split=d.get("split") or _split((d.get("expect") or [d["question"]])[0]),
                   source=d.get("source", "fixture"), note_type=d.get("note_type", ""),
                   overlap=float(d.get("overlap", 0.0)), note_title=d.get("note_title", ""))


def load_cases(path: Path) -> list[Case]:
    cases = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            cases.append(Case.from_dict(json.loads(line)))
    return cases


def _norm(s: str) -> str:
    s = re.sub(r"\.md$", "", (s or "").strip().lower())
    return re.sub(r"[^a-z0-9/]+", "-", s).strip("-")


def matches(hit_ref: str, expect: list[str]) -> bool:
    """A hit answers the case when its path ends with an expected path (or stem)."""
    ref = _norm(hit_ref)
    for e in expect:
        e = _norm(e)
        if e and (ref == e or ref.endswith("/" + e) or ref.rsplit("/", 1)[-1] == e.rsplit("/", 1)[-1]):
            return True
    return False


# --------------------------------------------------------------------------- #
# Retrieval eval
# --------------------------------------------------------------------------- #


def _title_key(t: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (t or "").lower())


def _rank(refs: list[str], expect: list[str], titles: Optional[list[str]] = None, note_title: str = "") -> Optional[int]:
    want = _title_key(note_title)
    for i, ref in enumerate(refs, 1):
        if matches(ref, expect) or (want and titles and _title_key(titles[i - 1]) == want):
            return i
    return None


def baseline(question: str, settings: recall_mod.Settings, runner: recall_mod.Runner) -> list[str]:
    """The free alternative: one keyword search of the whole prompt, no ladder, no pooling."""
    exe = recall_mod.qmd_executable(settings)
    if not exe:
        return []
    base = [exe] + (["--index", settings.index] if settings.index else [])
    got = runner(base + ["search", question, "-n", str(settings.search_n), "--json"], settings.timeout) or []
    skip = [re.compile(p) for p in settings.skip_paths]
    refs = []
    for item in got:
        uri = isinstance(item, dict) and (item.get("file") or item.get("path"))
        if uri and not any(p.search(uri) for p in skip):
            refs.append(uri)
    return refs[: settings.inject]


def score_case(case: Case, settings: recall_mod.Settings, runner: recall_mod.Runner) -> dict:
    res = recall_mod.recall(case.question, settings, runner)
    refs = [h.path or h.uri for h in res.hits]
    titles = [h.title for h in res.hits]
    row = {"id": case.id, "question": case.question, "expect": case.expect, "split": case.split, "engine": res.engine, "reranked": res.reranked,
           "source": case.source, "note_type": case.note_type, "overlap": round(case.overlap, 2),
           "status": res.status, "reason": res.reason, "ms": res.ms,
           "query": " ".join(res.query), "narrowed": " ".join(res.narrowed),
           "injected": [{"ref": h.path or h.uri, "title": h.title, "type": h.type, "score": round(h.score, 4)} for h in res.hits]}
    if case.expect:
        row["rank"] = _rank(refs, case.expect, titles, case.note_title)
        row["in_candidates"] = _rank(res.candidates, case.expect) is not None
        row["baseline_rank"] = _rank(baseline(case.question, settings, runner), case.expect)
    return row


def summarize(rows: list[dict]) -> dict:
    """Metrics for one group of scored cases."""
    pos = [r for r in rows if r["expect"]]
    neg = [r for r in rows if not r["expect"]]
    out: dict = {"cases": len(rows), "positives": len(pos), "negatives": len(neg)}
    if pos:
        n = len(pos)
        out["hit_at_k"] = sum(r["rank"] is not None for r in pos) / n
        out["hit_at_1"] = sum(r["rank"] == 1 for r in pos) / n
        out["mrr"] = sum(1 / r["rank"] for r in pos if r["rank"]) / n
        out["miss_rate"] = sum(r["status"] != "hit" for r in pos) / n
        # Found among the candidates but cut before injection: a ranking problem, not a search one.
        out["ranked_out"] = sum(r["rank"] is None and r.get("in_candidates") for r in pos) / n
        out["baseline_hit_at_k"] = sum(r.get("baseline_rank") is not None for r in pos) / n
        hard = [r for r in pos if r.get("overlap", 0) < 0.34]
        if hard:
            out["hard_cases"] = len(hard)
            out["hard_hit_at_k"] = sum(r["rank"] is not None for r in hard) / len(hard)
    if neg:
        out["false_injection_rate"] = sum(r["status"] == "hit" for r in neg) / len(neg)
    ms = sorted(r["ms"] for r in rows if r["status"] in ("hit", "miss"))
    if ms:
        out["p50_ms"], out["p95_ms"] = ms[len(ms) // 2], ms[min(len(ms) - 1, int(len(ms) * 0.95))]
    return out


def run_retrieval(cases: list[Case], settings: Optional[recall_mod.Settings] = None,
                  runner: recall_mod.Runner = recall_mod._run_qmd, label: str = "",
                  progress: Optional[Callable[[int, int], None]] = None) -> dict:
    settings = settings or recall_mod.Settings.load()
    settings.log = False
    rows = []
    for i, case in enumerate(cases, 1):
        rows.append(score_case(case, settings, runner))
        if progress:
            progress(i, len(cases))
    groups = {"all": summarize(rows)}
    for split in ("dev", "test"):
        part = [r for r in rows if r["split"] == split]
        if part:
            groups[split] = summarize(part)
    return {"kind": "retrieval", "t": _now(), "label": label, "set": _set_id(cases),
            "settings": {"inject": settings.inject, "search_n": settings.search_n,
                         "memory_slots": settings.memory_slots, "index": settings.index,
                         "single_term_fallback": settings.single_term_fallback,
                         "engine": settings.engine, "vector": settings.vector, "vector_weight": settings.vector_weight,
                         "vector_gate": settings.vector_gate, "rerank": settings.rerank,
                         "rerank_command": bool(settings.rerank_command)},
            "metrics": groups, "rows": rows}


def _set_id(cases: list[Case]) -> str:
    h = hashlib.sha1("\n".join(sorted(c.id + c.question for c in cases)).encode()).hexdigest()[:10]
    sources = sorted({c.source for c in cases})
    return f"{'+'.join(sources)}:{len(cases)}:{h}"


def save_run(run: dict) -> None:
    EVAL_DIR.mkdir(parents=True, exist_ok=True)
    LAST_RUN.write_text(json.dumps(run, indent=1))
    slim = {k: v for k, v in run.items() if k != "rows"}
    with open(RUNS, "a") as fh:
        fh.write(json.dumps(slim) + "\n")


def load_runs() -> list[dict]:
    try:
        return [json.loads(l) for l in RUNS.read_text().splitlines() if l.strip()]
    except (OSError, ValueError):
        return []


# --------------------------------------------------------------------------- #
# Probes: questions written from your own notes (labels from the source note)
# --------------------------------------------------------------------------- #


def candidate_notes(settings: recall_mod.Settings, limit_per_type: int = 400) -> list[dict]:
    """Typed notes from the indexed collections, skipping session records and the inbox."""
    dirs = recall_mod._collection_dirs(settings.index)
    if settings.collections:
        dirs = {k: v for k, v in dirs.items() if k in settings.collections}
    skip = [re.compile(p) for p in settings.skip_paths]
    found: dict[str, list[dict]] = {}
    for coll, root in dirs.items():
        if not root.is_dir():
            continue
        for path in root.rglob("*.md"):
            rel = f"{coll}/{path.relative_to(root)}"
            if any(p.search("/" + rel) for p in skip) or "/." in "/" + rel:
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            kind = recall_mod.note_type(text)
            body = recall_mod.body_excerpt(text, 1600)
            if kind not in PROBE_TYPES or len(body) < 300:
                continue
            bucket = found.setdefault(kind, [])
            if len(bucket) < limit_per_type * 4:
                title = (re.search(r"^title:\s*[\"']?(.+?)[\"']?\s*$", text[:1500], re.M) or
                         re.search(r"^#\s+(.+)$", text, re.M))
                bucket.append({"ref": rel, "path": str(path), "type": kind,
                               "title": (title.group(1) if title else path.stem).strip(), "body": body})
    return [n for kind in sorted(found) for n in found[kind]]


def sample_notes(notes: list[dict], n: int, seed: int = 7) -> list[dict]:
    """Stratified by type, so a vault full of references still gets gotchas and decisions."""
    rng = random.Random(seed)
    by_type: dict[str, list[dict]] = {}
    for note in notes:
        by_type.setdefault(note["type"], []).append(note)
    for v in by_type.values():
        rng.shuffle(v)
    picked, types = [], sorted(by_type)
    while len(picked) < n and any(by_type.values()):
        for t in types:
            if by_type[t] and len(picked) < n:
                picked.append(by_type[t].pop())
    return picked


PROBE_PROMPT = """You write evaluation questions for a team's note search.
For each note below, write ONE question a teammate or a coding agent would type BEFORE they knew
this note existed, while they were hitting the problem or making the decision the note covers.
Rules:
- Write it the way a person would ask: 8 to 25 words, plain language.
- Do not copy the note's title or its distinctive phrases. Use your own words, the words of
  someone who has the problem, not the answer.
- No file paths, no note names, no dates.
- The note must be the natural answer to the question.
Reply with JSON only: {"probes": [{"id": "<note id>", "question": "..."}]}

NOTES:
"""


def claude_json(prompt: str, timeout: int = 180, model: str = "") -> Optional[dict]:
    """One `claude -p` call in an empty directory, with no project hooks (so recall cannot leak
    into the eval), JSON out. None on any failure: the caller decides whether that is fatal."""
    cmd = ["claude", "-p", prompt, "--output-format", "json", "--setting-sources", "project"]
    if model:
        cmd += ["--model", model]
    env = {**os.environ, "CLAUDE_NOTE_RECALL": "off", "CLAUDE_CODE_HOOKS_ENABLED": "false"}
    try:
        with tempfile.TemporaryDirectory() as cwd:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd,
                                  stdin=subprocess.DEVNULL, env=env)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    try:
        text = json.loads(proc.stdout).get("result", "")
    except ValueError:
        text = proc.stdout
    m = re.search(r"\{.*\}", text or "", re.S)
    try:
        return json.loads(m.group(0)) if m else None
    except ValueError:
        return None


def title_overlap(question: str, title: str) -> float:
    q = {t.lower() for t in recall_mod.content_terms(question, 30)}
    t = {w.lower() for w in recall_mod.content_terms(title, 30)}
    return len(q & t) / len(q) if q else 0.0


def build_probes(n: int = 60, seed: int = 7, batch: int = 10, model: str = "",
                 settings: Optional[recall_mod.Settings] = None,
                 ask: Callable[[str], Optional[dict]] = None,
                 progress: Optional[Callable[[str], None]] = None) -> list[dict]:
    settings = settings or recall_mod.Settings.load()
    ask = ask or (lambda p: claude_json(p, model=model))
    notes = sample_notes(candidate_notes(settings), n, seed)
    probes: list[dict] = []
    for start in range(0, len(notes), batch):
        chunk = notes[start:start + batch]
        listing = "\n\n".join(f"[id {i}] TITLE: {nt['title']}\nTYPE: {nt['type']}\n{nt['body'][:1400]}"
                              for i, nt in enumerate(chunk))
        got = ask(PROBE_PROMPT + listing)
        if got is None:
            if not probes and start == 0:
                raise RuntimeError("the first probe batch failed: is the `claude` CLI installed and signed in?")
            if progress:
                progress(f"batch {start // batch + 1} failed, skipped")
            continue
        for p in got.get("probes", []):
            try:
                nt = chunk[int(p["id"])]
            except (KeyError, ValueError, IndexError, TypeError):
                continue
            q = str(p.get("question", "")).strip()
            if len(q) < 15:
                continue
            probes.append({"id": hashlib.sha1((nt["ref"] + q).encode()).hexdigest()[:8], "question": q,
                           "expect": [nt["ref"]], "split": _split(nt["ref"]), "source": "probe",
                           "note_type": nt["type"], "note_title": nt["title"],
                           "overlap": round(title_overlap(q, nt["title"]), 2), "created": _now()})
        if progress:
            progress(f"{len(probes)} probes from {min(start + batch, len(notes))}/{len(notes)} notes")
    return probes


def save_probes(probes: list[dict]) -> Path:
    EVAL_DIR.mkdir(parents=True, exist_ok=True)
    with open(PROBES, "w") as fh:
        for p in probes:
            fh.write(json.dumps(p, ensure_ascii=False) + "\n")
    return PROBES


# --------------------------------------------------------------------------- #
# Live: the real hook's log, joined with what the agent did next
# --------------------------------------------------------------------------- #


def _parse_ts(s: str) -> Optional[float]:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return None


def _turn_tool_text(transcript: Path, after: float, horizon: float = 1800) -> Optional[str]:
    """Tool inputs (Read paths, shell commands) the agent issued after `after`, up to the next
    typed user prompt or `horizon` seconds. None when the transcript cannot be read."""
    try:
        lines = transcript.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return None
    parts: list[str] = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        ts = _parse_ts(row.get("timestamp", ""))
        if ts is None or ts < after - 2:
            continue
        if ts > after + horizon:
            break
        msg = row.get("message") or {}
        content = msg.get("content")
        if row.get("type") == "user" and isinstance(content, str) and ts > after + 5:
            break  # the next prompt the person typed: the turn is over
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    parts.append(json.dumps(block.get("input", {})))
    return "\n".join(parts)


def live(days: int = 30, log: Optional[Path] = None) -> dict:
    log = log or recall_mod.log_path()
    cutoff = time.time() - days * 86400
    rows = []
    try:
        for line in log.read_text().splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            ts = _parse_ts(r.get("ts", "")) or _parse_ts(r.get("t", "") + "Z" if r.get("t") else "")
            if ts and ts >= cutoff and r.get("status") in ("hit", "miss"):
                r["_ts"] = ts
                rows.append(r)
    except OSError:
        pass
    prompts = [r for r in rows if r.get("kind") == "prompt"]
    out: dict = {"kind": "live", "t": _now(), "days": days, "events": len(rows), "prompts": len(prompts)}
    if not prompts:
        return out
    hits = [r for r in prompts if r["status"] == "hit"]
    out["hit_rate"] = len(hits) / len(prompts)
    ms = sorted(int(r.get("ms", 0)) for r in prompts)
    out["p50_ms"], out["p95_ms"] = ms[len(ms) // 2], ms[min(len(ms) - 1, int(len(ms) * 0.95))]
    # Opened: the agent read an injected note (Read tool, or a shell command naming the file).
    checked = opened = 0
    note_shown: dict[str, int] = {}
    note_opened: dict[str, int] = {}
    for r in hits:
        paths = [p for p in r.get("paths", []) if p]
        transcript = r.get("transcript")
        if not paths or not transcript:
            continue
        text = _turn_tool_text(Path(transcript), r["_ts"])
        if text is None:
            continue
        checked += 1
        any_open = False
        for p in paths:
            note_shown[p] = note_shown.get(p, 0) + 1
            if p in text or Path(p).name in text:
                note_opened[p] = note_opened.get(p, 0) + 1
                any_open = True
        opened += any_open
    if checked:
        out["turns_checked"] = checked
        out["opened_rate"] = opened / checked
        shown_often = sorted(note_shown.items(), key=lambda kv: -kv[1])
        out["never_opened"] = [{"path": p, "shown": n} for p, n in shown_often if p not in note_opened][:10]
        out["most_used"] = [{"path": p, "opened": n, "shown": note_shown[p]}
                            for p, n in sorted(note_opened.items(), key=lambda kv: -kv[1])[:10]]
    misses: dict[str, int] = {}
    for r in prompts:
        if r["status"] == "miss" and r.get("q"):
            misses[r["q"]] = misses.get(r["q"], 0) + 1
    out["top_missed"] = [{"query": q, "times": n} for q, n in sorted(misses.items(), key=lambda kv: -kv[1])[:10]]
    out["miss_rate"] = 1 - out["hit_rate"]
    return out


# --------------------------------------------------------------------------- #
# Answers: with and without recall, judged blind, with a control for the judge
# --------------------------------------------------------------------------- #

ANSWER_PROMPT = "Answer in at most three sentences.\n\n{context}Question: {question}"
JUDGE_PROMPT = """A team keeps notes about how it works. Here is one of its notes:

<note>
{note}
</note>

Two answers to the question "{question}":

<answer id="X">
{x}
</answer>

<answer id="Y">
{y}
</answer>

Which answer follows what this note says the team does or knows? An answer that is generally
reasonable but ignores or contradicts the note does not follow it. If the note is not about this
question at all, answer "neither".
Reply with JSON only: {{"follows": "X" | "Y" | "both" | "neither", "why": "<one sentence>"}}"""


def run_answers(probes: list[dict], n: int = 10, seed: int = 11, model: str = "", judge_model: str = "",
                settings: Optional[recall_mod.Settings] = None, ask: Callable[..., Optional[dict]] = None,
                answer: Callable[[str], Optional[str]] = None,
                progress: Optional[Callable[[str], None]] = None) -> dict:
    """Paired A/B on test-split probes about team-specific knowledge (decisions, gotchas)."""
    settings = settings or recall_mod.Settings.load()
    settings.log = False
    ask = ask or (lambda p: claude_json(p, model=judge_model))
    answer = answer or (lambda p: _claude_text(p, model=model))
    rng = random.Random(seed)
    pool = [p for p in probes if p.get("split") == "test" and p.get("note_type") in ("decision", "gotcha", "feedback")]
    rng.shuffle(pool)
    pool = pool[:n]
    notes = {p["expect"][0]: _note_text(p["expect"][0], settings) for p in pool}
    rows = []
    for i, p in enumerate(pool):
        note = notes.get(p["expect"][0]) or ""
        if not note:
            continue
        res = recall_mod.recall(p["question"], settings)
        context = (recall_mod.format_context(res) + "\n\n") if res.status == "hit" else ""
        without = answer(ANSWER_PROMPT.format(context="", question=p["question"]))
        with_ = answer(ANSWER_PROMPT.format(context=context, question=p["question"]))
        if not without or not with_:
            continue
        flip = rng.random() < 0.5
        x, y = (with_, without) if flip else (without, with_)
        verdict = (ask(JUDGE_PROMPT.format(note=note[:3000], question=p["question"], x=x, y=y)) or {}).get("follows")
        mapping = {"X": "with" if flip else "without", "Y": "without" if flip else "with"}
        follows = {"both": "both", "neither": "neither"}.get(verdict, mapping.get(verdict, "invalid"))
        # Control: the same pair judged against an unrelated note must come back "neither".
        other = next((notes[k] for k in notes if k != p["expect"][0] and notes[k]), "")
        control = (ask(JUDGE_PROMPT.format(note=other[:3000], question=p["question"], x=x, y=y)) or {}).get("follows") if other else None
        rows.append({"id": p["id"], "question": p["question"], "expect": p["expect"],
                     "recall_status": res.status,
                     "recall_found_note": any(matches(h.path or h.uri, p["expect"]) for h in res.hits),
                     "follows": follows, "control": control,
                     "with": with_[:600], "without": without[:600]})
        if progress:
            progress(f"{i + 1}/{len(pool)} judged")
    judged = [r for r in rows if r["follows"] != "invalid"]
    out = {"kind": "answers", "t": _now(), "n": len(judged), "rows": rows}
    if judged:
        k = len(judged)
        out["with_follows"] = sum(r["follows"] in ("with", "both") for r in judged) / k
        out["without_follows"] = sum(r["follows"] in ("without", "both") for r in judged) / k
        controls = [r for r in judged if r["control"]]
        if controls:
            out["control_neither_rate"] = sum(r["control"] == "neither" for r in controls) / len(controls)
            # A judge that finds "follows" against an unrelated note is not reading the note.
            out["judge_trusted"] = out["control_neither_rate"] >= 0.8
    return out


def _note_text(ref: str, settings: recall_mod.Settings) -> str:
    coll, _, rel = ref.partition("/")
    root = recall_mod._collection_dirs(settings.index).get(coll)
    try:
        return (root / rel).read_text(encoding="utf-8", errors="ignore") if root else ""
    except OSError:
        return ""


def _claude_text(prompt: str, model: str = "", timeout: int = 180) -> Optional[str]:
    cmd = ["claude", "-p", prompt, "--setting-sources", "project"]
    if model:
        cmd += ["--model", model]
    env = {**os.environ, "CLAUDE_NOTE_RECALL": "off", "CLAUDE_CODE_HOOKS_ENABLED": "false"}
    try:
        with tempfile.TemporaryDirectory() as cwd:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd,
                                  stdin=subprocess.DEVNULL, env=env)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout.strip() if proc.returncode == 0 and proc.stdout.strip() else None


# --------------------------------------------------------------------------- #
# Advice: what to change, from the failures (shown on the dashboard and by `eval report`)
# --------------------------------------------------------------------------- #


def advice(run: Optional[dict], live_: Optional[dict]) -> list[dict]:
    tips: list[dict] = []
    if run:
        m = run["metrics"].get("test") or run["metrics"]["all"]
        rows = [r for r in run.get("rows", []) if r["expect"]]
        missed = [r for r in rows if r["status"] != "hit"]
        wrong = [r for r in rows if r["status"] == "hit" and r["rank"] is None]
        ranked_out = [r for r in wrong if r.get("in_candidates")]
        if m.get("baseline_hit_at_k") is not None and m.get("hit_at_k", 0) <= m["baseline_hit_at_k"]:
            tips.append({"area": "query ladder", "text": "Recall does no better than one plain keyword search on this set. Check that the ladder runs (qmd version, stopwords) before tuning anything else."})
        if ranked_out:
            tips.append({"area": "ranking", "text": f"{len(ranked_out)} expected note(s) were found but cut before injection. Try a larger `inject`, or a `rerank_command`.", "cases": [r["id"] for r in ranked_out[:8]]})
        if missed:
            tips.append({"area": "vocabulary", "text": f"{len(missed)} question(s) matched nothing. Their notes use other words than people ask with: add the asker's words as tags or a line in the note, or `claude-note remember` a short note in those words.", "cases": [r["id"] for r in missed[:8]]})
        if len(wrong) > len(ranked_out):
            tips.append({"area": "competing notes", "text": f"{len(wrong) - len(ranked_out)} question(s) got other notes instead. Look for near-duplicates to merge, or retitle the expected note so it says what it is about.", "cases": [r["id"] for r in wrong if not r.get("in_candidates")][:8]})
        if m.get("false_injection_rate", 0) > 0.3:
            tips.append({"area": "noise", "text": "Recall injects notes for questions your vault cannot answer. Set a `rerank_command` so weak matches are dropped."})
        if m.get("p95_ms", 0) > 3000:
            tips.append({"area": "latency", "text": "p95 recall latency is over 3 s. Narrow `collections`, or lower `search_n`."})
    if live_ and live_.get("turns_checked", 0) >= 10 and live_.get("opened_rate", 1) < 0.15:
        tips.append({"area": "usefulness", "text": "Agents rarely open the notes recall injects. Check the never-opened list: those notes are being shown for the wrong questions."})
    return tips
