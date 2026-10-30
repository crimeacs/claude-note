"""Real current Claude Note admission over PUBLIC, isolated fixture vaults.

API: evaluate(case: dict, workdir: pathlib.Path) -> dict. Calls must be serial.
No semantic rule/proof evaluation happens in this adapter. A real local HMAC
receipt binds canonical Markdown and evidence bytes; it does not prove the atom.

Case schema (metadata such as split/category/description/expected is ignored):
  id: text
  context: {repository:"public-demo", environment:"test",
            audiences:["host"], now:"2026-10-30T12:00:00+00:00"}
  facts: [{id, atom:[s,p,o], text?:text, authority:"approved"|"verified"|"none",
           scope:{repository:"public-demo",environment:"test"},
           audiences:["host"], status:"observed", valid_from:"",valid_until:"",
           observed_at:"",recorded_at:"",source_revision?:text,
           depends_on:[],contradicts:[],supersedes:[],retrieved:true,
           mutation:"none"|...}]
  target: {same fields, id defaults to "goal"} -- always evaluated.
  mutations: [{op, id, text?:text}] -- applied AFTER real attestations.
Aliases triple/attestation are accepted for atom/authority, but conflicting
aliases are rejected. Per-claim mutation aliases are listed in MUTATIONS.
Rules and proof are deliberately never inspected by the baseline.

Paths are generated from validated IDs, never supplied by case data. Arbitrary
derivation_sources are refused: this public pilot does not dereference external
origins. workdir must be a new or empty caller-owned directory. Its contents,
including a private fixture-only receipt.key, must not be committed/published.
No HOME environment override; fixture Path.home/expanduser and XDG are installed
before actual app imports. All mutation/Claim/attest/authority/applicability
functions are REAL. Only standard home/env lookups are patched for isolation.
"""
from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
import stat
import sys
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

DEFAULTS = {
    "repository": "public-demo",
    "environment": "test",
    "audiences": ["host"],
    "now": "2026-10-30T12:00:00+00:00",
    "authority": "approved",
    "scope": {"repository": "public-demo", "environment": "test"},
    "claim_audiences": ["host"],
    "status": "observed",
}
MUTATIONS = {
    "none": "none",
    "evidence_changed": "tamper_evidence",
    "tamper_evidence": "tamper_evidence",
    "evidence_deleted": "delete_evidence",
    "delete_evidence": "delete_evidence",
    "canonical_changed": "tamper_canonical",
    "tamper_canonical": "tamper_canonical",
    "canonical_deleted": "delete_canonical",
    "delete_canonical": "delete_canonical",
    "receipt_tampered": "tamper_receipt",
    "tamper_receipt": "tamper_receipt",
    "receipt_deleted": "delete_receipt",
    "delete_receipt": "delete_receipt",
    "receipt_revoked": "revoke",
    "revoke": "revoke",
    "key_changed": "replace_key",
    "replace_key": "replace_key",
    "key_deleted": "delete_key",
    "delete_key": "delete_key",
    "key_permissions": "key_permissions",
}
_ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}\Z")
_CHECKOUT = Path(__file__).resolve().parents[2]
_SOURCE = _CHECKOUT / "src"
_COMPONENTS = (
    "verified_claims.py", "mutation.py", "state_io.py", "egress.py", "freshness.py"
)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _alias(record: dict, first: str, second: str, default=None):
    if first in record and second in record and record[first] != record[second]:
        raise ValueError("Conflicting public case aliases")
    return record.get(first, record.get(second, default))


def _atom(record: dict) -> list[str]:
    value = _alias(record, "atom", "triple")
    if not isinstance(value, list) or len(value) != 3:
        raise ValueError("Public atom must contain exactly three string terms")
    if any(not isinstance(term, str) or not term or len(term) > 512 for term in value):
        raise ValueError("Invalid public atom term")
    return list(value)


def _claim_id(value) -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise ValueError("Invalid public fixture claim ID")
    return value


@contextmanager
def _isolation(work: Path):
    original_home = os.environ.get("HOME")
    fixture_home = work / "home"
    fixture_home.mkdir(mode=0o700)
    vault = work / "vault"
    vault.mkdir(mode=0o700)
    for relative in ("claims", "evidence"):
        (vault / relative).mkdir(mode=0o700)
    xdg = work / "config"
    (xdg / "claude-note").mkdir(parents=True, mode=0o700)
    (xdg / "claude-note/config.toml").write_text(
        "vault_root = " + json.dumps(str(vault)) + "\n", encoding="utf-8"
    )
    old_expanduser = os.path.expanduser

    def expanduser(path):
        value = os.fsdecode(os.fspath(path))
        if value == "~" or value.startswith("~/"):
            return str(fixture_home) + value[1:]
        return old_expanduser(path)

    clean = {
        key: value for key, value in os.environ.items()
        if key in ("HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "TZ")
    }
    clean.update(
        PATH="/usr/bin:/bin:/usr/sbin:/sbin",
        XDG_CONFIG_HOME=str(xdg),
        XDG_CACHE_HOME=str(work / "cache"),
        XDG_DATA_HOME=str(work / "data"),
        XDG_STATE_HOME=str(work / "state"),
        CLAUDE_NOTE_VAULT=str(vault),
        CLAUDE_NOTE_VAULT_ROOT=str(vault),
        CLAUDE_NOTE_AUTHOR="public-fixture@example.invalid",
    )
    old_bytecode = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    source_added = str(_SOURCE) not in sys.path
    if source_added:
        sys.path.insert(0, str(_SOURCE))
    try:
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(Path, "home", return_value=fixture_home))
            stack.enter_context(mock.patch.object(os.path, "expanduser", side_effect=expanduser))
            stack.enter_context(mock.patch.dict(os.environ, clean, clear=True))
            # No application import occurs before these fixture lookups exist.
            vc = importlib.import_module("claude_note.verified_claims")
            mutation = importlib.import_module("claude_note.mutation")
            if Path(vc.__file__).resolve() != (_SOURCE / "claude_note/verified_claims.py").resolve():
                raise RuntimeError("Unexpected loaded current-validator module")
            if Path(mutation.__file__).resolve() != (_SOURCE / "claude_note/mutation.py").resolve():
                raise RuntimeError("Unexpected loaded mutation module")
            yield vault, vc, mutation
            if os.environ.get("HOME") != original_home:
                raise RuntimeError("Real HOME environment changed")
    finally:
        sys.dont_write_bytecode = old_bytecode
        if source_added:
            sys.path.remove(str(_SOURCE))
        if os.environ.get("HOME") != original_home:
            raise RuntimeError("Real HOME environment was not restored")


def _claim(vc, spec, identifier, evidence_relative, evidence_digest):
    allowed = {
        "claim_type", "scope", "observed_at", "recorded_at", "valid_from",
        "valid_until", "status", "source_spans", "source_revision",
        "verification_evidence", "supersedes", "contradicts", "depends_on",
        "audiences",
    }
    if spec.get("derivation_sources"):
        raise ValueError("External derivation origins are outside this public pilot")
    atom = _atom(spec)
    fields = {
        "scope": dict(DEFAULTS["scope"]),
        "audiences": list(DEFAULTS["claim_audiences"]),
        "status": DEFAULTS["status"],
        "source_spans": [evidence_relative + "#L1"],
        "source_revision": evidence_digest,
    }
    fields.update({key: value for key, value in spec.items() if key in allowed})
    text = spec.get("text", json.dumps(atom, ensure_ascii=False, separators=(",", ":")))
    claim = vc.Claim(identifier, text, **fields).validate()
    return claim, atom


def _apply_mutation(item, records, vault, vc):
    op = MUTATIONS.get(item.get("op", item.get("mutation", "none")))
    if op is None:
        raise ValueError("Unknown public fixture mutation")
    if op == "none":
        return {"op": "none", "applied": False}
    if op in ("replace_key", "delete_key", "key_permissions"):
        key = vault / ".claude-note/claims/receipt.key"
        if op == "replace_key":
            # A fixture-only replacement, never a production/operator key.
            key.write_bytes(os.urandom(32))
            key.chmod(0o600)
        elif op == "delete_key":
            key.unlink(missing_ok=True)
        else:
            key.chmod(0o644)
        return {"op": op, "applied": True}
    identifier = _claim_id(item.get("id"))
    if identifier not in records:
        raise ValueError("Mutation names an unknown public fixture claim")
    record = records[identifier]
    evidence = record["evidence"]
    canonical = record["canonical"]
    receipt = vault / ".claude-note/claims" / (identifier + ".json")
    if op == "tamper_evidence":
        evidence.write_text(
            item.get("text", "Changed public fixture evidence.\n"), encoding="utf-8"
        )
    elif op == "delete_evidence":
        evidence.unlink()
    elif op == "tamper_canonical":
        changed = replace(record["claim"], text=item.get("text", "Changed canonical public claim."))
        canonical.write_text(changed.marker() + "\n", encoding="utf-8")
    elif op == "delete_canonical":
        canonical.unlink()
    elif op == "tamper_receipt":
        body = json.loads(receipt.read_bytes())
        body["authority"] = "verified" if body["authority"] == "approved" else "approved"
        # Retain the old signature: this is note-content poisoning, not a host compromise.
        receipt.write_text(json.dumps(body, sort_keys=True), encoding="utf-8")
    elif op == "delete_receipt":
        receipt.unlink(missing_ok=True)
    elif op == "revoke":
        vc.revoke(record["claim"], vault_root=vault, reason="Public fixture revocation")
    else:
        raise ValueError("Unsupported public fixture mutation")
    return {"op": op, "id": identifier, "applied": True}


def evaluate(case: dict, workdir: Path) -> dict:
    """Run current real admission, returning observations without reading labels."""
    if not isinstance(case, dict):
        raise ValueError("Public case must be an object")
    if len(json.dumps(case, ensure_ascii=False).encode()) > 512 * 1024:
        raise ValueError("Public case exceeds the adapter byte bound")
    facts = case.get("facts", [])
    target = case.get("target")
    if not isinstance(facts, list) or len(facts) > 300 or not isinstance(target, dict):
        raise ValueError("Public facts/target shape is invalid")
    specs = [(dict(value), False) for value in facts] + [(dict(target), True)]
    identifiers = [_claim_id(spec.get("id", "goal" if is_target else None))
                   for spec, is_target in specs]
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("Public facts and target must have distinct claim IDs")
    work = Path(workdir).absolute()
    if work.is_symlink():
        raise ValueError("Public fixture workdir cannot be a symlink")
    work = work.resolve()
    if work.exists():
        if not work.is_dir() or work.stat().st_uid != os.getuid() or any(work.iterdir()):
            raise ValueError("Use a new/empty caller-owned public fixture workdir")
    else:
        work.mkdir(mode=0o700, parents=True)
    work.chmod(0o700)
    components = {
        name: _digest(_SOURCE / "claude_note" / name) for name in _COMPONENTS
    }
    context = {
        "repository": DEFAULTS["repository"],
        "environment": DEFAULTS["environment"],
        "audiences": list(DEFAULTS["audiences"]),
        "now": DEFAULTS["now"],
    }
    context.update(case.get("context", {}))
    now = datetime.fromisoformat(context["now"].replace("Z", "+00:00"))
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    context["now"] = now.isoformat()
    records, mutation_receipts = {}, []
    with _isolation(work) as (vault, vc, mutation):
        for (spec, is_target), identifier in zip(specs, identifiers):
            evidence = vault / "evidence" / (identifier + ".md")
            body = spec.get("evidence_text", "Public synthetic source for " + identifier + ".\n")
            if not isinstance(body, str) or len(body.encode()) > 16384:
                raise ValueError("Public evidence must be bounded text")
            evidence_relative = evidence.relative_to(vault).as_posix()
            # Exercise the real confined publication and ownership/lineage writer.
            evidence_result = mutation.create_note(
                evidence, body, vault_root=vault, operation_id="pilot-evidence-" + identifier
            )
            if not evidence_result.applied:
                raise RuntimeError("Public evidence publication was staged")
            claim, atom = _claim(vc, spec, identifier, evidence_relative, _digest(evidence))
            canonical = vault / "claims" / (identifier + ".md")
            canonical_result = mutation.create_note(
                canonical, claim.marker() + "\n", vault_root=vault,
                operation_id="pilot-claim-" + identifier
            )
            if not canonical_result.applied:
                raise RuntimeError("Public canonical claim publication was staged")
            grant = _alias(spec, "authority", "attestation", DEFAULTS["authority"])
            if grant not in ("approved", "verified", "none"):
                raise ValueError("Unknown public attestation authority")
            if grant != "none":
                vc.attest(
                    claim, vault_root=vault, authority=grant, claim_path=canonical,
                    evidence=[{
                        "path": evidence_relative,
                        "revision": _digest(evidence),
                        "verifier": "public-fixture-provenance-only",
                    }],
                )
            records[identifier] = {
                "claim": claim, "atom": atom, "canonical": canonical, "evidence": evidence,
                "retrieved": True if is_target else bool(spec.get("retrieved", True)),
                "is_target": is_target, "authority_requested": grant,
            }
        for spec, is_target in specs:
            item = spec.get("mutation", "none")
            if item != "none":
                item = {"op": item} if isinstance(item, str) else dict(item)
                item.setdefault("id", spec.get("id", "goal" if is_target else None))
                mutation_receipts.append(_apply_mutation(item, records, vault, vc))
        for item in case.get("mutations", []):
            mutation_receipts.append(_apply_mutation(dict(item), records, vault, vc))
        queried = [record["claim"].marker() for record in records.values() if record["retrieved"]]
        groups = vc.applicability(
            "\n".join(queried), vault_root=vault,
            repository=context["repository"], environment=context["environment"],
            audiences=context["audiences"], now=now
        )
        by_id = {}
        for group, rows in groups.items():
            for row in rows:
                by_id[row["claim_id"]] = {
                    "group": group, "verification": row["verification"],
                    "reason_codes": list(row["reason_codes"]),
                    "claim_digest": records[row["claim_id"]]["claim"].digest(),
                    "retrieved": True,
                }
        # Non-retrieved canonical receipts still affect real related governance,
        # but their atoms do not become proof premises merely by being on disk.
        for identifier, record in records.items():
            if identifier not in by_id:
                by_id[identifier] = {
                    "group": "not_retrieved",
                    "verification": vc.authority(record["claim"], vault),
                    "reason_codes": [], "claim_digest": record["claim"].digest(),
                    "retrieved": False,
                }
        target_id = identifiers[-1]
        admitted = by_id[target_id]["group"] == "accepted"
        eligible = sorted(
            identifier for identifier, record in records.items()
            if not record["is_target"] and record["retrieved"]
            and by_id[identifier]["group"] == "accepted"
        )
        manifest = []
        for identifier, record in records.items():
            entry = {"id": identifier}
            for kind, path in (
                ("canonical", record["canonical"]), ("evidence", record["evidence"]),
                ("receipt", vault / ".claude-note/claims" / (identifier + ".json")),
            ):
                entry[kind] = {
                    "path": path.relative_to(work).as_posix(),
                    "exists": path.exists(),
                    "sha256": _digest(path) if path.is_file() else None,
                }
            manifest.append(entry)
    if components != {name: _digest(_SOURCE / "claude_note" / name) for name in _COMPONENTS}:
        raise RuntimeError("Current admission component changed during evaluation")
    result = {
        "case_id": str(case.get("id", "")),
        "current_target_accepted": admitted,
        "target_admitted": admitted,
        "eligible_fact_ids": eligible,
        "eligible_atoms": [records[identifier]["atom"] for identifier in eligible],
        "per_claim": by_id,
        "reason_codes_by_id": {
            identifier: value["reason_codes"] for identifier, value in by_id.items()
        },
        "actual_validator_sha256": components["verified_claims.py"],
        "actual_component_sha256": components,
        "context": context,
        "defaults": json.loads(json.dumps(DEFAULTS)),
        "fixture_manifest": manifest,
        "fixture_mutations": mutation_receipts,
        "fixture_provenance": {
            "real_claim_markers": True, "real_detached_hmac": True,
            "real_confined_mutation": True, "real_authority_and_applicability": True,
            "evidence_verifier": "public-fixture-provenance-only",
            "HOME_environment_override": False,
            "no_private_source_or_model_or_index_access": True,
            "private_fixture_key_not_exported": True,
        },
        "semantic_entailment_checked": False,
        "scope_limit": (
            "Current local provenance/applicability baseline only. Source-stable "
            "attested domain assertions can be logically bogus; this adapter does "
            "not inspect rules, proof, expected labels or domain validity."
        ),
    }
    (work / "current-admission.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return result
