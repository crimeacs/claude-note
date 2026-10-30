"""Bounded public-study Responses transport; no credentials in artifacts or argv."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

MODEL = "gpt-4.1-mini-2025-04-14"
ENDPOINT = "https://api.openai.com/v1/responses"
MAX_REQUEST_BYTES = 32768
MAX_RESPONSE_BYTES = 262144
INPUT_NANO_USD = 400
OUTPUT_NANO_USD = 1600
BUDGET_NANO_USD = 2_000_000_000
MAX_CALLS = 500

class ProviderUnavailable(RuntimeError):
    pass

def encoded(value):
    return json.dumps(value,sort_keys=True,separators=(",",":"),ensure_ascii=False,allow_nan=False).encode()

def append(path,value):
    with Path(path).open("ab") as out:
        out.write(encoded(value)+b"\n");out.flush();os.fsync(out.fileno())

def write_new(path,value):
    raw=encoded(value)+b"\n"
    with Path(path).open("xb") as out:
        out.write(raw);out.flush();os.fsync(out.fileno())

def validate_output(value,schema):
    kind=schema.get("type")
    if kind=="object":
        if type(value) is not dict: raise ValueError("output-not-object")
        props=schema.get("properties",{})
        if any(k not in value for k in schema.get("required",[])): raise ValueError("required-field-missing")
        if schema.get("additionalProperties") is False and set(value)-set(props): raise ValueError("extra-output-field")
        for key,item in value.items():
            if key in props: validate_output(item,props[key])
    elif kind=="array":
        if type(value) is not list: raise ValueError("output-not-array")
        for item in value: validate_output(item,schema["items"])
    elif kind=="string":
        if type(value) is not str: raise ValueError("output-not-string")
    elif kind=="integer":
        if type(value) is not int: raise ValueError("output-not-integer")
    elif kind=="boolean":
        if type(value) is not bool: raise ValueError("output-not-boolean")
    else: raise ValueError("unsupported-output-schema")
    if "enum" in schema and value not in schema["enum"]: raise ValueError("output-enum")
    return value

def _worker():
    """Trusted child: fixed host, no SDK/config, bounded request and response."""
    import urllib.request,urllib.error
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self,*args,**kwargs):
            return None
    # Owned child only. Explicitly ignore ambient proxy configuration and refuse
    # redirects, so Authorization cannot migrate to another destination.
    urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect()))
    raw=sys.stdin.buffer.read(MAX_REQUEST_BYTES+1)
    if len(raw)>MAX_REQUEST_BYTES: raise ValueError("request-bound")
    payload=json.loads(raw)
    if payload.get("model")!=MODEL or payload.get("store") is not False:
        raise ValueError("model-or-storage-profile")
    key=os.environ.get("OPENAI_API_KEY")
    if not key: raise ValueError("key-unavailable")
    request=urllib.request.Request(ENDPOINT,data=raw,
        headers={"Authorization":"Bearer "+key,"Content-Type":"application/json"},method="POST")
    try:
        with urllib.request.urlopen(request,timeout=20) as response:
            data=response.read(MAX_RESPONSE_BYTES+1)
            if len(data)>MAX_RESPONSE_BYTES: raise ValueError("response-bound")
            value={"ok":True,"http_status":response.status,"response":json.loads(data)}
    except urllib.error.HTTPError as error:
        value={"ok":False,"http_status":error.code,"error_code":"http-error"}
        try:
            obj=json.loads(error.read(65536)).get("error",{})
            value["error_code"]=str(obj.get("code") or obj.get("type") or "http-error")[:100]
        except Exception: pass
    except Exception as error:
        value={"ok":False,"error_code":type(error).__name__}
    sys.stdout.buffer.write(encoded(value))

class BudgetedProvider:
    """One run, all actors/extractors, no retries, worst-case pre-spend admission.

    Prices pinned from official model docs on 2026-10-30. Reported costs use
    uncached input rates conservatively, not a billing statement. Every unknown
    usage retains its full reservation and stops all subsequent generation.
    """
    def __init__(self,root,*,timeout_seconds=25,max_calls=MAX_CALLS):
        self.root=Path(root).resolve()
        self.root.mkdir(parents=True,exist_ok=False)
        self.lock=threading.Lock()
        self.spent=0;self.reserved=0;self.calls=0;self.stopped=None
        self.timeout_seconds=timeout_seconds
        if type(max_calls) is not int or not 1<=max_calls<=MAX_CALLS: raise ValueError("call-cap-profile")
        self.max_calls=max_calls
        if not 1<=timeout_seconds<=30: raise ValueError("timeout-profile")
        if not os.environ.get("OPENAI_API_KEY"): raise ProviderUnavailable("key-unavailable")
        write_new(self.root/"profile.json",{"model":MODEL,"endpoint":ENDPOINT,"store":False,
            "ceiling_nano_usd":BUDGET_NANO_USD,"max_calls":self.max_calls,
            "input_nano_usd_per_token":INPUT_NANO_USD,"output_nano_usd_per_token":OUTPUT_NANO_USD,
            "pricing_source":"https://developers.openai.com/api/docs/models/gpt-4.1-mini",
            "no_automatic_retry":True,"timeout_seconds":timeout_seconds})

    def call(self,*,instructions,input_value,schema,kind,tags,max_output_tokens):
        if kind not in ("act","extract") or type(max_output_tokens) is not int or not 16<=max_output_tokens<=1536:
            raise ValueError("call-profile")
        request={"model":MODEL,"store":False,"temperature":0,"instructions":instructions,
                 "input":json.dumps(input_value,sort_keys=True,ensure_ascii=False),
                 "max_output_tokens":max_output_tokens,
                 "text":{"format":{"type":"json_schema","name":"public_"+kind,"strict":True,"schema":schema}}}
        raw=encoded(request)
        if len(raw)>MAX_REQUEST_BYTES: raise ProviderUnavailable("request-bound")
        if os.environ.get("OPENAI_API_KEY","").encode() in raw: raise ProviderUnavailable("credential-in-public-payload")
        # Token count cannot exceed UTF8 bytes for these public text requests;
        # additional reserve covers message/schema protocol overhead.
        reservation=(len(raw)+4096)*INPUT_NANO_USD+max_output_tokens*OUTPUT_NANO_USD
        with self.lock:
            if self.stopped: raise ProviderUnavailable("run-stopped:"+self.stopped)
            if self.calls>=self.max_calls or self.spent+self.reserved+reservation>BUDGET_NANO_USD:
                raise ProviderUnavailable("total-run-budget-or-call-cap")
            number=self.calls;self.calls+=1;self.reserved+=reservation
            stem=f"{number:04d}"
            try:
                write_new(self.root/(stem+".request.json"),request)
                append(self.root/"budget.jsonl",{"event":"reserve","call":number,"kind":kind,
                    "tags":tags,"reservation_nano_usd":reservation,"request_sha256":hashlib.sha256(raw).hexdigest()})
            except OSError as error:
                self.stopped="reserve-persistence-failed"
                raise ProviderUnavailable(self.stopped) from error
        started=time.monotonic();proc=None;output=b"";errors=b"";run_error=None
        deadline=started+self.timeout_seconds
        env={k:os.environ[k] for k in ("OPENAI_API_KEY","HOME","LANG","LC_ALL") if k in os.environ}
        env["PATH"]="/usr/bin:/bin";env["PYTHONDONTWRITEBYTECODE"]="1"
        try:
            if time.monotonic()>=deadline:
                raise ProviderUnavailable("deadline-before-spawn")
            proc=subprocess.Popen([sys.executable,"-I","-B",str(Path(__file__).resolve()),"--http-worker"],
                stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=env,start_new_session=True)
            output,errors=proc.communicate(raw,timeout=max(.01,deadline-time.monotonic()))
            if time.monotonic()>deadline: run_error="absolute-deadline"
            if len(output)>MAX_RESPONSE_BYTES+2048 or len(errors)>8192:run_error="output-bound"
        except subprocess.TimeoutExpired:
            run_error="absolute-deadline"
        except Exception as error:
            run_error=type(error).__name__
        finally:
            if proc is not None:
                # communicate() reaps a clean owner. Never signal its numeric
                # PGID afterward: the kernel may reuse it. Before a timeout reap,
                # an outstanding child PID still owns its original identity.
                if proc.returncode is None:
                    try: os.killpg(proc.pid,signal.SIGKILL)
                    except ProcessLookupError: pass
                    try: output,errors=proc.communicate(timeout=1)
                    except subprocess.TimeoutExpired: run_error="cleanup-unavailable"
        empty=False
        if proc is not None:
            try: os.killpg(proc.pid,0)
            except ProcessLookupError: empty=True
        owner={"started":proc is not None,"reaped":proc is not None and proc.poll() is not None,
               "returncode":proc.returncode if proc else None,"owned_group_empty":empty,
               "both_outputs_collected":proc is not None and proc.stdout.closed and proc.stderr.closed,
               "elapsed_ms":(time.monotonic()-started)*1000,"error":run_error}
        packet=None
        if not run_error and owner["returncode"]==0 and owner["reaped"] and owner["owned_group_empty"] and owner["both_outputs_collected"]:
            try:packet=json.loads(output)
            except Exception:run_error="malformed-worker-output"
        if type(packet) is not dict or type(packet.get("ok")) is not bool:
            run_error=run_error or "malformed-worker-packet"
        response=packet.get("response") if type(packet) is dict and packet.get("ok") is True else None
        usage=response.get("usage") if type(response) is dict else None
        accounted=reservation;known=False
        if type(usage) is dict:
            ti,to=usage.get("input_tokens"),usage.get("output_tokens")
            if type(ti) is int and type(to) is int and ti>=0 and 0<=to<=max_output_tokens:
                accounted=ti*INPUT_NANO_USD+to*OUTPUT_NANO_USD;known=True
        value=None
        if accounted>reservation: run_error="usage-exceeded-reservation"
        if response and not known:run_error="usage-unavailable"
        if response and not run_error:
            try:
                if response.get("model")!=MODEL or response.get("status")!="completed":
                    raise ValueError("model-mismatch-or-incomplete")
                outputs=response.get("output")
                if type(outputs) is not list:raise ValueError("output-shape")
                texts=[]
                for item in outputs:
                    if type(item) is not dict:raise ValueError("output-shape")
                    if item.get("type")=="message":
                        if type(item.get("content")) is not list:raise ValueError("content-shape")
                        for part in item["content"]:
                            if type(part) is not dict:raise ValueError("content-shape")
                            if part.get("type")=="output_text":texts.append(part.get("text"))
                            elif part.get("type")=="refusal":raise ValueError("provider-refusal")
                if len(texts)!=1 or type(texts[0]) is not str:raise ValueError("missing-output")
                value=validate_output(json.loads(texts[0]),schema)
            except (ValueError,TypeError,KeyError,AttributeError) as error:
                run_error="protocol:"+str(error)[:120]
        with self.lock:
            self.reserved-=reservation;self.spent+=accounted
            if run_error or not response or not known:
                self.stopped=run_error or str(packet.get("error_code","usage-unavailable") if type(packet) is dict else "malformed-worker-packet")
            if self.spent+self.reserved>BUDGET_NANO_USD:self.stopped="budget-integrity-error"
            receipt={"event":"settle","call":number,"kind":kind,"tags":tags,"usage_known":known,
                     "accounted_nano_usd":accounted,"spent_nano_usd":self.spent,
                     "reservation_nano_usd":reservation,"owner":owner,"run_error":run_error,
                     "packet":packet if packet is not None else {"ok":False,"error_code":run_error or "worker-error"}}
            try:
                write_new(self.root/(stem+".result.json"),receipt)
                append(self.root/"budget.jsonl",{k:v for k,v in receipt.items() if k!="packet"})
            except OSError as error:
                self.stopped="settlement-persistence-failed"
                raise ProviderUnavailable(self.stopped) from error
        if run_error or not response or not known or value is None:
            raise ProviderUnavailable(run_error or self.stopped or "provider-unavailable")
        return value,{"call":number,"kind":kind,"usage":usage,"accounted_nano_usd":accounted,
                      "latency_ms":owner["elapsed_ms"],"request_sha256":hashlib.sha256(raw).hexdigest(),
                      "response_id":response.get("id"),"model":response.get("model")}

    def summary(self):
        with self.lock:
            return {"calls":self.calls,"accounted_upper_bound_usd":self.spent/1e9,
                    "inflight_reservation_usd":self.reserved/1e9,"budget_usd":2.0,
                    "stopped":self.stopped,"model":MODEL}

if __name__=="__main__":
    if sys.argv[1:]!=["--http-worker"]:raise SystemExit("internal bounded transport only")
    _worker()
