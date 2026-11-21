"""Collect environment evidence without executing the reference on the host."""

import re

def analyze_task(task, recording=None):
    script = (task / "solution/solve.sh").read_text(encoding="utf-8-sig")
    instruction = (task / "instruction.md").read_text(encoding="utf-8-sig")
    if not script.strip() or not instruction.strip():
        raise ValueError("reference and instruction must be nonempty")
    evidence = {"solution": script, "instruction": instruction}
    if recording and recording.is_dir():
        for name in ("info.json", "commands.json"):
            path = recording / name
            if path.is_file():
                evidence[name] = path.read_text(encoding="utf-8-sig")
        path = recording / "recording.txt"
        if path.is_file():
            evidence["transcript"] = path.read_text(encoding="utf-8-sig")
    evidence["signals"] = {
        "python_imports": sorted(set(re.findall(r"(?:from|import)\s+([A-Za-z_]\w*)", script))),
        "node_modules": sorted(set(re.findall(r"require\(['\"]([^'\"]+)['\"]\)", script))),
        "urls": sorted(set(re.findall(r"https?://[^\s'\"<>]+", script))),
    }
    return evidence
