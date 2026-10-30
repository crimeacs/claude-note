"""Pre-results analysis of the frozen public V2 curriculum.

Reads only an explicitly supplied study directory and the frozen public evaluator.
No provider, SDK, LearningLoop, QMD or network invocation. Missing outcomes remain
null; lower bounds use every scheduled cell. This is a study-specific analyzer,
not a general statistical package or an attestation of provider honesty.
"""
from __future__ import annotations
import argparse
from collections import Counter, defaultdict
import hashlib
import importlib.util
import itertools
import json
from pathlib import Path
import random
import re
import statistics
import tempfile

HERE=Path(__file__).resolve().parent
ARMS=("frozen","raw_history","learned")
PRIMARY=(5,8,9,10,12)
SUBSETS={"acquisition":(1,2,3),"exposure":(1,2,3,4,6,7,11),
         "primary":PRIMARY,"early":(5,),"middle":(8,9),"late":(10,12),
         "correction":(7,),"all":tuple(range(1,13))}
INPUT_NANO=400
OUTPUT_NANO=1600
BUDGET_NANO=2_000_000_000
MAX_CALLS=500
MEMORY_CHARS=4096
MODEL="gpt-4.1-mini-2025-04-14"
RUN_ID="public-learning-v2"
BOOTSTRAP_SEED=640218
BOOTSTRAP_REPLICATES=10000

def encoded(value):
    return json.dumps(value,sort_keys=True,separators=(",",":"),ensure_ascii=False,allow_nan=False).encode()
def digest(raw):
    return hashlib.sha256(raw).hexdigest()
def load(path):
    return json.loads(path.read_text())
def public_evaluator():
    spec=importlib.util.spec_from_file_location("frozen_public_learning_tasks",HERE/"tasks.py")
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module
def finite_values(values):
    return [x for x in values if type(x) in (int,float)]
def distribution(values):
    values=sorted(finite_values(values))
    return {"count":len(values),"median":statistics.median(values) if values else None,
            "min":min(values) if values else None,"max":max(values) if values else None}
def rate(values):
    known=[value for value in values if type(value) is bool]
    successes=sum(known)
    return {"successes":successes,"known":len(known),"scheduled":len(values),
            "unknown":len(values)-len(known),"success_rate_among_known":successes/len(known) if known else None,
            "known_success_lower_bound":successes/len(values) if values else None}
def percentile(values,p):
    values=sorted(values)
    return values[max(0,min(len(values)-1,int(p*(len(values)-1))))] if values else None
def paired_statistics(differences):
    """Configuration units only. No normal approximation or episode IID fiction."""
    values=[x for x in differences if x is not None]
    result={"paired_configuration_count":len(values),"differences":differences,
            "mean_difference":statistics.mean(values) if values else None,
            "min_difference":min(values) if values else None,"max_difference":max(values) if values else None,
            "exact_sign_flip_two_sided_p_descriptive":None,"sign_flip_assignments":2**len(values),
            "cluster_bootstrap_mean_difference_95_percentile_descriptive":None}
    if not values:return result
    observed=abs(statistics.mean(values))
    flips=[abs(statistics.mean(v*s for v,s in zip(values,signs)))
           for signs in itertools.product((-1,1),repeat=len(values))]
    result["exact_sign_flip_two_sided_p_descriptive"]=sum(x>=observed-1e-12 for x in flips)/len(flips)
    rng=random.Random(BOOTSTRAP_SEED)
    means=[statistics.mean(rng.choice(values) for _ in values) for _ in range(BOOTSTRAP_REPLICATES)]
    result["cluster_bootstrap_mean_difference_95_percentile_descriptive"]=[percentile(means,.025),percentile(means,.975)]
    return result

def comparison_gate(source_manifest_unchanged,audit_passed):
    reasons=[]
    if source_manifest_unchanged is not True:
        reasons.append("run-source-manifest-changed" if source_manifest_unchanged is False
                       else "run-source-manifest-integrity-unverified")
    if not audit_passed:reasons.append("artifact-or-source-binding-audit-failed")
    return {"valid":not reasons,"reasons":reasons,
            "rule":"Require affirmative end-of-run unchanged-source evidence AND passing artifact audit; otherwise no scientific arm comparisons or resampling results are emitted."}

class ArtifactAudit:
    def __init__(self,root):
        self.root=root.resolve();self.issues=[];self.reconciled_final_extras=[]
    def issue(self,code,**details):
        self.issues.append({"code":code,**details})
    def confined(self,path):
        path=Path(path)
        if not path.is_relative_to(self.root) or path.resolve()!=path:
            raise ValueError("analysis-path-not-owned-or-symlink")
        return path
    def read(self,path):
        return self.confined(path).read_bytes()
    def json(self,path):
        return json.loads(self.read(path))
    def loop_json(self,path):
        """Decode ONLY LearningLoop._save records; ordinary artifacts stay plain."""
        path=self.confined(path)
        with path.open("rb") as stream:raw=stream.read(262144+1)
        if len(raw)>262144:raise ValueError("loop-record-byte-bound")
        def unique(pairs):
            value={}
            for key,item in pairs:
                if key in value:raise ValueError("duplicate-loop-record-key")
                value[key]=item
            return value
        try:
            record=json.loads(raw.decode("utf-8"),object_pairs_hook=unique)
            if (type(record) is not dict or set(record)!={"payload","sha256"}
                    or type(record["payload"]) is not dict
                    or record["sha256"]!=digest(encoded(record["payload"]))
                    or raw!=encoded(record)+b"\n"):
                raise ValueError("altered-or-noncanonical-loop-record")
            return record["payload"]
        except (ValueError,TypeError,KeyError,RecursionError) as error:
            raise ValueError("invalid-owned-loop-record") from error
    def require(self,condition,code,**details):
        if not condition:self.issue(code,**details)

def expected_source_bindings(root,audit):
    attempt=audit.json(root/"attempt.json")
    files=attempt.get("files",{})
    for name in ("tasks.py","corpus.json","study_design.json","analyze_study.py"):
        relative="experiments/learning_curve/"+name
        actual=digest((HERE/name).read_bytes())
        audit.require(files.get(relative)==actual,"evaluator_or_analysis_source_binding",file=relative)
    return attempt

def rows_from_artifacts(root,audit):
    final=root/"predictions.json"
    incremental=root/"predictions.jsonl"
    rows=audit.json(final) if final.exists() else [
        json.loads(line) for line in audit.read(incremental).decode().splitlines()] if incremental.exists() else []
    if type(rows) is not list:raise ValueError("prediction-list-required")
    if final.exists() and incremental.exists():
        inc=[json.loads(line) for line in audit.read(incremental).decode().splitlines()]
        by_key=lambda rs:{(r.get("company"),r.get("arm"),r.get("task_id")):r for r in rs}
        inc_map=by_key(inc);final_map=by_key(rows)
        audit.require(len(inc)==len(inc_map) and all(final_map.get(key)==value for key,value in inc_map.items()),
                      "incremental_prediction_duplicates_or_changed_final_row")
        for key in set(final_map)-set(inc_map):
            row=final_map[key]
            cell=root/"chains"/str(key[0])/str(key[1])/(str(key[2])+".json")
            journal=cell.with_suffix(".actions.jsonl")
            valid=False;reason="missing-cell-unavailable"
            try:
                if cell.exists():
                    valid=audit.json(cell)==row;reason="durable-cell-recovery"
                elif row.get("recovered_partial_action_journal") and journal.exists():
                    events=[json.loads(line) for line in audit.read(journal).decode().splitlines() if line]
                    valid=(row.get("actions")==[e["action"] for e in events]
                           and row.get("operational_feedback")==[e["feedback"] for e in events]
                           and row.get("provider_calls")==[e["provider"] for e in events])
                    reason="durable-action-journal-recovery"
                else:
                    valid=row.get("status")=="unavailable" and not row.get("actions") and row.get("scores") is None
            except (OSError,ValueError,KeyError,TypeError):pass
            audit.require(valid,"unsupported_final_reconciliation_row",cell=list(key))
            audit.reconciled_final_extras.append({"cell":list(key),"basis":reason,"validated":valid})
    return rows

def raw_line(observation):
    return encoded({"task_id":observation["task_id"],"sequence":observation["sequence"],
                    "scope":observation["scope"],**observation["experience"]}).decode()
def raw_view(observations,query):
    terms=set(re.findall(r"\w+",query.casefold()));fragments=[]
    for observation in observations:
        sequence=observation["sequence"];line=raw_line(observation)
        for offset in range(0,len(line),512):
            body=line[offset:offset+512]
            header=f"[raw observation {sequence}, characters {offset}:{offset+len(body)}]\n"
            score=len(terms&set(re.findall(r"\w+",body.casefold())))
            fragments.append((score,sequence,offset,header+body))
    fragments.sort(key=lambda item:(-item[0],-item[1],item[2]))
    parts=[];selected=[];size=0
    for score,sequence,offset,part in fragments:
        if size+len(part)+(2 if parts else 0)<=MEMORY_CHARS:
            size+=len(part)+(2 if parts else 0)
            parts.append(part);selected.append({"sequence":sequence,"offset":offset,"score":score,
                                               "sha256":digest(part.encode())})
    return "\n\n".join(parts),selected,len(fragments)
def query_for(task):
    req=task["task"]["request"]
    return " ".join([req[k] for k in ("operation","visibility","team","region")]+
                    [task["scope"]["workflow"],task["policy_revision"]])

def audit_observations(root,company,arm,evaluator,audit):
    memory=root/"chains"/company/arm/"memory"
    observations=[]
    folder=memory/"observations"
    if not folder.exists():return memory,observations
    for index,path in enumerate(sorted(folder.glob("*.json"))):
        try:
            observation=audit.loop_json(path)
            audit.require(path.name==f"{index:05d}.json" and observation["sequence"]==index,
                          "observation_sequence",company=company,arm=arm,path=str(path.relative_to(root)))
            task=next(t for t in evaluator.get_sequences()[company] if t["id"]==observation["task_id"])
            audit.require(observation["run_id"]==RUN_ID and observation["arm_id"]==arm,
                          "observation_owner",company=company,arm=arm,sequence=index)
            scope=observation["scope"]
            audit.require(scope=={"repository":"public-learning-study","environment":"public-study",
                                 "company_id":company,"workflow":"customer-records"},
                          "observation_scope",company=company,arm=arm,sequence=index)
            capture=memory/"captures"/f"{index:05d}.jsonl"
            capture_raw=audit.read(capture)
            audit.require(digest(capture_raw)==observation["source_revision"],
                          "capture_revision",company=company,arm=arm,sequence=index)
            experience=observation["experience"]
            expected_rows=[
                {"type":"user","message":{"content":experience["task"]}},
                {"type":"assistant","message":{"content":experience["actual_response"]}},
                {"type":"user","message":{"content":encoded(experience["verifier_feedback"]).decode()}}]
            expected_raw=b"".join(encoded(record)+b"\n" for record in expected_rows)
            expected_anchors=[str(capture)+"#L"+str(line)+"@"+observation["source_revision"] for line in (1,2,3)]
            audit.require(set(experience)=={"task","actual_response","verifier_feedback"}
                          and capture_raw==expected_raw and observation["source_spans"]==expected_anchors,
                          "capture_experience_bytes_or_anchors",company=company,arm=arm,sequence=index)
            audit.require(json.loads(experience["task"])==evaluator.public_view(task),
                          "capture_public_task",company=company,arm=arm,sequence=index)
            actions=json.loads(experience["actual_response"])
            feedback=experience["verifier_feedback"]["attempts"]
            audit.require(feedback==[evaluator.evaluate_action(task["id"],action) for action in actions],
                          "capture_operational_feedback",company=company,arm=arm,sequence=index)
            observations.append(observation)
        except (ValueError,KeyError,StopIteration,OSError,TypeError) as error:
            audit.issue("observation_unreadable",company=company,arm=arm,path=str(path.relative_to(root)),error=type(error).__name__)
    return memory,observations

def audit_memory_before(row,task,memory,observations,audit,root):
    view=row.get("memory_before")
    if view is None:return {}
    company=row["company"];arm=row["arm"];ordinal=task["ordinal"]
    prior=[o for o in observations if int(o["task_id"][-2:])<ordinal]
    audit.require(type(view.get("context")) is str and len(view["context"])<=MEMORY_CHARS
                  and view.get("characters")==len(view["context"]),
                  "context_cap_or_count",company=company,arm=arm,task_id=task["id"])
    if arm=="raw_history":
        context,selected,total=raw_view(prior,query_for(task))
        audit.require(view["context"]==context and view.get("entries")==selected,
                      "raw_prior_window_reconstruction",company=company,arm=arm,task_id=task["id"])
        audit.require(view.get("receipt",{}).get("context_sha256")==digest(context.encode()),
                      "raw_context_digest",company=company,arm=arm,task_id=task["id"])
        return {"prior_observations":len(prior),"prior_raw_characters":sum(len(raw_line(o)) for o in prior),
                "selected_windows":len(selected),"candidate_windows":total,
                "omitted_windows":total-len(selected),"selected_context_characters":len(context),
                "raw_fragment_profile":"512-character fragments, potentially partial JSON"}
    entries=view.get("entries",[])
    parts=[]
    for entry in entries:
        sequence=entry.get("sequence");same=[o for o in prior if o["sequence"]==sequence]
        audit.require(len(same)==1 and entry.get("run_id")==RUN_ID and entry.get("arm_id")==arm,
                      "learned_entry_prior_owner",company=company,arm=arm,task_id=task["id"],key=entry.get("key"))
        if not same:continue
        observation=same[0]
        audit.require(entry.get("source_revision")==observation["source_revision"]
                      and set(entry.get("source_spans",[]))<=set(observation["source_spans"])
                      and entry.get("observation_receipt_digest")==digest(encoded(observation)),
                      "learned_entry_capture_binding",company=company,arm=arm,task_id=task["id"],key=entry.get("key"))
        audit.require(entry.get("scope")==observation["scope"],"learned_entry_scope",
                      company=company,arm=arm,task_id=task["id"],key=entry.get("key"))
        if arm=="frozen":
            audit.require(int(observation["task_id"][-2:])<=3,"frozen_post_snapshot_entry",
                          company=company,task_id=task["id"],key=entry.get("key"))
        try:
            audit.require(digest(audit.read(memory/entry["relative_path"]))==entry["revision"],
                          "learned_entry_revision",company=company,arm=arm,task_id=task["id"],key=entry.get("key"))
            result=audit.loop_json(memory/"learning"/f"{sequence:05d}.result.json")
            audit.require(result.get("status")=="completed" and entry in result.get("entries",[]),
                          "learned_entry_completed_producer",company=company,arm=arm,task_id=task["id"],key=entry.get("key"))
            for artifact in entry.get("input_revisions",[]):
                audit.require(digest(audit.read(memory/artifact["relative_path"]))==artifact["revision"],
                              "learned_entry_input_revision",company=company,arm=arm,task_id=task["id"],key=entry.get("key"))
        except (ValueError,KeyError,OSError,TypeError):
            audit.issue("learned_entry_unreadable",company=company,arm=arm,task_id=task["id"],key=entry.get("key"))
        parts.append("["+entry["key"]+"; proposed] "+entry["text"])
    audit.require(view["context"]=="\n\n".join(parts),"learned_context_reconstruction",
                  company=company,arm=arm,task_id=task["id"])
    receipt=view.get("receipt",{})
    expected_cutoff=min(3,len(prior)) if arm=="frozen" else len(prior)
    audit.require(receipt.get("run_id")==RUN_ID and receipt.get("arm_id")==arm
                  and receipt.get("before_sequence")==expected_cutoff,
                  "memory_cutoff_owner",company=company,arm=arm,task_id=task["id"])
    if receipt:
        without={k:v for k,v in receipt.items() if k!="digest"}
        audit.require(receipt.get("digest")==digest(encoded(without))
                      and receipt.get("context_sha256")==digest(view["context"].encode())
                      and receipt.get("entries")==[e["revision"] for e in entries],
                      "memory_receipt_digest",company=company,arm=arm,task_id=task["id"])
    return {"prior_observations":len(prior),"selected_learned_entries":len(entries),
            "selected_context_characters":len(view["context"])}

def output_value(result):
    packet=result.get("packet")
    response=packet.get("response") if type(packet) is dict else None
    if type(response) is not dict:return None
    texts=[part["text"] for item in response.get("output",[]) if type(item) is dict
           and item.get("type")=="message" for part in item.get("content",[])
           if type(part) is dict and part.get("type")=="output_text" and type(part.get("text")) is str]
    return json.loads(texts[0]) if len(texts)==1 else None

def cost_audit(root,cells,audit):
    folder=root/"provider";ledger_path=folder/"budget.jsonl"
    if not ledger_path.exists():return {"calls":0,"accounted_upper_bound_usd":0,"by_arm_and_kind":{}}
    events=[json.loads(s) for s in audit.read(ledger_path).decode().splitlines()]
    reserves={};settles={};spent=0;inflight=0;groups=defaultdict(lambda:{"calls":0,"known_usage_calls":0,
        "unknown_or_unsettled_calls":0,"accounted_nano_usd":0,"input_tokens":0,"output_tokens":0,"latency_ms":[]})
    for event in events:
        number=event["call"]
        if event["event"]=="reserve":
            audit.require(number not in reserves,"duplicate_cost_reservation",call=number)
            reserves[number]=event;inflight+=event["reservation_nano_usd"]
        elif event["event"]=="settle":
            audit.require(number in reserves and number not in settles,"cost_settlement_membership",call=number)
            settles[number]=event
            if number in reserves:inflight-=reserves[number]["reservation_nano_usd"]
            spent+=event["accounted_nano_usd"]
        audit.require(spent+inflight<=BUDGET_NANO,"global_cost_admission_ceiling",call=number)
    audit.require(len(reserves)<=MAX_CALLS,"global_call_cap")
    for number,reservation in reserves.items():
        tags=reservation.get("tags",{});arm=tags.get("arm");kind=reservation.get("kind")
        group=groups[str(arm)+"/"+str(kind)];group["calls"]+=1
        result_path=folder/f"{number:04d}.result.json";result=audit.json(result_path) if result_path.exists() else None
        accounted=result["accounted_nano_usd"] if result else reservation["reservation_nano_usd"]
        group["accounted_nano_usd"]+=accounted
        if result and result.get("usage_known"):
            usage=result.get("packet",{}).get("response",{}).get("usage",{})
            input_tokens=usage.get("input_tokens");output_tokens=usage.get("output_tokens")
            audit.require(type(input_tokens) is int and type(output_tokens) is int
                          and accounted==input_tokens*INPUT_NANO+output_tokens*OUTPUT_NANO,
                          "usage_cost_recalculation",call=number)
            group["known_usage_calls"]+=1
            group["input_tokens"]+=input_tokens;group["output_tokens"]+=output_tokens
        else:group["unknown_or_unsettled_calls"]+=1
        if result:
            group["latency_ms"].append(result.get("owner",{}).get("elapsed_ms"))
            audit.require({k:v for k,v in result.items() if k!="packet"}==settles.get(number),
                          "durable_cost_result_matches_ledger",call=number)
        request=audit.json(folder/f"{number:04d}.request.json")
        audit.require(request.get("model")==MODEL and request.get("store") is False
                      and request.get("text",{}).get("format",{}).get("strict") is True,
                      "provider_request_profile",call=number)
        key=(tags.get("company"),arm,tags.get("task_id"));cell=cells.get(key)
        audit.require(cell is not None and kind in ("act","extract"),"provider_call_cell_membership",call=number)
        if cell:
            task=cell["task"]
            if kind=="act":
                payload=json.loads(request["input"])
                attempt=tags.get("attempt")
                index=attempt-1 if type(attempt) is int else -1
                audit.require(payload.get("task")==task and payload.get("memory")==cell["row"].get("memory_before",{}).get("context"),
                              "provider_actor_public_task_and_memory",call=number)
                audit.require(payload.get("current_attempt_feedback")==cell["feedback"][:max(index,0)],
                              "provider_actor_prior_own_attempt_feedback",call=number)
                if result and not result.get("run_error") and result.get("usage_known"):
                    try:
                        audit.require(0<=index<len(cell["actions"]) and output_value(result)==cell["actions"][index],
                                      "provider_actual_action_binding",call=number)
                    except (ValueError,TypeError,KeyError):
                        audit.issue("provider_action_unreadable",call=number)
            elif kind=="extract":
                payload=json.loads(request["input"])
                own=[o for o in cell["observations"] if o["task_id"]==task["id"]]
                audit.require(len(own)==1,"extract_owned_capture_membership",call=number)
                if own:
                    observation=own[0]
                    aliases=["s"+str(i) for i in range(len(observation["source_spans"]))]
                    audit.require(payload.get("experience")==observation["experience"]
                        and payload.get("source_revision")==observation["source_revision"]
                        and payload.get("scope")==observation["scope"]
                        and payload.get("sequence")==observation["sequence"]
                        and payload.get("source_spans")==aliases
                        and payload.get("feedback_source_alias")==(aliases[-1] if aliases else None),
                        "extract_actual_own_experience_binding",call=number)
                    active={}
                    for earlier in cell["observations"]:
                        if earlier["sequence"]>=observation["sequence"]:continue
                        path=cell["memory_root"]/"learning"/f"{earlier['sequence']:05d}.result.json"
                        prior=audit.loop_json(path) if path.exists() else {}
                        if prior.get("status")!="completed":continue
                        for entry in prior.get("entries",[]):
                            scope_key=digest(encoded(entry["scope"]))
                            for key in entry["supersedes"]:active.pop((scope_key,key),None)
                            active[(scope_key,entry["key"])]=entry
                    expected=[{k:entry[k] for k in ("key","text","scope")} for entry in active.values()]
                    audit.require(payload.get("current_memory")==expected,
                                  "extract_prior_own_consolidated_memory",call=number)
                if arm=="raw_history":
                    audit.issue("raw_control_extraction_call",call=number)
                elif arm=="frozen":
                    audit.require(task["ordinal"]<=3,"frozen_post_snapshot_extraction",call=number)
                elif arm=="learned":
                    audit.require(task["ordinal"]<12,"terminal_unnecessary_extraction",call=number)

    return {"calls":len(reserves),"settled":len(settles),"unsettled":len(reserves)-len(settles),
            "accounted_upper_bound_usd":sum(g["accounted_nano_usd"] for g in groups.values())/1e9,
            "by_arm_and_kind":{key:{**{k:v for k,v in group.items() if k!="latency_ms"},
                "accounted_upper_bound_usd":group["accounted_nano_usd"]/1e9,
                "provider_latency_ms":distribution(group["latency_ms"])} for key,group in sorted(groups.items())},
            "interpretation":"Uncached-input/output accounting estimate, not a billing statement. Unknown/unsettled usage keeps the full admitted reservation. Learning cost is additional, not compute-identical."}

def analyze(root):
    root=Path(root).resolve();audit=ArtifactAudit(root);attempt=expected_source_bindings(root,audit)
    evaluator=public_evaluator();sequences=evaluator.get_sequences(5)
    rows=rows_from_artifacts(root,audit);provided={}
    for row in rows:
        key=(row.get("company"),row.get("arm"),row.get("task_id"))
        audit.require(key not in provided,"duplicate_prediction_cell",cell=list(key))
        provided[key]=row
    cells={};details=[]
    for company,trajectory in sequences.items():
        for arm in ARMS:
            memory,observations=audit_observations(root,company,arm,evaluator,audit)
            for task in trajectory:
                key=(company,arm,task["id"]);row=provided.get(key,{})
                actions=row.get("actions",[]);actions=actions if type(actions) is list else []
                if len(actions)>2:audit.issue("action_cap",cell=list(key))
                outcomes=[evaluator.evaluate_action(task["id"],action) for action in actions[:2]]
                scores=evaluator.score_episode(task["id"],actions[:2])
                first=outcomes[0]["success"] if outcomes else None
                action_stage_complete=row.get("action_stage_completed",row.get("action_stage_complete",row.get("status")=="completed"))
                # A real routed action proves completion even if later persistence failed.
                final=True if any(o["success"] for o in outcomes) else False if action_stage_complete else None
                if row:
                    for field,value in (("company",company),("arm",arm),("task_id",task["id"]),
                                        ("ordinal",task["ordinal"]),("phase",task["phase"]),
                                        ("primary_transfer",task["ordinal"] in PRIMARY)):
                        if field in row:audit.require(row[field]==value,"prediction_public_case_membership",cell=list(key),field=field)
                    audit.require(row.get("operational_feedback",[])==outcomes,"actual_outcome_regrade",cell=list(key))
                    saved=row.get("scores")
                    if saved is not None:
                        audit.require(all(saved.get(k)==v for k,v in scores.items()
                                          if k not in ("final_task_success",)),
                                      "saved_score_regrade",cell=list(key))
                        audit.require(saved.get("final_task_success") in (scores["final_task_success"],final),
                                      "saved_final_score_regrade",cell=list(key))
                diagnostic=audit_memory_before(row,task,memory,observations,audit,root) if row.get("memory_before") else {}
                item={"company":company,"arm":arm,"task_id":task["id"],"ordinal":task["ordinal"],
                      "phase":task["phase"],"primary_transfer":task["ordinal"] in PRIMARY,
                      "first_action_success":first,"final_task_success":final,"action_count":len(actions),
                      "invalid_action_count":scores["invalid_action_count"],"stale_route_count":scores["stale_route_count"],
                      "overscoped_correction_count":scores["overscoped_correction_count"],
                      "valid_but_wrong_count":scores["valid_but_wrong_count"],
                      "status":row.get("status","missing"),"pipeline_error":row.get("pipeline_error"),
                      "learning_status":row.get("learning",{}).get("status","not_recorded"),
                      "context_characters":row.get("memory_before",{}).get("characters"),
                      "elapsed_ms":row.get("elapsed_ms"),"memory_diagnostics":diagnostic}
                details.append(item);cells[key]={ "row":row,"task":evaluator.public_view(task),"actions":actions,"feedback":outcomes,
                                          "memory_root":memory,"observations":observations}
    scheduled=set(cells)
    for key in set(provided)-scheduled:audit.issue("unscheduled_prediction_cell",cell=list(key))
    summaries={}
    for arm in ARMS:
        arm_details=[d for d in details if d["arm"]==arm]
        summaries[arm]={"subsets":{},"status_counts":dict(Counter(d["status"] for d in arm_details)),
            "pipeline_error_counts":dict(Counter(d["pipeline_error"] for d in arm_details if d["pipeline_error"])),
            "learning_status_counts":dict(Counter(d["learning_status"] for d in arm_details))}
        for name,ordinals in SUBSETS.items():
            chosen=[d for d in arm_details if d["ordinal"] in ordinals]
            summaries[arm]["subsets"][name]={
                "first_action":rate([d["first_action_success"] for d in chosen]),
                "final_task":rate([d["final_task_success"] for d in chosen]),
                "action_count":sum(d["action_count"] for d in chosen),
                "action_count_distribution":distribution([d["action_count"] for d in chosen if d["first_action_success"] is not None]),
                "invalid_actions":sum(d["invalid_action_count"] for d in chosen),
                "stale_routes":sum(d["stale_route_count"] for d in chosen),
                "overscoped_corrections":sum(d["overscoped_correction_count"] for d in chosen),
                "context_characters":distribution([d["context_characters"] for d in chosen]),
                "cell_elapsed_ms_including_learning":distribution([d["elapsed_ms"] for d in chosen])}
    costs=cost_audit(root,cells,audit)
    completion=root/"run-summary.json"
    run_summary=audit.json(completion) if completion.exists() else {}
    source_unchanged=run_summary.get("source_manifest_unchanged")
    gate=comparison_gate(source_unchanged,not audit.issues)
    paired={}
    for control in (("frozen","raw_history") if gate["valid"] else ()):
        paired[control]={}
        for subset in ("primary","early","middle","late"):
            records=[];complete=[];lower=[]
            for company in sequences:
                learned=[d for d in details if d["company"]==company and d["arm"]=="learned" and d["ordinal"] in SUBSETS[subset]]
                baseline=[d for d in details if d["company"]==company and d["arm"]==control and d["ordinal"] in SUBSETS[subset]]
                lr=rate([d["first_action_success"] for d in learned]);br=rate([d["first_action_success"] for d in baseline])
                difference=(lr["success_rate_among_known"]-br["success_rate_among_known"]
                            if lr["unknown"]==br["unknown"]==0 else None)
                records.append({"company":company,"learned":lr,"control":br,"complete_pair_difference":difference,
                    "known_success_lower_bound_difference":lr["known_success_lower_bound"]-br["known_success_lower_bound"]})
                complete.append(difference);lower.append(records[-1]["known_success_lower_bound_difference"])
            paired[control][subset]={"companies":records,"complete_configuration_pairs":paired_statistics(complete),
                "all_scheduled_known_success_lower_bound_differences":paired_statistics(lower)}
    return {"analysis_version":2,"analyzer_sha256":digest(Path(__file__).read_bytes()),
        "source_commit":attempt.get("source_commit"),"scheduled_cells":180,"retained_prediction_rows":len(rows),
        "scheduled_primary_cells_per_arm":25,"metrics_frozen_before_results":True,
        "audit":{"passed":not audit.issues,"issues":audit.issues,"reconciled_final_extras":audit.reconciled_final_extras},
        "arms":summaries,"scientific_comparisons":gate,
        "source_manifest_unchanged":source_unchanged,
        "paired_learned_minus_control":paired if gate["valid"] else None,
        "descriptive_outcomes_only":not gate["valid"],"costs":costs,"cells":details,
        "statistical_contract":{"unit":"five independently permuted company configurations from one workflow family",
            "primary_ordinals":PRIMARY,"subsets":SUBSETS,"sign_flip":"2^n exact two-sided sign flip of complete company differences; 32 assignments when all five complete. Assumes sign exchangeability, not ensured by configuration construction.",
            "bootstrap":"10000 fixed-seed configuration resamples; descriptive percentile interval, n=5 is small.",
            "missingness":"Unknowns remain null. Complete-pair estimates and all-scheduled known-success lower bounds shown separately. Lower-bound differences are not unbiased effect estimates under differential missingness.",
            "testing":"No universal significance/adoption claim from these dependent cells, multiple displayed subsets or one deterministic workflow family."},
        "limitations":["Backend constraint explanations are legitimate shared operational feedback; this is not discovery of latent policies from labels alone.",
          "Frozen is each arm's OWN post-acquisition snapshot, not empty memory. Raw is one own-history 512-character lexical-window baseline, potentially partial JSON; it is not optimal raw-history retrieval.",
          "First-action transfer is primary. Current-item corrective feedback makes two-action final success susceptible to ceiling.",
          "Each arm owns its actions and observations; exposure may differ when actions fail or calls are unavailable. Availability and extraction failure are outcomes, not silently excluded cells.",
          "Memory entries remain proposed, not attested truth. Integrity/provenance checks do not prove extraction semantics.",
          "More stored text, extraction count, retrieval overlap, proof validity or a fitted chronological slope alone is not evidence of compounding utility.",
          "One workflow with two new procedures, correction and precedence transfer does not establish indefinite growth, general productivity, seven-arm production benefit, native QMD readiness or actual host delivery."]}

def self_test():
    # Authored arithmetic controls only; never benchmark/model observations.
    assert rate([True,False,None])=={"successes":1,"known":2,"scheduled":3,"unknown":1,
        "success_rate_among_known":.5,"known_success_lower_bound":1/3}
    assert rate([None,None])["success_rate_among_known"] is None
    stats=paired_statistics([1.,1.,1.,1.,1.])
    assert stats["sign_flip_assignments"]==32 and stats["exact_sign_flip_two_sided_p_descriptive"]==2/32
    assert paired_statistics([None]*5)["mean_difference"] is None
    assert paired_statistics([0.]*5)["exact_sign_flip_two_sided_p_descriptive"]==1
    assert len(PRIMARY)==5 and len(set(SUBSETS["exposure"])&set(PRIMARY))==0
    # One end-to-end authored source-change control: even byte-bound input
    # files cannot rescue a run explicitly reporting source changes.
    with tempfile.TemporaryDirectory(prefix="public-analysis-source-change-control-") as temporary:
        root=Path(temporary)
        files={"experiments/learning_curve/"+name:digest((HERE/name).read_bytes())
               for name in ("tasks.py","corpus.json","study_design.json","analyze_study.py")}
        (root/"attempt.json").write_text(json.dumps({"source_commit":"authored-source-change-control","files":files}))
        (root/"predictions.json").write_text("[]")
        (root/"run-summary.json").write_text(json.dumps({"source_manifest_unchanged":False}))
        report=analyze(root)
        assert report["scientific_comparisons"]["valid"] is False
        assert "run-source-manifest-changed" in report["scientific_comparisons"]["reasons"]
        assert report["paired_learned_minus_control"] is None
        assert report["descriptive_outcomes_only"] is True
        assert all(report["arms"][arm]["subsets"]["primary"]["first_action"]["unknown"]==25 for arm in ARMS)
    print("7 offline arithmetic/missingness/source-integrity controls passed; not model results")

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--input",type=Path)
    parser.add_argument("--output",type=Path)
    parser.add_argument("--self-test",action="store_true")
    args=parser.parse_args()
    if args.self_test:self_test();return
    if not args.input or not args.output:parser.error("--input and --output required")
    report=analyze(args.input)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    with args.output.open("x") as stream:stream.write(json.dumps(report,indent=2,sort_keys=True)+"\n")
    print(json.dumps({"audit_passed":report["audit"]["passed"],"scheduled_cells":180,
                     "primary":{arm:report["arms"][arm]["subsets"]["primary"]["first_action"] for arm in ARMS}}))
    if not report["audit"]["passed"] or not report["scientific_comparisons"]["valid"]:raise SystemExit(1)
if __name__=="__main__":main()
