"""Run all paired public cases, persist predictions BEFORE opening label files."""
from __future__ import annotations
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import tempfile
import time
import traceback

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from python_checker import serialize_rules, verify_certificate as python_verify
from current_validator import evaluate

def digest(data): return hashlib.sha256(data).hexdigest()
def write_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name+".tmp")
    with temporary.open("w") as stream:
        stream.write(json.dumps(obj, sort_keys=True, indent=2) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary,path)
    descriptor=os.open(path.parent,os.O_RDONLY)
    try: os.fsync(descriptor)
    finally: os.close(descriptor)

def append_attempt(path,row):
    with path.open("a") as stream:
        stream.write(json.dumps(row,sort_keys=True)+"\n")
        stream.flush()
        os.fsync(stream.fileno())

def mutate_certificate(original, kind):
    cert = copy.deepcopy(original)
    rows = [x.split("\t") for x in cert["horn_tsv"].splitlines() if x]
    if kind == "none": return cert
    if kind == "asserted_injection":
        cert["asserted_tsv"] += "\t".join(cert["goal"]) + "\n"
    elif kind == "rules_replacement":
        # Preserve a well-formed table, but change its head to an unauthorized one.
        fields = cert["rules_tsv"].splitlines()[0].split("\t")
        fields[-1] = "<urn:pilot:forged>"
        cert["rules_tsv"] = "\t".join(fields) + "\n"
    elif kind == "goal_binding_changed":
        cert["goal"] = ["<urn:pilot:other>", *cert["goal"][1:]]
    elif kind == "drop_all_steps":
        cert["horn_tsv"] = ""
    else:
        if not rows: raise ValueError("mutation-requires-a-derived-step")
        row = rows[0]
        offset = 2 + 2 * int(row[1])
        if kind == "rule_index_out_of_range": row[0] = "999"
        elif kind == "binding_value_changed": row[3] = "<urn:pilot:forged>"
        elif kind == "conclusion_changed": row[offset+2] = "<urn:pilot:forged>"
        elif kind == "premise_changed": row[offset+5] = "<urn:pilot:forged>"
        elif kind == "truncated_step": del row[-1]
        elif kind == "forward_reference":
            if len(rows) < 2: raise ValueError("mutation-requires-two-steps")
            rows = list(reversed(rows))
        else: raise ValueError("unknown-proof-mutation")
        cert["horn_tsv"] = "".join("\t".join(row) + "\n" for row in rows)
    return cert

def error_record(stage, error):
    # Inputs are public, but keep deterministic fixed diagnostic categories.
    return {"status":"unavailable","accepted":False,"checker_valid":None,
            "goal_supported":None,"reason":stage+":"+type(error).__name__}

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--upstream",type=Path)
    parser.add_argument("--checker",type=Path)
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--baseline-only",action="store_true")
    args=parser.parse_args()
    if args.output.exists() and any(args.output.iterdir()):
        parser.error("Refusing to overwrite an existing attempt directory")
    args.output.mkdir(parents=True,exist_ok=True)
    paths = [HERE/"cases_development.json", HERE/"cases_heldout.json"]
    if args.baseline_only: paths = [p for p in paths if p.exists()]
    cases=[case for p in paths for case in json.loads(p.read_text())]
    if not args.baseline_only and len(cases)!=100:
        raise ValueError("Expected exactly 100 frozen cases")
    ids=[c["id"] for c in cases]
    if len(ids)!=len(set(ids)): raise ValueError("Duplicate case identity")
    # Opaquely hash source/input/answerkey bytes before execution; do not parse answerkeys.
    tracked=[p for p in HERE.iterdir() if p.is_file() and p.suffix in {".py",".json",".sh",".md"}]
    components={p.name:digest(p.read_bytes()) for p in sorted(tracked)}
    commit=subprocess.check_output(["git","rev-parse","HEAD"],cwd=HERE,text=True).strip()
    checker_hash=digest(args.checker.read_bytes()) if args.checker else None
    manifest={"format":1,"source_commit":commit,"python":sys.version,"platform":platform.platform(),
              "components":components,"case_ids":ids,"case_count":len(cases),
              "truth_contract":"same frozen inputs and paired submitted certificate; ground entailment, source admission, and goal-certificate validity separate",
              "provider_calls":0,"model_spend_usd":0,"checker_sha256":checker_hash,
              "production_services_changed":False,"baseline_only":args.baseline_only}
    write_json(args.output/"attempt.json",manifest)
    if not args.baseline_only:
        if not args.upstream or not args.checker: parser.error("Real upstream and checker required")
        from lean_candidate import create_certificate, verify_certificate as lean_verify
    outcomes=[]
    # A TemporaryDirectory holds only constructed public fixtures.
    with tempfile.TemporaryDirectory(prefix="claude-note-proof-public-") as temporary:
        work=Path(temporary)
        for case in cases:
            row={"id":case["id"],"split":case["split"],"category":case["category"],
                 "case_sha256":digest(json.dumps(case,sort_keys=True).encode())}
            started=time.perf_counter_ns()
            try:
                baseline=evaluate(case,work/case["id"]/"vault")
                row["current"] = baseline
                row["current"]["latency_ns"] = time.perf_counter_ns()-started
                flattened={"facts":baseline["eligible_atoms"],"rules_text":serialize_rules(case["rules"]),
                           "goal":case["target"]["atom"]}
                row["expected_snapshot"] = flattened
            except Exception as error:
                row["current"]=error_record("current",error)
                row["exception_trace"]=traceback.format_exc()
                if not args.baseline_only:
                    for arm in ("python","lean"):
                        row[arm]=error_record("current-admission",error)
                outcomes.append(row)
                append_attempt(args.output/"predictions.jsonl",row)
                write_json(args.output/"predictions.json",outcomes)
                continue
            if not args.baseline_only:
                cert=None
                try:
                    begin=time.perf_counter_ns()
                    cert=create_certificate(flattened,work/case["id"]/"producer",upstream_dir=args.upstream)
                    row["producer_latency_ns"]=time.perf_counter_ns()-begin
                    row["original_python_check"]=python_verify(cert,flattened)
                    row["original_certificate_hashes"]={k:digest(cert[k].encode()) for k in ("rules_tsv","asserted_tsv","horn_tsv")}
                    cert=mutate_certificate(cert,case.get("proof",{}).get("mutation","none"))
                    write_json(args.output/"certificates"/(case["id"]+".json"),cert)
                    row["submitted_certificate_sha256"]=digest(json.dumps(cert,sort_keys=True).encode())
                    row["certificate_bytes"]=sum(len(cert[k].encode()) for k in ("rules_tsv","asserted_tsv","horn_tsv"))
                except Exception as error:
                    row["producer_exception_trace"]=traceback.format_exc()
                    cert=None
                    for arm in ("python","lean"):
                        row[arm]=error_record("producer",error)
                if cert is not None:
                    for arm in ("python","lean"):
                        try:
                            row[arm]=python_verify(cert,flattened) if arm=="python" else lean_verify(
                                cert,flattened,args.checker,work/case["id"]/"lean",
                                expected_checker_sha256=checker_hash)
                        except Exception as error:
                            row[arm]=error_record(arm,error)
                            row[arm+"_exception_trace"]=traceback.format_exc()
                        row[arm]["authorized_use"]=bool(baseline["current_target_accepted"]
                            and case.get("rules_authorized",True) and row[arm]["accepted"])
            outcomes.append(row)
            append_attempt(args.output/"predictions.jsonl",row)
            # Every completed attempt, including unavailable ones, survives interruption.
            write_json(args.output/"predictions.json",outcomes)
    write_json(args.output/"predictions.json",outcomes)
    if args.baseline_only:
        write_json(args.output/"summary.json",{"count":len(outcomes),"complete_baseline":sum("target_admitted" in r["current"] for r in outcomes),
                                             "provider_calls":0,"model_spend_usd":0})
        return
    # First semantic access to answerkeys: all candidate predictions are already durable.
    labels={}
    for name in ("labels_development.json","labels_heldout.json"):
        labels.update(json.loads((HERE/name).read_text()))
    if set(labels)!=set(ids): raise ValueError("Truth membership mismatch")
    score={"count":len(outcomes),"case_membership_sha256":digest("\n".join(sorted(ids)).encode()),
           "provider_calls":0,"model_spend_usd":0,"arms":{},"mismatches":[]}
    for arm in ("current","python","lean"):
        armrows=[row[arm] for row in outcomes if arm in row]
        unavailable=sum(r.get("status")=="unavailable" for r in armrows)
        expectedfield="target_admitted" if arm=="current" else "authorized_use"
        actualfield="current_target_accepted" if arm=="current" else "authorized_use"
        confusion={"true_accept":0,"true_reject":0,"false_accept":0,"false_reject":0,"unavailable":unavailable}
        for row in outcomes:
            expected=labels[row["id"]][expectedfield]
            actual=row.get(arm,{}).get(actualfield,False)
            if row.get(arm,{}).get("status") != "unavailable":
                key=("true_" if actual==expected else "false_")+("accept" if actual else "reject")
                confusion[key]+=1
            if actual!=expected or row.get(arm,{}).get("status")=="unavailable":
                score["mismatches"].append({"id":row["id"],"arm":arm,"expected":expected,
                                            "actual":actual,"reason":row.get(arm,{}).get("reason"),
                                            "expected_why":labels[row["id"]]["why"]})
        times=[r["latency_ns"]/1e6 for r in armrows if "latency_ns" in r]
        score["arms"][arm]={"contract":expectedfield,"confusion":confusion,
            "timing_boundary":"fixture-creation-plus-admission" if arm=="current" else "checker-wrapper-only",
            "latency_ms":{"median":statistics.median(times) if times else None,
                          "p95":sorted(times)[min(len(times)-1,int(.95*len(times)))] if times else None,
                          "max":max(times) if times else None}}
    score["certificate_detail_mismatches"]=[]
    for row in outcomes:
        truth=labels[row["id"]]
        for arm in ("python","lean"):
            result=row.get(arm,{})
            for field in ("certificate_valid","goal_supported"):
                actual=(result.get("checker_valid") is True) if field=="certificate_valid" else result.get(field,False)
                if actual!=truth[field]:
                    score["certificate_detail_mismatches"].append({"id":row["id"],"arm":arm,"field":field,
                                                                  "expected":truth[field],"actual":actual})
    score["original_entailment_mismatches"]=[
        {"id":row["id"],"expected":labels[row["id"]]["entailed"],
         "actual":row.get("original_python_check",{}).get("accepted")}
        for row in outcomes if row.get("original_python_check",{}).get("accepted") != labels[row["id"]]["entailed"]]
    score["by_split"]={}
    for split in ("development","heldout"):
        members=[row for row in outcomes if row["split"]==split]
        score["by_split"][split]={"count":len(members),"mismatches":[m for m in score["mismatches"] if m["id"] in {r["id"] for r in members}]}
    score["current_admitted_without_valid_goal_proof"]=[
        row["id"] for row in outcomes if row["current"].get("current_target_accepted") and not labels[row["id"]]["authorized_use"]]
    score["python_lean_final_disagreements"]=[
        row["id"] for row in outcomes if row.get("python",{}).get("authorized_use")!=row.get("lean",{}).get("authorized_use")]
    score["lean_checker_invocations"]=sum(r.get("lean",{}).get("checker_invoked",False) for r in outcomes)
    score["certificate_bytes"]={"total":sum(r.get("certificate_bytes",0) for r in outcomes),
                                "median":statistics.median([r["certificate_bytes"] for r in outcomes if "certificate_bytes" in r])}
    # Independently authored eligible source identities must also match.
    score["admission_detail_mismatches"]=[
        {"id":r["id"],"expected":labels[r["id"]]["eligible_fact_ids"],"actual":r["current"].get("eligible_fact_ids")}
        for r in outcomes if sorted(r["current"].get("eligible_fact_ids",[]))!=sorted(labels[r["id"]]["eligible_fact_ids"])]
    write_json(args.output/"summary.json",score)
    print(json.dumps(score,indent=2))
    if any(score[key] for key in ("mismatches","admission_detail_mismatches","certificate_detail_mismatches","original_entailment_mismatches")):
        sys.exit(1)
if __name__=="__main__": main()
