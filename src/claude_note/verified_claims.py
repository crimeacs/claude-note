"""Markdown claim records with applicability and detached local attestations.

Markdown is canonical. Receipts attest an exact claim and evidence revision;
status text, assistant identity and statements of approval never confer authority.
The receipt key protects against note-content poisoning, not a compromised host.
"""

import hashlib
import hmac
import json
import os
import re
import stat
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .state_io import atomic_json, read_json

MARKER = re.compile(r"(?m)^<!-- claude-note:claim (\{[^\n]*\}) -->\s*$")
STATUSES = {"proposed", "observed", "verified", "approved", "superseded"}
RELATIONS = {"supersedes", "contradicts", "depends_on"}


@dataclass
class Claim:
    claim_id: str
    text: str
    claim_type: str = "lesson"
    scope: dict = field(default_factory=dict)
    observed_at: str = ""
    recorded_at: str = ""
    valid_from: str = ""
    valid_until: str = ""
    status: str = "proposed"
    source_spans: list[str] = field(default_factory=list)
    source_revision: str = ""
    verification_evidence: list[dict] = field(default_factory=list)
    supersedes: list[str] = field(default_factory=list)
    contradicts: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    audiences: list[str] = field(default_factory=lambda: ["host"])
    derivation_sources: list[str] = field(default_factory=list)

    def validate(self):
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}", self.claim_id):
            raise ValueError("Invalid claim ID")
        if not isinstance(self.text, str) or not self.text.strip() or len(self.text) > 16000:
            raise ValueError("Invalid claim text")
        if self.status not in STATUSES or not isinstance(self.scope, dict):
            raise ValueError("Invalid claim status/scope")
        for key in ("source_spans", "audiences", "derivation_sources", *RELATIONS):
            value = getattr(self, key)
            if not isinstance(value, list) or len(value) > (2000 if key == "derivation_sources" else 200) or any(not isinstance(v, str) for v in value):
                raise ValueError(f"Invalid claim {key}")
        if not isinstance(self.verification_evidence, list) or len(self.verification_evidence) > 100:
            raise ValueError("Invalid verification evidence")
        for value in (self.valid_from, self.valid_until, self.observed_at, self.recorded_at):
            if not isinstance(value, str):
                raise ValueError("Claim dates must be text")
            if value:
                _date(value)
        return self

    def digest(self):
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()

    def marker(self):
        self.validate()
        payload = json.dumps(asdict(self), sort_keys=True, ensure_ascii=False).replace("-->", "\\u002d\\u002d>")
        return "<!-- claude-note:claim " + payload + " -->"


def _date(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def parse(text):
    claims = []
    for match in MARKER.finditer(text):
        try:
            claims.append(Claim(**json.loads(match[1])).validate())
        except (ValueError, TypeError):
            continue
    return claims


def _state(root):
    root = Path(root).resolve()
    state = root / ".claude-note" / "claims"
    for path in (root, root / ".claude-note", state):
        if path.is_symlink():
            raise ValueError("Claim state cannot be symlinked")
    return state


def _key(root, create=False):
    from .mutation import _parent
    _state(root)
    with _parent(Path(root).resolve(), Path(".claude-note/claims/receipt.key"), create=create) as (parent, name):
        if create:
            try:
                descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(os.urandom(32))
                    handle.flush()
                    os.fsync(handle.fileno())
                os.fsync(parent)
            except FileExistsError:
                pass
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(descriptor, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size != 32 or info.st_mode & 0o077:
                raise ValueError("Claim receipt key must be a private, singly linked 32-byte regular file")
            key = handle.read(33)
    if len(key) != 32:
        raise ValueError("Invalid receipt key")
    return key


def _evidence(root, evidence):
    """Evidence must resolve to an unchanged, real file inside the owned vault."""
    from .mutation import read_text, resolve_note_path
    path = resolve_note_path(evidence["path"], Path(root), allow_protected=True)
    body = read_text(path, vault_root=Path(root), allow_protected=True)
    if body is None:
        raise ValueError("Verification evidence is missing")
    actual = hashlib.sha256(body.encode()).hexdigest()
    if actual != evidence.get("revision"):
        raise ValueError("Evidence revision changed")
    return {"path": str(path.relative_to(Path(root).resolve())), "revision": actual,
            "verifier": str(evidence.get("verifier", "human-inspection"))}


def attest(claim, *, vault_root, authority, evidence, claim_path=None):
    """Explicit local attestation, separate from extraction; never called by a model.

    `authority` is verified (observed check) or approved (owner's policy choice).
    Calling tools need to establish who authorized this action. It is not inferred
    from transcript text or an assistant's claim that a test passed.
    """
    claim.validate()
    if authority not in {"verified", "approved"} or not evidence:
        raise ValueError("Attestation requires authority and inspectable evidence")
    from .mutation import read_text, resolve_note_path
    canonical = resolve_note_path(claim_path or f"claims/{claim.claim_id}.md", Path(vault_root), allow_protected=True)
    if not any(c.digest() == claim.digest() for c in parse(read_text(canonical, vault_root=Path(vault_root), allow_protected=True))):
        raise ValueError("Attestation requires the exact canonical Markdown claim")
    checked = [_evidence(Path(vault_root), item) for item in evidence]
    body = {"version": 1, "claim_id": claim.claim_id, "claim_digest": claim.digest(),
            "authority": authority, "evidence": checked, "claim": asdict(claim),
            "claim_path": str(canonical.relative_to(Path(vault_root).resolve())),
            "recorded_at": datetime.now(timezone.utc).isoformat()}
    signature = hmac.new(_key(vault_root, True), json.dumps(body, sort_keys=True).encode(), hashlib.sha256).hexdigest()
    atomic_json(_state(vault_root) / (claim.claim_id + ".json"), {**body, "signature": signature})
    return body


def authority(claim, vault_root):
    try:
        receipt = read_json(_state(vault_root) / (claim.claim_id + ".json"))
        signature = receipt.pop("signature")
        expected = hmac.new(_key(vault_root), json.dumps(receipt, sort_keys=True).encode(), hashlib.sha256).hexdigest()
        if receipt.get("revoked") or not hmac.compare_digest(signature, expected) or receipt["claim_digest"] != claim.digest():
            return "unverified"
        from .mutation import read_text
        canonical = read_text(receipt["claim_path"], vault_root=Path(vault_root), allow_protected=True)
        if not any(c.digest() == claim.digest() for c in parse(canonical)):
            return "unverified"
        for item in receipt["evidence"]:
            _evidence(Path(vault_root), item)
        return receipt["authority"] if receipt["authority"] in {"verified", "approved"} else "unverified"
    except (OSError, ValueError, KeyError, TypeError):
        return "unverified"


def revoke(claim, *, vault_root, reason):
    receipt = read_json(_state(vault_root) / (claim.claim_id + ".json"))
    receipt.pop("signature", None)
    receipt.update(revoked=True, revocation_reason=reason, revoked_at=datetime.now(timezone.utc).isoformat())
    receipt["signature"] = hmac.new(_key(vault_root), json.dumps(receipt, sort_keys=True).encode(), hashlib.sha256).hexdigest()
    atomic_json(_state(vault_root) / (claim.claim_id + ".json"), receipt)


def _reasons(claim, repository, environment, audiences, now):
    reasons = []
    for key, context in (("repository", repository), ("environment", environment)):
        required = claim.scope.get(key)
        if required and required != context:
            reasons.append("unknown-" + key if not context else "scope-" + key)
    if claim.valid_from and now < _date(claim.valid_from):
        reasons.append("not-yet-valid")
    if claim.valid_until and now >= _date(claim.valid_until):
        reasons.append("expired")
    if not set(audiences).issubset(claim.audiences):
        reasons.append("audience-denied")
    return reasons


def _related_attestations(vault_root):
    # A bounded derived registry: receipts bind canonical Markdown paths and
    # digests, so deleted/edited claims cannot silently retain authority.
    state = _state(vault_root)
    paths = sorted(state.glob("*.json"))
    partial = len(paths) > 256
    claims = []
    for path in paths[:256]:
        try:
            receipt = read_json(path)
            claim = Claim(**receipt["claim"]).validate()
            if authority(claim, vault_root) in {"verified", "approved"}:
                claims.append(claim)
        except (OSError, ValueError, TypeError, KeyError):
            continue
    return claims, partial


def _relation_edges(records, available):
    # Influence runs prerequisite -> dependent and replacement -> replaced.
    # A mixed cycle is just as unsafe as circular support or succession: no
    # member may use that cycle to demote an unrelated approved claim.
    edges = {key: set() for key in available}
    for key in available:
        for dependency in records[key].depends_on:
            if dependency in available:
                edges[dependency].add(key)
        edges[key].update(target for target in records[key].supersedes if target in available)
    return edges


def _relation_cycles(edges):
    reverse = {key: set() for key in edges}
    for key, targets in edges.items():
        for target in targets:
            reverse[target].add(key)
    visited, finished = set(), []
    for start in sorted(edges):
        stack = [(start, False)]
        while stack:
            key, expanded = stack.pop()
            if expanded:
                finished.append(key)
            elif key not in visited:
                visited.add(key)
                stack.append((key, True))
                stack.extend((target, False) for target in sorted(edges[key]) if target not in visited)
    visited, cyclic = set(), set()
    for start in reversed(finished):
        if start in visited:
            continue
        component, stack = set(), [start]
        while stack:
            key = stack.pop()
            if key in visited:
                continue
            visited.add(key)
            component.add(key)
            stack.extend(reverse[key] - visited)
        if len(component) > 1 or start in edges[start]:
            cyclic.update(component)
    return cyclic


def _governance(records, potential):
    edges = _relation_edges(records, potential)
    cyclic = _relation_cycles(edges)
    pending = {key: 0 for key in potential - cyclic}
    for key in pending:
        for target in edges[key] - cyclic:
            pending[target] += 1
    ready = [key for key, count in pending.items() if count == 0]
    governing, superseded, invalidated, issuers = set(), set(), set(), set()
    while ready:
        key = ready.pop()
        # All prerequisite and replacement influences are already resolved.
        # Historical replacements retain demotions after being replaced while
        # their premises remain valid: A -> B -> C cannot revive C. An invalid
        # dependent emits no effects, so it cannot demote an unrelated claim.
        if set(records[key].depends_on).issubset(governing):
            issuers.add(key)
            superseded.update(records[key].supersedes)
            if key not in superseded:
                governing.add(key)
        else:
            invalidated.add(key)
        for target in edges[key] - cyclic:
            pending[target] -= 1
            if pending[target] == 0:
                ready.append(target)
    return governing, superseded, cyclic, invalidated, issuers


def _consistent_governance(records, potential):
    cyclic = _relation_cycles(_relation_edges(records, potential))
    conflicts = {}
    while True:
        governing, superseded, _, invalidated, issuers = _governance(records, potential - cyclic - conflicts.keys())
        superseders = {}
        for key in issuers:
            for target in records[key].supersedes:
                superseders.setdefault(target, set()).add(key)
        ancestors = {}
        def premises(key):
            if key not in ancestors:
                seen, stack = set(), list(records[key].depends_on)
                while stack:
                    dependency = stack.pop()
                    if dependency in seen:
                        continue
                    seen.add(dependency)
                    if dependency in records:
                        stack.extend(records[dependency].depends_on)
                ancestors[key] = seen
            return ancestors[key]
        unresolved = {}
        for key in issuers:
            for target in set(records[key].contradicts) & issuers:
                if target == key:
                    continue
                pair = {key, target}
                # An explicit, evidenced historical replacement resolves a
                # contradiction only if its premises do not assume either
                # side of that same unresolved conflict.
                resolved = any(not (premises(issuer) & pair)
                               for endpoint in pair for issuer in superseders.get(endpoint, ()))
                if not resolved:
                    unresolved.setdefault(key, set()).add(target)
                    unresolved.setdefault(target, set()).add(key)
        if not unresolved:
            return governing, superseded, cyclic, invalidated, conflicts
        for key, targets in unresolved.items():
            conflicts.setdefault(key, set()).update(targets)


def applicability(text, *, vault_root, repository="", environment="", audiences=("host",), now=None):
    now = now or datetime.now(timezone.utc)
    groups = {key: [] for key in ("accepted", "observed", "proposed", "denied")}
    records = parse(text)
    related, partial = _related_attestations(vault_root)
    all_records = {c.claim_id: c for c in related}
    # A same-ID marker with different bytes cannot borrow a canonical receipt,
    # nor replace its authoritative relation graph merely by being retrieved.
    record_authority = {c.digest(): authority(c, vault_root) for c in records}
    for claim in records:
        if claim.claim_id not in all_records or record_authority[claim.digest()] in {"verified", "approved"}:
            all_records[claim.claim_id] = claim
    attested = {key: authority(c, vault_root) for key, c in all_records.items()}
    applicable = {key for key, c in all_records.items() if not _reasons(c, repository, environment, audiences, now)}
    potential = {key for key in applicable if attested[key] in {"verified", "approved"} and all_records[key].status != "superseded"}
    governing, superseded, cyclic, invalidated, conflicts = _consistent_governance(all_records, potential)
    for claim in records:
        actual_authority = record_authority[claim.digest()]
        reasons = _reasons(claim, repository, environment, audiences, now)
        if claim.digest() != all_records[claim.claim_id].digest():
            reasons.append("claim-id-conflict")
        if claim.claim_id in cyclic:
            reasons.append("relation-cycle")
        if claim.claim_id in invalidated:
            reasons.append("dependency-not-governing")
        if claim.claim_id in superseded or (claim.status == "superseded" and actual_authority != "unverified"):
            reasons.append("superseded")
        for dependency in claim.depends_on:
            if dependency not in governing or claim.claim_id == dependency:
                reasons.append("dependency-unverified:" + dependency)
        contradictions = set(claim.contradicts) | {key for key, c in all_records.items() if claim.claim_id in c.contradicts}
        unresolved = set(conflicts.get(claim.claim_id, ())) | {key for key in contradictions if key in governing and key != claim.claim_id}
        if unresolved:
            reasons.append("unresolved-contradiction:" + ",".join(sorted(unresolved)))
        if partial:
            reasons.append("relation-coverage-partial")
        record = {**asdict(claim), "verification": actual_authority, "reason_codes": reasons}
        group = "denied" if reasons else "accepted" if actual_authority in {"verified", "approved"} else "observed" if claim.status == "observed" else "proposed"
        groups[group].append(record)
    return groups
