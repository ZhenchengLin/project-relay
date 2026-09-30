// Relay ADE end-to-end: real extension + real prelayd + real Bash/Git,
// mock claude.ai (PM) and mock chatgpt.com (Worker) in two tabs.
//
// Every claude.ai / chatgpt.com request is answered locally from the mock
// pages; every other external request is aborted. Throwaway Chrome profile.
//
// usage: node run-e2e-ade.mjs --port P --token T --ext DIR --profile DIR --report FILE
import { spawn } from "node:child_process";
import { readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright-core";

const here = path.dirname(fileURLToPath(import.meta.url));
const args = Object.fromEntries(
  process.argv.slice(2).reduce((pairs, value, i, all) => {
    if (value.startsWith("--")) pairs.push([value.slice(2), all[i + 1]]);
    return pairs;
  }, []),
);
const FENCE = "`".repeat(3);
const pages = {
  claude: await readFile(path.join(here, "mock-claude.html"), "utf8"),
  chatgpt: await readFile(path.join(here, "mock-chatgpt.html"), "utf8"),
};
const sends = [];

// PM (Claude): plan -> approve the risky commit -> done once evidence shows it.
function planPm(prompt) {
  if (prompt.includes("Relay will not run it until you decide")) {
    return { text: "The command only adds and commits hello.txt.\n\nRELAY_APPROVE" };
  }
  if (prompt.includes("Result of the Worker's command") && prompt.includes("add hello")) {
    return { text: "The commit is in the log. Goal met.\n\nRELAY_PLAN\n- [x] T1 Create hello.txt\n- [x] T2 Commit it\n"
      + "END_RELAY_PLAN\n\nRELAY_DONE" };
  }
  if (prompt.includes("Project Relay ADE is starting")) {
    return {
      text: "Plan: one small commit.\n\nRELAY_NOTE: commit only the files a task names\n\n"
        + "RELAY_PLAN\n- [~] T1 Create hello.txt\n- [ ] T2 Commit it\nEND_RELAY_PLAN\n\n"
        + "RELAY_TASK\nCreate hello.txt containing the word hello, "
        + "commit only that file with the message 'add hello', then print git log -1 --oneline.\nEND_RELAY_TASK",
    };
  }
  return { text: "RELAY_ASK_HUMAN: unexpected prompt in the mock" };
}

// Worker (ChatGPT): one bash block per task.
function planWorker(prompt) {
  if (prompt.includes("Task from the PM") && prompt.includes("hello.txt")) {
    return {
      text: `Here is the command.\n\n${FENCE}bash\necho hello > hello.txt && git add hello.txt && `
        + `git -c user.email=relay@test -c user.name=relay commit -qm "add hello" && git log -1 --oneline\n${FENCE}\n`,
      thinkMs: 1500,
    };
  }
  return { text: "I cannot tell what to do." };
}

async function daemon(pathname) {
  const res = await fetch(`http://127.0.0.1:${args.port}${pathname}`, { headers: { "X-Relay-Token": args.token } });
  return res.json();
}

const CHROME = process.env.CHROME_PATH
  || (process.platform === "darwin" ? "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" : "google-chrome");
const chromeProcess = spawn(CHROME, [
  `--user-data-dir=${args.profile}`, "--remote-debugging-port=0", "--enable-unsafe-extension-debugging",
  "--headless=new", "--no-first-run", "--no-default-browser-check", "--disable-sync", "about:blank",
], { stdio: ["ignore", "ignore", "pipe"] });

const devtoolsUrl = await new Promise((resolve, reject) => {
  let buffer = "";
  const timer = setTimeout(() => reject(new Error("Chrome did not expose DevTools")), 20000);
  chromeProcess.stderr.on("data", (chunk) => {
    buffer += chunk;
    const match = /DevTools listening on (ws:\/\/\S+)/.exec(buffer);
    if (match) {
      clearTimeout(timer);
      resolve(match[1]);
    }
  });
  chromeProcess.on("exit", (code) => reject(new Error(`Chrome exited ${code}`)));
});

const browser = await chromium.connectOverCDP(devtoolsUrl);
const cdp = await browser.newBrowserCDPSession();
const loaded = await cdp.send("Extensions.loadUnpacked", { path: args.ext });
const context = browser.contexts()[0];

await context.route("**/*", async (route) => {
  const url = new URL(route.request().url());
  if (url.hostname === "127.0.0.1" || url.protocol === "chrome-extension:") return route.continue();
  const site = url.hostname === "claude.ai" ? "claude" : /^(www\.)?chatgpt\.com$/.test(url.hostname) ? "chatgpt" : null;
  if (!site) return route.abort();
  if (url.pathname === "/__mock/reply") {
    const body = JSON.parse(route.request().postData() || "{}");
    const decision = site === "claude" ? planPm(body.prompt) : planWorker(body.prompt);
    sends.push({ at: Date.now(), site, path: body.path, head: body.prompt.slice(0, 120) });
    return route.fulfill({ contentType: "application/json", body: JSON.stringify(decision) });
  }
  if (route.request().resourceType() === "document") {
    return route.fulfill({ contentType: "text/html", body: pages[site] });
  }
  return route.fulfill({ status: 404, body: "" });
});

let worker = context.serviceWorkers().find((w) => w.url().includes(loaded.id));
for (let i = 0; !worker && i < 60; i++) {
  await new Promise((r) => setTimeout(r, 250));
  worker = context.serviceWorkers().find((w) => w.url().includes(loaded.id));
}
if (!worker) throw new Error("extension service worker did not start");

async function relayTab(url) {
  const page = await context.newPage();
  await page.goto(url);
  await page.evaluate(() => {
    sessionStorage.setItem("projectRelayActive", "1");
    sessionStorage.setItem("projectRelayProject", "demo");
  });
  await page.reload();
  return page;
}
const pmPage = await relayTab("https://claude.ai/new");
const workerPage = await relayTab("https://chatgpt.com/");

const deadline = Date.now() + Number(args.timeout || 240000);
let final = null;
let pmReloaded = false;
while (Date.now() < deadline) {
  // Reload the Claude tab once the Worker has its task: the PM's next message
  // (the review) must then be sent and read on a page whose earlier replies
  // come from history, as after any reload on claude.ai.
  if (!pmReloaded && sends.some((s) => s.site === "chatgpt")) {
    pmReloaded = true;
    await pmPage.reload();
  }
  const status = await daemon("/v2/status");
  const rt = status.runtimes?.[0];
  if (rt && rt.status !== "RUNNING") {
    final = rt;
    break;
  }
  await new Promise((r) => setTimeout(r, 500));
}

// The pinned dashboard: must render the finished run without script errors.
const dashboard = await context.newPage();
const consoleErrors = [];
dashboard.on("pageerror", (err) => consoleErrors.push(String(err)));
dashboard.on("console", (msg) => { if (msg.type() === "error") consoleErrors.push(msg.text()); });
await dashboard.setViewportSize({ width: 1280, height: 1600 });
await dashboard.goto(`chrome-extension://${loaded.id}/dashboard.html`);
await dashboard.waitForTimeout(3500);
const dashboardText = await dashboard.evaluate(() => document.body.innerText);
if (args.screenshot) await dashboard.screenshot({ path: args.screenshot, fullPage: true });

await writeFile(args.report, JSON.stringify({
  final, sends, dashboardText, consoleErrors, pmReloaded,
  claudeStore: JSON.parse((await pmPage.evaluate(() => localStorage.getItem("mock-claude-convs"))) || "{}"),
  chatgptStore: JSON.parse((await workerPage.evaluate(() => localStorage.getItem("mock-convs"))) || "{}"),
  pmUrl: pmPage.url(), workerUrl: workerPage.url(),
}, null, 2));
await browser.close();
chromeProcess.kill();
console.log(final ? `E2E_FINAL=${final.status}` : "E2E_TIMEOUT");
