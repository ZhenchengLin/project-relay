import test from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const Core = require("../../project_relay/relay/extension/relay-core.js");

test("pending-chatgpt-submit is never a durable user turn", () => {
  const baseline = ["group:user:a", "group:assistant:a"];
  const inventory = {
    ids: [...baseline, "group:user:pending-chatgpt-submit", "group:assistant:pending-chatgpt-submit"],
    legacyUsers: [],
  };
  assert.deepEqual(Core.newUserTurns(baseline, inventory), []);
  assert.equal(Core.isProvisional("group:user:pending-chatgpt-submit"), true);
  assert.equal(Core.isProvisional("group:user:pending-foo"), true);
  assert.equal(Core.isProvisional("group:user:9f2c-pending"), false);
});

test("exactly one new stable grouped user turn is found from logical ids", () => {
  const baseline = ["group:user:a", "group:assistant:a"];
  const inventory = { ids: [...baseline, "group:user:b", "group:assistant:b"], legacyUsers: [] };
  assert.deepEqual(Core.newUserTurns(baseline, inventory), ["group:user:b"]);
});

test("multiple new user turns are all reported (caller fails closed)", () => {
  const inventory = { ids: ["group:user:b", "group:user:c"], legacyUsers: [] };
  assert.equal(Core.newUserTurns([], inventory).length, 2);
});

test("legacy standalone user turns are detected", () => {
  const inventory = { ids: ["t1", "t2", "t3"], legacyUsers: ["t1", "t3"] };
  assert.deepEqual(Core.newUserTurns(["t1", "t2"], inventory), ["t3"]);
});

test("assistant identity is derived from the same group key", () => {
  assert.equal(Core.expectedAssistantId("group:user:K1"), "group:assistant:K1");
  assert.equal(Core.expectedAssistantId("legacy-id"), null);
  assert.equal(Core.groupKey("group:assistant:K1"), "K1");
});

test("composer comparison ignores whitespace differences only", () => {
  assert.ok(Core.composerMatches("a  b\n\nc", "a b c"));
  assert.ok(!Core.composerMatches("a b", "a b c"));
  // Live ChatGPT reads back a space before an auto-linked URL.
  assert.ok(Core.composerMatches('"eventStatus": " http://schema.org/x",', '"eventStatus": "http://schema.org/x",'));
  assert.ok(Core.composerMatches("a\u200bb", "ab"));
  assert.ok(!Core.composerMatches('"eventStatus": "https://schema.org/x",', '"eventStatus": "http://schema.org/x",'));
  assert.ok(!Core.composerMatches("", ""));
});

test("notice classification", () => {
  assert.equal(Core.classifyNotice("The conversation is too long, please start a new one."), "CONVERSATION_LIMIT");
  assert.equal(Core.classifyNotice("You've reached the maximum length for this conversation."), "CONVERSATION_LIMIT");
  assert.equal(Core.classifyNotice("Something went wrong. Retry"), "REPLY_FAILED");
  assert.equal(Core.classifyNotice("All tests passed"), null);
});

test("completion requires stop, stability and action row (or long stability)", () => {
  const observe = Core.createCompletionTracker({ stoppedMs: 1000, stableMs: 2000, fallbackStableMs: 6000 });
  assert.equal(observe({ now: 0, text: "a", generating: true, actionsVisible: false }).done, false);
  assert.equal(observe({ now: 500, text: "ab", generating: true, actionsVisible: false }).done, false);
  assert.equal(observe({ now: 1000, text: "abc", generating: false, actionsVisible: false }).done, false);
  assert.equal(observe({ now: 2500, text: "abc", generating: false, actionsVisible: false }).done, false);
  assert.equal(observe({ now: 3100, text: "abc", generating: false, actionsVisible: true }).done, true);

  const fallback = Core.createCompletionTracker({ stoppedMs: 1000, stableMs: 2000, fallbackStableMs: 6000 });
  fallback({ now: 0, text: "x", generating: false, actionsVisible: false });
  assert.equal(fallback({ now: 5000, text: "x", generating: false, actionsVisible: false }).done, false);
  assert.equal(fallback({ now: 6000, text: "x", generating: false, actionsVisible: false }).done, true);

  const empty = Core.createCompletionTracker();
  assert.equal(empty({ now: 0, text: "", generating: false, actionsVisible: true }).done, false);
  assert.equal(empty({ now: 99999, text: "", generating: false, actionsVisible: true }).done, false);
});

test("text change resets stability", () => {
  const observe = Core.createCompletionTracker({ stoppedMs: 0, stableMs: 1000, fallbackStableMs: 1000 });
  observe({ now: 0, text: "a", generating: false, actionsVisible: true });
  assert.equal(observe({ now: 900, text: "ab", generating: false, actionsVisible: true }).done, false);
  assert.equal(observe({ now: 1800, text: "ab", generating: false, actionsVisible: true }).done, false);
  assert.equal(observe({ now: 1900, text: "ab", generating: false, actionsVisible: true }).done, true);
});

test("conversation URLs", () => {
  assert.equal(Core.conversationIdFromUrl("https://chatgpt.com/c/0f0f0f0f-1111-4222"), "0f0f0f0f-1111-4222");
  assert.equal(Core.conversationIdFromUrl("https://chatgpt.com/g/g-p-abc/c/0f0f0f0f-1111?x=1"), "0f0f0f0f-1111");
  assert.equal(Core.conversationIdFromUrl("https://evil.com/c/0f0f0f0f-1111"), null);
  assert.equal(Core.isNewChatUrl("https://chatgpt.com/"), true);
  assert.equal(Core.isNewChatUrl("https://chatgpt.com/?temporary-chat=true"), false);
  assert.equal(Core.isNewChatUrl("https://chatgpt.com/c/abcdefgh"), false);
});

test("unknown code language is not assumed to be bash", () => {
  assert.equal(Core.codeLanguage({ className: "hljs language-bash" }), "bash");
  assert.equal(Core.codeLanguage({ headerText: "python Copy code" }), "python");
  assert.equal(Core.codeLanguage({ className: "", headerText: "Copy" }), "");
});

test("default tracker never completes on stability alone", () => {
  const observe = Core.createCompletionTracker();
  observe({ now: 0, text: "规划验证流程", generating: false, actionsVisible: false });
  assert.equal(observe({ now: 10 * 60 * 1000, text: "规划验证流程", generating: false, actionsVisible: false }).done, false);
  assert.equal(observe({ now: 10 * 60 * 1000 + 1, text: "规划验证流程", generating: false, actionsVisible: true }).done, true);
});

test("claude.ai urls and positional turn identities", () => {
  assert.equal(Core.siteOf("https://claude.ai/chat/abc12345-0000"), "claude");
  assert.equal(Core.conversationIdFromUrl("https://claude.ai/chat/abc12345-0000?x=1"), "abc12345-0000");
  assert.equal(Core.conversationIdFromUrl("https://claude.ai/project/abc12345-0000"), null);
  assert.equal(Core.isNewChatUrl("https://claude.ai/new"), true);
  assert.equal(Core.isNewChatUrl("https://claude.ai/recents"), false);
  assert.equal(Core.expectedAssistantId("claude:user:4"), "claude:assistant:4");
  assert.equal(Core.claudeIndex("claude:assistant:12"), 12);
  const baseline = ["claude:user:0", "claude:assistant:0"];
  assert.deepEqual(Core.newUserTurns(baseline, { ids: [...baseline, "claude:user:1"], legacyUsers: [] }), ["claude:user:1"]);
});

test("usage limit notices", () => {
  assert.equal(Core.classifyNotice("You are out of free messages until 3 PM"), "USAGE_LIMIT");
  assert.equal(Core.classifyNotice("Your message will exceed the length limit for this chat."), "CONVERSATION_LIMIT");
});
