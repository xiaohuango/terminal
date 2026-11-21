"""Synthesize task pairs from the recordings admitted by data filtering."""

import argparse
from collections import Counter
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from tempfile import TemporaryDirectory

from data_filtering.filter_recordings import load_recording, read_object, write_json

from .extract_solution import UnsupportedWorkflow, extract_solution
from .generate_instruction import generate_instruction


class ModelCallError(ValueError):
    def __init__(self, operation, message, **details):
        super().__init__(message)
        self.details = {"operation": operation, **details}


ACCOUNT_ERRORS = {401: "model API authentication failed", 402: "model API balance is insufficient"}


def model_completion(model):
    import litellm

    litellm.suppress_debug_info = True
    model_options = {"reasoning_effort": "low"} if model in {
        "deepseek/deepseek-flash", "deepseek/deepseek-flash-latest"
    } else {}

    def complete(operation, prompt, source):
        messages = [{"role": "system", "content": prompt},
                    {"role": "user", "content": json.dumps(source, ensure_ascii=False)}]
        for max_tokens in (8192, 16384, 32768):
            try:
                response = litellm.completion(
                    model=model, messages=messages, temperature=0, max_tokens=max_tokens,
                    timeout=120, num_retries=2, **model_options,
                )
            except Exception as error:
                status = getattr(getattr(error, "response", None), "status_code", None)
                if not isinstance(status, int):
                    status = getattr(error, "status_code", None)
                # Some versions of the adapter mask a DeepSeek 402 as BadRequestError.
                if model.startswith("deepseek/") and "insufficient balance" in str(error).casefold():
                    status = 402
                label = ACCOUNT_ERRORS.get(status, f"model request failed ({type(error).__name__})")
                raise ModelCallError(operation, label, http_status=status, max_tokens=max_tokens) from None
            if not response.choices:
                raise ModelCallError(operation, "model did not return a completion", max_tokens=max_tokens)
            choice = response.choices[0]
            if choice.finish_reason != "length":
                break
        content = choice.message.content
        details = {"finish_reason": choice.finish_reason, "content_type": type(content).__name__, "max_tokens": max_tokens,
                   "completion_tokens": getattr(getattr(response, "usage", None), "completion_tokens", None)}
        if choice.finish_reason not in ("stop", None) or not isinstance(content, str) or not content.strip():
            raise ModelCallError(operation, "model did not return a complete text response", **details)
        content = content.strip()
        fence = re.fullmatch(r"```(?:json)?\s*\n(.*)\n```", content, re.S)
        try:
            result = json.loads(fence[1] if fence else content)
        except ValueError:
            raise ModelCallError(operation, "model response was not valid JSON", **details) from None
        if not isinstance(result, dict):
            raise ModelCallError(operation, "model response must be a JSON object", **details)
        return result

    return complete


def bash_executable(configured):
    if configured:
        return configured
    if os.name == "nt" and (git := shutil.which("git")):
        git_bash = Path(git).parent.parent / "bin" / "bash.exe"
        if git_bash.is_file():
            return str(git_bash)
    if bash := shutil.which("bash"):
        return bash
    raise ValueError("Bash not found; point --bash at the Bash executable")


def check_syntax(bash, script):
    env = {key: value for key, value in os.environ.items() if key not in {
        "BASH_ENV", "ENV", "SHELLOPTS", "BASHOPTS"
    } and not key.startswith("BASH_FUNC_")}
    env["LC_ALL"] = "C"
    try:
        result = subprocess.run(
            [bash, "--noprofile", "--norc", "-n"], input=script.encode("utf-8"),
            capture_output=True, env=env, timeout=15, check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise ValueError("Bash syntax check timed out") from error
    # Bash can return zero while warning about a missing heredoc delimiter.
    if result.returncode or result.stderr:
        return {"status": "failed", "diagnostic": result.stderr.decode("utf-8", errors="replace").strip()[:500]}
    return {"status": "passed"}


def recording_source(folder, selected):
    _, transcript, fields, commands, _, _ = load_recording(folder)
    for key, value in {**fields, "commands": commands}.items():
        if key in selected and selected[key] != value:
            raise ValueError(f"recording no longer matches the filtered {key}")
    return {"title": fields["title"], "description": fields["description"],
            "environment": fields["environment"], "transcript": transcript, "commands": commands}


def publish_task(folder, script, instruction):
    files = {Path("solution/solve.sh"): script.encode("utf-8"),
             Path("instruction.md"): instruction.encode("utf-8")}
    folder.parent.mkdir(parents=True, exist_ok=True)
    if not folder.exists():
        with TemporaryDirectory(dir=folder.parent) as scratch:
            staged = Path(scratch) / "task"
            (staged / "solution").mkdir(parents=True)
            for path, content in files.items():
                (staged / path).write_bytes(content)
            (staged / "solution" / "solve.sh").chmod(0o755)
            try:
                staged.rename(folder)
                return
            except OSError:
                if not folder.exists():
                    raise
    if not all((folder / path).is_file() and (folder / path).read_bytes() == content
               for path, content in files.items()):
        raise ValueError("published files differ; use a fresh output directory for this run")


def synthesize(folder, selected, destination, bash, complete, review):
    report = {"status": "review", "step": "load_recording"}
    try:
        source = recording_source(folder, selected)
        report["step"] = "extract_solution"
        script = extract_solution(source, complete, review)
        report["step"] = "check_syntax"
        report["syntax"] = check_syntax(bash, script)
        if report["syntax"]["status"] != "passed":
            return report

        report["step"] = "generate_and_audit_instruction"
        intent = {key: source[key] for key in ("title", "description", "environment")}
        instruction, audit = generate_instruction(intent, script, complete, review)
        report["audit"] = audit
        if audit["omissions"] or any(item["status"] != "met" for item in audit["checks"]):
            # Retain the exact numbered candidate for diagnosing checks on unpublished pairs.
            report["candidate"] = {"solution": script, "instruction": instruction}
            return report

        report["step"] = "publish"
        publish_task(destination, script, instruction)
        return {**report, "status": "synthesized", "step": "complete"}
    except UnsupportedWorkflow as error:
        return {**report, "message": str(error)}
    except ModelCallError as error:
        return {**report, "status": "error", "message": str(error), "model_call": error.details}
    except (OSError, ValueError) as error:
        return {**report, "status": "error", "message": str(error)}


def run(args):
    accepted = read_object(args.input)
    ids = list(dict.fromkeys(args.ids)) if args.ids else sorted(accepted)
    for recording_id in ids:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", recording_id) or recording_id.endswith("."):
            raise ValueError(f"invalid recording ID: {recording_id!r}")
        selected = accepted.get(recording_id)
        if not isinstance(selected, dict) or (
            "decision" in selected and not str(selected["decision"]).startswith("ACCEPT_")
        ):
            raise ValueError(f"recording {recording_id} is outside the accepted input")

    reviews, complete, records = {}, None, {}
    if ids:
        if not args.model and not args.reviews:
            raise ValueError("select --model or --reviews for a nonempty input")
        bash = bash_executable(args.bash)
        probe = check_syntax(bash, ":\n")
        if probe["status"] != "passed":
            raise ValueError(f"Bash is unavailable: {probe['diagnostic'] or 'process exited with an error'}")
        if args.model:
            complete = model_completion(args.model)
        else:
            reviews = read_object(args.reviews)
        account_status = None
        for recording_id in ids:
            if account_status is not None:
                records[recording_id] = {"status": "blocked", "step": "model_account",
                                         "message": ACCOUNT_ERRORS[account_status],
                                         "model_call": {"http_status": account_status}}
                continue
            review = reviews.get(recording_id)
            if not complete and not isinstance(review, dict):
                records[recording_id] = {"status": "review", "step": "load_review",
                                         "message": "no offline review for this recording"}
                continue
            record = synthesize(
                args.recordings_dir / recording_id, accepted[recording_id],
                args.output_dir / "tasks" / recording_id, bash, complete, review,
            )
            records[recording_id] = record
            status = record.get("model_call", {}).get("http_status")
            if status in ACCOUNT_ERRORS:
                account_status = status

    counts = Counter(synthesized=0, review=0, error=0)
    counts.update(record["status"] for record in records.values())
    args.output_dir.mkdir(parents=True, exist_ok=True)
    write_json(args.output_dir / "synthesis_report.json", {
        "input": str(args.input), "mode": "model" if args.model else "offline" if args.reviews else None,
        "model": args.model, "summary": {"total": len(ids), **counts}, "records": records,
    })
    print(f"total={len(ids)} " + " ".join(f"{status}={count}" for status, count in counts.items()))
    if not ids:
        print("The filtered input contains no accepted recordings.")
    return int(bool(counts["review"] or counts["error"]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=Path("output/filtered_recordings.json"))
    parser.add_argument("--recordings-dir", type=Path, default=Path("data/recordings"))
    parser.add_argument("--output-dir", type=Path, default=Path("output/synthesis"))
    parser.add_argument("--ids", nargs="+", help="limit the batch to these accepted recording IDs")
    parser.add_argument("--bash", help="path to Bash for syntax checking")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--model", help="LiteLLM model ID for extraction, instruction, and audit")
    modes.add_argument("--reviews", type=Path, help="JSON with reviewed source ranges, intent, and audit")
    args = parser.parse_args()
    try:
        return run(args)
    except ModuleNotFoundError as error:
        parser.exit(1, f"Synthesis failed: missing model dependency ({error.name}); install LiteLLM for --model.\n")
    except (OSError, ValueError) as error:
        parser.exit(1, f"Synthesis failed: {error}\n")


if __name__ == "__main__":
    raise SystemExit(main())
