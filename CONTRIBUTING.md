# Contributing

Thanks for helping. Project Relay is small and dependency-free on purpose:
the daemon is Python standard library only, and the extension is plain
JavaScript with no build step.

## Layout

| Path | What |
|---|---|
| `project_relay/relay/engine.py` | State machine: browser jobs, execution, judges, ADE sequencing, rollover, recovery |
| `project_relay/relay/ade.py` | Relay ADE protocol: prompts, PM directive parsing, review policy |
| `project_relay/relay/shell.py` | Shell block extraction, destructive-command guard, completeness check |
| `project_relay/relay/server.py` | `prelayd` HTTP API (127.0.0.1, token) |
| `project_relay/relay/store.py`, `project_relay/storage/` | SQLite schema, migrations and queries |
| `project_relay/relay/extension/` | Chrome extension: `content.js` (site profiles for ChatGPT and Claude), `relay-core.js` (pure logic), `background.js`, popup, dashboard |
| `project_relay/core.py` | Local loop judges (deterministic + Ollama) and manual mode |
| `tests/` | pytest suites; `tests/js/` extension unit tests and browser E2E harness |

## Ground rules

- **Safety invariants are not negotiable:** at most one Send per request, never
  re-run a started command, never act on a reply before it is final, never run
  a guarded or cut-off script. Changes that touch these need tests.
- **Selectors for chatgpt.com / claude.ai change often.** Prefer
  language-neutral signals (test ids, structure, position) over visible text,
  fail closed, and add what you learned to the mock pages in `tests/js/e2e/`.
  Use `BROWSER_DIAG` events (`prelay log PROJECT --verbose`) as evidence.
- Keep the daemon free of third-party dependencies.

## Tests

```bash
python3 -m pip install --user -e ".[dev]"
pytest
(cd tests/js && node --test ./*.test.mjs)
(cd tests/js && npm install) && RELAY_E2E=1 pytest tests/test_v2_e2e.py tests/test_ade_e2e.py
```

The E2E suites need Google Chrome installed and never touch the real sites:
every chatgpt.com / claude.ai request is answered by a local mock page in a
throwaway browser profile.

## Pull requests

Describe what changed and why, include test evidence, and call out anything
that affects the safety invariants or the database schema (migrations are
append-only in `project_relay/storage/`).
