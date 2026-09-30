# Project Relay

**Relay ADE — Claude plans, ChatGPT writes one Bash block, your machine runs
it, and the real output goes back. No API keys.**

Both models run in your own signed-in browser (your normal ChatGPT and Claude
subscriptions). A small Chrome extension moves messages between the two chats,
and a local daemon (`prelayd`) runs the commands in your repository, records
everything in SQLite, and enforces the safety rules. You stop copy-pasting
between a chat window and a terminal.

<!-- Meme slot: drop an image at docs/assets/beggars.png and uncomment:
![Get these beggars out of here — no API bill](docs/assets/beggars.png) -->

| Role | Where | Job |
|---|---|---|
| **PM** | claude.ai | Plans one small task at a time, reviews risky commands, reads the real terminal output, decides when the goal is done. |
| **Worker** | chatgpt.com | Turns each task into exactly one Bash block. |
| **Relay** | your machine | Moves the messages, runs the Bash, records everything, runs local loop judges, enforces the safety rules. |
| **You** | a pinned dashboard tab | Watch, pause, stop, and answer when the PM asks for a human. |

```
PM (Claude) ──task──▶ Worker (ChatGPT) ──one bash block──▶ Relay safety checks
    ▲                                                            │
    │                               risky? ──▶ PM review: approve / revise
    │                                                            ▼
    └── output + judge verdict ◀── local loop judges ◀── runs in your repo
```

There is also a **solo mode** (one ChatGPT chat plans *and* writes the
commands) and a **manual mode** (clipboard helpers, no extension).

## Requirements

- **macOS or Linux.** On Windows, run Relay inside **WSL** (Ubuntu); Chrome on
  Windows reaches it at `127.0.0.1`.
- **Python 3.10+** and **bash**.
- **Google Chrome** (or another Chromium browser that can load unpacked
  extensions).
- A **ChatGPT** account, and a **Claude** account for the ADE.
- Optional: **[Ollama](https://ollama.com)** for the local loop judges (Relay
  falls back to a built-in deterministic judge without it).

## Install

Fork the repo (or use it directly), then install the command line tool:

```bash
pipx install git+https://github.com/<you>/project-relay
```

or, from a clone:

```bash
git clone https://github.com/<you>/project-relay && cd project-relay
python3 -m pip install --user .
```

Then set everything up once:

```bash
prelay init
```

`prelay init` creates `~/.project-relay/` (config, database, a random token),
writes the Chrome extension to `~/.project-relay/extension`, starts the
daemon, and checks the local judges.

### Load the extension (once)

1. Open `chrome://extensions` and turn on **Developer mode**.
2. **Load unpacked** → choose `~/.project-relay/extension` (on macOS press
   **Cmd+Shift+G** in the file picker and paste the path; the folder is hidden).
3. Pin **Project Relay** in the toolbar, click it, and choose **Open Relay ADE
   dashboard**. Keep that tab pinned.

After upgrading Project Relay, run `prelay install-extension` and press **↻**
on the extension card, then reload any ChatGPT/Claude tabs.

## Run it

```bash
prelay register myapp ~/code/myapp        # any git repository
```

In the dashboard: pick the project, write a goal and the rules the PM must
always enforce, choose a review policy, and click **Start & arrange windows**.
Claude and ChatGPT open side by side and Relay takes it from there. From a
terminal the same is:

```bash
prelay ade myapp --goal "Get the test suite green" --rules "Never push. Never touch uncommitted files."
```

| Command | What it does |
|---|---|
| `prelay status` | Every run: status, step, chats, cycle count |
| `prelay log myapp` | Event log (add `--verbose` for browser diagnostics) |
| `prelay pause myapp` / `prelay resume myapp [--message "…"]` / `prelay stop myapp` | Control a run |
| `prelay start myapp --url https://chatgpt.com/c/…` | Solo mode in an existing ChatGPT chat (or `--new-chat --seed "…"`) |
| `prelay notes myapp [--add "…" \| --remove ID]` | Show/edit the project's memory and see its plan |
| `prelay doctor` | Check the installation |
| `prelay models --local-llm NAME --logic-model NAME` | Choose the Ollama judge models |
| `prelay daemon` / `prelay stop-daemon` | Start/stop `prelayd` (e.g. after a reboot) |

### Review policy (`--review`)

| Policy | The PM must approve before a command runs |
|---|---|
| `risky` (default) | commits, pushes, merges, deletes, moves, permission changes, package installs, `curl … \| sh`, scripts over 200 lines |
| `always` | every command (safest, uses the most Claude messages) |
| `never` | nothing (the PM still sees every result) |

## Supervisor, memory, plan and KPIs

- **Alerts.** `prelayd` sends a desktop notification (macOS Notification
  Center, or `notify-send` on Linux) when a run needs you, pauses, finishes,
  or has had no activity for `stall_minutes`. Alerts also appear in the
  dashboard timeline.
- **Budgets and quiet hours.** Cap how many messages Relay sends to Claude and
  ChatGPT per day (runs pause when a cap is reached), and set hours when Relay
  sends nothing new (replies in progress and commands still finish).
- **Project memory.** Facts a project must never forget. The PM (or the solo
  chat) writes `RELAY_NOTE: …` lines; you add or remove notes in the dashboard
  or with `prelay notes myapp --add "…"` / `--remove ID`. Active notes are put
  into every new chat, so automatic chat rollovers keep them.
- **Plan.** The PM keeps a checklist in a `RELAY_PLAN` block
  (`- [ ] T1 …`, `[x]` done, `[~]` in progress, `[!]` blocked); the dashboard
  shows progress.
- **KPIs.** Each run card shows commands run, success rate, commands per hour,
  average command time, loops caught, PM approvals and revisions, messages sent
  to Claude and ChatGPT, chat rollovers and time since the last activity.
  `prelay status` prints a one-line summary.

## Safety

Relay runs AI-written commands on your machine. Read
[SECURITY.md](SECURITY.md). In short, always on:

- A message is sent **at most once**; anything re-sent is a new, logged request.
- A reply is used only when it is **final**, never while a model is still writing.
- **Cut-off scripts never run** (unclosed heredoc, `bash -n` error).
- **Blocked outright:** `sudo`, `git reset --hard`, `git clean -f`, `git stash`,
  `git checkout --`, `git restore`, `git switch -f`, `git branch -D`, force
  push, `rm -rf ~`, disk formatting, … — also when written with `git -C dir` or
  `git -c key=value`.
- A command that started is **never re-run** after a crash; the PM is told.
- Two loop verdicts in a row **stop the run** for you.
- The daemon listens on `127.0.0.1` only and requires a token that only the
  extension's service worker holds; web pages cannot call it.

Use it on repositories you can afford to have modified, with a clean or
committed working tree, and give the PM explicit rules.

## Tips

- Keep the Claude and ChatGPT windows **visible** (side by side is ideal).
  Hidden or minimized tabs make both sites slow down their replies.
- Long chats **roll over** automatically: the model writes a handoff and Relay
  continues in a fresh chat for that role.
- ChatGPT and Claude UIs change. When something no longer matches, Relay stops
  safely and records `BROWSER_DIAG` / `BROWSER_FAILURE` events;
  `prelay log PROJECT --verbose` shows what the page looked like.

## Configuration

`~/.project-relay/config.json` (set `PROJECT_RELAY_HOME` to use another folder):

```json
{
  "relay": {
    "port": 7340,
    "max_cycles": 50,
    "command_timeout_seconds": 1800,
    "rollover_char_budget": 350000,
    "evidence_max_chars": 12000,
    "models": { "default_label": "", "strong_label": "Thinking" }
  },
  "supervisor": {
    "notifications": true,
    "stall_minutes": 25,
    "claude_daily_messages": 0,
    "chatgpt_daily_messages": 0,
    "quiet_hours": "23:00-07:00"
  },
  "watchdog": {
    "ollama_url": "http://127.0.0.1:11434",
    "voters": [
      { "name": "local_llm", "model": "qwen3.5:4b", "role": "progress_judge" },
      { "name": "logic_model", "model": "qwen3:1.7b", "role": "logic_judge" }
    ]
  }
}
```

Budgets of `0` mean unlimited; an empty `quiet_hours` disables quiet hours.
`models.*_label` apply to solo mode: when the judges detect a loop, Relay
switches ChatGPT's model picker to `strong_label`.

## How it works

- [docs/ADE.md](docs/ADE.md) — the Relay ADE design (roles, protocol, review, loops, rollover).
- [docs/architecture.md](docs/architecture.md) — the extension ↔ daemon ↔ SQLite architecture and recovery rules.
- [docs/manual-mode.md](docs/manual-mode.md) — clipboard workflow without the extension.

## Development

```bash
python3 -m pip install --user -e ".[dev]"
pytest                                   # engine, protocol, guard, runner, server
(cd tests/js && node --test ./*.test.mjs) # extension decision logic
```

The browser end-to-end tests drive the real extension, daemon, bash and git
against local mock ChatGPT/Claude pages in a throwaway Chrome profile (nothing
reaches the network). They need Google Chrome and `npm install` in `tests/js`:

```bash
(cd tests/js && npm install)
RELAY_E2E=1 pytest tests/test_v2_e2e.py tests/test_ade_e2e.py
```

See [CONTRIBUTING.md](CONTRIBUTING.md).

## Disclaimer

Project Relay automates the chatgpt.com and claude.ai websites through your own
browser session. This is unofficial and not endorsed or sanctioned by OpenAI or
Anthropic, and automating their consumer apps may conflict with their terms;
you are responsible for how you use it. Keep a human pace. The software is
provided as is, without warranty (see [LICENSE](LICENSE)).
