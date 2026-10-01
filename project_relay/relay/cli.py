"""Relay commands: init, start, status, pause, resume, stop, log, daemon, doctor."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from .. import core
from ..storage.database import RelayDatabase
from . import config as relay_cfg
from .server import load_token

EXTENSION_SOURCE = Path(__file__).with_name("extension")
EXTENSION_FILES = ("manifest.json", "background.js", "relay-core.js", "content.js", "popup.html", "popup.js",
                   "dashboard.html", "dashboard.js")


def _port(args: Any = None) -> int:
    return int(getattr(args, "port", None) or relay_cfg.relay_config()["port"])


def call(method: str, path: str, body: dict[str, Any] | None = None, *, port: int | None = None,
         timeout: float = 15.0) -> dict[str, Any]:
    port = port or _port()
    data = json.dumps(body or {}).encode() if method == "POST" else None
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=data, method=method,
        headers={"Content-Type": "application/json", "X-Relay-Token": load_token()},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read()).get("error")
        except Exception:
            detail = exc.reason
        raise core.RelayError(f"prelayd: {detail}") from None
    except (urllib.error.URLError, ConnectionError, TimeoutError) as exc:
        raise core.RelayError(f"prelayd is not reachable on port {port}: {exc}") from None


def daemon_alive(port: int | None = None) -> bool:
    try:
        return bool(call("GET", "/v2/health", port=port, timeout=2).get("ok"))
    except core.RelayError:
        return False


def require_posix() -> None:
    if os.name == "nt":
        raise core.RelayError(
            "Native Windows is not supported (Relay runs commands in POSIX process groups). "
            "Install Project Relay inside WSL (Ubuntu) and run it there; Chrome on Windows reaches "
            "it at 127.0.0.1 through WSL's localhost forwarding.")


def ensure_daemon(port: int | None = None) -> None:
    require_posix()
    if daemon_alive(port):
        return
    load_token()
    package_parent = str(Path(__file__).resolve().parents[2])
    env = os.environ.copy()
    env["PYTHONPATH"] = package_parent + os.pathsep + env.get("PYTHONPATH", "")
    relay_cfg.DAEMON_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    log = open(relay_cfg.DAEMON_LOG_PATH, "a", encoding="utf-8")
    command = [sys.executable, "-m", "project_relay.relay.server"]
    if port:
        command += ["--port", str(port)]
    subprocess.Popen(command, stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                     start_new_session=True, env=env, cwd=str(Path.home()))
    for _ in range(50):
        if daemon_alive(port):
            return
        time.sleep(0.2)
    raise core.RelayError(f"prelayd did not start; see {relay_cfg.DAEMON_LOG_PATH}")


# ----------------------------------------------------------------- commands

def cmd_daemon(args: Any) -> int:
    if args.foreground:
        from .server import serve
        serve(args.port)
        return 0
    ensure_daemon(args.port)
    print(f"prelayd running on 127.0.0.1:{_port(args)} (log {relay_cfg.DAEMON_LOG_PATH})")
    return 0


def cmd_stop_daemon(args: Any) -> int:
    path = relay_cfg.DAEMON_PID_PATH
    if not path.exists():
        print("prelayd is not running.")
        return 0
    pid = int(path.read_text().strip())
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        path.unlink(missing_ok=True)
        print("prelayd was not running (stale pid file removed).")
        return 0
    for _ in range(50):
        if not daemon_alive(args.port):
            print("prelayd stopped.")
            return 0
        time.sleep(0.2)
    print("prelayd did not stop within 10s.", file=sys.stderr)
    return 1


def cmd_install_extension(args: Any) -> int:
    target = Path(args.dir).expanduser() if args.dir else relay_cfg.EXTENSION_INSTALL_DIR
    target.mkdir(parents=True, exist_ok=True)
    for name in EXTENSION_FILES:
        shutil.copy2(EXTENSION_SOURCE / name, target / name)
    config = {"port": _port(args), "token": load_token()}
    config_path = target / "relay-config.js"
    config_path.write_text(f"self.RELAY_CONFIG = {json.dumps(config)};\n", encoding="utf-8")
    config_path.chmod(0o600)
    print(f"Project Relay extension written to {target}\n")
    print("One-time Chrome setup (your normal, signed-in Chrome):")
    print("  1. Open chrome://extensions")
    print("  2. Turn on Developer mode (top right)")
    print(f"  3. Load unpacked -> choose {target}")
    print("  4. Pin 'Project Relay' in the toolbar")
    print("\nAfter upgrading Project Relay, run this again and press Reload on the extension card.")
    return 0


def cmd_start(args: Any) -> int:
    core.project_from_config(args.project)  # fail fast on unknown projects
    seed = args.seed
    if args.seed_file:
        seed = Path(args.seed_file).expanduser().read_text(encoding="utf-8")
    ensure_daemon(args.port)
    result = call("POST", "/v2/control/start", {
        "project": args.project, "conversation_url": args.url, "new_chat": args.new_chat,
        "seed": seed, "max_cycles": args.max_cycles,
    }, port=args.port)
    where = result.get("conversation_url") or "a new chat"
    print(f"Relay started for {args.project} in {where}.")
    print("In Chrome, open chatgpt.com and click 'Relay this tab' (or use the toolbar popup).")
    return 0


def cmd_ade(args: Any) -> int:
    core.project_from_config(args.project)
    goal = args.goal
    if args.goal_file:
        goal = Path(args.goal_file).expanduser().read_text(encoding="utf-8")
    ensure_daemon(args.port)
    result = call("POST", "/v2/control/start", {
        "project": args.project, "mode": "ade", "goal": goal, "rules": args.rules or "",
        "review_policy": args.review, "max_cycles": args.max_cycles,
        "conversation_url": args.worker_url, "new_chat": args.worker_new_chat,
        "pm_conversation_url": args.pm_url, "pm_new_chat": not args.pm_url,
    }, port=args.port)
    print(f"Relay ADE started for {args.project}.")
    print(f"  PM (claude.ai):      {result.get('pm_conversation_url') or 'a new Claude chat'}")
    print(f"  Worker (chatgpt.com): {result.get('conversation_url') or 'a new ChatGPT chat'}")
    print("Chrome (with the Project Relay extension) opens both chats by itself within ~30 s.")
    print("If it does not, open the Relay dashboard from the toolbar popup and click 'Arrange windows'.")
    return 0


def _print_status(runtimes: list[dict[str, Any]]) -> None:
    if not runtimes:
        print("No Relay runs yet. Start one: prelay start PROJECT --url https://chatgpt.com/c/...")
        return
    for rt in runtimes:
        conv = rt.get("conversation") or {}
        req = rt.get("request") or {}
        print(f"{rt['project']}{' [ADE]' if rt.get('mode') == 'ade' else ''}: {rt['status']}"
              + (f" — {rt['reason']}" if rt.get("reason") else ""))
        print(f"  cycles {rt['cycle_count']}/{rt['max_cycles']}  model {rt['model_mode'].lower()}"
              f"  chat #{conv.get('chat_number')} {conv.get('url') or '(new chat)'}"
              f"  length {conv.get('char_count', 0)}/{conv.get('budget', '?')}")
        k = rt.get("kpi") or {}
        if k.get("commands"):
            rate = k.get("success_rate")
            print(f"  run: {k['commands']} commands, {round((rate or 0) * 100)}% ok, "
                  + (f"{k['commands_per_hour']}/h, " if k.get("commands_per_hour") else "")
                  + f"loops caught {k['loops_caught']}, rollovers {k['rollovers']}"
                  + (f", plan {k['plan']['done']}/{k['plan']['total']}" if k.get("plan", {}).get("total") else ""))
        if req:
            print(f"  request {req['id']} {req['kind']} {req['state']}"
                  + (f" model={req['model']}" if req.get("model") else ""))
        for text in rt.get("pending_human") or []:
            print(f"  > waiting to tell the PM: {text[:120]}")
        missing = rt.get("missing_tab")
        if missing:
            site = "Claude (claude.ai)" if missing["site"] == "claude" else "ChatGPT (chatgpt.com)"
            print(f"  ! waiting for a {site} tab, but none is connected. The extension opens it within ~30 s;"
                  " if not, click Arrange windows in the dashboard. Nothing is re-sent.")


def cmd_status(args: Any) -> int:
    if not daemon_alive(args.port):
        print("prelayd is not running (prelay daemon).")
        return 1
    runtimes = call("GET", "/v2/status", port=args.port)["runtimes"]
    if args.json:
        print(json.dumps(runtimes, indent=2))
    else:
        _print_status(runtimes)
    return 0


def cmd_control(args: Any) -> int:
    body: dict[str, Any] = {"project": args.project}
    if getattr(args, "message", None):
        body["message"] = args.message
    call("POST", f"/v2/control/{args.action}", body, port=args.port)
    print(f"{args.project}: {args.action} ok")
    return 0


def cmd_tell(args: Any) -> int:
    result = call("POST", "/v2/control/tell", {"project": args.project, "text": args.text,
                                                "remember": args.remember}, port=args.port)
    print(f"{args.project}: added to the top of the PM's next message"
          + (" (already queued, so it goes out right away)" if result.get("in_next_message_now") else "")
          + ("; also kept in project memory." if args.remember else "."))
    return 0


def cmd_notes(args: Any) -> int:
    if args.add:
        call("POST", "/v2/notes/add", {"project": args.project, "text": args.add}, port=args.port)
    if args.remove is not None:
        call("POST", "/v2/notes/remove", {"project": args.project, "id": args.remove}, port=args.port)
    data = call("GET", f"/v2/notes?project={urllib.parse.quote(args.project)}", port=args.port)
    print(f"Project memory for {args.project}:")
    for note in data["notes"] or []:
        print(f"  #{note['id']:<4} {note['text']}  ({note['source']})")
    if not data["notes"]:
        print("  (none)")
    if data["plan"]:
        marks = {"done": "x", "doing": "~", "blocked": "!", "todo": " "}
        print("Plan:")
        for task in data["plan"]:
            print(f"  [{marks.get(task['status'], ' ')}] {task['task_key']} {task['title']}")
    return 0


def cmd_log(args: Any) -> int:
    with RelayDatabase(relay_cfg.DB_PATH) as db:
        project = db.conn.execute("SELECT id FROM projects WHERE name = ?", (args.project,)).fetchone()
        if project is None:
            raise core.RelayError(f"No Relay history for {args.project}.")
        rows = db.conn.execute(
            "SELECT created_at, event_type, request_id, payload_json FROM events "
            "WHERE project_id = ? ORDER BY id DESC LIMIT ?", (project["id"], args.limit),
        ).fetchall()
    for row in reversed(rows):
        payload = json.loads(row["payload_json"])
        short = {k: v for k, v in payload.items() if k not in {"message", "evidence"} or args.verbose}
        print(f"{row['created_at'][:19]}  {row['event_type']:<24} {row['request_id'] or '':<22} "
              f"{json.dumps(short, ensure_ascii=False)[:200]}")
    return 0


def cmd_init(args: Any) -> int:
    """One-command setup: config, extension, daemon, local judges."""
    require_posix()
    if not core.CONFIG_FILE.exists():
        core.save_config(core.load_config())
        print(f"Created {core.CONFIG_FILE}")
    cmd_install_extension(argparse.Namespace(dir=None, port=args.port))
    print()
    _report_ollama()
    ensure_daemon(args.port)
    print(f"\nprelayd running on 127.0.0.1:{_port(args)}.")
    print("\nNext:")
    print("  1. Load the extension (steps above), then toolbar icon -> 'Open Relay ADE dashboard'.")
    print("  2. prelay register NAME /path/to/repo")
    print("  3. In the dashboard: goal + rules -> 'Start & arrange windows'")
    print("     or: prelay ade NAME --goal '...' --rules '...'")
    print("  Solo mode (ChatGPT only): prelay start NAME --url https://chatgpt.com/c/<chat-id>")
    return 0


def _report_ollama() -> bool:
    wcfg = relay_cfg.watchdog_config()
    url = str(wcfg.get("ollama_url", "http://127.0.0.1:11434"))
    wanted = [v.get("model") for v in wcfg.get("voters", []) if v.get("enabled", True) and v.get("model")]
    try:
        installed = set(core.list_ollama_models(url))
    except core.RelayError as exc:
        print(f"Local judges: Ollama not reachable at {url} ({exc}).")
        print("  Relay still runs; loop detection falls back to the deterministic rule.")
        print("  For LLM judges: install Ollama, then `ollama pull " + " && ollama pull ".join(wanted) + "`")
        return False
    missing = [m for m in wanted if m not in installed]
    if missing:
        print(f"Local judges: missing Ollama models {missing}. Run: ollama pull {' '.join(missing)}")
        print("  or pick installed ones: prelay models --local-llm NAME --logic-model NAME")
        return False
    print(f"Local judges: {', '.join(wanted)} ready at {url}.")
    return True


def cmd_doctor(args: Any) -> int:
    ok = True

    def check(passed: bool, label: str, hint: str = "") -> None:
        nonlocal ok
        ok = ok and passed
        print(f"  [{'ok' if passed else '!!'}] {label}" + (f" — {hint}" if hint and not passed else ""))

    print("Project Relay doctor")
    config = core.load_config()
    check(core.CONFIG_FILE.exists(), f"config {core.CONFIG_FILE}", "run: prelay init")
    check(relay_cfg.TOKEN_PATH.exists(), "extension token", "run: prelay init")
    installed = relay_cfg.EXTENSION_INSTALL_DIR
    current = all(
        (installed / name).exists() and (installed / name).read_bytes() == (EXTENSION_SOURCE / name).read_bytes()
        for name in EXTENSION_FILES
    )
    check(current, f"extension files in {installed}",
          "run: prelay install-extension, then Reload the extension in chrome://extensions")
    check(daemon_alive(args.port), f"prelayd on 127.0.0.1:{_port(args)}", "run: prelay daemon")
    for name, entry in sorted(config.get("projects", {}).items()):
        root = Path(entry.get("root", "")).expanduser()
        check((root / ".git").exists(), f"project {name}: {root}", "not a git repository")
    if not config.get("projects"):
        check(False, "registered projects", "run: prelay register NAME /path/to/repo")
    _report_ollama()
    print("Status:", "PASS" if ok else "ATTENTION NEEDED")
    return 0 if ok else 1


def add_commands(sub: Any) -> None:
    p = sub.add_parser("init", help="One-time setup: config, Chrome extension, daemon, judge check.")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("doctor", help="Check the installation.")
    p.set_defaults(func=cmd_doctor)

    rs = sub
    p = rs.add_parser("daemon", help="Start prelayd in the background.")
    p.add_argument("--foreground", action="store_true")
    p.set_defaults(func=cmd_daemon)

    p = rs.add_parser("stop-daemon", help="Stop prelayd.")
    p.set_defaults(func=cmd_stop_daemon)

    p = rs.add_parser("install-extension", help="Write the Chrome extension (with its token).")
    p.add_argument("--dir")
    p.set_defaults(func=cmd_install_extension)

    p = rs.add_parser("start", help="Start an autonomous run for a registered project.")
    p.add_argument("project")
    where = p.add_mutually_exclusive_group()
    where.add_argument("--url", help="Existing ChatGPT conversation URL to continue.")
    where.add_argument("--new-chat", action="store_true", help="Start in a brand-new chat.")
    seed = p.add_mutually_exclusive_group()
    seed.add_argument("--seed", help="First message (default: continue the current plan).")
    seed.add_argument("--seed-file")
    p.add_argument("--max-cycles", type=int)
    p.set_defaults(func=cmd_start)

    p = rs.add_parser("ade", help="Start a Relay ADE run: Claude (PM) plans, ChatGPT (Worker) writes commands.")
    p.add_argument("project")
    goal = p.add_mutually_exclusive_group(required=True)
    goal.add_argument("--goal", help="What the PM should achieve.")
    goal.add_argument("--goal-file")
    p.add_argument("--rules", help="Rules the PM must always enforce (e.g. 'never push').")
    p.add_argument("--review", choices=["risky", "always", "never"], default="risky",
                   help="Which commands the PM must approve before they run (default: risky).")
    p.add_argument("--pm-url", help="Existing claude.ai chat for the PM (default: a new chat).")
    worker = p.add_mutually_exclusive_group()
    worker.add_argument("--worker-url", help="Existing chatgpt.com chat for the Worker.")
    worker.add_argument("--worker-new-chat", action="store_true")
    p.add_argument("--max-cycles", type=int)
    p.set_defaults(func=cmd_ade)

    p = rs.add_parser("status", help="Status of every Relay run.")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_status)

    helps = {"pause": "Pause a run.", "resume": "Resume a paused or stopped run.", "stop": "Stop a run."}
    for action in ("pause", "resume", "stop"):
        p = rs.add_parser(action, help=helps[action])
        p.add_argument("project")
        if action == "resume":
            p.add_argument("--message", help="Message to send to ChatGPT when resuming after a stop.")
        p.set_defaults(func=cmd_control, action=action)

    p = rs.add_parser("tell", help="Tell the PM (or ChatGPT in solo mode) something; it arrives with "
                                   "its next message, without pausing the run.")
    p.add_argument("project")
    p.add_argument("text")
    p.add_argument("--remember", action="store_true",
                   help="Also keep it in project memory, so every future chat gets it too.")
    p.set_defaults(func=cmd_tell)

    p = rs.add_parser("notes", help="Show, add or remove a project's memory notes (and see its plan).")
    p.add_argument("project")
    p.add_argument("--add", metavar="TEXT", help="Remember this fact in every future chat.")
    p.add_argument("--remove", metavar="ID", type=int, help="Forget note #ID.")
    p.set_defaults(func=cmd_notes)

    p = rs.add_parser("log", help="Recent Relay events for a project.")
    p.add_argument("project")
    p.add_argument("--limit", type=int, default=40)
    p.add_argument("--verbose", action="store_true")
    p.set_defaults(func=cmd_log)
