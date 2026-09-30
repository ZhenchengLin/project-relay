// End-to-end: real extension + real prelayd + real Bash, mock chatgpt.com.
//
// Every chatgpt.com request is answered locally from mock-chatgpt.html and
// every other external request is aborted, so nothing reaches the network.
// Runs in a throwaway Chrome profile (never the user's or Relay's profile).
//
// usage: node run-e2e.mjs --port P --token T --ext DIR --profile DIR --report FILE
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
const bash = (cmd) => `Next step.\n\n${FENCE}bash\n${cmd}\n${FENCE}\n`;

const mockHtml = await readFile(path.join(here, "mock-chatgpt.html"), "utf8");
const sends = [];
let dropped = false;

// Content-based script so the scenario is robust to where rollovers land.
function plan(prompt) {
  if (prompt.includes("Relay will continue this exact work in a NEW chat")) {
    return { text: "RELAY_HANDOFF\nGoal: demo project. Step one is committed. Continue from the pending evidence." };
  }
  if (prompt.includes("fixed-output")) return { text: "All steps verified.\n\nRELAY_DONE" };
  if (prompt.includes("loop-try")) return { text: bash("echo fixed-output") };
  if (prompt.includes("committed")) {
    if (!dropped) {
      dropped = true;
      return { mode: "drop" };
    }
    return { text: bash("echo loop-try; exit 1") };
  }
  if (prompt.includes("Build the demo")) {
    return {
      text: bash(
        "echo step-one > one.txt && git add one.txt && "
        + "git -c user.email=relay@test -c user.name=relay commit -qm one && echo committed",
      ),
    };
  }
  return { text: "Unrecognized prompt; nothing to run.\n\nRELAY_DONE" };
}

async function daemon(pathname) {
  try {
    const res = await fetch(`http://127.0.0.1:${args.port}${pathname}`, {
      headers: { "X-Relay-Token": args.token }, signal: AbortSignal.timeout(5000),
    });
    return await res.json();
  } catch (error) {
    return { error: String(error) };
  }
}

// Never outlive the test, and never leave headless Chrome running.
const hardStop = setTimeout(() => {
  try { chromeProcess.kill("SIGKILL"); } catch (_) {}
  process.exit(3);
}, Number(args.timeout || 240000) + 60000);

// Branded Chrome ignores --load-extension, so launch it with remote
// debugging on the throwaway profile and load the unpacked extension over
// CDP (Extensions.loadUnpacked needs --enable-unsafe-extension-debugging).
const CHROME = process.env.CHROME_PATH
  || (process.platform === "darwin" ? "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" : "google-chrome");
const chromeProcess = spawn(CHROME, [
  `--user-data-dir=${args.profile}`,
  "--remote-debugging-port=0",
  "--enable-unsafe-extension-debugging",
  "--headless=new",
  "--no-first-run",
  "--no-default-browser-check",
  "--disable-sync",
  "about:blank",
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
  if (url.hostname === "127.0.0.1") return route.continue();
  if (!/^(www\.)?chatgpt\.com$/.test(url.hostname)) return route.abort();
  if (url.pathname === "/__mock/reply") {
    const body = JSON.parse(route.request().postData() || "{}");
    const decision = plan(body.prompt);
    sends.push({ at: Date.now(), path: body.path, model: body.model, mode: decision.mode || "ok",
                 head: body.prompt.slice(0, 160) });
    return route.fulfill({ contentType: "application/json", body: JSON.stringify(decision) });
  }
  if (route.request().resourceType() === "document") {
    return route.fulfill({ contentType: "text/html", body: mockHtml });
  }
  return route.fulfill({ status: 404, body: "" });
});

let worker = context.serviceWorkers().find((w) => w.url().includes(loaded.id));
for (let i = 0; !worker && i < 60; i++) {
  await new Promise((r) => setTimeout(r, 250));
  worker = context.serviceWorkers().find((w) => w.url().includes(loaded.id));
}
if (!worker) throw new Error("extension service worker did not start");

const page = await context.newPage();
await page.goto("https://chatgpt.com/");
await page.evaluate(() => sessionStorage.setItem("projectRelayActive", "1"));
await page.reload();

const deadline = Date.now() + Number(args.timeout || 240000);
let final = null;
let lastStatus = null;
while (Date.now() < deadline) {
  const status = await daemon("/v2/status");
  const rt = status.runtimes?.[0];
  lastStatus = rt;
  if (rt && !["RUNNING"].includes(rt.status)) {
    final = rt;
    break;
  }
  await new Promise((r) => setTimeout(r, 500));
}

const withTimeout = (promise, fallback) =>
  Promise.race([promise, new Promise((resolve) => setTimeout(() => resolve(fallback), 5000))]);
const storage = await withTimeout(page.evaluate(() => localStorage.getItem("mock-convs")), null);
const decoyValue = await withTimeout(page.evaluate(() => document.getElementById("decoy")?.value ?? null), null);
const trace = await withTimeout(page.evaluate(() => sessionStorage.getItem("projectRelayTrace")), null);
const panelText = await page.evaluate(() => document.title);
await writeFile(args.report, JSON.stringify({
  final, lastStatus, trace: JSON.parse(trace || "[]"), sends, dropped, mockConversations: JSON.parse(storage || "{}"), url: page.url(), panelText, decoyValue,
  extensionWorker: worker.url(),
}, null, 2));
await withTimeout(browser.close(), null);
chromeProcess.kill("SIGKILL");
clearTimeout(hardStop);
console.log(final ? `E2E_FINAL=${final.status}` : "E2E_TIMEOUT");
