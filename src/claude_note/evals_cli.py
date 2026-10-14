"""`claude-note eval …`: run, probes, live, answers, report, dashboard. See docs/evals.md."""

from __future__ import annotations

import json
import hashlib
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
    n.add_argument("--private-probes", action="store_true", help="Explicitly include saved private-vault probes")
    tasks = es.add_parser("tasks", help="Offline policy contracts or captured executable-task traces")
    tasks.add_argument("--trace", help="JSON {suite,task_ids,baselines,records}; verifiers run externally")
    tasks.add_argument("--json", action="store_true")
    tasks.add_argument("--no-save", action="store_true")
    agent = es.add_parser("agent-tasks", help="Plan or execute isolated synthetic coding-agent outcomes")
    agent.add_argument("--execute", action="store_true", help="Invoke the configured model within the explicit budget")
    agent.add_argument("--model", default="")
    agent.add_argument("--budget-usd", type=float, default=1.0)
    agent.add_argument("--per-attempt-usd", type=float, default=.25)
    agent.add_argument("--timeout", type=int, default=90)
    agent.add_argument("--split", choices=["dev", "holdout", "all"], default="dev")
    agent.add_argument("--cases", type=int, default=4)
    agent.add_argument("--seed", type=int, default=17)
    agent.add_argument("--baselines", default="no_memory,static_instructions,scoped_lexical,current_hybrid,corrected_hybrid,bounded_history")
    agent.add_argument("--embed-model", help="Absolute existing local GGUF; never downloaded by harness")
    agent.add_argument("--rerank-model", help="Absolute existing local GGUF; never downloaded by harness")
    agent.add_argument("--baseline-checkout", help="Read-only checkout pinned at5c1ec7e for the old hybrid baseline")
    agent.add_argument("--prepare-retrieval", help="Write public synthetic corpora/config into an empty output directory; no indexing/model call")
    agent.add_argument("--out", default=str(evals.EVAL_DIR / "last-agent-tasks.json"))
    agent.add_argument("--json", action="store_true")
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
            line.append(f"raw search reference {m['baseline_hit_at_k']:.0%}")
            line.append(f"scoped lexical {m.get('scoped_lexical_hit_at_k', 0):.0%}")
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
    if cmd == "agent-tasks":
        from . import agent_tasks
        if args.prepare_retrieval:
            out = agent_tasks.prepare_retrieval_fixture(Path(args.prepare_retrieval), embed_model=args.embed_model or "",
                                                       rerank_model=args.rerank_model or "")
            print(json.dumps(out, indent=2))
            return 0
        baselines = tuple(b.strip() for b in args.baselines.split(",") if b.strip())
        cases = [c for c in agent_tasks.fixtures() if args.split == "all" or c["split"] == args.split][:args.cases]
        if not args.execute:
            print(json.dumps({"mode": "plan_no_model_calls", "suite": agent_tasks.SUITE, "tasks": [c["id"] for c in cases],
                              "baselines": baselines, "scheduled_attempts": len(cases) * len(baselines),
                              "budget_usd": args.budget_usd, "model": args.model or "required for --execute"}, indent=2))
            return 0
        out = run_agent_tasks(vars(args))
        target = Path(args.out).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(out, indent=2))
        print(json.dumps(out, indent=2) if args.json else json.dumps({"measurement": out["measurement"], "metrics": out["metrics"],
              "model_calls": out["model_calls"], "reserved_budget_usd": out["reserved_budget_usd"], "out": str(target)}, indent=2))
        return 0 if out.get("model_responses") else 1
    if cmd == "tasks":
        if args.trace:
            payload = json.loads(Path(args.trace).expanduser().read_text())
            out = evals.score_task_traces(payload["records"], payload["task_ids"], payload["baselines"], suite=payload["suite"])
        else:
            out = evals.run_policy_contracts()
        if not args.no_save:
            evals.EVAL_DIR.mkdir(parents=True, exist_ok=True)
            (evals.EVAL_DIR / "last-tasks.json").write_text(json.dumps(out, indent=2))
        if args.json:
            print(json.dumps(out, indent=2))
        else:
            print(out.get("measurement") or out["verification"])
            for baseline, metrics in out["metrics"].items():
                print(f"{baseline}: {json.dumps(metrics, sort_keys=True)}")
        return 0
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
        except (RuntimeError, ValueError) as exc:
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
        try:
            out = evals.run_answers(probes, args.n, model=args.model, judge_model=args.judge_model,
                                    progress=lambda m: print(m, file=sys.stderr))
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        evals.EVAL_DIR.mkdir(parents=True, exist_ok=True)
        ANSWERS_LAST.write_text(json.dumps(out, indent=1))
        with open(evals.RUNS, "a") as fh:
            fh.write(json.dumps({k: v for k, v in out.items() if k != "rows"}) + "\n")
        if args.json:
            print(json.dumps(out, indent=1))
        else:
            print(f"{out['attempted']} attempted · {out['judged']} judged · {out['unjudged']} unjudged")
            if out['attempted']:
                print(f"Across all attempted questions, answer follows the team's note: with recall {out['with_follows']:.0%}, "
                      f"without {out['without_follows']:.0%}")
            if out['judged'] and out['unjudged']:
                print(f"Among judged pairs only: with recall {out['judged_with_follows']:.0%}, "
                      f"without {out['judged_without_follows']:.0%}")
            if out['control_attempted']:
                print(f"Judge control: {out['control_judged']}/{out['control_attempted']} judged · "
                      f"{out['control_neither_rate']:.0%} 'neither' across attempted controls")
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
        page = render(evals.trend_history(last, evals.load_runs()), last, live, _latest("answers"), evals.advice(last, live))
        out = Path(args.out).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(page)
        print(out)
        if not args.no_open:
            webbrowser.open(out.as_uri())
        return 0
    print("usage: claude-note eval {run,probes,live,answers,report,dashboard}", file=sys.stderr)
    return 2


def run_agent_tasks(options: dict) -> dict:
    from . import agent_tasks
    from contextlib import ExitStack, nullcontext
    baseline_names = options.get("baselines", ",".join(agent_tasks.BASELINES))
    baselines = tuple(b.strip() for b in baseline_names.split(",") if b.strip()) if isinstance(baseline_names, str) else tuple(baseline_names)
    paths = [options.get(k) for k in ("embed_model", "rerank_model", "baseline_checkout")]
    if any(paths) and not all(paths):
        raise ValueError("embed_model, rerank_model and baseline_checkout must be provided together")
    context = agent_tasks.isolated_hybrid_backends(embed_model=Path(paths[0]), rerank_model=Path(paths[1]), baseline_checkout=Path(paths[2])) if all(paths) else nullcontext({})
    with ExitStack() as stack:
        setup_error = ""
        try:
            retrievers = stack.enter_context(context)
        except (RuntimeError, ValueError, OSError) as exc:
            setup_error = f"{type(exc).__name__}: {exc}"[:300]
            def unavailable(case):
                return {"available": False, "reason": setup_error, "synthetic_corpus": True,
                        "code_revision": "setup-unavailable"}
            retrievers = {name: unavailable for name in ("current_hybrid", "corrected_hybrid")}
        result = agent_tasks.run(model=options.get("model", ""), budget_usd=options.get("budget_usd", 1.0),
            per_attempt_usd=options.get("per_attempt_usd", .25), timeout=options.get("timeout", 90),
            split=options.get("split", "dev"), max_cases=options.get("cases", 4), seed=options.get("seed", 17),
            baselines=baselines, retrievers=retrievers)
        if setup_error:
            result["retrieval_setup_error"] = setup_error
        return result


def agent_task_regressions(run: dict, history: list[dict]) -> list[str]:
    """Compare only complete fixed-model/fixture/config identities, retaining failed attempts."""
    from statistics import median
    same = [old for old in history if old.get("comparable") and run.get("comparable")
            and old.get("fingerprint") == run.get("fingerprint") and old.get("t") != run.get("t")][-3:]
    if len(same) < 3:
        return []
    findings = []
    for baseline, metrics in run["metrics"].items():
        prior = [old["metrics"][baseline]["success_rate"] for old in same]
        threshold = max(.10, 1 / max(1, metrics["scheduled"]))
        if median(prior) - metrics["success_rate"] >= threshold - 1e-9:
            findings.append(f"agent tasks {baseline}: success {metrics['success_rate']:.0%} vs prior median {median(prior):.0%} ({metrics['scheduled']} scheduled)")
    return findings


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
        from dataclasses import replace
        from . import setup_recall
        contracts = evals.run_policy_contracts()
        (evals.EVAL_DIR / "last-policy-contracts.json").write_text(json.dumps(contracts, indent=2))
        found = []
        policy = contracts["metrics"].get("verified_policy", {})
        if policy.get("contract_pass_rate") != 1 or policy.get("harmful_actions"):
            found.append("offline verified-policy contract regression")
        status["policy_contracts"] = {k: v for k, v in contracts.items() if k != "rows"}
        agent_config = (recall_mod._load_toml().get("eval", {}) or {}).get("agent_tasks", {}) or {}
        if recall_mod._as_bool(agent_config.get("enabled"), False):
            # Real model calls are opt-in and require an explicit recurring budget.
            if not agent_config.get("model") or "budget_usd" not in agent_config:
                raise ValueError("[eval.agent_tasks] requires model and budget_usd when enabled")
            history_path = evals.EVAL_DIR / "agent-task-runs.jsonl"
            history = [json.loads(line) for line in history_path.read_text().splitlines() if line.strip()] if history_path.exists() else []
            outcome = run_agent_tasks(agent_config)
            (evals.EVAL_DIR / "last-agent-tasks.json").write_text(json.dumps(outcome, indent=2))
            compact = {k: v for k, v in outcome.items() if k not in ("rows", "records")}
            with history_path.open("a") as stream:
                stream.write(json.dumps(compact) + "\n")
            agent_findings = agent_task_regressions(outcome, history)
            if not outcome["model_responses"]:
                agent_findings.append("agent tasks enabled but no model response arrived; no executable outcomes")
            if any("unavailable" in record.get("error", "") for record in outcome["records"]):
                agent_findings.append("agent task baseline unavailable; see all-attempt denominators")
            found += agent_findings
            status["agent_tasks"] = {**compact, "regressions": agent_findings}
        else:
            status["agent_tasks"] = {"skipped": "real-model task evaluation requires explicit [eval.agent_tasks] enabled/model/budget_usd"}
        settings = recall_mod.Settings.load()
        public_registered = setup_recall.PUBLIC_CANARY_COLLECTION in recall_mod._collection_dirs("")
        public_run = None
        if public_registered:
            capabilities = setup_recall.probe_default_server(settings)
            status["default_server"] = capabilities
            if not capabilities["ok"]:
                found.append("default-server public canary/capability probe failed")
            public = replace(settings, engine="server", index="", collections=[setup_recall.PUBLIC_CANARY_COLLECTION],
                             memory_collections=[], log=False)
            source = f"qmd://{setup_recall.PUBLIC_CANARY_COLLECTION}/doctor-canary.md"
            case = evals.Case("public-payment-retry", "Why are customers charged twice when a payment request is retried?",
                              [source], source="public-canary", expected_sources=[{"source_id": source,
                              "revision": hashlib.sha256(setup_recall.CANARY.encode()).hexdigest()}])
            history = evals.load_runs()
            public_run = evals.run_retrieval([case], public, label="nightly-public")
            evals.save_run(public_run)
            found += evals.regressions(public_run, history)
            status.update(set=public_run["set"], metrics=public_run["metrics"],
                          compared_with=len(evals.comparable(public_run, history)))
        else:
            status["default_server"] = {"ok": False, "skipped": "public canary not registered; run setup and refresh"}
        opt_in = getattr(args, "private_probes", False) is True or recall_mod._as_bool(
            (recall_mod._load_toml().get("eval", {}) or {}).get("private_probes"), False)
        if not opt_in or not evals.PROBES.exists():
            status["skipped"] = "private probe set not enabled/present; opt in with --private-probes or [eval] private_probes=true"
        else:
            cases = evals.load_cases(evals.PROBES) + evals.load_cases(evals.FIXTURE.parent / "offtopic.jsonl")
            if not recall_mod.qmd_executable(settings):
                raise RuntimeError("qmd is not installed")
            history = evals.load_runs()
            run = evals.run_retrieval(cases, settings, label="nightly-private-opt-in")
            evals.save_run(run)
            private_findings = evals.regressions(run, history)
            found += private_findings
            live = evals.live(30)
            page = render(evals.trend_history(run, evals.load_runs()), run, live, _latest("answers"), evals.advice(run, live))
            (evals.EVAL_DIR / "dashboard.html").write_text(page)
            status["private_probes"] = {"set": run["set"], "metrics": run["metrics"],
                                       "compared_with": len(evals.comparable(run, history)), "regressions": private_findings}
            if public_run is None:
                status["metrics"] = run["metrics"]
        status.update(ok=not found, regressions=found)
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
        print(f"nightly: policy contracts complete; {status['skipped']}"
              + ("" if status["ok"] else " · REGRESSION: " + "; ".join(status["regressions"])))
    else:
        m = status["metrics"].get("test") or status["metrics"]["all"]
        print(f"nightly: right note injected {m.get('hit_at_k', 0):.0%} (test) · "
              f"off-topic {status['metrics']['all'].get('false_injection_rate', 0):.0%} · p95 {m.get('p95_ms')} ms"
              + ("" if status["ok"] else " · REGRESSION: " + "; ".join(status["regressions"])))
    return 0 if status["ok"] else 3
