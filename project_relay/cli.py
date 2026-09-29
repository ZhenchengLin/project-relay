from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from .core import (
    CONFIG_FILE,
    RelayError,
    build_gpt_prompt,
    build_human_required_report,
    choose_shell_block,
    clipboard_read,
    clipboard_write,
    command_warnings,
    complete_cycle,
    git_metadata,
    load_config,
    load_cycles,
    load_pending_command,
    project_table,
    register_project,
    resolve_project,
    run_watchdog,
    save_config,
    save_pending_command,
    use_project,
)


def read_input(path: str | None) -> str:
    if path:
        return Path(path).expanduser().read_text(encoding="utf-8")
    if not sys.stdin.isatty():
        return sys.stdin.read()
    return clipboard_read()


def cmd_register(args):
    project = register_project(args.name, args.root, args.branch or "")
    print(f"Registered {project.name}: {project.root}")
    return 0


def cmd_use(args):
    project = use_project(args.name)
    print(f"Active project: {project.name} ({project.root})")
    return 0


def cmd_projects(args):
    rows = project_table()
    if not rows:
        print("No projects registered.")
        return 0
    for row in rows:
        print(f"{row['active']:1} {row['name']:<12} {row['branch']:<32} {row['head']:<12} {row['root']}")
    return 0


def cmd_status(args):
    project = resolve_project(args.project)
    meta = git_metadata(project)
    pending = load_pending_command(project)
    recent = load_cycles(project, 3)
    print(f"Project: {project.name}")
    print(f"Root:    {project.root}")
    print(f"Branch:  {meta['branch']}")
    print(f"HEAD:    {meta['head']}")
    print(f"Pending: {'yes' if pending else 'no'}")
    print(f"Cycles:  {len(load_cycles(project))}")
    print("Git status:")
    print(meta["git_status"])
    if recent:
        last = recent[-1].get("watchdog", {})
        if last:
            print(f"Last watchdog: {last.get('status')} — {last.get('reason')}")
    return 0


def cmd_from_gpt(args):
    project = resolve_project(args.project)
    response = read_input(args.file)
    block = choose_shell_block(response, args.index)
    pending = save_pending_command(project, block, response)
    clipboard_write(block + "\n")
    print(f"Copied shell block for {project.name} ({len(block)} chars) to clipboard.")
    if pending["warnings"]:
        print("Warnings:")
        for warning in pending["warnings"]:
            print(f"  - {warning}")
    print("Review it, then paste into Terminal.")
    return 0


from .core import persist_completed_cycle


def cmd_to_gpt(args):
    project = resolve_project(
        args.project
    )

    terminal_output = read_input(
        args.file
    )

    cycle, decision = complete_cycle(
        project,
        terminal_output,
        cwd=args.cwd,
        persist=False,
    )

    if (
        decision.status
        == "HUMAN_REQUIRED"
    ):
        handoff = (
            build_human_required_report(
                project,
                cycle,
                decision,
            )
        )
    else:
        handoff = build_gpt_prompt(
            project,
            cycle,
            decision,
        )

    # Generate the complete handoff before
    # mutating cycle history. A template
    # failure therefore cannot leave a
    # half-recorded cycle.
    clipboard_write(
        handoff
    )

    persist_completed_cycle(
        project,
        cycle,
    )

    if (
        decision.status
        == "HUMAN_REQUIRED"
    ):
        print(
            "HUMAN_REQUIRED: "
            "loop majority detected."
        )

        for vote in decision.votes:
            print(
                f"  {vote.voter}: "
                f"{vote.verdict}"
            )

        print(
            "A human-intervention report "
            "was copied to the clipboard."
        )

        return 3

    print(
        f"Watchdog: "
        f"{decision.status}"
    )

    for vote in decision.votes:
        print(
            f"  {vote.voter}: "
            f"{vote.verdict}"
        )

    if (
        decision.status
        == "UNCERTAIN"
    ):
        print(
            "Warning: no majority; "
            "one loop vote exists. "
            "Review before continuing."
        )

    print(
        "Copied GPT handoff for "
        f"{project.name} "
        f"({len(terminal_output)} "
        "terminal chars) to clipboard."
    )

    return 0

def cmd_watch(args):
    project = resolve_project(args.project)
    decision = run_watchdog(project)
    print(f"Decision: {decision.status}")
    print(f"Reason:   {decision.reason}")
    for vote in decision.votes:
        model = f" [{vote.model}]" if vote.model else ""
        print(f"- {vote.voter}{model}: {vote.verdict}")
        if args.verbose:
            print(f"  {vote.reason}")
    return 3 if decision.status == "HUMAN_REQUIRED" else 0


def cmd_history(args):
    project = resolve_project(args.project)
    rows = load_cycles(project, args.limit)
    if not rows:
        print("No cycles recorded yet.")
        return 0
    for row in rows:
        wd = row.get("watchdog", {})
        print(
            f"{row.get('cycle_id')}  {wd.get('status', '(unscored)'):<14} "
            f"{row.get('git_before', {}).get('head', '?')} -> {row.get('git_after', {}).get('head', '?')}"
        )
        if args.verbose:
            print("  command:", row.get("command", "").splitlines()[0][:120] if row.get("command") else "")
            print("  reason: ", wd.get("reason", ""))
    return 0


from .core import list_ollama_models


def cmd_models(args):
    config = load_config()
    wd = config["watchdog"]

    changing = any((
        args.ollama_url,
        args.local_llm,
        args.logic_model,
    ))

    if changing:
        candidate_url = str(
            args.ollama_url
            or wd.get(
                "ollama_url",
                "http://127.0.0.1:11434",
            )
        ).strip()

        if args.local_llm:
            wd["voters"][0]["model"] = (
                args.local_llm
            )

        if args.logic_model:
            if len(
                wd["voters"]
            ) < 2:
                wd["voters"].append({
                    "name": "logic_model",
                    "model": (
                        args.logic_model
                    ),
                    "enabled": True,
                    "role": "logic_judge",
                })
            else:
                wd["voters"][1]["model"] = (
                    args.logic_model
                )

        installed = set(
            list_ollama_models(
                candidate_url
            )
        )

        configured = []

        for voter in wd.get(
            "voters",
            [],
        ):
            if not voter.get(
                "enabled",
                True,
            ):
                continue

            model = str(
                voter.get(
                    "model",
                    "",
                )
            ).strip()

            if model:
                configured.append(
                    (
                        str(
                            voter.get(
                                "name",
                                "model",
                            )
                        ),
                        model,
                    )
                )

        missing = [
            (
                voter_name,
                model,
            )
            for (
                voter_name,
                model,
            ) in configured
            if model not in installed
        ]

        if missing:
            missing_text = ", ".join(
                f"{name}={model}"
                for name, model
                in missing
            )

            available = (
                ", ".join(
                    sorted(
                        installed
                    )
                )
                or "(none)"
            )

            raise RelayError(
                "Configured Ollama model "
                "is not installed: "
                f"{missing_text}\n"
                "Available models: "
                f"{available}"
            )

        wd["ollama_url"] = (
            candidate_url
        )

        save_config(
            config
        )

    print(
        f"Config: {CONFIG_FILE}"
    )

    print(
        "Ollama: "
        f"{wd.get('ollama_url')}"
    )

    for voter in wd.get(
        "voters",
        [],
    ):
        print(
            f"{voter.get('name')}: "
            f"{voter.get('model')} "
            "enabled="
            f"{voter.get('enabled', True)} "
            "role="
            f"{voter.get('role')}"
        )

    return 0

from .relay import cli as relay_cli


def make_parser():
    ap = argparse.ArgumentParser(
        prog="prelay",
        description="Relay ADE: Claude plans, ChatGPT writes one Bash block, this machine runs it. "
                    "Start with 'prelay init'.",
    )
    ap.add_argument("--port", type=int, help="prelayd port (default from config: 7340)")
    sub = ap.add_subparsers(dest="command", required=True)

    relay_cli.add_commands(sub)

    p = sub.add_parser("register", help="Register a project (a git repository).")
    p.add_argument("name")
    p.add_argument("root")
    p.add_argument("--branch", default="")
    p.set_defaults(func=cmd_register)

    p = sub.add_parser("projects", help="List registered projects.")
    p.set_defaults(func=cmd_projects)

    p = sub.add_parser("models", help="Show or set the local Ollama judge models.")
    p.add_argument("--local-llm")
    p.add_argument("--logic-model")
    p.add_argument("--ollama-url")
    p.set_defaults(func=cmd_models)

    manual = sub.add_parser("manual", help="Clipboard workflow without the extension.")
    ms = manual.add_subparsers(dest="manual_command", required=True)

    p = ms.add_parser("use", help="Set the default project for manual commands.")
    p.add_argument("name")
    p.set_defaults(func=cmd_use)

    p = ms.add_parser("status")
    p.add_argument("--project")
    p.set_defaults(func=cmd_status)

    p = ms.add_parser("cmd", help="Copy the one shell block from a ChatGPT reply on the clipboard.")
    p.add_argument("--project")
    p.add_argument("--file")
    p.add_argument("--index", type=int)
    p.set_defaults(func=cmd_from_gpt)

    p = ms.add_parser("gpt", help="Record terminal output, run the judges, copy the next prompt.")
    p.add_argument("--project")
    p.add_argument("--file")
    p.add_argument("--cwd")
    p.set_defaults(func=cmd_to_gpt)

    p = ms.add_parser("watch", help="Run the loop judges on recorded manual cycles.")
    p.add_argument("--project")
    p.add_argument("--verbose", action="store_true")
    p.set_defaults(func=cmd_watch)

    p = ms.add_parser("history")
    p.add_argument("--project")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--verbose", action="store_true")
    p.set_defaults(func=cmd_history)

    return ap


def main():
    try:
        args = make_parser().parse_args()
        return int(args.func(args))
    except RelayError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
