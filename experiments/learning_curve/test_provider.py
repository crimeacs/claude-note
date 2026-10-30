"""Offline provider controls, not model/task outcomes or native QMD tests.

All apparent network/provider packets are authored public transport fixtures.
One test redirects the owned child to a fixed no-network sleeping Python helper.
No test calls the Responses API or a model.
"""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

HERE=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location("learning_provider_under_test",HERE/"provider.py")
provider=importlib.util.module_from_spec(spec);spec.loader.exec_module(provider)
FAKE_KEY="public-fixture-token-NOT-A-REAL-CREDENTIAL"
SCHEMA={"type":"object","properties":{"ok":{"type":"boolean"}},
        "required":["ok"],"additionalProperties":False}
CALL={"instructions":"Public transport contract only.","input_value":{"public":"fixture"},
      "schema":SCHEMA,"kind":"act","tags":{"fixture":"public"},"max_output_tokens":64}

def packet(*,usage=True,text='{"ok":true}',model=None,status="completed"):
    response={"id":"resp_public_fixture","model":model or provider.MODEL,"status":status,
              "output":[{"type":"message","content":[{"type":"output_text","text":text}]}]}
    if usage:response["usage"]={"input_tokens":100,"output_tokens":20}
    return {"ok":True,"http_status":200,"response":response}

class FakeProcess:
    """Controller fixture only. Never creates a native worker or network socket."""
    next_pid=880001
    def __init__(self,body,*,timeout_first=False,closed=True,remaining_group=False,barrier=None):
        self.body=body if isinstance(body,bytes) else json.dumps(body).encode()
        self.timeout_first=timeout_first;self.closed=closed;self.remaining_group=remaining_group
        self.barrier=barrier;self.communications=[];self.killed=False;self.returncode=None
        self.stdin=io.BytesIO();self.stdout=io.BytesIO();self.stderr=io.BytesIO()
        self.pid=FakeProcess.next_pid;FakeProcess.next_pid+=1
    def communicate(self,input=None,timeout=None):
        self.communications.append({"input":input,"timeout":timeout})
        if self.timeout_first and len(self.communications)==1:
            raise subprocess.TimeoutExpired("owned-public-fixture",timeout)
        if self.barrier is not None:
            self.barrier.wait(timeout=3)
        self.returncode=-signal.SIGKILL if self.killed and self.timeout_first else 0
        if self.closed:
            self.stdout.close();self.stderr.close()
        return self.body,b""
    def poll(self):
        return self.returncode

class FakeFactory:
    def __init__(self,*processes):
        self.processes=list(processes);self.launched=[];self.lock=threading.Lock()
    def __call__(self,argv,**kwargs):
        with self.lock:
            process=self.processes.pop(0)
            self.launched.append((argv,kwargs,process))
            return process
    def killpg(self,pid,sig):
        matches=[p for _,_,p in self.launched if p.pid==pid]
        if not matches:raise ProcessLookupError
        process=matches[-1]
        if sig==signal.SIGKILL:
            process.killed=True
            return
        if sig==0 and process.remaining_group:return
        raise ProcessLookupError

class ProviderControls(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix="public-provider-contract-")
        self.root=Path(self.temp.name)
        self.key=mock.patch.dict(os.environ,{"OPENAI_API_KEY":FAKE_KEY,
                                            "UNRELATED_PRIVATE_CONFIGURATION":"never-forward"})
        self.key.start();self.count=0
    def tearDown(self):
        self.key.stop();self.temp.cleanup()
    def make(self,**kwargs):
        self.count+=1
        return provider.BudgetedProvider(self.root/("run-%d"%self.count),**kwargs)
    @contextmanager
    def transport(self,*processes):
        factory=FakeFactory(*processes)
        with mock.patch.object(provider.subprocess,"Popen",side_effect=factory),\
             mock.patch.object(provider.os,"killpg",side_effect=factory.killpg):
            yield factory
    def receipt(self,instance,number=0):
        return json.loads((instance.root/("%04d.result.json"%number)).read_text())
    def assert_stopped_after(self,instance):
        before=instance.summary()["calls"]
        self.assertTrue(instance.summary()["stopped"])
        with self.assertRaises(provider.ProviderUnavailable):
            instance.call(**CALL)
        self.assertEqual(instance.summary()["calls"],before)

    def test_success_uses_exact_model_store_false_strict_json_and_known_cost(self):
        p=self.make()
        with self.transport(FakeProcess(packet())) as factory:
            value,receipt=p.call(**CALL)
        self.assertEqual(value,{"ok":True})
        self.assertEqual(receipt["accounted_nano_usd"],100*400+20*1600)
        request=json.loads((p.root/"0000.request.json").read_text())
        self.assertEqual(request["model"],"gpt-4.1-mini-2025-04-14")
        self.assertIs(request["store"],False)
        self.assertIs(request["text"]["format"]["strict"],True)
        self.assertEqual(request["text"]["format"]["schema"],SCHEMA)
        self.assertEqual(p.summary()["calls"],1)
        self.assertEqual(p.reserved,0)
        self.assertIsNone(p.stopped)

    def test_credentials_only_in_owned_environment_and_never_artifacts_or_argv(self):
        p=self.make()
        with self.transport(FakeProcess(packet())) as factory:p.call(**CALL)
        argv,kwargs,_=factory.launched[0]
        self.assertTrue(kwargs["start_new_session"])
        self.assertEqual(kwargs["env"]["OPENAI_API_KEY"],FAKE_KEY)
        self.assertNotIn("UNRELATED_PRIVATE_CONFIGURATION",kwargs["env"])
        self.assertNotIn(FAKE_KEY,json.dumps(argv))
        self.assertTrue(all(FAKE_KEY.encode() not in f.read_bytes() for f in p.root.iterdir()))
        self.assertIn("--http-worker",argv)

    def test_reservation_is_durable_and_covers_actor_plus_extractor(self):
        p=self.make()
        with self.transport(FakeProcess(packet()),FakeProcess(packet())):
            p.call(**CALL);p.call(**{**CALL,"kind":"extract"})
        events=[json.loads(s) for s in (p.root/"budget.jsonl").read_text().splitlines()]
        self.assertEqual([e["event"] for e in events],["reserve","settle","reserve","settle"])
        self.assertEqual([e["kind"] for e in events],["act","act","extract","extract"])
        for index in (0,2):
            self.assertGreaterEqual(events[index]["reservation_nano_usd"],
                                    events[index+1]["accounted_nano_usd"])
        self.assertEqual(p.spent,2*(100*400+20*1600))
        self.assertEqual(p.reserved,0)

    def test_global_budget_prevents_another_paid_spawn_while_first_is_inflight(self):
        p=self.make()
        started=threading.Event();release=threading.Event()
        process=FakeProcess(packet())
        original=process.communicate
        def held(input=None,timeout=None):
            started.set();release.wait(timeout=3)
            return original(input,timeout)
        process.communicate=held
        with mock.patch.object(provider,"BUDGET_NANO_USD",3_000_000),self.transport(process) as factory:
            with ThreadPoolExecutor(max_workers=1) as pool:
                first=pool.submit(p.call,**CALL)
                self.assertTrue(started.wait(timeout=2))
                try:
                    with self.assertRaises(provider.ProviderUnavailable):
                        p.call(**{**CALL,"kind":"extract"})
                    self.assertEqual(len(factory.launched),1)
                    self.assertLessEqual(p.spent+p.reserved,provider.BUDGET_NANO_USD)
                finally:release.set()
                first.result(timeout=3)

    def test_concurrent_known_settlements_do_not_lose_or_double_charge(self):
        p=self.make();barrier=threading.Barrier(2)
        second=packet();second["response"]["usage"]={"input_tokens":120,"output_tokens":30}
        with self.transport(FakeProcess(packet(),barrier=barrier),FakeProcess(second,barrier=barrier)):
            with ThreadPoolExecutor(max_workers=2) as pool:
                a=pool.submit(p.call,**CALL);b=pool.submit(p.call,**{**CALL,"kind":"extract"})
                a.result(timeout=4);b.result(timeout=4)
        self.assertEqual(p.calls,2)
        self.assertEqual(p.reserved,0)
        self.assertEqual(p.spent,(100+120)*400+(20+30)*1600)

    def test_call_cap_five_hundred_and_no_automatic_retry(self):
        with self.assertRaises(ValueError):self.make(max_calls=1000)
        p=self.make()
        self.assertEqual(p.max_calls,500)
        p.calls=500
        with self.transport() as factory:
            with self.assertRaises(provider.ProviderUnavailable):p.call(**CALL)
        self.assertEqual(len(factory.launched),0)
        p=self.make(max_calls=1)
        with self.transport(FakeProcess(packet())) as factory:
            p.call(**CALL)
            with self.assertRaises(provider.ProviderUnavailable):p.call(**CALL)
        self.assertEqual(len(factory.launched),1)

    def test_request_bound_prevents_paid_spawn(self):
        p=self.make()
        with self.transport() as factory:
            with self.assertRaises(provider.ProviderUnavailable):
                p.call(**{**CALL,"input_value":{"text":"x"*provider.MAX_REQUEST_BYTES}})
        self.assertEqual(len(factory.launched),0)
        self.assertEqual(p.calls,0)

    def test_missing_usage_retains_full_upper_bound_and_current_result_unavailable(self):
        p=self.make()
        with self.transport(FakeProcess(packet(usage=False))):
            with self.assertRaises(provider.ProviderUnavailable):p.call(**CALL)
            self.assert_stopped_after(p)
        receipt=self.receipt(p)
        self.assertFalse(receipt["usage_known"])
        self.assertEqual(receipt["accounted_nano_usd"],receipt["reservation_nano_usd"])
        self.assertEqual(p.spent,receipt["reservation_nano_usd"])

    def test_invalid_usage_types_or_output_over_cap_halt(self):
        for usage in ({"input_tokens":True,"output_tokens":1},
                      {"input_tokens":1,"output_tokens":True},
                      {"input_tokens":-1,"output_tokens":1},
                      {"input_tokens":1,"output_tokens":65}):
            p=self.make();body=packet();body["response"]["usage"]=usage
            with self.subTest(usage=usage),self.transport(FakeProcess(body)):
                with self.assertRaises(provider.ProviderUnavailable):p.call(**CALL)
                self.assert_stopped_after(p)
                self.assertFalse(self.receipt(p)["usage_known"])

    def test_reported_usage_exceeding_reservation_stops_global_generation(self):
        p=self.make();body=packet()
        body["response"]["usage"]={"input_tokens":1_000_000,"output_tokens":1}
        with self.transport(FakeProcess(body)):
            with self.assertRaises(provider.ProviderUnavailable):p.call(**CALL)
            self.assert_stopped_after(p)
        receipt=self.receipt(p)
        self.assertIn("reservation",receipt["run_error"])

    def test_protocol_failures_are_durable_current_unavailable_and_global_stop(self):
        examples=[]
        for model,status in (("another-model","completed"),(provider.MODEL,"incomplete")):
            examples.append(packet(model=model,status=status))
        refusal=packet();refusal["response"]["output"]=[{"type":"message","content":[{"type":"refusal","refusal":"public fixture refusal"}]}]
        examples.append(refusal)
        examples.append(packet(text='not-json'))
        malformed=packet();malformed["response"]["output"]=[1];examples.append(malformed)
        malformed=packet();malformed["response"]["output"][0]["content"]=[1];examples.append(malformed)
        examples.append(packet(text='[]'))
        examples.append(packet(text='{"ok":true,"extra":"not-in-schema"}'))
        for number,body in enumerate(examples):
            p=self.make()
            with self.subTest(case=number),self.transport(FakeProcess(body)):
                with self.assertRaises(provider.ProviderUnavailable):p.call(**CALL)
                self.assert_stopped_after(p)
                receipt=self.receipt(p)
                self.assertTrue(receipt["run_error"])

    def test_http_and_malformed_worker_packets_halt_without_retry(self):
        for body in ({"ok":False,"http_status":429,"error_code":"rate_limit_exceeded"},
                     b"not-a-json-packet",b"[]",b"[1]"):
            p=self.make()
            with self.subTest(body=body),self.transport(FakeProcess(body)) as factory:
                with self.assertRaises(provider.ProviderUnavailable):p.call(**CALL)
                self.assert_stopped_after(p)
                self.assertEqual(len(factory.launched),1)

    def test_supported_nested_schema_rejects_wrong_enum_and_boolean_integer(self):
        schema={"type":"object","properties":{"items":{"type":"array","items":{"type":"object",
                "properties":{"count":{"type":"integer"},"kind":{"type":"string","enum":["a","b"]}},
                "required":["count","kind"],"additionalProperties":False}}},
                "required":["items"],"additionalProperties":False}
        for text in ('{"items":[{"count":1,"kind":"c"}]}',
                     '{"items":[{"count":true,"kind":"a"}]}',
                     '{"items":[{"kind":"a"}]}'):
            p=self.make()
            with self.subTest(text=text),self.transport(FakeProcess(packet(text=text))):
                with self.assertRaises(provider.ProviderUnavailable):p.call(**{**CALL,"schema":schema})
                self.assert_stopped_after(p)

    def test_absolute_deadline_timeout_kills_reaps_and_collects_owned_child(self):
        p=self.make(timeout_seconds=25);process=FakeProcess(packet(),timeout_first=True)
        with self.transport(process):
            with self.assertRaises(provider.ProviderUnavailable):p.call(**CALL)
            self.assert_stopped_after(p)
        owner=self.receipt(p)["owner"]
        self.assertTrue(process.killed)
        self.assertLessEqual(process.communications[0]["timeout"],25)
        self.assertEqual(process.communications[-1]["timeout"],1)
        self.assertTrue(owner["reaped"])
        self.assertTrue(owner["owned_group_empty"])
        self.assertTrue(owner["both_outputs_collected"])

    def test_uncollected_output_is_unavailable_even_with_valid_packet(self):
        p=self.make()
        with self.transport(FakeProcess(packet(),closed=False)):
            with self.assertRaises(provider.ProviderUnavailable):p.call(**CALL)
            self.assert_stopped_after(p)
        self.assertFalse(self.receipt(p)["owner"]["both_outputs_collected"])

    def test_remaining_owned_group_is_unavailable(self):
        p=self.make()
        with self.transport(FakeProcess(packet(),remaining_group=True)):
            with self.assertRaises(provider.ProviderUnavailable):p.call(**CALL)
            self.assert_stopped_after(p)
        self.assertFalse(self.receipt(p)["owner"]["owned_group_empty"])

    def test_request_journal_failure_stops_before_spawn(self):
        p=self.make();original=provider.write_new
        def fail_request(path,value):
            if str(path).endswith(".request.json"):raise OSError("public-fixture-write-failure")
            return original(path,value)
        with self.transport() as factory,mock.patch.object(provider,"write_new",side_effect=fail_request):
            with self.assertRaises((provider.ProviderUnavailable,OSError)):p.call(**CALL)
            self.assert_stopped_after(p)
            self.assertEqual(len(factory.launched),0)

    def test_result_journal_failure_stops_further_paid_calls(self):
        p=self.make();original=provider.write_new
        def fail_result(path,value):
            if str(path).endswith(".result.json"):raise OSError("public-fixture-write-failure")
            return original(path,value)
        with self.transport(FakeProcess(packet())) as factory,\
             mock.patch.object(provider,"write_new",side_effect=fail_result):
            with self.assertRaises((provider.ProviderUnavailable,OSError)):p.call(**CALL)
            self.assert_stopped_after(p)
            self.assertEqual(len(factory.launched),1)

    def test_real_owned_no_network_helper_is_killed_at_controller_deadline(self):
        # Actual lifecycle check only. Fixed helper never imports HTTP/SDK/model code.
        p=self.make(timeout_seconds=1);real_popen=subprocess.Popen
        helper="import sys,time;sys.stdin.buffer.read();time.sleep(5)"
        def no_network_helper(argv,**kwargs):
            return real_popen([sys.executable,"-I","-B","-c",helper],**kwargs)
        started=time.monotonic()
        with mock.patch.object(provider.subprocess,"Popen",side_effect=no_network_helper):
            with self.assertRaises(provider.ProviderUnavailable):p.call(**CALL)
        self.assertLess(time.monotonic()-started,4)
        owner=self.receipt(p)["owner"]
        self.assertTrue(owner["started"])
        self.assertTrue(owner["reaped"])
        self.assertTrue(owner["both_outputs_collected"])
        self.assertTrue(owner["owned_group_empty"])
        self.assertIsNotNone(owner["error"])

    def test_clean_reaped_completion_is_never_sent_SIGKILL(self):
        p=self.make();process=FakeProcess(packet())
        with self.transport(process):p.call(**CALL)
        self.assertEqual(process.returncode,0)
        self.assertFalse(process.killed)
        self.assertTrue(self.receipt(p)["owner"]["reaped"])

    def test_expired_absolute_deadline_never_spawns_owned_worker(self):
        p=self.make(timeout_seconds=25)
        with self.transport() as factory,\
             mock.patch.object(provider.time,"monotonic",side_effect=[0.,25.,25.]):
            with self.assertRaises(provider.ProviderUnavailable):p.call(**CALL)
            self.assert_stopped_after(p)
        self.assertEqual(len(factory.launched),0)
        self.assertFalse(self.receipt(p)["owner"]["started"])

class WorkerPrivacyControls(unittest.TestCase):
    def worker(self,request,callback):
        output=io.BytesIO()
        stdin=mock.Mock(buffer=io.BytesIO(json.dumps(request).encode()))
        stdout=mock.Mock(buffer=output)
        with mock.patch.dict(os.environ,{"OPENAI_API_KEY":FAKE_KEY}),\
             mock.patch.object(provider.sys,"stdin",stdin),mock.patch.object(provider.sys,"stdout",stdout),\
             mock.patch("urllib.request.urlopen",side_effect=callback):
            provider._worker()
        return json.loads(output.getvalue())

    def test_worker_fixed_destination_header_only_credential_and_bounded_read(self):
        seen={}
        class Response:
            status=200
            def __enter__(self):return self
            def __exit__(self,*args):return None
            def read(self,size):
                seen["read_size"]=size
                return json.dumps(packet()["response"]).encode()
        def open_fixture(request,timeout):
            seen["url"]=request.full_url;seen["body"]=request.data
            seen["headers"]=dict(request.header_items());seen["timeout"]=timeout
            return Response()
        result=self.worker({"model":provider.MODEL,"store":False},open_fixture)
        self.assertTrue(result["ok"])
        self.assertEqual(seen["url"],"https://api.openai.com/v1/responses")
        self.assertEqual(seen["timeout"],20)
        self.assertEqual(seen["read_size"],provider.MAX_RESPONSE_BYTES+1)
        self.assertNotIn(FAKE_KEY.encode(),seen["body"])
        self.assertEqual(seen["headers"]["Authorization"],"Bearer "+FAKE_KEY)

    def test_worker_http_error_drops_error_message_and_request_credential(self):
        import urllib.error
        error=urllib.error.HTTPError(provider.ENDPOINT,401,"unauthorized",{},io.BytesIO(
            json.dumps({"error":{"code":"invalid_api_key","message":FAKE_KEY}}).encode()))
        result=self.worker({"model":provider.MODEL,"store":False},lambda *a,**k:(_ for _ in ()).throw(error))
        self.assertFalse(result["ok"])
        self.assertEqual(result["http_status"],401)
        self.assertNotIn(FAKE_KEY,json.dumps(result))

    def test_worker_storage_or_model_mismatch_never_reaches_network(self):
        for request in ({"model":"other","store":False},{"model":provider.MODEL,"store":True}):
            callback=mock.Mock(side_effect=AssertionError("network forbidden"))
            with self.assertRaises(ValueError):self.worker(request,callback)
            callback.assert_not_called()

    def test_worker_explicitly_denies_redirects_and_ambient_proxy_configuration(self):
        import urllib.request
        class Response:
            status=200
            def __enter__(self):return self
            def __exit__(self,*args):return None
            def read(self,size):return json.dumps(packet()["response"]).encode()
        real_build=urllib.request.build_opener
        with mock.patch("urllib.request.build_opener",wraps=real_build) as build,\
             mock.patch("urllib.request.install_opener") as install:
            result=self.worker({"model":provider.MODEL,"store":False},lambda *a,**k:Response())
        self.assertTrue(result["ok"])
        self.assertEqual(build.call_count,1)
        handlers=build.call_args.args
        self.assertIsInstance(handlers[0],urllib.request.ProxyHandler)
        self.assertEqual(handlers[0].proxies,{})
        redirect=[h for h in handlers if isinstance(h,urllib.request.HTTPRedirectHandler)]
        self.assertEqual(len(redirect),1)
        for code in (301,302,303,307,308):
            self.assertIsNone(redirect[0].redirect_request(None,None,code,"public redirect",{},
                                                         "https://outside.example.invalid/"))
        self.assertEqual(install.call_count,1)

if __name__=="__main__":
    unittest.main()
