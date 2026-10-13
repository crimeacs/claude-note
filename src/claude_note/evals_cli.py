"""`claude-note eval …`: run, probes, live, answers, report, dashboard. See docs/evals.md."""

from __future__ import annotations

import json
import sys
import webbrowser
from pathlib import Path

from . import evals
from . import recall as recall_mod

ANSWERS_LAST = evals.EVAL_DIR / "last-answers.json"


def add_parser(sub) -> None:
    e = sub.add_parser("eval", help="Measure recall: labelled runs, live signals, answer A/B, dashboard")
    es = e.add_subparsers(dest="eval_command")

    r = es.add_parser("run", help="Score recall on a labelled set (default: your probes, else the demo fixture)")
    r.add_argument("--set", help="JSONL of {question, expect: [note paths], split}")
    r.add_argument("--fixture", action="store_true", help="The bundled demo-vault set (index it first; see docs/evals.md)")
    r.add_argument("--index", help="QMD index name (overrides [recall] index)")
    r.add_argument("-c", "--collection", action="append", help="Limit to a QMD collection (repeatable)")
    r.add_argument("--inject", type=int, help="Notes injected per prompt (to compare settings)")
    r.add_argument("--label", default="", help="Name this run, e.g. the setting you changed")
    r.add_argument("--single-term-fallback", action="store_true", help="Try the [recall] single_term_fallback setting")
    r.add_argument("--negatives", action="store_true", help="Add the bundled off-topic questions (junk-injection check)")
    r.add_argument("--engine", choices=["auto", "server", "cli"], help="Recall engine to measure")
    r.add_argument("--no-vector", action="store_true", help="Server engine without the search by meaning")
    r.add_argument("--no-rerank", action="store_true", help="Server engine without the reranker")
    r.add_argument("--vector-weight", type=float, help="Weight of the meaning list in the fusion")
    r.add_argument("--gate", type=float, help="Minimum meaning score before anything is injected")
    r.add_argument("--no-save", action="store_true", help="Do not add this run to the history")
    r.add_argument("--set-option", action="append", default=[], metavar="KEY=VALUE",
                   help="Try any [recall] setting for this run, e.g. rerank_top=20 (repeatable)")
    r.add_argument("--json", action="store_true")
    r.add_argument("--quiet", action="store_true")

    p = es.add_parser("probes", help="Write questions from your own notes (uses the Claude CLI)")
    p.add_argument("-n", type=int, default=60, help="Notes to sample (stratified by type)")
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--model", default="", help="Claude model for writing probes")
    p.add_argument("--yes", action="store_true", help="Replace an existing probe set without asking")
    p.add_argument("-c", "--collection", action="append", help="Sample only these QMD collections (repeatable)")
    p.add_argument("--index", help="QMD index name (overrides [recall] index)")

    l = es.add_parser("live", help="What the real recall hook did: misses, latency, notes the agent opened")
    l.add_argument("--days", type=int, default=30)
    l.add_argument("--json", action="store_true")

    a = es.add_parser("answers", help="Same questions with and without recall, judged blind (uses the Claude CLI)")
    a.add_argument("-n", type=int, default=10)
    a.add_argument("--model", default="", help="Model that answers")
    a.add_argument("--judge-model", default="", help="Model that judges (use a different one if you can)")
    a.add_argument("--json", action="store_true")

    rp = es.add_parser("report", help="Latest results and what to fix, for people and agents")
    rp.add_argument("--json", action="store_true")

    n = es.add_parser("nightly", help="Run the evals the same way every night; exit 3 on a regression")
    n.add_argument("--heartbeat", help="Also touch this file when the run completes (for an absence alert)")
    n.add_argument("--json", action="store_true")
    d = es.add_parser("dashboard", help="Write the HTML dashboard (and open it)")
    d.add_argument("--out", default=str(evals.EVAL_DIR / "dashboard.html"))
    d.add_argument("--no-open", action="store_true")


def _settings(args) -> recall_mod.Settings:
    s = recall_mod.Settings.load()
    if getattr(args, "index", None):
        s.index = args.index
    if getattr(args, "collection", None):
        s.collections, s.memory_collections = args.collection, []
    if getattr(args, "inject", None):
        s.inject = args.inject
    if getattr(args, "single_term_fallback", False):
        s.single_term_fallback = True
    if getattr(args, "engine", None):
        s.engine = args.engine
    if getattr(args, "no_vector", False):
        s.vector = False
    if getattr(args, "no_rerank", False):
        s.rerank = False
    if getattr(args, "vector_weight", None) is not None:
        s.vector_weight = args.vector_weight
    if getattr(args, "gate", None) is not None:
        s.vector_gate = args.gate
    for kv in getattr(args, "set_option", None) or []:
        key, _, raw = kv.partition("=")
        key = key.strip()
        if not hasattr(s, key):
            raise SystemExit(f"unknown [recall] setting: {key}")
        cur = getattr(s, key)
        if isinstance(cur, bool):
            val = raw.strip().lower() in ("1", "true", "yes", "on")
        elif isinstance(cur, int):
            val = int(raw)
        elif isinstance(cur, float):
            val = float(raw)
        elif isinstance(cur, list):
            val = [x.strip() for x in raw.split(",") if x.strip()]
        else:
            val = raw
        setattr(s, key, val)
    return s


def _cases(args):
    extra = evals.load_cases(evals.FIXTURE.parent / "offtopic.jsonl") if getattr(args, "negatives", False) else []
    base = _base_cases(args)
    return None if base is None else base + extra


def _base_cases(args):
    if args.set:
        return evals.load_cases(Path(args.set).expanduser())
    if args.fixture:
        return evals.load_cases(evals.FIXTURE)
    if evals.PROBES.exists():
        return evals.load_cases(evals.PROBES)
    print("No probe set yet. Build one from your notes with `claude-note eval probes`, "
          "or score the demo set with `claude-note eval run --fixture`.", file=sys.stderr)
    return None


def _print_metrics(run: dict) -> None:
    for group in ("test", "dev", "all"):
        m = run["metrics"].get(group)
        if not m:
            continue
        line = [f"{group:>4}: {m['cases']} questions"]
        if "hit_at_k" in m:
            line.append(f"right note injected {m['hit_at_k']:.0%} (first {m['hit_at_1']:.0%}, MRR {m['mrr']:.2f})")
            line.append(f"found nothing {m['miss_rate']:.0%}")
            line.append(f"plain search {m['baseline_hit_at_k']:.0%}")
        if "false_injection_rate" in m:
            line.append(f"off-topic injected {m['false_injection_rate']:.0%}")
        if "p95_ms" in m:
            line.append(f"p95 {m['p95_ms']} ms")
        print(" · ".join(line))


def _latest(kind: str) -> dict | None:
    if kind == "retrieval":
        try:
            return json.loads(evals.LAST_RUN.read_text())
        except (OSError, ValueError):
            return None
    try:
        return json.loads(ANSWERS_LAST.read_text())
    except (OSError, ValueError):
        return None


def main(args) -> int:
    cmd = args.eval_command
    if cmd == "run":
        cases = _cases(args)
        if cases is None:
            return 2
        settings = _settings(args)
        if not recall_mod.qmd_executable(settings):
            print("qmd is not installed: recall cannot be measured.", file=sys.stderr)
            return 1
        prog = None if (args.quiet or args.json) else (lambda i, n: print(f"\r{i}/{n}", end="", file=sys.stderr))
        run = evals.run_retrieval(cases, settings, label=args.label, progress=prog)
        if prog:
            print(file=sys.stderr)
        if not args.no_save:
            evals.save_run(run)
        if args.json:
            print(json.dumps(run, indent=1))
        elif not args.quiet:
            _print_metrics(run)
            for t in evals.advice(run, None):
                print(f"- {t['area']}: {t['text']}")
        return 0
    if cmd == "probes":
        if evals.PROBES.exists() and not args.yes:
            print(f"{evals.PROBES} exists. Rebuilding changes the set, so old and new runs stop being "
                  "comparable. Re-run with --yes to replace it.", file=sys.stderr)
            return 2
        print(f"Writing questions for {args.n} notes with the Claude CLI (about {max(1, args.n // 10)} calls)…", file=sys.stderr)
        try:
            probes = evals.build_probes(args.n, args.seed, model=args.model, settings=_settings(args),
                                        progress=lambda m: print(m, file=sys.stderr))
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        path = evals.save_probes(probes)
        dev = sum(p["split"] == "dev" for p in probes)
        print(f"{len(probes)} probes ({dev} dev, {len(probes) - dev} test) -> {path}")
        print("Next: `claude-note eval run`, then `claude-note eval dashboard`.")
        return 0
    if cmd == "live":
        out = evals.live(args.days)
        print(json.dumps(out, indent=1) if args.json else _live_text(out))
        return 0
    if cmd == "answers":
        if not evals.PROBES.exists():
            print("Needs probes: `claude-note eval probes` first.", file=sys.stderr)
            return 2
        probes = [json.loads(l) for l in evals.PROBES.read_text().splitlines() if l.strip()]
        out = evals.run_answers(probes, args.n, model=args.model, judge_model=args.judge_model,
                                progress=lambda m: print(m, file=sys.stderr))
        evals.EVAL_DIR.mkdir(parents=True, exist_ok=True)
        ANSWERS_LAST.write_text(json.dumps(out, indent=1))
        with open(evals.RUNS, "a") as fh:
            fh.write(json.dumps({k: v for k, v in out.items() if k != "rows"}) + "\n")
        if args.json:
            print(json.dumps(out, indent=1))
        elif out.get("n"):
            print(f"{out['n']} questions · answer follows the team's note: with recall {out['with_follows']:.0%}, "
                  f"without {out['without_follows']:.0%} · judge control {out.get('control_neither_rate', 0):.0%} 'neither'")
        else:
            print("No question could be judged (no decision/gotcha probes in the test split, or the CLI failed).")
        return 0
    if cmd == "report":
        last, live = _latest("retrieval"), evals.live(30)
        out = {"retrieval": {k: v for k, v in (last or {}).items() if k != "rows"} or None,
               "answers": {k: v for k, v in (_latest("answers") or {}).items() if k != "rows"} or None,
               "live": live, "advice": evals.advice(last, live)}
        if args.json:
            print(json.dumps(out, indent=1))
        else:
            if last:
                _print_metrics(last)
            print(_live_text(live))
            for t in out["advice"]:
                print(f"- {t['area']}: {t['text']}")
        return 0
    if cmd == "nightly":
        return nightly(args)
    if cmd == "dashboard":
        from .evals_dashboard import render
        last, live = _latest("retrieval"), evals.live(30)
        page = render(evals.load_runs(), last, live, _latest("answers"), evals.advice(last, live))
        out = Path(args.out).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(page)
        print(out)
        if not args.no_open:
            webbrowser.open(out.as_uri())
        return 0
    print("usage: claude-note eval {run,probes,live,answers,report,dashboard}", file=sys.stderr)
    return 2


def _live_text(out: dict) -> str:
    if not out.get("prompts"):
        return f"live: no recall-hook prompts logged in the last {out['days']} days"
    s = (f"live ({out['days']} days): {out['prompts']} prompts · found nothing {out['miss_rate']:.0%} · "
         f"p95 {out['p95_ms']} ms")
    if "opened_rate" in out:
        s += f" · agent opened an injected note in {out['opened_rate']:.0%} of {out['turns_checked']} turns"
    return s


def nightly(args) -> int:
    """The continuous eval: frozen probes plus the off-topic set, the real-use log, the dashboard.
    Settings are whatever recall uses today, so a regression is caught the night it happens.
    Exit 0 when the run is fine (or there is nothing to run yet), 3 on a regression, 1 if it failed."""
    from datetime import datetime, timezone
    from .evals_dashboard import render
    evals.EVAL_DIR.mkdir(parents=True, exist_ok=True)
    status = {"t": evals._now(), "ok": False}
    try:
        if not evals.PROBES.exists():
            status.update(ok=True, skipped="no probe set yet: run `claude-note eval probes`")
        else:
            cases = evals.load_cases(evals.PROBES) + evals.load_cases(evals.FIXTURE.parent / "offtopic.jsonl")
            settings = recall_mod.Settings.load()
            if not recall_mod.qmd_executable(settings):
                raise RuntimeError("qmd is not installed")
            history = evals.load_runs()
            run = evals.run_retrieval(cases, settings, label="nightly")
            evals.save_run(run)
            found = evals.regressions(run, history)
            live = evals.live(30)
            page = render(evals.load_runs(), run, live, _latest("answers"), evals.advice(run, live))
            (evals.EVAL_DIR / "dashboard.html").write_text(page)
            status.update(ok=not found, regressions=found, set=run["set"],
                          metrics={k: run["metrics"][k] for k in ("test", "dev", "all") if k in run["metrics"]},
                          compared_with=len(evals.comparable(run, history)),
                          live={k: live.get(k) for k in ("prompts", "miss_rate", "opened_rate", "p95_ms")},
                          advice=evals.advice(run, live))
    except Exception as exc:  # noqa: BLE001 - recorded, not raised: the status file is the report
        status.update(ok=False, error=f"{type(exc).__name__}: {exc}"[:300])
        evals.NIGHTLY.write_text(json.dumps(status, indent=1))
        print(status["error"], file=sys.stderr)
        return 1
    evals.NIGHTLY.write_text(json.dumps(status, indent=1))
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") + "\n"
    for hb in filter(None, [evals.NIGHTLY_OK, Path(args.heartbeat).expanduser() if args.heartbeat else None]):
        hb.parent.mkdir(parents=True, exist_ok=True)
        hb.write_text(stamp)
    if args.json:
        print(json.dumps(status, indent=1))
    elif status.get("skipped"):
        print(f"nightly: {status['skipped']}")
    else:
        m = status["metrics"].get("test") or status["metrics"]["all"]
        print(f"nightly: right note injected {m.get('hit_at_k', 0):.0%} (test) · "
              f"off-topic {status['metrics']['all'].get('false_injection_rate', 0):.0%} · p95 {m.get('p95_ms')} ms"
              + ("" if status["ok"] else " · REGRESSION: " + "; ".join(status["regressions"])))
    return 0 if status["ok"] else 3

