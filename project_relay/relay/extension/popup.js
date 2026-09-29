"use strict";

const $ = (id) => document.getElementById(id);

function api(method, path, body) {
  return new Promise((resolve) => {
    chrome.runtime.sendMessage({ type: "api", method, path, body }, (reply) => {
      resolve(reply || { ok: false, status: 0, data: { error: chrome.runtime.lastError?.message } });
    });
  });
}

async function activeTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  return tab;
}

function isChatGpt(url) {
  try { return /^(www\.)?chatgpt\.com$/.test(new URL(url).hostname); } catch (_) { return false; }
}

function conversationUrl(url) {
  try {
    const match = /\/c\/([0-9A-Za-z-]{8,})/.exec(new URL(url).pathname);
    return match ? `https://chatgpt.com/c/${match[1]}` : null;
  } catch (_) { return null; }
}

function showError(text) { $("error").textContent = text || ""; }

function el(tag, attrs, ...children) {
  const node = document.createElement(tag);
  Object.assign(node, attrs || {});
  for (const child of children) node.append(child);
  return node;
}

async function control(action, project, extra) {
  const res = await api("POST", `/v2/control/${action}`, { project, ...(extra || {}) });
  showError(res.ok ? "" : res.data.error);
  await refresh();
}

async function refresh() {
  const status = await api("GET", "/v2/status");
  if (!status.ok) {
    $("daemon").textContent = status.status === 0
      ? "prelayd is not running. In Terminal: prelay daemon"
      : `prelayd error: ${status.data.error}`;
    $("runtimes").replaceChildren();
    return;
  }
  $("daemon").textContent = "prelayd is running.";
  const cards = status.data.runtimes.map((rt) => {
    const conv = rt.conversation || {};
    const pct = conv.budget ? Math.min(100, Math.round((100 * conv.char_count) / conv.budget)) : 0;
    const card = el("div", { className: "card" },
      el("div", {}, el("b", { textContent: rt.project }), " ",
        el("span", { className: `status-${rt.status}`, textContent: rt.status })),
      el("div", { className: "muted", textContent:
        `cycle ${rt.cycle_count}/${rt.max_cycles} · model ${rt.model_mode === "ESCALATED" ? "strong" : "default"}`
        + ` · chat #${conv.chat_number || "?"}` }),
      el("div", { className: "bar", title: `chat length ${pct}% of rollover budget` },
        el("div", { style: `width:${pct}%` })),
    );
    if (rt.request) card.append(el("div", { className: "muted", textContent: `request: ${rt.request.kind} · ${rt.request.state}` }));
    if (rt.reason) card.append(el("div", { textContent: rt.reason }));
    const row = el("div", { className: "row" });
    const add = (label, fn, secondary) => row.append(el("button", { textContent: label, className: secondary ? "secondary" : "", onclick: fn }));
    if (rt.status === "RUNNING") add("Pause", () => control("pause", rt.project), true);
    if (["PAUSED", "HUMAN_REQUIRED", "STOPPED", "FINISHED"].includes(rt.status)) {
      add("Resume", () => {
        const message = rt.status === "PAUSED" ? undefined : prompt("Message to ChatGPT (optional):") || undefined;
        control("resume", rt.project, { message });
      });
    }
    if (["RUNNING", "PAUSED", "HUMAN_REQUIRED"].includes(rt.status)) add("Stop", () => control("stop", rt.project), true);
    card.append(row);
    return card;
  });
  $("runtimes").replaceChildren(...cards);
}

async function loadProjects() {
  const res = await api("GET", "/v2/projects");
  const names = res.ok ? res.data.projects : [];
  $("project").replaceChildren(...names.map((n) => el("option", { value: n, textContent: n })));
  if (!names.length) $("chat-hint").textContent = "No projects. In Terminal: prelay register NAME /path/to/repo";
}

async function updateHint() {
  const tab = await activeTab();
  const mode = document.querySelector('input[name="chat"]:checked').value;
  if (!tab || !isChatGpt(tab.url)) {
    $("chat-hint").textContent = "Open chatgpt.com in this tab first.";
  } else if (mode === "this") {
    const url = conversationUrl(tab.url);
    $("chat-hint").textContent = url ? `Continue ${url}` : "This tab is not on a saved chat; choose New chat.";
  } else {
    $("chat-hint").textContent = "Relay will open a new chat in this tab.";
  }
}

$("start").addEventListener("click", async () => {
  const tab = await activeTab();
  if (!tab || !isChatGpt(tab.url)) return showError("Open chatgpt.com in this tab first.");
  const project = $("project").value;
  if (!project) return showError("Pick a project.");
  const mode = document.querySelector('input[name="chat"]:checked').value;
  const url = conversationUrl(tab.url);
  if (mode === "this" && !url) return showError("This tab is not on a saved chat.");
  const res = await api("POST", "/v2/control/start", {
    project,
    conversation_url: mode === "this" ? url : null,
    new_chat: mode === "new",
    seed: $("seed").value.trim() || null,
    max_cycles: Number($("max").value) || null,
  });
  if (!res.ok) return showError(res.data.error);
  showError("");
  chrome.tabs.sendMessage(tab.id, { type: "relay-activate", active: true, project }, () => void chrome.runtime.lastError);
  await refresh();
});

for (const radio of document.querySelectorAll('input[name="chat"]')) radio.addEventListener("change", updateHint);

$("dashboard").addEventListener("click", () => {
  chrome.runtime.sendMessage({ type: "open-dashboard" }, () => window.close());
});

loadProjects().then(updateHint);
refresh();
setInterval(refresh, 2000);
