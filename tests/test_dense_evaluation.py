"""Pure injected adapters: never import SDKs or invoke a model/server/provider."""
import argparse
import copy
from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from claude_note import agent_tasks as tasks, evals_cli


def fixture_response():
    collection = 'cn-agent-payments-learned'
    uri = f'qmd://{collection}/lesson.md'
    text = 'A synthetic operating lesson whose exact evidence is supplied without lexical matching.'
    revision = hashlib.sha256(text.encode()).hexdigest()
    anchor = {'start_char': 0, 'end_char': len(text), 'start_line': 1, 'end_line': 1}
    identity = hashlib.sha256(json.dumps([uri, revision, anchor, text], ensure_ascii=False,
                                       separators=(',', ':')).encode()).hexdigest()
    response = {'lex': [], 'vec': [{'file': uri, 'source_id': uri, 'revision': revision,
        'revision_kind': 'indexed_body_sha256', 'scored_text': text, 'anchor': anchor,
        'passage_id': identity, 'score': .5, 'lexical_evidence': False}], 'partial': False,
        'coverage': {'semantic': 'complete_scoped_exact_vectors'},
        'manifest': {'ok': True, 'protocol_version': 2, 'index': {'id': 'default'},
            'vector_ready': True, 'models': {'embed': {'id': 'synthetic-embed', 'ready': True}}}}
    return collection, {uri: text}, response


class DenseAdapterTests(unittest.TestCase):
    def render(self, response, documents=None, **kwargs):
        collection, original, _ = fixture_response()
        return tasks.dense_result(response, collection=collection, documents=documents or original,
                                  expected_model='synthetic-embed', **kwargs)

    def test_raw_semantic_wire_and_scored_text_only_no_other_routes(self):
        collection, documents, response = fixture_response()
        case = {'id': 'payments-learned', 'request': '日本語 paraphrase with no source word overlap'}
        def post(settings, route, body, timeout):
            self.assertEqual(route, '/search')
            self.assertEqual(body['rungs'], [])
            self.assertEqual(body['question'], case['request'])
            self.assertEqual(body['collections'], [collection])
            return response
        with mock.patch.object(tasks.recall, 'fuse', side_effect=AssertionError('fusion forbidden')), \
             mock.patch.object(tasks.recall, '_rank_passages', side_effect=AssertionError('rerank forbidden')):
            result = tasks.dense_retrieve(case, tasks.recall.Settings(), documents=documents,
                                         expected_model='synthetic-embed', post=post)
        self.assertTrue(result['available'])
        self.assertEqual(result['status'], 'hit')
        self.assertFalse(result['reranked'])
        self.assertIn(response['vec'][0]['scored_text'], result['context'])
        self.assertNotIn('snippet', result['context'])

    def test_unavailable_is_distinct_from_valid_empty_vector_miss(self):
        _, _, response = fixture_response()
        for value in (None, {}, {'error': 'deadline'}):
            self.assertFalse(self.render(value)['available'])
        response['vec'] = []
        result = self.render(response)
        self.assertTrue(result['available'])
        self.assertEqual(result['status'], 'miss')

    def test_vector_row_with_no_readable_passage_is_unavailable_not_a_valid_empty_miss(self):
        _,_,response=fixture_response()
        response['vec'][0]['scored_text']=''
        self.assertFalse(self.render(response)['available'])
        response['vec']=[]
        self.assertEqual(self.render(response)['status'],'miss')

    def test_partial_and_missing_vectors_cannot_fall_back_to_lexical(self):
        _, _, response = fixture_response()
        variants = []
        for key, value in [('partial', True), ('lex', [[response['vec'][0]]]), ('vec', None)]:
            bad = copy.deepcopy(response); bad[key] = value; variants.append(bad)
        bad = copy.deepcopy(response); bad['coverage']['semantic'] = 'model_cache_unavailable'; variants.append(bad)
        bad = copy.deepcopy(response); bad['manifest']['vector_ready'] = False; variants.append(bad)
        for bad in variants:
            with self.subTest(bad=bad):
                result = self.render(bad)
                self.assertFalse(result['available'])
                self.assertEqual(result['context'], '')

    def test_malformed_scope_revision_identity_and_numeric_scores_are_denied(self):
        _, _, response = fixture_response()
        for key, value in [('file', 'qmd://other/lesson.md'), ('source_id', 'qmd://other/lesson.md'),
                           ('revision', 'a'*64), ('passage_id', 'b'*64), ('scored_text', 'forged'),
                           ('score', True), ('score', float('nan')), ('score', float('inf')), ('score', '0.5')]:
            bad = copy.deepcopy(response); bad['vec'][0][key] = value
            self.assertFalse(self.render(bad)['available'], (key, value))
        bad = copy.deepcopy(response); bad['vec'] *= 2
        self.assertFalse(self.render(bad)['available'])
        bad = copy.deepcopy(response); bad['manifest']['models']['embed']['id'] = 'other'
        self.assertFalse(self.render(bad)['available'])

    def test_vector_order_is_preserved_and_whole_passages_fit_budget(self):
        collection, documents, response = fixture_response()
        row = copy.deepcopy(response['vec'][0]); row['file'] = f'qmd://{collection}/second.md'; row['source_id'] = row['file']
        row['score'] = .9  # Do not rerank or sort the adapter's vector ordering.
        row['passage_id'] = hashlib.sha256(json.dumps([row['file'], row['revision'], row['anchor'], row['scored_text']], separators=(',', ':')).encode()).hexdigest()
        response['vec'].append(row); documents[row['file']] = row['scored_text']
        full = self.render(response, documents)
        self.assertEqual(full['source_ids'], [r['file'] for r in response['vec']])
        first_size = len(json.dumps([full['evidence'][0]], ensure_ascii=False, separators=(',', ':')))
        limited = self.render(response, documents, context_budget=first_size)
        self.assertEqual(limited['source_ids'], [response['vec'][0]['file']])
        self.assertEqual(len(limited['context']), first_size)
        self.assertEqual(limited['coverage']['context'], 'budget_limited')

    def test_positive_rerank_requires_complete_current_model_contract_not_point5_heuristic(self):
        passage = {'passage_id': 'a'*64}
        response = {'manifest': {'ok': True, 'protocol_version': 2, 'models': {'rerank': {'id': 'fixture-rerank', 'ready': True}}, 'rerank_ready': True},
            'model': 'fixture-rerank', 'rerank_status': 'scored', 'coverage': 'exact_passage',
            'passage_scores': {passage['passage_id']: .5}, 'scored_passage_ids': [passage['passage_id']]}
        self.assertTrue(tasks.positive_rerank(response, [passage], 'fixture-rerank'))
        for key, value in [('error', 'deadline'), ('model', 'fallback'), ('rerank_status', 'partial_scores'), ('coverage', 'sdk_fallback_no_rerank'),
                           ('passage_scores', {passage['passage_id']: True}), ('scored_passage_ids', [])]:
            bad = copy.deepcopy(response); bad[key] = value
            self.assertFalse(tasks.positive_rerank(bad, [passage], 'fixture-rerank'))
        for key, value in [('ok', False), ('protocol_version', 1)]:
            bad = copy.deepcopy(response); bad['manifest'][key] = value
            self.assertFalse(tasks.positive_rerank(bad, [passage], 'fixture-rerank'))


class ScheduleAndDenominatorTests(unittest.TestCase):
    def test_seven_arms_and_exact_selected_dependencies(self):
        p = tasks.plan(max_cases=1)
        self.assertEqual(p['scheduled_attempts'], 7)
        self.assertEqual(p['dependencies']['dense_only'], ['embed_model'])
        self.assertEqual(p['dependencies']['corrected_hybrid'], ['embed_model', 'rerank_model'])
        self.assertEqual(p['dependencies']['current_hybrid'], ['embed_model', 'rerank_model', 'baseline_checkout'])
        self.assertIn('not_own_learning', p['measurement'])

    def test_invalid_plan_rejected_before_client_or_retrieval_preparation(self):
        for options in ({'baselines': ()}, {'baselines': ('dense_only', 'dense_only')},
                        {'budget_usd': float('nan')}, {'timeout': True}, {'max_cases': -1}, {'split': 'unknown'}):
            with self.subTest(options=options), mock.patch.object(tasks, 'client_identity', side_effect=AssertionError('client invoked')):
                with self.assertRaises(ValueError):
                    tasks.run(model='unit', **{'budget_usd': 1, **options})
        with mock.patch.object(tasks, 'isolated_hybrid_backends', side_effect=AssertionError('prepared before validation')):
            with self.assertRaises(ValueError):
                evals_cli.run_agent_tasks({'model': 'unit', 'baselines': 'dense_only,dense_only', 'embed_model': '/synthetic/model'})

    def test_stage_counts_reconcile_unavailable_quota_and_remaining_skips(self):
        ask = mock.Mock(side_effect=RuntimeError("You've hit your weekly limit"))
        out = tasks.run(model='unit', budget_usd=10, max_cases=1, ask=ask)
        self.assertEqual(sum(m['scheduled'] for m in out['stage_counts'].values()), 7)
        self.assertEqual(sum(m['model_called'] for m in out['stage_counts'].values()), 1)
        self.assertEqual(sum(m['model_skipped'] for m in out['stage_counts'].values()), 6)
        self.assertEqual(sum(m['verification_attempted'] for m in out['stage_counts'].values()), 0)
        self.assertEqual(sum(m['verified_success'] for m in out['stage_counts'].values()), 0)
        for m in out['stage_counts'].values():
            self.assertEqual(m['scheduled'], m['model_called'] + m['model_skipped'])
        self.assertEqual(ask.call_count, 1)
        self.assertIn('not_live', out['measurement'])

    def test_cli_default_plan_and_prepare_validation_are_model_free(self):
        parser = argparse.ArgumentParser(); evals_cli.add_parser(parser.add_subparsers(dest='command'))
        args = parser.parse_args(['eval', 'agent-tasks', '--cases', '1'])
        with io.StringIO() as output, redirect_stdout(output):
            self.assertEqual(evals_cli.main(args), 0)
            self.assertEqual(json.loads(output.getvalue())['scheduled_attempts'], 7)
        args.baselines = 'unknown'; args.prepare_retrieval = '/synthetic/not-created'
        with mock.patch.object(tasks, 'prepare_retrieval_fixture', side_effect=AssertionError('preparation invoked')):
            with self.assertRaises(ValueError): evals_cli.main(args)

    def test_embed_only_fixture_does_not_invent_rerank_dependency(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tasks.prepare_retrieval_fixture(root, embed_model='/synthetic/embed.gguf')
            config = (root/'qmd/index.yml').read_text()
            self.assertIn('embed:', config)
            self.assertNotIn('rerank:', config)


class OwnershipTests(unittest.TestCase):
    def test_late_timer_and_drain_cannot_signal_after_reap(self):
        process = mock.Mock(pid=424242, returncode=0)
        process.stdout.read1.return_value = b''
        process.stderr.read1.return_value = b''
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(tasks.subprocess, 'Popen', return_value=process), \
             mock.patch.object(tasks, '_group_members', return_value=[]), \
             mock.patch.object(tasks.os, 'killpg') as signal_group:
            group = tasks._OwnedGroup(['synthetic'], cwd=directory, env={}, seconds=10)
            proof = group.close()
            group._expire()
            group.signal(9)
            self.assertTrue(proof['group_empty'] and proof['pipes_drained'] and proof['leader_reaped'])
            signal_group.assert_not_called()
            process.poll.assert_not_called()
            process.wait.assert_called_once()

    @unittest.skipUnless(Path('/bin/ps').exists(), 'owned metadata command unavailable')
    def test_trusted_child_output_and_exact_owned_cleanup(self):
        import sys
        with tempfile.TemporaryDirectory() as directory:
            result = tasks._owned_command([sys.executable, '-I', '-c', 'print("synthetic-ok")'],
                cwd=directory, env={'PATH': '/usr/bin:/bin'}, seconds=3)
            self.assertEqual(result['stdout'].strip(), 'synthetic-ok')
            self.assertEqual(result['returncode'], 0)
            self.assertTrue(result['cleanup']['group_empty'])
            self.assertTrue(result['cleanup']['pipes_drained'])
            self.assertTrue(result['cleanup']['leader_reaped'])
            self.assertEqual(tasks._group_members(result['cleanup']['pgid']), [])

    @unittest.skipUnless(Path('/bin/ps').exists(), 'owned metadata command unavailable')
    def test_trusted_blocked_and_excess_output_children_fail_bounded(self):
        import sys
        for program in ['import time;time.sleep(10)', 'import sys;sys.stdout.write("x"*1048576);sys.stdout.flush()']:
            with tempfile.TemporaryDirectory() as directory:
                with self.assertRaises(RuntimeError):
                    tasks._owned_command([sys.executable, '-I', '-c', program], cwd=directory,
                                         env={'PATH': '/usr/bin:/bin'}, seconds=.2)

    def test_selected_missing_hybrid_dependencies_never_prepare_or_disable_dense(self):
        with mock.patch.object(tasks.setup_recall, 'qmd_exe', side_effect=AssertionError('SDK discovery forbidden')):
            with tasks.isolated_hybrid_backends(baselines=('dense_only', 'corrected_hybrid')) as backends:
                case = tasks.fixtures()[0]
                hybrid = backends['corrected_hybrid'](case)
                dense = backends['dense_only'](case)
                self.assertIn('missing embed_model', hybrid['reason'])
                self.assertIn('missing embed_model', dense['reason'])
                self.assertEqual(backends.lifecycle['events'], [])

    @unittest.skipUnless(__import__('sys').platform == 'darwin' and Path('/usr/bin/sandbox-exec').exists(),
                         'macOS filesystem fence unavailable')
    def test_actual_filesystem_fence_reads_fixture_but_denies_synthetic_sibling(self):
        import sys
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); fixture = root/'allowed'; fixture.mkdir()
            (fixture/'public.txt').write_text('PUBLIC')
            forbidden = root/'synthetic-sibling.txt'; forbidden.write_text('SYNTHETIC-DENIED')
            control = root/'controls'; control.mkdir()
            profile = control/'fixture.sb'
            profile.write_text(tasks._sandbox_profile(fixture, read_paths=[fixture, control, Path(sys.base_prefix)]))
            program = "from pathlib import Path;print(Path('public.txt').read_text());\ntry: Path(" + repr(str(forbidden)) + ").read_text()\nexcept PermissionError: print('DENIED')\nelse: raise Exception('Fence failed')"
            result = tasks._owned_command(['/usr/bin/sandbox-exec', '-f', str(profile), sys.executable, '-I', '-c', program],
                cwd=fixture, env={'PATH':'/usr/bin:/bin'}, seconds=3)
            self.assertEqual(result['returncode'], 0, result['stderr'])
            self.assertEqual(result['stdout'].splitlines(), ['PUBLIC', 'DENIED'])

    @unittest.skipUnless(__import__('sys').platform == 'darwin' and Path('/usr/bin/sandbox-exec').exists(),
                         'macOS Node filesystem fence unavailable')
    def test_empty_openssl_config_allows_fenced_node_without_host_config_access(self):
        import os
        import shutil
        executable = shutil.which('node')
        if not executable:
            self.skipTest('Node unavailable')
        node = Path(executable).resolve()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            fixture = root/'allowed'; fixture.mkdir()
            control = root/'controls'; control.mkdir()
            forbidden = root/'synthetic-openssl.cnf'
            forbidden.write_text('config_diagnostics = 1\n')
            profile = control/'node.sb'
            profile.write_text(tasks._sandbox_profile(fixture,
                read_paths=[fixture, control, node.parent.parent]))
            program = "console.log(require('node:crypto').createHash('sha256').update('PUBLIC').digest('hex'))"
            command = ['/usr/bin/sandbox-exec', '-f', str(profile), str(node), '-e', program]
            environment = {key: os.environ[key] for key in ('HOME', 'LANG', 'LC_ALL') if key in os.environ}
            environment['PATH'] = str(node.parent) + ':/usr/bin:/bin'
            denied = tasks._owned_command(command, cwd=fixture,
                env={**environment, 'OPENSSL_CONF':str(forbidden)}, seconds=5)
            self.assertNotEqual(denied['returncode'], 0)
            self.assertIn('OpenSSL configuration error', denied['stderr'])
            allowed = tasks._owned_command(command, cwd=fixture,
                env={**environment, 'OPENSSL_CONF':'/dev/null'}, seconds=5)
            self.assertEqual(allowed['returncode'], 0, allowed['stderr'])
            self.assertEqual(allowed['stdout'].strip(), hashlib.sha256(b'PUBLIC').hexdigest())
            for result in (denied, allowed):
                self.assertTrue(all(result['cleanup'][key] is True
                    for key in ('group_empty', 'pipes_drained', 'leader_reaped')))

    @unittest.skipUnless(__import__('sys').platform == 'darwin' and Path('/usr/bin/sandbox-exec').exists(),
                         'macOS network fence unavailable')
    def test_actual_network_fence_only_connects_declared_disposable_port(self):
        import sys
        import time
        program = "import socket\ns=socket.socket();s.bind(('127.0.0.1',0));s.listen();print(s.getsockname()[1],flush=True)\nwhile True:\n c,_=s.accept();c.sendall(b'OK');c.close()"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); fixture = root/'fixture'; fixture.mkdir(); control=root/'controls';control.mkdir()
            groups = [tasks._OwnedGroup([sys.executable,'-I','-c',program],cwd=fixture,
                         env={'PATH':'/usr/bin:/bin'},seconds=8) for _ in range(2)]
            try:
                deadline = time.monotonic()+3
                while any(b'\n' not in g.output['stdout'] for g in groups) and time.monotonic()<deadline:
                    time.sleep(.01)
                ports = [int(bytes(g.output['stdout']).strip()) for g in groups]
                self.assertNotIn(8182, ports)  # Never probe the owner service.
                profile=control/'client.sb'
                profile.write_text(tasks._sandbox_profile(fixture,read_paths=[fixture,control,Path(sys.base_prefix)],port=ports[0]))
                client = f"import socket\ns=socket.socket();s.settimeout(.3);s.connect(('127.0.0.1',{ports[0]}));assert s.recv(2)==b'OK';s.close();print('ALLOWED')\ntry:\n s=socket.socket();s.settimeout(.3);s.connect(('127.0.0.1',{ports[1]}))\nexcept PermissionError:print('DENIED')\nelse:raise Exception('other port admitted')"
                result=tasks._owned_command(['/usr/bin/sandbox-exec','-f',str(profile),sys.executable,'-I','-c',client],
                    cwd=fixture,env={'PATH':'/usr/bin:/bin'},seconds=3)
                self.assertEqual(result['returncode'],0,result['stderr'])
                self.assertEqual(result['stdout'].splitlines(),['ALLOWED','DENIED'])
            finally:
                for group in groups:
                    proof=group.close();self.assertTrue(proof['group_empty'] and proof['pipes_drained'])


class FrozenInputsAndConstructionTests(unittest.TestCase):
    def test_original_inventory_rejects_modified_added_missing_and_linked_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); manifest=tasks.prepare_retrieval_fixture(root)
            documents=tasks._frozen_documents(root,manifest)
            self.assertTrue(documents)
            target=next((root/'notes').rglob('*.md')); original=target.read_bytes()
            target.write_bytes(original+b'\nchanged')
            with self.assertRaises(ValueError):tasks._frozen_documents(root,manifest)
            target.write_bytes(original)
            extra=target.parent/'extra.md';extra.write_text('extra')
            with self.assertRaises(ValueError):tasks._frozen_documents(root,manifest)
            extra.unlink();target.unlink()
            with self.assertRaises(ValueError):tasks._frozen_documents(root,manifest)
            other=root/'synthetic-other';other.write_bytes(original);target.symlink_to(other)
            with self.assertRaises(OSError):tasks._frozen_documents(root,manifest)

    def test_each_thread_start_failure_retires_registered_owner(self):
        import threading
        for failure_at in (1,2,3):
            with self.subTest(failure_at=failure_at):
                process=mock.Mock(pid=424242,returncode=-9)
                process.stdout.read1.return_value=b'';process.stderr.read1.return_value=b''
                original=threading.Thread.start; calls=[]; registry=[]
                def start(thread):
                    calls.append(thread)
                    if len(calls)==failure_at:raise RuntimeError('synthetic thread start failure')
                    return original(thread)
                with tempfile.TemporaryDirectory() as directory, \
                     mock.patch.object(tasks.subprocess,'Popen',return_value=process), \
                     mock.patch.object(tasks,'_group_members',return_value=[]), \
                     mock.patch.object(tasks.os,'killpg') as signals, \
                     mock.patch.object(threading.Thread,'start',new=start):
                    with self.assertRaisesRegex(RuntimeError,'cleanup unproven'):
                        tasks._OwnedGroup(['synthetic'],cwd=directory,env={},seconds=10,registry=registry)
                self.assertEqual(len(registry),1)
                self.assertFalse(registry[0].retired)
                self.assertTrue(registry[0].uncertain_starts)
                process.wait.assert_called_once()
                process.poll.assert_not_called()
                signals.assert_called_once()

    def test_constructor_cleanup_failure_retains_unretired_handle(self):
        process=mock.Mock(pid=424242)
        registry=[]
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(tasks.subprocess,'Popen',return_value=process), \
             mock.patch.object(tasks.threading.Thread,'start',side_effect=RuntimeError('start failed')), \
             mock.patch.object(tasks,'_group_members',side_effect=RuntimeError('metadata unavailable')), \
             mock.patch.object(tasks.os,'killpg'):
            with self.assertRaisesRegex(RuntimeError,'metadata unavailable'):
                tasks._OwnedGroup(['synthetic'],cwd=directory,env={},seconds=10,registry=registry)
        self.assertEqual(len(registry),1)
        self.assertFalse(registry[0].retired)
        process.wait.assert_not_called()
        process.poll.assert_not_called()

    @unittest.skipUnless(__import__('sys').platform=='darwin','macOS fence')
    def test_owner_port_is_never_a_legal_fence_target(self):
        with tempfile.TemporaryDirectory() as directory:
            for server in (False,True):
                with self.assertRaises(ValueError):
                    tasks._sandbox_profile(Path(directory),read_paths=[Path(directory)],port=8182,server=server)

    def test_interruption_after_drain_launch_keeps_thread_tracked_and_joined(self):
        import threading
        release=threading.Event();process=mock.Mock(pid=424242,returncode=-9)
        def read(size):
            release.wait(1);return b''
        process.stdout.read1.side_effect=read
        process.stderr.read1.return_value=b''
        original=threading.Thread.start;registry=[];called=[]
        timer=threading.Timer(.05,release.set);timer.start()
        def start(thread):
            original(thread);called.append(thread)
            if len(called)==1:raise KeyboardInterrupt('after-launch interruption')
        try:
            with tempfile.TemporaryDirectory() as directory, \
                 mock.patch.object(tasks.subprocess,'Popen',return_value=process), \
                 mock.patch.object(tasks,'_group_members',return_value=[]), \
                 mock.patch.object(tasks.os,'killpg'), \
                 mock.patch.object(threading.Thread,'start',new=start):
                with self.assertRaises(KeyboardInterrupt):
                    tasks._OwnedGroup(['synthetic'],cwd=directory,env={},seconds=10,registry=registry)
            self.assertTrue(registry[0].retired)
            self.assertEqual(len(registry[0].threads),1)
            self.assertFalse(registry[0].threads[0].is_alive())
            self.assertTrue(registry[0].receipt['pipes_drained'])
        finally:
            release.set();timer.join()


class SerializedIntegrationTests(unittest.TestCase):
    """Synthetic byte files and injected process/HTTP adapters, never real SDK code."""
    def environment(self, *, rank=True, broken_constructor=False, mutate_source=False, fault=''):
        from contextlib import contextmanager, ExitStack
        import sys
        import threading
        @contextmanager
        def harness():
            with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
                root=Path(directory); sdk=root/'node_modules/@tobilu/qmd/dist/index.js'
                sdk.parent.mkdir(parents=True)
                (sdk.parent.parent/'package.json').write_text('{"version":"2.8.3"}')
                for name in ('index.js','store.js','llm.js'):(sdk.parent/name).write_text('SYNTHETIC NOT EXECUTABLE SDK')
                (sdk.parent/'cli').mkdir();(sdk.parent/'cli/qmd.js').write_text('SYNTHETIC NO CLI')
                embed=root/'embed.gguf';embed.write_bytes(b'GGUF'+b'SYNTHETIC'*256)
                rerank=root/'rerank.gguf';rerank.write_bytes(b'GGUF'+b'SYNTHETIC RANK'*256)
                state={'owners':0,'maximum':0,'activations':0,'routes':[],'fixture':None,'current':None,'queries':0,'commands':0}
                class Group:
                    def __init__(self,command,*,cwd,env,seconds,registry=None,body=None):
                        self.retired=False;self.expired=threading.Event();self.overflow=threading.Event()
                        self.pid=10000+state['activations'];self.process=mock.Mock(pid=self.pid,returncode=0);self.pgid=self.pid;self.signaling_closed=False;self.port=int(command[-3]);self.cwd=Path(cwd)
                        self.name='dense_only' if env['QMD_RERANK_MODEL'].endswith('absent-rerank.gguf') else 'corrected_hybrid'
                        self.command=command;self.output={'stdout':bytearray(),'stderr':bytearray(f'claude-note recall server on 127.0.0.1:{self.port} synthetic'.encode())}
                        if registry is not None:registry.append(self)
                        state['activations']+=1;state['current']=self
                        if broken_constructor:raise RuntimeError('synthetic unproven constructor cleanup')
                        if fault=='constructor' and state['activations']==1:
                            self.retired=True;self.signaling_closed=True
                            self.receipt={'group_empty':True,'pipes_drained':True,'leader_reaped':True,'returncode':7}
                            raise RuntimeError('synthetic cleanly retired constructor failure')
                        state['owners']+=1;state['maximum']=max(state['maximum'],state['owners'])
                        assert state['owners']==1,'simultaneous model owners'
                    def live(self):return [] if self.retired else [(self.pid,'S')]
                    def close(self,seconds=5):
                        if broken_constructor or fault=='close':raise RuntimeError('synthetic cleanup unproven')
                        if not self.retired:state['owners']-=1;self.retired=True
                        self.signaling_closed=True
                        self.receipt={'group_empty':True,'pipes_drained':True,'leader_reaped':True,'returncode':0,'pid':self.pid,'pgid':self.pid}
                        return self.receipt
                def manifest():
                    group=state['current']
                    return {'ok':True,'protocol_version':2,'vector_ready':True,'rerank_ready':True,
                        'degradation':['historical_public_deadline'],'queue':{'expired':3,'pending':0,'active':False},
                        'uptime_s':17,'loaded_ms':23,
                        'index':{'id':'default','db_path':group.command[-2],'config_path':group.command[-1]},
                        'models':{'embed':{'id':str(embed),'revision':hashlib.sha256(embed.read_bytes()).hexdigest(),'ready':True},
                                  'rerank':{'id':str(rerank),'revision':hashlib.sha256(rerank.read_bytes()).hexdigest()
                                            if group.name!='dense_only' else 'not_requested','ready':group.name!='dense_only'}}}
                def post(settings,route,body,timeout):
                    state['routes'].append((state['current'].name,route))
                    if fault=='warmup' and len(state['routes'])==1:
                        return {'error':'deadline','manifest':manifest(),
                            'client_transport':{'route':route,'request_id':'a'*24,'outcome':'timeout','elapsed_ms':8000}}
                    if fault=='task_unavailable' and len(state['routes'])>1:
                        return {'error':'deadline','coverage':{'semantic':'deadline'},'manifest':manifest(),
                            'client_transport':{'route':route,'request_id':'b'*24,'outcome':'timeout','elapsed_ms':6000}}
                    if route=='/rerank':
                        ids=[p['passage_id'] for p in body['passages']]
                        return {'manifest':manifest(),'model':str(rerank),'rerank_status':'scored','coverage':'exact_passage',
                                'passage_scores':dict.fromkeys(ids,.5),'scored_passage_ids':ids}
                    collection=body['collections'][0];rows=[]
                    for path in sorted((state['fixture']/'corpus/notes'/collection).glob('*.md')):
                        text=path.read_text();uri=f'qmd://{collection}/{path.name}'
                        anchor={'start_char':0,'end_char':len(text),'start_line':1,'end_line':text.count('\n')+1}
                        shown=' '.join(__import__('re').sub(r'^#+\s*','',text,flags=__import__('re').M).split())
                        revision=hashlib.sha256(text.encode()).hexdigest()
                        identity=hashlib.sha256(json.dumps([uri,revision,anchor,shown],ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
                        rows.append({'file':uri,'source_id':uri,'revision':revision,'scored_text':shown,'passage_id':identity,
                                     'anchor':anchor,'score':.5,'lexical_evidence':False})
                    return {'lex':[],'vec':rows,'partial':False,'coverage':{'semantic':'complete_scoped_exact_vectors'},'manifest':manifest()}
                def owned(command,*,cwd,env,seconds,body=None,registry=None):
                    state['fixture']=Path(cwd)
                    state['commands']+=1
                    cleanup={'group_empty':True,'pipes_drained':True,'leader_reaped':True,'returncode':0}
                    if fault=='provision' and state['commands']==1:
                        from types import SimpleNamespace
                        receipt={**cleanup,'returncode':-9}
                        registry.append(SimpleNamespace(process=SimpleNamespace(pid=50000,returncode=-9),pgid=50000,
                            retired=True,signaling_closed=True,receipt=receipt,expired=threading.Event(),overflow=threading.Event(),
                            output={'stdout':b'bounded synthetic output','stderr':b'bounded synthetic deadline diagnostic'},
                            close=lambda:receipt))
                        raise RuntimeError('owned command deadline/output limit')
                    if body is None:
                        database=Path(cwd)/'cache/qmd/index.sqlite'
                        if fault=='database_parent' and command[-1]=='update':
                            import sqlite3
                            assert database == Path(env['QMD_DB_PATH'])
                            assert Path(env['XDG_CACHE_HOME']).is_dir()
                            assert Path(env['XDG_DATA_HOME']).is_dir()
                            with sqlite3.connect(database) as connection:
                                connection.execute('CREATE TABLE fixture (id INTEGER)')
                            state['database_opened']=True
                        if command[-1]=='embed':database.write_bytes(b'SYNTHETIC NOT SQLITE')
                        output=str(database) if command[-1]=='--help' else '2.8.3'
                    else:
                        state['queries']+=1
                        if fault=='client' and state['queries']==1:
                            state['current'].output['stderr'].extend(b'\nsynthetic ready server diagnostic after client failure')
                            return {'returncode':7,'stdout':'synthetic client failure','stderr':'x'*5000,'cleanup':cleanup}
                        options=json.loads(body)
                        if options['baseline']=='dense_only':
                            settings=tasks.recall.Settings()
                            result=tasks.dense_retrieve(options['case'],settings,documents=options['documents'],
                                                       expected_model=options['expected_model'],post=post)
                        else:result={'available':True,'status':'miss','context':'','source_ids':[],'manifest':manifest()}
                        if fault=='foreign_source':
                            result.update(source_ids=['qmd://foreign-public/lesson.md'],
                                runtime_observation={'manifest_runtime':{'queue':{'expired':17},'degradation':['query_specific_deadline']}},
                                server_calls=[{'route':'/search','request_id':'d'*24,'outcome':'http_response','elapsed_ms':200,'http_status':200}])
                        if mutate_source:
                            path=next((state['fixture']/'corpus/notes').rglob('*.md'));path.write_text('changed source')
                        output=json.dumps(result)
                    return {'returncode':0,'stdout':output,'stderr':'','cleanup':cleanup}
                for name,value in [('qmd_exe','synthetic-qmd'),('real_node',sys.executable),('qmd_sdk',sdk)]:
                    stack.enter_context(mock.patch.object(tasks.setup_recall,name,return_value=value))
                stack.enter_context(mock.patch.object(tasks,'_OwnedGroup',Group))
                stack.enter_context(mock.patch.object(tasks,'_owned_command',side_effect=owned))
                stack.enter_context(mock.patch.object(tasks,'_sandbox_profile',return_value='SYNTHETIC PROFILE'))
                stack.enter_context(mock.patch.object(tasks.recall,'server_capabilities',side_effect=lambda *a,**k:manifest()))
                stack.enter_context(mock.patch.object(tasks.recall,'server_post',side_effect=post))
                with tasks.isolated_hybrid_backends(embed_model=embed,rerank_model=rerank if rank else None,
                        baselines=('dense_only','corrected_hybrid','current_hybrid')) as backends:
                    yield backends,state
                if not backends.lifecycle['cleanup_proven']:
                    import shutil
                    shutil.rmtree(backends.lifecycle['retained_public_fixture'])
                    shutil.rmtree(backends.lifecycle['retained_controls'])
        return harness()

    def test_missing_rerank_and_old_checkout_do_not_disable_healthy_dense(self):
        with self.environment(rank=False) as (backends,state):
            case=tasks.fixtures()[0]
            self.assertFalse(backends['corrected_hybrid'](case)['available'])
            self.assertFalse(backends['current_hybrid'](case)['available'])
            result=backends['dense_only'](case)
            self.assertTrue(result['available'],result)
            self.assertEqual(state['maximum'],1)
            self.assertTrue(all(route=='/search' for _,route in state['routes']))
        self.assertEqual(state['owners'],0)
        self.assertTrue(backends.lifecycle['cleanup_proven'])

    def test_unrelated_missing_dependency_preserves_healthy_dense_owner(self):
        with self.environment(rank=False) as (backends,state):
            case=tasks.fixtures()[0]
            self.assertTrue(backends['dense_only'](case)['available'])
            self.assertFalse(backends['corrected_hybrid'](case)['available'])
            self.assertTrue(backends['dense_only'](case)['available'])
            self.assertEqual(state['activations'],1)
            self.assertEqual(state['owners'],1)

    def test_switching_arms_and_reusing_adjacent_owner_are_explicit_and_serial(self):
        with self.environment() as (backends,state):
            case=tasks.fixtures()[0]
            for baseline in ('dense_only','dense_only','corrected_hybrid','dense_only'):
                result=backends[baseline](case);self.assertTrue(result['available'],result)
            self.assertEqual(state['maximum'],1)
            self.assertEqual(state['activations'],3)
        self.assertEqual(state['owners'],0)
        activations=[e for e in backends.lifecycle['events'] if e['event']=='activation']
        self.assertEqual([e['rerank_warmups'] for e in activations],[0,1,0])
        self.assertEqual([e['vector_warmups'] for e in activations],[1,1,1])
        self.assertEqual(sum(e['event']=='teardown' for e in backends.lifecycle['events']),3)

    def test_unproven_constructor_cleanup_blocks_all_later_activations(self):
        with self.environment(broken_constructor=True) as (backends,state):
            case=tasks.fixtures()[0]
            self.assertFalse(backends['dense_only'](case)['available'])
            self.assertFalse(backends['corrected_hybrid'](case)['available'])
            self.assertEqual(state['activations'],1)
            self.assertFalse(backends.lifecycle['cleanup_proven'])

    def test_changed_original_source_never_becomes_new_frozen_truth(self):
        with self.environment(mutate_source=True) as (backends,state):
            result=backends['dense_only'](tasks.fixtures()[0])
            self.assertFalse(result['available'])
            self.assertIn('original snapshot',result['reason'])

    def test_failed_cell_is_latched_but_different_cell_may_activate_after_cleanup(self):
        for fault in ('constructor','client','warmup'):
            with self.subTest(fault=fault), self.environment(fault=fault) as (backends,state):
                first=backends['dense_only'](tasks.fixtures()[0])
                attempts=(state['activations'],state['queries'],len(state['routes']),state['commands'])
                repeated=backends['dense_only'](tasks.fixtures()[0])
                self.assertFalse(first['available']);self.assertFalse(repeated['available'])
                self.assertTrue(repeated['cell_retry_blocked'])
                self.assertEqual(repeated['new_operation_attempts'],0)
                self.assertEqual((state['activations'],state['queries'],len(state['routes']),state['commands']),attempts)
                later=backends['dense_only'](tasks.fixtures()[1])
                self.assertTrue(later['available'],later)
                self.assertEqual(state['activations'],2)
                self.assertEqual(state['maximum'],1)
                if fault=='client':
                    event=next(e for e in first['preparation_events'] if e['event']=='client_attempt')
                    self.assertEqual(event['returncode'],7)
                    self.assertEqual(len(event['stderr']),4096)
                    self.assertEqual(event['stdout'],'synthetic client failure')
                    self.assertTrue(event['cleanup']['leader_reaped'])
                    teardown=next(e for e in first['preparation_events'] if e['event']=='teardown')
                    self.assertIn('synthetic ready server diagnostic after client failure',teardown['owned_groups'][0]['stderr'])
                if fault=='warmup':
                    self.assertEqual(first['server_calls'][0]['request_id'],'a'*24)
                    event=next(e for e in first['preparation_events'] if e['event']=='vector_warmup')
                    self.assertEqual(event['response_error'],'deadline')
                    self.assertEqual(event['runtime_observation']['manifest_runtime']['queue']['expired'],3)

    def test_provision_exception_keeps_attempt_diagnostics_and_known_cleanup(self):
        with self.environment(fault='provision') as (backends,state):
            result=backends['dense_only'](tasks.fixtures()[0])
            self.assertFalse(result['available'])
            event=next(e for e in result['preparation_events'] if e['event']=='provision')
            self.assertEqual(event['status'],'raised')
            owner=event['owned_groups'][0]
            self.assertEqual(owner['returncode'],-9)
            self.assertEqual(owner['stderr'],'bounded synthetic deadline diagnostic')
            self.assertEqual(owner['cleanup_status'],'proven')
            self.assertTrue(owner['cleanup']['leader_reaped'])
            self.assertEqual(state['activations'],0)

    def test_disposable_database_opens_before_embedding_without_mock_directory_repair(self):
        with self.environment(fault='database_parent') as (backends,state):
            result=backends['dense_only'](tasks.fixtures()[0])
            self.assertTrue(result['available'],result)
            self.assertTrue(state['database_opened'])

    def test_optional_rank_read_hash_and_later_stat_failure_never_disable_dense(self):
        original_open=Path.open;original_stat=Path.stat;original_hash=tasks._file_sha256
        for mode in ('read','hash','stat'):
            calls=[]
            def opened(path,*args,**kwargs):
                if mode=='read' and path.name=='rerank.gguf':raise PermissionError('synthetic optional rank unreadable')
                return original_open(path,*args,**kwargs)
            def hashed(path):
                if mode=='hash' and path.name=='rerank.gguf':raise PermissionError('synthetic optional hash unreadable')
                return original_hash(path)
            def stated(path,*args,**kwargs):
                if mode=='stat' and path.name=='rerank.gguf':
                    calls.append(1)
                    if len(calls)>1:raise PermissionError('synthetic optional stat unreadable')
                return original_stat(path,*args,**kwargs)
            with self.subTest(mode=mode), self.environment() as (backends,state), \
                 mock.patch.object(Path,'open',new=opened),mock.patch.object(Path,'stat',new=stated), \
                 mock.patch.object(tasks,'_file_sha256',side_effect=hashed):
                result=backends['dense_only'](tasks.fixtures()[0])
                self.assertTrue(result['available'],result)
                self.assertEqual(state['activations'],1)
                self.assertFalse(backends['corrected_hybrid'](tasks.fixtures()[0])['available'])
                self.assertTrue(backends['dense_only'](tasks.fixtures()[1])['available'])
                self.assertEqual(state['activations'],1)

    def test_success_keeps_actual_runtime_history_outside_normalized_manifest(self):
        with self.environment() as (backends,state):
            result=backends['dense_only'](tasks.fixtures()[0])
            self.assertTrue(result['available'])
            self.assertNotIn('queue',result['manifest'])
            runtime=result['runtime_observation']['manifest_runtime']
            self.assertEqual(runtime['queue']['expired'],3)
            self.assertEqual(runtime['degradation'],['historical_public_deadline'])
            self.assertEqual(runtime['uptime_s'],17)

    def test_rejected_dense_task_retains_diagnostics_but_accepts_no_raw_evidence(self):
        with self.environment(fault='task_unavailable') as (backends,state):
            result=backends['dense_only'](tasks.fixtures()[0])
            self.assertFalse(result['available'])
            self.assertEqual(result['context'],'')
            self.assertEqual(result['source_ids'],[])
            self.assertEqual(result['evidence'],[])
            observed=result['runtime_observation']
            self.assertEqual(observed['error'],'deadline')
            self.assertEqual(observed['coverage'],{'semantic':'deadline'})
            self.assertEqual(observed['manifest_runtime']['queue']['expired'],3)
            self.assertEqual(observed['manifest_runtime']['degradation'],['historical_public_deadline'])
            out=tasks.run(model='unit',budget_usd=1,max_cases=1,baselines=('dense_only',),
                retrievers={'dense_only':lambda case:result},ask=mock.Mock())
            record=out['records'][0]
            self.assertEqual(record['retrieval_observation']['runtime_observation'],observed)
            self.assertNotIn('retrieval',record['fingerprint'])

    def test_unproven_teardown_keeps_server_diagnostic_without_cleanup_claim(self):
        with self.environment(fault='close') as (backends,state):
            first=backends['dense_only'](tasks.fixtures()[0])
            self.assertTrue(first['available'])
            state['current'].output['stderr'].extend(b'\nsynthetic live server cleanup diagnostic')
            failed=backends['corrected_hybrid'](tasks.fixtures()[0])
            self.assertFalse(failed['available'])
            teardown=next(e for e in failed['preparation_events'] if e['event']=='teardown_unproven')
            owner=teardown['owned_groups'][0]
            self.assertIn('synthetic live server cleanup diagnostic',owner['stderr'])
            self.assertEqual(owner['cleanup_status'],'unproven')
            self.assertNotIn('cleanup',owner)
            self.assertFalse(backends.lifecycle['cleanup_proven'])

    def test_post_query_scope_rejection_retains_structured_query_diagnostics(self):
        with self.environment(fault='foreign_source') as (backends,state):
            result=backends['dense_only'](tasks.fixtures()[0])
            self.assertFalse(result['available'])
            self.assertEqual(result['context'],'')
            self.assertEqual(result['source_ids'],[])
            self.assertEqual(result['evidence'],[])
            event=next(e for e in result['preparation_events'] if e['event']=='client_attempt')
            self.assertEqual(event['server_calls'][0]['request_id'],'d'*24)
            self.assertEqual(event['runtime_observation']['manifest_runtime']['queue']['expired'],17)
            self.assertIn('query_specific_deadline',json.dumps(result['runtime_observation']))
            self.assertIn('d'*24,json.dumps(result['server_calls']))


class IdentityAndFailureEvidenceTests(unittest.TestCase):
    def test_one_known_response_cannot_lend_model_identity_to_an_unknown_cell(self):
        count=[]
        def ask(*args,**kwargs):
            count.append(1)
            return {'output':{'files':[{'path':'app.py','content':'def solve(p): return {}'}]},
                    'observed_model_ids':['synthetic-20261014'] if len(count)==1 else []}
        verify=lambda *args:{'name':'synthetic','verifier':'unit-only','passed':True}
        with mock.patch.object(tasks,'call_claude',ask), mock.patch.object(tasks,'verify_code',verify), \
             mock.patch.object(tasks,'client_identity',return_value={'complete':True}):
            out=tasks.run(model='synthetic-20261014',budget_usd=1,max_cases=2,
                          baselines=('no_memory',),ask=ask,verify=verify)
        self.assertFalse(out['comparable'])
        self.assertEqual(out['stage_counts']['no_memory']['scheduled'],2)
        self.assertEqual(out['stage_counts']['no_memory']['model_responded'],2)
        self.assertEqual(out['stage_counts']['no_memory']['verified_success'],2)
        self.assertEqual(sum(r.get('model_identity_complete') is True for r in out['records']),1)

    def test_invalid_returned_edit_still_retains_bounded_observed_model_identity(self):
        reply={'output':{},'observed_model_ids':['synthetic-20261014']}
        out=tasks.run(model='unit',budget_usd=1,max_cases=1,baselines=('no_memory',),
                      ask=lambda *a,**k:reply)
        self.assertEqual(out['records'][0]['observed_model_ids'],['synthetic-20261014'])
        self.assertTrue(out['records'][0]['model_identity_complete'])
        self.assertEqual(out['stage_counts']['no_memory']['verification_attempted'],0)
        self.assertEqual(out['stage_counts']['no_memory']['verified_success'],0)

    def test_failed_external_backend_keeps_preparation_and_http_observations(self):
        failure={'available':False,'reason':'warmup failed','synthetic_corpus':True,'code_revision':'unit',
                 'preparation_events':[{'event':'activation','ordinal':1}], 'server_calls':[{'route':'/search','outcome':'deadline'}]}
        ask=mock.Mock()
        out=tasks.run(model='unit',budget_usd=1,max_cases=1,baselines=('dense_only',),
                      retrievers={'dense_only':lambda case:failure},ask=ask)
        ask.assert_not_called()
        self.assertEqual(out['records'][0]['retrieval_observation']['preparation_events'],failure['preparation_events'])
        self.assertEqual(out['stage_counts']['dense_only']['external_backend_invoked'],1)
        self.assertEqual(out['stage_counts']['dense_only']['model_called'],0)
        self.assertIn('callback',out['stage_definitions']['external_backend_invoked'])

    def test_runtime_observations_survive_success_and_failure_without_fingerprint_effect(self):
        for available in (True,False):
            observation={'manifest_runtime':{'queue':{'expired':4},'degradation':['historical_deadline']}}
            result={'available':available,'reason':'synthetic','context':'[]','synthetic_corpus':True,
                    'code_revision':'unit','runtime_observation':observation}
            out=tasks.run(model='unit',budget_usd=1,max_cases=1,baselines=('dense_only',),
                retrievers={'dense_only':lambda case:result},ask=lambda *a,**k:{'output':{}})
            record=out['records'][0]
            self.assertEqual(record['retrieval_observation']['runtime_observation'],observation)
            self.assertNotIn('runtime_observation',record['fingerprint'].get('retrieval',{}))
