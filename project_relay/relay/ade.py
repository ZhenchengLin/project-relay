"""
Relay ADE protocol: the PM (claude.ai) plans and reviews, the Worker
(ChatGPT) writes one Bash block per task. Pure functions: prompts, directive
parsing and the review policy. The engine does the sequencing.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from . import prompts
from .memory import PLANNER_HINT
from .shell import GIT

TASK_START = "RELAY_TASK"
TASK_END = "END_RELAY_TASK"
DONE = "RELAY_DONE"
ASK_HUMAN = "RELAY_ASK_HUMAN"
APPROVE = "RELAY_APPROVE"
REVISE = "RELAY_REVISE"

REVIEW_POLICIES = ("risky", "always", "never")

PM_PROTOCOL = f"""
[Relay ADE — you are the PM]
You manage this project. A separate Worker model writes the shell commands and
Project Relay runs them on the user's machine; you see the real results.
Reply with EXACTLY ONE of:

{TASK_START}
<one self-contained task for the Worker: goal of this step, exact paths,
constraints, what evidence the command must print>
{TASK_END}

{DONE}   (only when the whole goal is finished and verified by evidence)

{ASK_HUMAN}: <what you need from the human and why>

Keep each task small and verifiable. Never write the shell command yourself.
{PLANNER_HINT}
""".strip()

WORKER_PROTOCOL = """
[Relay ADE — you are the Worker]
A PM model gives you one task at a time and reviews the real terminal output.
Turn the task into EXACTLY ONE fenced bash block that Project Relay will run.
Print clear evidence (=== section headers, final status lines). No prose
alternatives, no second block. If the task is unsafe or impossible, say why
without a bash block.
""".strip()


@dataclass(frozen=True)
class PmDirective:
    kind: str                  # TASK | DONE | ASK_HUMAN | APPROVE | REVISE | NONE
    text: str = ""


def _lines(text: str) -> list[str]:
    return [line.rstrip() for line in (text or "").splitlines()]


def parse_plan(reply: str) -> PmDirective:
    """PM planning reply → TASK / DONE / ASK_HUMAN / NONE."""
    lines = _lines(reply)
    stripped = [line.strip().strip("`*").strip() for line in lines]
    if TASK_START in stripped:
        start = stripped.index(TASK_START)
        end = next((i for i in range(start + 1, len(stripped)) if stripped[i] == TASK_END), None)
        if end is not None:
            task = "\n".join(lines[start + 1:end]).strip()
            if task:
                return PmDirective("TASK", task)
    for line in stripped:
        if line.startswith(ASK_HUMAN):
            return PmDirective("ASK_HUMAN", line[len(ASK_HUMAN):].lstrip(": ").strip() or reply.strip())
    if DONE in stripped:
        return PmDirective("DONE")
    return PmDirective("NONE")


def parse_review(reply: str) -> PmDirective:
    """PM review reply → APPROVE / REVISE (with feedback) / NONE."""
    lines = _lines(reply)
    for i, line in enumerate(lines):
        token = line.strip().strip("`*").strip()
        if token == APPROVE:
            return PmDirective("APPROVE")
        if token.startswith(REVISE):
            feedback = token[len(REVISE):].lstrip(": ").strip()
            rest = "\n".join(lines[i + 1:]).strip()
            return PmDirective("REVISE", "\n".join(x for x in (feedback, rest) if x) or "Revise the command.")
    return PmDirective("NONE")


# Commands the PM must approve under the default `risky` policy.
RISKY_PATTERNS = (
    (re.compile(r"(?im)" + GIT + r"(commit|push|merge|rebase|tag|cherry-pick|revert|am)\b"), "changes Git history"),
    (re.compile(r"(?im)(^|[\s;&|(])(rm|rmdir|unlink|shred|truncate)\s"), "deletes files"),
    (re.compile(r"(?im)(^|[\s;&|(])mv\s"), "moves files"),
    (re.compile(r"(?im)(^|[\s;&|(])(chmod|chown)\s"), "changes permissions"),
    (re.compile(r"(?im)\b(curl|wget)\b[^\n|]*\|\s*(ba|z)?sh\b"), "pipes a download into a shell"),
    (re.compile(r"(?im)\b(pip3?|npm|pnpm|yarn|brew|conda|cargo|gem)\s+(install|add|uninstall|remove)\b"),
     "installs or removes packages"),
    (re.compile(r"(?im)\bgh\s+(pr|release|repo|issue)\s+(create|merge|close|delete|edit)\b"), "changes GitHub state"),
)
LONG_SCRIPT_LINES = 200


def review_reasons(script: str, policy: str) -> list[str]:
    """Why the PM must review this command before it runs (empty = run it)."""
    if policy == "never":
        return []
    if policy == "always":
        return ["review policy is 'always'"]
    reasons = [label for pattern, label in RISKY_PATTERNS if pattern.search(script)]
    if len(script.splitlines()) > LONG_SCRIPT_LINES:
        reasons.append(f"longer than {LONG_SCRIPT_LINES} lines")
    return sorted(set(reasons))


# ------------------------------------------------------------------ prompts

def pm_kickoff(*, project: str, root: str, goal: str, rules: str, git: dict[str, Any],
               memory: str = "") -> str:
    parts = [
        f"Project Relay ADE is starting on project {project}.",
        f"Repository: {root}",
        f"Branch: {git.get('branch', '(unknown)')}  HEAD: {git.get('head', '(unknown)')}",
        "",
        "Goal:",
        goal.strip() or "Continue from the current plan in this conversation.",
    ]
    if rules.strip():
        parts += ["", "Rules from the human (must always hold):", rules.strip()]
    parts += ["", "Uncommitted state right now:", _fence(prompts.truncate_middle(git.get("git_status", ""), 3000))]
    if memory.strip():
        parts += ["", memory.strip()]
    return "\n".join(parts) + "\n\n" + PM_PROTOCOL + "\n"


def pm_evidence(*, evidence: str, watchdog_line: str, loop: bool) -> str:
    note = ""
    if loop:
        note = ("[Relay watchdog] The local judges voted that the last cycles are LOOPING. "
                "Change approach: state the root cause and give a genuinely different task.\n\n")
    return (note + "Result of the Worker's command:\n\n" + evidence.rstrip()
            + f"\n\nLoop judges: {watchdog_line}\n\nDecide the next step.\n\n" + PM_PROTOCOL + "\n")


def pm_review(*, task: str, command: str, reasons: list[str]) -> str:
    return (
        "The Worker proposes this command for your task. Relay will not run it until you decide.\n"
        f"Why review: {', '.join(reasons)}.\n\n"
        "Task:\n" + task.strip() + "\n\nCommand:\n" + _fence(prompts.truncate_middle(command, 12000), "bash")
        + f"\n\nReply with {APPROVE} to run it exactly as written, or {REVISE} followed by what the "
        "Worker must change. Do not rewrite the command yourself.\n"
    )


def pm_nudge(expected: str) -> str:
    return (f"[Relay] Relay could not find a valid {expected} in your last reply. "
            "Reply again using exactly the format below.\n\n" + PM_PROTOCOL + "\n")


def pm_review_nudge() -> str:
    return f"[Relay] Reply with exactly {APPROVE}, or {REVISE} followed by feedback.\n"


def worker_task(*, task: str, last_result: str | None) -> str:
    parts = ["Task from the PM:", "", task.strip()]
    if last_result:
        parts += ["", "Result of your previous command (for context):",
                  _fence(prompts.truncate_middle(last_result, 6000))]
    return "\n".join(parts) + "\n\n" + WORKER_PROTOCOL + "\n"


def worker_revise(*, feedback: str, command: str) -> str:
    return ("The PM did not approve your command. Feedback:\n\n" + feedback.strip()
            + "\n\nYour previous command:\n" + _fence(prompts.truncate_middle(command, 6000), "bash")
            + "\n\nSend a corrected version.\n\n" + WORKER_PROTOCOL + "\n")


def _fence(text: str, lang: str = "text") -> str:
    fence = "`" * 3
    return f"{fence}{lang}\n{(text or '').replace(fence, chr(39) * 3).rstrip()}\n{fence}"
