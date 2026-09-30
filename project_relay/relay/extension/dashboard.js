"use strict";
// Relay ADE dashboard (extension page, meant to be pinned). Talks to prelayd
// only through the service worker, like every other extension page.

const $ = (id) => document.getElementById(id);
const lastEventId = {};
const notesOpen = {};
const timelines = {};

function api(method, path, body) {
  return new Promise((resolve) => {
    chrome.runtime.sendMessage({ type: "api", method, path, body }, (reply) => {
      resolve(reply || { ok: false, status: 0, data: { error: chrome.runtime.lastError?.message } });
    });
  });
}

function el(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (key === "class") node.className = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (value !== undefined && value !== null) node.setAttribute(key, value);
  }
  for (const child of children.flat()) if (child !== null && child !== undefined) node.append(child);
  return node;
}

// ------------------------------------------------------------ wording

function stepText(rt) {
  const req = rt.request;
  const who = req && req.role === "pm" ? "PM (Claude)" : "Worker (ChatGPT)";
  const statusText = {
    PAUSED: "Paused.",
    STOPPED: "Stopped.",
    FINISHED: "Finished: the goal was reported done.",
    HUMAN_REQUIRED: "Waiting for you.",
  }[rt.status];
  if (statusText) return statusText;
  if (!req) return "Starting…";
  const s = req.state;
  if (req.kind === "HANDOFF") return `${who}'s chat is getting long: writing a handoff for a fresh chat.`;
  if (["QUEUED", "PREPARING_BROWSER", "READY_TO_SUBMIT"].includes(s)) {
    if (req.kind === "PM_REVIEW") return "Sending a risky command to the PM (Claude) for approval.";
    return `Sending the next message to the ${who}.`;
  }
  if (["SUBMITTING", "PROMPT_ACCEPTED", "WAITING_ASSISTANT", "ASSISTANT_BOUND"].includes(s)) {
    if (req.kind === "PM_REVIEW") return "PM (Claude) is reviewing a risky command before it runs.";
    return req.role === "pm" ? "PM (Claude) is deciding the next step." : "Worker (ChatGPT) is writing the command.";
  }
  if (s === "ASSISTANT_COMPLETE") return `Reading the ${who}'s reply.`;
  if (["COMMAND_VALIDATED", "RUNNING_CLI"].includes(s)) return "Running the command on your Mac.";
  if (["CLI_COMPLETE", "RUNNING_WATCHDOG"].includes(s)) return "The local judges are voting on progress.";
  if (s === "RECOVERY_REQUIRED") return "Recovering from a browser problem.";
  return s;
}

function statusBadge(status) {
  const cls = { RUNNING: "ok", PAUSED: "warn", HUMAN_REQUIRED: "warn", FINISHED: "ok", STOPPED: "" }[status] || "";
  return el("span", { class: `badge ${cls}` }, status.replace("_", " ").toLowerCase());
}

function eventLine(e) {
  const p = e.payload || {};
  const d = p.details || {};
  switch (e.event_type) {
    case "REQUEST_CREATED": {
      const who = p.role === "pm" ? "PM" : "Worker";
      const what = { PM_PLAN: "planning message", PM_REVIEW: "review request", TASK: "task", REVISE: "revision",
                     NUDGE: "format reminder", HANDOFF: "handoff request", ROLLOVER_SEED: "fresh-chat seed",
                     SEED: "first message", USER: "your message", CYCLE: "evidence", RETRY: "retry" }[p.kind] || p.kind;
      return `Queued ${what} for the ${who}.`;
    }
    case "REQUEST_STATE_CHANGED":
      if (p.to_state === "SUBMITTING") return "Sent (exactly once).";
      if (p.to_state === "ASSISTANT_COMPLETE") return `Reply received (${d.chars ?? "?"} chars).`;
      if (p.to_state === "COMMAND_VALIDATED" && d.approved_by === "pm") return "PM approved the command.";
      if (p.to_state === "RUNNING_CLI") return "Command started.";
      if (p.to_state === "CLI_COMPLETE") return `Command finished: exit ${d.return_code}${d.timed_out ? " (timed out)" : ""}.`;
      if (p.to_state === "HUMAN_REQUIRED") return "Stopped for a human.";
      if (p.to_state === "RECOVERY_REQUIRED") return `Recovery needed: ${d.code || ""}`;
      return null;
    case "RUNTIME_CHANGED":
      return `Run is now ${String(p.status || p.model_mode || "").toLowerCase()}`
        + `${p.reason ? ": " + String(p.reason).replace(/\.+$/, "") : ""}.`;
    case "BROWSER_FAILURE": return `Browser: ${p.code} ${p.message || ""}`.trim();
    case "CONVERSATION_STARTED": return `New ${p.role === "pm" ? "Claude" : "ChatGPT"} chat${p.url ? "" : " (will open fresh)"}.`;
    case "CONVERSATION_RETIRED": return `Chat retired (${p.reason}).`;
    case "RECOVERY_SUCCESSOR": return `Recovered from ${p.code} with a new request.`;
    case "SUPERVISOR_ALERT": return `⚑ ${p.message}`;
    case "NOTE_ADDED": return `Remembered (${p.source}): ${p.text}`;
    case "NOTE_REMOVED": return `Forgot note #${p.id}.`;
    case "PLAN_UPDATED": return `Plan updated: ${p.done}/${p.tasks} done.`;
    default: return null;
  }
}

// --------------------------------------------------------- run cards

async function loadTimeline(project) {
  const after = lastEventId[project] || 0;
  const res = await api("GET", `/v2/events?project=${encodeURIComponent(project)}&after=${after}&limit=200`);
  if (!res.ok) return timelines[project] || [];
  const lines = timelines[project] || [];
  for (const e of res.data.events) {
    lastEventId[project] = Math.max(lastEventId[project] || 0, e.id);
    const text = eventLine(e);
    if (text) lines.push({ at: e.created_at, text });
  }
  timelines[project] = lines.slice(-60);
  return timelines[project];
}

function chatLink(conv, fallbackLabel) {
  if (!conv) return el("span", { class: "muted" }, "—");
  const pct = conv.budget ? Math.min(100, Math.round((100 * conv.char_count) / conv.budget)) : 0;
  return el("span", {},
    conv.url ? el("a", { href: conv.url, target: "_blank" }, `chat #${conv.chat_number}`) : `${fallbackLabel} (opens fresh)`,
    el("span", { class: "muted" }, ` · ${pct}% of rollover budget`),
    el("div", { class: "bar", title: `${conv.char_count} chars` }, el("div", { style: `width:${pct}%` })));
}

async function control(action, project, extra) {
  const res = await api("POST", `/v2/control/${action}`, { project, ...(extra || {}) });
  if (!res.ok) alert(res.data.error || `Could not ${action}.`);
  refresh();
}

function screenBox() {
  return { left: screen.availLeft || 0, top: screen.availTop || 0, width: screen.availWidth, height: screen.availHeight };
}

function arrange(rt) {
  chrome.runtime.sendMessage({
    type: "arrange", project: rt.project,
    pmUrl: rt.mode === "ade" ? (rt.pm_conversation?.url || "https://claude.ai/new") : null,
    workerUrl: rt.conversation?.url || "https://chatgpt.com/",
    screen: screenBox(),
  }, () => void chrome.runtime.lastError);
}

function tile(value, label) {
  return el("div", { class: "tile" }, el("div", { class: "v" }, value), el("div", { class: "k" }, label));
}

function kpiTiles(k) {
  if (!k || !k.started_at) return null;
  const pct = (x) => (x === null || x === undefined ? "—" : `${Math.round(x * 100)}%`);
  const idle = k.idle_seconds === null ? "—" : k.idle_seconds < 90 ? `${k.idle_seconds}s`
    : k.idle_seconds < 5400 ? `${Math.round(k.idle_seconds / 60)}m` : `${Math.round(k.idle_seconds / 3600)}h`;
  return el("div", { class: "kpis" },
    tile(String(k.commands), "commands run"),
    tile(pct(k.success_rate), "succeeded (exit 0)"),
    tile(k.commands_per_hour === null ? "—" : String(k.commands_per_hour), "commands / hour"),
    tile(k.avg_command_seconds === null ? "—" : `${k.avg_command_seconds}s`, "avg command time"),
    tile(String(k.loops_caught), "loops caught"),
    tile(k.reviews ? `${k.approved}/${k.reviews}` : "—", `PM approvals (${k.revised} revised)`),
    tile(`${k.messages.pm} · ${k.messages.worker}`, "sent to Claude · ChatGPT"),
    tile(String(k.rollovers), "chat rollovers"),
    tile(idle, "since last activity"));
}

function planView(rt) {
  const tasks = rt.plan || [];
  if (!tasks.length) return null;
  const done = tasks.filter((t) => t.status === "done").length;
  const mark = { done: "✓", doing: "▸", blocked: "!", todo: "○" };
  return el("div", { class: "plan" },
    el("p", { class: "muted" }, `Plan · ${done}/${tasks.length} done`),
    el("div", { class: "bar" }, el("div", { style: `width:${Math.round((100 * done) / tasks.length)}%` })),
    ...tasks.map((t) => el("div", { class: `st-${t.status}` }, `${mark[t.status] || "○"} ${t.task_key} ${t.title}`)));
}

async function notesView(rt) {
  const res = await api("GET", `/v2/notes?project=${encodeURIComponent(rt.project)}`);
  const notes = res.ok ? res.data.notes : [];
  const add = async () => {
    const text = prompt(`Something ${rt.project} must always remember:`);
    if (text && text.trim()) {
      await api("POST", "/v2/notes/add", { project: rt.project, text: text.trim() });
      refresh();
    }
  };
  const remove = async (note) => {
    if (!confirm(`Forget: “${note.text}”?`)) return;
    await api("POST", "/v2/notes/remove", { project: rt.project, id: note.id });
    refresh();
  };
  const box = el("details", { class: "notes", ...(notesOpen[rt.project] ? { open: "" } : {}) },
    el("summary", {}, `Project memory (${notes.length})`),
    ...notes.map((n) => el("div", {}, `• ${n.text} `, el("span", { class: "muted" }, `(${n.source})`),
      el("button", { onclick: () => remove(n), title: "Forget this" }, "×"))),
    el("button", { onclick: add }, "Add note…"));
  box.addEventListener("toggle", () => { notesOpen[rt.project] = box.open; });
  return box;
}

async function runCard(rt) {
  const events = await loadTimeline(rt.project);
  const notes = await notesView(rt);
  const exec = rt.last_execution;
  const card = el("div", { class: "panel card" },
    el("div", { class: "head" },
      el("h3", {}, rt.project, " ",
        el("span", { class: `badge ${rt.mode === "ade" ? "pm" : "worker"}` }, rt.mode === "ade" ? "ADE" : "solo"), " ",
        statusBadge(rt.status)),
      el("span", { class: "muted" }, `command ${rt.cycle_count} of ${rt.max_cycles}`
        + (rt.mode === "ade" ? ` · review: ${rt.review_policy}` : ""))),
    el("div", { class: "step" }, stepText(rt)),
    kpiTiles(rt.kpi),
    planView(rt),
    rt.reason && rt.status !== "RUNNING"
      ? el("p", { class: rt.status === "FINISHED" || rt.status === "STOPPED" ? "muted" : "error" }, rt.reason)
      : null,
    el("div", { class: "kv" },
      rt.goal ? [el("span", { class: "muted" }, "Goal"), el("span", {}, rt.goal)] : [],
      rt.mode === "ade" ? [el("span", { class: "muted" }, "PM (Claude)"), chatLink(rt.pm_conversation, "Claude")] : [],
      [el("span", { class: "muted" }, rt.mode === "ade" ? "Worker (ChatGPT)" : "ChatGPT"), chatLink(rt.conversation, "ChatGPT")],
      [el("span", { class: "muted" }, "Repository"), el("code", {}, rt.root)]),
    notes,
    exec ? el("div", {},
      el("p", { class: "muted" }, `Last command · ${exec.completed_at ? "exit " + exec.return_code : "running…"}`),
      el("pre", {}, exec.command_head)) : null,
    el("div", { class: "controls" },
      el("div", { class: "left" },
        rt.status === "RUNNING" ? el("button", { onclick: () => control("pause", rt.project) }, "Pause") : null,
        ["RUNNING", "PAUSED", "HUMAN_REQUIRED"].includes(rt.status)
          ? el("button", { class: "danger", onclick: () => confirm(`Stop ${rt.project}?`) && control("stop", rt.project) }, "Stop")
          : null),
      el("div", { class: "right" },
        el("button", { onclick: () => arrange(rt) }, "Arrange windows"),
        rt.status !== "RUNNING" ? el("button", {
          class: "primary",
          onclick: () => {
            const needsMessage = rt.status !== "PAUSED";
            const message = needsMessage
              ? prompt(`Message to the ${rt.mode === "ade" ? "PM (Claude)" : "ChatGPT"} when resuming (optional):`)
              : null;
            if (message === null && needsMessage) return;
            if (!needsMessage && !confirm(`Resume ${rt.project}?`)) return;
            control("resume", rt.project, message ? { message } : {});
          },
        }, rt.status === "PAUSED" ? "Resume" : "Resume with a message…") : null)),
    el("div", { class: "timeline" }, ...events.slice(-15).reverse().map((line) =>
      el("div", {}, el("time", {}, line.at.slice(11, 19)), line.text))));
  return card;
}

async function refresh() {
  const status = await api("GET", "/v2/status");
  if (!status.ok) {
    $("daemon").textContent = status.status === 0 ? "prelayd is not running — run: prelay daemon" : `prelayd error: ${status.data.error}`;
    $("daemon").className = "badge bad";
    return;
  }
  $("daemon").textContent = "prelayd running";
  $("daemon").className = "badge ok";
  const sup = await api("GET", "/v2/supervisor");
  if (sup.ok && sup.data.enabled) {
    const s = sup.data;
    const cap = (used, max) => (max ? `${used}/${max}` : `${used}`);
    $("supervisor").textContent = `Supervisor: ${s.quiet ? "quiet hours now — no new messages" : "watching"}`
      + ` · today sent to Claude ${cap(s.sends_today.claude, s.budget.claude)}`
      + `, ChatGPT ${cap(s.sends_today.chatgpt, s.budget.chatgpt)}`
      + (s.quiet_hours ? ` · quiet hours ${s.quiet_hours}` : "");
  }
  const runtimes = status.data.runtimes.filter((rt) => rt.status !== "STOPPED" || timelines[rt.project]);
  const cards = await Promise.all(runtimes.map(runCard));
  $("runs").replaceChildren(...(cards.length ? cards : [el("p", { class: "muted" }, "No runs yet. Start one below.")]));
}

// ------------------------------------------------------------- start form

async function loadProjects() {
  const res = await api("GET", "/v2/projects");
  const names = res.ok ? res.data.projects : [];
  $("project").replaceChildren(...names.map((n) => el("option", { value: n }, n)));
}

async function start(arrangeAfter) {
  $("form-error").textContent = "";
  const project = $("project").value;
  const goal = $("goal").value.trim();
  if (!project) return ($("form-error").textContent = "Pick a project.");
  if (!goal) return ($("form-error").textContent = "Give the PM a goal.");
  const pmUrl = $("pm-url").value.trim() || null;
  const workerUrl = $("worker-url").value.trim() || null;
  const res = await api("POST", "/v2/control/start", {
    project, mode: "ade", goal, rules: $("rules").value.trim(), review_policy: $("review").value,
    max_cycles: Number($("max").value) || null,
    pm_conversation_url: pmUrl, pm_new_chat: !pmUrl,
    conversation_url: workerUrl, new_chat: !workerUrl,
  });
  if (!res.ok) return ($("form-error").textContent = res.data.error || "Could not start.");
  if (arrangeAfter) {
    arrange({ project, mode: "ade", pm_conversation: { url: res.data.pm_conversation_url },
              conversation: { url: res.data.conversation_url } });
  }
  refresh();
}

$("start").addEventListener("click", () => start(false));
$("start-arrange").addEventListener("click", () => start(true));

// Remember whether the explanation is collapsed (a per-viewer convenience).
try {
  const about = $("about");
  if (localStorage.getItem("relay-about-open") === "0") about.open = false;
  about.addEventListener("toggle", () => {
    try { localStorage.setItem("relay-about-open", about.open ? "1" : "0"); } catch (_) {}
  });
} catch (_) {}

loadProjects();
refresh();
setInterval(refresh, 2000);
