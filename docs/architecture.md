# Project Relay V2 — extension-driven relay

## Why V2

V1 drove ChatGPT from a Playwright-launched Chrome. On 2026-09-28 a read-only
diagnostic showed Cloudflare answers even the top-level `chatgpt.com` document
with `403 cf-mitigated: challenge` for that browser, while ordinary Chrome on the
same profile loads normally. Session restore and storageState cannot fix a
page-level challenge, and Relay does not evade bot detection. V2 therefore runs
inside the user's normal, signed-in Chrome as an extension, the same place the
prototype userscript always worked.

## Components

```
Chrome (normal profile, signed in)
  └─ Project Relay extension (MV3)
       content.js + relay-core.js   chatgpt.com page driver (isolated world)
       background.js                only holder of the daemon token
       popup                        start / pause / resume / stop, status
            │  HTTP 127.0.0.1:7340, X-Relay-Token, chrome-extension:// origin only
            ▼
prelayd (python -m project_relay.relay.server)
  RelayEngine        state machine, rollover, escalation, recovery
  runner             /bin/bash in its own process group, timeout, bounded output
  watchdog           prototype deterministic + Ollama voters, fed from SQLite
  SQLite (~/.project-relay/relay.db, schema v2) — the authority
```

The extension never executes anything. The daemon never touches the page.

## One request

| Step | Who | State |
|---|---|---|
| poll returns a `submit` job | daemon | QUEUED → PREPARING_BROWSER |
| navigate, select model, fill composer, capture logical baseline | extension | |
| `ready` — the only path to SUBMITTING, granted at most once | daemon | → READY_TO_SUBMIT → SUBMITTING |
| click Send once | extension | |
| exactly one new stable `group:user:K` (not `pending-*`, not in baseline, last group, persists for the settle interval) | extension | |
| `accepted` (binds the new chat URL on first send) | daemon | → PROMPT_ACCEPTED → WAITING_ASSISTANT |
| `group:assistant:K` appears | extension | → ASSISTANT_BOUND |
| completion evidence: text non-empty, Stop gone, text stable, turn Copy action (or long stability) | extension | → ASSISTANT_COMPLETE |
| exactly one bash block, guard passes | daemon | → COMMAND_VALIDATED |
| execution row `started_at` persisted, then run | daemon | → RUNNING_CLI → CLI_COMPLETE |
| local judges vote | daemon | → RUNNING_WATCHDOG → CONTINUE_READY → COMPLETED + successor QUEUED |

## Invariants

- One request → at most one Send. Anything re-sent is a **new** request; the
  predecessor records `successor_request_id`.
- `pending-chatgpt-submit` (any `pending-*` key) is never a durable identity.
- A command whose execution started is never re-run. After a crash Relay tells
  ChatGPT the command may have partially run and asks it to inspect first.
- Ambiguity → `RECOVERY_REQUIRED` → a bounded policy, else `HUMAN_REQUIRED`.

## Recovery policies

| Code (from the extension or the engine) | Policy |
|---|---|
| `NOT_PERSISTED` — no durable turn after Send **and** a full reload | new request with the same prompt, at most `unpersisted_resubmits` (1) in a row |
| `AMBIGUOUS_SUBMISSION` | human |
| `CONVERSATION_LIMIT` | hard-limit rollover with a Relay-built handoff |
| `REPLY_FAILED` / `REPLY_TIMEOUT` | ask ChatGPT to repeat the reply, once in a row |
| `INTERRUPTED_EXECUTION` (daemon restarted in RUNNING_CLI) | tell ChatGPT, never re-run |
| `MODEL_UNAVAILABLE` (pre-send) | drop the model choice, return to default mode |
| `AUTH_REQUIRED`, `PAGE_BROKEN`, `COMPOSER_NOT_EMPTY` (pre-send) | pause the run |

## Chat rollover

Each project owns a lineage of `conversations`. When the active chat's prompt +
reply characters would exceed `rollover_char_budget` (default 350 000), the next
request becomes a `HANDOFF` request: ChatGPT writes a self-contained handoff
starting with `RELAY_HANDOFF`. Relay retires the chat, opens a new chat, and
sends the handoff plus the evidence it was about to send. If ChatGPT's hard limit
arrives first, Relay builds the handoff itself from the original instructions,
recent cycles and the last reply.

## Loop escalation

After each cycle the prototype watchdog (deterministic rule + Ollama
`progress_judge` + `logic_judge`) votes. A LOOP majority switches the run to the
strong model (`models.strong_label`, default "Thinking") and adds a
step-back note to the next prompt. A second LOOP on the strong model stops for a
human. After `deescalate_after_progress` (2) progress cycles the run returns to
`models.default_label`. Set that label to your normal model name
if you want Relay to switch back; if it is empty, Relay does not change the
model picker.

## Configuration (`~/.project-relay/config.json`, key `relay`)

```json
{
  "relay": {
    "port": 7340,
    "max_cycles": 50,
    "command_timeout_seconds": 1800,
    "rollover_char_budget": 350000,
    "evidence_max_chars": 12000,
    "format_nudges": 1,
    "unpersisted_resubmits": 1,
    "models": { "default_label": "", "strong_label": "Thinking" },
    "deescalate_after_progress": 2
  }
}
```

The watchdog voters keep using the existing `watchdog` key.

## Tests

- `pytest` — engine, runner, guard, server, migration (V1 tests unchanged).
- `cd tests/js && node --test ./*.test.mjs` — extension decision logic.
- `RELAY_E2E=1 pytest tests/test_v2_e2e.py` — real extension (loaded through CDP
  `Extensions.loadUnpacked` into a throwaway profile), real daemon, bash and git,
  against an intercepted mock `chatgpt.com` that uses the live DOM markers.
  Nothing reaches the network.

## Known limits

- Live ChatGPT markup changes can break selectors; every miss fails closed
  (pause / recovery), never a blind resend.
- Keep the Relay tab open (a pinned tab is best) and exclude chatgpt.com from
  Chrome's Memory Saver so the tab is not discarded.
- The model picker automation matches labels in the menu; confirm
  `strong_label` against your account's menu.
- Automating the ChatGPT website is unofficial and not sanctioned by OpenAI's
  terms; use a normal, human-like pace.
