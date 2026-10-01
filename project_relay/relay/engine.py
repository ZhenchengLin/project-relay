"""
Relay engine: SQLite-authoritative orchestration of one autonomous run per project.

Browser side (called by the extension through the HTTP server):
    poll -> ready -> [extension clicks Send once] -> accepted -> bound -> complete
    failure(code) at any point.

Engine side (step(), driven by one background thread):
    ASSISTANT_COMPLETE -> COMMAND_VALIDATED -> RUNNING_CLI -> CLI_COMPLETE
    -> RUNNING_WATCHDOG -> CONTINUE_READY -> COMPLETED (+ successor QUEUED)

Invariants:
    * One request -> at most one Send. `ready` is the only path to SUBMITTING and
      succeeds at most once per request. Anything that must be re-sent becomes a
      NEW request whose predecessor records successor_request_id.
    * pending-chatgpt-submit is never a durable identity.
    * A command whose execution started but did not provably finish is never
      re-run; ChatGPT is told and asked to inspect state instead.
    * Ambiguity -> RECOVERY_REQUIRED -> policy or HUMAN_REQUIRED, never guessing.
"""
from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable

from .. import core
from ..state import StateError, sha256_text, transition_request_in
from ..storage.database import RelayDatabase
from . import ade, kpi, memory, prompts, store
from .config import relay_config, watchdog_config
from .runner import RunResult, run_bash
from .shell import dangerous_reason, extract_shell_blocks, script_problems
from .urls import ROLE_SITE, canonical_conversation_url, conversation_id, is_new_chat_page, site_of

PROVISIONAL_USER_TURN = "group:user:pending-chatgpt-submit"
LEASE_TTL_SECONDS = 45.0
# A run whose current step needs a Claude/ChatGPT tab reports "no tab" after this
# much silence from every tab that could serve it (tabs send a heartbeat every 20 s).
TAB_SILENT_SECONDS = 90.0

BROWSER_STATES = frozenset(
    {"QUEUED", "PREPARING_BROWSER", "READY_TO_SUBMIT", "SUBMITTING",
     "PROMPT_ACCEPTED", "WAITING_ASSISTANT", "ASSISTANT_BOUND"}
)
OBSERVE_STATES = frozenset({"SUBMITTING", "PROMPT_ACCEPTED", "WAITING_ASSISTANT", "ASSISTANT_BOUND"})
ACTIVE_RUNTIME = frozenset({"RUNNING", "PAUSED"})

UNAVAILABLE_VOTE_PREFIXES = ("Local model unavailable", "No model configured")

Judge = Callable[[str, str, list[dict[str, Any]]], dict[str, Any]]
GitReader = Callable[[str], dict[str, Any]]
Runner = Callable[..., RunResult]


class RelayRefused(RuntimeError):
    """A browser or control call that is refused without changing state."""


def default_judge(wcfg: dict[str, Any]) -> Judge:
    """Prototype watchdog (deterministic + Ollama voters), SQLite-fed."""

    def judge(project_name: str, root: str, cycles: list[dict[str, Any]]) -> dict[str, Any]:
        if not wcfg.get("enabled", True):
            return {"status": "CONTINUE", "reason": "Watchdog disabled.", "votes": [], "progress": False}
        project = core.Project(project_name, Path(root))
        deterministic = core.deterministic_vote(cycles, wcfg)
        votes = [deterministic]
        url = str(wcfg.get("ollama_url", "http://127.0.0.1:11434"))
        for voter in wcfg.get("voters", []):
            if voter.get("enabled", True):
                votes.append(core.ollama_vote(project, cycles, voter, url))
        # Voters that could not run (no Ollama, missing model) do not vote.
        available = [v for v in votes if not v.reason.startswith(UNAVAILABLE_VOTE_PREFIXES)]
        if len(available) == 1:
            status = {"LOOP": "LOOP", "PROGRESS": "CONTINUE"}.get(deterministic.verdict, "CONTINUE")
            reason = f"Only the deterministic judge is available: {deterministic.reason}"
            progress = deterministic.verdict == "PROGRESS"
        else:
            decision = core.aggregate_votes(available)
            status = {"HUMAN_REQUIRED": "LOOP", "UNCERTAIN": "UNCERTAIN"}.get(decision.status, "CONTINUE")
            reason = decision.reason
            progress = sum(v.verdict == "PROGRESS" for v in available) >= 2
        return {"status": status, "reason": reason, "votes": [asdict(v) for v in votes], "progress": progress}

    return judge


def default_git(root: str) -> dict[str, Any]:
    return core.git_metadata(core.Project("relay", Path(root)))


class RelayEngine:
    def __init__(
        self,
        db: RelayDatabase,
        *,
        config: dict[str, Any] | None = None,
        judge: Judge | None = None,
        runner: Runner = run_bash,
        git: GitReader = default_git,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.db = db
        self.lock = threading.RLock()
        self.cfg = relay_config(config)
        wcfg = watchdog_config(config)
        self.history_window = max(int(wcfg.get("history_window", 4)), 2)
        self.judge = judge or default_judge(wcfg)
        self.runner = runner
        self.git = git
        self.clock = clock
        self._lease_seen: dict[str, float] = {}
        self._tab_seen: dict[tuple[str, str], float] = {}  # (site, project or "*") -> last heard
        self._started = clock()
        self.tab_silent_seconds = float(self.cfg.get("tab_silent_seconds") or TAB_SILENT_SECONDS)
        self._executing: set[str] = set()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        # Set by prelayd; consulted for quiet hours before handing out a send.
        self.supervisor: Any = None

    # ================================================================ control

    def start(
        self,
        *,
        name: str,
        root: str,
        branch: str | None = None,
        conversation_url: str | None = None,
        new_chat: bool = False,
        seed: str | None = None,
        max_cycles: int | None = None,
        mode: str = "solo",
        goal: str | None = None,
        rules: str = "",
        review_policy: str | None = None,
        pm_conversation_url: str | None = None,
        pm_new_chat: bool = False,
        pace: str | None = None,
        checkin_every: int | None = None,
    ) -> dict[str, Any]:
        if conversation_url and new_chat:
            raise RelayRefused("Use either a conversation URL or --new-chat, not both.")
        if mode not in {"solo", "ade"}:
            raise RelayRefused(f"Unknown mode {mode!r}.")
        pace = (pace or ("milestone" if mode == "ade" else "step")) if mode == "ade" else "step"
        if pace not in ade.PACES:
            raise RelayRefused(f"Pace must be one of {', '.join(ade.PACES)}.")
        review_policy = review_policy or ("push" if pace == "milestone" else "risky")
        checkin_every = max(1, int(checkin_every or self.cfg.get("checkin_every") or 8))
        if review_policy not in ade.REVIEW_POLICIES:
            raise RelayRefused(f"Review policy must be one of {', '.join(ade.REVIEW_POLICIES)}.")
        url = canonical_conversation_url(conversation_url) if conversation_url else None
        if url and site_of(url) != "chatgpt":
            raise RelayRefused("The Worker (and solo) chat must be a chatgpt.com conversation.")
        pm_url = canonical_conversation_url(pm_conversation_url) if pm_conversation_url else None
        if pm_url and site_of(pm_url) != "claude":
            raise RelayRefused("The PM chat must be a claude.ai conversation.")
        if mode == "ade" and not pm_url and not pm_new_chat:
            pm_new_chat = True
        git_now = self.git(root) if mode == "ade" else {}

        with self.lock, self.db.transaction() as conn:
            project = store.ensure_project(conn, name=name, root=root, branch=branch)
            pid = project["id"]
            rt = store.runtime(conn, pid)
            if rt and rt["status"] in ACTIVE_RUNTIME:
                raise RelayRefused(f"{name} is already {rt['status']}. Stop it first.")

            active = store.active_conversation(conn, pid)
            if new_chat or (url and active and active["conversation_url"] != url):
                if active:
                    store.retire_conversation(conn, active["id"], "NEW_CHAT" if new_chat else "SWITCHED")
                active = None
            if active is None:
                if url is None and not new_chat and project["conversation_url"]:
                    url = canonical_conversation_url(project["conversation_url"])
                active = store.create_conversation(conn, project_id=pid, url=url, predecessor_id=None)

            session_id = store.create_session(conn, pid)
            store.put_runtime(conn, project_id=pid, session_id=session_id,
                              max_cycles=int(max_cycles or self.cfg["max_cycles"]),
                              mode=mode, goal=goal or seed, review_policy=review_policy,
                              pace=pace, checkin_every=checkin_every)
            pm_conv = None
            if mode == "ade":
                pm_conv = store.active_conversation(conn, pid, "pm")
                if pm_new_chat or (pm_url and pm_conv and pm_conv["conversation_url"] != pm_url):
                    if pm_conv:
                        store.retire_conversation(conn, pm_conv["id"], "NEW_CHAT" if pm_new_chat else "SWITCHED")
                    pm_conv = None
                if pm_conv is None:
                    pm_conv = store.create_conversation(conn, project_id=pid, url=pm_url, predecessor_id=None,
                                                        role="pm", site="claude")
                kickoff, told = self._with_human(conn, pid, ade.pm_kickoff(
                    project=name, root=root, goal=goal or seed or "", rules=rules, git=git_now,
                    memory=memory.render(conn, pid), pace=pace))
                request_id = store.create_request(
                    conn, session_id=session_id, conversation_id=pm_conv["id"], kind="PM_PLAN", role="pm",
                    prompt=kickoff, model=None,
                )
                self._delivered(conn, pid, request_id, told)
            else:
                remembered = memory.render(conn, pid)
                seed_text = (seed or prompts.DEFAULT_SEED) + (f"\n\n{remembered}" if remembered else "")
                seed_prompt, told = self._with_human(conn, pid, prompts.with_protocol(seed_text))
                request_id = store.create_request(
                    conn, session_id=session_id, conversation_id=active["id"], kind="SEED",
                    prompt=seed_prompt, model=self._model_label(conn, pid) or None,
                )
                self._delivered(conn, pid, request_id, told)
        self._wake.set()
        return {"project": name, "session_id": session_id, "request_id": request_id, "mode": mode,
                "pace": pace, "review_policy": review_policy, "checkin_every": checkin_every,
                "conversation_url": active["conversation_url"],
                "pm_conversation_url": pm_conv["conversation_url"] if pm_conv else None}

    def pause(self, name: str, reason: str = "Paused by user.") -> None:
        self._set_status(name, {"RUNNING"}, "PAUSED", reason)

    def resume(self, name: str, message: str | None = None) -> dict[str, Any]:
        with self.lock, self.db.transaction() as conn:
            pid, rt = self._runtime_for(conn, name)
            last = store.latest_request(conn, rt["session_id"])
            settled = last is None or last["state"] in {"COMPLETED", "HUMAN_REQUIRED", "FAILED",
                                                        "CANCELLED", "RECOVERY_REQUIRED"}
            if rt["status"] == "PAUSED" and not message:
                store.update_runtime(conn, pid, status="RUNNING", reason=None)
                self._wake.set()
                return {"resumed": True}
            if rt["status"] not in {"PAUSED", "HUMAN_REQUIRED", "STOPPED", "FINISHED"}:
                raise RelayRefused(f"{name} is {rt['status']}; nothing to resume.")
            if not settled:
                raise RelayRefused(
                    f"Latest request is still {last['state']}; a message can only be added between "
                    "requests. Resume without a message, or stop the run and start it again with a seed.")
            role = "pm" if rt["mode"] == "ade" else "worker"
            conv = store.active_conversation(conn, pid, role)
            if conv is None:
                raise RelayRefused("No active conversation; use start.")
            text = message or "The human reviewed the situation. Continue from the latest evidence."
            prompt = (text.rstrip() + "\n\n" + ade.pm_protocol(rt["pace"]) + "\n" if role == "pm"
                      else prompts.with_protocol(text))
            kind = "USER"
            # A chat that never received anything (its first message was not
            # persisted) has no context: send that first message again, with
            # the human's note, instead of a bare note.
            opener = conn.execute(
                "SELECT kind, prompt_text FROM requests WHERE conversation_id = ? ORDER BY sequence_number LIMIT 1",
                (conv["id"],)).fetchone()
            if conv["conversation_url"] is None and not conv["char_count"] and opener is not None:
                kind = opener["kind"]
                prompt = opener["prompt_text"] + (f"\n\nNote from the human: {message.strip()}\n" if message else "")
            prompt, told = self._with_human(conn, pid, prompt)
            request_id = store.create_request(
                conn, session_id=rt["session_id"], conversation_id=conv["id"], kind=kind, role=role,
                prompt=prompt, model=None if role == "pm" else self._model_label(conn, pid) or None,
            )
            self._delivered(conn, pid, request_id, told)
            if last and last["successor_request_id"] is None:
                store.set_request_fields(conn, last["id"], successor_request_id=request_id)
            store.update_runtime(conn, pid, status="RUNNING", reason=None)
        self._wake.set()
        return {"resumed": True, "request_id": request_id}

    def stop(self, name: str) -> None:
        with self.lock, self.db.transaction() as conn:
            pid, rt = self._runtime_for(conn, name)
            last = store.latest_request(conn, rt["session_id"])
            if last and last["state"] in {"QUEUED", "PREPARING_BROWSER", "READY_TO_SUBMIT"}:
                transition_request_in(conn, request_id=last["id"], to_state="CANCELLED",
                                      payload={"reason": "stopped by user"})
            store.update_runtime(conn, pid, status="STOPPED", reason="Stopped by user.")

    def tell(self, name: str, text: str, remember: bool = False) -> dict[str, Any]:
        """Queue a message for the planner (the PM, or ChatGPT in solo mode): it goes at the top
        of the planner's next message, without pausing the run. remember=True also keeps it
        in project memory, so every future chat gets it."""
        text = (text or "").strip()
        if not text:
            raise RelayRefused("Nothing to tell.")
        with self.lock, self.db.transaction() as conn:
            pid, rt = self._runtime_for(conn, name)
            store.event(conn, project_id=pid, event_type="HUMAN_MESSAGE",
                        payload={"text": text[:4000], "remember": bool(remember)})
            if remember:
                memory.add_note(conn, pid, text, source="user")
            # The planner's next message is queued but not typed yet: replace it with
            # the same message plus this one (prompts are never edited in place).
            last = store.latest_request(conn, rt["session_id"]) if rt["session_id"] else None
            planner = "pm" if rt["mode"] == "ade" else "worker"
            now = bool(last and last["state"] == "QUEUED" and last["role"] == planner and last["kind"] != "HANDOFF")
            if now:
                prompt, told = self._with_human(conn, pid, last["prompt_text"])
                transition_request_in(conn, request_id=last["id"], to_state="CANCELLED",
                                      payload={"reason": "replaced to add a message from the human"})
                replacement = store.create_request(
                    conn, session_id=rt["session_id"], conversation_id=last["conversation_id"], kind=last["kind"],
                    prompt=prompt, model=last["model"], role=last["role"], detail=last["detail"])
                store.set_request_fields(conn, last["id"], successor_request_id=replacement)
                self._delivered(conn, pid, replacement, told)
        self._wake.set()
        return {"queued": True, "remembered": bool(remember), "in_next_message_now": now}

    def set_pace(self, name: str, pace: str, checkin_every: int | None = None) -> dict[str, Any]:
        """Switch a run's ADE pace in place. The PM is told with its next message; a RELAY_TASK it
        still sends under milestone pace is taken as an assignment."""
        if pace not in ade.PACES:
            raise RelayRefused(f"Pace must be one of {', '.join(ade.PACES)}.")
        with self.lock, self.db.transaction() as conn:
            pid, rt = self._runtime_for(conn, name)
            if rt["mode"] != "ade":
                raise RelayRefused(f"{name} is a solo run; pace applies to Relay ADE runs.")
            every = max(1, int(checkin_every or rt["checkin_every"] or 8))
            changed = rt["pace"] != pace or rt["checkin_every"] != every
            store.update_runtime(conn, pid, pace=pace, checkin_every=every)
            store.event(conn, project_id=pid, event_type="PACE_CHANGED", payload={"pace": pace, "checkin_every": every})
        if changed and pace != rt["pace"]:
            note = (f"Relay switched this run to milestone pace. From now on give the Worker a whole milestone with "
                    f"{ade.ASSIGN_START} … {ade.ASSIGN_END} (what to achieve, done-when criteria). The Worker works "
                    f"through it on its own; you are checked in when it is done, blocked or looping, or every "
                    f"{every} commands. At a check-in you may also reply {ade.CONTINUE}: <guidance>."
                    if pace == "milestone" else
                    "Relay switched this run to step pace: give the Worker one small task at a time with "
                    f"{ade.TASK_START} … {ade.TASK_END}; you see every result.")
            self.tell(name, note)
        return {"pace": pace, "checkin_every": every}

    @staticmethod
    def _pending_human(conn, project_id: str) -> list[tuple[int, str]]:
        delivered = conn.execute("SELECT COALESCE(MAX(id), 0) FROM events WHERE project_id = ? "
                                 "AND event_type = 'HUMAN_MESSAGE_DELIVERED'", (project_id,)).fetchone()[0]
        return [(row[0], json.loads(row[1])["text"]) for row in conn.execute(
            "SELECT id, payload_json FROM events WHERE project_id = ? AND event_type = 'HUMAN_MESSAGE' "
            "AND id > ? ORDER BY id", (project_id, delivered))]

    def _with_human(self, conn, project_id: str, prompt: str) -> tuple[str, list[int]]:
        """Put waiting human messages at the top of a planner prompt."""
        pending = self._pending_human(conn, project_id)
        if not pending:
            return prompt, []
        block = "\n".join(f"- {text}" for _, text in pending)
        return (f"Message from the human (read this first, then continue):\n{block}\n\n{prompt}",
                [event_id for event_id, _ in pending])

    def _delivered(self, conn, project_id: str, request_id: str, ids: list[int]) -> None:
        if ids:
            store.event(conn, project_id=project_id, request_id=request_id,
                        event_type="HUMAN_MESSAGE_DELIVERED", payload={"messages": ids})

    def status(self) -> list[dict[str, Any]]:
        with self.lock:
            conn = self.db.conn
            result = []
            for rt in store.all_runtimes(conn):
                last = store.latest_request(conn, rt["session_id"]) if rt["session_id"] else None

                def conv_info(role: str) -> dict[str, Any] | None:
                    conv = store.active_conversation(conn, rt["project_id"], role)
                    if conv is None:
                        return None
                    ordinal = conn.execute(
                        "SELECT COUNT(*) FROM conversations WHERE project_id = ? AND role = ? AND sequence_number <= ?",
                        (rt["project_id"], role, conv["sequence_number"])).fetchone()[0]
                    return {
                        "url": conv["conversation_url"], "chat_number": ordinal,
                        "char_count": conv["char_count"], "site": conv["site"],
                        "budget": self.cfg["rollover_char_budget"]}

                execution = conn.execute(
                    """SELECT e.command_text, e.return_code, e.completed_at, e.started_at FROM executions e
                       JOIN requests r ON r.id = e.request_id WHERE r.session_id = ?
                       ORDER BY e.started_at DESC LIMIT 1""", (rt["session_id"],)).fetchone()
                result.append({
                    "kpi": kpi.run_kpis(conn, rt["project_id"], rt["session_id"]),
                    "plan": memory.plan(conn, rt["project_id"]),
                    "notes": len(memory.active_notes(conn, rt["project_id"])),
                    "project": rt["project_name"],
                    "root": rt["repository_root"],
                    "status": rt["status"],
                    "reason": rt["reason"],
                    "mode": rt["mode"],
                    "goal": rt["goal"],
                    "review_policy": rt["review_policy"],
                    "missing_tab": self.missing_tab(rt, last),
                    "pace": rt["pace"],
                    "checkin_every": rt["checkin_every"],
                    "assignment": self._assignment(conn, rt) if rt["mode"] == "ade" and rt["session_id"] else "",
                    "since_checkin": (len(self._commands_since_pm(conn, rt))
                                      if rt["mode"] == "ade" and rt["session_id"] else 0),
                    "pending_human": [text for _, text in self._pending_human(conn, rt["project_id"])],
                    "model_mode": rt["model_mode"],
                    "cycle_count": rt["cycle_count"],
                    "max_cycles": rt["max_cycles"],
                    "conversation": conv_info("worker"),
                    "pm_conversation": conv_info("pm") if rt["mode"] == "ade" else None,
                    "request": last and {
                        "id": last["id"], "state": last["state"], "kind": last["kind"], "role": last["role"],
                        "model": last["model"], "detail": last["detail"]},
                    "last_execution": execution and {
                        "command_head": "\n".join((execution["command_text"] or "").splitlines()[:12]),
                        "return_code": execution["return_code"], "started_at": execution["started_at"],
                        "completed_at": execution["completed_at"]},
                })
            return result

    def _set_status(self, name: str, allowed: set[str], status: str, reason: str) -> None:
        with self.lock, self.db.transaction() as conn:
            pid, rt = self._runtime_for(conn, name)
            if rt["status"] not in allowed:
                raise RelayRefused(f"{name} is {rt['status']}.")
            store.update_runtime(conn, pid, status=status, reason=reason)

    def _runtime_for(self, conn, name: str) -> tuple[str, dict[str, Any]]:
        project = store.project_by_name(conn, name)
        rt = store.runtime(conn, project["id"]) if project else None
        if rt is None:
            raise RelayRefused(f"{name} has never been started.")
        return project["id"], rt

    def _model_label(self, conn, project_id: str) -> str:
        rt = store.runtime(conn, project_id)
        models = self.cfg["models"]
        if rt and rt["model_mode"] == "ESCALATED":
            return models.get("strong_label") or ""
        return models.get("default_label") or ""

    # ================================================================ browser

    def _browser_request(self, conn, project: str | None, role: str) -> tuple[dict, dict] | None:
        """Latest request of a RUNNING runtime that needs a browser tab of this role."""
        for rt in store.all_runtimes(conn):
            if project and rt["project_name"] != project:
                continue
            if rt["status"] != "RUNNING" or not rt["session_id"]:
                continue
            req = store.latest_request(conn, rt["session_id"])
            if req and req["state"] in BROWSER_STATES and req["role"] == role:
                return rt, req
        return None

    def _lease_fresh(self, lease: str | None) -> bool:
        seen = self._lease_seen.get(lease or "")
        return seen is not None and self.clock() - seen < LEASE_TTL_SECONDS

    def _heard(self, lease: str, page_url: str | None, project: str | None) -> None:
        now = self.clock()
        self._lease_seen[lease] = now
        self._tab_seen[(site_of(page_url) or "chatgpt", project or "*")] = now

    def alive(self, *, lease: str, page_url: str | None = None, project: str | None = None) -> dict[str, Any]:
        """Heartbeat from a Relay tab, sent even while it is busy with a long step."""
        if not lease:
            raise RelayRefused("lease required")
        with self.lock:
            self._heard(lease, page_url, project)
        return {"ok": True}

    def missing_tab(self, rt: dict[str, Any], req: dict[str, Any] | None) -> dict[str, Any] | None:
        """The tab a running step is waiting for, when no tab that could serve it is connected."""
        if rt["status"] != "RUNNING" or not req or req["state"] not in BROWSER_STATES:
            return None
        now = self.clock()
        if now - self._started < self.tab_silent_seconds:
            return None
        site = ROLE_SITE.get(req["role"], "chatgpt")
        heard = [t for (tab_site, project), t in self._tab_seen.items()
                 if tab_site == site and project in {rt["project_name"], "*"}]
        if req["browser_lease"] in self._lease_seen:
            heard.append(self._lease_seen[req["browser_lease"]])
        last = max(heard, default=None)
        if last is not None and now - last < self.tab_silent_seconds:
            return None
        return {"role": req["role"], "site": site,
                "silent_seconds": None if last is None else int(now - last)}

    def poll(self, *, lease: str, page_url: str | None = None, project: str | None = None) -> dict[str, Any]:
        if not lease:
            raise RelayRefused("lease required")
        with self.lock:
            self._heard(lease, page_url, project)
            role = "pm" if site_of(page_url) == "claude" else "worker"
            with self.db.transaction() as conn:
                found = self._browser_request(conn, project, role)
                if found is None:
                    return {"type": "idle", "role": role, "runtimes": self._runtime_summary(conn)}
                rt, req = found
                owner = req["browser_lease"]
                if owner and owner != lease and self._lease_fresh(owner):
                    return {"type": "idle", "role": role, "reason": "Another tab owns this request."}
                if (req["state"] in {"QUEUED", "PREPARING_BROWSER"} and self.supervisor is not None
                        and self.supervisor.is_quiet()):
                    return {"type": "idle", "role": role,
                            "reason": "Quiet hours: Relay sends nothing new until they end."}
                if req["state"] == "QUEUED":
                    transition_request_in(conn, request_id=req["id"], to_state="PREPARING_BROWSER",
                                          updates={"browser_lease": lease})
                    req = store.get_request(conn, req["id"])
                elif owner != lease:
                    store.set_request_fields(conn, req["id"], browser_lease=lease)
                    store.event(conn, project_id=rt["project_id"], request_id=req["id"],
                                event_type="BROWSER_LEASE_TAKEN", payload={"from": owner, "to": lease})
                conv = store.get_conversation(conn, req["conversation_id"])
                job = {
                    "request_id": req["id"],
                    "project": rt["project_name"],
                    "role": req["role"],
                    "state": req["state"],
                    "kind": req["kind"],
                    "conversation_url": conv["conversation_url"],
                    "model_label": req["model"] or "",
                }
                if req["state"] in OBSERVE_STATES:
                    baseline = json.loads(req["baseline_json"] or "{}")
                    job.update(type="observe", baseline=baseline.get("ids", []),
                               user_turn_id=req["user_turn_id"],
                               assistant_turn_id=req["assistant_turn_id"])
                else:
                    job.update(type="submit", prompt=req["prompt_text"])
                if conv["site"] == "claude":
                    # claude.ai shows only the tail of a long chat, so Claude turns are
                    # found by content: the extension matches this text, not a position.
                    job["match_text"] = ade.match_text(req["prompt_text"])
                return job

    def _runtime_summary(self, conn) -> list[dict[str, Any]]:
        return [{"project": r["project_name"], "status": r["status"], "reason": r["reason"], "mode": r["mode"]}
                for r in store.all_runtimes(conn)]

    def _owned(self, conn, lease: str, request_id: str, states: set[str]) -> dict[str, Any]:
        self._lease_seen[lease] = self.clock()
        req = store.get_request(conn, request_id)
        if req["browser_lease"] != lease:
            raise RelayRefused("This tab does not own the request.")
        if req["state"] not in states:
            raise RelayRefused(f"Request is {req['state']}.")
        return req

    def _check_page(self, conn, req: dict[str, Any], page_url: str | None) -> dict[str, Any]:
        conv = store.get_conversation(conn, req["conversation_id"])
        if conv["status"] != "ACTIVE":
            raise RelayRefused("Conversation was retired.")
        target = conv["conversation_url"]
        if site_of(page_url) != conv["site"]:
            raise RelayRefused(f"This request belongs on {conv['site']}.")
        if target is None:
            if not is_new_chat_page(page_url, conv["site"]):
                raise RelayRefused("Expected a new-chat page.")
        elif conversation_id(page_url) != conversation_id(target):
            raise RelayRefused("Tab is not on the bound conversation.")
        return conv

    def ready(self, *, lease: str, request_id: str, page_url: str, baseline: list[str]) -> dict[str, Any]:
        """The only path to SUBMITTING. True at most once per request."""
        with self.lock, self.db.transaction() as conn:
            try:
                req = self._owned(conn, lease, request_id, {"PREPARING_BROWSER"})
                rt = store.runtime(conn, req["project_id"])
                if rt is None or rt["status"] != "RUNNING":
                    raise RelayRefused("Relay is not running.")
                self._check_page(conn, req, page_url)
            except RelayRefused as exc:
                return {"send": False, "reason": str(exc)}
            ids = sorted({str(i) for i in baseline if isinstance(i, str) and i})
            detail = json.dumps({"ids": ids, "page_url": page_url})
            transition_request_in(conn, request_id=request_id, to_state="READY_TO_SUBMIT",
                                  updates={"baseline_json": detail})
            transition_request_in(conn, request_id=request_id, to_state="SUBMITTING",
                                  payload={"baseline_count": len(ids)})
            return {"send": True}

    def accepted(self, *, lease: str, request_id: str, user_turn_id: str, page_url: str) -> dict[str, Any]:
        with self.lock, self.db.transaction() as conn:
            req = store.get_request(conn, request_id)
            if req["state"] != "SUBMITTING":
                if req["user_turn_id"] == user_turn_id:
                    return {"ok": True, "replay": True}
                raise RelayRefused(f"Request is {req['state']}.")
            req = self._owned(conn, lease, request_id, {"SUBMITTING"})
            turn = (user_turn_id or "").strip()
            baseline = set(json.loads(req["baseline_json"] or "{}").get("ids", []))
            if not turn or turn == PROVISIONAL_USER_TURN:
                raise RelayRefused("Provisional or empty user turn is not a durable identity.")
            if turn in baseline:
                raise RelayRefused("User turn existed before Send.")
            if conn.execute("SELECT 1 FROM requests WHERE user_turn_id = ? AND id <> ?",
                            (turn, request_id)).fetchone():
                raise RelayRefused("User turn is already bound to another request.")
            conv = store.get_conversation(conn, req["conversation_id"])
            page_cid = conversation_id(page_url)
            if site_of(page_url) != conv["site"]:
                raise RelayRefused("Accepted turn is on the wrong site.")
            if conv["conversation_url"] is None:
                if page_cid is None:
                    raise RelayRefused("New chat has no conversation URL yet.")
                store.set_conversation_url(conn, conv["id"], canonical_conversation_url(page_url))
            elif page_cid != conversation_id(conv["conversation_url"]):
                raise RelayRefused("Accepted turn is on a different conversation.")
            transition_request_in(conn, request_id=request_id, to_state="PROMPT_ACCEPTED",
                                  updates={"user_turn_id": turn})
            transition_request_in(conn, request_id=request_id, to_state="WAITING_ASSISTANT")
            store.add_conversation_chars(conn, conv["id"], len(req["prompt_text"]))
            return {"ok": True}

    def bound(self, *, lease: str, request_id: str, assistant_turn_id: str) -> dict[str, Any]:
        with self.lock, self.db.transaction() as conn:
            req = store.get_request(conn, request_id)
            if req["assistant_turn_id"] and req["assistant_turn_id"] == assistant_turn_id:
                return {"ok": True, "replay": True}
            req = self._owned(conn, lease, request_id, {"WAITING_ASSISTANT"})
            self._bind_assistant(conn, req, assistant_turn_id)
            return {"ok": True}

    def _bind_assistant(self, conn, req: dict[str, Any], assistant_turn_id: str) -> None:
        turn = (assistant_turn_id or "").strip()
        user = req["user_turn_id"] or ""
        if user.startswith("group:user:"):
            if turn != "group:assistant:" + user[len("group:user:"):]:
                raise RelayRefused("Assistant group does not match the accepted user group.")
        elif user.startswith("claude:user:"):
            # claude:user:<index>:<hash> pairs with claude:assistant:<index>
            index = user.split(":")[2]
            if turn != f"claude:assistant:{index}":
                raise RelayRefused("Assistant turn does not follow the accepted user turn.")
        elif not turn or turn == user:
            raise RelayRefused("Invalid assistant turn identity.")
        if turn in set(json.loads(req["baseline_json"] or "{}").get("ids", [])) and not user.startswith("group:"):
            raise RelayRefused("Assistant turn existed before Send.")
        transition_request_in(conn, request_id=req["id"], to_state="ASSISTANT_BOUND",
                              updates={"assistant_turn_id": turn})

    def complete(self, *, lease: str, request_id: str, assistant_turn_id: str, text: str) -> dict[str, Any]:
        text = (text or "").strip()
        if not text:
            raise RelayRefused("Assistant text is empty.")
        digest = sha256_text(text)
        with self.lock, self.db.transaction() as conn:
            req = store.get_request(conn, request_id)
            if req["assistant_text_sha256"]:
                if req["assistant_text_sha256"] == digest:
                    return {"ok": True, "replay": True}
                raise RelayRefused("A different reply was already recorded.")
            req = self._owned(conn, lease, request_id, {"WAITING_ASSISTANT", "ASSISTANT_BOUND"})
            if req["state"] == "WAITING_ASSISTANT":
                self._bind_assistant(conn, req, assistant_turn_id)
            elif req["assistant_turn_id"] != assistant_turn_id:
                raise RelayRefused("Reply belongs to a different assistant turn.")
            transition_request_in(conn, request_id=request_id, to_state="ASSISTANT_COMPLETE",
                                  updates={"assistant_text": text, "assistant_text_sha256": digest},
                                  payload={"chars": len(text)})
            store.add_conversation_chars(conn, req["conversation_id"], len(text))
        self._wake.set()
        return {"ok": True}

    DIAG_LIMIT_PER_REQUEST = 40

    def diag(self, *, lease: str, request_id: str, stage: str, probe: dict[str, Any]) -> dict[str, Any]:
        """Store a structural DOM probe from the extension (bounded per request)."""
        with self.lock, self.db.transaction() as conn:
            self._lease_seen[lease] = self.clock()
            req = store.get_request(conn, request_id)
            if req["browser_lease"] != lease:
                raise RelayRefused("This tab does not own the request.")
            count = conn.execute(
                "SELECT COUNT(*) FROM events WHERE request_id = ? AND event_type = 'BROWSER_DIAG'",
                (request_id,)).fetchone()[0]
            if count >= self.DIAG_LIMIT_PER_REQUEST:
                return {"ok": True, "stored": False}
            raw = json.dumps(probe)
            stored = probe if len(raw) <= 20000 else {"truncated": raw[:20000]}
            store.event(conn, project_id=req["project_id"], request_id=request_id,
                        event_type="BROWSER_DIAG",
                        payload={"stage": str(stage)[:40], "state": req["state"], "probe": stored})
            return {"ok": True, "stored": True}

    PRE_SEND_PAUSE_CODES = frozenset({"AUTH_REQUIRED", "PAGE_BROKEN", "COMPOSER_NOT_EMPTY"})

    def failure(self, *, lease: str, request_id: str, code: str, message: str = "",
                evidence: dict[str, Any] | None = None) -> dict[str, Any]:
        code = (code or "UNKNOWN").upper()
        with self.lock, self.db.transaction() as conn:
            req = store.get_request(conn, request_id)
            if req["browser_lease"] != lease:
                raise RelayRefused("This tab does not own the request.")
            state = req["state"]
            info = {"code": code, "message": message[:2000], "evidence": evidence or {}}
            store.event(conn, project_id=req["project_id"], request_id=request_id,
                        event_type="BROWSER_FAILURE", payload={"state": state, **info})

            if state == "PREPARING_BROWSER":
                if code == "MODEL_UNAVAILABLE":
                    store.set_request_fields(conn, request_id, model=None)
                    store.update_runtime(conn, req["project_id"], model_mode="DEFAULT", progress_streak=0)
                    return {"ok": True, "action": "retry_without_model"}
                if code == "CONVERSATION_LIMIT":
                    self._to_recovery(conn, req, info)
                    return {"ok": True, "action": "rollover"}
                if code == "CONVERSATION_NOT_FOUND":
                    self._to_recovery(conn, req, info)
                    return {"ok": True, "action": "human"}
                store.update_runtime(conn, req["project_id"], status="PAUSED",
                                     reason=f"Browser: {code}: {message[:300]}")
                return {"ok": True, "action": "paused"}

            if state == "SUBMITTING":
                if code in {"NOT_PERSISTED", "AMBIGUOUS_SUBMISSION", "CONVERSATION_LIMIT"}:
                    self._to_recovery(conn, req, info)
                    return {"ok": True, "action": "recovery"}
                raise RelayRefused(f"{code} is not a valid SUBMITTING outcome.")

            if state in {"PROMPT_ACCEPTED", "WAITING_ASSISTANT", "ASSISTANT_BOUND"}:
                if code in {"REPLY_FAILED", "REPLY_TIMEOUT", "CONVERSATION_LIMIT"}:
                    self._to_recovery(conn, req, info)
                    return {"ok": True, "action": "recovery"}
                raise RelayRefused(f"{code} is not a valid reply outcome.")

            raise RelayRefused(f"Request is {state}.")

    def _to_recovery(self, conn, req: dict[str, Any], info: dict[str, Any]) -> None:
        info = info | {"original_detail": req["detail"]}
        transition_request_in(conn, request_id=req["id"], to_state="RECOVERY_REQUIRED",
                              updates={"detail": json.dumps(info)}, payload={"code": info["code"]})
        self._wake.set()

    # ================================================================ engine

    def run_forever(self) -> None:
        while not self._stop.is_set():
            try:
                progressed = self.step()
            except Exception as exc:  # keep the daemon alive; surface in status
                self._record_engine_error(exc)
                progressed = False
            if not progressed:
                self._wake.wait(0.5)
                self._wake.clear()

    def start_background(self) -> None:
        self._thread = threading.Thread(target=self.run_forever, name="relay-engine", daemon=True)
        self._thread.start()

    def shutdown(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _record_engine_error(self, exc: Exception) -> None:
        with self.lock, self.db.transaction() as conn:
            for rt in store.all_runtimes(conn):
                if rt["status"] == "RUNNING":
                    store.update_runtime(conn, rt["project_id"], status="PAUSED",
                                         reason=f"Engine error: {type(exc).__name__}: {exc}"[:500])

    def step(self) -> bool:
        """Advance at most one request by one stage. True if anything changed."""
        with self.lock:
            work = None
            for rt in store.all_runtimes(self.db.conn):
                if rt["status"] != "RUNNING" or not rt["session_id"]:
                    continue
                req = store.latest_request(self.db.conn, rt["session_id"])
                if req is None:
                    continue
                handler = {
                    "ASSISTANT_COMPLETE": self._on_reply,
                    "COMMAND_VALIDATED": self._on_validated,
                    "RUNNING_CLI": self._on_running_cli,
                    "CLI_COMPLETE": self._on_cli_complete,
                    "RUNNING_WATCHDOG": self._on_cli_complete,
                    "CONTINUE_READY": self._on_continue_ready,
                    "RECOVERY_REQUIRED": self._on_recovery,
                }.get(req["state"])
                if handler is None:
                    continue
                if req["state"] == "RECOVERY_REQUIRED" and req["successor_request_id"]:
                    continue
                if req["state"] == "RUNNING_CLI" and req["id"] in self._executing:
                    continue
                work = (handler, rt, req)
                break
        if work is None:
            return False
        handler, rt, req = work
        handler(rt, req)
        return True

    # -- helpers

    def _root(self, rt: dict[str, Any]) -> str:
        return rt["repository_root"]

    def _human(self, conn, rt: dict[str, Any], req: dict[str, Any], reason: str, report: str = "") -> None:
        transition_request_in(conn, request_id=req["id"], to_state="HUMAN_REQUIRED",
                              updates={"detail": json.dumps({"reason": reason, "report": report[:20000]})})
        store.update_runtime(conn, rt["project_id"], status="HUMAN_REQUIRED", reason=reason[:500])

    def _queue(self, conn, rt: dict[str, Any], *, prompt: str, kind: str,
               model: str | None = None, role: str = "worker", detail: str | None = None) -> str:
        """Create the successor request, inserting a rollover handoff when over budget."""
        conv = store.active_conversation(conn, rt["project_id"], role)
        if conv is None:
            raise StateError(f"No active {role} conversation.")
        planner = "pm" if rt["mode"] == "ade" else "worker"
        told: list[int] = []
        if role == planner and kind != "HANDOFF":
            prompt, told = self._with_human(conn, rt["project_id"], prompt)
        if role == "pm":
            label = ""
        else:
            label = model if model is not None else self._model_label(conn, rt["project_id"])
        budget = int(self.cfg["rollover_char_budget"])
        if (kind not in {"HANDOFF", "ROLLOVER_SEED"} and conv["conversation_url"]
                and conv["char_count"] + len(prompt) > budget):
            request_id = store.create_request(
                conn, session_id=rt["session_id"], conversation_id=conv["id"], kind="HANDOFF", role=role,
                prompt=prompts.handoff_request(), model=label or None,
                detail=json.dumps({"pending_prompt": prompt, "pending_kind": kind,
                                   "pending_model": label, "pending_detail": detail}),
            )
        else:
            request_id = store.create_request(conn, session_id=rt["session_id"], conversation_id=conv["id"],
                                              kind=kind, prompt=prompt, model=label or None, role=role,
                                              detail=detail)
        self._delivered(conn, rt["project_id"], request_id, told)
        return request_id

    def _finish_with(self, conn, rt, req, *, prompt: str, kind: str, model: str | None = None,
                     from_state: str = "ASSISTANT_COMPLETE", detail: str | None = None,
                     role: str = "worker", successor_detail: str | None = None) -> str:
        successor = self._queue(conn, rt, prompt=prompt, kind=kind, model=model, role=role,
                                detail=successor_detail)
        updates = {"successor_request_id": successor}
        if detail is not None:
            updates["detail"] = detail
        transition_request_in(conn, request_id=req["id"], to_state="COMPLETED",
                              expected_from=from_state, updates=updates)
        return successor

    def _rollover(self, conn, rt, *, reason: str, handoff: str, pending_prompt: str,
                  pending_model: str | None, role: str = "worker",
                  pending_detail: str | None = None) -> str:
        old = store.active_conversation(conn, rt["project_id"], role)
        store.retire_conversation(conn, old["id"], reason)
        new = store.create_conversation(conn, project_id=rt["project_id"], url=None, predecessor_id=old["id"],
                                        role=role, site=old["site"])
        prompt = prompts.rollover_seed(chat_number=new["sequence_number"], handoff=handoff,
                                       pending_prompt=pending_prompt,
                                       memory=memory.render(conn, rt["project_id"]))
        if role == "pm" and not pending_prompt.strip():
            prompt = prompts.without_protocol(prompt) + "\n\n" + ade.pm_protocol(rt["pace"]) + "\n"
        return store.create_request(
            conn, session_id=rt["session_id"], conversation_id=new["id"], kind="ROLLOVER_SEED", role=role,
            prompt=prompt, model=pending_model or None, detail=pending_detail,
        )

    def _consecutive(self, conn, rt, predicate: Callable[[dict[str, Any]], bool]) -> int:
        count = 0
        for req in store.recent_requests(conn, rt["session_id"], 20):
            if not predicate(req):
                break
            count += 1
        return count

    # -- ASSISTANT_COMPLETE

    def _on_reply(self, rt: dict[str, Any], req: dict[str, Any]) -> None:
        text = req["assistant_text"] or ""
        git_before = None
        blocks = extract_shell_blocks(text)
        if (req["kind"] != "HANDOFF" and len(blocks) == 1 and not dangerous_reason(blocks[0])
                and not script_problems(blocks[0])):
            git_before = self.git(self._root(rt))  # outside the DB transaction

        with self.lock, self.db.transaction() as conn:
            rt = store.runtime(conn, rt["project_id"]) | {"repository_root": rt["repository_root"],
                                                           "project_name": rt["project_name"]}
            # Planner replies (the ADE PM, or the solo chat) may carry notes and a plan.
            if req["role"] == "pm" or rt["mode"] != "ade":
                memory.capture(conn, rt["project_id"], text, request_id=req["id"])
            if req["kind"] == "HANDOFF":
                handoff = text
                lines = text.splitlines()
                for i, line in enumerate(lines):
                    if line.strip() == prompts.HANDOFF_MARKER:
                        handoff = "\n".join(lines[i + 1:]).strip() or text
                        break
                pending = store.detail_json(req)
                successor = self._rollover(conn, rt, reason="BUDGET", handoff=handoff,
                                           pending_prompt=pending.get("pending_prompt", ""),
                                           pending_model=pending.get("pending_model"), role=req["role"],
                                           pending_detail=pending.get("pending_detail"))
                transition_request_in(conn, request_id=req["id"], to_state="COMPLETED",
                                      updates={"successor_request_id": successor})
                return

            if req["role"] == "pm":
                self._on_pm_reply(conn, rt, req, text, git_before)
                return

            ade_mode = rt["mode"] == "ade"
            milestone = ade_mode and rt["pace"] == "milestone"
            if milestone and not blocks:
                signal = ade.parse_worker(text)
                if signal.kind in {"CONCERN", "MILESTONE_DONE", "BLOCKED"}:
                    self._checkin(conn, rt, req, signal.kind, worker_message=signal.text or text)
                    return
            if ade_mode and not milestone and not blocks:
                # The Worker declined or explained; the PM decides what happens next.
                self._to_pm(conn, rt, req, ade.pm_evidence(
                    evidence="The Worker did not produce a command. Its reply:\n\n" + prompts.truncate_middle(text, 6000),
                    watchdog_line="(no command ran)", loop=False))
                return

            if prompts.contains_marker(text, prompts.DONE_MARKER) and not blocks:
                transition_request_in(conn, request_id=req["id"], to_state="COMPLETED",
                                      updates={"detail": json.dumps({"done": True})})
                store.update_runtime(conn, rt["project_id"], status="FINISHED",
                                     reason="ChatGPT reported the project goal complete.")
                return

            problem = None
            if len(blocks) != 1:
                problem = ("format", prompts.format_nudge(len(blocks)),
                           f"Reply contained {len(blocks)} bash blocks.")
            else:
                danger = dangerous_reason(blocks[0])
                broken = [] if danger else script_problems(blocks[0])
                if danger:
                    problem = ("danger", prompts.blocked_command_prompt(danger),
                               f"Safety guard blocked: {danger}.")
                elif broken:
                    problem = ("incomplete", prompts.incomplete_script_prompt(broken),
                               "Script not run: " + "; ".join(broken)[:400])
            if problem:
                _, nudge, reason = problem
                used = self._consecutive(conn, rt, lambda r: r["kind"] == "NUDGE")
                task_detail = req["detail"] if ade_mode else None
                if used < int(self.cfg["format_nudges"]):
                    self._finish_with(conn, rt, req, prompt=nudge, kind="NUDGE", successor_detail=task_detail)
                elif milestone:
                    self._checkin(conn, rt, req, "NO_COMMAND",
                                  worker_message=f"Relay did not run the Worker's reply: {reason}\n\n"
                                                 + prompts.truncate_middle(text, 3000))
                elif ade_mode:
                    self._to_pm(conn, rt, req, ade.pm_evidence(
                        evidence=f"Relay did not run the Worker's command: {reason}\n\nWorker reply:\n\n"
                                 + prompts.truncate_middle(text, 6000),
                        watchdog_line="(no command ran)", loop=False))
                else:
                    self._human(conn, rt, req, reason, text)
                return

            if rt["cycle_count"] >= rt["max_cycles"]:
                self._human(conn, rt, req, f"Reached max cycles ({rt['max_cycles']}).", text)
                return

            if ade_mode:
                reasons = ade.review_reasons(blocks[0], rt["review_policy"])
                if reasons:
                    task = store.detail_json(req).get("task", "")
                    review = json.dumps({"command": blocks[0], "task": task, "reasons": reasons,
                                         "worker_request": req["id"]})
                    self._finish_with(conn, rt, req, role="pm", kind="PM_REVIEW", successor_detail=review,
                                      prompt=ade.pm_review(task=task, command=blocks[0], reasons=reasons))
                    return

            store.create_execution(conn, request_id=req["id"], command=blocks[0],
                                   cwd=self._root(rt), git_before=git_before or {})
            transition_request_in(conn, request_id=req["id"], to_state="COMMAND_VALIDATED")

    # -- ADE

    def _to_pm(self, conn, rt, req, prompt: str, from_state: str = "ASSISTANT_COMPLETE") -> str:
        return self._finish_with(conn, rt, req, role="pm", kind="PM_PLAN", prompt=prompt, from_state=from_state)

    def _last_output(self, conn, rt) -> str | None:
        cycles = store.recent_cycles(conn, rt["session_id"], 1)
        return cycles[-1]["terminal_output"] if cycles else None

    def _assignment(self, conn, rt) -> str:
        """The Worker's current milestone (milestone pace): the newest request that carries one."""
        for row in conn.execute("SELECT detail FROM requests WHERE session_id = ? AND detail LIKE '%\"assignment\"%' "
                                "ORDER BY sequence_number DESC LIMIT 20", (rt["session_id"],)):
            try:
                assignment = json.loads(row["detail"] or "{}").get("assignment")
            except ValueError:
                continue
            if assignment:
                return assignment
        return ""

    def _commands_since_pm(self, conn, rt) -> list[dict[str, Any]]:
        """Commands run since the PM's last planning message (reviews do not count)."""
        last_pm = conn.execute("SELECT COALESCE(MAX(sequence_number), 0) FROM requests WHERE session_id = ? "
                               "AND role = 'pm' AND kind != 'PM_REVIEW'", (rt["session_id"],)).fetchone()[0]
        rows = conn.execute(
            """SELECT e.command_text, e.return_code, e.git_before_json, e.git_after_json FROM executions e
               JOIN requests r ON r.id = e.request_id
               WHERE r.session_id = ? AND r.sequence_number > ? AND e.completed_at IS NOT NULL
               ORDER BY e.started_at""", (rt["session_id"], last_pm)).fetchall()
        total = conn.execute("""SELECT COUNT(*) FROM executions e JOIN requests r ON r.id = e.request_id
                                WHERE r.session_id = ? AND e.completed_at IS NOT NULL""",
                             (rt["session_id"],)).fetchone()[0]
        first = total - len(rows) + 1
        return [{"n": first + i, "command": r["command_text"], "return_code": r["return_code"],
                 "git_before": json.loads(r["git_before_json"] or "{}"),
                 "git_after": json.loads(r["git_after_json"] or "{}")} for i, r in enumerate(rows)]

    def _checkin(self, conn, rt, req, reason: str, *, worker_message: str = "", last_evidence: str = "",
                 watchdog_line: str = "", from_state: str = "ASSISTANT_COMPLETE") -> str:
        """Hand the PM a progress report (milestone pace)."""
        commands = self._commands_since_pm(conn, rt)
        opener = conn.execute("SELECT assistant_text FROM requests WHERE session_id = ? AND kind = 'ASSIGN' "
                              "ORDER BY sequence_number DESC LIMIT 1", (rt["session_id"],)).fetchone()
        worker_plan = ""
        if opener and opener["assistant_text"]:
            worker_plan = re.sub(r"`{3}.*?`{3}", "[first command]", opener["assistant_text"], flags=re.S).strip()
        if not last_evidence and commands:
            last_evidence = self._last_output(conn, rt) or ""
        git_now = commands[-1]["git_after"] if commands else {}
        prompt = ade.pm_checkin(
            reason=reason, assignment=self._assignment(conn, rt) or "(none yet)", worker_plan=worker_plan,
            worker_message=worker_message, commands=commands,
            head_before=commands[0]["git_before"].get("head", "") if commands else "",
            git_now=git_now, last_evidence=last_evidence, watchdog_line=watchdog_line,
            checkin_every=rt["checkin_every"])
        store.event(conn, project_id=rt["project_id"], request_id=req["id"], event_type="PM_CHECKIN",
                    payload={"reason": reason, "commands": len(commands)})
        return self._finish_with(conn, rt, req, role="pm", kind="PM_PLAN", prompt=prompt, from_state=from_state,
                                 successor_detail=json.dumps({"checkin": reason}))

    def _pm_nudges_used(self, conn, rt) -> int:
        return self._consecutive(conn, rt, lambda r: r["role"] == "pm" and store.detail_json(r).get("nudge"))

    def _on_pm_reply(self, conn, rt, req, text: str, git_before: dict[str, Any] | None) -> None:
        if req["kind"] == "PM_REVIEW":
            review = store.detail_json(req)
            decision = ade.parse_review(text)
            if decision.kind == "APPROVE":
                # The review request owns the execution: run exactly the reviewed command.
                store.create_execution(conn, request_id=req["id"], command=review["command"],
                                       cwd=self._root(rt), git_before=git_before or self.git(self._root(rt)))
                transition_request_in(conn, request_id=req["id"], to_state="COMMAND_VALIDATED",
                                      payload={"approved_by": "pm"})
                return
            if decision.kind == "REVISE":
                self._finish_with(conn, rt, req, role="worker", kind="REVISE",
                                  prompt=ade.worker_revise(feedback=decision.text, command=review["command"]),
                                  successor_detail=json.dumps({"task": review.get("task", "")}))
                return
            if self._pm_nudges_used(conn, rt) < int(self.cfg["format_nudges"]):
                self._finish_with(conn, rt, req, role="pm", kind="PM_REVIEW", prompt=ade.pm_review_nudge(),
                                  successor_detail=json.dumps(review | {"nudge": True}))
            else:
                self._human(conn, rt, req, "PM reply had no RELAY_APPROVE / RELAY_REVISE.", text)
            return

        decision = ade.parse_plan(text)
        milestone = rt["pace"] == "milestone"
        if milestone and decision.kind in {"ASSIGN", "TASK"}:
            if rt["cycle_count"] >= rt["max_cycles"]:
                self._human(conn, rt, req, f"Reached max cycles ({rt['max_cycles']}).", text)
                return
            plan_text = "\n".join(f"[{memory.MARK_BY_STATUS.get(t['status'], ' ')}] {t['task_key']} {t['title']}"
                                  for t in memory.plan(conn, rt["project_id"]))
            self._finish_with(conn, rt, req, role="worker", kind="ASSIGN",
                              prompt=ade.worker_assignment(assignment=decision.text, plan=plan_text,
                                                           last_result=self._last_output(conn, rt)),
                              successor_detail=json.dumps({"assignment": decision.text}))
            return
        if milestone and decision.kind == "CONTINUE":
            assignment = self._assignment(conn, rt)
            if assignment:
                self._finish_with(conn, rt, req, role="worker", kind="GUIDE",
                                  prompt=ade.worker_guidance(guidance=decision.text or "Continue.",
                                                             assignment=assignment),
                                  successor_detail=json.dumps({"assignment": assignment}))
                return
        if not milestone and decision.kind == "ASSIGN":
            decision = ade.PmDirective("TASK", decision.text)
        if decision.kind == "TASK":
            if rt["cycle_count"] >= rt["max_cycles"]:
                self._human(conn, rt, req, f"Reached max cycles ({rt['max_cycles']}).", text)
                return
            self._finish_with(conn, rt, req, role="worker", kind="TASK",
                              prompt=ade.worker_task(task=decision.text, last_result=self._last_output(conn, rt)),
                              successor_detail=json.dumps({"task": decision.text}))
        elif decision.kind == "DONE":
            transition_request_in(conn, request_id=req["id"], to_state="COMPLETED",
                                  updates={"detail": json.dumps({"done": True})})
            store.update_runtime(conn, rt["project_id"], status="FINISHED",
                                 reason="The PM reported the project goal complete.")
        elif decision.kind == "ASK_HUMAN":
            self._human(conn, rt, req, "PM asks the human: " + decision.text[:400], text)
        elif self._pm_nudges_used(conn, rt) < int(self.cfg["format_nudges"]):
            expected = "RELAY_ASSIGN / RELAY_CONTINUE" if milestone else "RELAY_TASK"
            self._finish_with(conn, rt, req, role="pm", kind="PM_PLAN", prompt=ade.pm_nudge(expected, rt["pace"]),
                              successor_detail=json.dumps({"nudge": True}))
        else:
            self._human(conn, rt, req, "PM reply had no RELAY_TASK / RELAY_DONE / RELAY_ASK_HUMAN.", text)

    # -- COMMAND_VALIDATED -> RUNNING_CLI -> CLI_COMPLETE

    def _on_validated(self, rt: dict[str, Any], req: dict[str, Any]) -> None:
        with self.lock, self.db.transaction() as conn:
            execution = store.execution_for(conn, req["id"])
            store.mark_execution_started(conn, execution["id"])
            transition_request_in(conn, request_id=req["id"], to_state="RUNNING_CLI",
                                  payload={"execution_id": execution["id"]})
            self._executing.add(req["id"])

        def on_start(pid: int) -> None:
            with self.lock, self.db.transaction() as c:
                store.set_execution_pid(c, execution["id"], pid)

        try:
            result = self.runner(execution["command_text"], cwd=execution["cwd"],
                                 timeout_seconds=float(self.cfg["command_timeout_seconds"]),
                                 on_start=on_start)
            git_after = self.git(execution["cwd"])
        except Exception:
            with self.lock:
                self._executing.discard(req["id"])
            raise

        with self.lock, self.db.transaction() as conn:
            store.finish_execution(conn, execution_id=execution["id"], return_code=result.return_code,
                                   output=result.output, git_after=git_after)
            transition_request_in(conn, request_id=req["id"], to_state="CLI_COMPLETE",
                                  payload={"return_code": result.return_code,
                                           "timed_out": result.timed_out})
            fresh = store.runtime(conn, rt["project_id"])
            store.update_runtime(conn, rt["project_id"], cycle_count=fresh["cycle_count"] + 1)
            self._executing.discard(req["id"])

    def _on_running_cli(self, rt: dict[str, Any], req: dict[str, Any]) -> None:
        """RUNNING_CLI that this process is not executing: Relay restarted mid-command."""
        with self.lock, self.db.transaction() as conn:
            execution = store.execution_for(conn, req["id"])
            self._to_recovery(conn, req, {"code": "INTERRUPTED_EXECUTION",
                                          "message": "Relay restarted during execution.",
                                          "evidence": {"execution_id": execution and execution["id"],
                                                       "pid": execution and execution["pid"]}})

    # -- CLI_COMPLETE -> RUNNING_WATCHDOG -> CONTINUE_READY -> COMPLETED

    def _on_cli_complete(self, rt: dict[str, Any], req: dict[str, Any]) -> None:
        with self.lock, self.db.transaction() as conn:
            if req["state"] == "CLI_COMPLETE":
                transition_request_in(conn, request_id=req["id"], to_state="RUNNING_WATCHDOG")
            cycles = store.recent_cycles(conn, rt["session_id"], self.history_window)
            execution = store.execution_for(conn, req["id"])

        verdict = self.judge(rt["project_name"], self._root(rt), cycles)  # may take minutes (Ollama)

        with self.lock, self.db.transaction() as conn:
            rt = store.runtime(conn, rt["project_id"]) | {"repository_root": rt["repository_root"],
                                                           "project_name": rt["project_name"]}
            store.record_watchdog(conn, request_id=req["id"], status=verdict["status"],
                                  reason=verdict["reason"], votes=verdict.get("votes", []))
            cycle = store.cycle_from_execution(execution)
            votes = "; ".join(f"{v['voter']}={v['verdict']}" for v in verdict.get("votes", []))
            watchdog_line = f"{verdict['status']} ({votes})" if votes else verdict["status"]
            strong = self.cfg["models"].get("strong_label") or ""
            note = ""
            model = None

            if rt["mode"] == "ade":
                previous = conn.execute(
                    """SELECT d.status FROM watchdog_decisions d JOIN requests r ON r.id = d.request_id
                       WHERE r.session_id = ? AND d.request_id != ? ORDER BY d.created_at DESC LIMIT 1""",
                    (rt["session_id"], req["id"])).fetchone()
                loop = verdict["status"] == "LOOP"
                if loop and previous and previous["status"] == "LOOP":
                    self._human(conn, rt, req, "Loop persisted after the PM changed approach: " + verdict["reason"])
                    return
                evidence = prompts.without_protocol(prompts.evidence_prompt(
                    project=rt["project_name"], root=self._root(rt), command=cycle["command"],
                    return_code=cycle["return_code"], output=cycle["terminal_output"],
                    git_before=cycle["git_before"], git_after=cycle["git_after"],
                    watchdog_line=watchdog_line, max_chars=int(self.cfg["evidence_max_chars"])))
                if rt["pace"] == "milestone":
                    reason = ("LOOP" if loop
                              else "HUMAN" if self._pending_human(conn, rt["project_id"])
                              else "PERIODIC" if len(self._commands_since_pm(conn, rt)) >= rt["checkin_every"]
                              else None)
                    if reason is None:
                        assignment = self._assignment(conn, rt)
                        next_prompt = ade.worker_evidence(evidence=evidence, assignment=assignment)
                        transition_request_in(conn, request_id=req["id"], to_state="CONTINUE_READY",
                                              updates={"detail": json.dumps({"next_prompt": next_prompt,
                                                                             "next_role": "worker",
                                                                             "next_kind": "CYCLE",
                                                                             "assignment": assignment})})
                        self._finish_with(conn, rt, req, role="worker", kind="CYCLE", prompt=next_prompt,
                                          from_state="CONTINUE_READY",
                                          successor_detail=json.dumps({"assignment": assignment}))
                        return
                    transition_request_in(conn, request_id=req["id"], to_state="CONTINUE_READY")
                    self._checkin(conn, rt, req, reason, last_evidence=evidence, watchdog_line=watchdog_line,
                                  from_state="CONTINUE_READY")
                    return
                next_prompt = ade.pm_evidence(evidence=evidence, watchdog_line=watchdog_line, loop=loop)
                transition_request_in(conn, request_id=req["id"], to_state="CONTINUE_READY",
                                      updates={"detail": json.dumps({"next_prompt": next_prompt, "next_role": "pm",
                                                                     "next_kind": "PM_PLAN"})})
                self._finish_with(conn, rt, req, role="pm", kind="PM_PLAN", prompt=next_prompt,
                                  from_state="CONTINUE_READY")
                return

            if verdict["status"] == "LOOP":
                if rt["model_mode"] == "DEFAULT" and strong:
                    store.update_runtime(conn, rt["project_id"], model_mode="ESCALATED", progress_streak=0)
                    note, model = prompts.ESCALATION_NOTE, strong
                else:
                    report = core.build_human_required_report(
                        core.Project(rt["project_name"], Path(self._root(rt))),
                        {"command": cycle["command"], "terminal_output": cycle["terminal_output"]},
                        core.Decision("HUMAN_REQUIRED",
                                      [core.Vote(v["voter"], v["verdict"], v["reason"], v.get("model"))
                                       for v in verdict.get("votes", [])],
                                      verdict["reason"]),
                    )
                    self._human(conn, rt, req, "Loop persisted on the strong model: " + verdict["reason"],
                                report)
                    return
            elif rt["model_mode"] == "ESCALATED":
                streak = rt["progress_streak"] + 1 if verdict.get("progress") else 0
                if streak >= int(self.cfg["deescalate_after_progress"]):
                    store.update_runtime(conn, rt["project_id"], model_mode="DEFAULT", progress_streak=0)
                else:
                    store.update_runtime(conn, rt["project_id"], progress_streak=streak)

            next_prompt = prompts.evidence_prompt(
                project=rt["project_name"], root=self._root(rt), command=cycle["command"],
                return_code=cycle["return_code"], output=cycle["terminal_output"],
                git_before=cycle["git_before"], git_after=cycle["git_after"],
                watchdog_line=watchdog_line, max_chars=int(self.cfg["evidence_max_chars"]), note=note,
            )
            transition_request_in(conn, request_id=req["id"], to_state="CONTINUE_READY",
                                  updates={"detail": json.dumps({"next_prompt": next_prompt,
                                                                 "next_model": model})})
            self._finish_with(conn, rt, req, prompt=next_prompt, kind="CYCLE", model=model,
                              from_state="CONTINUE_READY")

    def _on_continue_ready(self, rt: dict[str, Any], req: dict[str, Any]) -> None:
        with self.lock, self.db.transaction() as conn:
            d = store.detail_json(req)
            self._finish_with(conn, rt, req, prompt=d["next_prompt"], kind=d.get("next_kind", "CYCLE"),
                              model=d.get("next_model"), from_state="CONTINUE_READY",
                              role=d.get("next_role", "worker"))

    # -- RECOVERY_REQUIRED

    def _on_recovery(self, rt: dict[str, Any], req: dict[str, Any]) -> None:
        info = store.detail_json(req)
        code = info.get("code", "UNKNOWN")
        git_now = self.git(self._root(rt)) if code == "INTERRUPTED_EXECUTION" else None

        with self.lock, self.db.transaction() as conn:
            def same_code(r: dict[str, Any]) -> bool:
                return r["state"] == "RECOVERY_REQUIRED" and store.detail_json(r).get("code") == code

            successor: str | None = None

            if code == "NOT_PERSISTED":
                if self._consecutive(conn, rt, same_code) <= int(self.cfg["unpersisted_resubmits"]):
                    original = store.detail_json(req).get("original_detail")
                    successor = store.create_request(
                        conn, session_id=rt["session_id"], conversation_id=req["conversation_id"],
                        kind=req["kind"], prompt=req["prompt_text"], model=req["model"], role=req["role"],
                        detail=original)

            elif code == "CONVERSATION_LIMIT":
                conv = store.get_conversation(conn, req["conversation_id"])
                if conv["status"] == "ACTIVE" and conv["char_count"] > 0:
                    original = json.loads(store.detail_json(req).get("original_detail") or "{}")
                    if req["kind"] == "HANDOFF":
                        pending = original.get("pending_prompt", "")
                    else:
                        pending = req["prompt_text"]
                    seed = conn.execute(
                        "SELECT prompt_text FROM requests WHERE session_id = ? ORDER BY sequence_number LIMIT 1",
                        (rt["session_id"],)).fetchone()["prompt_text"]
                    last = conn.execute(
                        """SELECT assistant_text FROM requests WHERE session_id = ?
                           AND assistant_text IS NOT NULL ORDER BY sequence_number DESC LIMIT 1""",
                        (rt["session_id"],)).fetchone()
                    handoff = prompts.fallback_handoff(
                        project=rt["project_name"], root=self._root(rt), seed=seed,
                        cycles=store.recent_cycles(conn, rt["session_id"],
                                                   int(self.cfg["fallback_handoff_cycles"])),
                        last_reply=last["assistant_text"] if last else "")
                    successor = self._rollover(conn, rt, reason="HARD_LIMIT", handoff=handoff,
                                               pending_prompt=pending, pending_model=req["model"],
                                               role=req["role"],
                                               pending_detail=store.detail_json(req).get("original_detail"))

            elif code in {"REPLY_FAILED", "REPLY_TIMEOUT"}:
                if self._consecutive(conn, rt, same_code) <= 1:
                    retry = prompts.reply_failed_prompt()
                    if req["role"] == "pm":
                        retry = prompts.without_protocol(retry) + "\n\n" + ade.pm_protocol(rt["pace"]) + "\n"
                    elif rt["mode"] == "ade" and rt["pace"] == "milestone":
                        retry = prompts.without_protocol(retry) + "\n\n" + ade.WORKER_MILESTONE_PROTOCOL + "\n"
                    successor = self._queue(conn, rt, prompt=retry, kind="RETRY", role=req["role"],
                                            detail=store.detail_json(req).get("original_detail"))

            elif code == "INTERRUPTED_EXECUTION":
                execution = store.execution_for(conn, req["id"])
                recovery = prompts.interrupted_execution_prompt(
                    execution["command_text"] if execution else "", git_now or {})
                if rt["mode"] == "ade":
                    successor = self._queue(conn, rt, kind="PM_PLAN", role="pm", prompt=ade.pm_evidence(
                        evidence=prompts.without_protocol(recovery), watchdog_line="(interrupted)", loop=False,
                        pace=rt["pace"]))
                else:
                    successor = self._queue(conn, rt, kind="RECOVERY", prompt=recovery)

            if successor is None:
                transition_request_in(conn, request_id=req["id"], to_state="HUMAN_REQUIRED")
                store.update_runtime(conn, rt["project_id"], status="HUMAN_REQUIRED",
                                     reason=f"Recovery needs a human: {code}: {info.get('message', '')}"[:500])
            else:
                store.set_request_fields(conn, req["id"], successor_request_id=successor)
                store.event(conn, project_id=rt["project_id"], request_id=req["id"],
                            event_type="RECOVERY_SUCCESSOR", payload={"code": code, "successor": successor})
