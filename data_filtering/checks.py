"""Static checks for local recordings. The transcript is always treated as data."""

from http.client import HTTPException, HTTPSConnection
import math
import re
import shlex
from urllib.parse import urljoin, urlsplit


ANSI = re.compile(r"\x1b(?:\][^\x07\x1b]*(?:\x07|\x1b\\)|\[[0-?]*[ -/]*[@-~]|[@-_])")
PROMPT = re.compile(
    r"^(?:[$#%❯] |(?:\([^)]*\) )?(?:[\w.-]+@[\w.-]+[^\s]*|[~/][^\s]*)[$#%] |"
    r"(?:PS )?[A-Za-z]:\\[^>]*>\s*)"
)
WINDOWS_PROMPT = re.compile(r"^(?:PS )?[A-Za-z]:\\[^>]*>", re.MULTILINE)
URL = re.compile(r"""https?://[^\s<>"'`|]+""", re.I)
REPO = re.compile(
    r"https?://(github\.com|gitlab\.com|bitbucket\.org|codeberg\.org)/"
    r"([A-Za-z0-9_-][A-Za-z0-9_.-]*/[A-Za-z0-9_-][A-Za-z0-9_.-]*)/?",
    re.I,
)
PUBLIC_DEPENDENCY_HOSTS = {
    "github.com", "raw.githubusercontent.com", "codeload.github.com",
    "objects.githubusercontent.com", "release-assets.githubusercontent.com",
    "gitlab.com", "bitbucket.org", "codeberg.org",
    "cdn.kernel.org", "www.kernel.org", "files.pythonhosted.org",
    "www.gutenberg.org", "gutenberg.org", "dl.suckless.org",
}
PRIVACY = {
    "pii": re.compile(
        r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b|"
        r"/(?:home|Users)/[\w.-]+/|\b[A-Z]:\\Users\\[\w.-]+\\|"
        r"\b\d{3}-\d{2}-\d{4}\b",
        re.I,
    ),
    "credential": re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----|"
        r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b|\b(?:gh[pousr]_|github_pat_|sk-(?:proj-)?)[\w-]{12,}|"
        r"(?i:(?:\b|(?<=_))(?:password|passwd|api[_-]?key|(?:access[_-]?)?token|"
        r"secret(?:[_-]?(?:access[_-]?)?key)?)['\"]?)"
        r"\s*[:=]\s*['\"]?(?!\$|<)[^\s'\"<>]{3,}|"
        r"(?i:https?://)[^/\s:@]+:[^/\s@]+@|"
        r"(?i:\b(?:bearer|basic))\s+[A-Za-z0-9._+/-]{12,}"
    ),
}
TUI = set("vi vim nvim nano emacs micro less more htop btop top tmux screen ranger mc dialog whiptail".split())
GUI = set("firefox chromium chromium-browser google-chrome xdg-open gedit gimp xterm".split())
NON_LINUX = set("cmd cmd.exe powershell powershell.exe xcodebuild brew launchctl".split())
PROPRIETARY = {"matlab", "stata", "sas", "mathematica", "wolframscript"}
OFFENSIVE = set("msfconsole msfvenom sqlmap hydra responder bettercap mimikatz".split())
TRIVIAL = set("cd ls cat pwd echo clear history exit man which type command".split())
WRAPPERS = {"env", "sudo", "command", "time", "nohup", "exec"}
SEPARATORS = {";", "&&", "||", "|", "|&", "&", "(", ")"}


def duration_seconds(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        value = value.strip()
        if re.fullmatch(r"\d+(?::[0-5]?\d){1,2}(?:\.\d+)?", value):
            value = sum(float(part) * 60**index for index, part in enumerate(reversed(value.split(":"))))
        else:
            clock = re.fullmatch(r"(?:(\d+)h\s*)?(?:(\d+)m\s*)?(?:(\d+(?:\.\d+)?)s)?", value)
            if clock and any(clock.groups()):
                hours, minutes, seconds = (float(part or 0) for part in clock.groups())
                value = hours * 3600 + minutes * 60 + seconds
    try:
        seconds = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return seconds if math.isfinite(seconds) and seconds >= 0 else None


def extract_commands(transcript):
    commands = []
    for line in transcript.splitlines():
        if commands and line.startswith("> "):
            previous = commands[-1]
            trailing_backslashes = len(previous) - len(previous.rstrip("\\"))
            if trailing_backslashes % 2:
                commands[-1] = previous[:-1] + line[2:].lstrip()
                continue
        match = PROMPT.match(line)
        if match:
            command = line[match.end():].strip()
            if command:
                commands.append(command)
    return commands


def command_segments(command):
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()")
        lexer.whitespace_split = True
        lexer.commenters = ""
        words = list(lexer)
    except ValueError:
        words = command.split()
    segment = []
    for word in words:
        if word in SEPARATORS:
            if segment:
                yield segment
            segment = []
        else:
            segment.append(word)
    if segment:
        yield segment


def command_name(segment):
    index = 0
    while index < len(segment):
        word = segment[index]
        name = word.rsplit("/", 1)[-1].lower()
        index += 1
        if name == "sudo":
            while index < len(segment) and segment[index].startswith("-"):
                index += 2 if segment[index] in {"-u", "-g", "-h", "-p", "-C", "-R", "-D", "--user", "--group", "--host"} else 1
        elif name in WRAPPERS or re.match(r"^[a-zA-Z_]\w*=", word) or word.startswith("-"):
            continue
        else:
            return name, segment[index:]
    return "", []


def extract_urls(text):
    return {url.rstrip(").,]};&") for url in URL.findall(text)}


def is_loopback(url):
    try:
        return urlsplit(url).hostname in {"localhost", "127.0.0.1", "::1"}
    except ValueError:
        return False


def check_recording(raw, transcript, fields, commands, seconds, min_duration, min_commands):
    """Return fixed reason codes, observations and inferred dependency URLs."""
    text = "\n".join((transcript, *fields.values(), *commands))
    reasons = [reason for reason, pattern in PRIVACY.items()
               if pattern.search(text) or (reason == "credential" and pattern.search(raw))]
    reviews = []
    dependency_urls = set()
    if not transcript.strip():
        return ["empty_transcript"], [], dependency_urls
    if re.search(r"\x1b\[\?(?:47|1047|1049)h", raw):
        reasons.append("tui")
    if WINDOWS_PROMPT.search(transcript) or (
        re.search(r"\bwindows\b", fields["environment"], re.I)
        and not re.search(r"\b(?:wsl2?|linux)\b", fields["environment"], re.I)
    ):
        reasons.append("non_linux")

    substantive = False
    # Inspect transcript commands even when a sidecar supplies the commands used for counting.
    for command in dict.fromkeys([*commands, *extract_commands(transcript)]):
        for segment in command_segments(command):
            name, args = command_name(segment)
            batch_mode = (name == "top" and "-b" in args) or (name == "emacs" and "--batch" in args)
            if name in TUI and not batch_mode and not set(args) & {"--version", "--help", "-V"}:
                reasons.append("tui")
            if name in GUI:
                reasons.append("gui")
            if name in NON_LINUX:
                reasons.append("non_linux")
            if name in PROPRIETARY:
                reasons.append("proprietary_dependency")
            if name in OFFENSIVE:
                reasons.append("offensive_workflow")
            if (name == "rm" and any(arg in {"/", "/*", "/**"} for arg in args)) or name in {
                "wipefs", "mkfs", "shred"
            }:
                reasons.append("destructive_command")
            if name in {"curl", "wget"} or (name == "git" and "clone" in args) or (
                name in {"pip", "pip3", "uv"} and "install" in args
            ):
                urls = extract_urls(" ".join(args))
                dependency_urls.update(url for url in urls if not is_loopback(url))
                if name == "git" and not urls and any(
                    re.match(r"(?:ssh|git)://|[^\s/@]+@[^\s:]+:", arg) for arg in args
                ):
                    reviews.append("dependency_unverified")
            if name and (name not in TRIVIAL or any(arg.startswith(">") for arg in args)):
                substantive = True

    if seconds is not None and seconds < min_duration:
        reasons.append("too_short")
    if not commands:
        reviews.append("commands_unrecognized")
    elif len(commands) < min_commands:
        reasons.append("too_few_commands")
    elif not substantive:
        reasons.append("exploratory_only")
    return sorted(set(reasons)), sorted(set(reviews)), dependency_urls


def repository_available(url):
    """Verify a public repository page without fetching artifacts or following redirects."""
    match = REPO.fullmatch(url)
    if match is None:
        return None
    host, repo_path = match.groups()
    connection = HTTPSConnection(host.lower(), timeout=5)
    try:
        connection.request("HEAD", "/" + repo_path.removesuffix(".git"),
                           headers={"User-Agent": "TerminalWorld-Reproduction/1.0"})
        with connection.getresponse() as response:
            if 200 <= response.status < 300:
                return True
            if response.status in {401, 404, 410}:
                return False
    except (OSError, HTTPException):
        return None
    finally:
        connection.close()
    return None


def check_dependencies(urls, required, index, check_repository):
    reasons = set()
    context = 0
    for url in sorted(urls | required):
        entry = index.get(url, {})
        status = entry.get("accessible")
        if status is None and check_repository:
            status = check_repository(url)
        if status is True:
            is_repo = entry.get("is_repo", bool(REPO.fullmatch(url)))
            context = max(context, 3 if is_repo else 2)
        elif status is False:
            context = max(context, 1)
            if url in required:
                reasons.add("dependency_inaccessible")
        elif url in required:
            reasons.add("dependency_unverified")
    return sorted(reasons), context


def public_link_available(url):
    """Check public project and artifact hosts without downloading dependencies."""
    repo = REPO.fullmatch(url)
    if repo:
        host, path = repo.groups()
        url = f"https://{host}/{path.removesuffix('.git')}"
    for _ in range(4):
        try:
            target = urlsplit(url)
            if (target.scheme not in {"http", "https"} or target.hostname not in PUBLIC_DEPENDENCY_HOSTS
                    or target.username is not None or target.port is not None or target.query):
                return None
            connection = HTTPSConnection(target.hostname, timeout=6)
            try:
                connection.request("HEAD", target.path or "/", headers={"User-Agent": "TerminalWorld-Reproduction/1.0"})
                with connection.getresponse() as response:
                    status = response.status
                    location = response.getheader("Location")
            finally:
                connection.close()
            if 200 <= status < 300:
                return True
            if status in {401, 404, 410}:
                return False
            if status not in {301, 302, 303, 307, 308} or not location:
                return None
            url = urljoin(target._replace(scheme="https").geturl(), location)
            if urlsplit(url).scheme != "https":
                return None
        except (OSError, HTTPException, ValueError):
            return None
    return None
