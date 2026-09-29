from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

Verdict = Literal["PROGRESS", "LOOP", "UNCERTAIN"]

APP_DIR = Path(os.environ.get("PROJECT_RELAY_HOME") or Path.home() / ".project-relay").expanduser()
CONFIG_FILE = APP_DIR / "config.json"
STATE_DIR = APP_DIR / "projects"


class RelayError(RuntimeError):
    pass


@dataclass(frozen=True)
class Project:
    name: str
    root: Path
    preferred_branch: str = ""


@dataclass
class Vote:
    voter: str
    verdict: Verdict
    reason: str
    model: str | None = None


@dataclass
class Decision:
    status: Literal["CONTINUE", "HUMAN_REQUIRED", "UNCERTAIN"]
    votes: list[Vote]
    reason: str


def default_config() -> dict[str, Any]:
    return {
        "active_project": "",
        "projects": {},
        "watchdog": {
            "enabled": True,
            "history_window": 4,
            "command_similarity": 0.92,
            "repeat_threshold": 3,
            "ollama_url": "http://127.0.0.1:11434",
            "voters": [
                {
                    "name": "local_llm",
                    "model": "qwen3.5:4b",
                    "enabled": True,
                    "role": "progress_judge",
                },
                {
                    "name": "logic_model",
                    "model": "qwen3:1.7b",
                    "enabled": True,
                    "role": "logic_judge",
                },
            ],
        },
        "prompt_template": (
            "I just ran the previous CLI step for {project}. "
            "Continue from this exact terminal result. "
            "Use the existing conversation plan and history; do not restart from scratch. "
            "Do not invent success.\n\n"
            "Project: {project}\n"
            "Repository: {root}\n"
            "Branch: {branch}\n"
            "HEAD before command: {head_before}\n"
            "HEAD after command: {head_after}\n"
            "Working directory: {cwd}\n"
            "Loop watchdog: {watchdog_status}\n\n"
            "Git status after command:\n```text\n{git_status_after}\n```\n\n"
            "Terminal output:\n```text\n{terminal_output}\n```\n\n"
            "Continue from this evidence. If another CLI action is appropriate, "
            "give me one pasteable shell block."
        ),
    }


def load_config() -> dict[str, Any]:
    base = default_config()
    if not CONFIG_FILE.exists():
        return base
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RelayError(f"Cannot read {CONFIG_FILE}: {exc}") from exc
    merged = base
    merged.update(data)
    merged.setdefault("projects", {})
    merged.setdefault("watchdog", base["watchdog"])
    merged["watchdog"].setdefault("voters", base["watchdog"]["voters"])
    return merged


def list_ollama_models(
    ollama_url: str,
    timeout_seconds: int = 10,
) -> list[str]:
    url = (
        ollama_url.rstrip("/")
        + "/api/tags"
    )

    req = urllib.request.Request(
        url,
        method="GET",
    )

    try:
        with urllib.request.urlopen(
            req,
            timeout=timeout_seconds,
        ) as resp:
            data = json.loads(
                resp.read().decode("utf-8")
            )
    except (
        urllib.error.URLError,
        TimeoutError,
        json.JSONDecodeError,
    ) as exc:
        raise RelayError(
            "Cannot query Ollama model inventory at "
            f"{ollama_url}: "
            f"{type(exc).__name__}: {exc}"
        ) from exc

    raw_models = data.get(
        "models",
        [],
    )

    if not isinstance(
        raw_models,
        list,
    ):
        raise RelayError(
            "Ollama /api/tags returned an "
            "unexpected models payload."
        )

    names = sorted({
        str(
            item.get(
                "name",
                "",
            )
        ).strip()
        for item in raw_models
        if isinstance(
            item,
            dict,
        )
        and str(
            item.get(
                "name",
                "",
            )
        ).strip()
    })

    return names


def save_config(config: dict[str, Any]) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(
        json.dumps(config, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def register_project(name: str, root: str, preferred_branch: str = "") -> Project:
    path = Path(root).expanduser().resolve()
    if not path.is_dir():
        raise RelayError(f"Project root does not exist: {path}")
    config = load_config()
    config["projects"][name] = {
        "root": str(path),
        "preferred_branch": preferred_branch,
    }
    if not config.get("active_project"):
        config["active_project"] = name
    save_config(config)
    project_dir(name).mkdir(parents=True, exist_ok=True)
    return Project(name, path, preferred_branch)


def project_from_config(name: str, config: dict[str, Any] | None = None) -> Project:
    config = config or load_config()
    raw = config["projects"].get(name)
    if not raw:
        raise RelayError(f"Unknown project: {name}")
    return Project(
        name=name,
        root=Path(raw["root"]).expanduser().resolve(),
        preferred_branch=str(raw.get("preferred_branch", "")),
    )


def use_project(name: str) -> Project:
    config = load_config()
    project = project_from_config(name, config)
    config["active_project"] = name
    save_config(config)
    return project


def _is_inside(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def resolve_project(name: str | None = None, cwd: Path | None = None) -> Project:
    config = load_config()
    if name:
        return project_from_config(name, config)
    cwd = (cwd or Path.cwd()).resolve()
    matches: list[Project] = []
    for project_name in config["projects"]:
        project = project_from_config(project_name, config)
        if _is_inside(cwd, project.root):
            matches.append(project)
    if matches:
        return max(matches, key=lambda p: len(p.root.parts))
    active = config.get("active_project")
    if active:
        return project_from_config(active, config)
    raise RelayError("No project resolved. Register one with: prelay register NAME /path/to/repo")


def project_dir(name: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("_") or "project"
    return STATE_DIR / safe


def cycles_file(project: Project) -> Path:
    return project_dir(project.name) / "cycles.jsonl"


def pending_file(project: Project) -> Path:
    return project_dir(project.name) / "pending.json"


def run_git(project: Project, args: list[str]) -> str:
    try:
        cp = subprocess.run(
            ["git", "-C", str(project.root), *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except Exception:
        return ""
    return cp.stdout.rstrip("\n") if cp.returncode == 0 else ""


def git_metadata(project: Project) -> dict[str, str]:
    branch = run_git(project, ["branch", "--show-current"]) or "(not a git repo)"
    head = run_git(project, ["rev-parse", "--short=12", "HEAD"]) or "(unknown)"
    status = run_git(project, ["status", "--short", "--untracked-files=all"])
    return {
        "branch": branch,
        "head": head,
        "git_status": status or "(clean or unavailable)",
    }


def _clipboard_backend() -> tuple[list[str], list[str]]:
    if shutil.which("pbcopy") and shutil.which("pbpaste"):
        return ["pbcopy"], ["pbpaste"]
    if shutil.which("wl-copy") and shutil.which("wl-paste"):
        return ["wl-copy"], ["wl-paste", "-n"]
    if shutil.which("xclip"):
        return (["xclip", "-selection", "clipboard"], ["xclip", "-selection", "clipboard", "-o"])
    raise RelayError("No supported clipboard command found (pbcopy/pbpaste, wl-copy/wl-paste, or xclip).")


def clipboard_read() -> str:
    _, read_cmd = _clipboard_backend()
    cp = subprocess.run(read_cmd, capture_output=True, check=False)
    if cp.returncode != 0:
        raise RelayError("Could not read clipboard.")
    return cp.stdout.decode("utf-8", errors="replace")


def clipboard_write(text: str) -> None:
    write_cmd, _ = _clipboard_backend()
    cp = subprocess.run(write_cmd, input=text.encode("utf-8"), capture_output=True, check=False)
    if cp.returncode != 0:
        raise RelayError("Could not write clipboard.")


SHELL_FENCE_RE = re.compile(r"```(?:bash|sh|shell|zsh)\s*\n(.*?)```", re.IGNORECASE | re.DOTALL)


def extract_shell_blocks(text: str) -> list[str]:
    return [m.strip() for m in SHELL_FENCE_RE.findall(text) if m.strip()]


def looks_like_shell(text: str) -> bool:
    stripped = text.strip()
    if not stripped or "\n" not in stripped:
        return False
    starters = ("cd ", "git ", "python ", "python3 ", "pytest ", "bash ", "set ", "echo ", "uv ", "mkdir ", "cat ", "if ", "for ")
    hits = sum(1 for line in stripped.splitlines() if line.strip().startswith(starters))
    return hits >= 2


def choose_shell_block(text: str, index: int | None = None) -> str:
    blocks = extract_shell_blocks(text)
    if not blocks and looks_like_shell(text):
        blocks = [text.strip()]
    if not blocks:
        raise RelayError("No bash/sh/shell/zsh fenced block found and clipboard does not look like a shell script.")
    if index is not None:
        if index < 1 or index > len(blocks):
            raise RelayError(f"Shell block index {index} is out of range; found {len(blocks)} block(s).")
        return blocks[index - 1]
    if len(blocks) != 1:
        raise RelayError(f"Found {len(blocks)} shell blocks. Refusing to guess. Re-run with --index N.")
    return blocks[0]


def command_warnings(command: str) -> list[str]:
    warnings: list[str] = []
    if re.search(r"(^|\n)\s*set\s+-[^\n]*u", command):
        warnings.append("Command enables nounset (-u). If pasted into an interactive zsh, shell prompt hooks may break. Prefer an isolated bash heredoc for long strict-mode blocks.")
    if re.search(r"\bgit\s+(reset\s+--hard|clean\s+-[a-zA-Z]*f|push\s+--force|push\s+-f)\b", command):
        warnings.append("Potentially destructive Git command detected. Review carefully before execution.")
    return warnings


def save_pending_command(project: Project, command: str, source_text: str = "") -> dict[str, Any]:
    meta = git_metadata(project)
    pdir = project_dir(project.name)
    pdir.mkdir(parents=True, exist_ok=True)
    record = {
        "project": project.name,
        "created_at": time.time(),
        "cwd": os.getcwd(),
        "command": command,
        "command_sha256": hashlib.sha256(command.encode()).hexdigest(),
        "source_sha256": hashlib.sha256(source_text.encode()).hexdigest() if source_text else None,
        "git_before": meta,
        "warnings": command_warnings(command),
    }
    pending_file(project).write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return record


def load_pending_command(project: Project) -> dict[str, Any] | None:
    path = pending_file(project)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RelayError(f"Cannot read pending command state: {exc}") from exc


def clear_pending(project: Project) -> None:
    pending_file(project).unlink(missing_ok=True)


def append_cycle(project: Project, cycle: dict[str, Any]) -> None:
    path = cycles_file(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(cycle, ensure_ascii=False) + "\n")


def load_cycles(project: Project, limit: int | None = None) -> list[dict[str, Any]]:
    path = cycles_file(project)
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows[-limit:] if limit else rows


def normalize_command(command: str) -> str:
    lines = []
    for line in command.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        stripped = re.sub(r"\s+", " ", stripped)
        stripped = re.sub(r"\b[0-9a-f]{8,40}\b", "<HASH>", stripped, flags=re.IGNORECASE)
        lines.append(stripped)
    return "\n".join(lines)


def command_similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, normalize_command(a), normalize_command(b)).ratio()


_ERROR_PATTERNS = (
    r"(?i)\b(error|exception|traceback|failed|failure|not found|no such file|stop:)\b.*",
    r"(?i)^E\s+.*",
    r"(?i)assertionerror.*",
)


def error_signature(output: str) -> str:
    matches: list[str] = []
    for raw in output.splitlines():
        line = re.sub(r"\x1b\[[0-9;]*m", "", raw).strip()
        if not line:
            continue
        for pat in _ERROR_PATTERNS:
            m = re.search(pat, line)
            if m:
                text = m.group(0).lower()
                text = re.sub(r"/Users/[^/\s]+", "/Users/<USER>", text)
                text = re.sub(r"\b\d+(?:\.\d+)?\b", "<N>", text)
                text = re.sub(r"\s+", " ", text)
                matches.append(text[:240])
                break
    return " | ".join(matches[:5])


def _normalized_error_signature(output: str) -> str:
    signature = error_signature(output)

    if not signature:
        return ""

    text = signature.lower()

    text = re.sub(
        r"\b0x[0-9a-f]+\b",
        "<hex>",
        text,
    )

    text = re.sub(
        r"(?:(?:\.\.?/)|/)?(?:[\w.-]+/)+[\w.-]+",
        "<path>",
        text,
    )

    text = re.sub(
        r"\b\d+(?:\.\d+)?\b",
        "<n>",
        text,
    )

    return " ".join(text.split())

def deterministic_vote(
    cycles: list[dict[str, Any]],
    cfg: dict[str, Any],
) -> Vote:
    if not cycles:
        return Vote(
            "deterministic",
            "UNCERTAIN",
            "No cycle history yet.",
        )

    last = cycles[-1]

    before = (
        last.get("git_before", {})
        .get("head")
    )

    after = (
        last.get("git_after", {})
        .get("head")
    )

    if before and after and before != after:
        return Vote(
            "deterministic",
            "PROGRESS",
            "Git HEAD changed during the latest cycle.",
        )

    latest_output = last.get(
        "terminal_output",
        "",
    )

    if not error_signature(latest_output):
        lowered = latest_output.lower()

        progress_tokens = (
            "passed",
            "committed",
            "pushed",
            "verified",
            "created:",
            "pass:",
            "success",
        )

        if any(
            token in lowered
            for token in progress_tokens
        ):
            return Vote(
                "deterministic",
                "PROGRESS",
                (
                    "Latest output contains a "
                    "success/progress signal and "
                    "no error signature."
                ),
            )

    if len(cycles) < 2:
        return Vote(
            "deterministic",
            "UNCERTAIN",
            "Not enough cycle history yet.",
        )

    repeat_threshold = int(
        cfg.get(
            "repeat_threshold",
            3,
        )
    )

    sim_threshold = float(
        cfg.get(
            "command_similarity",
            0.92,
        )
    )

    recent = cycles[
        -max(
            repeat_threshold,
            3,
        ):
    ]

    heads = [
        cycle.get(
            "git_after",
            {},
        ).get("head")
        for cycle in recent
    ]

    commands = [
        cycle.get(
            "command",
            "",
        )
        for cycle in recent
    ]

    errors = [
        _normalized_error_signature(
            cycle.get(
                "terminal_output",
                "",
            )
        )
        for cycle in recent
    ]

    if len(recent) >= repeat_threshold:

        recent_commands = commands[
            -repeat_threshold:
        ]

        recent_errors = errors[
            -repeat_threshold:
        ]

        recent_heads = heads[
            -repeat_threshold:
        ]

        similarities = [
            command_similarity(
                recent_commands[i - 1],
                recent_commands[i],
            )
            for i in range(
                1,
                len(recent_commands),
            )
        ]

        sameish_commands = (
            bool(similarities)
            and all(
                similarity >= sim_threshold
                for similarity in similarities
            )
        )

        repeated_error = (
            all(recent_errors)
            and len(
                set(recent_errors)
            ) == 1
        )

        head_static = (
            all(recent_heads)
            and len(
                set(recent_heads)
            ) == 1
        )

        if (
            repeated_error
            and head_static
            and sameish_commands
        ):
            return Vote(
                "deterministic",
                "LOOP",
                (
                    f"{repeat_threshold} near-identical "
                    "commands produced the same "
                    "normalized error with unchanged HEAD."
                ),
            )

        if (
            repeated_error
            and head_static
        ):
            return Vote(
                "deterministic",
                "LOOP",
                (
                    f"{repeat_threshold} consecutive cycles "
                    "ended in the same normalized failure "
                    "with unchanged HEAD despite command "
                    "variation."
                ),
            )

    return Vote(
        "deterministic",
        "UNCERTAIN",
        (
            "No hard repeated-loop pattern and no "
            "strong deterministic progress signal."
        ),
    )

def _compact_cycle(cycle: dict[str, Any], max_output: int = 3500) -> dict[str, Any]:
    output = cycle.get("terminal_output", "")
    if len(output) > max_output:
        output = output[:1700] + "\n...<truncated>...\n" + output[-1700:]
    return {
        "command": cycle.get("command", "")[:3500],
        "terminal_output": output,
        "head_before": cycle.get("git_before", {}).get("head"),
        "head_after": cycle.get("git_after", {}).get("head"),
        "error_signature": error_signature(cycle.get("terminal_output", "")),
    }


def ollama_vote(
    project: Project,
    cycles: list[dict[str, Any]],
    voter_cfg: dict[str, Any],
    ollama_url: str,
) -> Vote:
    model = str(
        voter_cfg.get(
            "model",
            "",
        )
    ).strip()

    voter_name = str(
        voter_cfg.get(
            "name",
            "model",
        )
    )

    role = str(
        voter_cfg.get(
            "role",
            "progress_judge",
        )
    )

    if not model:
        return Vote(
            voter_name,
            "UNCERTAIN",
            "No model configured.",
            model=None,
        )

    compact = [
        _compact_cycle(c)
        for c in cycles[-4:]
    ]

    cycle_count = len(compact)

    if role == "logic_judge":
        instruction = (
            "Judge whether the newest development cycle "
            "introduced genuinely new discriminating evidence "
            "or instead repeated a previously ineffective action. "
            "Do not propose the next project step. "
            "Do not judge architecture quality."
        )
    else:
        instruction = (
            "Judge whether this CLI development workflow is "
            "making meaningful progress or is stuck repeating "
            "the same ineffective approach. "
            "Do not propose the next project step."
        )

    rules = (
        "Voting rules:\n"
        "- PROGRESS means the newest cycle produced new verified "
        "evidence, a successful artifact, a completed bounded stage, "
        "or otherwise materially advanced the workflow.\n"
        "- LOOP means the newest cycle repeats an earlier ineffective "
        "cycle or approach without materially new evidence.\n"
        "- LOOP requires at least TWO comparable observed cycles. "
        "With fewer than two cycles, LOOP is not a valid verdict.\n"
        "- An unchanged Git HEAD does NOT by itself imply LOOP. "
        "Read-only investigations and private artifact generation "
        "can be genuine progress.\n"
        "- A successful command with explicit completion evidence "
        "should normally be PROGRESS unless an earlier comparable "
        "cycle proves that the same action was already ineffective.\n"
        "- Use UNCERTAIN only when the evidence is insufficient.\n"
    )

    prompt = (
        f"Project: {project.name}\n"
        f"Observed cycle count: {cycle_count}\n"
        f"{instruction}\n\n"
        f"{rules}\n"
        "Recent cycles JSON:\n"
        f"{json.dumps(compact, ensure_ascii=False, indent=2)}\n\n"
        "Return exactly one word on the final line: "
        "PROGRESS, LOOP, or UNCERTAIN."
    )

    payload_obj: dict[str, Any] = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {
            "temperature": 0.0,
        },
    }

    # Qwen3-family models can emit reasoning separately.
    # Request a direct final response when supported.
    if model.lower().startswith(
        "qwen3"
    ):
        payload_obj["think"] = False

    payload = json.dumps(
        payload_obj
    ).encode(
        "utf-8"
    )

    req = urllib.request.Request(
        ollama_url.rstrip("/")
        + "/api/generate",
        data=payload,
        headers={
            "Content-Type":
                "application/json",
        },
    )

    try:
        with urllib.request.urlopen(
            req,
            timeout=120,
        ) as response_stream:
            data = json.loads(
                response_stream
                .read()
                .decode(
                    "utf-8"
                )
            )

    except Exception as exc:
        return Vote(
            voter_name,
            "UNCERTAIN",
            (
                "Local model unavailable: "
                f"{type(exc).__name__}: {exc}"
            ),
            model=model,
        )

    response_text = str(
        data.get(
            "response",
            "",
        )
    ).strip()

    thinking_text = str(
        data.get(
            "thinking",
            "",
        )
    ).strip()

    parse_text = (
        response_text
        or thinking_text
    )

    verdict: Verdict = "UNCERTAIN"

    for line in reversed(
        parse_text.splitlines()
    ):
        token = (
            line
            .strip()
            .upper()
        )

        if token in {
            "PROGRESS",
            "LOOP",
            "UNCERTAIN",
        }:
            verdict = token  # type: ignore[assignment]
            break

    # A cross-cycle LOOP cannot be established
    # from only one observed development cycle.
    if (
        verdict == "LOOP"
        and cycle_count < 2
    ):
        return Vote(
            voter_name,
            "UNCERTAIN",
            (
                "Rejected LOOP verdict because only "
                f"{cycle_count} cycle is available; "
                "at least two comparable cycles are "
                "required to establish repetition."
            ),
            model=model,
        )

    if response_text:
        reason = response_text[-600:]

    elif thinking_text:
        reason = (
            "Thinking-only model output: "
            + thinking_text[-520:]
        )

    else:
        reason = (
            "No response or thinking text."
        )

    return Vote(
        voter_name,
        verdict,
        reason,
        model=model,
    )

def aggregate_votes(votes: list[Vote]) -> Decision:
    counts = {"PROGRESS": 0, "LOOP": 0, "UNCERTAIN": 0}
    for vote in votes:
        counts[vote.verdict] += 1
    if counts["LOOP"] >= 2:
        return Decision("HUMAN_REQUIRED", votes, f"Loop vote reached majority ({counts['LOOP']}/{len(votes)}).")
    if counts["PROGRESS"] >= 2:
        return Decision("CONTINUE", votes, f"Progress vote reached majority ({counts['PROGRESS']}/{len(votes)}).")
    if counts["LOOP"] == 0:
        return Decision("CONTINUE", votes, "No voter detected a loop; continuing with uncertainty noted.")
    return Decision("UNCERTAIN", votes, "No majority. One loop vote exists, so review is recommended.")


def run_watchdog(project: Project, cycles: list[dict[str, Any]] | None = None) -> Decision:
    config = load_config()
    cfg = config.get("watchdog", {})
    if not cfg.get("enabled", True):
        return Decision("CONTINUE", [Vote("watchdog", "UNCERTAIN", "Watchdog disabled.")], "Watchdog disabled.")
    window = int(cfg.get("history_window", 4))
    cycles = cycles if cycles is not None else load_cycles(project, window)
    votes: list[Vote] = [deterministic_vote(cycles, cfg)]
    ollama_url = str(cfg.get("ollama_url", "http://127.0.0.1:11434"))
    for voter_cfg in cfg.get("voters", []):
        if voter_cfg.get("enabled", True):
            votes.append(ollama_vote(project, cycles, voter_cfg, ollama_url))
    return aggregate_votes(votes)


def complete_cycle(
    project: Project,
    terminal_output: str,
    cwd: str | None = None,
    persist: bool = True,
) -> tuple[dict[str, Any], Decision]:
    terminal_output = terminal_output.rstrip()

    if not terminal_output:
        raise RelayError(
            "Terminal output is empty."
        )

    pending = load_pending_command(
        project
    )

    if not pending:
        raise RelayError(
            "No pending command exists for "
            f"{project.name}. "
            "Run 'prelay cmd' on the GPT reply "
            "before running 'prelay gpt'."
        )

    command = str(
        pending.get(
            "command",
            "",
        )
    ).strip()

    command_sha256 = pending.get(
        "command_sha256"
    )

    if not command or not command_sha256:
        raise RelayError(
            "Pending command record is incomplete. "
            "Run 'prelay cmd' again before "
            "'prelay gpt'."
        )

    meta_after = git_metadata(
        project
    )

    cycle = {
        "cycle_id": int(
            time.time() * 1000
        ),
        "completed_at": time.time(),
        "project": project.name,
        "cwd": cwd or os.getcwd(),
        "command": command,
        "command_sha256": command_sha256,
        "git_before": pending.get(
            "git_before",
            {},
        ),
        "git_after": meta_after,
        "terminal_output": terminal_output,
        "terminal_output_sha256": (
            hashlib.sha256(
                terminal_output.encode()
            ).hexdigest()
        ),
    }

    config = load_config()

    history_window = int(
        config.get(
            "watchdog",
            {},
        ).get(
            "history_window",
            4,
        )
    )

    prior_cycles = load_cycles(
        project,
        history_window,
    )

    decision = run_watchdog(
        project,
        cycles=prior_cycles + [cycle],
    )

    cycle["watchdog"] = {
        "status": decision.status,
        "reason": decision.reason,
        "votes": [
            asdict(vote)
            for vote in decision.votes
        ],
    }

    if persist:
        persist_completed_cycle(
            project,
            cycle,
        )

    return cycle, decision

def persist_completed_cycle(
    project: Project,
    cycle: dict[str, Any],
) -> None:
    pending = load_pending_command(
        project
    )

    if not pending:
        raise RelayError(
            "Cannot persist completed cycle: "
            "pending command no longer exists."
        )

    pending_sha = pending.get(
        "command_sha256"
    )

    cycle_sha = cycle.get(
        "command_sha256"
    )

    if (
        not pending_sha
        or not cycle_sha
        or pending_sha != cycle_sha
    ):
        raise RelayError(
            "Cannot persist completed cycle: "
            "pending command changed."
        )

    existing = load_cycles(
        project,
        10,
    )

    cycle_id = cycle.get(
        "cycle_id"
    )

    output_sha = cycle.get(
        "terminal_output_sha256"
    )

    for previous in existing:
        if (
            cycle_id is not None
            and previous.get(
                "cycle_id"
            ) == cycle_id
        ):
            raise RelayError(
                "Cycle has already been recorded."
            )

        if (
            output_sha
            and previous.get(
                "terminal_output_sha256"
            ) == output_sha
            and previous.get(
                "command_sha256"
            ) == cycle_sha
        ):
            raise RelayError(
                "This command/output cycle "
                "has already been recorded."
            )

    append_cycle(
        project,
        cycle,
    )

    clear_pending(
        project
    )


def build_gpt_prompt(
    project: Project,
    cycle: dict[str, Any],
    decision: Decision,
) -> str:
    config = load_config()

    template = config[
        "prompt_template"
    ]

    before = cycle.get(
        "git_before",
        {},
    )

    after = cycle.get(
        "git_after",
        {},
    )

    vote_summary = "; ".join(
        f"{vote.voter}={vote.verdict}"
        for vote in decision.votes
    )

    watchdog_status = (
        f"{decision.status} "
        f"({vote_summary})"
    )

    head_before = before.get(
        "head",
        "(unknown)",
    )

    head_after = after.get(
        "head",
        "(unknown)",
    )

    git_status_after = after.get(
        "git_status",
        "(unknown)",
    )

    values = {
        "project": project.name,
        "root": str(
            project.root
        ),
        "branch": after.get(
            "branch",
            "(unknown)",
        ),

        # Current template fields.
        "head_before": head_before,
        "head_after": head_after,
        "git_status_after": (
            git_status_after
        ),
        "watchdog_status": (
            watchdog_status
        ),

        # Backward-compatible fields used
        # by Project Relay v0.1 configs.
        "head": head_after,
        "git_status": git_status_after,

        "cwd": cycle.get(
            "cwd",
            os.getcwd(),
        ),
        "terminal_output": cycle.get(
            "terminal_output",
            "",
        ),
    }

    try:
        rendered = template.format(
            **values
        )
    except KeyError as exc:
        missing = str(
            exc
        ).strip("'")

        raise RelayError(
            "Prompt template contains an "
            "unsupported placeholder: "
            f"{{{missing}}}"
        ) from exc

    return (
        rendered.rstrip()
        + "\n"
    )

def build_human_required_report(project: Project, cycle: dict[str, Any], decision: Decision) -> str:
    lines = [
        "PROJECT RELAY — HUMAN INTERVENTION REQUIRED",
        "",
        f"Project: {project.name}",
        f"Repository: {project.root}",
        f"Reason: {decision.reason}",
        "",
        "Votes:",
    ]
    for vote in decision.votes:
        model = f" [{vote.model}]" if vote.model else ""
        lines.append(f"- {vote.voter}{model}: {vote.verdict} — {vote.reason}")
    lines += [
        "",
        "Latest command:",
        "```text",
        cycle.get("command", ""),
        "```",
        "",
        "Latest terminal output:",
        "```text",
        cycle.get("terminal_output", ""),
        "```",
        "",
        "The automatic relay stopped because a loop majority was detected. Review the situation manually before continuing.",
    ]
    return "\n".join(lines) + "\n"


def project_table() -> list[dict[str, str]]:
    config = load_config()
    active = config.get("active_project", "")
    rows = []
    for name in sorted(config["projects"]):
        project = project_from_config(name, config)
        meta = git_metadata(project)
        rows.append({
            "active": "*" if name == active else "",
            "name": name,
            "root": str(project.root),
            "branch": meta["branch"],
            "head": meta["head"],
        })
    return rows
