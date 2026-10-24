// End-to-end browser check of the relay console against a running demo
// (scripts/console-demo.sh). Drives a separate headless Chrome with a throwaway
// profile over the DevTools protocol; no dependencies (Node 22 WebSocket + fetch).
//
//   node scripts/browser-flow.mjs <console-url> <screenshot-dir>
//
// Exits non-zero if any step fails. Screenshots: home.png, home-mobile.png,
// console-question.png, console-control.png, console-summary-working.png,
// console-summary-mobile.png, console-summary-decision.png, events.png.
import { spawn } from "node:child_process";
import { mkdtempSync, writeFileSync, rmSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const [URL0, OUT] = process.argv.slice(2);
if (!URL0 || !OUT) { console.error("usage: browser-flow.mjs <url> <outdir>"); process.exit(2); }
const CHROME = process.env.CHROME || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const PORT = 9300 + Math.floor(Math.random() * 500);
const profile = mkdtempSync(join(tmpdir(), "relay-chrome-"));
const chrome = spawn(CHROME, ["--headless=new", `--remote-debugging-port=${PORT}`, `--user-data-dir=${profile}`,
  "--no-first-run", "--no-default-browser-check", "--disable-extensions", "--window-size=1280,900", "about:blank"],
  { stdio: "ignore" });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const results = [];
const pageErrors = [];
let ws, nextId = 1;
const pending = new Map();

async function connect() {
  for (let i = 0; i < 100; i++) {
    try {
      const list = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
      const page = list.find((t) => t.type === "page");
      if (page) { ws = new WebSocket(page.webSocketDebuggerUrl); break; }
    } catch (_) {}
    await sleep(100);
  }
  if (!ws) throw new Error("could not reach headless Chrome");
  await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.method === "Runtime.consoleAPICalled" && process.env.TRACE) {
      console.log("  [page]", m.params.args.map((a) => a.value ?? a.description).join(" "));
    }
    if (m.method === "Runtime.exceptionThrown") {
      const d = m.params.exceptionDetails;
      pageErrors.push((d.exception && d.exception.description) || d.text);
    }
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
async function waitFor(expr, what, timeout = 30000) {
  const end = Date.now() + timeout;
  while (Date.now() < end) { try { if (await js(expr)) return; } catch (_) {} await sleep(200); }
  throw new Error(`timed out waiting for: ${what}`);
}
async function shot(name, width = 1280, height = 900, mobile = false) {
  await cdp("Emulation.setDeviceMetricsOverride", { width, height, deviceScaleFactor: 1, mobile });
  await sleep(500);
  const { data } = await cdp("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
  writeFileSync(join(OUT, name), Buffer.from(data, "base64"));
}
async function step(name, fn) {
  try { await fn(); results.push(["PASS", name]); console.log("PASS", name); }
  catch (e) { results.push(["FAIL", name, e.message]); console.log("FAIL", name, "—", e.message); }
}
const go = (hash) => js(`location.hash = ${JSON.stringify(hash)}; true`);
// Bracketed so `${text(sel)}.includes(...)` applies to the text, not to "" (operator precedence).
const text = (sel) => `((document.querySelector(${JSON.stringify(sel)})||{}).textContent||"")`;
// Rows are titled "<folder> · <client>"; find one by folder (and optionally client).
const rowFor = (folder, client) => `[...document.querySelectorAll(".rowbtn")].find(b => { const t = b.querySelector(".nm").textContent;
  return t.startsWith(${JSON.stringify(folder)} + " · ") && (${JSON.stringify(client || "")} === "" || t.endsWith(" · " + ${JSON.stringify(client || "")})); })`;

try {
  await connect();
  await cdp("Page.enable"); await cdp("Runtime.enable");
  await cdp("Emulation.setDeviceMetricsOverride", { width: 1280, height: 900, deviceScaleFactor: 1, mobile: false });
  await cdp("Page.navigate", { url: URL0 });

  await step("home lists sessions with who manages them", async () => {
    await waitFor(`document.querySelectorAll(".rowbtn").length >= 5`, "session rows");
    await waitFor(`${rowFor("docs-site")} && ${rowFor("docs-site")}.textContent.includes("Needs you")`, "docs-site needs you", 40000);
    const t = await js(`document.body.innerText`);
    for (const want of ["Demo mode", "No manager assigned", "Managed by", "payments-api · Claude"]) if (!t.includes(want)) throw new Error(`missing "${want}"`);
    await shot("home.png");
    await shot("home-mobile.png", 390, 844, true);
    await cdp("Emulation.setDeviceMetricsOverride", { width: 1280, height: 900, deviceScaleFactor: 1, mobile: false });
  });

  const clearAsk = () => js(`(() => { const i = document.querySelector(".ask input"); if (i) { i.value = ""; i.dispatchEvent(new Event("input")); } return true; })()`);
  await step("two sessions in agent-relay show different work; missing context is said plainly", async () => {
    await waitFor(`${rowFor("agent-relay", "Codex")} && ${rowFor("agent-relay", "Claude")}`, "both agent-relay rows");
    const cx = await js(`${rowFor("agent-relay", "Codex")}.querySelector(".dg").textContent`);
    const cl = await js(`${rowFor("agent-relay", "Claude")}.querySelector(".dg").textContent`);
    if (cx !== "Architecture and review of session management") throw new Error(`Codex row: ${cx}`);
    if (cl !== "Implementing the dashboard and control refinements") throw new Error(`Claude row: ${cl}`);
    const path = await js(`${rowFor("agent-relay", "Codex")}.querySelector(".who").textContent`);
    if (!path.includes("/projects/agent-relay")) throw new Error(`folder path not on the row: ${path}`);
    const bare = await js(`${rowFor("infra-scripts")}.querySelector(".dg").textContent`);
    if (bare !== "Work summary unavailable") throw new Error(`missing context shown as: ${bare}`);
  });

  await step("search covers folder, title and summary", async () => {
    try {
      for (const [q, want] of [["dashboard", 1], ["agent-relay", 2], ["session manager review", 1]]) {
        await js(`(() => { const i = document.querySelector(".ask input"); i.value = ${JSON.stringify(q)}; i.dispatchEvent(new Event("input")); return true; })()`);
        await waitFor(`document.querySelectorAll(".rowbtn").length === ${want}`, `${want} result(s) for "${q}"`);
        if (q === "agent-relay") await shot("home-agent-relay.png");
      }
    } finally {
      await js(`(() => { const i = document.querySelector(".ask input"); i.value = ""; i.dispatchEvent(new Event("input")); return true; })()`);
    }
  });

  await step("titles with filter words are searched literally; real filters still filter", async () => {
    const ask = (q) => js(`(() => { const i = document.querySelector(".ask input"); i.value = ${JSON.stringify(q)}; i.dispatchEvent(new Event("input")); return true; })()`);
    const names = () => js(`[...document.querySelectorAll(".rowbtn .nm")].map(n => n.textContent)`);
    try {
      // "manager" and "session" are filter/stop words, but this whole phrase is a session's title.
      for (const q of ["session manager review", "Session Manager Review", "\"session manager review\""]) {
        await ask(q);
        await waitFor(`document.querySelectorAll(".rowbtn").length === 1`, `one row for ${q}`);
        const n = await names();
        if (n[0] !== "agent-relay · Codex") throw new Error(`${q} found ${n}`);
        const u = await js(text(".understood"));
        if (!u.includes("matching “session manager review”") || u.includes("managed by AI")) throw new Error(`interpreted as: ${u}`);
      }
      // A quoted filter word is text: only rows whose text says "working", not the working state.
      await ask("\"working\"");
      await waitFor(`${text(".understood")}.includes("matching “working”") && !${text(".understood")}.includes("that are working")`, "quoted word read literally");
      // Explicit filters still filter, typed or via the quick chip.
      await ask("managed by ai");
      await waitFor(`${text(".understood")}.includes("that are managed by AI")`, "managed by ai is a filter");
      const typed = await js(`[...document.querySelectorAll(".rowbtn")].map(b => b.dataset.key)`);
      if (!typed.length) throw new Error("managed by ai found nothing");
      const kinds = await js(`fetch("/api/sessions", { headers: { "X-Relay-Token": new URLSearchParams(location.search).get("t") } })
        .then(r => r.json()).then(d => d.sessions.filter(s => (s.managed_by || {}).kind === "ai").map(s => s.key).sort())`);
      if (JSON.stringify([...typed].sort()) !== JSON.stringify(kinds)) throw new Error(`managed by ai: ${typed} vs ${kinds}`);
      await clearAsk();
      await js(`[...document.querySelectorAll(".chip")].find(c => c.textContent === "Managed by AI").click(); true`);
      await waitFor(`document.querySelectorAll(".rowbtn").length === ${typed.length}`, "quick chip matches typed filter");
    } finally { await clearAsk(); }
  });

  await step("Details show the full path and sources; an edit is saved and survives reload", async () => {
    await js(`${rowFor("infra-scripts")}.click(); true`);
    await waitFor(`location.hash.startsWith("#/s/") && !!document.querySelector("details.details")`, "session page");
    await js(`document.querySelector("details.details").open = true; true`);
    const det = await js(text("details.details"));
    if (!/\/projects\/infra-scripts/.test(det) || !det.includes("Copy")) throw new Error("full path or Copy missing");
    if (!det.includes("Unavailable")) throw new Error("missing context not stated in Details");
    await js(`[...document.querySelectorAll("details.details button")].find(b => b.textContent === "Edit title & summary").click(); true`);
    await js(`(() => { const t = document.querySelector(".edit input"); t.value = "Infra tidy-up";
      const s = document.querySelector(".edit textarea"); s.value = "Removing unused deploy scripts"; return true; })()`);
    await js(`[...document.querySelectorAll(".edit button")].find(b => b.textContent === "Save").click(); true`);
    await waitFor(`${text(".facts.work")}.includes("Removing unused deploy scripts")`, "summary shown after save");
    await js(`location.reload(); true`);
    await waitFor(`${text(".facts.work")}.includes("Removing unused deploy scripts")`, "summary after reload");
    await js(`document.querySelector("details.details").open = true; true`);
    if (!(await js(text("details.details"))).includes("from you")) throw new Error("source not shown");
    await shot("session-details.png");
    await go("#/");
  });

  await step("asking filters the list", async () => {
    try {
      await js(`(() => { const i = document.querySelector(".ask input"); i.value = "which are waiting?"; i.dispatchEvent(new Event("input")); return true; })()`);
      await waitFor(`document.querySelectorAll(".rowbtn").length === 1 && ${rowFor("docs-site")}`, "only docs-site shown");
      const u = await js(text(".understood"));
      if (!u.includes("need you")) throw new Error(`interpretation not shown: ${u}`);
    } finally { await clearAsk(); }
  });

  await step("keyboard focus survives polling on Home", async () => {
    await js(`${rowFor("payments-api")}.focus(); true`);
    await sleep(4500);  // two polls
    const ok = await js(`document.activeElement && document.activeElement.querySelector && ((document.activeElement.querySelector(".nm")||{}).textContent||"").startsWith("payments-api · ")`);
    if (!ok) throw new Error("focus was lost after a refresh");
  });

  await step("summary first: working session, attributed reports, conversation one click away, selection kept", async () => {
    await go("#/"); await waitFor(`${rowFor("billing-service")}`, "billing row");
    await js(`${rowFor("billing-service")}.click(); true`);
    await waitFor(`location.hash.startsWith("#/t/") && !!document.querySelector(".summary") && !document.querySelector(".summary").hidden`, "summary view");
    if (!(await js(`document.querySelector(".chat").hidden`))) throw new Error("conversation shown by default");
    await waitFor(`${text(".summary")}.includes("Happening now") && ${text(".summary")}.includes("What happened")`, "summary sections");
    await waitFor(`${text(".summary")}.includes("Reviewer reports")`, "an attributed manager report", 60000);
    const t = await js(text(".summary"));
    for (const want of ["billing-service (Claude)", "Reviewer (Claude)", "billing-service", "turns used", "Updated"]) if (!t.includes(want)) throw new Error(`summary misses "${want}": ${t}`);
    if (t.includes("u_") || t.includes("p_")) throw new Error("ids leaked into the summary");
    if (await js(`!!document.querySelector(".s-card.decision")`)) throw new Error("decision shown without one");
    // A selection in the summary survives polling.
    await js(`(() => { const n = document.querySelector(".s-card .s-text"); const r = document.createRange(); r.selectNodeContents(n);
      const s = getSelection(); s.removeAllRanges(); s.addRange(r); return true; })()`);
    await sleep(3500);
    if (!(await js(`!getSelection().isCollapsed && document.querySelector(".summary").contains(getSelection().anchorNode)`))) throw new Error("selection lost on refresh");
    await js(`getSelection().removeAllRanges(); true`);
    await shot("console-summary-working.png");
    await shot("console-summary-mobile.png", 390, 844, true);
    const overflow = await js(`document.documentElement.scrollWidth > window.innerWidth + 1`);
    await cdp("Emulation.setDeviceMetricsOverride", { width: 1280, height: 900, deviceScaleFactor: 1, mobile: false });
    if (overflow) throw new Error("summary scrolls sideways on a phone");
    await js(`[...document.querySelectorAll(".tabs button")].find(b => b.textContent === "Conversation").click(); true`);
    await waitFor(`!document.querySelector(".chat").hidden && document.querySelector(".summary").hidden && document.querySelectorAll(".msg").length >= 1`, "conversation view");
    await js(`[...document.querySelectorAll(".tabs button")].find(b => b.textContent === "Summary").click(); true`);
    await waitFor(`!document.querySelector(".summary").hidden`, "back to summary");
  });

  await step("summary of a session needing a decision shows the actual question and how to answer", async () => {
    await go("#/"); await waitFor(`${rowFor("docs-site")}`, "docs row");
    await js(`${rowFor("docs-site")}.click(); true`);
    await waitFor(`!!document.querySelector(".s-card.decision")`, "decision card");
    const t = await js(text(".s-card.decision"));
    for (const want of ["Needs your decision", "docs-site asks:", "Should I update both", "Answer below."]) if (!t.includes(want)) throw new Error(`decision misses "${want}": ${t}`);
    if (!(await js(`${text(".summary")}.includes("Waiting for your answer.")`))) throw new Error("now line doesn't say it waits for you");
    if (!(await js(`!!document.querySelector(".compose textarea")`))) throw new Error("no answer box on the summary");
    await shot("console-summary-decision.png");
    await go("#/");
  });

  await step("question names the asker; draft survives polling", async () => {
    await js(`${rowFor("docs-site")}.click(); true`);
    await waitFor(`location.hash.startsWith("#/t/") && ${text(".question")}.includes("docs-site asks you")`, "question box");
    if ((await js(text(".status-line"))).includes("asks you")) throw new Error("the question is repeated in the status line");
    const placeholder = await js(`document.querySelector(".compose textarea").placeholder`);
    if (!placeholder.includes("Answer docs-site")) throw new Error(`placeholder: ${placeholder}`);
    const label = await js(`[...document.querySelectorAll(".dock button")].map(b => b.textContent).join("|")`);
    if (!label.includes("Send & hand back")) throw new Error(`buttons: ${label}`);
    if (label.includes("Hand back|")) throw new Error("plain Hand back offered for a question");
    await js(`(() => { const t = document.querySelector(".compose textarea"); t.focus(); t.value = "Both, please — keep the"; t.dispatchEvent(new Event("input")); return true; })()`);
    await sleep(3500);
    const kept = await js(`document.querySelector(".compose textarea").value === "Both, please — keep the" && document.activeElement === document.querySelector(".compose textarea")`);
    if (!kept) throw new Error("draft or focus lost after refresh");
    const leadsInDetails = await js(`!!document.querySelector("details.details") && !document.querySelector(".c-head").textContent.includes("leads")`);
    if (!leadsInDetails) throw new Error("leads/follows still in the header");
    await shot("console-question.png");
    await js(`(() => { const t = document.querySelector(".compose textarea"); t.value = "Both, please — keep the examples short."; t.dispatchEvent(new Event("input")); return true; })()`);
    await js(`[...document.querySelectorAll(".dock button")].find(b => b.textContent === "Send & hand back").click(); true`);
    await waitFor(`!${text(".question")}.includes("asks you") || document.querySelector(".question").hidden`, "question cleared");
    await waitFor(`[...document.querySelectorAll(".msg.you")].some(m => m.textContent.includes("keep the examples short"))`, "answer in conversation");
  });

  await step("intervene: finishing, then in control; draft kept", async () => {
    await go("#/"); await waitFor(`${rowFor("billing-service")}`, "billing row");
    await js(`${rowFor("billing-service")}.click(); true`);
    await waitFor(`[...document.querySelectorAll(".dock button")].some(b => b.textContent === "Intervene")`, "Intervene button");
    await waitFor(`${text(".status-line")}.includes("working")`, "someone working", 40000);
    if (process.env.TRACE) await js(`(() => { const t0 = performance.now();
      new MutationObserver(() => console.log("dock", Math.round(performance.now() - t0), document.querySelector(".dock-in").textContent.slice(0, 60))).observe(document.querySelector(".dock-in"), { childList: true });
      new MutationObserver(() => console.log("line", Math.round(performance.now() - t0), document.querySelector(".status-line").textContent)).observe(document.querySelector(".status-line"), { childList: true });
      return true; })()`);
    const t0 = Date.now();
    console.log("  clicked Intervene at", new Date().toISOString().slice(11, 23), "on", await js(`location.hash`));
    await js(`[...document.querySelectorAll(".dock button")].find(b => b.textContent === "Intervene").click(); true`);
    await waitFor(`${text(".status-line")}.includes("Finishing the current turn")`, "finishing message");
    console.log("  saw Finishing after", Date.now() - t0, "ms; dock:", await js(`document.querySelector(".dock-in").textContent`));
    const dockNow = await js(`[...document.querySelectorAll(".dock-in")].map(d => d.textContent).join(" ## ") + " || docks: " + document.querySelectorAll(".dock").length + " || toast: " + (document.querySelector("#toast").hidden ? "" : document.querySelector("#toast").textContent)`);
    if (!(await js(`!!document.querySelector(".compose textarea")`))) {
      const api = await js(`fetch("/api/console/" + location.hash.slice(4), { headers: { "X-Relay-Token": new URLSearchParams(location.search).get("t") } }).then(r => r.json()).then(d => JSON.stringify({ line: d.line, attention: d.attention, actions: d.actions, finishing: d.finishing_turn }))`);
      const src = await js(`fetch("/app.js?t=" + new URLSearchParams(location.search).get("t")).then(r => r.text()).then(t => t.includes("asked = 0, shown = 0"))`);
      throw new Error(`no composer while finishing; dock shows: ${dockNow}; api: ${api}; new app.js served: ${src}`);
    }
    await js(`(() => { const t = document.querySelector(".compose textarea"); t.focus(); t.value = "Please also log each retry"; t.dispatchEvent(new Event("input")); return true; })()`);
    await waitFor(`${text(".status-line")} === "You're in control."`, "in control", 60000);
    const kept = await js(`document.querySelector(".compose textarea").value === "Please also log each retry"`);
    if (!kept) throw new Error("draft lost across the state change");
    await shot("console-control.png");
  });

  await step("stop managing stays put when it fails", async () => {
    await js(`(() => { const f = window.fetch; window.fetch = (u, o) => String(u).includes("/release")
      ? Promise.resolve(new Response(JSON.stringify({ error: "still stopping" }), { status: 409, headers: { "Content-Type": "application/json" } }))
      : f(u, o); return true; })()`);
    const before = await js(`location.hash`);
    await js(`[...document.querySelectorAll(".dock button")].find(b => b.textContent === "Stop managing").click(); true`);
    await sleep(300);
    await js(`[...document.querySelectorAll(".dock button")].find(b => b.textContent === "Stop managing").click(); true`);
    await sleep(1500);
    if ((await js(`location.hash`)) !== before) throw new Error("navigated away although release failed");
    if (!(await js(text("#toast"))).includes("still stopping")) throw new Error("error not shown");
    await js(`location.reload(); true`); await sleep(1500);
  });

  await step("hand back", async () => {
    await waitFor(`[...document.querySelectorAll(".dock button")].some(b => b.textContent === "Hand back" && !b.disabled)`, "Hand back enabled");
    await js(`[...document.querySelectorAll(".dock button")].find(b => b.textContent === "Hand back").click(); true`);
    await waitFor(`[...document.querySelectorAll(".dock button")].some(b => b.textContent === "Intervene")`, "back to Intervene");
  });

  await step("a slow response from the previous screen is discarded", async () => {
    await js(`(() => { const f = window.fetch; window.fetch = (u, o) => String(u).includes("/api/console/")
      ? new Promise(r => setTimeout(() => r(f(u, o)), 2500)) : f(u, o); return true; })()`);
    await go("#/");
    await sleep(3500);
    const ok = await js(`document.querySelector("h1").textContent === "Sessions" && !document.querySelector(".dock")`);
    if (!ok) throw new Error("console painted over Home");
    await js(`location.reload(); true`); await sleep(1500);
  });

  await step("manage setup: both manager paths render real choices and Start sends the selection", async () => {
    // Record each /api/manage body; the stubbed calls answer 409 so nothing is started.
    await js(`(() => { window.__manage = []; window.__stubManage = true; const f = window.fetch;
      window.fetch = (u, o) => { if (String(u).includes("/api/manage")) { window.__manage.push(JSON.parse(o.body));
        if (window.__stubManage) return Promise.resolve(new Response(JSON.stringify({ error: "stubbed by browser check" }),
          { status: 409, headers: { "Content-Type": "application/json" } })); }
        return f(u, o); }; return true; })()`);
    await go("#/"); await waitFor(`${rowFor("payments-api")}`, "payments-api row");
    await js(`${rowFor("payments-api")}.click(); true`);
    await waitFor(`[...document.querySelectorAll("button")].some(b => b.textContent === "Manage with AI")`, "Manage with AI");
    await js(`[...document.querySelectorAll("button")].find(b => b.textContent === "Manage with AI").click(); true`);
    await waitFor(`${text(".panel h3")}.startsWith("Who manages")`, "setup panel");
    const panel = `[...document.querySelectorAll(".panel")].find(p => (p.querySelector("h3")||{}).textContent?.startsWith("Who manages"))`;
    const noObject = async (where) => { const t = await js(`${panel}.textContent`); if (t.includes("[object")) throw new Error(`${where} renders "[object …]": ${t.slice(0, 160)}`); };
    const pressed = (sel, label) => js(`[...${panel}.querySelectorAll(${JSON.stringify(sel)})].find(b => b.textContent.startsWith(${JSON.stringify(label)}))?.getAttribute("aria-pressed")`);
    const click = (sel, label) => js(`[...${panel}.querySelectorAll(${JSON.stringify(sel)})].find(b => b.textContent.startsWith(${JSON.stringify(label)})).click(); true`);
    const clients = await js(`fetch("/api/sessions", { headers: { "X-Relay-Token": new URLSearchParams(location.search).get("t") } }).then(r => r.json()).then(d => d.new_session_clients || [])`);

    // New manager: client and personality are real, clickable buttons.
    await noObject("new-manager setup");
    await js(`${panel}.querySelector("textarea").value = "Tighten the timeout handling"; true`);
    const goalKept = async (after) => { const v = await js(`${panel}.querySelector("textarea").value`);
      if (v !== "Tighten the timeout handling") throw new Error(`goal lost after ${after}: ${JSON.stringify(v)}`); };
    if (clients.length > 1) {
      const segs = await js(`[...${panel}.querySelectorAll(".seg button")].map(b => b.textContent).join("|")`);
      if (segs !== "Claude|Codex") throw new Error(`client choices: ${segs}`);
      await click(".seg button", "Codex");
      if ((await pressed(".seg button", "Codex")) !== "true") throw new Error("Codex not selectable");
      await goalKept("choosing Codex");
    }
    const styles = await js(`[...${panel}.querySelectorAll(".choices")][1].querySelectorAll("button.choice").length`);
    if (styles < 3) throw new Error(`only ${styles} personality choices`);
    await click("button.choice", "Planner");
    if ((await pressed("button.choice", "Planner")) !== "true") throw new Error("Planner not selectable");
    await noObject("new-manager setup after choosing");
    await goalKept("choosing Planner");
    await click("button.primary", "Start");
    await waitFor(`window.__manage.length === 1 && ${panel}.querySelector(".err").textContent.includes("stubbed")`, "new-manager Start request");
    const sent1 = await js(`window.__manage[0]`);
    const wantClient = clients.length > 1 ? "codex" : clients[0];
    if (JSON.stringify(sent1.manager) !== JSON.stringify({ new: { client: wantClient, personality: "planner" } }))
      throw new Error(`new manager sent as ${JSON.stringify(sent1.manager)}`);
    if (sent1.goal !== "Tighten the timeout handling" || !sent1.key) throw new Error(`goal/key sent: ${JSON.stringify(sent1)}`);
    await shot("setup-new.png");

    // Existing manager: real, clickable session choices.
    await click("button.choice", "One of your sessions");
    await waitFor(`${text(".panel .step-label")}.includes("Which one")`, "existing-manager list");
    await noObject("existing-manager setup");
    await goalKept("switching to one of your sessions");
    const opts = await js(`[...${panel}.querySelectorAll(".choices")][1].querySelectorAll("button.choice").length`);
    if (opts < 2) throw new Error(`only ${opts} session choices`);
    const target = await js(`[...${panel}.querySelectorAll(".choices")][1].querySelectorAll("button.choice")[1].querySelector("b").textContent`);
    await click("button.choice", target);
    if ((await pressed("button.choice", target)) !== "true") throw new Error(`${target} not selectable`);
    await goalKept(`choosing ${target}`);
    await shot("setup-existing.png");
    await click("button.primary", "Start");
    await waitFor(`window.__manage.length === 2`, "existing-manager Start request");
    const sent2 = await js(`window.__manage[1]`);
    const want = await js(`fetch("/api/sessions", { headers: { "X-Relay-Token": new URLSearchParams(location.search).get("t") } })
      .then(r => r.json()).then(d => d.sessions.find(s => s.name === ${JSON.stringify(target)}).key)`);
    if (JSON.stringify(sent2.manager) !== JSON.stringify({ existing: want })) throw new Error(`existing manager sent as ${JSON.stringify(sent2.manager)}, want ${want}`);
    if (sent2.goal !== "Tighten the timeout handling") throw new Error(`goal sent: ${sent2.goal}`);

    // Now let it through for real: the server accepts the selection and opens the console with that manager.
    await js(`window.__stubManage = false; true`);
    await waitFor(`!${panel}.querySelector("button.primary").disabled`, "Start re-enabled");
    await click("button.primary", "Start");
    await waitFor(`location.hash.startsWith("#/t/") && !!document.querySelector(".c-pair")`, "console for the new team");
    const pair = await js(text(".c-pair"));
    if (!pair.includes(target)) throw new Error(`console shows ${pair}, not manager ${target}`);
    await js(`location.reload(); true`); await sleep(1500);
  });

  await step("hand-off: two open terminals handed over, ownership shown, one back to its terminal, then again", async () => {
    // Closing a terminal is the person's step; here it's stopping the pretend terminal's process.
    const HOME = process.env.DEMO_HOME;
    if (!HOME) throw new Error("set DEMO_HOME to the demo's fake home (the demo prints its folder)");
    const closeTerminal = (sid) => { const pid = JSON.parse(readFileSync(join(HOME, "sessions", sid, "info.json"))).pid; process.kill(pid, "SIGTERM"); };
    const owner = (name) => js(`(() => { const o = [...document.querySelectorAll(".owners .owner")].find(o => o.querySelector("b").textContent === ${JSON.stringify(name)}); return o ? o.textContent : ""; })()`);
    const CL = "mobile-app · Claude", CX = "mobile-app · Codex";
    await go("#/"); await waitFor(`${rowFor("mobile-app", "Claude")}`, "mobile-app rows");
    await js(`${rowFor("mobile-app", "Claude")}.click(); true`);
    await waitFor(`[...document.querySelectorAll("button")].some(b => b.textContent === "Manage with AI")`, "Manage with AI");
    await js(`[...document.querySelectorAll("button")].find(b => b.textContent === "Manage with AI").click(); true`);
    await waitFor(`${text(".panel h3")}.startsWith("Who manages")`, "setup");
    await js(`[...document.querySelectorAll(".panel button.choice")].find(b => b.textContent.startsWith("One of your sessions")).click(); true`);
    await js(`[...document.querySelectorAll(".panel button.choice")].find(b => b.querySelector("b") && b.querySelector("b").textContent === ${JSON.stringify(CX)}).click(); true`);
    const hn = await js(text(".handoff-note"));
    if (!hn.includes(CL) || !hn.includes(CX) || !hn.includes("/exit")) throw new Error(`hand-off preview: ${hn}`);
    if (!hn.includes("approvals off (workspace-write)") || !hn.includes("can't ask for permission")) throw new Error(`policy not shown: ${hn}`);
    await js(`document.querySelector(".panel textarea").value = "Add offline sync with tests"; true`);
    await js(`[...document.querySelectorAll(".panel button.primary")].find(b => b.textContent === "Start").click(); true`);
    await waitFor(`location.hash.startsWith("#/t/") && ${text(".status-line")}.includes("Waiting for you to close")`, "waiting for terminals");
    if (!(await owner(CL)).includes("waiting for you to close it")) throw new Error(`owner: ${await owner(CL)}`);
    if (!(await js(text(".dock"))).includes("type /exit")) throw new Error("manual step not shown");
    const sent0 = await js(`document.querySelectorAll(".msg.leader, .msg.follower").length`);
    if (sent0) throw new Error("something was sent before the terminals closed");
    await shot("handoff-waiting.png");
    closeTerminal("s-mobile-cl"); closeTerminal("s-mobile-cx");
    await waitFor(`(() => { const o = [...document.querySelectorAll(".owners .owner")]; return o.length === 2 && o.every(x => x.textContent.includes("the relay is driving it")); })()`, "relay owns both", 30000);
    await waitFor(`document.querySelectorAll(".msg.follower").length >= 1`, "a follower reply", 40000);
    await shot("handoff-owned.png");
    // Back to terminal for the follower (secondary, in Details).
    await js(`document.querySelector("details.details").open = true; true`);
    await waitFor(`[...document.querySelectorAll(".handoff .hrow")].some(r => r.textContent.startsWith(${JSON.stringify(CL)}) && r.querySelector("button"))`, "Back to terminal button");
    await js(`[...document.querySelectorAll(".handoff .hrow")].find(r => r.textContent.startsWith(${JSON.stringify(CL)})).querySelector("button").click(); true`);
    await waitFor(`[...document.querySelectorAll(".owners .owner")].some(o => o.textContent.startsWith(${JSON.stringify(CL)}) && o.textContent.includes("back in your terminal"))`, "follower back in terminal", 40000);
    if (!(await owner(CX)).includes("the relay is driving it")) throw new Error(`manager changed: ${await owner(CX)}`);
    await js(`document.querySelector("details.details").open = true; true`);
    const det = await js(text(".handoff"));
    if (!det.includes("claude --resume s-mobile-cl")) throw new Error(`resume command missing: ${det}`);
    if (!(await js(text(".dock"))).includes("Hand over again")) throw new Error("no Hand over again");
    if (!(await js(text(".status-line"))).includes("back in your terminal. The AI is waiting.")) throw new Error("status line after return");
    const n1 = await js(`document.querySelectorAll(".msg.follower").length`);
    await sleep(4000);
    if ((await js(`document.querySelectorAll(".msg.follower").length`)) !== n1) throw new Error("the returned follower got another turn");
    await shot("handoff-returned.png");
    // Hand it over again (its terminal is closed): the same team carries on.
    await js(`[...document.querySelectorAll(".dock button")].find(b => b.textContent === "Hand over again").click(); true`);
    await waitFor(`[...document.querySelectorAll(".owners .owner")].every(o => o.textContent.includes("the relay is driving it"))`, "relay owns both again", 30000);
    await waitFor(`document.querySelectorAll(".msg.follower").length > ${n1}`, "the follower continues", 40000);
    // Stop managing doesn't strand the conversations: the relay keeps them (claimed) and the
    // way to give them back stays reachable from the row and the ended team.
    const team = await js(`location.hash`);
    await js(`[...document.querySelectorAll(".dock button")].find(b => b.textContent === "Intervene").click(); true`);
    await waitFor(`[...document.querySelectorAll(".dock button")].some(b => b.textContent === "Stop managing")`, "Stop managing offered");
    await js(`[...document.querySelectorAll(".dock button")].find(b => b.textContent === "Stop managing").click(); true`);
    await sleep(300);
    await js(`[...document.querySelectorAll(".dock button")].find(b => b.textContent === "Stop managing").click(); true`);
    await waitFor(`location.hash === "#/" && ${rowFor("mobile-app", "Claude")}`, "back on Home", 30000);
    await waitFor(`${rowFor("mobile-app", "Claude")}.textContent.includes("The relay is driving it")`, "row says the relay still has it");
    await js(`${rowFor("mobile-app", "Claude")}.click(); true`);
    await waitFor(`location.hash === ${JSON.stringify(team)} && (${text(".dock")}.includes("This team has ended, but the relay still has") || ${text(".dock")}.includes("doesn't know its outcome"))`, "ended team offers give back (or asks about an unknown outcome first)");
    await shot("handoff-ended.png");
    // Stopped while a turn may still have been running: its outcome is unknown, so nothing is
    // given back until that's explicitly acknowledged. Then give both back.
    if (await js(`${text(".dock")}.includes("doesn't know its outcome")`)) {
      if (await js(`[...document.querySelectorAll(".dock button")].some(b => b.textContent.startsWith("Give "))`))
        throw new Error("give back offered before the unknown outcome was acknowledged");
      if (!(await js(`${text(".dock")}.includes("will not be resent")`))) throw new Error("statement not shown verbatim");
      await js(`[...document.querySelectorAll(".dock button")].find(b => b.textContent === "Confirm and continue").click(); true`);
      try {
        await waitFor(`!${text(".dock")}.includes("doesn't know its outcome")`, "acknowledgement recorded", 15000);
      } catch (e) { throw new Error(`${e.message}; dock: ${await js(text(".dock"))}`); }
    }
    for (const name of [CL, CX]) {
      await waitFor(`[...document.querySelectorAll(".dock button")].some(b => b.textContent === ${JSON.stringify("Give " + name + " back")})`, `give ${name} back`);
      await js(`[...document.querySelectorAll(".dock button")].find(b => b.textContent === ${JSON.stringify("Give " + name + " back")}).click(); true`);
      await waitFor(`[...document.querySelectorAll(".owners .owner")].some(o => o.textContent.startsWith(${JSON.stringify(name)}) && o.textContent.includes("back in your terminal"))`, `${name} back`, 30000);
    }
    await js(`document.querySelector("details.details").open = true; true`);
    const d2 = await js(text(".handoff"));
    if (!d2.includes("claude --resume s-mobile-cl") || !d2.includes("codex resume s-mobile-cx")) throw new Error(`resume commands: ${d2}`);
  });

  const NOTIFY_LOG = process.env.DEMO_HOME ? join(process.env.DEMO_HOME, "..", "notify-demo.log") : null;
  const notifyLines = () => { try { return readFileSync(NOTIFY_LOG, "utf8").trim().split("\n").filter(Boolean).map((l) => JSON.parse(l)); } catch (_) { return []; } };
  await step("notifications: off by default; a destination is verified with a code, then turned on", async () => {
    if (!NOTIFY_LOG) throw new Error("set DEMO_HOME");
    await go("#/"); await js(`location.reload(); true`); await sleep(1200);
    await go("#/notifications");
    await waitFor(`document.querySelectorAll(".n-card").length === 3`, "settings cards");
    const t0 = await js(text("#view"));
    if ((t0.match(/Not set up/g) || []).length !== 2) throw new Error(`channels not off by default: ${t0.slice(0, 300)}`);
    if (!t0.includes("custom format")) throw new Error("webhook not labelled as a custom format");
    await js(`(() => { const e = document.querySelector('[aria-label="HTTPS address"]'); e.value = "https://hooks.example.invalid/relay"; return true; })()`);
    await js(`[...document.querySelectorAll("button")].find(b => b.textContent === "Save destination").click(); true`);
    await waitFor(`${text("#view")}.includes("Not verified")`, "saved, unverified");
    await js(`[...document.querySelectorAll("button")].find(b => b.textContent === "Send a code").click(); true`);
    await waitFor(`${text("#toast")}.includes("Code sent")`, "code sent");
    const msg = notifyLines().find((m) => m.text.includes("verification code"));
    if (!msg) throw new Error("no code in the demo outbox");
    const code = msg.text.split("code: ")[1].slice(0, 6);
    await js(`(() => { const e = document.querySelector('[aria-label="Code"]'); e.value = ${JSON.stringify(code)}; return true; })()`);
    await js(`[...document.querySelectorAll("button")].find(b => b.textContent === "Verify").click(); true`);
    await waitFor(`${text("#view")}.includes("Verified, off")`, "verified");
    await js(`[...document.querySelectorAll("button")].find(b => b.textContent === "Turn on").click(); true`);
    await waitFor(`${text("#view")}.includes("On ·")`, "turned on");
    // A credential-bearing link address is refused; a plain https one is kept.
    await js(`(() => { const e = document.querySelector('[aria-label="Link address"]'); e.value = "https://me:pw@relay.example.invalid/?t=x"; return true; })()`);
    await js(`[...document.querySelectorAll("button")].filter(b => b.textContent === "Save").pop().click(); true`);
    await waitFor(`${text("#toast")}.includes("user name or password")`, "credential-bearing base refused");
    await js(`(() => { const e = document.querySelector('[aria-label="Link address"]'); e.value = "https://relay.example.invalid"; return true; })()`);
    await js(`[...document.querySelectorAll("button")].filter(b => b.textContent === "Save").pop().click(); true`);
    await waitFor(`${text("#toast")} === "Saved."`, "link base saved");
    await shot("notifications.png");
    if (notifyLines().some((m) => !m.text.includes("verification code"))) throw new Error("a notice was sent for a decision that existed before turning on");
  });

  await step("review loop: set up with a handover and a check; the relay checks, and an unaccepted finish stops for you", async () => {
    await go("#/"); await js(`location.reload(); true`); await sleep(1500);  // drop earlier steps' fetch stubs
    await waitFor(`${rowFor("infra-scripts")}`, "infra row");
    await js(`${rowFor("infra-scripts")}.click(); true`);
    await waitFor(`[...document.querySelectorAll("button")].some(b => b.textContent === "Manage with AI")`, "Manage with AI");
    await js(`[...document.querySelectorAll("button")].find(b => b.textContent === "Manage with AI").click(); true`);
    await waitFor(`[...document.querySelectorAll(".seg button")].some(b => b.textContent === "Codex")`, "client choice");
    await js(`[...document.querySelectorAll(".seg button")].find(b => b.textContent === "Codex").click(); true`);
    await waitFor(`!!document.querySelector(".check-row input")`, "review loop option");
    await js(`document.querySelector(".check-row input").click(); true`);
    await waitFor(`!!document.querySelector('[aria-label="Handover"]')`, "review fields");
    const fill = (label, v) => js(`(() => { const e = document.querySelector('[aria-label="${label}"]'); e.value = ${JSON.stringify(v)}; e.dispatchEvent(new Event("input")); return true; })()`);
    await fill("Handover", "Architecture: scripts only.\nScope: infra-scripts.\nDecisions: none yet.\nTask: tidy the deploy script.");
    await fill("Done when", "The relay check passes and the reviewer accepts.");
    await fill("Check command", "python3 -c \"print('check ok')\"");
    await fill("Goal", "Tidy the deploy script.");
    await shot("setup-review-loop.png");
    await js(`[...document.querySelectorAll("button")].find(b => b.textContent === "Start").click(); true`);
    await waitFor(`location.hash.startsWith("#/t/")`, "team page");
    await js(`document.querySelector("details.details").open = true; true`);
    await waitFor(`${text("details.details")}.includes("Review loop") && ${text("details.details")}.includes("min left")`, "review details");
    await waitFor(`${text(".summary")}.includes("Relay check of the latest revision: passed")`, "the relay's own check", 90000);
    await waitFor(`!!document.querySelector(".s-card.decision") && ${text(".s-card.decision")}.includes("Not accepted")`, "unaccepted finish stops", 90000);
    if (!(await js(`${text(".status-line")}.includes("wasn't accepted")`))) throw new Error("status line doesn't say it");
    if (!(await js(`!!document.querySelector(".compose textarea")`))) throw new Error("no way to say what happens next");
    await shot("console-review-loop.png");
    // That decision began after notifications were turned on: exactly one message, kind-only.
    await waitFor(`${text(".s-card.decision")}.includes("Message sent")`, "delivery shown on the decision", 30000);
    const notices = notifyLines().filter((m) => !m.text.includes("verification code"));
    if (notices.length !== 1 || !notices[0].text.includes("couldn't accept the work")) throw new Error(`notices: ${JSON.stringify(notices)}`);
    if (/p_[0-9a-f]{6}|tidy|deploy|t=/i.test(JSON.stringify(notices[0]))) throw new Error(`notice leaks details: ${JSON.stringify(notices[0])}`);
    // The opaque link opens the team page after the console's own sign-in.
    const link = notices[0].meta.link || "";
    if (!/^https:\/\/relay\.example\.invalid\/#\/d\/[0-9a-f]{24}$/.test(link)) throw new Error(`link: ${link}`);
    const teamHash = await js(`location.hash`);
    await go("#/"); await go(link.slice(link.indexOf("#")));
    await waitFor(`location.hash === ${JSON.stringify(teamHash)}`, "decision link opens the team", 15000);
    if (await js(`${text(".s-where")}.includes("/")`)) throw new Error("the summary still shows a full path");
    await go("#/");
  });

  await step("events expand into a full, redacted record", async () => {
    await go("#/events");
    await waitFor(`document.querySelectorAll(".events button.e").length > 5`, "event rows");
    await js(`[...document.querySelectorAll(".events button.e")].find(b => b.textContent.includes("dispatch_authorized")).click(); true`);
    await waitFor(`!!document.querySelector(".e-full")`, "expanded record");
    const full = await js(text(".e-full"));
    for (const want of ["stream", "sequence", "event id", "actor", "redacted", "controller ownership token"])
      if (!full.includes(want)) throw new Error(`record missing "${want}"`);
    if (/ctl_[0-9a-f]/.test(await js(`document.body.innerText`))) throw new Error("a controller token is visible");
    await shot("events.png");
  });
} catch (e) {
  results.push(["FAIL", "setup", e.message]); console.log("FAIL setup —", e.message);
} finally {
  try { ws && ws.close(); } catch (_) {}
  chrome.kill("SIGTERM");
  await sleep(300);
  try { rmSync(profile, { recursive: true, force: true }); } catch (_) {}
}
if (pageErrors.length) {
  results.push(["FAIL", "no JavaScript errors on the page", pageErrors.join(" | ")]);
  console.log("FAIL page errors —", [...new Set(pageErrors)].join("\n  "));
}
const failed = results.filter((r) => r[0] === "FAIL").length;
console.log(`${results.length - failed}/${results.length} browser steps passed`);
process.exit(failed ? 1 : 0);
