// Project Relay service worker: the only component that holds the daemon
// token and talks to prelayd. Content scripts and the popup call it via
// chrome.runtime messages; page scripts on chatgpt.com cannot.
importScripts("relay-config.js");

const ALLOWED_PATH = /^\/v2\/(health|status|projects|events|browser\/[a-z]+|control\/[a-z]+)(\?[\w=&%.-]*)?$/;

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
