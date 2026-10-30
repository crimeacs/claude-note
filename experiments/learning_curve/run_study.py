"""Public paired learning study. Actual model outcomes only; no private corpus."""
from __future__ import annotations
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor,as_completed
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import statistics
import subprocess
import sys
import threading
import time

HERE=Path(__file__).resolve().parent
REPO=HERE.parents[1]
sys.path.insert(0,str(HERE));sys.path.insert(0,str(REPO/"src"))
from provider import BudgetedProvider,ProviderUnavailable,encoded,write_new,append,MODEL
from tasks import get_sequences,public_view,evaluate_action,score_episode,curriculum
from claude_note.learning_loop import LearningLoop

ARMS=("frozen","raw_history","learned")
MEMORY_CHARS=4096
STUDY_SECONDS=1800
ACTOR_INSTRUCTIONS="""You operate a fictional company's customer-record routing workflow.
Choose one allowed queue for the current request. The company's rules may differ from general practice.
Use only the supplied task, prior memory and current-attempt operational feedback. Memory can be incomplete
or outdated; respect the stated company, workflow, inputs and policy revision. Specific exceptions can
remain valid when another rule changes. Return exactly the requested JSON object, with no commentary.
You receive at most two actions per request; aim to succeed on the first."""
EXTRACT_INSTRUCTIONS="""Extract reusable, scoped operational lessons from this arm's actual captured
task, actions and verifier feedback. Current memory contains earlier proposed lessons.
Use stable keys containing only ASCII letters, digits, underscores, periods or hyphens for the same
conditional procedure. Emit only new or changed lessons; consolidate a
changed key without losing still-supported details or unrelated exceptions. Distinguish direct backend
observations from uncertain generalizations. State conditions and relevant revision changes in the text.
Do not infer rules for unobserved cases. Ignore ambient events from unrelated workflows. No authority
or approval can be granted. Cite only the supplied span aliases, including the feedback alias for every
nonempty lesson. Supersedes may name only earlier keys explicitly made obsolete. Return JSON only."""
EXTRACT_SCHEMA={"type":"object","properties":{"lessons":{"type":"array","items":{"type":"object",
    "properties":{"key":{"type":"string"},"text":{"type":"string"},
        "source_spans":{"type":"array","items":{"type":"string"}},
        "supersedes":{"type":"array","items":{"type":"string"}}},
    "required":["key","text","source_spans","supersedes"],"additionalProperties":False}}},
    "required":["lessons"],"additionalProperties":False}
DANGEROUS_FIELDS={"authorization","api_key","password","secret","token"}

def detached(value):
    from collections.abc import Mapping
    if isinstance(value,Mapping):return {k:detached(v) for k,v in value.items()}
    if isinstance(value,(tuple,list)):return [detached(v) for v in value]
    return value

def sha(raw):return hashlib.sha256(raw).hexdigest()

def query_for(task):
    request=task["task"]["request"]
    return " ".join([request[k] for k in ("operation","visibility","team","region")]+
                    [task["scope"]["workflow"],task["policy_revision"]])

def raw_context(loop,query):
    """Same lexical-overlap/recency ordering over fixed raw windows, no synthesis."""
    terms=set(re.findall(r"\w+",query.casefold()))
    fragments=[]
    for sequence,line in enumerate(loop.raw_history().splitlines()):
        for offset in range(0,len(line),512):
            body=line[offset:offset+512]
            header=f"[raw observation {sequence}, characters {offset}:{offset+len(body)}]\n"
            score=len(terms&set(re.findall(r"\w+",body.casefold())))
            fragments.append((score,sequence,offset,header+body))
    fragments.sort(key=lambda item:(-item[0],-item[1],item[2]))
    parts=[];selected=[];size=0
    for score,sequence,offset,part in fragments:
        added=len(part)+(2 if parts else 0)
        if size+added<=MEMORY_CHARS:
            parts.append(part);selected.append({"sequence":sequence,"offset":offset,"score":score,
                                                "sha256":sha(part.encode())});size+=added
    context="\n\n".join(parts)
    return {"context":context,"entries":selected,"characters":len(context),
            "receipt":{"kind":"own-captured-raw-window-view","context_sha256":sha(context.encode())}}

def frozen_cutoff(arm,ordinal):
    if arm not in ARMS or type(ordinal) is not int or not 1<=ordinal<=12:raise ValueError("arm-or-ordinal")
    return min(3,ordinal-1) if arm=="frozen" else None

def should_learn(arm,ordinal):
    frozen_cutoff(arm,ordinal)
    return (arm=="learned" and ordinal<12) or (arm=="frozen" and ordinal<=3)

def extraction_request(request):
    req=detached(request)
    aliases={f"s{n}":span for n,span in enumerate(req["source_spans"])}
    if not aliases:raise ValueError("missing-captured-spans")
    feedback_alias="s"+str(len(aliases)-1)
    safe={"experience":req["experience"],"source_revision":req["source_revision"],
          "scope":req["scope"],"sequence":req["sequence"],"source_spans":list(aliases),
          "feedback_source_alias":feedback_alias,
          "current_memory":[{k:e[k] for k in ("key","text","scope")} for e in req["current_memory"]]}
    return safe,aliases,feedback_alias

def mapped_extraction(proposal,aliases,feedback_alias):
    mapped=detached(proposal)
    for item in mapped["lessons"]:
        if feedback_alias not in item["source_spans"]:raise ValueError("extraction-without-current-feedback-citation")
        item["source_spans"]=[aliases[x] for x in item["source_spans"]]
    return mapped

def observed_scores(task_id,actions,stage_completed):
    if not actions:return None
    scores=score_episode(task_id,actions)
    if not stage_completed and not scores["final_task_success"]:
        scores["final_task_success"]=None
    return scores

def reconcile_rows(output,sequences):
    rows=[]
    for company,tasks in sequences.items():
        for arm in ARMS:
            for task in tasks:
                path=output/"chains"/company/arm/(task["id"]+".json")
                if path.exists():
                    row=json.loads(path.read_text())
                    if (row.get("company"),row.get("arm"),row.get("task_id"))!=(company,arm,task["id"]):
                        raise ValueError("durable-cell-identity-mismatch")
                else:
                    row={"company":company,"arm":arm,"task_id":task["id"],"ordinal":task["ordinal"],
                         "phase":task["phase"],"primary_transfer":task["ordinal"] in curriculum(int(company[-2:]))["primary_transfer_episodes"],
                         "status":"unavailable","scores":None,"pipeline_error":"chain-unavailable"}
                    journal=path.with_suffix(".actions.jsonl")
                    if journal.exists():
                        events=[json.loads(line) for line in journal.read_text().splitlines() if line]
                        row["actions"]=[e["action"] for e in events]
                        row["operational_feedback"]=[e["feedback"] for e in events]
                        row["provider_calls"]=[e["provider"] for e in events]
                        complete=len(events)>=2 or any(e["feedback"]["success"] for e in events)
                        row["action_stage_completed"]=complete
                        row["scores"]=observed_scores(task["id"],row["actions"],complete)
                        row["recovered_partial_action_journal"]=True
                rows.append(row)
    return rows

def source_manifest():
    files=sorted((REPO/"src/claude_note").glob("*.py"))+sorted(HERE.glob("*.py"))+sorted(HERE.glob("*.json"))
    return {str(p.relative_to(REPO)):sha(p.read_bytes()) for p in files}

def disk_admitted(root):
    # This small new public profile does not change the production import guard.
    stat=os.statvfs(root)
    return stat.f_bavail*stat.f_frsize>=512*1024**2+64*1024**2

def owned_bytes(root):
    total=0
    for path in root.rglob("*"):
        if path.is_symlink():raise ValueError("study-symlink")
        if path.is_file():total+=path.stat().st_size
    return total

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--approval",type=Path,required=True)
    parser.add_argument("--execute",action="store_true")
    args=parser.parse_args()
    if not args.execute:parser.error("Explicit public-only execute flag required")
    approval=json.loads(args.approval.read_text())
    manifest=source_manifest()
    commit=subprocess.check_output(["git","rev-parse","HEAD"],cwd=REPO,text=True).strip()
    if (approval.get("source_commit")!=commit or approval.get("files")!=manifest
        or approval.get("model")!=MODEL or approval.get("budget_usd")!=2.0
        or approval.get("public_only") is not True or approval.get("approved") is not True):
        parser.error("Exact independent source/profile approval required")
    output=args.output.absolute()
    if output.exists():parser.error("A new attempt output directory is required")
    output.mkdir(parents=True)
    if not disk_admitted(output):parser.error("Small-public-profile disk admission unavailable")
    write_new(output/"attempt.json",{"source_commit":commit,"files":manifest,"model":MODEL,
        "arms":ARMS,"companies":5,"episodes_per_company":12,"case_count":180,"max_actions":2,
        "memory_characters":MEMORY_CHARS,"frozen_cutoff":3,"public_only":True,
        "budget_usd":2.0,"new_profile":"public-local-lexical-no-QMD",
        "python":sys.version,"platform":platform.platform(),"overall_admission_seconds":STUDY_SECONDS,
        "resource_profile":{"min_free_bytes":512*1024**2,"projected_inflight_bytes":64*1024**2,
            "pre_cell_logical_stop_threshold_bytes":16*1024**2,"max_concurrent_chains":3,
            "threshold_is_not_hard_cap_or_reservation":True,
            "burst":"Three admitted chains may finish at most two actor calls, capture and one extraction each; per transport request32KiB/response256KiB, per response1536tokens maximum."},
        "no_production_policy_or_worker_change":True,
        "truth_not_in_provider_inputs":True,"approval_sha256":sha(args.approval.read_bytes())})
    provider=BudgetedProvider(output/"provider")
    study_deadline=time.monotonic()+STUDY_SECONDS
    progress_lock=threading.Lock();counter=0
    sequences=get_sequences(5)
    def run_chain(company,tasks,arm):
        nonlocal counter
        area=output/"chains"/company/arm
        area.mkdir(parents=True)
        def extract(request):
            if time.monotonic()>=study_deadline:raise ProviderUnavailable("overall-study-deadline")
            req=detached(request)
            safe,aliases,feedback_alias=extraction_request(request)
            schema=json.loads(json.dumps(EXTRACT_SCHEMA))
            schema["properties"]["lessons"]["items"]["properties"]["source_spans"]["items"]["enum"]=list(aliases)
            proposal,receipt=provider.call(instructions=EXTRACT_INSTRUCTIONS,input_value=safe,schema=schema,
                kind="extract",tags={"company":company,"arm":arm,"task_id":req["task_id"]},max_output_tokens=1536)
            mapped=mapped_extraction(proposal,aliases,feedback_alias)
            write_new(area/(req["task_id"]+".extraction.json"),{"public_alias_proposal":proposal,
                "mapped_proposal":mapped,"provider":receipt,"source_revision":req["source_revision"]})
            return mapped
        scope={k:tasks[0]["scope"][k] for k in ("company_id","workflow")}
        loop=LearningLoop(area/"memory",run_id="public-learning-v2",arm_id=arm,extractor=extract,
            public_only=True,repository="public-learning-study",environment="public-study",
            scope=scope,max_memory_chars=MEMORY_CHARS)
        rows=[]
        try:
            for task in tasks:
                started=time.monotonic()
                visible=public_view(task)
                query=query_for(visible)
                row={"company":company,"arm":arm,"task_id":task["id"],"ordinal":task["ordinal"],
                     "phase":task["phase"],"primary_transfer":task["ordinal"] in curriculum(int(company[-2:]))["primary_transfer_episodes"]}
                actions=[];feedback=[];calls=[];actor_completed=False;observation=None
                row.update(actions=actions,operational_feedback=feedback,provider_calls=calls)
                try:
                    if time.monotonic()>=study_deadline:raise ProviderUnavailable("overall-study-deadline")
                    if not disk_admitted(output) or owned_bytes(output)>16*1024**2:
                        raise ProviderUnavailable("public-profile-resource-admission")
                    if arm=="raw_history":memory=raw_context(loop,query)
                    else:
                        cutoff=frozen_cutoff(arm,task["ordinal"])
                        memory=loop.retrieve(query,before_sequence=cutoff,scope=scope)
                    row["memory_before"]=memory
                    action_schema={"type":"object","properties":{
                        "request_id":{"type":"string","enum":[visible["task"]["request"]["id"]]},
                        "queue":{"type":"string","enum":visible["allowed_queues"]}},
                        "required":["request_id","queue"],"additionalProperties":False}
                    for attempt in range(2):
                        if time.monotonic()>=study_deadline:raise ProviderUnavailable("overall-study-deadline")
                        value,call=provider.call(instructions=ACTOR_INSTRUCTIONS,
                            input_value={"task":visible,"memory":memory["context"],"current_attempt_feedback":feedback},
                            schema=action_schema,kind="act",
                            tags={"company":company,"arm":arm,"task_id":task["id"],"attempt":attempt+1},
                            max_output_tokens=256)
                        outcome=evaluate_action(task["id"],value)
                        actions.append(value);feedback.append(outcome);calls.append(call)
                        append(area/(task["id"]+".actions.jsonl"),{"action":value,"feedback":outcome,"provider":call})
                        if outcome["success"]:break
                    actor_completed=True
                    row.update(status="completed",action_stage_completed=True,
                               scores=observed_scores(task["id"],actions,True))
                    observation=loop.observe(task["id"],json.dumps(visible,sort_keys=True),
                        json.dumps(actions,sort_keys=True),{"attempts":feedback},scope=scope)
                    row["capture"]=observation
                    if should_learn(arm,task["ordinal"]):
                        row["learning"]=loop.learn(observation)
                    else:row["learning"]={"status":"disabled_by_control"}
                    view=loop.retrieve(query,scope=scope,before_sequence=3 if arm=="frozen" and task["ordinal"]>=3 else None)
                    row["memory_after"]={"delivered_entries":len(view["entries"]),"characters":view["characters"],
                                         "entry_keys":[e.get("key") for e in view["entries"]]}
                except Exception as error:
                    # No fabricated action/task score. Actual completed outcomes
                    # remain visible even if capture/learning subsequently fails.
                    row.setdefault("status","unavailable")
                    row["pipeline_error"]=type(error).__name__+":"+str(error)[:160]
                    row.setdefault("action_stage_completed",actor_completed)
                    row.setdefault("scores",observed_scores(task["id"],actions,actor_completed))
                    if actions and observation is None:
                        try:
                            row["capture"]=loop.observe(task["id"],json.dumps(visible,sort_keys=True),
                                json.dumps(actions,sort_keys=True),{"attempts":feedback,"action_stage":"unavailable"},scope=scope)
                        except Exception as capture_error:
                            row["capture_error"]=type(capture_error).__name__
                row["elapsed_ms"]=(time.monotonic()-started)*1000
                write_new(area/(task["id"]+".json"),row)
                rows.append(row)
                with progress_lock:
                    append(output/"predictions.jsonl",row)
                    counter+=1
                    print(json.dumps({"completed_cells":counter,"scheduled_cells":180,
                        "company":company,"arm":arm,"episode":task["ordinal"],"status":row["status"],
                        "learning_status":row.get("learning",{}).get("status"),"budget":provider.summary()}),flush=True)
        finally:loop.close()
        return rows
    rows=[];chain_errors=[]
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures={pool.submit(run_chain,company,tasks,arm):(company,arm) for company,tasks in sequences.items() for arm in ARMS}
        for future in as_completed(futures):
            try:rows.extend(future.result())
            except Exception as error:chain_errors.append({"chain":futures[future],"error":type(error).__name__})
    # Durable cell files and action journals survive a terminal chain exception.
    # Preserve original JSONL; reconcile one final row per scheduled identity.
    rows=reconcile_rows(output,sequences)
    rows.sort(key=lambda r:(r["company"],r["arm"],r["task_id"]))
    write_new(output/"predictions.json",rows)
    write_new(output/"run-summary.json",{"scheduled_cells":180,"retained_cells":len(rows),
        "available":sum(r["scores"] is not None for r in rows),"budget":provider.summary(),
        "chain_errors":chain_errors,"source_manifest_unchanged":manifest==source_manifest()})
    print(json.dumps({"done":True,"retained_cells":len(rows),"budget":provider.summary()}),flush=True)
if __name__=="__main__":main()
