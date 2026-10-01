// Project Relay extension: DOM-free decision logic, shared by content.js and
// the Node unit tests (tests/js/extension-core.test.mjs).
(function (root) {
  "use strict";

  const PROVISIONAL_USER_TURN = "group:user:pending-chatgpt-submit";

  // ChatGPT wording varies; these match the observed "too long" notices.
  const LIMIT_RE = new RegExp(
    [
      "conversation is too long",
      "maximum (?:context )?length for this conversation",
      "reached the maximum length",
      "this conversation has reached",
      "start a new chat to continue",
      "exceed the length limit",
      "exceeds? the maximum length",
      "conversation has reached its maximum length",
    ].join("|"),
    "i",
  );

  // Usage caps (claude.ai "out of messages", ChatGPT plan limits that block sending).
  const USAGE_LIMIT_RE = new RegExp(
    [
      "out of (?:free )?messages",
      "message limit (?:reached|exceeded)",
      "usage limit",
      "you've reached your limit",
      "you have reached your limit",
    ].join("|"),
    "i",
  );

  const REPLY_ERROR_RE = new RegExp(
    [
      "something went wrong",
      "error (?:occurred )?while generating",
      "there was an error generating",
      "network error",
      "something seems to have gone wrong",
    ].join("|"),
    "i",
  );

  const AUTH_RE = /\b(log in|sign up|sign in)\b/i;

  function isProvisional(id) {
    // Optimistic pre-server keys: exactly the observed one, or the same family.
    return id === PROVISIONAL_USER_TURN || /^pending-/i.test(String(id || "").split(":").pop());
  }

  function groupKey(id) {
    const match = /^group:(?:user|assistant):(.+)$/.exec(String(id || ""));
    return match ? match[1] : null;
  }

  // claude.ai turns are identified by position: claude:user:<n> / claude:assistant:<n>.
  function claudeIndex(id) {
    const match = /^claude:(?:user|assistant):(\d+)$/.exec(String(id || ""));
    return match ? Number(match[1]) : null;
  }

  // Claude turns found by content are named after the request that sent them:
  // claude:user:r<request> / claude:assistant:r<request>.
  function claudeRequestTurn(requestId, role) {
    return `claude:${role}:r${String(requestId || "").replace(/^req-/, "")}`;
  }

  function isClaudeRequestTurn(id) {
    return /^claude:(?:user|assistant):r[0-9A-Za-z-]+$/.test(String(id || ""));
  }

  function expectedAssistantId(userTurnId) {
    const key = groupKey(userTurnId);
    if (key) return `group:assistant:${key}`;
    if (isClaudeRequestTurn(userTurnId)) return String(userTurnId).replace(/^claude:user:/, "claude:assistant:");
    const index = claudeIndex(userTurnId);
    return index === null ? null : `claude:assistant:${index}`;
  }

  // New durable user turns since the pre-Send baseline. Grouped identities
  // come from the logical id set (both roles exist per data-turn-key even when
  // virtualization unmounts a bubble); legacy standalone ids need a user role.
  function newUserTurns(baselineIds, inventory) {
    const baseline = new Set(baselineIds || []);
    const grouped = (inventory.ids || []).filter(
      (id) => (id.startsWith("group:user:") || id.startsWith("claude:user:")) && !baseline.has(id)
        && !isProvisional(id),
    );
    const legacy = (inventory.legacyUsers || []).filter(
      (id) => !baseline.has(id) && !isProvisional(id),
    );
    return [...new Set([...grouped, ...legacy])];
  }

  function normalizeText(text) {
    return String(text || "").replace(/ /g, " ").replace(/\s+/g, " ").trim();
  }

  // Same non-whitespace characters in the same order. ChatGPT's editor
  // renders URLs as links and reads back an extra space before them, so
  // whitespace is not compared; any added, missing or changed visible
  // character still fails.
  const INVISIBLE_RE = /[\s\u00a0\u200b-\u200d\u2060\ufeff]+/g;

  function composerMatches(composerText, prompt) {
    const a = String(composerText || "").replace(INVISIBLE_RE, "");
    const b = String(prompt || "").replace(INVISIBLE_RE, "");
    return a.length > 0 && a === b;
  }

  function classifyNotice(text) {
    const value = String(text || "");
    if (LIMIT_RE.test(value)) return "CONVERSATION_LIMIT";
    if (USAGE_LIMIT_RE.test(value)) return "USAGE_LIMIT";
    if (REPLY_ERROR_RE.test(value)) return "REPLY_FAILED";
    return null;
  }

  // Completion tracker: combined evidence, never a single selector.
  //   text non-empty AND not generating for >= stoppedMs AND text unchanged
  //   for >= stableMs AND the turn's action row is present.
  // There is deliberately no stability-only fallback by default: a
  // "thinking" status line or a code block that is still loading can stay
  // unchanged for a long time, and running a half-written command is worse
  // than waiting (a stuck reply ends in REPLY_TIMEOUT, not a guess).
  function createCompletionTracker(options = {}) {
    const stoppedMs = options.stoppedMs ?? 1500;
    const stableMs = options.stableMs ?? 2500;
    const fallbackStableMs = options.fallbackStableMs ?? Infinity;
    let lastText = null;
    let stableSince = null;
    let stoppedSince = null;

    return function observe({ now, text, generating, actionsVisible }) {
      if (text !== lastText || stableSince === null) {
        lastText = text;
        stableSince = now;
      }
      if (generating) {
        stoppedSince = null;
      } else if (stoppedSince === null) {
        stoppedSince = now;
      }
      const stable = now - stableSince;
      const stopped = stoppedSince === null ? 0 : now - stoppedSince;
      const done =
        Boolean(text && text.trim()) &&
        !generating &&
        stopped >= stoppedMs &&
        stable >= stableMs &&
        (actionsVisible || stable >= fallbackStableMs);
      return { done, stable, stopped };
    };
  }

  function siteOf(url) {
    try {
      const host = new URL(url).hostname;
      if (/^(www\.)?chatgpt\.com$/.test(host)) return "chatgpt";
      if (host === "claude.ai") return "claude";
    } catch (_) {}
    return null;
  }

  function conversationIdFromUrl(url) {
    try {
      const parsed = new URL(url);
      const site = siteOf(url);
      const pattern = site === "chatgpt" ? /\/c\/([0-9A-Za-z-]{8,})(?:\/|$)/
        : site === "claude" ? /^\/chat\/([0-9A-Za-z-]{8,})(?:\/|$)/ : null;
      const match = pattern && pattern.exec(parsed.pathname);
      return match ? match[1] : null;
    } catch (_) {
      return null;
    }
  }

  function isNewChatUrl(url) {
    try {
      const parsed = new URL(url);
      const site = siteOf(url);
      if (site === "claude") return parsed.pathname === "/new";
      return (
        site === "chatgpt" &&
        (parsed.pathname === "/" || parsed.pathname === "") &&
        parsed.searchParams.get("temporary-chat") !== "true"
      );
    } catch (_) {
      return false;
    }
  }

  // A code block language from class names / attributes / header label.
  // Unknown stays "" so the daemon's shell heuristic decides; defaulting to
  // bash would execute e.g. an unlabeled Python snippet.
  function codeLanguage({ className, dataLanguage, headerText }) {
    const fromClass = /language-([A-Za-z0-9_+-]+)/.exec(String(className || ""));
    if (fromClass) return fromClass[1].toLowerCase();
    if (dataLanguage && /^[A-Za-z0-9_+-]{1,20}$/.test(dataLanguage)) return dataLanguage.toLowerCase();
    const header = String(headerText || "").trim().split(/\s+/)[0] || "";
    if (/^(bash|sh|shell|zsh|console|python|py|json|text|javascript|js|ts|typescript|yaml|diff|sql|html|css)$/i.test(header)) {
      return header.toLowerCase();
    }
    return "";
  }

  const api = {
    PROVISIONAL_USER_TURN,
    AUTH_RE,
    isProvisional,
    groupKey,
    claudeIndex,
    claudeRequestTurn,
    isClaudeRequestTurn,
    siteOf,
    expectedAssistantId,
    newUserTurns,
    normalizeText,
    composerMatches,
    classifyNotice,
    createCompletionTracker,
    conversationIdFromUrl,
    isNewChatUrl,
    codeLanguage,
  };

  root.RelayCore = api;
  if (typeof module === "object" && module.exports) module.exports = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
