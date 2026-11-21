from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from task_synthesis.extract_solution import EXTRACT, REFINE
from task_synthesis.generate_instruction import AUDIT, INSTRUCT, generate_instruction, render_instruction
from task_synthesis.synthesize_tasks import check_syntax, model_completion, run, synthesize


ROOT = Path(__file__).resolve().parents[1]
SOURCE = (
    "$ cat missing.txt\ncat: missing.txt: No such file or directory\n"
    "$ pwd\n/app\n$ cat > /app/result.txt <<'EOF'\n> red\n> \n> blue\n> EOF\n"
    "$ cat /app/result.txt\nred\n\nblue\n"
)
SCRIPT = "#!/bin/bash\ncat > /app/result.txt <<'EOF'\nred\n\nblue\nEOF\n"
REVIEW = {
    "keep": [[5, 9]],
    "goal": "Produce a two-color report with a blank separator line.",
    "requirements": [{
        "text": "Save the plain text report to `/app/result.txt` with red and blue separated by one blank line.",
        "path": "/app/result.txt", "format": "plain text",
    }],
    "checks": [{"status": "met", "lines": [2, 3, 4, 5], "reason": "The heredoc writes the requested lines."}],
    "omissions": [],
}


class SynthesisTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(dir=ROOT)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.recording = self.root / "recordings" / "42"
        self.recording.mkdir(parents=True)
        (self.recording / "recording.txt").write_bytes(SOURCE.encode())
        (self.recording / "info.json").write_text(json.dumps({
            "title": "Color report", "description": REVIEW["goal"],
        }), encoding="utf-8")
        self.task = self.root / "task"

    def test_offline_review_omits_failed_command_and_preserves_heredoc(self):
        with patch("task_synthesis.synthesize_tasks.check_syntax", return_value={"status": "passed"}) as syntax:
            result = synthesize(self.recording, {}, self.task, "bash", None, REVIEW)
            repeated = synthesize(self.recording, {}, self.task, "bash", None, REVIEW)
        self.assertEqual(result, repeated)
        self.assertEqual(result["status"], "synthesized")
        syntax.assert_called_with("bash", SCRIPT)
        self.assertEqual((self.task / "solution" / "solve.sh").read_bytes(), SCRIPT.encode())
        instruction = (self.task / "instruction.md").read_text(encoding="utf-8")
        self.assertIn("plain text report to `/app/result.txt`", instruction)
        self.assertNotIn("cat >", instruction)

    def test_model_steps_use_reference_and_block_unmet_outputs(self):
        calls = []
        instruction_budgets = []

        def model_response(prompt, data):
            calls.append(prompt)
            if prompt == EXTRACT:
                self.assertIn("No such file", data["transcript"])
                return {"script": SCRIPT}
            if prompt == REFINE:
                self.assertEqual(data["extracted"], SCRIPT)
                return {"script": SCRIPT}
            if prompt == INSTRUCT:
                self.assertEqual(data, {"title": "Color report", "description": REVIEW["goal"],
                                        "environment": "", "script": SCRIPT})
                return REVIEW
            self.assertEqual(data["script_lines"]["4"], "")
            return {"checks": [{"status": "unmet", "lines": [], "reason": "Requested result is incomplete."}],
                    "omissions": []}

        def completion(**request):
            system, user = request["messages"]
            self.assertEqual(request["reasoning_effort"], "low")
            response = model_response(system["content"], json.loads(user["content"]))
            if system["content"] == INSTRUCT:
                instruction_budgets.append(request["max_tokens"])
                if len(instruction_budgets) == 1:
                    return SimpleNamespace(choices=[SimpleNamespace(
                        finish_reason="length", message=SimpleNamespace(content="{partial"),
                    )])
            return SimpleNamespace(choices=[SimpleNamespace(
                finish_reason="stop", message=SimpleNamespace(content=json.dumps(response)),
            )])

        def syntax(bash, script):
            self.assertEqual(script, SCRIPT)
            calls.append("syntax")
            return {"status": "passed"}

        with patch.dict(sys.modules, {"litellm": SimpleNamespace(completion=completion)}):
            complete = model_completion("deepseek/deepseek-flash")
            with patch("task_synthesis.synthesize_tasks.check_syntax", side_effect=syntax):
                result = synthesize(self.recording, {}, self.task, "bash", complete, None)
        self.assertEqual(calls, [EXTRACT, REFINE, "syntax", INSTRUCT, INSTRUCT, AUDIT])
        self.assertEqual(instruction_budgets, [8192, 16384])
        self.assertEqual(result["status"], "review", result)
        self.assertEqual(result["audit"]["checks"][0]["status"], "unmet")
        self.assertFalse(self.task.exists())

    def test_empty_filter_never_probes_bash_or_reads_the_recording(self):
        selected = self.root / "filtered_recordings.json"
        selected.write_text("{}", encoding="utf-8")
        args = SimpleNamespace(input=selected, ids=None, output_dir=self.root, model=None, reviews=None)
        with redirect_stdout(StringIO()), patch("task_synthesis.synthesize_tasks.bash_executable") as bash:
            self.assertEqual(run(args), 0)
        bash.assert_not_called()
        report = json.loads((self.root / "synthesis_report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["summary"], {"total": 0, "synthesized": 0, "review": 0, "error": 0})

    def test_render_adds_structured_output_details_missing_from_prose(self):
        spec = {**REVIEW, "requirements": [{
            "text": "Save the report.", "path": "/app/result.txt", "format": "plain text",
        }]}
        instruction, requirements = render_instruction(spec)
        self.assertIn("`/app/result.txt`", requirements[0]["text"])
        self.assertIn("plain text", requirements[0]["text"])
        self.assertIn(requirements[0]["text"], instruction)

    def test_model_repairs_missing_output_format_before_audit(self):
        invalid = {**REVIEW, "requirements": [{**REVIEW["requirements"][0], "format": None}]}

        def complete(operation, prompt, data):
            if operation == "generate_instruction":
                return invalid
            if operation == "repair_instruction":
                self.assertIn("output format", data["validation_error"])
                return REVIEW
            self.assertEqual(operation, "audit_instruction")
            self.assertEqual(data["requirements"], REVIEW["requirements"])
            return REVIEW

        instruction, audit = generate_instruction({}, SCRIPT, complete)
        self.assertIn("plain text", instruction)
        self.assertEqual(audit["checks"][0]["status"], "met")

    def test_account_error_stops_further_requests_and_reports_blocked_ids(self):
        class BadRequestError(Exception):
            status_code = 400

        calls = []

        def completion(**request):
            calls.append(request["model"])
            raise BadRequestError("Insufficient Balance")

        selected = self.root / "filtered_recordings.json"
        selected.write_text('{"42": {}, "43": {}}', encoding="utf-8")
        args = SimpleNamespace(input=selected, ids=None, output_dir=self.root, model="deepseek/deepseek-flash",
                               reviews=None, bash="bash", recordings_dir=self.recording.parent)
        with patch.dict(sys.modules, {"litellm": SimpleNamespace(completion=completion)}), patch(
            "task_synthesis.synthesize_tasks.check_syntax", return_value={"status": "passed"}
        ), redirect_stdout(StringIO()):
            self.assertEqual(run(args), 1)
        self.assertEqual(calls, [args.model])
        report = json.loads((self.root / "synthesis_report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["records"]["42"]["model_call"]["http_status"], 402)
        self.assertEqual(report["records"]["43"]["status"], "blocked")

    def test_truncated_instruction_reports_specific_call_without_model_text(self):
        def completion(**request):
            prompt = request["messages"][0]["content"]
            content = json.dumps({"script": SCRIPT}) if prompt in (EXTRACT, REFINE) else None
            return SimpleNamespace(choices=[SimpleNamespace(
                finish_reason="stop" if content else "length", message=SimpleNamespace(content=content),
            )], usage=SimpleNamespace(completion_tokens=request["max_tokens"]))

        with patch.dict(sys.modules, {"litellm": SimpleNamespace(completion=completion)}), patch(
            "task_synthesis.synthesize_tasks.check_syntax", return_value={"status": "passed"}
        ):
            result = synthesize(self.recording, {}, self.task, "bash", model_completion("mock"), None)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["model_call"], {
            "operation": "generate_instruction", "finish_reason": "length",
            "content_type": "NoneType", "max_tokens": 32768, "completion_tokens": 32768,
        })
        self.assertFalse(self.task.exists())

    def test_bash_heredoc_warning_blocks_publication_even_with_zero_exit(self):
        diagnostic = b"warning: here-document delimited by end-of-file\n"
        with patch("task_synthesis.synthesize_tasks.subprocess.run",
                   return_value=subprocess.CompletedProcess([], 0, b"", diagnostic)) as bash:
            result = check_syntax("bash", SCRIPT)
        self.assertEqual(bash.call_args.args[0], ["bash", "--noprofile", "--norc", "-n"])
        self.assertEqual(result, {"status": "failed", "diagnostic": diagnostic.decode().strip()})
