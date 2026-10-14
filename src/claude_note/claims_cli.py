"""Explicit owner inspection/attestation. Never called by automatic extraction."""

import hashlib
import json
from pathlib import Path

from . import verified_claims


def add_parser(sub):
    parser = sub.add_parser("claim", help="Inspect and explicitly attest scoped claim records")
    parser.add_argument("action", choices=("inspect", "attest", "revoke"))
    parser.add_argument("path", help="Markdown claim note relative to the vault")
    parser.add_argument("--vault", help="Vault root; defaults to the configured vault")
    parser.add_argument("--claim-id", default="")
    parser.add_argument("--authority", choices=("verified", "approved"), default="verified")
    parser.add_argument("--evidence", action="append", default=[], help="Inspectable local evidence file, repeatable")
    parser.add_argument("--confirm-digest", default="", help="Exact digest shown by inspect; confirms the reviewed claim")
    parser.add_argument("--reason", default="explicit-owner-revocation")


def main(args):
    from . import mutation
    if args.vault:
        root = Path(args.vault).expanduser().resolve()
    else:
        from . import config
        root = Path(config.VAULT_ROOT).resolve()
    text = mutation.read_text(args.path, vault_root=root, allow_protected=True)
    claims = [c for c in verified_claims.parse(text) if not args.claim_id or c.claim_id == args.claim_id]
    if not claims:
        raise ValueError("No valid claim found")
    if args.action == "inspect":
        from dataclasses import asdict
        print(json.dumps([{**asdict(c), "digest": c.digest(), "authority": verified_claims.authority(c, root)} for c in claims], indent=2))
        return 0
    if len(claims) != 1 or args.confirm_digest != claims[0].digest():
        raise ValueError("Inspect one claim and supply its exact --confirm-digest before attesting or revoking")
    claim = claims[0]
    if args.action == "revoke":
        verified_claims.revoke(claim, vault_root=root, reason=args.reason)
    else:
        evidence = []
        for source in args.evidence:
            path = mutation.resolve_note_path(source, root, allow_protected=True)
            body = mutation.read_text(path, vault_root=root, allow_protected=True)
            evidence.append({"path": str(path.relative_to(root)), "revision": hashlib.sha256(body.encode()).hexdigest(),
                             "verifier": "explicit-owner-inspection"})
        verified_claims.attest(claim, vault_root=root, authority=args.authority, evidence=evidence, claim_path=args.path)
    print(json.dumps({"claim_id": claim.claim_id, "authority": verified_claims.authority(claim, root)}))
    return 0
