import contextlib
import hashlib
import io
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

from test_generation.generate_tests import build_test_state
from task_verification.verify_tasks import assess, make_partial, run, run_trial, verify_task


CHECK = {'id': 'file', 'requirement': 'Write result', 'path': '/app/result.txt',
         'kind': 'file', 'op': 'eq', 'expected': True}


class VerificationTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.args = SimpleNamespace(output_dir=self.root / 'out', docker='docker', timeout=10, max_attempts=3)

    def test_exact_trial_rules_and_errors_never_qualify(self):
        rows = lambda *states: [{'status': s} for s in states]
        self.assertTrue(assess('AllPassing', rows('passed', 'passed')))
        self.assertFalse(assess('AllPassing', rows('passed', 'failed')))
        self.assertTrue(assess('Nop', rows('failed', 'failed')))
        self.assertFalse(assess('Nop', rows('failed', 'passed')))
        self.assertTrue(assess('Partial', rows('passed', 'failed')))
        self.assertFalse(assess('Partial', rows('passed')))
        for name in ('AllPassing', 'Nop', 'Partial'):
            self.assertFalse(assess(name, rows('failed', 'error')))
            self.assertFalse(assess(name, []))

    def test_partial_is_unique_reference_ablation_with_requirement(self):
        spec = {'omit': 'f.write(message)', 'replacement': 'pass', 'reason': 'omit output', 'requirement': 'Write result'}
        self.assertEqual(make_partial('with open(p) as f:\n    f.write(message)\n', spec, 'Write result'),
                         'with open(p) as f:\n    pass\n')
        for change in ({'omit': ''}, {'omit': 'missing'}, {'replacement': 'exit 1'}, {'requirement': 'invented'}, {'reason': ''}):
            with self.assertRaises(ValueError):
                make_partial('f.write(message)', {**spec, **change}, 'Write result')
        with self.assertRaises(ValueError):
            make_partial('f.write(message)\nf.write(message)', spec, 'Write result')

    def evaluate_runner(self, check, helper=''):
        module = ModuleType('test_state')
        exec(build_test_state([check]) + helper, module.__dict__)
        output = io.StringIO()
        runner = Path(__file__).parents[1] / 'task_verification/runner.py'
        # Patch only runner log writes; generated test module retains real Path.
        with patch.dict(sys.modules, {'test_state': module}), patch('pathlib.Path'), contextlib.redirect_stdout(output):
            with self.assertRaises(SystemExit) as stopped:
                exec(compile(runner.read_text(encoding='utf-8'), str(runner), 'exec'), {'__name__': '__main__'})
        return stopped.exception.code, json.loads(output.getvalue())

    def test_runner_missing_invalid_and_incorrect_artifacts_are_assertions(self):
        target = self.root / 'result'
        check = {**CHECK, 'path': str(target), 'kind': 'text', 'expected': 'hello'}
        for data in (None, b'wrong', b'\xff'):
            if data is not None:
                target.write_bytes(data)
            code, report = self.evaluate_runner(check)
            self.assertEqual(code, 1)
            self.assertEqual(report['assertions'][0]['status'], 'failed')
        target.write_bytes(b'hello')
        code, report = self.evaluate_runner(check)
        self.assertEqual(code, 0)
        self.assertEqual(report['assertions'][0]['status'], 'passed')

    def test_runner_permission_or_dependency_errors_are_not_assertion_failures(self):
        helper = '\ndef broken(check):\n    raise PermissionError("denied")\nobserve = broken\n'
        code, report = self.evaluate_runner(CHECK, helper)
        self.assertEqual(code, 2)
        self.assertEqual(report['assertions'][0]['status'], 'error')

    def fake_session(self, rows, failure=None, malformed=False):
        @contextlib.contextmanager
        def fake(args, context, image, plan, folder):
            folder.mkdir(parents=True, exist_ok=True)
            def call(step, command=None, stdin=None):
                log = folder / (step + '.log')
                payload = {'assertions': rows, 'tests_run': len(rows)}
                log.write_text('[]\n' + ('invalid JSON' if malformed else json.dumps(payload)), encoding='utf-8')
                code = 2 if any(r['status'] == 'error' for r in rows) else 1 if any(r['status'] == 'failed' for r in rows) else 0
                return {'log': str(log), 'exit_code': code}
            def require(step, command=None, stdin=None):
                if step == failure:
                    raise ValueError(step + ' failed')
                return call(step, command, stdin)
            yield call, require, [], {}
        return fake

    def trial(self, rows, name='Nop', **options):
        (self.root / 'tests').mkdir(exist_ok=True)
        (self.root / 'tests/test_state.py').write_text(build_test_state([CHECK]))
        (self.root / 'tests/test.sh').write_text('python3 /tests/runner.py\n')
        with patch('task_verification.verify_tasks.session', self.fake_session(rows, **options)):
            return run_trial(name, None if name == 'Nop' else 'echo partial', [CHECK], self.args,
                             self.root, 'image', {}, self.root / 'trial', self.root / 'tests')

    def test_incomplete_or_malformed_executor_output_is_error(self):
        for rows, options in (([], {}), ([{'id': 'file', 'status': 'failed'}], {'malformed': True})):
            result = self.trial(rows, **options)
            self.assertEqual(result['status'], 'error')
            self.assertEqual(result['errors'][0]['category'], 'test_executor')

    def test_test_errors_and_solution_errors_cannot_validate_partial(self):
        result = self.trial([{'id': 'file', 'status': 'error', 'detail': 'import error'}], 'Partial')
        self.assertEqual(result['status'], 'error')
        self.assertNotIn('failed', result['counts'])
        result = self.trial([], 'Partial', failure='solution_execution')
        self.assertEqual(result['errors'][0]['category'], 'solution_execution')
        self.assertEqual(result['assertions'], [])

    def test_launch_and_dependency_failures_have_separate_categories(self):
        for step, category in [('launch', 'container_start'), ('verifier_runtime', 'test_dependency')]:
            @contextlib.contextmanager
            def broken(args, context, image, plan, folder):
                folder.mkdir(parents=True)
                (folder / 'execution.json').write_text(json.dumps({step: {'exit_code': 1}}))
                raise ValueError(step + ' failed')
                yield
            with patch('task_verification.verify_tasks.session', broken):
                result = run_trial('Nop', None, [CHECK], self.args, self.root, 'image', {},
                                   self.root / step, self.root)
            self.assertEqual(result['errors'][0]['category'], category)
            self.assertEqual(result['assertions'], [])

    def upstream(self):
        task, source = self.root / 'stage3', self.root / 'stage4'
        (task / 'solution').mkdir(parents=True)
        (task / 'attempt/environment').mkdir(parents=True)
        source.mkdir()
        script = 'echo result > /app/result.txt\necho done\n'
        (task / 'solution/solve.sh').write_text(script)
        (task / 'instruction.md').write_text('Write result')
        (source / 'instruction.md').write_text('Write result')
        sha = hashlib.sha256(script.encode()).hexdigest()
        (source / 'assertions.json').write_text(json.dumps({'checks': [CHECK], 'solution_sha256': sha}))
        (task / 'attempt/plan.json').write_text('{"services":{}}')
        env = {'status': 'verified', 'run_dir': str(task), 'environment': str(task / 'attempt/environment'),
               'solution_sha256': sha, 'image': 'example'}
        record = {'status': 'verified', 'run_dir': str(source)}
        proposal = {'partial': {'omit': 'echo result > /app/result.txt\n', 'reason': 'omit output', 'requirement': 'Write result'},
                    'revisions': [{'reason': 'fix weak check'}]}
        return record, env, proposal

    def test_repair_reruns_all_three_and_preserves_rounds(self):
        record, env, proposal = self.upstream()
        calls = []
        def trial(name, *args):
            calls.append(name)
            status = 'rejected' if len(calls) == 3 else 'accepted'
            return {'status': status, 'assertions': [], 'errors': []}
        with patch('task_verification.verify_tasks.execute', side_effect=[{'exit_code': 0, 'tail': 'linux'},
                    {'exit_code': 0, 'tail': 'sha256:abc'}]), patch('task_verification.verify_tasks.run_trial', side_effect=trial):
            result = verify_task('42', record, env, self.args, proposal)
        self.assertEqual(calls, ['AllPassing', 'Nop', 'Partial'] * 2)
        self.assertEqual(result['status'], 'verified')
        self.assertEqual(len(result['attempts']), 2)

    def test_changed_reference_rejected_before_docker(self):
        record, env, proposal = self.upstream()
        env['solution_sha256'] = 'changed'
        with patch('task_verification.verify_tasks.execute') as command:
            result = verify_task('42', record, env, self.args, proposal)
        self.assertEqual(result['status'], 'error')
        command.assert_not_called()

    def test_empty_batch_needs_no_model_or_docker(self):
        env = self.root / 'environment.json'
        env.write_text('{"records":{}}')
        upstream = self.root / 'tests.json'
        upstream.write_text(json.dumps({'environment_report': str(env), 'records': {}}))
        self.args.test_report, self.args.ids, self.args.plans, self.args.model = upstream, None, None, None
        with patch('task_verification.verify_tasks.execute') as command:
            self.assertEqual(run(self.args), 0)
        command.assert_not_called()


if __name__ == '__main__':
    unittest.main()
