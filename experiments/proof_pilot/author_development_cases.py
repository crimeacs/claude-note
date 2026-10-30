"""Author the 80 development fixtures. Labels are explicit authored expectations.
No solver is imported here. The 20 held-out labels are authored independently.
"""
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
cases, labels = [], {}
def t(x): return "<urn:pilot:" + x + ">"
def a(s, p="state", o="seed"): return [t(s), t(p), t(o)]
def v(s, p="state", o="seed"): return [s if s.startswith("?") else t(s), t(p), o if o.startswith("?") else t(o)]
def f(key, atom=None, **kw): return {"id": key, "atom": atom or a("item"), **kw}
def r(key, body, head): return {"id": key, "body": body, "head": head}
def base(): return [f("f1")], [r("ready", [v("?x")], v("?x", o="ready"))], a("item", o="ready")
def add(category, description, *, facts=None, rules=None, goal=None, target=None,
        context=None, mutations=None, proof=None, expected=None, why=""):
    default_facts, default_rules, default_goal = base()
    facts = default_facts if facts is None else facts
    rules = default_rules if rules is None else rules
    goal = default_goal if goal is None else goal
    case_id = "dev-" + str(len(cases)+1).zfill(3)
    case = {"id":case_id,"split":"development","category":category,"description":description,
            "facts":facts,"rules":rules,"target":{"id":"goal","atom":goal,**(target or {})},
            "context":{"repository":"public-demo","environment":"test","audiences":["host"],
                       "now":"2026-10-30T12:00:00+00:00",**(context or {})},
            "mutations":mutations or [],"proof":{"mutation":"none",**(proof or {})}}
    truth = {"target_admitted":True,"eligible_fact_ids":[x["id"] for x in facts if x.get("retrieved",True)],
             "entailed":True,"certificate_valid":True,"goal_supported":True,"authorized_use":True}
    truth.update(expected or {})
    truth["why"] = why or description
    cases.append(case); labels[case_id] = truth

# 30 domain derivations and counterexamples.
add("one-hop", "One admitted source entails one conclusion.")
add("two-hop", "A conclusion of the first rule is the second rule's premise.",
    rules=[r("middle",[v("?x")],v("?x",o="middle")),r("ready",[v("?x",o="middle")],v("?x",o="ready"))])
add("conjunction", "Two independent properties of one subject jointly establish review.",
    facts=[f("f1",a("item","risk","high")),f("f2",a("item","amount","large"))],
    rules=[r("review",[v("?x","risk","high"),v("?x","amount","large")],v("?x","review","required"))],
    goal=a("item","review","required"))
add("three-premise", "A three-way join requires all three independently admitted facts.",
    facts=[f("f1",a("item","risk","high")),f("f2",a("item","amount","large")),f("f3",a("item","country","external"))],
    rules=[r("review",[v("?x","risk","high"),v("?x","amount","large"),v("?x","country","external")],v("?x","review","required"))],
    goal=a("item","review","required"))
add("relational-join", "The account on the transfer must be the same account with a flag.",
    facts=[f("f1",a("transfer","account","acct")),f("f2",a("acct","flag","watch"))],
    rules=[r("review",[v("?x","account","?a"),v("?a","flag","watch")],v("?x","review","required"))],
    goal=a("transfer","review","required"))
add("recursive-chain", "Three ancestor links yield a recursive transitive conclusion.",
    facts=[f("f1",a("a","parent","b")),f("f2",a("b","parent","c")),f("f3",a("c","parent","d"))],
    rules=[r("base",[v("?x","parent","?y")],v("?x","ancestor","?y")),
           r("step",[v("?x","ancestor","?y"),v("?y","parent","?z")],v("?x","ancestor","?z"))],
    goal=a("a","ancestor","d"))
add("symmetry", "A supplied symmetric relation permits reversing a known relation.",
    facts=[f("f1",a("a","peer","b"))],rules=[r("sym",[v("?x","peer","?y")],v("?y","peer","?x"))],
    goal=a("b","peer","a"))
add("transitivity", "Two edges entail a transitive edge, with no equality shortcut.",
    facts=[f("f1",a("a","before","b")),f("f2",a("b","before","c"))],
    rules=[r("trans",[v("?x","before","?y"),v("?y","before","?z")],v("?x","before","?z"))],
    goal=a("a","before","c"))
add("classification-chain", "Explicit type and class relation derive a wider type.",
    facts=[f("f1",a("item","type","highRisk")),f("f2",a("highRisk","subclass","reviewable"))],
    rules=[r("sub",[v("?x","type","?c"),v("?c","subclass","?d")],v("?x","type","?d"))],
    goal=a("item","type","reviewable"))
add("inverse", "The inverse rule preserves subject/object identity.",
    facts=[f("f1",a("alice","owns","account"))],rules=[r("inverse",[v("?x","owns","?y")],v("?y","ownedBy","?x"))],
    goal=a("account","ownedBy","alice"))
add("variable-predicate", "A predicate variable is bound consistently in a two-body rule.",
    facts=[f("f1",a("item","flag","watch")),f("f2",a("flag","kind","signal"))],
    rules=[r("signal",[["?x","?p",t("watch")],["?p",t("kind"),t("signal")]],v("?x","review","required"))],
    goal=a("item","review","required"))
add("repeated-variable", "A self relation satisfies the repeated-variable pattern.",
    facts=[f("f1",a("item","same","item"))],rules=[r("self",[v("?x","same","?x")],v("?x",o="ready"))])
add("constant-head", "A fully ground rule head is allowed under the supplied rule.",
    rules=[r("constant",[v("?x")],a("item",o="ready"))])
add("copied-object", "A bound source object may appear in the conclusion.",
    facts=[f("f1",a("item","assigned","desk"))],
    rules=[r("copy",[v("?x","assigned","?y")],v("?x","reviewDesk","?y"))],goal=a("item","reviewDesk","desk"))
add("duplicate-assertion", "Duplicate source facts retain two claims but one logical triple.",
    facts=[f("f1"),f("f2")])
add("alternative-paths", "Either of two proof paths may establish the same goal.",
    facts=[f("f1"),f("f2",a("item","flag","watch"))],
    rules=[r("seed",[v("?x")],v("?x",o="ready")),r("flag",[v("?x","flag","watch")],v("?x",o="ready"))])
add("irrelevant-fact", "An unrelated admitted source must not change the result.",
    facts=[f("f1"),f("f2",a("other","state","blocked"))])
add("irrelevant-rule", "An inapplicable rule must not manufacture an unrelated conclusion.",
    rules=[r("ready",[v("?x")],v("?x",o="ready")),r("other",[v("?x","risk","high")],v("?x","review","required"))])
add("ground-rule", "A fully ground premise and conclusion can be checked.",
    rules=[r("ground",[a("item")],a("item",o="ready"))])
add("asserted-goal", "An exact admitted goal needs no inference step.",
    facts=[f("f1",a("item",o="ready"))],rules=[])
add("seeded-cycle", "A seeded positive cycle can derive the other state.",
    rules=[r("forth",[v("?x")],v("?x",o="ready")),r("back",[v("?x",o="ready")],v("?x"))])
add("asserted-goal-cycle", "An asserted goal stays established when a rule points back to itself.",
    facts=[f("f1",a("item",o="ready"))],rules=[r("loop",[v("?x",o="ready")],v("?x",o="ready"))])
add("unseeded-cycle", "Circular rules alone establish neither member.",
    facts=[f("f1",a("item",o="other"))],
    rules=[r("forth",[v("?x")],v("?x",o="ready")),r("back",[v("?x",o="ready")],v("?x"))],
    expected={"entailed":False,"goal_supported":False,"authorized_use":False})
add("missing-conjunct", "A two-premise rule cannot fire with only one premise.",
    rules=[r("join",[v("?x"),v("?x","risk","high")],v("?x",o="ready"))],
    expected={"entailed":False,"goal_supported":False,"authorized_use":False})
add("disconnected-join", "Facts about different accounts do not satisfy one joined variable.",
    facts=[f("f1",a("transfer","account","acct1")),f("f2",a("acct2","flag","watch"))],
    rules=[r("join",[v("?x","account","?a"),v("?a","flag","watch")],v("?x",o="ready"))],
    goal=a("transfer",o="ready"),expected={"entailed":False,"goal_supported":False,"authorized_use":False})
add("directionality", "An edge does not entail its reverse without a rule.",
    facts=[f("f1",a("a","before","b"))],rules=[],goal=a("b","before","a"),
    expected={"entailed":False,"goal_supported":False,"authorized_use":False})
add("repeated-variable-mismatch", "Two unequal terms cannot satisfy a repeated variable.",
    facts=[f("f1",a("a","same","b"))],rules=[r("self",[v("?x","same","?x")],v("?x",o="ready"))],
    goal=a("a",o="ready"),expected={"entailed":False,"goal_supported":False,"authorized_use":False})
add("unreachable-head", "A valid rule about another predicate does not prove the requested goal.",
    rules=[r("other",[v("?x")],v("?x",o="middle"))],
    expected={"entailed":False,"goal_supported":False,"authorized_use":False})
add("empty-facts", "No facts and a positive rule do not establish the goal.",
    facts=[],expected={"entailed":False,"goal_supported":False,"authorized_use":False})
add("partial-chain", "The chain stops before the requested final state.",
    rules=[r("middle",[v("?x")],v("?x",o="middle")),r("end",[v("?x",o="missing")],v("?x",o="ready"))],
    expected={"entailed":False,"goal_supported":False,"authorized_use":False})

# 40 current-source/admission cases, evaluated with real detached receipts.
add("expired-target","Target expired before the evaluation time.",target={"valid_until":"2026-10-29T12:00:00Z"},
    expected={"target_admitted":False,"authorized_use":False})
add("expiry-boundary","valid_until is exclusive at the exact evaluation instant.",target={"valid_until":"2026-10-30T12:00:00Z"},
    expected={"target_admitted":False,"authorized_use":False})
add("before-expiry","One second before expiry is within the target validity window.",target={"valid_until":"2026-10-30T12:00:01Z"})
add("future-target","Target's validity window has not begun.",target={"valid_from":"2026-10-30T12:00:01Z"},
    expected={"target_admitted":False,"authorized_use":False})
add("valid-from-boundary","valid_from is inclusive at the exact evaluation instant.",target={"valid_from":"2026-10-30T12:00:00Z"})
add("target-repository","A target for another repository is inapplicable.",target={"scope":{"repository":"another","environment":"test"}},
    expected={"target_admitted":False,"authorized_use":False})
add("target-environment","A production target does not apply to test.",target={"scope":{"repository":"public-demo","environment":"prod"}},
    expected={"target_admitted":False,"authorized_use":False})
add("missing-repository","Absent required repository context admits neither source nor target.",context={"repository":""},
    expected={"target_admitted":False,"eligible_fact_ids":[],"entailed":False,"goal_supported":False,"authorized_use":False})
add("missing-environment","Absent required environment context admits neither source nor target.",context={"environment":""},
    expected={"target_admitted":False,"eligible_fact_ids":[],"entailed":False,"goal_supported":False,"authorized_use":False})
add("target-audience","A host request cannot borrow a model-only target.",target={"audiences":["model"]},
    expected={"target_admitted":False,"authorized_use":False})
add("audience-intersection","A host+model request requires both audiences on every used claim.",context={"audiences":["host","model"]},
    target={"audiences":["host","model"]},expected={"eligible_fact_ids":[],"entailed":False,"goal_supported":False,"authorized_use":False})
add("allowed-audiences","Matching host+model labels satisfy the applicability contract.",context={"audiences":["host","model"]},
    target={"audiences":["host","model"]},facts=[f("f1",audiences=["host","model"])])
for category, description, extra in [
    ("expired-premise","An expired premise is excluded before proof generation.",{"valid_until":"2026-10-30T12:00:00Z"}),
    ("future-premise","A future premise is excluded before proof generation.",{"valid_from":"2026-10-31T00:00:00Z"}),
    ("cross-repository-premise","A premise from another repository cannot support this goal.",{"scope":{"repository":"another","environment":"test"}}),
    ("cross-environment-premise","A premise from another environment cannot support this goal.",{"scope":{"repository":"public-demo","environment":"prod"}}),
    ("unattested-premise","Authored approved text without a receipt is not a premise.",{"authority":"none","status":"approved"})]:
    add(category,description,facts=[f("f1",**extra)],
        expected={"eligible_fact_ids":[],"entailed":False,"goal_supported":False,"authorized_use":False})
for category, op, description in [
    ("changed-evidence","tamper_evidence","Changed source bytes invalidate the premise receipt."),
    ("deleted-evidence","delete_evidence","Deleted source evidence invalidates the premise receipt."),
    ("changed-canonical","tamper_canonical","Edited canonical claim bytes cannot borrow the old receipt."),
    ("deleted-canonical","delete_canonical","Deleted canonical claim removes its authority."),
    ("tampered-receipt","tamper_receipt","A changed receipt cannot pass its detached HMAC."),
    ("deleted-receipt","delete_receipt","A missing receipt confers no authority."),
    ("revoked-premise","revoke","Explicit revocation removes premise authority.")]:
    add(category,description,mutations=[{"op":op,"id":"f1"}],
        expected={"eligible_fact_ids":[],"entailed":False,"goal_supported":False,"authorized_use":False})
add("replaced-key","Replacing the key invalidates both old receipts.",mutations=[{"op":"replace_key","id":"f1"}],
    expected={"target_admitted":False,"eligible_fact_ids":[],"entailed":False,"goal_supported":False,"authorized_use":False})
add("changed-target-evidence","A logically supported target still needs unchanged attestation evidence.",
    mutations=[{"op":"tamper_evidence","id":"goal"}],expected={"target_admitted":False,"authorized_use":False})
add("missing-dependency","An attested target depending on an absent claim is inapplicable.",
    target={"depends_on":["missing"]},expected={"target_admitted":False,"authorized_use":False})
add("governing-dependency","An applicable attested premise can govern a dependent target.",target={"depends_on":["f1"]})
add("invalid-dependency","An expired premise cannot govern the dependent target.",
    facts=[f("f1",valid_until="2026-10-30T12:00:00Z")],target={"depends_on":["f1"]},
    expected={"target_admitted":False,"eligible_fact_ids":[],"entailed":False,"goal_supported":False,"authorized_use":False})
add("dependency-cycle","Circular dependencies invalidate source and target governance.",
    facts=[f("f1",depends_on=["goal"])],target={"depends_on":["f1"]},
    expected={"target_admitted":False,"eligible_fact_ids":[],"entailed":False,"goal_supported":False,"authorized_use":False})
add("conflicting-premises","An unresolved contradiction invalidates both otherwise attested premises.",
    facts=[f("f1",contradicts=["f2"]),f("f2",a("other",o="different"))],
    expected={"eligible_fact_ids":[],"entailed":False,"goal_supported":False,"authorized_use":False})
add("superseded-premise","An applicable replacement demotes the old premise.",
    facts=[f("f1"),f("f2",a("other",o="different"),supersedes=["f1"])],
    expected={"eligible_fact_ids":["f2"],"entailed":False,"goal_supported":False,"authorized_use":False})
add("superseded-target-status","An attested superseded target cannot be used.",target={"status":"superseded"},
    expected={"target_admitted":False,"authorized_use":False})
add("observed-not-attested","An observed target without a detached receipt remains observed.",
    target={"authority":"none","status":"observed"},expected={"target_admitted":False,"authorized_use":False})
add("proposed-not-attested","A proposed target without a receipt remains proposed.",
    target={"authority":"none","status":"proposed"},expected={"target_admitted":False,"authorized_use":False})
add("missing-target-receipt","A supported goal with a deleted target receipt is not admitted.",
    mutations=[{"op":"delete_receipt","id":"goal"}],expected={"target_admitted":False,"authorized_use":False})
add("tampered-target-receipt","A supported goal with a tampered target receipt is not admitted.",
    mutations=[{"op":"tamper_receipt","id":"goal"}],expected={"target_admitted":False,"authorized_use":False})
add("missing-target-canonical","A receipt alone cannot retain a deleted target's authority.",
    mutations=[{"op":"delete_canonical","id":"goal"}],expected={"target_admitted":False,"authorized_use":False})
add("irrelevant-invalid-premise","Deleting irrelevant evidence must not block an independent valid proof.",
    facts=[f("f1"),f("f2",a("other",o="different"))],mutations=[{"op":"delete_evidence","id":"f2"}],
    expected={"eligible_fact_ids":["f1"]})
add("alternative-after-revocation","One remaining valid proof path survives revocation of another.",
    facts=[f("f1"),f("f2",a("item","flag","watch"))],mutations=[{"op":"revoke","id":"f1"}],
    rules=[r("seed",[v("?x")],v("?x",o="ready")),r("flag",[v("?x","flag","watch")],v("?x",o="ready"))],
    expected={"eligible_fact_ids":["f2"]})

# 10 submitted-artifact adversaries, one shared proof per pair of verifiers.
for mutation, description in [
    ("rule_index_out_of_range","A step referring to a nonexistent rule is rejected."),
    ("binding_value_changed","A variable binding inconsistent with the premises is rejected."),
    ("conclusion_changed","An unlicensed conclusion cannot borrow a valid step."),
    ("premise_changed","An unstated premise cannot be cited by a step."),
    ("truncated_step","An incomplete step is not a valid derivation.")]:
    add("certificate-"+mutation,description,proof={"mutation":mutation},
        expected={"certificate_valid":False,"goal_supported":False,"authorized_use":False})
add("certificate-forward-reference","A premise established only by a later step is not established yet.",
    rules=[r("middle",[v("?x")],v("?x",o="middle")),r("ready",[v("?x",o="middle")],v("?x",o="ready"))],
    proof={"mutation":"forward_reference"},expected={"certificate_valid":False,"goal_supported":False,"authorized_use":False})
for mutation, description in [
    ("asserted_injection","A forged asserted goal cannot change the expected admitted snapshot."),
    ("rules_replacement","A swapped rule table cannot change the approved rules snapshot."),
    ("goal_binding_changed","An artifact for another target cannot satisfy the request.")]:
    add("binding-"+mutation,description,proof={"mutation":mutation},
        expected={"certificate_valid":False,"goal_supported":False,"authorized_use":False})
add("certificate-goal-omitted","A valid empty proof does not establish a derived target.",
    proof={"mutation":"drop_all_steps"},
    expected={"certificate_valid":True,"goal_supported":False,"authorized_use":False})
assert len(cases) == 80, len(cases)
(HERE/"cases_development.json").write_text(json.dumps(cases,indent=2)+"\n")
(HERE/"labels_development.json").write_text(json.dumps(labels,indent=2,sort_keys=True)+"\n")
if __name__ == "__main__": print("Authored",len(cases),"development cases; no solver was called.")
