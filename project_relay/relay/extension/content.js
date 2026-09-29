// Project Relay content script (chatgpt.com and claude.ai, isolated world).
//
// Drives exactly one chat tab for prelayd. On chatgpt.com it serves Worker
// (and solo) requests; on claude.ai it serves the Relay ADE PM. The daemon
// routes each request to a tab of the matching site. Per request:
//   poll -> [navigate] -> fill composer -> ready (daemon grants Send once)
//   -> click Send once -> observe durable user turn -> accepted
//   -> observe same-group assistant -> completion evidence -> complete
//
// It never executes anything locally and never re-sends: a request that
// might have been sent is only ever observed; the daemon decides recovery.
(function () {
  "use strict";
  if (window.__projectRelayLoaded) return;
  window.__projectRelayLoaded = true;

  const Core = globalThis.RelayCore;
  const FENCE = "`".repeat(3);
  const LEASE_KEY = "projectRelayLease";
  const ACTIVE_KEY = "projectRelayActive";
  const PROJECT_KEY = "projectRelayProject";
  const SENT_PREFIX = "projectRelaySent:";
  const RELOAD_PREFIX = "projectRelayReload:";

  // Everything site-specific lives in these two profiles; the request flow
  // below (fill, send once, accept, observe, complete) is shared.
  const CHATGPT = {
    name: "chatgpt",
    label: "ChatGPT",
    newChatUrl: "https://chatgpt.com/",
    assistantSel: [
      '[data-message-author-role="assistant"]',
      '[data-conversation-role="assistant"]',
      "[data-chatgpt-agent-turn-start]",
    ].join(","),
    userSel: [
      "[data-user-message-bubble]",
      '[data-message-author-role="user"]',
      '[data-turn="user"]',
      '[class*="group/user-message"]',
    ].join(","),
    // Reply content roots: current renderer uses CSS-module MarkdownRoot-*,
    // older ones used .markdown.
    markdownSel: '[class*="MarkdownRoot-"], .markdown',
    composerSel: [
      '[data-testid="prompt-textarea"]',
      "#prompt-textarea",
      '[contenteditable="true"][data-lexical-editor="true"]',
      'form[data-chatgpt-composer] [contenteditable="true"][role="textbox"]',
      'div.ProseMirror[contenteditable="true"]',
      "textarea",
    ].join(","),
    sendSel: [
      'button[data-testid="send-button"]',
      'button[data-testid="composer-submit-button"]',
      'form[data-chatgpt-composer] button[type="submit"]',
      'button[aria-label="Send prompt"]',
    ].join(","),
    // Generation in progress. Test ids are language-neutral; aria-labels are
    // localized (the UI may be Chinese), so they are only a secondary signal.
    stopSel: [
      '[data-testid="stop-button"]',
      '[data-testid="composer-stop-button"]',
      '[data-testid*="stop"][data-testid*="button"]',
      'form[data-chatgpt-composer] button[aria-label^="Stop"]',
      'button[aria-label="Stop streaming"]',
      'button[aria-label*="停止"]',
    ].join(","),
    streamingSel: '.result-streaming, [class*="streaming"], [data-is-streaming="true"], [aria-busy="true"]',
    // The finished turn's action row (copy / share / read aloud / regenerate).
    // Live ChatGPT (2026-09) has no test ids on these buttons; the row is a
    // .turn-action-controls container. Older builds used *turn-action-button.
    turnActionSel: '[data-testid$="turn-action-button"], .turn-action-controls button',
    turnSel: "[data-turn-key], [data-turn-id-container]",
    noticeSel: '[role="alert"], .text-token-text-error, [class*="text-red-"], [data-testid*="error"]',
    loginSel: '[data-testid="login-button"], a[href*="/auth/login"]',
    loginPath: "/auth",
    modelPicker: true,
  };

  const CLAUDE = {
    name: "claude",
    label: "Claude",
    newChatUrl: "https://claude.ai/new",
    userSel: '[data-testid="user-message"]',
    // Each assistant message is wrapped in an element carrying data-is-streaming.
    assistantSel: "[data-is-streaming]",
    markdownSel: '.font-claude-response, .font-claude-message, [class*="font-claude"], .standard-markdown',
    composerSel: [
      '[data-testid="chat-input"]',
      'div.ProseMirror[contenteditable="true"]',
      '[contenteditable="true"][role="textbox"]',
    ].join(","),
    sendSel: [
      'button[aria-label="Send message"]',
      'button[aria-label="Send Message"]',
      'button[data-testid="send-button"]',
      'button[aria-label*="发送"]',
    ].join(","),
    stopSel: [
      'button[aria-label="Stop response"]',
      'button[aria-label*="Stop"]',
      'button[data-testid="stop-button"]',
      'button[aria-label*="停止"]',
    ].join(","),
    streamingSel: '[data-is-streaming="true"]',
    turnActionSel: '[data-testid="action-bar-copy"], [data-testid*="action-bar"] button',
    turnSel: '[data-testid="user-message"], [data-is-streaming]',
    noticeSel: '[role="alert"], [data-testid*="error"], [class*="text-danger"]',
    loginSel: 'a[href*="/login"], button[data-testid="login-button"]',
    loginPath: "/login",
    modelPicker: false,
  };

  const SITE = location.hostname === "claude.ai" ? CLAUDE : CHATGPT;
  const CODE_BLOCK_CLASS = "CodeBlock-";

  let lease = sessionStorage.getItem(LEASE_KEY);
  if (!lease) {
    lease = `tab-${crypto.randomUUID()}`;
    sessionStorage.setItem(LEASE_KEY, lease);
  }
  let active = sessionStorage.getItem(ACTIVE_KEY) === "1";
  let busy = false;
  let lastJob = null;

  // Timing defaults; relay-config.js may override them (tests use short ones).
  const T = {
    acceptAfterSendMs: 90000,   // wait for a durable user turn after our own Send
    acceptObservedMs: 25000,    // same, after a reload or tab takeover
    acceptSettleMs: 2000,       // the new turn must persist this long
    replyTimeoutMs: 45 * 60 * 1000,
    probeEveryMs: 20000,        // diagnostics while waiting for a reply
    pollMs: 4000,
    completion: {},             // stoppedMs / stableMs / fallbackStableMs
  };
  const timingReady = new Promise((resolve) => {
    try {
      chrome.runtime.sendMessage({ type: "timing" }, (overrides) => {
        void chrome.runtime.lastError;
        Object.assign(T, overrides || {});
        resolve();
      });
    } catch (_) {
      resolve();
    }
  });

  // ------------------------------------------------------------ timing
  // Port ticks from the service worker are not throttled in background tabs.
  const tickWaiters = new Set();
  let port = null;

  function connect() {
    try {
      port = chrome.runtime.connect({ name: "relay-tick" });
    } catch (_) {
      return; // extension reloaded; this document is orphaned
    }
    port.onMessage.addListener(() => {
      for (const waiter of [...tickWaiters]) waiter();
      try { port.postMessage({ type: "tock" }); } catch (_) {}
      if (active && !busy) run();
    });
    port.onDisconnect.addListener(() => {
      port = null;
      setTimeout(connect, 1000);
    });
  }

  function sleep(ms) {
    return new Promise((resolve) => {
      const end = Date.now() + ms;
      const timer = setTimeout(done, ms);
      function check() { if (Date.now() >= end) done(); }
      function done() {
        clearTimeout(timer);
        tickWaiters.delete(check);
        resolve();
      }
      tickWaiters.add(check);
    });
  }

  function api(method, path, body) {
    return new Promise((resolve) => {
      try {
        chrome.runtime.sendMessage({ type: "api", method, path, body }, (reply) => {
          if (chrome.runtime.lastError || !reply) {
            resolve({ ok: false, status: 0, data: { error: chrome.runtime.lastError?.message || "no reply" } });
          } else {
            resolve(reply);
          }
        });
      } catch (error) {
        resolve({ ok: false, status: 0, data: { error: String(error) } });
      }
    });
  }

  const post = (path, body) => api("POST", path, { lease, ...body });

  async function fail(job, code, message, evidence) {
    setStatus(`${code}: ${message || ""}`);
    return post("/v2/browser/failure", { request_id: job.request_id, code, message, evidence: evidence || {} });
  }

  // --------------------------------------------------------------- DOM
  // SVG and other non-HTML elements have no innerText.
  function textOf(el) {
    if (!el) return "";
    return typeof el.innerText === "string" ? el.innerText : el.textContent || "";
  }

  function visible(el) {
    if (!el || !el.isConnected) return false;
    const rect = el.getBoundingClientRect();
    const style = getComputedStyle(el);
    return rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none";
  }

  function composer() {
    return [...document.querySelectorAll(SITE.composerSel)].find(
      (el) => visible(el) && !el.closest("#project-relay-panel-host"),
    ) || null;
  }

  function composerText(el) {
    if (!el) return "";
    return el.tagName === "TEXTAREA" ? el.value : el.innerText;
  }

  function selectAll(el) {
    el.focus();
    if (el.tagName === "TEXTAREA") {
      el.select();
      return;
    }
    const range = document.createRange();
    range.selectNodeContents(el);
    const selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
  }

  function setTextareaValue(el, value) {
    const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value").set;
    setter.call(el, value);
    el.dispatchEvent(new Event("input", { bubbles: true }));
  }

  async function clearComposer() {
    const el = composer();
    if (!el) return;
    if (el.tagName === "TEXTAREA") {
      setTextareaValue(el, "");
    } else {
      selectAll(el);
      document.execCommand("delete", false);
    }
    await sleep(150);
  }

  function attachmentCount() {
    const el = composer();
    const form = el && el.closest("form");
    return form ? form.querySelectorAll('[data-testid*="attachment"], [class*="attachment"]').length : 0;
  }

  async function filled(text, attachmentsBefore) {
    await sleep(300);
    return Core.composerMatches(composerText(composer()), text) && attachmentCount() === attachmentsBefore;
  }

  function askBackground(message) {
    return new Promise((resolve) => {
      try {
        chrome.runtime.sendMessage(message, (reply) => {
          resolve(chrome.runtime.lastError ? { ok: false, error: chrome.runtime.lastError.message } : reply);
        });
      } catch (error) {
        resolve({ ok: false, error: String(error) });
      }
    });
  }

  // Put the prompt into the composer. Each strategy is verified before the
  // next is tried; Relay only sends when the composer holds exactly the prompt.
  //   1. execCommand insertText: native editing, needs a focused window.
  //   2. synthetic paste: ProseMirror handles paste without window focus.
  //   3. Chrome debugger (Input.insertText with focus emulation): works in a
  //      background window; Chrome briefly shows its debugging bar.
  async function fillComposer(text) {
    const el = composer();
    if (!el) return { ok: false, method: null, tried: [] };
    if (el.tagName === "TEXTAREA") {
      setTextareaValue(el, text);
      return { ok: await filled(text, 0), method: "textarea", tried: ["textarea"] };
    }
    const before = attachmentCount();
    const tried = [];

    tried.push("insertText");
    selectAll(el);
    document.execCommand("insertText", false, text);
    if (await filled(text, before)) return { ok: true, method: "insertText", tried };
    await clearComposer();

    tried.push("paste");
    selectAll(el);
    const data = new DataTransfer();
    data.setData("text/plain", text);
    composer().dispatchEvent(new ClipboardEvent("paste", { clipboardData: data, bubbles: true, cancelable: true }));
    if (await filled(text, before)) return { ok: true, method: "paste", tried };
    await clearComposer();
    await removeNewAttachments(before);

    tried.push("debugger");
    selectAll(composer());
    const reply = await askBackground({ type: "insert-text", text });
    if (reply && reply.ok && (await filled(text, before))) return { ok: true, method: "debugger", tried };
    return { ok: false, method: null, tried, debugger_error: reply && reply.error };
  }

  // A rejected paste may have become a file attachment; remove it again.
  async function removeNewAttachments(before) {
    const el = composer();
    const form = el && el.closest("form");
    if (!form || attachmentCount() <= before) return;
    const removes = [...form.querySelectorAll('[data-testid*="attachment"] button, [class*="attachment"] button')];
    for (const button of removes.slice(0, attachmentCount() - before)) button.click();
    await sleep(300);
  }

  // Where the composer's text departs from the prompt (evidence for PAGE_BROKEN).
  function composerMismatch(prompt) {
    const want = Core.normalizeText(prompt);
    const el = composer();
    const got = Core.normalizeText(composerText(el));
    let i = 0;
    while (i < want.length && i < got.length && want[i] === got[i]) i++;
    const form = el && el.closest("form");
    return {
      expected_chars: want.length,
      composer_chars: got.length,
      first_difference_at: i,
      expected_context: want.slice(Math.max(0, i - 60), i + 60),
      composer_context: got.slice(Math.max(0, i - 60), i + 60),
      composer_tag: el ? el.tagName : null,
      form_attachment_like: form ? form.querySelectorAll('[data-testid*="attachment"], [class*="attachment"], [class*="file"]').length : 0,
      // Hidden/minimized windows lose focus; typing simulation may then fail.
      visibility: document.visibilityState,
      has_focus: document.hasFocus(),
      active_is_composer: Boolean(el && (document.activeElement === el || el.contains(document.activeElement))),
      version: chrome.runtime.getManifest().version,
    };
  }

  function sendButton() {
    return [...document.querySelectorAll(SITE.sendSel)].find((b) => visible(b) && !b.disabled) || null;
  }

  function generating(scope) {
    if ([...document.querySelectorAll(SITE.stopSel)].some(visible)) return true;
    if (!scope) return false;
    return Boolean(scope.matches(SITE.streamingSel) || scope.querySelector(SITE.streamingSel));
  }

  function press(el) {
    const opts = { bubbles: true, cancelable: true, composed: true, button: 0, pointerType: "mouse", isPrimary: true };
    el.dispatchEvent(new PointerEvent("pointerdown", opts));
    el.dispatchEvent(new MouseEvent("mousedown", opts));
    el.dispatchEvent(new PointerEvent("pointerup", opts));
    el.dispatchEvent(new MouseEvent("mouseup", opts));
    el.click();
  }

  function outermost(scope, selector) {
    return [...scope.querySelectorAll(selector)].filter((node) => {
      const parent = node.parentElement && node.parentElement.closest(selector);
      return !parent || !scope.contains(parent);
    });
  }

  function standaloneContainers() {
    return [...document.querySelectorAll("[data-turn-id-container]")].filter((el) => {
      if (el.closest("[data-turn-key]")) return false;
      const parent = el.parentElement && el.parentElement.closest("[data-turn-id-container]");
      return !parent;
    });
  }

  // Logical turn inventory.
  // ChatGPT (same rules as browser/src/chatgpt-turns.mjs): every data-turn-key
  // group contributes group:user:K and group:assistant:K; legacy standalone
  // turns use data-turn-id-container.
  // Claude: turns by position, claude:user:<n> / claude:assistant:<n>.
  function inventory() {
    if (SITE === CLAUDE) {
      const users = outermost(document, CLAUDE.userSel);
      const assistants = outermost(document, CLAUDE.assistantSel);
      return {
        ids: [...users.map((_, i) => `claude:user:${i}`), ...assistants.map((_, i) => `claude:assistant:${i}`)],
        legacyUsers: [],
      };
    }
    const ids = new Set();
    const legacyUsers = [];
    for (const group of document.querySelectorAll("[data-turn-key]")) {
      const key = (group.getAttribute("data-turn-key") || "").trim();
      if (!key) continue;
      ids.add(`group:user:${key}`);
      ids.add(`group:assistant:${key}`);
    }
    for (const container of standaloneContainers()) {
      const id = container.getAttribute("data-turn-id-container");
      ids.add(id);
      if (container.querySelector(CHATGPT.userSel) && !container.querySelector(CHATGPT.assistantSel)) {
        legacyUsers.push(id);
      }
    }
    return { ids: [...ids], legacyUsers };
  }

  // A new turn must be the newest one; anything else (an older turn
  // re-mounted by virtualization) is not our submission: fail closed.
  function notTailReason(fresh, baseline) {
    if (SITE === CLAUDE) {
      const users = (baseline || []).filter((id) => id.startsWith("claude:user:")).length;
      return Core.claudeIndex(fresh) === users ? null : "New Claude turn is not the next position";
    }
    const key = Core.groupKey(fresh);
    if (!key) return null;
    const keys = [...document.querySelectorAll("[data-turn-key]")]
      .map((g) => (g.getAttribute("data-turn-key") || "").trim())
      .filter((k) => k && !Core.isProvisional(`group:user:${k}`));
    return key === keys[keys.length - 1] ? null : "New turn group is not the last group";
  }

  // Server-side evidence that the submission was accepted.
  // ChatGPT: the chat has a /c/<id> URL. Claude shows the user message
  // optimistically, so additionally the reply container for the same
  // position must exist (the server started answering).
  function acceptedByServer(candidate) {
    if (!Core.conversationIdFromUrl(location.href)) return false;
    if (SITE !== CLAUDE) return true;
    return outermost(document, CLAUDE.assistantSel).length > Core.claudeIndex(candidate);
  }

  // Reply roots inside a turn scope, excluding the user's own message.
  function replyRoots(scope) {
    const roots = outermost(scope, SITE.markdownSel).filter((node) => !node.closest(SITE.userSel));
    return roots.length || SITE !== CLAUDE ? roots : [scope];
  }

  function findAssistant(userTurnId) {
    const index = Core.claudeIndex(userTurnId);
    if (index !== null) {
      const container = outermost(document, CLAUDE.assistantSel)[index];
      return container ? { id: `claude:assistant:${index}`, scope: container } : null;
    }
    const key = Core.groupKey(userTurnId);
    if (key) {
      const group = document.querySelector(`[data-turn-key="${CSS.escape(key)}"]`);
      if (!group) return null;
      if (!group.querySelector(CHATGPT.assistantSel) && !replyRoots(group).length) return null;
      return { id: `group:assistant:${key}`, scope: group };
    }
    const containers = standaloneContainers();
    const position = containers.findIndex((el) => el.getAttribute("data-turn-id-container") === userTurnId);
    if (position < 0) return null;
    for (const container of containers.slice(position + 1)) {
      if (container.querySelector(CHATGPT.assistantSel) || replyRoots(container).length) {
        return { id: container.getAttribute("data-turn-id-container"), scope: container };
      }
      if (container.querySelector(CHATGPT.userSel)) return null;
    }
    return null;
  }

  // Completion evidence: the assistant turn's action row, located AFTER the
  // reply content (so the user message's own copy button never counts) and
  // never inside a code block. It is rendered only once the reply is final.
  function inCode(b) {
    return Boolean(b.closest("pre") || b.closest(`[class*="${CODE_BLOCK_CLASS}"]`));
  }

  // Buttons after the reply content: not inside it, not in a code block, not
  // in the user message. While ChatGPT generates there are at most one or two
  // (e.g. scroll-to-bottom); a finished reply has its whole action row.
  function trailingButtons(found) {
    const roots = replyRoots(found.scope);
    const last = roots[roots.length - 1];
    if (!last) return [];
    const areas = [found.scope];
    const next = found.scope.nextElementSibling;
    if (next && !next.matches("[data-turn-key], [data-turn-id-container]")) areas.push(next);
    return areas.flatMap((area) => [...area.querySelectorAll("button")]).filter(
      (b) => !b.closest(SITE.userSel) && !inCode(b) && !last.contains(b)
        && Boolean(last.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING),
    );
  }

  const MIN_ACTION_ROW_BUTTONS = 3;

  // Claude marks a finished reply with data-is-streaming="false".
  function actionsVisible(found) {
    if (SITE === CLAUDE && found.scope.getAttribute("data-is-streaming") === "false") return true;
    const trailing = trailingButtons(found);
    return trailing.some((b) => b.matches(SITE.turnActionSel)) || trailing.length >= MIN_ACTION_ROW_BUTTONS;
  }

  // Structural DOM summary for the daemon's event log (no reply text), so
  // selector problems on the live site can be diagnosed from `prelay log`.
  function probe(found) {
    const scope = found ? found.scope : document.body;
    const roots = found ? replyRoots(found.scope) : [];
    const last = roots[roots.length - 1];
    const classOf = (el) => (typeof el.className === "string" ? el.className : el.className?.baseVal || "")
      .split(/\s+/).filter(Boolean).slice(0, 4).join(" ").slice(0, 80);
    const describe = (b) => ({
      testid: b.getAttribute("data-testid") || "",
      label: (b.getAttribute("aria-label") || "").slice(0, 40),
      visible: visible(b),
      after_reply: Boolean(last && !last.contains(b) && last.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING),
      in_reply: Boolean(last && last.contains(b)),
      in_code: inCode(b),
      in_user: Boolean(b.closest(SITE.userSel)),
      parent: b.parentElement ? classOf(b.parentElement) : "",
      grandparent: b.parentElement && b.parentElement.parentElement ? classOf(b.parentElement.parentElement) : "",
    });
    const classes = new Set();
    const attributes = new Set();
    for (const el of scope.querySelectorAll("*")) {
      const names = typeof el.className === "string" ? el.className : el.className?.baseVal || "";
      for (const name of names.split(/\s+/)) {
        if (/markdown|codeblock|stream|think|reason|result|prose|agent|cm-|turn/i.test(name)) classes.add(name.slice(0, 60));
      }
      for (const attr of el.getAttributeNames()) if (attr.startsWith("data-")) attributes.add(attr);
    }
    const form = document.querySelector("form[data-chatgpt-composer]") || (composer() && composer().closest("form"));
    return {
      scope_buttons: [...scope.querySelectorAll("button")].slice(-30).map(describe),
      composer_buttons: form ? [...form.querySelectorAll("button")].slice(0, 20).map(describe) : [],
      next_sibling_buttons: found && found.scope.nextElementSibling
        ? [...found.scope.nextElementSibling.querySelectorAll("button")].slice(0, 20).map(describe) : [],
      classes: [...classes].slice(0, 80),
      data_attributes: [...attributes].slice(0, 80),
      reply_roots: found ? replyRoots(scope).length : 0,
      reply_chars: found ? serializeReply(scope).length : 0,
      generating: generating(found && found.scope),
      actions: found ? actionsVisible(found) : null,
      trailing_buttons: found ? trailingButtons(found).length : null,
      code_blocks: found ? [...found.scope.querySelectorAll(`pre, [class*="${CODE_BLOCK_CLASS}"]`)]
        .filter((el) => !el.parentElement.closest(`pre, [class*="${CODE_BLOCK_CLASS}"]`)).slice(-3).map((el) => {
          const code = el.querySelector("code") || el;
          return {
            inner_chars: textOf(code).length, raw_chars: (code.textContent || "").length,
            inner_lines: (textOf(code).match(/\n/g) || []).length,
            raw_lines: ((code.textContent || "").match(/\n/g) || []).length,
            children: code.childElementCount,
          };
        }) : [],
      version: chrome.runtime.getManifest().version,
      lang: document.documentElement.lang || "",
      site: SITE.name,
      streaming_attr: found ? found.scope.getAttribute("data-is-streaming") : null,
      user_count: outermost(document, SITE.userSel).length,
      assistant_count: outermost(document, SITE.assistantSel).length,
      visibility: document.visibilityState,
      has_focus: document.hasFocus(),
    };
  }

  function scrollToBottom() {
    const groups = document.querySelectorAll(SITE.turnSel);
    const last = groups[groups.length - 1];
    if (last) last.scrollIntoView({ block: "end" });
  }

  // Error-styled UI only: never classify the assistant's own prose.
  function noticeIn(scope) {
    const nodes = scope.querySelectorAll(SITE.noticeSel);
    for (const node of nodes) {
      if (!visible(node) || node.closest(SITE.markdownSel)) continue;
      const kind = Core.classifyNotice(textOf(node));
      if (kind) return kind;
    }
    return null;
  }

  function limitNotice() {
    const kind = noticeIn(document);
    return kind === "CONVERSATION_LIMIT" || kind === "USAGE_LIMIT" ? kind : null;
  }

  // ------------------------------------------------------ serialization
  function serialize(node, inPre) {
    if (node.nodeType === Node.TEXT_NODE) return node.nodeValue || "";
    if (node.nodeType !== Node.ELEMENT_NODE) return "";
    const tag = node.tagName.toLowerCase();
    if (["button", "svg", "script", "style"].includes(tag)) return "";
    if (tag === "br") return "\n";
    if (tag === "pre" || String(node.className || "").includes(CODE_BLOCK_CLASS)) return serializeCode(node);
    if (tag === "code" && !inPre) return "`" + node.textContent + "`";
    let out = "";
    for (const child of node.childNodes) out += serialize(child, inPre);
    if (tag === "li") out = "- " + out.trim() + "\n";
    else if (/^h[1-6]$/.test(tag)) out = "#".repeat(Number(tag[1])) + " " + out.trim() + "\n\n";
    else if (["p", "div", "ul", "ol", "blockquote", "table", "tr"].includes(tag)) out += "\n";
    return out;
  }

  // innerText depends on rendering: a hidden tab or a scrolling code viewer
  // may not have laid out every line, and innerText then silently drops
  // them. textContent has every character; prefer it when it has at least as
  // many line breaks (i.e. the viewer keeps real newlines in its text nodes).
  function codeText(source) {
    const rendered = textOf(source);
    const raw = source.textContent || "";
    const lines = (t) => (t.match(/\n/g) || []).length;
    return raw.length > rendered.length && lines(raw) >= lines(rendered) ? raw : rendered;
  }

  // <pre> or a CodeBlock-* wrapper: code text plus its language label.
  function serializeCode(block) {
    const code = block.querySelector("code");
    const editor = block.querySelector(".cm-content");
    const pre = block.tagName === "PRE" ? block : block.querySelector("pre");
    const source = code || editor || pre || block;
    const body = codeText(source).replace(/\s+$/, "");
    const header = [...block.querySelectorAll("*")].find(
      (c) => c instanceof HTMLElement && !c.contains(source) && !source.contains(c)
        && c.children.length <= 2 && textOf(c).trim(),
    );
    const language = Core.codeLanguage({
      className: code ? code.className : "",
      dataLanguage: (code && code.getAttribute("data-language")) || block.getAttribute("data-language"),
      headerText: header ? textOf(header) : "",
    });
    return `\n${FENCE}${language}\n${body}\n${FENCE}\n`;
  }

  function serializeReply(scope) {
    return replyRoots(scope).map((r) => serialize(r, false)).join("\n\n").replace(/\n{3,}/g, "\n\n").trim();
  }

  // --------------------------------------------------------- page state
  async function waitForComposer(ms) {
    const end = Date.now() + ms;
    while (Date.now() < end) {
      if (composer()) return true;
      await sleep(300);
    }
    return false;
  }

  function pageProblem() {
    if (/just a moment/i.test(document.title)) return ["PAGE_BROKEN", "Cloudflare challenge page"];
    const login = document.querySelector(SITE.loginSel);
    if (location.pathname.startsWith(SITE.loginPath) || (login && visible(login) && !composer())) {
      return ["AUTH_REQUIRED", `${SITE.label} is signed out in this browser`];
    }
    return ["PAGE_BROKEN", `${SITE.label} composer not found`];
  }

  async function waitForTurns(ms) {
    const end = Date.now() + ms;
    while (Date.now() < end) {
      if (document.querySelector(SITE.turnSel)) return true;
      await sleep(300);
    }
    return false;
  }

  function onTarget(job) {
    if (job.conversation_url) {
      return Core.conversationIdFromUrl(location.href) === Core.conversationIdFromUrl(job.conversation_url);
    }
    return Core.isNewChatUrl(location.href);
  }

  function navigate(job) {
    setStatus("opening the Relay conversation…");
    location.assign(job.conversation_url || SITE.newChatUrl);
  }

  // ------------------------------------------------------- model picker
  function modelSwitcher() {
    const direct = document.querySelector('[data-testid="model-switcher-dropdown-button"]');
    if (direct && visible(direct)) return direct;
    return [...document.querySelectorAll('button[aria-haspopup="menu"]')].find(
      (b) => visible(b) && /model/i.test(b.getAttribute("aria-label") || ""),
    ) || null;
  }

  async function selectModel(label) {
    const want = Core.normalizeText(label).toLowerCase();
    const matches = (el) => Core.normalizeText(textOf(el)).toLowerCase().includes(want);
    let button = modelSwitcher();
    if (!button) return false;
    if (matches(button)) return true;
    press(button);
    await sleep(700);
    const menuItems = () => [...document.querySelectorAll(
      '[role="menuitem"], [role="menuitemradio"], [data-testid^="model-switcher-"]',
    )].filter((el) => visible(el) && el !== button);
    let item = menuItems().find(matches);
    if (!item) {
      const more = menuItems().find((el) => /more models|legacy models|other models/i.test(textOf(el)));
      if (more) {
        press(more);
        await sleep(700);
        item = menuItems().find(matches);
      }
    }
    if (!item) {
      document.body.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
      return false;
    }
    press(item);
    await sleep(900);
    button = modelSwitcher();
    return Boolean(button && matches(button));
  }

  // --------------------------------------------------------------- jobs
  async function handleSubmit(job) {
    if (!onTarget(job)) return navigate(job);
    if (!(await waitForComposer(30000))) {
      const [code, message] = pageProblem();
      return fail(job, code, message);
    }
    if (job.conversation_url && !(await waitForTurns(15000))) {
      return fail(job, "CONVERSATION_NOT_FOUND", "No turns rendered on the bound conversation");
    }
    const notice = limitNotice();
    if (notice) return fail(job, notice, "limit notice before send");

    if (job.model_label && SITE.modelPicker) {
      setStatus(`selecting model “${job.model_label}”…`);
      if (!(await selectModel(job.model_label))) {
        return fail(job, "MODEL_UNAVAILABLE", `model picker has no “${job.model_label}”`);
      }
    }

    const existing = composerText(composer());
    if (Core.normalizeText(existing) && !Core.composerMatches(existing, job.prompt)) {
      return fail(job, "COMPOSER_NOT_EMPTY", "The composer has someone else's draft");
    }
    setStatus("filling the composer…");
    if (!Core.composerMatches(existing, job.prompt)) {
      const fill = await fillComposer(job.prompt);
      if (!fill.ok) {
        const evidence = { ...composerMismatch(job.prompt), tried: fill.tried, debugger_error: fill.debugger_error };
        await clearComposer();
        return fail(job, "PAGE_BROKEN", "Composer text did not match the prompt after filling", evidence);
      }
      await post("/v2/browser/diag", {
        request_id: job.request_id, stage: "fill",
        probe: { method: fill.method, tried: fill.tried, visibility: document.visibilityState,
                 has_focus: document.hasFocus(), chars: job.prompt.length,
                 version: chrome.runtime.getManifest().version },
      });
    }
    let button = null;
    for (let i = 0; i < 40 && !button; i++) {
      button = sendButton();
      if (!button) await sleep(250);
    }
    if (!button) {
      const form = composer() && (composer().closest("form, fieldset") || composer().parentElement);
      const evidence = {
        site: SITE.name, version: chrome.runtime.getManifest().version,
        composer_area_buttons: form ? [...form.querySelectorAll("button")].slice(0, 20).map((b) => ({
          label: (b.getAttribute("aria-label") || "").slice(0, 40), testid: b.getAttribute("data-testid") || "",
          type: b.getAttribute("type") || "", disabled: b.disabled, visible: visible(b),
        })) : [],
      };
      await clearComposer();
      return fail(job, "PAGE_BROKEN", "Send button never became ready", evidence);
    }

    const baseline = inventory().ids;
    const grant = await post("/v2/browser/ready", {
      request_id: job.request_id, page_url: location.href, baseline,
    });
    if (!grant.ok || !grant.data.send) {
      await clearComposer();
      setStatus(`not sending: ${grant.data.reason || grant.data.error || grant.status}`);
      return;
    }

    // The single Send activation for this request.
    sessionStorage.setItem(SENT_PREFIX + job.request_id, String(Date.now()));
    (sendButton() || button).click();
    setStatus(`sent; waiting for ${SITE.label} to accept…`);
    return handleObserve({ ...job, state: "SUBMITTING", baseline, user_turn_id: null, assistant_turn_id: null });
  }

  async function handleObserve(job) {
    if (job.conversation_url && !onTarget(job)) return navigate(job);
    let userTurn = job.user_turn_id;
    if (!userTurn) {
      userTurn = await awaitAcceptance(job);
      if (!userTurn) return;
    }
    return awaitReply(job, userTurn, job.assistant_turn_id);
  }

  async function awaitAcceptance(job) {
    const sentHere = Number(sessionStorage.getItem(SENT_PREFIX + job.request_id)) || 0;
    const reloads = Number(sessionStorage.getItem(RELOAD_PREFIX + job.request_id)) || 0;
    const end = Date.now() + (sentHere ? T.acceptAfterSendMs : T.acceptObservedMs);
    let candidate = null;
    let since = 0;

    while (Date.now() < end) {
      const fresh = Core.newUserTurns(job.baseline, inventory());
      if (fresh.length > 1) {
        await fail(job, "AMBIGUOUS_SUBMISSION", `${fresh.length} new user turns`, { fresh });
        return null;
      }
      if (fresh.length === 1) {
        const tail = notTailReason(fresh[0], job.baseline);
        if (tail) {
          await fail(job, "AMBIGUOUS_SUBMISSION", tail, { fresh });
          return null;
        }
        if (candidate !== fresh[0]) {
          candidate = fresh[0];
          since = Date.now();
        } else if (Date.now() - since >= T.acceptSettleMs && acceptedByServer(candidate)) {
          const reply = await post("/v2/browser/accepted", {
            request_id: job.request_id, user_turn_id: candidate, page_url: location.href,
          });
          if (reply.ok) {
            setStatus("accepted; waiting for the reply…");
            return candidate;
          }
          setStatus(`acceptance refused: ${reply.data.error}`);
          return null;
        }
      } else {
        candidate = null;
        const notice = limitNotice();
        if (notice === "CONVERSATION_LIMIT") {
          await fail(job, notice, "limit notice after send");
          return null;
        }
      }
      await sleep(500);
    }

    // No durable turn. Reload once to read server state before concluding.
    if (reloads < 1) {
      sessionStorage.setItem(RELOAD_PREFIX + job.request_id, String(reloads + 1));
      sessionStorage.removeItem(SENT_PREFIX + job.request_id);
      setStatus("no durable turn yet; reloading to check the server state…");
      location.reload();
      return null;
    }
    await fail(job, "NOT_PERSISTED", "No new durable user turn after send and reload", {
      baseline_count: (job.baseline || []).length, url: location.href,
    });
    return null;
  }

  async function awaitReply(job, userTurn, boundAssistant) {
    const observe = Core.createCompletionTracker(T.completion);
    const end = Date.now() + T.replyTimeoutMs;
    let bound = boundAssistant;
    let nextProbe = 0;
    const report = (found, stage) =>
      post("/v2/browser/diag", { request_id: job.request_id, stage, probe: probe(found) });
    while (Date.now() < end) {
      scrollToBottom();
      const found = findAssistant(userTurn);
      const notice = (found && noticeIn(found.scope)) || limitNotice();
      if (notice) {
        await fail(job, notice === "USAGE_LIMIT" ? "REPLY_FAILED" : notice, `error notice on the reply (${notice})`);
        return;
      }
      if (found) {
        if (!bound) {
          const reply = await post("/v2/browser/bound", { request_id: job.request_id, assistant_turn_id: found.id });
          if (!reply.ok) {
            setStatus(`binding refused: ${reply.data.error}`);
            return;
          }
          bound = found.id;
        }
        const text = serializeReply(found.scope);
        const isGenerating = generating(found.scope);
        const actions = actionsVisible(found);
        const state = observe({ now: Date.now(), text, generating: isGenerating, actionsVisible: actions });
        setStatus(`${SITE.label} replying… ${text.length} chars · generating ${isGenerating} · done-row ${actions}`
          + ` · stable ${Math.round(state.stable / 1000)}s`);
        if (Date.now() >= nextProbe) {
          nextProbe = Date.now() + T.probeEveryMs;
          await report(found, "waiting");
        }
        if (state.done) {
          await report(found, "complete");
          const reply = await post("/v2/browser/complete", {
            request_id: job.request_id, assistant_turn_id: found.id, text,
          });
          if (reply.ok) {
            sessionStorage.removeItem(SENT_PREFIX + job.request_id);
            sessionStorage.removeItem(RELOAD_PREFIX + job.request_id);
            setStatus("reply delivered to Relay; running locally…");
          } else {
            setStatus(`reply refused: ${reply.data.error}`);
          }
          return;
        }
      }
      await sleep(700);
    }
    await fail(job, "REPLY_TIMEOUT", `No complete reply within ${Math.round(T.replyTimeoutMs / 60000)} minutes`);
  }

  async function run() {
    if (busy || !active) return;
    busy = true;
    try {
      await timingReady;
      const project = sessionStorage.getItem(PROJECT_KEY) || null;
      const res = await post("/v2/browser/poll", { page_url: location.href, project });
      if (!res.ok) {
        setStatus(res.status === 0 ? "prelayd is not running (prelay daemon)" : `daemon: ${res.data.error}`);
        return;
      }
      const job = res.data;
      lastJob = job;
      if (job.type === "submit") await handleSubmit(job);
      else if (job.type === "observe") await handleObserve(job);
      else renderIdle(job);
    } catch (error) {
      setStatus(`error: ${error && error.message ? error.message : error}`);
    } finally {
      busy = false;
    }
  }

  // --------------------------------------------------------------- panel
  let panel = null;

  function buildPanel() {
    const host = document.createElement("div");
    host.id = "project-relay-panel-host";
    host.style.cssText = "position:fixed;right:16px;bottom:88px;z-index:2147483647;";
    const shadow = host.attachShadow({ mode: "closed" });
    shadow.innerHTML = `
      <style>
        .box{font:12px/1.4 -apple-system,system-ui,sans-serif;background:#111;color:#eee;border-radius:10px;
             padding:10px 12px;width:260px;box-shadow:0 6px 24px rgba(0,0,0,.35)}
        .row{display:flex;align-items:center;justify-content:space-between;gap:8px}
        b{font-size:12px} .dot{width:8px;height:8px;border-radius:50%;background:#666;display:inline-block;margin-right:6px}
        .on .dot{background:#3ecf6e} .status{margin-top:6px;color:#bbb;word-break:break-word;max-height:90px;overflow:auto}
        button{font:inherit;border:0;border-radius:6px;padding:4px 8px;cursor:pointer;background:#2d6cdf;color:#fff}
        .off button{background:#444} .min{background:transparent;color:#888;padding:0 4px}
      </style>
      <div class="box"><div class="row"><b><span class="dot"></span>Project Relay <span class="ver"></span></b>
        <span><button class="toggle"></button><button class="min" title="Hide">–</button></span></div>
        <div class="status"></div></div>`;
    const box = shadow.querySelector(".box");
    shadow.querySelector(".ver").textContent = chrome.runtime.getManifest().version;
    shadow.querySelector(".toggle").addEventListener("click", () => setActive(!active));
    shadow.querySelector(".min").addEventListener("click", () => { host.style.display = "none"; });
    document.documentElement.appendChild(host);
    panel = { box, toggle: shadow.querySelector(".toggle"), status: shadow.querySelector(".status") };
    renderActive();
  }

  function renderActive() {
    if (!panel) return;
    panel.box.classList.toggle("on", active);
    panel.box.classList.toggle("off", !active);
    panel.toggle.textContent = active ? "Stop using tab" : "Relay this tab";
    if (!active) panel.status.textContent = "This tab is not relaying.";
  }

  function setStatus(text) {
    if (panel && active) panel.status.textContent = text;
  }

  function renderIdle(job) {
    const runtimes = job.runtimes || [];
    if (job.reason) return setStatus(job.reason);
    if (!runtimes.length) return setStatus("Idle. Start a run: prelay start <project>");
    setStatus(runtimes.map((r) => `${r.project}: ${r.status}${r.reason ? " — " + r.reason : ""}`).join("\n"));
  }

  function setActive(value) {
    active = value;
    sessionStorage.setItem(ACTIVE_KEY, value ? "1" : "0");
    renderActive();
    if (active) run();
  }

  chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
    if (sender.id !== chrome.runtime.id) return false;
    if (message?.type === "relay-activate") {
      if (message.project !== undefined) sessionStorage.setItem(PROJECT_KEY, message.project || "");
      setActive(Boolean(message.active));
      sendResponse({ ok: true, active, lease });
    } else if (message?.type === "relay-tab-state") {
      sendResponse({ active, lease, job: lastJob });
    }
    return false;
  });

  buildPanel();
  connect();
  timingReady.then(() => setInterval(() => { if (active && !busy) run(); }, T.pollMs));
  if (active) run();
})();
