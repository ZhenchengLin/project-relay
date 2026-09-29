"""Every text Relay sends to ChatGPT is built here."""
from __future__ import annotations

import json
from typing import Any

FENCE = "`" * 3

DONE_MARKER = "RELAY_DONE"
HANDOFF_MARKER = "RELAY_HANDOFF"


PROTOCOL = f"""
[Project Relay protocol]
- Relay runs your reply automatically on the user's machine and sends the real terminal output back here.
- If a CLI action is appropriate, reply with EXACTLY ONE fenced bash block. No alternatives, no second block.
- Never ask the human to copy/paste; Relay does that.
- Never claim success without evidence from the terminal output.
- If human judgment is required, explain why and do not include a bash block.
- When the whole project goal is finished and verified, reply with a line containing only {DONE_MARKER} and no bash block.
""".strip()


DEFAULT_SEED = """
Project Relay AUTO mode is now active for this project.
Continue from the exact current state of this conversation and the existing project plan.
Do not restart or redesign the project. Use the latest verified evidence already in this conversation.
""".strip()


def with_protocol(text: str) -> str:
    return text.rstrip() + "\n\n" + PROTOCOL + "\n"


def without_protocol(text: str) -> str:
    """The message body without the solo protocol footer (to re-target it at the ADE PM)."""
    body = text.rstrip()
    return body[: -len(PROTOCOL)].rstrip() if body.endswith(PROTOCOL) else body


def truncate_middle(text: str, limit: int) -> str:
    text = text or ""
    if limit <= 0 or len(text) <= limit:
        return text
    marker = f"\n...[Relay truncated {len(text) - limit} characters]...\n"
    keep = max(limit - len(marker), 0)
    head = keep // 3
    tail = keep - head
    return text[:head] + marker + text[len(text) - tail:]


MAX_EVIDENCE_LINE = 400


def clip_long_lines(text: str, limit: int = MAX_EVIDENCE_LINE) -> str:
    """Cut single lines (minified HTML/JSON, progress bars) that bloat the chat."""
    out = []
    for line in (text or "").splitlines():
        if len(line) > limit:
            line = line[:limit] + f" ...[Relay cut {len(line) - limit} chars of this line]"
        out.append(line)
    return "\n".join(out)


def _block(text: str, lang: str = "text") -> str:
    # Keep evidence from breaking out of its fence.
    safe = (text or "").replace(FENCE, "'''")
    return f"{FENCE}{lang}\n{safe.rstrip()}\n{FENCE}"


def evidence_prompt(
    *,
    project: str,
    root: str,
    command: str,
    return_code: int | None,
    output: str,
    git_before: dict[str, Any],
    git_after: dict[str, Any],
    watchdog_line: str,
    max_chars: int,
    note: str = "",
) -> str:
    parts = []
    if note:
        parts.append(note.strip())
        parts.append("")
    parts += [
        f"Relay ran your command for {project}. Continue from this exact result.",
        "",
        f"Repository: {root}",
        f"Branch: {git_after.get('branch', '(unknown)')}",
        f"HEAD before: {git_before.get('head', '(unknown)')}  HEAD after: {git_after.get('head', '(unknown)')}",
        f"Exit code: {return_code}",
        f"Loop watchdog: {watchdog_line}",
        "",
        "Command that ran:",
        _block(truncate_middle(command, 6000), "bash"),
        "",
        "Terminal output:",
        _block(truncate_middle(clip_long_lines(output), max_chars)),
        "",
        "Git status after:",
        _block(truncate_middle(git_after.get("git_status", ""), 4000)),
    ]
    return with_protocol("\n".join(parts))


ESCALATION_NOTE = (
    "[Relay watchdog] Local judges voted that the last cycles are LOOPING "
    "(repeating an approach without new evidence). Step back: re-read the "
    "evidence, state the actual root cause, and choose a genuinely different, "
    "more discriminating next step."
)


def format_nudge(block_count: int) -> str:
    if block_count == 0:
        problem = "Your last reply contained no bash block."
    else:
        problem = f"Your last reply contained {block_count} bash blocks."
    return with_protocol(
        f"[Relay] {problem} Relay can only run exactly one. "
        "If more CLI work is needed, reply again with exactly one fenced bash block "
        "(combine steps into one script). If human judgment is truly required, say so "
        f"without a bash block. If the project is complete, reply with {DONE_MARKER}."
    )


def blocked_command_prompt(reason: str) -> str:
    return with_protocol(
        f"[Relay safety guard] Relay refused to run your last command: {reason}. "
        "Propose a safe alternative as exactly one bash block, or explain why a human "
        "must do it."
    )


def incomplete_script_prompt(reason: str) -> str:
    return with_protocol(
        f"[Relay] Relay did NOT run your last script: {reason}. A long reply can "
        "arrive cut off. Please send the complete script again as exactly one bash "
        "block (shorter is safer)."
    )


def handoff_request() -> str:
    # Deliberately no protocol footer: this reply must not contain a command.
    return (
        "[Project Relay] This chat is getting close to ChatGPT's length limit. "
        "Relay will continue this exact work in a NEW chat and will paste your answer "
        "there as the only context.\n\n"
        f"Reply with a line containing only {HANDOFF_MARKER}, followed by a complete, "
        "self-contained handoff: the project goal, repository paths and branches, the "
        "working rules and constraints the user gave, everything already completed "
        "(with commit SHAs and test counts), the current state, the latest evidence, "
        "open problems, and the immediate next step. Do NOT include a bash block."
    )


def rollover_seed(
    *,
    chat_number: int,
    handoff: str,
    pending_prompt: str,
) -> str:
    body = [
        f"[Project Relay] Continuation chat #{chat_number}. The previous chat reached "
        "its length limit. Below is the handoff written in that chat; treat it as the "
        "full context. Do not restart or redesign.",
        "",
        "=== HANDOFF ===",
        handoff.strip(),
        "=== END HANDOFF ===",
    ]
    if pending_prompt.strip():
        body += [
            "",
            "Relay's next message for the previous chat was:",
            "",
            pending_prompt.strip(),
        ]
        return "\n".join(body) + "\n"
    return with_protocol("\n".join(body))


def fallback_handoff(
    *,
    project: str,
    root: str,
    seed: str,
    cycles: list[dict[str, Any]],
    last_reply: str,
) -> str:
    compact = [
        {
            "command": truncate_middle(c.get("command", ""), 1500),
            "exit_code": c.get("return_code"),
            "output": truncate_middle(c.get("terminal_output", ""), 2500),
            "head_after": c.get("git_after", {}).get("head"),
        }
        for c in cycles
    ]
    return "\n".join(
        [
            "(Relay-built handoff: the previous chat hit its hard limit before it could "
            "write one.)",
            f"Project: {project}",
            f"Repository: {root}",
            "",
            "Original instructions to the previous chat:",
            truncate_middle(seed, 6000),
            "",
            "Most recent cycles (oldest first):",
            _block(json.dumps(compact, ensure_ascii=False, indent=2), "json"),
            "",
            "Last assistant reply in the previous chat:",
            truncate_middle(last_reply, 6000),
        ]
    )


def interrupted_execution_prompt(command: str, git_after: dict[str, Any]) -> str:
    return with_protocol(
        "\n".join(
            [
                "[Relay recovery] Relay restarted while your last command was running. "
                "It may have partially executed; Relay will NOT re-run it automatically.",
                "",
                "Command:",
                _block(truncate_middle(command, 6000), "bash"),
                "",
                f"Branch: {git_after.get('branch')}  HEAD: {git_after.get('head')}",
                "Git status now:",
                _block(truncate_middle(git_after.get("git_status", ""), 4000)),
                "",
                "Inspect the current state first (read-only) before repeating any step.",
            ]
        )
    )


def reply_failed_prompt() -> str:
    return with_protocol(
        "[Relay] Your previous reply failed to generate completely. "
        "Please give that reply again."
    )


def contains_marker(text: str, marker: str) -> bool:
    return any(line.strip() == marker for line in (text or "").splitlines())
