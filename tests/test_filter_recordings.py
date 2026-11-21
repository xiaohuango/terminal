from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from data_filtering.checks import extract_commands, public_link_available, repository_available
from data_filtering.filter_recordings import run
from data_filtering.import_terminalworld_tasks import render_transcript


ROOT = Path(__file__).resolve().parents[1]
RECORDING = "$ ls\n$ sort items.txt > sorted.txt\n$ wc -l sorted.txt\n3 sorted.txt\n"
SCORES = {"state_action_alignment": 3, "task_complexity": 2, "signal_clarity": 3}


class FilterTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(dir=ROOT)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.recordings = self.root / "recordings"
        self.recordings.mkdir()
        self.output = self.root / "output"

    def json_file(self, path, value):
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def recording(self, recording_id, transcript=RECORDING, **info):
        folder = self.recordings / recording_id
        folder.mkdir()
        (folder / "recording.txt").write_text(transcript, encoding="utf-8")
        self.json_file(folder / "info.json", {"duration": "0:25", **info})
        return folder

    def read_output(self, filename):
        return json.loads((self.output / filename).read_text(encoding="utf-8"))

    def test_cli_admits_only_valid_scored_recordings(self):
        url = "https://github.com/example/task.git"
        dependency_recording = "$ git clone \\" + f"\n>   {url}\n$ make -C task\n$ ls task\n"
        self.recording("accepted")
        self.recording("short", duration="2s")
        self.recording("private", title="Contact learner@example.org")
        self.recording("credential", RECORDING + "$ export SERVICE_API_KEY=fakeExampleValue\n")
        self.recording("tui", RECORDING + "$ ls && sudo -u root /usr/bin/vim -b report.txt\n")
        self.recording("gui", RECORDING + "$ xdg-open sorted.txt\n")
        self.recording("windows", environment="Windows 11")
        self.recording("proprietary", RECORDING + "$ matlab -batch demo\n")
        self.recording("exploration", "$ pwd\n$ ls\n$ cat README\n")
        self.recording("dead_dependency", dependency_recording)
        self.recording("unknown_dependency", dependency_recording)
        self.recording("verified_dependency", dependency_recording)
        self.recording("pending_score")
        self.recording("low_quality")
        self.recording("bad_score")
        self.recording("unknown_commands", "plain text with no shell prompts")
        sidecar = self.recording("sidecar", "plain text with a command sidecar")
        self.json_file(sidecar / "commands.json", [{"command": line[2:]} for line in RECORDING.splitlines()[:3]])

        scores = self.json_file(self.root / "scores.json", {
            "accepted": SCORES, "sidecar": SCORES, "verified_dependency": {"llm_score": SCORES},
            "low_quality": {**SCORES, "signal_clarity": 1},
            "bad_score": {**SCORES, "task_complexity": True},
            "credential": SCORES,
        })
        index = self.json_file(self.root / "urls.json", {
            "dead_dependency": [{"url": url, "accessible": False}],
            "verified_dependency": [{"url": url, "accessible": True, "is_repo": True}],
        })
        result = subprocess.run(
            [sys.executable, "-B", "-m", "data_filtering.filter_recordings",
             "--recordings-dir", str(self.recordings), "--output-dir", str(self.output),
             "--scores", str(scores), "--url-index", str(index)],
            cwd=ROOT, capture_output=True, text=True, check=True,
        )
        self.assertIn("total=17 accepted=3 rejected=10 skipped=4", result.stdout)
        filtered = self.read_output("filtered_recordings.json")
        self.assertEqual(set(filtered), {"accepted", "sidecar", "verified_dependency"})
        self.assertEqual(filtered["verified_dependency"]["value_score"], 11)
        self.assertEqual(filtered["accepted"]["duration"], 25)
        report = self.read_output("filter_report.json")
        for recording_id, reason in {
            "short": "too_short", "private": "pii", "credential": "credential",
            "tui": "tui", "gui": "gui", "windows": "non_linux", "proprietary": "proprietary_dependency",
            "exploration": "exploratory_only", "dead_dependency": "dependency_inaccessible",
            "unknown_dependency": "dependency_unverified", "pending_score": "score_pending",
            "low_quality": "low_quality", "bad_score": "invalid_imported_score",
            "unknown_commands": "commands_unrecognized",
        }.items():
            self.assertIn(reason, report["records"][recording_id]["reasons"])
        outputs = json.dumps([filtered, report])
        self.assertNotIn("fakeExampleValue", outputs)
        self.assertNotIn("learner@example.org", outputs)

    def test_scoring_receives_only_rule_passes_and_saves_results(self):
        self.recording("ok", title="Sort items")
        self.recording("skip", RECORDING + "$ nano output.txt\n")
        self.recording("error", title="Mock provider failure")
        responses = iter([TimeoutError("provider timed out"), {"exclusion": None, **SCORES}])

        def complete(**request):
            self.assertNotIn("nano output.txt", request["messages"][1]["content"])
            response = next(responses)
            if isinstance(response, Exception):
                raise response
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(response)))])

        args = SimpleNamespace(
            recordings_dir=self.recordings, output_dir=self.output, url_index=None, scores=None,
            limit=None, model="mock", check_repos=False, check_links=False, workers=1,
            min_duration=10, min_commands=3, min_score=7, min_signal_clarity=2,
        )
        with patch.dict(sys.modules, {"litellm": SimpleNamespace(completion=complete)}), redirect_stdout(StringIO()):
            run(args)
        self.assertEqual(set(self.read_output("filtered_recordings.json")), {"ok"})
        rows = self.read_output("filter_report.json")["records"]
        self.assertEqual(rows["error"]["reasons"], ["scoring_failed"])
        self.assertEqual(rows["error"]["status"], "skipped")
        self.assertEqual(rows["ok"]["score_source"], "model")
        self.assertEqual(self.read_output("recording_value_scores.json")["ok"]["llm_score"], SCORES)
        self.assertEqual(rows["skip"]["reasons"], ["tui"])

    def test_repository_check_only_contacts_supported_hosts(self):
        with patch("data_filtering.checks.HTTPSConnection") as connect:
            self.assertIsNone(repository_available("https://example.org/code/repo"))
            self.assertIsNone(repository_available("https://github.com/example/repo?key=ignored"))
            connect.assert_not_called()
            response = MagicMock()
            response.__enter__.return_value.status = 200
            connect.return_value.getresponse.return_value = response
            self.assertTrue(repository_available("https://github.com/example/repo.git"))
            connect.assert_called_once_with("github.com", timeout=5)
            args, _ = connect.return_value.request.call_args
            self.assertEqual(args, ("HEAD", "/example/repo"))
            for status, expected in [(302, None), (404, False), (429, None)]:
                response.__enter__.return_value.status = status
                self.assertIs(repository_available("https://github.com/example/repo"), expected)

    def test_public_link_verifies_artifacts_and_ignores_unsupported_redirects(self):
        with patch("data_filtering.checks.HTTPSConnection") as connect:
            response = connect.return_value.getresponse.return_value.__enter__.return_value
            response.status = 200
            response.getheader.return_value = None
            self.assertTrue(public_link_available("https://cdn.kernel.org/archive.tar.xz"))
            connect.return_value.request.assert_called_once_with(
                "HEAD", "/archive.tar.xz", headers={"User-Agent": "TerminalWorld-Reproduction/1.0"},
            )
            connect.reset_mock()
            response.status = 302
            response.getheader.return_value = "https://127.0.0.1/archive"
            self.assertIsNone(public_link_available("https://cdn.kernel.org/archive.tar.xz"))
            connect.assert_called_once_with("cdn.kernel.org", timeout=6)

    def test_import_keeps_heredoc_body_out_of_command_count(self):
        script = "#!/bin/bash\ncat <<'EOF' > report.txt\n$ example prompt from file\nEOF\ncat report.txt\n"
        transcript = render_transcript(script)
        self.assertIn("> $ example prompt from file", transcript)
        self.assertEqual(extract_commands(transcript), ["cat <<'EOF' > report.txt", "cat report.txt"])


if __name__ == "__main__":
    unittest.main()
