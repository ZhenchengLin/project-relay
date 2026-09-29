"""
Shell blocks in ChatGPT replies, and the commands Relay refuses to run unattended.
"""
from __future__ import annotations

import re
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


def incomplete_reason(script: str) -> str | None:
    """
    Why a script looks cut off or unparsable, or None.

    A truncated reply still starts like a valid script, and bash happily runs
    an unterminated heredoc to end-of-file (`bash -n` accepts it), so heredoc
    closure is checked explicitly before `bash -n`.
    """
    delim = unterminated_heredoc(script)
    if delim:
        return f"heredoc '{delim}' is never closed (the script looks cut off)"
    try:
        result = subprocess.run([bash_path(), "-n"], input=script, text=True,
                                capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"bash -n could not run: {exc}"
    if result.returncode != 0:
        return "bash -n: " + (result.stderr.strip().splitlines() or ["syntax error"])[-1][:300]
    return None
