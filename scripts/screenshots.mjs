// Documentation screenshots of the web console, taken against a running demo
// (scripts/console-demo.sh). Drives a separate headless Chrome with a throwaway
// profile over the DevTools protocol; no dependencies (Node 22 WebSocket + fetch).
//
//   DEMO=/tmp/relay-demo scripts/console-demo.sh        # in one terminal
//   node scripts/screenshots.mjs <console-url> docs/images
//
// Nothing is submitted: the screens are only opened and, where shown, typed into.
import { spawn } from "node:child_process";
import { mkdtempSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const [URL0, OUT] = process.argv.slice(2);
if (!URL0 || !OUT) { console.error("usage: screenshots.mjs <url> <outdir>"); process.exit(2); }
const CHROME = process.env.CHROME || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const PORT = 9300 + Math.floor(Math.random() * 500);
const W = 1280, H = 860, SCALE = 2;
const profile = mkdtempSync(join(tmpdir(), "relay-chrome-"));
const chrome = spawn(CHROME, ["--headless=new", `--remote-debugging-port=${PORT}`, `--user-data-dir=${profile}`,
  "--no-first-run", "--no-default-browser-check", "--disable-extensions", "--hide-scrollbars",
  `--window-size=${W},${H}`, "about:blank"], { stdio: "ignore" });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let ws, nextId = 1;
const pending = new Map();

async function connect() {
  for (let i = 0; i < 100; i++) {
    try {
      const page = (await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json()).find((t) => t.type === "page");
      if (page) {
        // Attach the handlers before yielding, or the open event can be missed.
        ws = new WebSocket(page.webSocketDebuggerUrl);
        await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
        break;
      }
    } catch (_) {}
    await sleep(100);
  }
  if (!ws) throw new Error("could not reach headless Chrome");
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.id && pending.has(m.id)) { const { res, rej } = pending.get(m.id); pending.delete(m.id); m.error ? rej(new Error(m.error.message)) : res(m.result); }
  };
}
const cdp = (method, params = {}) => new Promise((res, rej) => {
  const id = nextId++; pending.set(id, { res, rej }); ws.send(JSON.stringify({ id, method, params }));
});
async function js(expr) {
  const r = await cdp("Runtime.evaluate", { expression: expr, awaitPromise: true, returnByValue: true });
  if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description || r.exceptionDetails.text);
  return r.result.value;
}
async function waitFor(expr, what, timeout = 60000) {
  const end = Date.now() + timeout;
  while (Date.now() < end) { try { if (await js(expr)) return; } catch (_) {} await sleep(200); }
  throw new Error(`timed out waiting for: ${what}`);
}
async function size(width, height, mobile = false) {
  await cdp("Emulation.setDeviceMetricsOverride", { width, height, deviceScaleFactor: SCALE, mobile });
}
async function shot(name) {
  await sleep(700);
  const { data } = await cdp("Page.captureScreenshot", { format: "png" });
  writeFileSync(join(OUT, name), Buffer.from(data, "base64"));
  console.log("saved", name);
}
const go = async (hash) => { await js(`location.hash = ${JSON.stringify(hash)}; true`); await sleep(300); };
const theme = (t) => js(`(() => { document.documentElement.dataset.theme = ${JSON.stringify(t)};
  try { localStorage.setItem("relay-theme", ${JSON.stringify(t)}); } catch (_) {} return true; })()`);
const rowFor = (folder) => `[...document.querySelectorAll(".rowbtn")].find(b => b.querySelector(".nm").textContent.startsWith(${JSON.stringify(folder)} + " · "))`;
const button = (label, scope = "document") => `[...${scope}.querySelectorAll("button")].find(b => b.textContent.startsWith(${JSON.stringify(label)}))`;
const type = (sel, value) => js(`(() => { const t = document.querySelector(${JSON.stringify(sel)}); t.value = ${JSON.stringify(value)};
  t.dispatchEvent(new Event("input")); t.blur(); return true; })()`);
const PANEL = `[...document.querySelectorAll(".panel")].find(p => (p.querySelector("h3")||{}).textContent?.startsWith("Who manages"))`;
const openRow = async (folder, ready, what) => {
  await go("#/"); await waitFor(rowFor(folder), `${folder} row`);
  await js(`${rowFor(folder)}.click(); true`);
  await waitFor(ready, what);
};

let failed = false;
try {
  await connect();
  await cdp("Page.enable"); await cdp("Runtime.enable");
  await size(W, H);
  await cdp("Page.navigate", { url: URL0 });
  await waitFor(`document.querySelectorAll(".rowbtn").length >= 5`, "session rows");
  await waitFor(`${rowFor("docs-site")} && ${rowFor("docs-site")}.textContent.includes("Needs you")`, "docs-site needs you", 90000);

  for (const t of ["dark", "light"]) {
    await theme(t);
    await go("#/"); await shot(`home-${t}.png`);
  }
  await theme("dark");

  const summary = `location.hash.startsWith("#/t/") && !!document.querySelector(".summary") && !document.querySelector(".summary").hidden`;
  await openRow("billing-service", summary, "billing summary");
  await waitFor(`document.querySelector(".summary").textContent.includes("reports")`, "a progress report");
  await shot("summary.png");

  await js(`${button("Conversation", 'document.querySelector(".tabs")')}.click(); true`);
  await waitFor(`document.querySelectorAll(".msg").length >= 2`, "conversation messages");
  await shot("conversation.png");
  await js(`${button("Summary", 'document.querySelector(".tabs")')}.click(); true`);

  await openRow("docs-site", `location.hash.startsWith("#/t/") && !!document.querySelector(".compose textarea")`, "docs-site question");
  await type(".compose textarea", "Both, please. Keep the examples short.");
  await shot("decision.png");

  await openRow("payments-api", `!!${button("Manage with AI")}`, "Manage with AI");
  await js(`${button("Manage with AI")}.click(); true`);
  await waitFor(`!!${PANEL}`, "manager setup");
  await js(`${button("A new session", PANEL)}.click(); true`);
  await js(`${button("Codex", PANEL)}.click(); true`);
  await js(`${button("Planner", PANEL)}.click(); true`);
  await js(`(() => { const t = ${PANEL}.querySelector("textarea"); t.value = "Tighten the timeout handling and add a test for slow responses.";
    t.dispatchEvent(new Event("input")); t.blur(); ${PANEL}.scrollIntoView({ block: "center" }); return true; })()`);
  await shot("manage.png");

  await go("#/events");
  await waitFor(`document.querySelectorAll(".events button.e").length > 5`, "event rows");
  await js(`[...document.querySelectorAll(".events button.e")].find(b => b.textContent.includes("dispatch_authorized")).click(); true`);
  await waitFor(`!!document.querySelector(".e-full")`, "expanded record");
  const live = `[...document.querySelectorAll("button")].find(b => b.textContent.trim() === "Live")`;
  await js(`(${live})?.click(); true`);  // pause live updates so the record stays as edited below
  await sleep(500);
  // Records carry the real machine name in `actor.host`; show a neutral one in published images.
  await js(`(() => { const w = document.createTreeWalker(document.querySelector(".events"), NodeFilter.SHOW_TEXT);
    for (let n; (n = w.nextNode());) n.nodeValue = n.nodeValue.replace(/("host": ?")[^"]+(")/g, "$1my-mac.local$2");
    return true; })()`);
  await sleep(1500);
  if (!(await js(`document.querySelector(".events").textContent.includes("my-mac.local")`)) ||
      (await js(`/"host": ?"(?!my-mac\\.local)/.test(document.querySelector(".events").textContent)`)))
    throw new Error("the machine name is still visible on the events page");
  await shot("events.png");

  await size(390, 844, true);
  await openRow("billing-service", summary, "billing summary (mobile)");
  await shot("mobile.png");
} catch (e) {
  failed = true;
  console.error("FAILED:", e.message);
} finally {
  try { ws && ws.close(); } catch (_) {}
  chrome.kill();
  await sleep(300);
  rmSync(profile, { recursive: true, force: true });
}
process.exit(failed ? 1 : 0);
