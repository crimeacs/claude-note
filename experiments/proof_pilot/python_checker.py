"""Independent, deliberately small executable verifier for the pilot Horn subset.

This does not import the upstream proposer, Lean checker, or expected answers.
Like the Lean executable it checks a finite submitted derivation, not completeness.
"""
from __future__ import annotations

import hashlib
import re
import time

MAX_BYTES = 256 * 1024
MAX_STEPS = 256
MAX_RULES = 32
MAX_FACTS = 256


def _rows(text):
    if (not isinstance(text, str) or len(text.encode()) > MAX_BYTES or not text.isascii()
            or any((ord(c)<32 and c not in "\n\t") or ord(c)==127 for c in text)):
        raise ValueError("unsupported-encoding-or-size")
    return [line.split("\t") for line in text.splitlines() if line]


def _nat(field):
    if not field.isascii() or not field.isdecimal():
        raise ValueError("invalid-natural")
    return int(field)


def _term(value):
    # Intentional supported pilot profile, not a complete RDF parser.
    if not isinstance(value,str) or re.fullmatch(r"<urn:pilot:[A-Za-z0-9:._~-]+>",value) is None:
        raise ValueError("unsupported-term")
    return value


def _pattern(value):
    if value.startswith("?"):
        if not value[1:].isascii() or not value[1:].isidentifier():
            raise ValueError("unsupported-variable")
        return value
    return _term(value)


def _rules(text):
    parsed = []
    for row in _rows(text):
        if len(row) < 5:
            raise ValueError("truncated-rule")
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", row[0]) is None:
            raise ValueError("unsupported-rule-name")
        count = _nat(row[1])
        if count > 8 or len(row) != 2 + 3 * count + 3:
            raise ValueError("rule-arity")
        body = [tuple(map(_pattern, row[i:i+3])) for i in range(2, 2+3*count, 3)]
        head = tuple(map(_pattern, row[-3:]))
        parsed.append((body, head))
    if len(parsed) > MAX_RULES:
        raise ValueError("too-many-rules")
    return parsed


def _facts(text):
    facts = set()
    for row in _rows(text):
        if len(row) != 3:
            raise ValueError("fact-arity")
        facts.add(tuple(map(_term, row)))
    if len(facts) > MAX_FACTS:
        raise ValueError("too-many-facts")
    return facts


def verify_certificate(cert, case, **_):
    """Same submitted artifact and expected snapshot as the Lean wrapper.

    A complete but irrelevant proof may check; goal binding still has to pass.
    No certificate files are trusted as the expected rules or asserted snapshot.
    """
    start = time.perf_counter_ns()
    out = {"checker_valid": False, "goal_supported": False, "accepted": False,
           "status": "rejected", "reason": "unprocessed"}
    try:
        if sum(len(cert[k].encode()) for k in ("rules_tsv","asserted_tsv","horn_tsv"))>MAX_BYTES:
            raise ValueError("aggregate-certificate-bound")
        rules_text = case["rules_text"]
        facts_text = "".join("\t".join(fact) + "\n" for fact in sorted(set(map(tuple, case["facts"]))))
        goal = tuple(case["goal"])
        if cert["rules_tsv"] != rules_text or cert["asserted_tsv"] != facts_text:
            raise ValueError("snapshot-binding-mismatch")
        if tuple(cert["goal"]) != goal:
            raise ValueError("goal-binding-mismatch")
        rules, known = _rules(rules_text), _facts(facts_text)
        for field in goal:
            _term(field)
        rows = _rows(cert["horn_tsv"])
        if len(rows) > MAX_STEPS:
            raise ValueError("too-many-steps")
        for row in rows:
            if len(row) < 5:
                raise ValueError("truncated-step")
            index, count = _nat(row[0]), _nat(row[1])
            if index >= len(rules) or count > 32:
                raise ValueError("rule-or-binding-limit")
            body, head = rules[index]
            if len(row) != 2 + 2 * count + 3 + 3 * len(body):
                raise ValueError("step-arity")
            binds = {}
            for i in range(2, 2 + 2 * count, 2):
                key, val = row[i:i+2]
                if not key.isascii() or not key.isidentifier() or key in binds:
                    raise ValueError("duplicate-or-invalid-binding")
                binds[key] = _term(val)
            offset = 2 + 2 * count
            conclusion = tuple(map(_term, row[offset:offset+3]))
            premises = [tuple(map(_term, row[j:j+3]))
                        for j in range(offset+3, len(row), 3)]
            def instantiate(atom):
                return tuple(binds[value[1:]] if value.startswith("?") else value for value in atom)
            expected_body = [instantiate(atom) for atom in body]
            if premises != expected_body or conclusion != instantiate(head):
                raise ValueError("substitution-mismatch")
            if any(premise not in known for premise in premises):
                raise ValueError("premise-not-established")
            known.add(conclusion)
        out.update(checker_valid=True, goal_supported=goal in known,
                   accepted=goal in known, reason="goal-checked" if goal in known else "goal-not-established")
        out["status"] = "accepted" if out["accepted"] else "rejected"
    except (ValueError, TypeError, KeyError, IndexError) as error:
        out["reason"] = str(error) if isinstance(error, ValueError) else "malformed-certificate"
    out["latency_ns"] = time.perf_counter_ns() - start
    return out


def serialize_rules(rules):
    lines = []
    for rule in rules:
        body, head = rule["body"], rule["head"]
        fields = [rule["id"], str(len(body)), *(term for atom in body for term in atom), *head]
        if any("\t" in x or "\n" in x or "\r" in x for x in fields):
            raise ValueError("unserializable-field")
        lines.append("\t".join(fields) + "\n")
    return "".join(lines)


def sha256(text):
    return hashlib.sha256(text.encode()).hexdigest()
