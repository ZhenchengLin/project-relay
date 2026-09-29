# Security model

Project Relay runs shell commands written by language models on your machine.
This page describes what protects you and what does not.

## What runs where

| Component | Runs | Can execute commands? |
|---|---|---|
| Chrome extension (content scripts on chatgpt.com / claude.ai) | in Chrome's isolated world | No |
| Extension service worker | in Chrome | No — it only forwards messages to the daemon |
| `prelayd` | your user account, on `127.0.0.1` | Yes — `bash` in the registered repository |

Commands run with **your** user permissions, in the registered project's
directory. Relay is not a sandbox.

## The daemon's API

- Binds to `127.0.0.1` only.
- Every request needs the `X-Relay-Token` header. The token is random, stored
  in `~/.project-relay/extension_token` (mode 0600) and in the installed
  extension's `relay-config.js`; only the extension's service worker reads it.
- Requests carrying a web `Origin` (anything other than `chrome-extension://`)
  are rejected, so scripts on chatgpt.com, claude.ai or any other site cannot
  drive Relay even if they guessed the port.
- The `debugger` permission is used only to type into the requesting
  chatgpt.com/claude.ai tab when its window is not focused, and detaches
  immediately.

## Before a command runs

1. Exactly one fenced bash block in the reply (otherwise: one reminder, then the
   PM or you decide).
2. **Guard** — blocked outright: `sudo`, `git reset --hard`, `git clean -f/-d/-x`,
   `git stash` (except `list`/`show`), `git checkout -- …` / `.` / `-f`,
   `git restore` (except `--staged`), `git switch -f/--discard-changes`,
   `git branch -D`, force pushes (`--force`, `+refspec`), `gh repo delete`,
   `mkfs`, destructive `diskutil`, shutdown/reboot, `rm -r` of `/` or `~`.
   Git patterns also match `git -C dir …`, `git -c key=value …` and other
   global options.
3. **Completeness** — every heredoc must be closed and `bash -n` must pass, so a
   reply that arrived cut off never runs.
4. **Relay ADE review** — under the default `risky` policy the PM must reply
   `RELAY_APPROVE` before commits, pushes, merges, deletions, moves, permission
   changes, package installs, `curl | sh` or long scripts run.

## What the guard does not stop

Pattern guards are a safety net, not a sandbox. A model can still write
commands that damage your project in ways no pattern recognizes (overwriting
files with `>`, deleting with a script, network calls, …). Mitigations:

- Run Relay on repositories whose work is committed or backed up.
- Give the PM explicit rules (`--rules "Never push. Never modify files outside src/."`).
- Use `--review always` for sensitive projects.
- Watch the dashboard, keep `max_cycles` modest, and read `prelay log`.
- Consider a dedicated user account, VM or container for untrusted work.

## Your chat accounts

Relay uses your existing chatgpt.com and claude.ai sessions in your own
browser. It never reads cookies or passwords, and never sends conversation
content anywhere except your local daemon. Automating these consumer sites is
unofficial and may conflict with the providers' terms.

## Reporting a vulnerability

Please open a GitHub security advisory (Security → Report a vulnerability) on
the repository rather than a public issue.
