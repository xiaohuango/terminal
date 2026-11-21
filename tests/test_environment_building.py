import copy
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from environment_building.build_environment import (
    build_task, execute, output_check, replay, run, validate_plan, write_context,
)


PLAN = {"base_image": "debian:bookworm-slim", "workdir": "/app", "system_packages": [],
        "setup": "", "initial_files": [], "outputs": ["/app/result.txt"], "services": {}}


class EnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.task = self.root / "tasks/42"
        (self.task / "solution").mkdir(parents=True)
        (self.task / "solution/solve.sh").write_text("printf success > /app/result.txt\n", encoding="utf-8")
        (self.task / "instruction.md").write_text("Write /app/result.txt.", encoding="utf-8")
        self.args = SimpleNamespace(max_attempts=2, generate_only=False, docker="docker", build_timeout=20,
                                    replay_timeout=10)

    def test_rejects_initial_output_and_traversal(self):
        for path in ("/app/result.txt", "/app/result.txt/child", "/app/../etc/passwd"):
            plan = copy.deepcopy(PLAN)
            plan["initial_files"] = [{"path": path, "content": "leak"}]
            with self.assertRaises(ValueError):
                validate_plan(plan)

    def test_context_never_copies_reference(self):
        plan = validate_plan(copy.deepcopy(PLAN))
        context = self.root / "environment"
        write_context(context, plan, "test:1")
        text = (context / "Dockerfile").read_text()
        self.assertNotIn("solve.sh", text)
        self.assertNotIn("result.txt", text)
        self.assertNotIn("COPY .", text)
        self.assertFalse((context / "compose.yaml").exists())

    def test_compose_has_health_dependency_and_no_host_resources(self):
        plan = copy.deepcopy(PLAN)
        plan["services"] = {"redis": {"image": "redis:7", "healthcheck": {"test": ["CMD", "redis-cli", "ping"]}}}
        context = self.root / "environment"
        write_context(context, validate_plan(plan), "test:1")
        services = json.loads((context / "compose.yaml").read_text())["services"]
        self.assertEqual(services["task"]["depends_on"]["redis"], {"condition": "service_healthy"})
        plan["services"]["redis"]["volumes"] = ["/:/host"]
        with self.assertRaises(ValueError):
            validate_plan(plan)

    def test_contamination_blocks_reference_and_cleans_container(self):
        calls = []
        def command(argv, log, timeout, stdin=None):
            calls.append((argv, log.stem, stdin))
            return {"exit_code": int(log.stem == "initial_state"), "log": str(log), "tail": ""}
        result = replay("docker", self.root, "image", PLAN, "REFERENCE", self.root, 10, command)
        self.assertNotIn("replay", result)
        self.assertEqual(calls[-1][1], "cleanup")
        self.assertIn("[ ! -L", calls[-2][2])
        self.assertFalse(any("--mount" in c[0] or "--privileged" in c[0] for c in calls))

    def test_nonzero_reference_is_not_verified(self):
        def command(argv, log, timeout, stdin=None):
            return {"exit_code": 7 if log.stem == "replay" else 0, "log": str(log), "tail": ""}
        result = replay("docker", self.root, "image", PLAN, "exit 7", self.root, 10, command)
        self.assertEqual(result["replay"]["exit_code"], 7)
        self.assertNotIn("final_state", result)
        self.assertEqual(result["cleanup"]["exit_code"], 0)

    def test_repair_receives_logs_and_rebuilds_without_modifying_solution(self):
        calls = []
        def complete(operation, prompt, source):
            calls.append((operation, source))
            return copy.deepcopy(PLAN)
        builds = 0
        def command(argv, log, timeout, stdin=None):
            nonlocal builds
            if "info" in argv:
                return {"exit_code": 0, "tail": "linux\n"}
            builds += 1
            return {"exit_code": int(builds == 1), "tail": "missing package" if builds == 1 else "built"}
        checks = {key: {"exit_code": 0} for key in ("initial_state", "replay", "final_state", "cleanup")}
        with patch("environment_building.build_environment.execute", side_effect=command), patch(
            "environment_building.build_environment.replay", return_value=checks
        ):
            result = build_task(self.task, None, self.root / "output", self.args, complete)
        self.assertEqual(result["status"], "verified")
        self.assertEqual(calls[1][0], "repair_environment")
        self.assertEqual(calls[1][1]["diagnostics"][0]["build"]["tail"], "missing package")
        self.assertEqual((self.task / "solution/solve.sh").read_text(), "printf success > /app/result.txt\n")
        self.assertNotEqual(result["attempts"][0]["image"], result["attempts"][1]["image"])

    def test_generation_is_not_reported_as_verified(self):
        self.args.generate_only = True
        result = build_task(self.task, None, self.root / "output", self.args, review=copy.deepcopy(PLAN))
        self.assertEqual(result["status"], "generated")
        self.assertNotIn("replay_exit_code", result)

    def test_missing_docker_records_blocked_after_context_generation(self):
        with patch("environment_building.build_environment.execute", return_value={"exit_code": 127, "tail": "missing"}):
            result = build_task(self.task, None, self.root / "output", self.args, review=copy.deepcopy(PLAN))
        self.assertEqual(result["status"], "blocked")
        self.assertTrue((Path(result["environment"]) / "Dockerfile").is_file())

    def test_timeout_persists_diagnostic(self):
        result = execute([sys.executable, "-c", "import time; time.sleep(5)"], self.root / "timeout.log", .05)
        self.assertEqual(result["exit_code"], 124)
        self.assertIn("TIMEOUT", result["tail"])

    def test_empty_stage_two_needs_neither_docker_nor_model(self):
        self.args.tasks_dir = self.root / "missing"
        self.args.ids = None
        self.args.model = self.args.plans = None
        self.args.output_dir = self.root / "empty"
        with patch("environment_building.build_environment.execute") as command:
            self.assertEqual(run(self.args), 0)
        command.assert_not_called()


if __name__ == "__main__":
    unittest.main()
