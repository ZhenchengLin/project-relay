// Project Relay service worker: the only component that holds the daemon
// token and talks to prelayd. Content scripts and the popup call it via
// chrome.runtime messages; page scripts on chatgpt.com cannot.
importScripts("relay-config.js");

const ALLOWED_PATH = /^\/v2\/(health|status|projects|events|supervisor|notes(\/[a-z]+)?|browser\/[a-z]+|control\/[a-z]+)(\?[\w=&%.-]*)?$/;

async function callDaemon(method, path, body) {
  if (!ALLOWED_PATH.test(path) || !["GET", "POST"].includes(method)) {
    return { ok: false, status: 0, data: { error: "path not allowed" } };
  }
  try {
    const response = await fetch(`http://127.0.0.1:${RELAY_CONFIG.port}${path}`, {
      method,
      headers: { "Content-Type": "application/json", "X-Relay-Token": RELAY_CONFIG.token },
      body: method === "POST" ? JSON.stringify(body || {}) : undefined,
      cache: "no-store",
    });
    const data = await response.json().catch(() => ({}));
    return { ok: response.ok, status: response.status, data };
  } catch (error) {
    return { ok: false, status: 0, data: { error: `daemon unreachable: ${error}` } };
  }
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (sender.id !== chrome.runtime.id) return false;
  if (message?.type === "timing") {
    // Optional overrides (tests); never exposes the token.
    sendResponse(RELAY_CONFIG.timing || {});
    return false;
  }
  if (message?.type === "arrange") {
    arrangeWindows(message).then(sendResponse);
    return true;
  }
  if (message?.type === "open-dashboard") {
    chrome.tabs.create({ url: chrome.runtime.getURL("dashboard.html"), pinned: true }).then(() => sendResponse({ ok: true }));
    return true;
  }
  if (message?.type === "insert-text" && sender.tab?.id !== undefined) {
    insertTextWithDebugger(sender.tab.id, String(message.text || "")).then(sendResponse);
    return true;
  }
  if (message?.type !== "api") return false;
  callDaemon(message.method, message.path, message.body).then(sendResponse);
  return true;
});

// Heartbeat for content scripts. Timers in background tabs are heavily
// throttled; port messages are not, so the Relay tab keeps working while
// the user is in another tab. Each reply from the page also keeps this
// service worker alive.
const ports = new Set();
chrome.runtime.onConnect.addListener((port) => {
  if (port.name !== "relay-tick" || port.sender?.id !== chrome.runtime.id) return;
  ports.add(port);
  port.onDisconnect.addListener(() => ports.delete(port));
  port.onMessage.addListener(() => {});
});
setInterval(() => {
  for (const port of ports) {
    try { port.postMessage({ type: "tick" }); } catch (_) { ports.delete(port); }
  }
}, 1000);

// Type into a ChatGPT or Claude tab whose window is not focused (browsers ignore
// simulated editing there). Only ever targets the tab that asked, only on
// chatgpt.com, and detaches immediately.
async function insertTextWithDebugger(tabId, text) {
  const target = { tabId };
  try {
    const tab = await chrome.tabs.get(tabId);
    if (!/^https:\/\/((www\.)?chatgpt\.com|claude\.ai)\//.test(tab.url || "")) {
      return { ok: false, error: "not a ChatGPT or Claude tab" };
    }
    await chrome.debugger.attach(target, "1.3");
    try {
      await chrome.debugger.sendCommand(target, "Emulation.setFocusEmulationEnabled", { enabled: true });
      await chrome.debugger.sendCommand(target, "Input.insertText", { text });
    } finally {
      await chrome.debugger.detach(target).catch(() => {});
    }
    return { ok: true };
  } catch (error) {
    return { ok: false, error: String(error && error.message ? error.message : error) };
  }
}

// Relay ADE layout: Claude (PM) on the left, ChatGPT (Worker) on the right,
// both visible so neither site throttles its replies, each bound to the run.
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function bindWhenReady(tabId, project) {
  for (let i = 0; i < 90; i++) {
    try {
      const reply = await chrome.tabs.sendMessage(tabId, { type: "relay-activate", active: true, project });
      if (reply && reply.ok) return true;
    } catch (_) {
      // content script not loaded yet
    }
    await sleep(500);
  }
  return false;
}

async function arrangeWindows({ project, pmUrl, workerUrl, screen }) {
  const allowed = /^https:\/\/((www\.)?chatgpt\.com|claude\.ai)\//;
  const urls = [pmUrl, workerUrl].filter(Boolean);
  if (!project || !urls.length || !urls.every((u) => allowed.test(u))) return { ok: false, error: "bad request" };
  const box = screen && screen.width ? screen : { left: 0, top: 0, width: 1440, height: 900 };
  const width = urls.length === 2 ? Math.floor(box.width / 2) : box.width;
  const created = [];
  for (const [i, url] of urls.entries()) {
    const win = await chrome.windows.create({
      url, left: box.left + i * width, top: box.top, width: i === urls.length - 1 ? box.width - i * width : width,
      height: box.height, focused: i === urls.length - 1,
    });
    created.push(win.tabs[0].id);
  }
  const bound = await Promise.all(created.map((tabId) => bindWhenReady(tabId, project)));
  return { ok: bound.every(Boolean), bound };
}

// ---------------------------------------------------------------- auto-open
// When a running step needs a Claude or ChatGPT tab and none is connected
// (prelayd reports it as missing_tab), open it: the run's chat, or a new chat,
// in its own window (Claude left, ChatGPT right) and bind it to the run. A tab
// already on that chat but cut off (e.g. by an extension reload) is reloaded
// instead. At most one attempt per run and site every few minutes, so a tab
// that is still loading is never opened twice. Off: the dashboard switch.
const AUTO_OPEN_RETRY_MS = 3 * 60 * 1000;
const autoOpenTried = {};
let autoOpening = false;

function chatKey(url) {
  const match = /^https:\/\/(?:www\.)?(?:chatgpt\.com\/c|claude\.ai\/chat)\/([0-9A-Za-z-]{8,})/.exec(url || "");
  return match ? match[1] : null;
}

async function screenArea() {
  try {
    const win = await chrome.windows.getLastFocused();
    if (win && win.width) return { left: win.left, top: win.top, width: win.width, height: win.height };
  } catch (_) {}
  return { left: 0, top: 0, width: 1440, height: 900 };
}

async function openForRun(project, site, url) {
  const tabs = await chrome.tabs.query({ url: site === "claude" ? "https://claude.ai/*" : "https://chatgpt.com/*" });
  const key = chatKey(url);
  const existing = key && tabs.find((t) => chatKey(t.url) === key);
  if (existing) {
    await chrome.tabs.reload(existing.id);
    await bindWhenReady(existing.id, project);
    return;
  }
  const box = await screenArea();
  const half = Math.floor(box.width / 2);
  // Opened blank, then navigated: tools that watch new tabs (tests) attach first.
  const win = await chrome.windows.create({
    url: "about:blank", top: box.top, height: box.height, focused: true,
    left: site === "claude" ? box.left : box.left + half,
    width: site === "claude" ? half : box.width - half,
  });
  const tabId = win.tabs[0].id;
  await sleep(500);
  await chrome.tabs.update(tabId, { url });
  await bindWhenReady(tabId, project);
}

async function autoOpen() {
  if (autoOpening) return;
  autoOpening = true;
  try {
    const { autoOpenTabs } = await chrome.storage.local.get({ autoOpenTabs: true });
    if (!autoOpenTabs) return;
    const res = await callDaemon("GET", "/v2/status");
    if (!res.ok) return;
    for (const rt of res.data.runtimes || []) {
      const need = rt.status === "RUNNING" ? rt.missing_tab : null;
      if (!need) continue;
      const key = `${rt.project}:${need.site}`;
      if (Date.now() - (autoOpenTried[key] || 0) < AUTO_OPEN_RETRY_MS) continue;
      autoOpenTried[key] = Date.now();
      const url = need.site === "claude"
        ? rt.pm_conversation?.url || "https://claude.ai/new"
        : rt.conversation?.url || "https://chatgpt.com/";
      await openForRun(rt.project, need.site, url);
    }
  } catch (_) {
    // best effort; the next check retries
  } finally {
    autoOpening = false;
  }
}

chrome.alarms.create("relay-auto-open", { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener((alarm) => { if (alarm.name === "relay-auto-open") autoOpen(); });
chrome.runtime.onStartup.addListener(autoOpen);
chrome.runtime.onInstalled.addListener(autoOpen);
if (RELAY_CONFIG.timing?.autoOpenCheckMs) setInterval(autoOpen, RELAY_CONFIG.timing.autoOpenCheckMs); // tests
