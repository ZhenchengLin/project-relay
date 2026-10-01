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
ASSIGN_START = "RELAY_ASSIGN"
ASSIGN_END = "END_RELAY_ASSIGN"
CONTINUE = "RELAY_CONTINUE"
# Worker markers in milestone pace.
AGREE = "RELAY_AGREE"
CONCERN = "RELAY_CONCERN"
MILESTONE_DONE = "RELAY_MILESTONE_DONE"
BLOCKED = "RELAY_BLOCKED"

REVIEW_POLICIES = ("risky", "push", "always", "never")
PACES = ("milestone", "step")

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


PM_MILESTONE_PROTOCOL = f"""
[Relay ADE — you are the PM]
You manage this project. A separate Worker model (ChatGPT) does the work: it
writes shell commands that Project Relay runs on the user's machine, reads the
real output itself and keeps going until its assignment is done. You do not
see every command; you check in when the Worker finishes, is blocked, raises a
concern, when the local loop judges flag a loop, or every few commands.
Reply with EXACTLY ONE of:

{ASSIGN_START}
<the next milestone for the Worker: what to achieve, the exact done-when
criteria and the evidence that proves them, constraints, paths, what not to
touch. A meaningful chunk of work, not a single command.>
{ASSIGN_END}

{CONTINUE}: <the Worker keeps its current assignment; your guidance on how to
push it forward>

{DONE}   (only when the whole goal is finished and verified by evidence)

{ASK_HUMAN}: <what you need from the human and why>

Never write the shell commands yourself.
{PLANNER_HINT}
""".strip()

WORKER_MILESTONE_PROTOCOL = f"""
[Relay ADE — you are the Worker]
The PM gave you an assignment (a milestone). You own it until it is done:
Project Relay runs each command you send and sends you the real output.
- First reply to a new assignment: a line `{AGREE}` followed by your short step
  list for it, then your first command. If the assignment or plan is wrong or
  unclear, reply `{CONCERN}: <why>` instead, with no command.
- Every reply: EXACTLY ONE fenced bash block for your next step, printing clear
  evidence (=== section headers, final status lines). No second block.
- When the done-when criteria are met and verified by evidence:
  `{MILESTONE_DONE}: <what you did, the evidence, anything the PM must know>`
  with no command.
- If you cannot make progress: `{BLOCKED}: <why, what you tried>` with no command.
""".strip()


@dataclass(frozen=True)
class PmDirective:
    kind: str                  # TASK | DONE | ASK_HUMAN | APPROVE | REVISE | NONE
    text: str = ""


def _lines(text: str) -> list[str]:
    return [line.rstrip() for line in (text or "").splitlines()]


def parse_plan(reply: str) -> PmDirective:
    """PM planning reply → TASK / ASSIGN / CONTINUE / DONE / ASK_HUMAN / NONE."""
    lines = _lines(reply)
    stripped = [line.strip().strip("`*").strip() for line in lines]
    if ASSIGN_START in stripped:
        start = stripped.index(ASSIGN_START)
        end = next((i for i in range(start + 1, len(stripped)) if stripped[i] == ASSIGN_END), None)
        if end is not None:
            assignment = "\n".join(lines[start + 1:end]).strip()
            if assignment:
                return PmDirective("ASSIGN", assignment)
    for i, line in enumerate(stripped):
        if line.startswith(CONTINUE):
            guidance = line[len(CONTINUE):].lstrip(": ").strip()
            rest = "\n".join(lines[i + 1:]).strip()
            return PmDirective("CONTINUE", "\n".join(x for x in (guidance, rest) if x))
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


@dataclass(frozen=True)
class WorkerSignal:
    kind: str                  # AGREE | CONCERN | MILESTONE_DONE | BLOCKED | NONE
    text: str = ""


def parse_worker(reply: str) -> WorkerSignal:
    """Milestone-pace markers in a Worker reply (a reply may also carry a command)."""
    lines = _lines(reply)
    for marker in (CONCERN, MILESTONE_DONE, BLOCKED):
        for i, line in enumerate(lines):
            token = line.strip().strip("`*").strip()
            if token.startswith(marker):
                head = token[len(marker):].lstrip(": ").strip()
                rest = "\n".join(lines[i + 1:]).strip()
                return WorkerSignal(marker.removeprefix("RELAY_"), "\n".join(x for x in (head, rest) if x))
    if any(line.strip().strip("`*").strip().startswith(AGREE) for line in lines):
        return WorkerSignal("AGREE")
    return WorkerSignal("NONE")


# Under the `push` policy the PM approves only what leaves the machine or is
# hard to undo locally.
PUSH_PATTERNS = (
    (re.compile(r"(?im)" + GIT + r"(push|merge|rebase)\b"), "pushes, merges or rebases"),
    (re.compile(r"(?im)\bgh\s+(pr|release|repo|issue)\s+(create|merge|close|delete|edit)\b"), "changes GitHub state"),
    (re.compile(r"(?im)(^|[\s;&|(])rm\s+(-\w*r\w*|--recursive)\b"), "deletes directories"),
    (re.compile(r"(?im)\b(curl|wget)\b[^\n|]*\|\s*(ba|z)?sh\b"), "pipes a download into a shell"),
)


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
    if policy == "push":
        return sorted({label for pattern, label in PUSH_PATTERNS if pattern.search(script)})
    reasons = [label for pattern, label in RISKY_PATTERNS if pattern.search(script)]
    if len(script.splitlines()) > LONG_SCRIPT_LINES:
        reasons.append(f"longer than {LONG_SCRIPT_LINES} lines")
    return sorted(set(reasons))


# ------------------------------------------------------------------ prompts

def match_text(prompt: str) -> str:
    """The part of a PM message that identifies it on claude.ai: the message
    without the protocol block every PM message ends with."""
    head = PM_PROTOCOL.strip().splitlines()[0]
    cut = prompt.find(head)
    return (prompt[:cut] if cut > 0 else prompt).strip()


def pm_protocol(pace: str) -> str:
    return PM_MILESTONE_PROTOCOL if pace == "milestone" else PM_PROTOCOL


def pm_kickoff(*, project: str, root: str, goal: str, rules: str, git: dict[str, Any],
               memory: str = "", pace: str = "step") -> str:
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
    if pace == "milestone":
        parts += ["", "First agree the plan: write it as a RELAY_PLAN block, then give the Worker its first "
                      "milestone. The Worker will confirm the plan or raise concerns before it starts."]
    return "\n".join(parts) + "\n\n" + pm_protocol(pace) + "\n"


def pm_evidence(*, evidence: str, watchdog_line: str, loop: bool, pace: str = "step") -> str:
    note = ""
    if loop:
        note = ("[Relay watchdog] The local judges voted that the last cycles are LOOPING. "
                "Change approach: state the root cause and give a genuinely different task.\n\n")
    return (note + "Result of the Worker's command:\n\n" + evidence.rstrip()
            + f"\n\nLoop judges: {watchdog_line}\n\nDecide the next step.\n\n" + pm_protocol(pace) + "\n")


def pm_review(*, task: str, command: str, reasons: list[str]) -> str:
    return (
        "The Worker proposes this command for your task. Relay will not run it until you decide.\n"
        f"Why review: {', '.join(reasons)}.\n\n"
        "Task:\n" + task.strip() + "\n\nCommand:\n" + _fence(prompts.truncate_middle(command, 12000), "bash")
        + f"\n\nReply with {APPROVE} to run it exactly as written, or {REVISE} followed by what the "
        "Worker must change. Do not rewrite the command yourself.\n"
    )


def pm_nudge(expected: str, pace: str = "step") -> str:
    return (f"[Relay] Relay could not find a valid {expected} in your last reply. "
            "Reply again using exactly the format below.\n\n" + pm_protocol(pace) + "\n")


CHECKIN_REASONS = {
    "MILESTONE_DONE": "The Worker reports the milestone done. Verify it against the done-when criteria.",
    "BLOCKED": "The Worker is blocked.",
    "CONCERN": "The Worker raised a concern about the assignment or plan before starting.",
    "LOOP": "The local loop judges say the Worker is going in circles.",
    "PERIODIC": "Periodic check-in.",
    "NO_COMMAND": "The Worker stopped producing usable commands.",
    "HUMAN": "The human sent you a message (above).",
}


def pm_checkin(*, reason: str, assignment: str, worker_plan: str, worker_message: str,
               commands: list[dict[str, Any]], head_before: str, git_now: dict[str, Any],
               last_evidence: str, watchdog_line: str, checkin_every: int) -> str:
    """Progress report for a PM check-in (milestone pace)."""
    lines = [f"[Relay check-in] {CHECKIN_REASONS.get(reason, reason)}", "",
             "Current assignment:", _fence(prompts.truncate_middle(assignment, 3000))]
    if worker_plan:
        lines += ["", "The Worker's plan when it took the assignment:", _fence(prompts.truncate_middle(worker_plan, 2000))]
    if worker_message:
        lines += ["", "The Worker says:", _fence(prompts.truncate_middle(worker_message, 4000))]
    if commands:
        lines += ["", f"Commands since your last message ({len(commands)}):"]
        for c in commands:
            first = next((l.strip() for l in (c["command"] or "").splitlines()
                          if l.strip() and not l.strip().startswith(("bash <<", "set -", "#", "cd "))), "")
            lines.append(f"- #{c['n']} exit {c['return_code']}: {first[:140]}")
    head_now = git_now.get("head", "")
    lines += ["", f"Git: branch {git_now.get('branch', '?')}, HEAD {head_before[:10] or '?'} → {head_now[:10] or '?'}",
              _fence(prompts.truncate_middle(git_now.get("git_status", ""), 1500))]
    if last_evidence:
        lines += ["", "Last command's result:", _fence(prompts.truncate_middle(last_evidence, 5000))]
    lines += ["", f"Loop judges (last command): {watchdog_line or 'n/a'}",
              f"(The Worker continues on its own; you are checked in every {checkin_every} commands or when "
              "something above happens.)", "", "Decide how to push the project forward.", "",
              PM_MILESTONE_PROTOCOL]
    return "\n".join(lines) + "\n"


def worker_assignment(*, assignment: str, plan: str, guidance: str = "", last_result: str | None = None) -> str:
    parts = ["Assignment from the PM:", "", assignment.strip()]
    if plan:
        parts += ["", "The overall plan (agreed with the PM):", plan.strip()]
    if guidance:
        parts += ["", "Guidance from the PM:", guidance.strip()]
    if last_result:
        parts += ["", "Result of your previous command (for context):", _fence(prompts.truncate_middle(last_result, 4000))]
    return "\n".join(parts) + "\n\n" + WORKER_MILESTONE_PROTOCOL + "\n"


def worker_guidance(*, guidance: str, assignment: str) -> str:
    return ("Guidance from the PM on your current assignment:\n\n" + guidance.strip()
            + "\n\nYour assignment (unchanged):\n" + _fence(prompts.truncate_middle(assignment, 2000))
            + "\n\nContinue with your next command.\n\n" + WORKER_MILESTONE_PROTOCOL + "\n")


def worker_evidence(*, evidence: str, assignment: str) -> str:
    return (evidence.rstrip() + "\n\nYour assignment (keep going until its done-when criteria hold):\n"
            + _fence(prompts.truncate_middle(assignment, 1500)) + "\n\n" + WORKER_MILESTONE_PROTOCOL + "\n")


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
