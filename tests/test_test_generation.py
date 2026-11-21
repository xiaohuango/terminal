import io
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_generation.generate_tests import (
    build_test_sh, build_test_state, generate_task, run, session, validate_checks,
)
from test_generation.state import difference, snapshot


CHECK = {"id": "result", "requirement": "Write /app/result.txt", "path": "/app/result.txt",
         "kind": "file", "op": "eq", "expected": True}


class GenerationTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def evaluate(self, checks):
        namespace = {"__name__": "generated_tests"}
        exec(compile(build_test_state(checks), "test_state.py", "exec"), namespace)
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(namespace["StateTests"])
        return unittest.TextTestRunner(stream=io.StringIO()).run(suite)

    def test_content_delta_ignores_mtime_and_detects_same_size_edits_and_deletion(self):
        path = self.root / "record.json"
        path.write_text('{"x":1}', encoding="utf-8")
        first = snapshot([str(self.root)])
        os.utime(path, (100, 100))
        self.assertEqual(difference(first, snapshot([str(self.root)]))["modified"], {})
        path.write_text('{"x":2}', encoding="utf-8")
        second = snapshot([str(self.root)])
        self.assertIn(str(path), difference(first, second)["modified"])
        path.unlink()
        self.assertIn(str(path), difference(second, snapshot([str(self.root)]))["deleted"])

    def test_snapshot_limits_are_explicit(self):
        (self.root / "data").write_text("large content", encoding="utf-8")
        state = snapshot([str(self.root)], max_bytes=2)
        item = state["entries"][str(self.root / "data")]
        self.assertIn("sha256", item)
        self.assertNotIn("text", item)
        self.assertTrue(snapshot([str(self.root)], max_entries=1)["errors"])

    def test_every_check_requires_instruction_quote_and_unique_id(self):
        for raw in ({"checks": []}, {"checks": [dict(CHECK, requirement="invented")]},
                    {"checks": [CHECK, CHECK]}):
            with self.assertRaises(ValueError):
                validate_checks(raw, CHECK["requirement"])

    def test_rejects_traversal_volatile_fields_and_injected_operations(self):
        for change in ({"path": "/app/../etc/passwd"}, {"kind": "python"},
                       {"op": "eval"}, {"kind": "json", "selector": ["timestamp"]},
                       {"op": "count", "expected": True}):
            with self.assertRaises(ValueError):
                validate_checks({"checks": [dict(CHECK, **change)]}, CHECK["requirement"])

    def test_exact_text_rejects_wrong_content_newline_and_invalid_utf8(self):
        path = self.root / "result.txt"
        check = dict(CHECK, kind="text", path=str(path), selector=[], expected="hello world")
        path.write_bytes(b"hello world")
        self.assertTrue(self.evaluate([check]).wasSuccessful())
        for data in (b"hello world\n", b"hello world\r\n", b"wrong", b"\xff"):
            path.write_bytes(data)
            self.assertFalse(self.evaluate([check]).wasSuccessful())

    def test_structured_fields_record_counts_and_directory_entries(self):
        path = self.root / "records.json"
        path.write_text('{"records":[{"name":"alice"},{"name":"bob"}]}', encoding="utf-8")
        checks = [dict(CHECK, id="count", path=str(path), kind="json", selector=["records"], op="count", expected=2),
                  dict(CHECK, id="name", path=str(path), kind="json", selector=["records", 1, "name"], expected="bob"),
                  dict(CHECK, id="keys", path=str(path), kind="json", selector=[], op="keys", expected=["records"]),
                  dict(CHECK, id="entries", path=str(self.root), kind="entries", selector=[], expected=["records.json"])]
        self.assertTrue(self.evaluate(checks).wasSuccessful())
        path.write_text('{"records":[{"name":"alice"}]}', encoding="utf-8")
        self.assertFalse(self.evaluate(checks).wasSuccessful())

    def test_csv_rows_and_fields(self):
        path = self.root / "records.csv"
        path.write_text("name,score\nalice,7\nbob,8\n", encoding="utf-8")
        checks = [dict(CHECK, id="count", path=str(path), kind="csv", selector=[], op="count", expected=2),
                  dict(CHECK, id="score", path=str(path), kind="csv", selector=[1, "score"], expected="8")]
        self.assertTrue(self.evaluate(checks).wasSuccessful())
        path.write_text("name,score\nalice,7\nbob,9\n", encoding="utf-8")
        self.assertFalse(self.evaluate(checks).wasSuccessful())

    def test_expected_strings_are_data_and_cannot_inject_python(self):
        payload = "'); raise RuntimeError('injected') #"
        path = self.root / "text"
        path.write_text(payload, encoding="utf-8")
        check = dict(CHECK, path=str(path), kind="text", selector=[], expected=payload)
        self.assertTrue(self.evaluate([check]).wasSuccessful())

    def test_runner_sets_default_zero_reward_and_preserves_failure(self):
        runner = build_test_sh()
        self.assertLess(runner.index("printf '0"), runner.index("python3 /tests/test_state.py"))
        self.assertIn('exit "$status"', runner)

    def test_session_cleans_up_after_launch_failure(self):
        calls = []
        def command(argv, log, timeout, stdin=None):
            calls.append(log.stem)
            return {"exit_code": int(log.stem == "launch"), "log": str(log), "tail": "failed"}
        args = SimpleNamespace(docker="docker", timeout=10)
        with patch("test_generation.generate_tests.execute", side_effect=command):
            with self.assertRaises(ValueError):
                with session(args, self.root, "image", {"services": {}}, self.root / "session"):
                    self.fail("launch failure must not enter the session")
        self.assertEqual(calls[-1], "cleanup")

    def test_unverified_environment_is_rejected_without_running_docker(self):
        args = SimpleNamespace(output_dir=self.root)
        with patch("test_generation.generate_tests.execute") as execute:
            result = generate_task("42", {"status": "generated"}, args)
        self.assertEqual(result["status"], "error")
        execute.assert_not_called()

    def test_empty_upstream_needs_neither_model_nor_docker(self):
        report = self.root / "environment_report.json"
        report.write_text('{"records":{}}', encoding="utf-8")
        args = SimpleNamespace(environment_report=report, output_dir=self.root / "output", ids=None,
                               model=None, reviews=None, timeout=30, build_timeout=30)
        with patch("test_generation.generate_tests.execute") as execute:
            self.assertEqual(run(args), 0)
        execute.assert_not_called()
        output = json.loads((args.output_dir / "test_generation_report.json").read_text(encoding="utf-8"))
        self.assertEqual(output["summary"], {"total": 0})

    def stage_three(self):
        task = self.root / "stage3"
        (task / "solution").mkdir(parents=True)
        (task / "environment").mkdir()
        script = "printf result > /app/result.txt\n"
        (task / "solution/solve.sh").write_text(script, encoding="utf-8")
        (task / "instruction.md").write_text(CHECK["requirement"], encoding="utf-8")
        (task / "plan.json").write_text(json.dumps({"services": {}, "workdir": "/app",
                                                    "outputs": ["/app/result.txt"]}), encoding="utf-8")
        return {"status": "verified", "environment": str(task / "environment"), "run_dir": str(task),
                "image": "example:1", "solution_sha256": hashlib.sha256(script.encode()).hexdigest()}

    def test_model_receives_delta_and_instruction_and_both_states_are_verified(self):
        record = self.stage_three()
        args = SimpleNamespace(output_dir=self.root / "output", docker="docker", timeout=10)
        before = {"entries": {}, "errors": []}
        after = {"entries": {"/app/result.txt": {"kind": "file", "text": "result"}}, "errors": []}
        @contextmanager
        def fake_session(*args):
            def call(*args, **kwargs):
                return {"exit_code": 1, "tail": "FAIL: test_result"}
            yield call, lambda *a, **k: None, [], {}
        def capture(require, prefix, roots, destination, step):
            return before if step == "before" else after
        complete = unittest.mock.Mock(return_value={"checks": [CHECK]})
        with patch("test_generation.generate_tests.execute", return_value={"exit_code": 0, "tail": "linux"}), patch(
            "test_generation.generate_tests.session", side_effect=fake_session
        ), patch("test_generation.generate_tests.capture", side_effect=capture):
            result = generate_task("42", record, args, complete=complete)
        self.assertEqual(result["status"], "verified")
        evidence = complete.call_args.args[2]
        self.assertEqual(evidence["instruction"], CHECK["requirement"])
        self.assertEqual(evidence["delta"]["created"], after["entries"])
        self.assertEqual(evidence["before"], before)
        self.assertEqual(evidence["after"], after)

    def test_reference_failure_stops_generation(self):
        record = self.stage_three()
        args = SimpleNamespace(output_dir=self.root / "output", docker="docker", timeout=10)
        @contextmanager
        def fake_session(*args):
            def require(step, **kwargs):
                if step == "replay":
                    raise ValueError("replay failed")
            yield None, require, [], {}
        complete = unittest.mock.Mock()
        with patch("test_generation.generate_tests.execute", return_value={"exit_code": 0, "tail": "linux"}), patch(
            "test_generation.generate_tests.session", side_effect=fake_session
        ), patch("test_generation.generate_tests.capture", return_value={"entries": {}, "errors": []}):
            result = generate_task("42", record, args, complete=complete)
        self.assertEqual(result["status"], "error")
        self.assertIn("replay failed", result["message"])
        complete.assert_not_called()
        self.assertFalse((Path(result["run_dir"]) / "tests/test_state.py").exists())

    def test_missing_docker_is_blocked_and_does_not_call_model(self):
        record = self.stage_three()
        args = SimpleNamespace(output_dir=self.root / "output", docker="docker")
        complete = unittest.mock.Mock()
        with patch("test_generation.generate_tests.execute", return_value={"exit_code": 127, "tail": "missing"}):
            result = generate_task("42", record, args, complete=complete)
        self.assertEqual(result["status"], "blocked")
        complete.assert_not_called()

    def test_changed_reference_is_rejected_before_docker(self):
        record = self.stage_three()
        record["solution_sha256"] = "changed"
        with patch("test_generation.generate_tests.execute") as execute:
            result = generate_task("42", record, SimpleNamespace(output_dir=self.root / "output"))
        self.assertEqual(result["status"], "error")
        self.assertIn("no longer matches", result["message"])
        execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
