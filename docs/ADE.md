# Relay ADE — plan

The **Relay ADE** (Agentic Development Environment) turns Project Relay from
one chat driving your terminal into a small team:

| Role | Where it runs | Job |
|---|---|---|
| **PM** | claude.ai (your normal login) | Plans, breaks work into tasks, reviews risky commands, reads the evidence, decides when the goal is done. The strong model. |
| **Worker** | chatgpt.com (your normal login) | Turns one task into exactly one Bash block. |
| **Relay** (`prelayd` + extension) | your Mac | Moves messages between them, runs the Bash locally, records everything in SQLite, runs the local loop judges, enforces the safety rules. |
| **You** | the pinned dashboard tab | Watch, pause, answer when the PM asks for a human. |

It still costs nothing beyond the two web subscriptions: no API keys.

## One step

```
  ┌──────────── PM (claude.ai) ─────────────┐
  │ RELAY_TASK … END_RELAY_TASK             │  or RELAY_DONE / RELAY_ASK_HUMAN
  └───────────────┬─────────────────────────┘
                  ▼
  ┌──────────── Worker (chatgpt.com) ───────┐
  │ exactly one ```bash block               │
  └───────────────┬─────────────────────────┘
                  ▼
        Relay safety checks (guard, heredoc/bash -n)
                  │ risky? (git commit/push, rm, mv, …)
                  ├── yes ──► PM review: RELAY_APPROVE or RELAY_REVISE <feedback> ──► Worker
                  ▼
        Relay runs it in the repo, captures output + Git
                  ▼
        local judges vote (deterministic + Ollama)
                  ▼
  PM receives the evidence (+ judge verdict) and decides the next step
```

## Milestone pace (default)

Step pace asks Claude about every command (plus reviews), which burns
claude.ai usage. Milestone pace keeps the PM strategic:

```
PM kickoff ──► RELAY_PLAN + RELAY_ASSIGN <milestone, done-when criteria>
Worker      ──► RELAY_AGREE + its step list + first bash block
               (or RELAY_CONCERN: … → PM, nothing runs)
Relay runs it ──► the output goes back to the Worker ──► next bash block …
Worker      ──► RELAY_MILESTONE_DONE: <summary>  or  RELAY_BLOCKED: <why>
PM check-in ◄── progress report: assignment, the Worker's plan, commands since
               the last PM message with exit codes, Git HEAD before → after,
               last result, judges
PM          ──► RELAY_ASSIGN (next milestone) | RELAY_CONTINUE: <guidance> |
               RELAY_DONE | RELAY_ASK_HUMAN
```

The PM is checked in when the Worker reports done, blocked or a concern, when
the judges vote LOOP (two in a row stop the run), when you send a message
(`prelay tell`), when the Worker stops producing usable commands, and at
least every `checkin_every` commands (default 8). Reviews follow `--review`
(`push` by default here). A `RELAY_TASK` under milestone pace is taken as an
assignment, so a run can switch pace mid-way (`prelay pace`).

## Protocol (what the models must reply)

PM planning reply — exactly one of:

```
RELAY_TASK
<self-contained instructions for the Worker>
END_RELAY_TASK
```
```
RELAY_DONE
```
```
RELAY_ASK_HUMAN: <what you need from the human>
```

PM review reply: `RELAY_APPROVE`, or `RELAY_REVISE` followed by feedback.

Worker reply: exactly one fenced `bash` block (the existing solo protocol).

Anything else gets one format nudge, then the run stops for a human.

## Review policy (`--review`)

| Policy | PM reviews before running |
|---|---|
| `push` (default, milestone pace) | `git push/merge/rebase`, `gh pr/release/repo …` changes, `rm -r`, `curl … | sh` |
| `risky` (default, step pace) | `git commit`, `git push`, `rm`, `mv`, `chmod`, `curl … | sh`, package installs, scripts over 200 lines |
| `always` | every command |
| `never` | nothing (PM still sees every result) |

PM messages are the scarce resource (claude.ai usage limits), so `risky` is
the default.

## Loops

The local judges still vote after every command. In the ADE a LOOP verdict is
not a model switch: the PM already is the strong model. The PM gets the
verdict with the evidence and is asked to change approach. Two LOOP verdicts
in a row stop the run for a human.

## Chats and rollover

Each role has its own conversation lineage (`conversations.role`). Rollover
works per role exactly as in solo mode: the role writes a handoff, Relay
opens a new chat for that role and continues. The PM's handoff carries the
plan; the Worker needs little context because every task is self-contained.

## Safety model (unchanged, applied to both roles)

- One request → at most one Send; anything re-sent is a new request.
- Only durable, verified turn identities are accepted.
- A reply is taken only when it is final (action row / streaming flag), never
  on stability alone.
- Scripts that are cut off (unclosed heredoc, `bash -n` failure) or hit the
  guard (`reset --hard`, `stash`, `clean`, `checkout --`, `restore`, force
  push, `sudo`, …) never run.
- A command that started is never re-run after a crash.

## Browser layout

A browser extension cannot legitimately embed chatgpt.com and claude.ai in
one page (both forbid framing; stripping that protection would weaken their
security). The ADE therefore uses:

- **Dashboard** — an extension page you pin as a tab: explanation, live
  status of every project (current step, both chats, last command, exit code,
  judge votes, commits, timeline) and controls.
- **Arrange windows** — opens the PM (Claude) and Worker (ChatGPT) chats as
  two visible windows side by side and binds each to its project and role.
  Visible windows also avoid background-tab throttling, which stalls replies.
- **Auto-open** — when a running step needs a Claude or ChatGPT tab and none
  is connected (`missing_tab` in `/v2/status`), the extension opens that chat
  (or a new one) and binds it, or reloads a tab on that chat that was cut off.

## Build phases

| Phase | Scope | Proof |
|---|---|---|
| A | Schema v3 (roles, mode, goal, review policy); role-aware engine; ADE orchestration; PM protocol parsing; dashboard page; arrange windows | engine tests for every ADE transition |
| B | claude.ai site profile in the extension (composer, send, turn identity, completion, reply text), mock claude.ai page | E2E: mock Claude PM + mock ChatGPT Worker in one run |
| C | Live claude.ai probing with BROWSER_DIAG (same method that fixed ChatGPT), selector fixes | first live ADE cycle |
| D | Two-cycle live ADE run on a real project, restart in the middle | evidence in `prelay log` |

## Known limits

- claude.ai markup is not verified yet; every mismatch fails closed and is
  diagnosed from `BROWSER_DIAG` events (Phase C).
- Automating chatgpt.com and claude.ai is unofficial and not sanctioned by
  either provider's consumer terms. Keep a human-like pace.
- claude.ai usage limits make PM messages scarce; the review policy and
  self-contained tasks keep PM traffic low.
