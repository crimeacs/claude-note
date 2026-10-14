"""Console entry point.

Some commands are answered before `cli` is imported: `cli` imports `config`,
which exits on a machine with no vault configured. A health check must report
that state rather than die of it, and a hook must never fail (or be slow)
because of it: `hook recall` runs on every prompt.
"""

import argparse
import sys


def _knowledge_parser(with_eval: bool = False) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="claude-note")
    sub = p.add_subparsers(dest="command")

    h = sub.add_parser("hook", help="Hook handlers (read the hook JSON on stdin)")
    h.add_argument("which", choices=["recall", "sentinel"])

    r = sub.add_parser("recall", help="Search prior knowledge the way the recall hook does")
    r.add_argument("query", nargs="*", help="What you are about to work on")
    r.add_argument("--json", action="store_true", help="Machine-readable result")
    r.add_argument("--stats", action="store_true", help="Hit rate and latency of the recall hook")
    r.add_argument("--days", type=int, default=7)
    r.add_argument("-n", type=int, default=0, help="Notes to return (default: [recall] inject)")
    r.add_argument("-c", "--collection", action="append", help="Limit to a QMD collection (repeatable)")

    f = sub.add_parser("refresh", help="Refresh the QMD index (text, then vectors) with a heartbeat")
    f.add_argument("--no-embed", action="store_true", help="Text index only")

    d = sub.add_parser("doctor", help="Prove the knowledge loop works end to end")
    d.add_argument("--json", action="store_true")
    d.add_argument("--skip-probe", action="store_true", help="Configuration checks only")
    d.add_argument("--claude-settings", default="~/.claude/settings.json")
    d.add_argument("--codex-hooks", default="~/.codex/hooks.json")

    from .claims_cli import add_parser as add_claim_parser
    add_claim_parser(sub)

    if with_eval:  # only for `eval`: the hooks run on every prompt and must not pay for it
        from .evals_cli import add_parser as add_eval_parser
        add_eval_parser(sub)
    return p


KNOWLEDGE_COMMANDS = {"hook", "recall", "refresh", "doctor", "eval", "claim"}


def _knowledge_main(argv: list) -> int:
    args = _knowledge_parser(with_eval=argv[:1] == ["eval"]).parse_args(argv)
    if args.command == "hook":
        if args.which == "recall":
            from .recall import hook_main
        else:
            from .sentinel import hook_main
        try:
            return hook_main()
        except Exception:
            return 0  # a hook must never break the prompt
    if args.command == "recall":
        import json
        from . import recall
        if args.stats:
            print(json.dumps(recall.stats(args.days), indent=2))
            return 0
        query = " ".join(args.query).strip()
        if not query:
            print("usage: claude-note recall \"<what you are about to work on>\"", file=sys.stderr)
            return 2
        settings = recall.Settings.load()
        settings.min_prompt = 3
        if args.n:
            settings.inject = args.n
        if args.collection:
            settings.collections, settings.memory_collections = args.collection, []
        result = recall.recall(query, settings)
        if settings.log:
            recall.log_event("manual", result)
        print(json.dumps(result.to_dict(), indent=2) if args.json else recall.format_terminal(result))
        return 0 if result.status == "hit" else 1
    if args.command == "eval":
        from .evals_cli import main as eval_main
        return eval_main(args)
    if args.command == "claim":
        from .claims_cli import main as claim_main
        return claim_main(args)
    if args.command == "refresh":
        from .setup_recall import refresh
        return refresh(embed=not args.no_embed)
    from .setup_recall import cmd_doctor
    return cmd_doctor(args)


def main() -> int:
    argv = sys.argv[1:]
    if argv[:1] == ["status"] and "--json" in argv[1:]:
        from . import health
        return health.main()
    if argv[:1] and argv[0] in KNOWLEDGE_COMMANDS:
        return _knowledge_main(argv)
    from .cli import main as cli_main
    return cli_main()
