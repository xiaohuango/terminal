"""Import additional published tasks as reference-script transcripts."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import re
import shlex
import subprocess
from urllib.parse import urljoin

from .filter_recordings import write_json


SITE = "https://terminalworld.ai"
CATEGORIES = {
    "Scripting & Automation", "Software Build & Test", "Data Analysis", "Version Control",
    "Debugging & Testing", "File & Storage", "Media Processing", "Environment Setup",
    "Database Operations", "ML Training & Experiments", "Performance Optimization",
    "Containers & Orchestration",
}
OFFENSIVE_CONTENT = re.compile(
    r"\b(?:msfvenom|msfconsole|sqlmap|mimikatz|buffer overflow|shellcode|reverse shell|"
    r"sql injection|privilege escalation|remote code execution|exploit(?:s|ed|ing)?|"
    r"ransomware|malware|credential theft)\b",
    re.IGNORECASE,
)
HEREDOC = re.compile(r"""(?<!<)<<(?!<)-?\s*['"]?([A-Za-z_][A-Za-z0-9_]*)['"]?""")
STRUCTURAL_LINES = {"then", "do", "else", "fi", "done", "esac", "{", "}", ";;"}


def fetch(url, *, timeout):
    from bs4 import BeautifulSoup

    response = subprocess.run(
        ["curl", "-fsSL", "--compressed", "--connect-timeout", "12", "--max-time", str(timeout),
         "--retry", "1", url],
        check=True, capture_output=True, timeout=2 * timeout + 5,
    )
    return BeautifulSoup(response.stdout.decode("utf-8"), "html.parser")


def render_transcript(reference):
    lines = ["[Synthetic terminal input derived from the published reference script]",
             "[Command text only. Execution output and timing are unavailable.]", ""]
    delimiters, pending = [], []
    for line in reference.splitlines():
        code = line.strip()
        if delimiters:
            lines.append("> " + line)
            if code == delimiters[0]:
                delimiters.pop(0)
            continue
        if not pending and (not code or code.startswith("#")):
            lines.append("  " + line if code else "")
            continue
        prompt = "> " if pending or code in STRUCTURAL_LINES else "$ "
        lines.append(prompt + (line if pending else code))
        pending.append(line)
        if (len(line) - len(line.rstrip("\\"))) % 2:
            continue
        command = "\n".join(pending)
        try:
            shlex.split(command, comments=True)
        except ValueError:
            continue
        delimiters.extend(HEREDOC.findall(command))
        pending.clear()
    return "\n".join(lines).rstrip() + "\n"


def candidates(listing, existing):
    rows = {}
    for card in listing.select(".task-card[data-href]"):
        route = card["data-href"]
        if not re.fullmatch(r"/tasks/[0-9]+", route) or card["data-cat"] not in CATEGORIES:
            continue
        identifier = route.rsplit("/", 1)[-1]
        tools = [item.get_text(strip=True) for item in card.select("span.mono")]
        if identifier not in existing and len(set(tools)) >= 2:
            rows.setdefault(identifier, (identifier, card, tools))
    return sorted(rows.values(), key=lambda row: (row[1].get("data-subset") != "verified", int(row[0])))


def load_task(candidate):
    identifier, card, tools = candidate
    url = f"{SITE}/tasks/{identifier}"
    try:
        detail = fetch(url, timeout=20)
        headings = {h.get_text(strip=True): h for h in detail.select("h2")}
        instruction = headings["Instruction"].find_next_sibling().get_text().strip()
        solution = headings["Ground Truth Solution"].parent.select_one("button[data-code]")["data-code"].strip()
        if detail.h1.get_text(strip=True) != "#" + identifier or not instruction or not solution:
            return None
    except (subprocess.SubprocessError, KeyError, TypeError, AttributeError, UnicodeError):
        return None
    if OFFENSIVE_CONTENT.search(instruction) or OFFENSIVE_CONTENT.search(solution):
        return None
    recording = card.select_one("a.recording-link")
    row = {
        "task_id": "tw_" + identifier, "instruction": instruction, "terminal_domain": card["data-cat"],
        "difficulty": card["data-diff"], "subset": "verified" if card.get("data-subset") == "verified" else "full",
        "task_url": url, "source_url": urljoin(SITE, recording["href"]) if recording else None,
        "tools": tools, "license": "CC-BY-4.0",
    }
    return identifier, row, render_transcript(solution)


def run(args):
    if args.manifest.exists():
        raise ValueError(f"manifest already exists: {args.manifest}")
    existing = {path.name for path in args.recordings_dir.iterdir() if path.is_dir()}
    pool_inputs = candidates(fetch(SITE + "/tasks/", timeout=70), existing)
    ready = []
    batch = args.workers * 2
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for start in range(0, len(pool_inputs), batch):
            ready.extend(result for result in pool.map(load_task, pool_inputs[start:start + batch]) if result)
            print(f"Fetched {min(len(ready), args.count)}/{args.count} official task records", flush=True)
            if len(ready) >= args.count:
                break
    if len(ready) < args.count:
        raise ValueError(f"only {len(ready)} of {args.count} requested records were available")

    selected = ready[:args.count]
    for identifier, row, transcript in selected:
        folder = args.recordings_dir / identifier
        folder.mkdir()
        info = {
            **row, "id": identifier, "title": f"TerminalWorld {row['terminal_domain']} task {row['task_id']}",
            "description": row["instruction"], "href": row["source_url"] or row["task_url"],
            "recording_kind": "synthetic_reference_transcript", "reference_status": "ok", "duration_seconds": None,
        }
        write_json(folder / "info.json", info)
        (folder / "recording.txt").write_text(transcript, encoding="utf-8")
    with args.manifest.open("x", encoding="utf-8", newline="\n") as output:
        for _, row, _ in selected:
            output.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"Added {len(selected)} recordings. Total: {len(existing) + len(selected)}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=100)
    parser.add_argument("--recordings-dir", type=Path, default=Path("data/recordings"))
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=6)
    args = parser.parse_args()
    if args.count < 1 or args.workers < 1:
        parser.error("--count and --workers must be positive")
    try:
        run(args)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        parser.exit(1, f"Task import failed: {error}\n")


if __name__ == "__main__":
    main()
