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
        review_policy: str = "risky",
        pm_conversation_url: str | None = None,
        pm_new_chat: bool = False,
    ) -> dict[str, Any]:
        if conversation_url and new_chat:
            raise RelayRefused("Use either a conversation URL or --new-chat, not both.")
        if mode not in {"solo", "ade"}:
            raise RelayRefused(f"Unknown mode {mode!r}.")
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
                              mode=mode, goal=goal or seed, review_policy=review_policy)
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
                request_id = store.create_request(
                    conn, session_id=session_id, conversation_id=pm_conv["id"], kind="PM_PLAN", role="pm",
                    prompt=ade.pm_kickoff(project=name, root=root, goal=goal or seed or "",
                                          rules=rules, git=git_now, memory=memory.render(conn, pid)),
                    model=None,
                )
            else:
                remembered = memory.render(conn, pid)
                seed_text = (seed or prompts.DEFAULT_SEED) + (f"\n\n{remembered}" if remembered else "")
                request_id = store.create_request(
                    conn, session_id=session_id, conversation_id=active["id"], kind="SEED",
                    prompt=prompts.with_protocol(seed_text),
                    model=self._model_label(conn, pid) or None,
                )
        self._wake.set()
        return {"project": name, "session_id": session_id, "request_id": request_id, "mode": mode,
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
            prompt = text.rstrip() + "\n\n" + ade.PM_PROTOCOL + "\n" if role == "pm" else prompts.with_protocol(text)
            request_id = store.create_request(
                conn, session_id=rt["session_id"], conversation_id=conv["id"], kind="USER", role=role,
                prompt=prompt, model=None if role == "pm" else self._model_label(conn, pid) or None,
            )
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

    def poll(self, *, lease: str, page_url: str | None = None, project: str | None = None) -> dict[str, Any]:
        if not lease:
            raise RelayRefused("lease required")
        with self.lock:
            self._lease_seen[lease] = self.clock()
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
                return job

    def _runtime_summary(self, conn) -> list[dict[str, Any]]:
        return [{"project": r["project_name"], "status": r["status"], "reason": r["reason"], "mode": r["mode"]}
                for r in store.all_runtimes(conn)]

    def _owned(self, conn, lease: str, request_id: str, states: set[str]) -> dict[str, Any]:
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
        if role == "pm":
            label = ""
        else:
            label = model if model is not None else self._model_label(conn, rt["project_id"])
        budget = int(self.cfg["rollover_char_budget"])
        if (kind not in {"HANDOFF", "ROLLOVER_SEED"} and conv["conversation_url"]
                and conv["char_count"] + len(prompt) > budget):
            return store.create_request(
                conn, session_id=rt["session_id"], conversation_id=conv["id"], kind="HANDOFF", role=role,
                prompt=prompts.handoff_request(), model=label or None,
                detail=json.dumps({"pending_prompt": prompt, "pending_kind": kind,
                                   "pending_model": label, "pending_detail": detail}),
            )
        return store.create_request(conn, session_id=rt["session_id"], conversation_id=conv["id"],
                                    kind=kind, prompt=prompt, model=label or None, role=role, detail=detail)

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
            prompt = prompts.without_protocol(prompt) + "\n\n" + ade.PM_PROTOCOL + "\n"
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
            if ade_mode and not blocks:
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
            self._finish_with(conn, rt, req, role="pm", kind="PM_PLAN", prompt=ade.pm_nudge("RELAY_TASK"),
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
                        retry = prompts.without_protocol(retry) + "\n\n" + ade.PM_PROTOCOL + "\n"
                    successor = self._queue(conn, rt, prompt=retry, kind="RETRY", role=req["role"],
                                            detail=store.detail_json(req).get("original_detail"))

            elif code == "INTERRUPTED_EXECUTION":
                execution = store.execution_for(conn, req["id"])
                recovery = prompts.interrupted_execution_prompt(
                    execution["command_text"] if execution else "", git_now or {})
                if rt["mode"] == "ade":
                    successor = self._queue(conn, rt, kind="PM_PLAN", role="pm", prompt=ade.pm_evidence(
                        evidence=prompts.without_protocol(recovery), watchdog_line="(interrupted)", loop=False))
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
