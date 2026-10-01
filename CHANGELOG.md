## 2.5.0 — Milestone pace: Claude plans and checks in, ChatGPT does the work

- New ADE pace, **milestone** (default): Claude agrees a plan and assigns a
  milestone (`RELAY_ASSIGN … END_RELAY_ASSIGN`, with done-when criteria);
  ChatGPT confirms it (`RELAY_AGREE` + its steps, or `RELAY_CONCERN`) and works
  through it on its own, each result going back to ChatGPT; it ends with
  `RELAY_MILESTONE_DONE` or `RELAY_BLOCKED`. Claude checks in with a progress
  report (commands and exit codes, Git HEAD before → after, last result, the
  Worker's plan and message) when the Worker is done, blocked or concerned,
  on a LOOP verdict, on a message from you, or every `checkin_every` commands
  (default 8), and replies `RELAY_ASSIGN` / `RELAY_CONTINUE: guidance` /
  `RELAY_DONE` / `RELAY_ASK_HUMAN`. **step** pace is the previous behaviour.
- New review policy **push** (default with milestone pace): the PM approves
  only pushes, merges, rebases, GitHub changes, recursive deletes and
  `curl | sh`, not every commit.
- `prelay ade --pace milestone|step --checkin-every N`; `prelay pace PROJECT
  milestone|step` switches a running run (the PM is told; a `RELAY_TASK` is then
  taken as an assignment); pace menu and current milestone on dashboard cards;
  `prelay status` shows commands since the last check-in.
- Schema v5: `project_runtime.pace`, `checkin_every`.
- E2E: a milestone run where the Worker runs two commands on its own and Claude
  is asked twice in total.

## 2.4.1 — Tell the PM, in the dashboard

- Each run card has a **Tell the PM** box (Tell ChatGPT in solo mode): type,
  tick "Remember it for every future chat" if it is a lasting fact, press Send
  or ⌘Enter. Messages waiting to go out are listed under it. The box keeps
  what you type and the cursor across the 2-second refresh.

## 2.4.0 — Tell the PM

- `prelay tell PROJECT "…" [--remember]` and **Tell the PM…** on each run card:
  a message for the planner (the PM, or ChatGPT in solo mode) that goes at the
  top of its next message without pausing the run. If that message is queued
  but not typed yet, it is replaced by the same message with yours on top (the
  old one is cancelled, never edited). Also delivered with a kickoff or resume.
  `--remember` also keeps it in project memory for every future chat.
- Waiting messages show on the run card and in `prelay status`; the timeline
  shows when they went out. API: `POST /v2/control/tell`; `pending_human` in
  `/v2/status`.

## 2.3.0 — Relay opens the chats itself

- Start a run with just a goal (`prelay ade PROJECT --goal-file FILE`): when a
  running step needs a Claude or ChatGPT tab and none is connected, the
  extension opens it (the run's chat, or a new chat; Claude left, ChatGPT
  right) and binds it to the run, and reopens or reloads it if it is closed or
  cut off by an extension reload. Checked every 30 s; at most one attempt per
  run and site every 3 minutes. Switch in the dashboard (on by default).
- `relay.tab_silent_seconds` in config.json (default 90).
- E2E: a run where the test opens no tab; the E2E Chrome maps claude.ai and
  chatgpt.com to a dead local address so it can never reach the real sites.
- Extension permission: `alarms`.

## 2.2.4 — Claude messages found by content

- Fix: claude.ai renders only the tail of a long chat (the previous reply, the
  last message and its reply), so position-based turn ids ("message #3") were
  wrong whenever a chat was longer than the page. The message Relay sent is now
  recognised by its content (letters and digits at a dozen spots, so Markdown
  rendering does not matter), must be the newest message and not the one that
  was newest before Send; its reply is the first reply after it. Turn ids are
  named after the request (`claude:user:r<request>`).
- Claude jobs carry `match_text` (the message without the protocol block).
- NOT_PERSISTED on Claude now reports what the page showed.
- Checked on the 11 real PM messages: all recognised after Markdown rendering,
  none confused with the previous message.
- E2E mock renders only the tail of the chat and Markdown user messages.

## 2.2.3 — Claude after a reload

- Claude replies are paired with the user message before them instead of
  being counted on their own (superseded by 2.2.4).
- Resuming into a chat that never received its first message re-sends that
  message instead of a bare note.
- Tabs report a probe while they are still looking for a reply.
- E2E: the Claude tab is reloaded mid-run; the mock renders history like
  claude.ai.

## 2.2.2 — Know when a chat tab is missing

- Relay tabs send a heartbeat every 20 s, even during a long step. When a
  run's current step needs a Claude or ChatGPT tab and none that could serve
  it has been heard from for 90 s (closed, unloaded by Chrome, or cut off by
  an extension reload), the dashboard card turns amber and says which tab to
  open, `prelay status` prints it, and the supervisor sends one alert.
- A tab whose script lost the extension after a reload/update now says
  "reload this tab" instead of "prelayd is not running".
- API: `POST /v2/browser/alive`; `missing_tab` in `/v2/status`.

## 2.2.1 — Dashboard for long goals

- Run cards fold the goal to its first line with its size (lines · chars);
  expand to read the whole plan in its own scroll box, with a Copy button.
  The fold and scroll position survive the 2-second refresh.
- Run cards are laid out in two columns (goal, plan and last command | chats,
  memory, activity), with Pause/Stop/Resume at the top.
- Start form: a large goal box with a live size count, "Load from file…" for
  the goal and the rules, and the optional chat URLs folded away.

## 2.2.0 — Supervisor, memory, plan, KPIs

- Supervisor inside prelayd: desktop alerts when a run needs you, pauses,
  finishes or stalls; daily Claude/ChatGPT message budgets; quiet hours.
- Project memory: `RELAY_NOTE:` lines from the PM (or solo chat) and notes you
  add are injected into every kickoff, handoff and fresh chat
  (`prelay notes`, dashboard).
- Plan: the PM's `RELAY_PLAN` checklist, with progress in the dashboard.
- KPIs per run in the dashboard and `prelay status`.
- Schema v4 (project_notes, plan_tasks); API `/v2/notes`, `/v2/supervisor`.
- Script checks before running (Layer 1), each reported to the model with its
  line: unclosed heredocs, bash syntax errors, Python syntax errors inside
  quoted `python … <<'PY'` blocks, a Markdown fence inside the script,
  invisible characters, Windows line endings, elided code
  (`# ... rest unchanged`), placeholders (`<your-path>`, `/path/to/`), and
  shellcheck errors when shellcheck is installed. Replayed on 51 real
  commands: only the one truly broken script is flagged.

## 2.1.2

- Fix: if the chat is still generating when Relay arrives to send (Stop shown
  instead of Send), wait for it to finish (up to 15 minutes) instead of
  pausing after 10 seconds. Seen live on a Chinese-language ChatGPT UI.
  Regression-tested: the mock page is busy for 12 s when Relay arrives; the
  old code pauses, the new code waits and finishes.
- Send-button failures record whether the chat was generating.
- Tests: optional status trace for E2E debugging; the E2E harness bounds
  every daemon call, always kills its headless Chrome and hard-stops itself.

## 2.1.1

- Fix: on a fresh ChatGPT chat the prompt could be typed into another
  textarea on the page (document order beat selector priority), so Send never
  enabled and the run paused (seen after an automatic chat rollover). The
  composer is now chosen by selector priority. Regression-tested with a decoy
  textarea in the mock page.

## 2.1.0 — Relay ADE

- Packaged for anyone to fork and install: MIT license, `pip`/`pipx`
  install with the extension bundled, `prelay init` one-step setup, `prelayd`
  entry point, new-user README, SECURITY.md, CONTRIBUTING.md, CI (macOS and
  Linux). Runs on macOS and Linux; Windows through WSL.
- Removed the obsolete V1 Playwright browser worker.
- Relay ADE: a PM chat on claude.ai plans and reviews, a Worker chat on
  chatgpt.com writes one bash block per task, Relay runs it and sends the
  evidence to the PM (`prelay ade`, dashboard **Start & arrange windows**).
- Review policy `risky` / `always` / `never`; the approved review request owns
  the execution, so nothing risky runs unapproved.
- Extension runs on claude.ai and chatgpt.com through site profiles; Claude
  turns are identified by position and accepted only once the server starts
  answering.
- Pinned **Relay ADE dashboard**: explanation, live step per run, both chats,
  last command, timeline, controls, arrange windows.
- Schema v3 (roles, run mode, goal, review policy).
- Fix: `git -C dir …` / `git -c k=v …` no longer bypass the destructive-command
  guard or the review policy.
- Fix: a handoff that hit the hard length limit lost its pending message.
- `resume --message` works on a paused run between requests.

## 2.0.0

- New extension-driven relay (`prelay relay ...`): runs in the user's normal
  Chrome instead of a Playwright-launched browser, which Cloudflare challenges
  at the document level.
- `prelayd` daemon: SQLite-authoritative engine, token-authenticated
  127.0.0.1 API, managed bash runner with process-group timeouts.
- Exactly-once Send per request; non-persisted sends recover as new requests;
  interrupted executions are never re-run.
- Local-judge LOOP escalates to a stronger ChatGPT model, with de-escalation.
- Automatic chat rollover with a ChatGPT-written handoff (or a Relay-built one
  when the hard limit hits first).
- Guard also blocks commands that discard uncommitted work.
- SQLite schema v2 (conversations, project_runtime, request kind/model).

## 0.3.7

- Require at least two comparable cycles before an LLM judge may establish LOOP.
- Give local judges explicit progress-versus-loop evidence rules.
- Treat unchanged Git HEAD as compatible with read-only/private-artifact progress.
- Request direct non-thinking output from Qwen3-family Ollama judges.
- Fall back to Ollama thinking text when response text is empty.
- Preserve the existing 2-of-3 watchdog aggregation policy.

## 0.3.6

- Detect current ChatGPT assistant responses using MarkdownRoot-* DOM nodes.
- Detect current user messages using group/user-message wrappers.
- Stop depending on removed conversation-turn and author-role attributes.
- Exclude CodeBlock-* Copy buttons from message-completion detection.
- Correlate new assistant MarkdownRoot nodes with the sent prompt.
- Recover an existing WAITING_ASSISTANT response without resending the prompt.
- Add assistant-count, candidate-size, message-Copy, and stability diagnostics.

## 0.3.5

- Stop requiring data-message-author-role=assistant for response detection.
- Detect ChatGPT replies from generic conversation-turn DOM nodes.
- Correlate the reply with the newly-sent user prompt.
- Treat stable reply text plus generation completion as a valid completion signal.
- Keep native Copy inside the reply turn as a stronger optional signal.
- Add live turn/new-turn/candidate/generation diagnostics to the browser panel.

## 0.3.4

- Add Hide control to the ChatGPT Project Relay panel.
- Keep AUTO execution active while the panel is hidden.
- Show a minimal PR restore button while hidden.
- Preserve panel visibility across reloads in the same tab.

## 0.3.3

- Port the proven Mues native-Copy response completion strategy into Project Relay.
- Snapshot native Copy controls by DOM identity before sending a prompt.
- Treat a new native Copy control as the primary completed-assistant signal.
- Keep stable assistant-DOM completion as a fallback only.
- Recover an already-finished assistant response after userscript reload while the backend is WAITING_ASSISTANT.
- Correlate recovery with the latest user turn before importing the response.

## 0.3.2

- Update ChatGPT composer discovery for current textarea, role=textbox, ProseMirror, and Lexical layouts.
- Add current composer submit-button detection.
- Support native textarea value updates as well as contenteditable composers.
- Include browser DOM diagnostics when composer discovery fails.

## 0.3.1

- Fix AUTO background launch falsely detecting its own foreground child as an already-running server.
- Validate AUTO PID ownership before status or termination.
- Avoid terminating an unrelated process if macOS reuses a stale PID.
- Add regression coverage for foreground self-PID ownership.

## 0.3.0

- Add end-to-end Project Relay AUTO mode.
- Add a localhost browser bridge and ChatGPT Tampermonkey adapter.
- Automatically capture one assistant shell block and execute it.
- Automatically capture stdout/stderr/exit code and record the Relay cycle.
- Automatically run deterministic, progress-model, and logic-model votes.
- Automatically queue terminal evidence back into the same ChatGPT conversation.
- Stop on UNCERTAIN, HUMAN_REQUIRED, ambiguous shell output, dangerous commands, timeout, or max-cycle limits.
- Add auto-status, auto-stop, and browser-install commands.

## 0.2.3

- Support legacy prompt templates using {head} and {git_status}.
- Reject `prelay gpt` when no pending GPT command exists.
- Build and copy the GPT handoff before persisting a completed cycle.
- Prevent duplicate cycle persistence.
- Store watchdog votes inside completed cycle records.

## 0.2.2

- Validate configured local voters against the live Ollama model inventory.
- Reject nonexistent model names without mutating Project Relay configuration.
- Make model inspection read-only when no configuration arguments are supplied.
- Use qwen3:1.7b as the validated default logic voter.

## 0.2.1

- Treat a real Git HEAD change as deterministic progress immediately.
- Detect repeated normalized failures even when command wording changes.
- Increase local-model timeout resilience.
- Prefer qwen3:1.7b over llama3.2:3b as the second local voter for the validated setup.

# Changelog

## 0.2.0 — 2026-09-26

- Standalone multi-project architecture
- Clipboard relay for GPT ↔ Terminal
- Per-project cycle history
- Deterministic loop detection
- Two configurable local Ollama voters
- Majority voting and HUMAN_REQUIRED stop state
- No automatic execution or project replanning
