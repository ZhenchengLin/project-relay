"""
Shell blocks in ChatGPT replies, and the commands Relay refuses to run unattended.
"""
from __future__ import annotations

import re
import shutil
import subprocess

from .runner import bash_path

FENCE = "`" * 3
SHELL_LANGS = {"", "bash", "sh", "shell", "zsh", "console"}

_BLOCK_RE = re.compile(re.escape(FENCE) + r"([A-Za-z0-9_+-]*)[^\n]*\n(.*?)" + re.escape(FENCE), re.DOTALL)

_SHELL_HINTS = (
    r"(?m)^\s*(?:bash|sh|zsh)\b",
    r"(?m)^\s*set\s+-",
    r"(?m)^\s*cd\s+",
    r"(?m)^\s*git\s+",
    r"(?m)^\s*echo\b",
    r"(?m)^\s*(?:python|python3)\b",
    r"(?m)^\s*[A-Z_][A-Z0-9_]*=",
    r"(?m)^\s*\(",
)


def _looks_like_shell(body: str) -> bool:
    if body.lstrip().startswith("#!"):
        return True
    return any(re.search(pattern, body) for pattern in _SHELL_HINTS)


def extract_shell_blocks(text: str) -> list[str]:
    """Bodies of fenced shell blocks. Unlabeled blocks count only if they look like shell."""
    blocks = []
    for match in _BLOCK_RE.finditer(text or ""):
        language = match.group(1).strip().lower()
        body = match.group(2).strip()
        if not body or language not in SHELL_LANGS:
            continue
        if not language and not _looks_like_shell(body):
            continue
        blocks.append(body)
    return blocks


# `git` followed by any global options (-C <dir>, -c k=v, --git-dir=..., -P, …)
# before the subcommand, so `git -C repo reset --hard` is still caught.
GIT = (r"\bgit(?:\s+(?:-[cC]\s+\S+|--(?:git-dir|work-tree|namespace|exec-path)(?:=|\s+)\S+"
       r"|--[\w-]+(?:=\S+)?|-[a-zA-Z]))*\s+")

DANGEROUS_PATTERNS = (
    (re.compile(r"(?im)^\s*sudo\b"), "sudo command"),
    (re.compile(r"(?im)" + GIT + r"reset\s+--hard\b"), "git reset --hard"),
    (re.compile(r"(?im)" + GIT + r"clean\s+-[^\n]*[fdx]"), "destructive git clean"),
    (re.compile(r"(?im)" + GIT + r"push\b[^\n]*--force(?:-with-lease)?\b"), "forced Git push"),
    (re.compile(r"(?im)" + GIT + r"push\b[^\n]*\s\+\S"), "forced Git push (+refspec)"),
    (re.compile(r"(?im)" + GIT + r"stash\b(?!\s+(?:list|show)\b)"), "git stash (hides uncommitted work)"),
    (re.compile(r"(?im)" + GIT + r"checkout\b[^\n]*(?:\s-f\b|\s--force\b|\s--\s|\s\.(?:\s|;|&|$))"),
     "git checkout that discards local changes"),
    (re.compile(r"(?im)" + GIT + r"restore\b(?![^\n]*--staged)"), "git restore (discards local changes)"),
    (re.compile(r"(?im)" + GIT + r"switch\b[^\n]*(?:\s-f\b|\s--force\b|\s--discard-changes\b)"),
     "git switch that discards local changes"),
    # Case-sensitive: -d (merged-only delete) is fine, -D is forced.
    (re.compile(r"(?m)" + GIT + r"branch\b[^\n]*(?:\s-D\b|--delete\s+--force\b|--force\s+--delete\b)"),
     "forced branch deletion"),
    (re.compile(r"(?im)\bgh\s+repo\s+delete\b"), "GitHub repository deletion"),
    (re.compile(r"(?im)\bmkfs(?:\.\w+)?\b"), "filesystem formatting"),
    (re.compile(r"(?im)\bdiskutil\s+(?:erase|partition|apfs\s+delete)"), "disk destructive operation"),
    (re.compile(r"(?im)\b(?:shutdown|reboot)\b"), "system shutdown/reboot"),
    (re.compile(r"(?im)\brm\s+-[^\n]*r[^\n]*f[^\n]*\s+/(?:\s|$)"), "recursive deletion of root"),
    (re.compile(r"(?im)\brm\s+-[^\n]*r[^\n]*\s+(?:~|\$HOME|\$\{HOME\})/?(?:\s|;|&|$)"),
     "recursive deletion of the home directory"),
)


def dangerous_reason(script: str) -> str | None:
    """Why Relay must not run this script unattended, or None."""
    for pattern, label in DANGEROUS_PATTERNS:
        if pattern.search(script):
            return label
    return None


# `cmd <<DELIM`, `<<-DELIM`, `<<'DELIM'`, `<<"DELIM"`; not here-strings (<<<).
_HEREDOC_RE = re.compile(r"(?<!<)<<(?!<)(-?)\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\2")


def unterminated_heredoc(script: str) -> str | None:
    """Name of the first heredoc whose closing delimiter line never appears."""
    pending: list[tuple[str, bool]] = []
    for line in script.splitlines():
        if pending:
            delim, strip_tabs = pending[0]
            if (line.lstrip("\t") if strip_tabs else line) == delim:
                pending.pop(0)
            continue
        pending.extend((m.group(3), m.group(1) == "-") for m in _HEREDOC_RE.finditer(line))
    return pending[0][0] if pending else None


# ------------------------------------------------------------ lint (Layer 1)
#
# Deterministic checks that prove a script is broken before it runs. Each
# problem names the line, so the model can fix it in one reply. Only things
# that are certainly wrong block; style issues never do.

_PYTHON_HEREDOC = re.compile(r"\bpython(?:3(?:\.\d+)?)?\b[^\n<]*<<(-?)\s*(['\"])([A-Za-z_][A-Za-z0-9_]*)\2")
_INVISIBLE = {"\u00a0": "non-breaking space", "\u200b": "zero-width space", "\u200c": "zero-width non-joiner",
              "\u200d": "zero-width joiner", "\u2060": "word joiner", "\ufeff": "byte-order mark"}
_ELISION = re.compile(r"#\s*(?:\.\.\.|…)\s*(?:\(?\s*)(?:rest|remaining|same|unchanged|existing|other|omitted|etc)\b",
                      re.IGNORECASE)
_PLACEHOLDER = re.compile(
    r"<\s*(?:your|insert|replace|enter|path|project|repo|user|file|directory|dir)[-_ a-z]*>"
    r"|/path/to/|\bYOUR_[A-Z_]+\b|\bREPLACE_ME\b",
    re.IGNORECASE)
_BASH_LINE = re.compile(r"line (\d+):")


def _snippet(line: str) -> str:
    line = line.strip()
    return line if len(line) <= 90 else line[:87] + "..."


def _python_heredoc_problems(lines: list[str]) -> list[str]:
    problems = []
    i = 0
    while i < len(lines):
        match = _PYTHON_HEREDOC.search(lines[i])
        if not match:
            i += 1
            continue
        strip_tabs, delim = match.group(1) == "-", match.group(3)
        body, j = [], i + 1
        while j < len(lines) and (lines[j].lstrip("\t") if strip_tabs else lines[j]) != delim:
            body.append(lines[j].lstrip("\t") if strip_tabs else lines[j])
            j += 1
        if j < len(lines):  # an unclosed heredoc is reported separately
            try:
                compile("\n".join(body), "<heredoc>", "exec")
            except SyntaxError as exc:
                at = i + 1 + (exc.lineno or 1)
                problems.append(f"line {at}: Python syntax error in the {delim} block: {exc.msg}: "
                                f"`{_snippet(lines[at - 1] if 0 < at <= len(lines) else '')}`")
        i = j + 1
    return problems


def _shellcheck_problems(script: str) -> list[str]:
    tool = shutil.which("shellcheck")
    if not tool:
        return []
    try:
        result = subprocess.run([tool, "-s", "bash", "-S", "error", "-f", "gcc", "-"], input=script,
                                text=True, capture_output=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return []
    problems = []
    for row in result.stdout.splitlines():
        parts = row.split(":", 4)  # -:LINE:COL: error: message [SCxxxx]
        if len(parts) == 5 and parts[3].strip() == "error":
            problems.append(f"line {parts[1]}: shellcheck: {parts[4].strip()}")
    return problems


def script_problems(script: str) -> list[str]:
    """Reasons this script is certainly broken (empty = fine to run)."""
    lines = script.split("\n")
    delim = unterminated_heredoc(script)
    if delim:
        return [f"heredoc '{delim}' is never closed (the script looks cut off)"]

    problems: list[str] = []
    if "\r" in script:
        problems.append("the script has Windows line endings (\\r); send it with plain \\n line endings")
    for number, line in enumerate(lines, 1):
        stripped = line.strip()
        if stripped.startswith(FENCE):
            problems.append(f"line {number}: a Markdown code fence is inside the script: `{_snippet(line)}`")
        for char, name in _INVISIBLE.items():
            if char in line:
                problems.append(f"line {number}: contains a {name} (U+{ord(char):04X}); retype this line: "
                                f"`{_snippet(line.replace(char, '·'))}`")
                break
        if _ELISION.search(line) or stripped in {"...", "…"} and not _inside_python(lines, number):
            problems.append(f"line {number}: part of the script is left out: `{_snippet(line)}`")
        if _PLACEHOLDER.search(line):
            problems.append(f"line {number}: contains a placeholder to fill in: `{_snippet(line)}`")
    if problems:
        return problems

    try:
        result = subprocess.run([bash_path(), "-n"], input=script, text=True, capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return [f"bash -n could not run: {exc}"]
    if result.returncode != 0:
        for message in result.stderr.strip().splitlines()[-2:]:
            found = _BASH_LINE.search(message)
            where = int(found.group(1)) if found else None
            text = message.split(": ", 2)[-1] if ": " in message else message
            line = f": `{_snippet(lines[where - 1])}`" if where and 0 < where <= len(lines) else ""
            problems.append(f"line {where}: bash syntax error: {text}{line}" if where else f"bash -n: {text}")
        return problems

    problems += _python_heredoc_problems(lines)
    problems += _shellcheck_problems(script)
    return problems


def _inside_python(lines: list[str], number: int) -> bool:
    """True if line `number` (1-based) sits inside a python heredoc body (where `...` is valid code)."""
    open_delim = None
    strip_tabs = False
    for i, line in enumerate(lines[: number - 1]):
        if open_delim is None:
            match = _PYTHON_HEREDOC.search(line)
            if match:
                open_delim, strip_tabs = match.group(3), match.group(1) == "-"
        elif (line.lstrip("\t") if strip_tabs else line) == open_delim:
            open_delim = None
    return open_delim is not None


def incomplete_reason(script: str) -> str | None:
    """Backwards-compatible single-string form of script_problems()."""
    problems = script_problems(script)
    return "; ".join(problems) if problems else None
