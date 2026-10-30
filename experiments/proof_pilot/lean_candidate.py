"""Public pilot adapter for the pinned Open Ontologies Horn producer/checker.

The upstream Python proposer is untrusted. The native Lean-compiled checker has
a proved sound checking algorithm, but each run is not kernel proof replay.
Parser, compiler/runtime, this binding wrapper, and trusted supplied facts/rules
remain in the trusted computing base. No world-truth or non-entailment claim.

Upstream MIT source: fabio-rovai/open-ontologies at UPSTREAM_COMMIT below.
Only the public ASCII URN Horn profile is supported. No package engine init,
model, provider, Rust build, production store, or source policy mutation.
"""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import sys
import time
import types

UPSTREAM_COMMIT = "2ecb50c2ca7e99778d767251ec8ca64146c1669f"
LEAN_TOOLCHAIN = "leanprover/lean4:v4.33.1"
PYOXIGRAPH_VERSION = "0.5.11"
MAX_BYTES = 256 * 1024
MAX_STEPS, MAX_RULES, MAX_FACTS = 256, 32, 256
SOURCE_HASHES = {
    "terms.py": "50d533d594bcaa6e9d1772ca35ec6c649456840c6914c138210049e242834f36",
    "rules.py": "31b2e22ba8bc4d8376c85fce92b7f840fc22aa37302043fa3ed49cf58363b9ef",
    "reason.py": "ea04667facfbda5bd862ddf25827d10009b7a032471e7664c7941b0eed25e4b8",
    "tables/builtin_rules.tsv": "50d3668e978bfc23009a8fdd5285f554fcb7b9da15b799fa970cd0eb8c7e9670",
}
TERM = re.compile(r"<urn:pilot:[A-Za-z0-9:._~-]+>\Z")
VAR = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


class CandidateUnavailable(RuntimeError):
    """An actual failed producer attempt, never a synthesized verdict."""


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def _read(path, limit=MAX_BYTES):
    with Path(path).open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("file-size-bound")
    return data


def _text(value):
    if not isinstance(value, str) or not value.isascii() or "\r" in value:
        raise ValueError("unsupported-encoding")
    if any((ord(c) < 32 and c not in "\t\n") or ord(c) == 127 for c in value):
        raise ValueError("unsupported-control")
    if len(value.encode()) > MAX_BYTES:
        raise ValueError("input-size-bound")
    return value


def _term(value):
    if not isinstance(value, str) or not TERM.fullmatch(value):
        raise ValueError("unsupported-term")
    return value


def _atom(atom):
    if not isinstance(atom, (list, tuple)) or len(atom) != 3:
        raise ValueError("invalid-atom")
    return tuple(_term(field) for field in atom)


def _case(case):
    facts = case["facts"]
    if not isinstance(facts, (list, tuple)) or len(facts) > MAX_FACTS:
        raise ValueError("facts-bound")
    facts = sorted(set(_atom(atom) for atom in facts))
    rules = _text(case["rules_text"])
    rows = [line.split("\t") for line in rules.split("\n") if line]
    if len(rows) > MAX_RULES:
        raise ValueError("rules-bound")
    for row in rows:
        if len(row) < 5 or not row[0] or not VAR.fullmatch(row[0]):
            raise ValueError("unsupported-rule-name")
        if not row[1].isascii() or not row[1].isdigit():
            raise ValueError("invalid-rule-arity")
        count = int(row[1])
        if count > 8 or len(row) != 2 + count * 3 + 3:
            raise ValueError("rule-arity-bound")
        for field in row[2:]:
            if field.startswith("?"):
                if not VAR.fullmatch(field[1:]):
                    raise ValueError("unsupported-variable")
            else:
                _term(field)
    goal = _atom(case["goal"])
    asserted = "".join("\t".join(atom) + "\n" for atom in facts)
    if len(asserted.encode()) + len(rules.encode()) > MAX_BYTES:
        raise ValueError("aggregate-input-bound")
    return {"facts": facts, "rules_text": rules, "goal": goal}, asserted


def _environment(work):
    # HOME retains the account's actual value. Only explicitly owned cache/tmp
    # locations are redirected. No credentials/proxies are passed to children.
    env = {key: os.environ[key] for key in (
        "HOME", "PATH", "LANG", "LC_ALL", "SYSTEMROOT", "LD_LIBRARY_PATH",
        "DYLD_LIBRARY_PATH", "DYLD_FALLBACK_LIBRARY_PATH") if key in os.environ}
    for key, name in (("TMPDIR", "tmp"), ("XDG_CACHE_HOME", "cache"),
                      ("XDG_CONFIG_HOME", "config"), ("XDG_DATA_HOME", "data")):
        path = work / name
        path.mkdir(parents=True, exist_ok=True)
        env[key] = str(path)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _owned(argv, cwd, timeout, output_limit=65536):
    """Absolute owner lifetime, bounded retained output, process-group cleanup.

    This is cooperative process ownership, not an OS memory/filesystem sandbox.
    Post-allocation output bounds do not imply an RSS bound.
    """
    started = time.monotonic()
    deadline = started + timeout
    proc, selector = None, selectors.DefaultSelector()
    buffers, eof = {"stdout": bytearray(), "stderr": bytearray()}, set()
    reason, retired = None, False

    def group_alive():
        nonlocal retired
        if proc is None or retired:
            return False
        try:
            os.killpg(proc.pid, 0)
            return True
        except ProcessLookupError:
            retired = True
            return False

    def pump(wait):
        nonlocal reason
        if not selector.get_map():
            time.sleep(min(wait, .01))
            return
        for key, _ in selector.select(max(0, wait)):
            data = os.read(key.fd, 8192)
            if not data:
                selector.unregister(key.fileobj)
                eof.add(key.data)
                key.fileobj.close()
            else:
                remaining = output_limit - sum(map(len, buffers.values()))
                buffers[key.data].extend(data[:max(0, remaining)])
                if len(data) > remaining:
                    reason = "output-bound"

    try:
        env = _environment(cwd)
        if time.monotonic() >= deadline:
            reason = "deadline-before-spawn"
        else:
            proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    start_new_session=True)
            for name in buffers:
                stream = getattr(proc, name)
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, name)
            while reason is None:
                if time.monotonic() >= deadline:
                    reason = "deadline"
                    break
                done = proc.poll() is not None
                if done and len(eof) == 2:
                    if group_alive():
                        reason = "remaining-owned-group"
                    break
                pump(min(.025, max(0, deadline-time.monotonic())))
            if reason is None and time.monotonic() >= deadline:
                reason = "deadline-after-result"
    except (OSError, ValueError) as error:
        reason = "controller-" + type(error).__name__
    finally:
        if proc is not None:
            if group_alive():
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    retired = True
            cleanup_deadline = time.monotonic() + 1.0
            while time.monotonic() < cleanup_deadline:
                reaped = proc.poll() is not None
                empty = not group_alive()
                if reaped and len(eof) == 2 and empty:
                    break
                pump(min(.025, max(0, cleanup_deadline-time.monotonic())))
        selector.close()
        if proc is not None:
            for name in buffers:
                getattr(proc, name).close()
    return {"argv": list(map(str, argv)), "returncode": proc.poll() if proc else None,
            "reason": reason, "stdout": buffers["stdout"].decode("utf-8", "replace"),
            "stderr": buffers["stderr"].decode("utf-8", "replace"),
            "owner_started": proc is not None,
            "owner_reaped": proc is not None and proc.poll() is not None,
            "both_output_EOF": len(eof) == 2,
            "owned_group_empty": not group_alive(),
            "elapsed_ms": (time.monotonic()-started)*1000}


def _load_upstream(upstream):
    if importlib.metadata.version("pyoxigraph") != PYOXIGRAPH_VERSION:
        raise ValueError("pyoxigraph-version-mismatch")
    source = upstream / "python/src/open_ontologies_lite/horn"
    contents = {name: _read(source/name) for name in SOURCE_HASHES}
    for name, data in contents.items():
        if sha256(data) != SOURCE_HASHES[name]:
            raise ValueError("upstream-source-hash-mismatch")
    # Bypass wider package __init__ only; every executed producer source byte is
    # exact upstream. Upstream reason.py imports the actual pinned pyoxigraph.
    for package, path in (("_pilot_upstream", source.parent),
                          ("_pilot_upstream.horn", source)):
        module = types.ModuleType(package)
        module.__path__ = [str(path)]
        sys.modules[package] = module
    loaded = {}
    for name in ("terms", "rules", "reason"):
        fullname = "_pilot_upstream.horn." + name
        module = types.ModuleType(fullname)
        module.__file__ = str(source/(name+".py"))
        module.__package__ = "_pilot_upstream.horn"
        sys.modules[fullname] = module
        exec(compile(contents[name+".py"], module.__file__, "exec"), module.__dict__)
        loaded[name] = module
    return loaded


def _triples(text):
    triples = set()
    for row in _text(text).split("\n"):
        if row:
            triples.add(_atom(row.split("\t")))
    if len(triples) > MAX_FACTS:
        raise ValueError("facts-bound")
    return triples


def _produce(case_path, directory, upstream):
    case, asserted = _case(json.loads(_read(case_path).decode()))
    modules = _load_upstream(upstream)
    out = directory / "certificate"
    out.mkdir()
    if not case["rules_text"]:
        # Upstream producer refuses zero rules. This transparent identity emitter
        # makes no conclusion: the real checker still receives empty proof/rules.
        for name, value in (("rules.tsv", ""), ("asserted.tsv", asserted), ("horn.tsv", "")):
            (out/name).write_text(value)
        stats = {"producer": "explicit-zero-rule-identity-emitter", "iterations": 0,
                 "fixpoint_reached": True, "skipped_unserialisable": 0, "steps": 0}
    else:
        rules = modules["rules"].parse_rules(case["rules_text"])
        if modules["rules"].rules_tsv(rules) != case["rules_text"]:
            raise ValueError("noncanonical-rules")
        import pyoxigraph as ox
        store = ox.Store()
        nt = "".join(" ".join(atom)+" .\n" for atom in case["facts"]).encode()
        store.load(nt, format=ox.RdfFormat.N_TRIPLES)
        result = modules["reason"].run_horn(store, rules, certificate_dir=out,
                    max_iterations=32, rules_source="public-proof-pilot-frozen-rules")
        stats = {"producer": "pinned-upstream-python-horn", "iterations": result.iterations,
                 "fixpoint_reached": result.fixpoint_reached,
                 "skipped_unserialisable": result.skipped_unserialisable,
                 "steps": len(result.steps)}
    cert = {key: _read(out/name).decode("utf-8") for key, name in (
        ("rules_tsv", "rules.tsv"), ("asserted_tsv", "asserted.tsv"), ("horn_tsv", "horn.tsv"))}
    if _triples(cert["asserted_tsv"]) != set(case["facts"]):
        raise ValueError("producer-snapshot-mismatch")
    if cert["rules_tsv"] != case["rules_text"]:
        raise ValueError("producer-rules-mismatch")
    raw_asserted_hash = sha256(cert["asserted_tsv"].encode())
    # Canonical set serialization, shared by both independent verifiers; only
    # assertion ordering changes, never membership or an actual derivation.
    cert["asserted_tsv"] = asserted
    if sum(len(cert[key].encode()) for key in cert) > MAX_BYTES:
        raise ValueError("aggregate-certificate-bound")
    if len([line for line in cert["horn_tsv"].split("\n") if line]) > MAX_STEPS:
        raise ValueError("steps-bound")
    cert["goal"] = list(case["goal"])
    cert["manifest"] = {"upstream_commit": UPSTREAM_COMMIT, "source_sha256": SOURCE_HASHES,
        "pyoxigraph_version": PYOXIGRAPH_VERSION, "stats": stats,
        "raw_upstream_asserted_sha256": raw_asserted_hash,
        "canonical_assertion_order": True,
        "component_sha256": {key: sha256(cert[key].encode()) for key in
                             ("rules_tsv", "asserted_tsv", "horn_tsv")}}
    (directory/"result.json").write_text(json.dumps(cert, sort_keys=True)+"\n")
    print(json.dumps({"produced": True, "stats": stats}, sort_keys=True))


def create_certificate(case, workdir, *, upstream_dir):
    case, _ = _case(case)
    workdir = Path(workdir).resolve()
    workdir.mkdir(parents=True, exist_ok=False)
    (workdir/"case.json").write_text(json.dumps(case, sort_keys=True)+"\n")
    run = _owned([sys.executable, "-I", "-B", str(Path(__file__).resolve()), "_produce",
                  str(workdir/"case.json"), str(workdir), str(Path(upstream_dir).resolve())],
                 workdir, 5.0)
    (workdir/"producer-run.json").write_text(json.dumps(run, sort_keys=True)+"\n")
    if (run["reason"] is not None or run["returncode"] != 0 or not run["owner_reaped"]
            or not run["both_output_EOF"] or not run["owned_group_empty"]):
        raise CandidateUnavailable("producer-attempt-failed:" + str(run["reason"] or run["returncode"]))
    cert = json.loads(_read(workdir/"result.json", 3*MAX_BYTES).decode())
    cert["manifest"]["producer_run"] = run
    return cert


def _conclusions(text):
    """Artifact goal membership only; validity is decided solely by real Lean."""
    rows = [line.split("\t") for line in _text(text).split("\n") if line]
    if len(rows) > MAX_STEPS:
        raise ValueError("steps-bound")
    conclusions = set()
    for row in rows:
        if len(row) < 5 or not row[1].isdigit() or len(row[1]) > 3:
            raise ValueError("malformed-step")
        count = int(row[1])
        offset = 2 + 2*count
        if count > 32 or len(row) < offset+3 or (len(row)-offset-3) % 3:
            raise ValueError("malformed-step")
        conclusions.add(_atom(row[offset:offset+3]))
    return conclusions


def _parser_rejection(run):
    # Exact authored HornParse/Parse diagnostics at the pinned source. Exit 2
    # also includes IO errors, so exit code alone cannot classify invalid proof.
    # Only one fixed parse diagnostic with its terminal LF and no stdout counts.
    if run["returncode"] != 2 or run["stdout"] or not run["stderr"].endswith("\n"):
        return False
    message = run["stderr"][:-1]
    patterns = (
        r"horn line [1-9][0-9]*: premises are not whole triples",
        r"horn line [1-9][0-9]*: malformed bindings or conclusion",
        r"horn line [1-9][0-9]*: rule index or bind count is not a number",
        r"horn line [1-9][0-9]*: expected index, bind count, bindings, conclusion",
        r"rules line [1-9][0-9]*: body length is not a number",
        r"rules line [1-9][0-9]*: expected [0-9]+ pattern fields, got [0-9]+",
        r"rules line [1-9][0-9]*: malformed patterns",
        r"rules line [1-9][0-9]*: expected name, body length, patterns",
        r"asserted\.tsv line [1-9][0-9]*: expected three tab-separated terms",
    )
    return any(re.fullmatch(pattern, message) for pattern in patterns)


def verify_certificate(certificate, case, checker_path, workdir, *,
                       expected_checker_sha256, timeout=2.0):
    started = time.perf_counter_ns()
    out = {"checker_invoked": False, "raw_checker_valid": None, "checker_valid": None,
           "goal_supported": False, "accepted": False, "status": "rejected", "reason": "unprocessed"}
    infrastructure = False
    try:
        if not isinstance(timeout, (int, float)) or not 0 < timeout <= 5:
            raise ValueError("checker-timeout-profile")
        case, asserted = _case(case)
        submitted = {key: _text(certificate[key]) for key in ("rules_tsv", "asserted_tsv", "horn_tsv")}
        if sum(len(value.encode()) for value in submitted.values()) > MAX_BYTES:
            raise ValueError("aggregate-certificate-bound")
        if tuple(certificate["goal"]) != case["goal"]:
            raise ValueError("goal-binding-mismatch")
        if submitted["rules_tsv"] != case["rules_text"] or submitted["asserted_tsv"] != asserted:
            raise ValueError("snapshot-binding-mismatch")
        # A malformed Horn artifact still goes to Lean, so parse-error/rejection
        # is an observed checker result. This scan cannot grant validity.
        try:
            goal_present = case["goal"] in (set(case["facts"]) | _conclusions(submitted["horn_tsv"]))
        except ValueError:
            goal_present = False
        infrastructure = True
        checker = Path(checker_path).resolve(strict=True)
        if not checker.is_file() or not os.access(checker, os.X_OK):
            raise ValueError("checker-not-executable")
        if not re.fullmatch("[0-9a-f]{64}", expected_checker_sha256):
            raise ValueError("checker-hash-required")
        if sha256(_read(checker, 128*1024*1024)) != expected_checker_sha256:
            raise ValueError("checker-hash-mismatch")
        work = Path(workdir).resolve()
        work.mkdir(parents=True, exist_ok=False)
        paths = {}
        for key, value in submitted.items():
            path = work/(key.removesuffix("_tsv")+".tsv")
            path.write_bytes(value.encode())
            paths[key] = path
        out["submitted_sha256"] = {key: sha256(value.encode()) for key, value in submitted.items()}
        out["checker_sha256"] = expected_checker_sha256
        run = _owned([str(checker), "check", str(paths["rules_tsv"]),
                      str(paths["asserted_tsv"]), str(paths["horn_tsv"])], work, timeout)
        out.update(checker_invoked=run["owner_started"], checker_run=run)
        if (run["reason"] is not None or not run["owner_reaped"] or not run["both_output_EOF"]
                or not run["owned_group_empty"]):
            out.update(status="unavailable", goal_supported=None, reason="checker-lifetime-or-output-bound")
            return out
        # All results are bound to files and executable which remain byte-exact.
        if sha256(_read(checker, 128*1024*1024)) != expected_checker_sha256:
            raise ValueError("checker-changed-during-run")
        if any(_read(paths[key]) != value.encode() for key, value in submitted.items()):
            raise ValueError("certificate-changed-during-run")
        raw = True if run["returncode"] == 0 else False if run["returncode"] == 1 else None
        out.update(raw_checker_valid=raw, checker_valid=raw)
        if raw is None:
            if _parser_rejection(run):
                out.update(checker_valid=False, reason="checker-parse-rejected",
                           raw_parse_rejected=True)
            else:
                out.update(status="unavailable", goal_supported=None, reason="checker-unreadable-or-error")
            return out
        if raw is False:
            out["reason"] = "checker-rejected"
            return out
        verdict = json.loads(run["stdout"])
        expected_theorem = "OOCert.horn_certificate_sound"
        expected_verdict = "entailed_under_supplied_rules"
        if sha256(submitted["rules_tsv"].encode()) == SOURCE_HASHES["tables/builtin_rules.tsv"]:
            expected_theorem, expected_verdict = "OOCert.entails_of_builtin_horn", "entailed"
        if verdict.get("ok") is not True or verdict.get("theorem") != expected_theorem or verdict.get("verdict") != expected_verdict:
            raise ValueError("checker-verdict-mismatch")
        out.update(goal_supported=goal_present, accepted=goal_present,
                   status="accepted" if goal_present else "rejected",
                   reason="goal-checked" if goal_present else "goal-not-established",
                   checker_verdict=verdict)
    except (ValueError, TypeError, KeyError, OSError) as error:
        out["reason"] = str(error) if isinstance(error, ValueError) else "wrapper-"+type(error).__name__
        if infrastructure or isinstance(error, OSError):
            out.update(status="unavailable", checker_valid=None, goal_supported=None)
    finally:
        out["latency_ns"] = time.perf_counter_ns()-started
    return out


def run_proof(facts, rules_text, goal, checker_path, workdir, *, upstream_dir,
              expected_checker_sha256, tamper="none"):
    if tamper != "none":
        raise ValueError("shared-caller-must-apply-identical-mutation-to-both-verifiers")
    case = {"facts": facts, "rules_text": rules_text, "goal": goal}
    work = Path(workdir)
    cert = create_certificate(case, work/"producer", upstream_dir=upstream_dir)
    return verify_certificate(cert, case, checker_path, work/"checker",
                              expected_checker_sha256=expected_checker_sha256)


if __name__ == "__main__":
    if len(sys.argv) != 5 or sys.argv[1] != "_produce":
        raise SystemExit("internal public-producer invocation only")
    _produce(Path(sys.argv[2]), Path(sys.argv[3]), Path(sys.argv[4]))
