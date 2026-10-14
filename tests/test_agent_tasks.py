"""No model/network calls. Exercise actual sandbox only with trusted synthetic code."""
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock

from claude_note import agent_tasks, evals_cli

DEADLINE_CODE = """def solve(payload):
    left = payload['budget']
    reserve = payload['reserve']
    values = []
    for stage in payload['stages']:
        if left <= reserve:
            break
        granted = min(stage['timeout'], left-reserve)
        values.append(granted)
        left -= min(stage['elapsed'], granted)
    return {'timeouts': values}
"""


class FixtureTests(unittest.TestCase):
    def test_frozen_longitudinal_splits_and_safety_ground_truth(self):
        cases = agent_tasks.fixtures()
        self.assertEqual(len(cases), 16)
        dev = {c['family'] for c in cases if c['split'] == 'dev'}
        holdout = {c['family'] for c in cases if c['split'] == 'holdout'}
        self.assertFalse(dev & holdout)
        self.assertEqual({c['phase'] for c in cases}, {'learned', 'paraphrase', 'changed_environment', 'poisoned_memory'})
        for case in cases:
            for value in agent_tasks._inputs(case, 17):
                result = agent_tasks._expected(case, value)
                if case['family'] == 'deadlines':
                    self.assertLessEqual(sum(min(s['elapsed'], t) for s, t in zip(value['stages'], result['timeouts'])), value['budget'])
                if case['family'] == 'recovery' and value['events'][0]['code'] == 'insufficient_quota':
                    self.assertEqual(result, {'actions': ['stop_budget']})
                if case['family'] == 'audiences' and (value['private'] or not value['source_audiences'] or ['host'] in value['source_audiences']):
                    self.assertIsNone(result['content'])

    def test_public_export_is_separate_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = agent_tasks.prepare_retrieval_fixture(root)
            self.assertTrue(manifest['synthetic_corpus'])
            self.assertEqual(len(manifest['collections']), 16)
            with mock.patch.dict(os.environ, {'QMD_CONFIG_DIR': str(root/'qmd')}):
                self.assertEqual(len(agent_tasks.recall._collection_dirs()), 16)
            self.assertTrue(all(not p.is_symlink() for p in root.rglob('*')))
            with self.assertRaises(ValueError):
                agent_tasks.prepare_retrieval_fixture(root)

    def test_only_constrained_python_file_edits_are_accepted(self):
        valid = {'files': [{'path': 'app.py', 'content': 'def solve(payload):\n    return {}\n'}]}
        self.assertIn('solve', agent_tasks.validate_edits(valid))
        for path, code in [('../escape.py', 'def solve(p): return p'), ('app.py', 'import os\ndef solve(p): return p'),
                           ('app.py', 'def solve(p): return eval(p)'), ('app.py', 'def solve(p): return p.__class__')]:
            with self.assertRaises(ValueError):
                agent_tasks.validate_edits({'files': [{'path': path, 'content': code}]})

    @unittest.skipUnless(shutil.which('node'), 'Node unavailable')
    def test_common_runtime_guard_forbids_nonlocal_models_and_native_builds(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sdk = root/'index.js'
            sdk.write_text('')
            (root/'llm.js').write_text('export class LlamaCpp { loadLlamaRuntime(build) { return build; } resolveModel(uri) { return uri; } }')
            model = root/'public-fixture.gguf'
            model.write_bytes(b'GGUF' + b'0'*1024)
            guard, digest = agent_tasks._model_guard(root, sdk, (model, model))
            script = f"import {{LlamaCpp}} from {json.dumps((root/'llm.js').as_uri())}; const llm=new LlamaCpp(); if(llm.loadLlamaRuntime(true)!==false) throw Error('build permitted'); if(await llm.resolveModel({json.dumps(str(model))})!=={json.dumps(str(model.resolve()))}) throw Error('local missing'); let denied=false; try{{await llm.resolveModel('hf:private/unavailable.gguf')}}catch{{denied=true}} if(!denied) throw Error('download permitted');"
            result = agent_tasks.subprocess.run(['node', '--import', str(guard), '--input-type=module', '-e', script],
                                                capture_output=True, text=True, timeout=10,
                                                env={'PATH': os.environ.get('PATH', '')})
            self.assertEqual(result.returncode, 0, result.stderr[:400])
            self.assertEqual(len(digest), 64)


class ModelBoundaryTests(unittest.TestCase):
    def test_actual_cli_contract_disables_tools_settings_hooks_mcp_and_persistence(self):
        def fake(command, **kwargs):
            self.assertEqual(command[command.index('--tools')+1], '')
            for flag in ('--restricted', '--safe-mode', '--strict-mcp-config', '--no-session-persistence', '--json-schema'):
                self.assertIn(flag, command)
            self.assertNotIn('DATABASE_URL', kwargs['env'])
            self.assertEqual(list(Path(kwargs['cwd']).iterdir()), [])
            return mock.Mock(returncode=0, stdout=json.dumps({'structured_output': {'files': [
                {'path': 'app.py', 'content': 'def solve(p): return p'}]}, 'total_cost_usd': .01}))
        with mock.patch.dict(os.environ, {'DATABASE_URL': 'synthetic-not-a-credential'}), \
             mock.patch.object(agent_tasks.subprocess, 'run', side_effect=fake):
            result = agent_tasks.call_claude('public synthetic fixture', model='test-model', budget=.1, timeout=10)
        self.assertEqual(result['cost_usd'], .01)

    def test_unavailable_hybrid_and_budget_failures_keep_scheduled_denominators(self):
        ask = mock.Mock(return_value={'output': {'files': [{'path': 'app.py', 'content': 'def solve(p): return {}'}]}})
        verify = lambda *args: {'name': 'unit synthetic', 'passed': True, 'verifier': 'injected-test-only'}
        run = agent_tasks.run(model='unit-only', budget_usd=.1, per_attempt_usd=.1, max_cases=1, ask=ask, verify=verify)
        self.assertEqual(sum(m['scheduled'] for m in run['metrics'].values()), 6)
        self.assertEqual(run['model_calls'], 1)
        self.assertEqual(ask.call_count, 1)
        self.assertEqual(run['metrics']['current_hybrid']['success_rate'], 0)
        self.assertIn('not_live', run['measurement'])

    def test_hybrid_attestation_is_required_and_never_faked(self):
        ask = mock.Mock()
        run = agent_tasks.run(model='unit', budget_usd=1, max_cases=1, baselines=('corrected_hybrid',), ask=ask,
                              retrievers={'corrected_hybrid': lambda case: {'available': True, 'context': 'unattested'}})
        self.assertEqual(ask.call_count, 0)
        self.assertIn('attest', run['records'][0]['error'])

    def test_provider_budget_failure_stops_later_calls_and_preserves_scheduled_denominators(self):
        ask = mock.Mock(side_effect=RuntimeError("You've hit your weekly limit; resets tomorrow"))
        run = agent_tasks.run(model='unit', budget_usd=10, max_cases=1,
            baselines=('no_memory', 'static_instructions', 'scoped_lexical', 'bounded_history'), ask=ask)
        self.assertEqual(ask.call_count, 1)
        self.assertEqual(sum(value['scheduled'] for value in run['metrics'].values()), 4)
        self.assertEqual(sum(value['attempted'] for value in run['metrics'].values()), 1)
        self.assertEqual(run['model_calls'], 1)
        self.assertTrue(run['halted_model_reason'])

    def test_empty_and_duplicate_baseline_plans_fail_before_spend(self):
        for baselines in [(), ('no_memory', 'no_memory')]:
            with self.assertRaises(ValueError):
                agent_tasks.run(model='unit', budget_usd=1, baselines=baselines)
        for amount in (float('nan'), float('inf'), -1):
            with self.assertRaises(ValueError):
                agent_tasks.run(model='unit', budget_usd=amount)

    def test_task_trends_require_complete_same_fingerprints_and_three_prior_runs(self):
        current = {'t': 'now', 'comparable': True, 'fingerprint': {'fixture': 'frozen', 'model': 'fixed'},
                   'metrics': {'scoped_lexical': {'scheduled': 4, 'success_rate': .5}}}
        previous = [{**current, 't': str(i), 'metrics': {'scoped_lexical': {'scheduled': 4, 'success_rate': 1}}}
                    for i in range(3)]
        self.assertEqual(len(evals_cli.agent_task_regressions(current, previous)), 1)
        self.assertEqual(evals_cli.agent_task_regressions(current, previous[:2]), [])
        self.assertEqual(evals_cli.agent_task_regressions({**current, 'comparable': False}, previous), [])
        self.assertEqual(evals_cli.agent_task_regressions({**current, 'fingerprint': {'fixture': 'changed'}}, previous), [])


@unittest.skipUnless((sys.platform == 'darwin' and Path('/usr/bin/sandbox-exec').exists()) or
                     (sys.platform.startswith('linux') and shutil.which('bwrap')), 'mandatory OS sandbox unavailable')
class ExecutableVerifierTests(unittest.TestCase):
    def setUp(self):
        self.case = next(c for c in agent_tasks.fixtures() if c['id'] == 'deadlines-changed_environment')

    def test_trusted_solution_passes_actual_parent_verifier(self):
        result = agent_tasks.verify_code(DEADLINE_CODE, self.case, 17)
        self.assertTrue(result['passed'])
        self.assertEqual(result['cases'], 5)

    def test_broken_implementation_fails_actual_parent_verifier(self):
        result = agent_tasks.verify_code('def solve(payload):\n    return {}\n', self.case, 17)
        self.assertFalse(result['passed'])

    def test_os_boundary_denies_a_synthetic_sibling_file(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory)/'synthetic-sibling.txt'
            marker.write_text('synthetic only')
            code = f'def solve(payload):\n    return open({str(marker)!r}).read()\n'
            # Deliberately bypass AST admission to independently verify the OS boundary.
            with self.assertRaises(RuntimeError):
                agent_tasks.verify_code(code, self.case, 17)

    def test_output_is_bounded_before_parent_memory_growth(self):
        code = "def solve(payload):\n    print('x' * 200000)\n    return {}\n"
        with self.assertRaises(ValueError):
            agent_tasks.verify_code(code, self.case, 17)


if __name__ == '__main__':
    unittest.main()
