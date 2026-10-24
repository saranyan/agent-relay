// Relay console — simple first.
//   #/            your sessions (+ ask to filter)
//   #/s/<key>     one session: manage with AI
//   #/t/<team>    console for an AI-managed session: watch, intervene, hand back
//   #/events      the raw event bus
//   #/notifications  optional messages when you're needed (off until you set one up)
// Everything from the server is rendered as text; nothing is parsed as HTML.
"use strict";

const TOKEN = new URLSearchParams(location.search).get("t") || "";
const $ = (s, r = document) => r.querySelector(s);
const SVGNS = "http://www.w3.org/2000/svg";

function el(tag, attrs = {}, ...kids) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") n.className = v;
    else if (k === "style") n.style.cssText = v;
    else if (k === "on") for (const [e, f] of Object.entries(v)) n.addEventListener(e, f);
    else if (k === "value") n.value = v;
    else n.setAttribute(k, v === true ? "" : v);
  }
  for (const k of kids.flat(Infinity)) if (k !== null && k !== undefined && k !== false && k !== "")
    n.append(k instanceof Node ? k : document.createTextNode(String(k)));
  return n;
}
const PATHS = {
  search: ["M11 4a7 7 0 1 0 0 14 7 7 0 0 0 0-14z", "m20 20-3.5-3.5"],
  back: ["m15 18-6-6 6-6"],
  sun: ["M12 4V2", "M12 22v-2", "m4.9 4.9 1.4 1.4", "m17.7 17.7 1.4 1.4", "M2 12h2", "M20 12h2", "m4.9 19.1 1.4-1.4", "m17.7 6.3 1.4-1.4", "M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8z"],
  moon: ["M20 14.5A8 8 0 0 1 9.5 4 8 8 0 1 0 20 14.5z"],
};
function icon(name) {
  const s = document.createElementNS(SVGNS, "svg");
  for (const [k, v] of Object.entries({ viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", "stroke-width": "2",
    "stroke-linecap": "round", "stroke-linejoin": "round", "aria-hidden": "true" })) s.setAttribute(k, v);
  for (const d of PATHS[name]) { const p = document.createElementNS(SVGNS, "path"); p.setAttribute("d", d); s.append(p); }
  return s;
}

async function api(path, body) {
  const opt = { headers: { "X-Relay-Token": TOKEN }, cache: "no-store" };
  if (body !== undefined) {
    opt.method = "POST";
    opt.headers["Content-Type"] = "application/json";
    opt.body = JSON.stringify(body);
  }
  const r = await fetch(path, opt);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || `request failed (${r.status})`);
  return data;
}

let toastTimer;
function toast(text) {
  const t = $("#toast"); t.textContent = text; t.hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => { t.hidden = true; }, 3600);
}
const home = (p) => !p ? "" : p.replace(/^\/Users\/[^/]+/, "~").replace(/^\/private\/tmp\//, "/tmp/");
const initial = (s) => (s || "?").trim().charAt(0).toUpperCase();
function ago(iso) {
  const s = Math.round((Date.now() - new Date(iso)) / 1000);
  if (!isFinite(s)) return "";
  if (s < 45) return "just now";
  if (s < 3600) return `${Math.max(1, Math.round(s / 60))} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
}
const STATE_WORD = { needs_you: "Needs you", working: "Working", waiting: "Waiting", idle: "Idle", done: "Done",
                     offline: "Offline", unknown: "Unknown" };
const stateTag = (s) => el("span", { class: "state", "data-s": s }, el("span", { class: "dot", "aria-hidden": "true" }), STATE_WORD[s] || s);
const avatar = (client, name) => el("span", { class: "avatar", "data-c": client || "", "aria-hidden": "true" }, initial(name || client));
const folderName = (p) => p ? p.replace(/\/+$/, "").split("/").pop() : "";
// Your sessions are known by folder; a manager the relay started is known by its role.
function heading(x) { return x.cwd && !x.started_by_relay ? `${folderName(x.cwd)} · ${x.client}` : `${x.name} · ${x.client}`; }
function workLine(x) {
  const c = x.summary || x.title;
  if (!c) {
    if (x.managed_goal) return ["Goal: ", x.managed_goal.text];
    return el("span", { class: "unavail" }, "Work summary unavailable");
  }
  return [c.text, c.stale ? el("span", { class: "unavail" }, " · may be out of date") : ""];
}
function srcLine(c) {
  const from = /^reported/.test(c.source) ? c.source.charAt(0).toUpperCase() + c.source.slice(1) : `from ${c.source}`;
  return [from, c.at ? ` · ${ago(c.at)}` : "", c.stale ? " · may be out of date" : ""];
}
const IDENTITY = { likely: "conversation not confirmed", ambiguous: "conversation not confirmed (several here)",
                   none: "conversation unknown" };
const uncertainty = (x) => x.identity && x.identity !== "exact"
  ? el("span", { class: "unavail" }, ` · ${IDENTITY[x.identity_note] || "conversation not confirmed"}`) : "";
function copyBtn(text) {
  return el("button", { type: "button", class: "link", on: { click: async () => {
    try { await navigator.clipboard.writeText(text); toast("Copied."); } catch (_) { toast("Couldn't copy — select it instead."); } } } }, "Copy");
}
// Hand-off: who writes a conversation right now, in plain words (rows and the team console).
const OWN_WORDS = { awaiting_exit: "Waiting for you to close it in its terminal", relay_starting: "The relay is taking it over",
  relay_owned: "The relay is driving it", returning: "Going back to your terminal", released: "Back in your terminal",
  blocked: "Hand-off stopped — needs you" };
function ownLine(x) {
  const o = x.ownership;
  return o && OWN_WORDS[o.state] ? [" · ", el("span", { class: "own", "data-own": o.state }, OWN_WORDS[o.state])] : "";
}
function managedLine(x) {
  const m = x.managed_by || {};
  if (m.kind === "ai") return ["Managed by ", el("b", {}, m.manager)];
  if (m.kind === "you") return ["Managed by ", el("b", {}, "you"), ` · ${m.manager} paused`];
  if (m.kind === "manager") return ["Managing ", el("b", {}, m.managing)];
  return "No manager assigned";
}

// ------------------------------------------------------------------ data + router
let catalog = null;
let gen = 0;                       // bumps on every navigation; stale responses are dropped
let timer = null;
const live = (g) => g === gen;
let catAsked = 0, catShown = 0;    // and within a screen, an older response never replaces a newer one
async function loadCatalog(g) {
  const n = ++catAsked;
  const c = await api("/api/sessions");
  if (live(g) && n > catShown) { catShown = n; catalog = c; }
  return catalog;
}

function route() {
  gen += 1;
  clearInterval(timer); timer = null;
  document.querySelectorAll(".dock").forEach((d) => d.remove());
  const h = location.hash || "#/";
  $("#view").className = "view";
  if (h.startsWith("#/s/")) return showSession(decodeURIComponent(h.slice(4)), gen);
  if (h.startsWith("#/t/")) return showConsole(decodeURIComponent(h.slice(4)), gen);
  if (h === "#/events") return showEvents(gen);
  if (h === "#/notifications") return showNotifications(gen);
  if (h.startsWith("#/d/")) return openDecision(h.slice(4), gen);
  return showHome(gen);
}
window.addEventListener("hashchange", route);
function every(g, ms, fn) { fn(); timer = setInterval(() => { if (live(g) && !document.hidden) fn(); }, ms); }
function demoBanner() {
  return catalog && catalog.fake_mode
    ? el("p", { class: "demo" }, "Demo mode — these are pretend sessions. Nothing here uses your Claude or Codex accounts.")
    : "";
}

// ================================================================== home
const STOP = new Set("which what whats what's who whose is are was the a an on with by for of in my me show sessions session any all about that there does do doing how list tell give currently right now".split(" "));
const SYN = [
  [/^(wait|waiting|stuck|need|needs|blocked|question|questions|help|attention)$/, { state: "needs_you", label: "need you" }],
  [/^(working|busy|running|active|progress)$/, { state: "working", label: "working" }],
  [/^(idle|free|available|nothing|quiet)$/, { state: "idle", label: "idle" }],
  [/^(done|finished|complete|completed)$/, { state: "done", label: "done" }],
  [/^(unknown|unclear)$/, { state: "unknown", label: "unknown" }],
  [/^(ai|managed|manager|managers|automated|automatic)$/, { managed: "ai", label: "managed by AI" }],
  [/^(mine|manual|human|myself|by-me)$/, { managed: "you", label: "managed by you" }],
  [/^(unassigned|unmanaged)$/, { managed: "none", label: "with no manager" }],
  [/^claude$/, { client: "Claude", label: "Claude" }],
  [/^codex$/, { client: "Codex", label: "Codex" }],
];
// How a query is read, in this order (the "Showing …" line always says which applied):
//  1. "quoted text" is always literal text, never a filter;
//  2. if the whole query appears as a phrase in some session's own name, folder, title
//     or summary, it is searched as that phrase ("session manager review" is a title,
//     not "session" + "managed by AI" + "review");
//  3. otherwise words like waiting / working / manager / claude are filters, the rest text.
const norm = (s) => ` ${(s || "").toLowerCase().replace(/[^a-z0-9._-]+/g, " ").trim()} `;
function ownText(x) { return norm([x.name, x.cwd, x.title && x.title.text, x.summary && x.summary.text].join(" ")); }
function understand(q, sessions) {
  const f = { states: new Set(), managed: null, client: null, words: [], phrases: [], labels: [] };
  const rest = (q || "").replace(/"([^"]*)"/g, (_, p) => { if (norm(p).trim()) f.phrases.push(norm(p).trim()); return " "; });
  const whole = norm(rest).trim();
  if (whole.includes(" ") && (sessions || []).some((x) => ownText(x).includes(` ${whole} `))) {
    f.phrases.push(whole);
    return f;
  }
  for (const raw of whole.split(" ").filter(Boolean)) {
    if (STOP.has(raw)) continue;
    const hit = SYN.find(([re]) => re.test(raw));
    if (hit) {
      const v = hit[1];
      if (v.state) f.states.add(v.state);
      if (v.managed) f.managed = v.managed;
      if (v.client) f.client = v.client;
      if (!f.labels.includes(v.label)) f.labels.push(v.label);
    } else if (raw.length > 1) f.words.push(raw);
  }
  return f;
}
function matches(x, f) {
  if (f.states.size && !f.states.has(x.state)) return false;
  if (f.managed && (x.managed_by || {}).kind !== f.managed) return false;
  if (f.client && x.client !== f.client) return false;
  const hay = [x.name, x.cwd, x.doing, x.client, (x.managed_by || {}).manager, (x.managed_by || {}).managing,
               x.title && x.title.text, x.summary && x.summary.text, x.managed_goal && x.managed_goal.text].join(" ").toLowerCase();
  return f.words.every((w) => hay.includes(w)) && f.phrases.every((p) => norm(hay).includes(` ${p} `));
}
const QUICK = [["All", ""], ["Needs you", "needs me"], ["Working", "working"], ["Managed by AI", "managed by ai"]];

function showHome(g) {
  const view = $("#view");
  const input = el("input", { type: "search", placeholder: "Ask — “which are waiting?” or “what's on payments?”",
                              "aria-label": "Ask about your sessions", autocomplete: "off", value: showHome.q || "" });
  const banner = el("div");
  const sub = el("p", { class: "sub" });
  const understood = el("div", { class: "understood", "aria-live": "polite" });
  const quick = el("div", { class: "chips" });
  const list = el("div");
  view.replaceChildren(el("h1", { class: "title" }, "Sessions"), sub, banner, el("label", { class: "ask" }, icon("search"), input),
    understood, quick, list);
  input.addEventListener("input", () => { showHome.q = input.value; paint(true); });
  input.addEventListener("keydown", (e) => { if (e.key === "Escape") { input.value = ""; showHome.q = ""; paint(true); } });
  let sig = "";

  function paint(force) {
    if (!catalog || !live(g)) return;
    const f = understand(showHome.q, catalog.sessions);
    const all = catalog.sessions;
    const shown = all.filter((x) => matches(x, f));
    const key = JSON.stringify([showHome.q, shown.map((x) => [x.key, x.state, x.doing, x.managed_by, x.team,
      x.title && x.title.text, x.summary && x.summary.text, x.summary && x.summary.stale, x.identity_note,
      x.managed_goal && x.managed_goal.text])]);
    if (!force && key === sig) return;  // nothing changed: keep the DOM (and keyboard focus) as is
    sig = key;
    const focusedKey = document.activeElement && document.activeElement.dataset && document.activeElement.dataset.key;
    const needs = all.filter((x) => x.state === "needs_you").length;
    const ai = all.filter((x) => (x.managed_by || {}).kind === "ai").length;
    sub.textContent = all.length ? `${all.length} session${all.length === 1 ? "" : "s"}` + (needs ? ` · ${needs} need${needs === 1 ? "s" : ""} you` : "") + (ai ? ` · ${ai} managed by AI` : "") : "";
    banner.replaceChildren(demoBanner());
    const asked = (showHome.q || "").trim();
    understood.replaceChildren(...(asked ? [
      `Showing ${shown.length} ${f.labels.length ? "that are " + f.labels.join(", ") : "sessions"}${
        [...f.phrases, ...(f.words.length ? [f.words.join(" ")] : [])].map((t) => ` matching “${t}”`).join(" and")}.`,
      el("button", { type: "button", class: "link", on: { click: () => { input.value = ""; showHome.q = ""; paint(true); input.focus(); } } }, "Clear")] : []));
    quick.replaceChildren(...QUICK.map(([label, q]) => el("button", { type: "button", class: `chip${(showHome.q || "") === q ? " on" : ""}`,
      "aria-pressed": String((showHome.q || "") === q), on: { click: () => { input.value = q; showHome.q = q; paint(true); } } }, label)));
    if (!all.length) {
      list.replaceChildren(el("div", { class: "empty-state" }, el("p", {}, "No sessions found yet."),
        el("p", { class: "small" }, "Open a Claude or Codex session and it will appear here.")));
      return;
    }
    if (!shown.length) { list.replaceChildren(el("div", { class: "empty-state" }, "Nothing matches.")); return; }
    list.replaceChildren(el("ul", { class: "list" }, shown.map((x) => el("li", { class: "item" },
      el("button", { type: "button", class: "rowbtn", "data-key": x.key, on: { click: () => {
          location.hash = x.team && ["ai", "you"].includes((x.managed_by || {}).kind) ? `#/t/${x.team}`
            : x.handoff_team ? `#/t/${x.handoff_team}` : `#/s/${encodeURIComponent(x.key)}`; } } },
        avatar(x.client, x.started_by_relay ? x.name : (folderName(x.cwd) || x.name)),
        el("span", { style: "min-width:0" }, el("div", { class: "nm" }, heading(x)), el("div", { class: "dg" }, workLine(x)),
          el("div", { class: "who" }, x.cwd ? [home(x.cwd), " · "] : "", managedLine(x), ownLine(x), uncertainty(x))),
        stateTag(x.state))))));
    if (focusedKey) { const b = list.querySelector(`[data-key="${CSS.escape(focusedKey)}"]`); if (b) b.focus(); }
  }
  every(g, 2000, async () => { try { await loadCatalog(g); paint(false); } catch (e) { if (live(g)) toast(e.message); } });
  paint(true);
}

// ================================================================== session
async function showSession(key, g) {
  const view = $("#view");
  const back = el("button", { type: "button", class: "back", on: { click: () => { location.hash = "#/"; } } }, icon("back"), "Sessions");
  view.replaceChildren(back);
  try { await loadCatalog(g); } catch (e) { if (live(g)) toast(e.message); }
  if (!live(g)) return;
  const x = catalog && catalog.sessions.find((s) => s.key === key);
  if (!x) { view.append(el("h1", { class: "title" }, "Not found"), el("p", { class: "sub" }, "This session isn't running any more.")); return; }
  if (x.team && ["ai", "you"].includes((x.managed_by || {}).kind)) { location.hash = `#/t/${x.team}`; return; }

  view.append(
    el("div", { class: "hero" }, avatar(x.client, folderName(x.cwd) || x.name), el("h1", { class: "title" }, heading(x))),
    el("p", { class: "facts work" }, workLine(x)),
    el("p", { class: "facts" }, x.cwd ? [home(x.cwd), " · "] : "", managedLine(x), uncertainty(x)),
    demoBanner(),
    el("div", { class: "panel" }, stateTag(x.state), el("p", { style: "margin:8px 0 0" }, x.doing)),
    sessionDetails(x, g));

  if ((x.managed_by || {}).kind === "manager") {
    view.append(el("div", { class: "panel" }, el("h3", {}, `Managing ${x.managed_by.managing}`),
      el("p", {}, "This session is the AI manager for another session."),
      el("button", { type: "button", class: "btn primary", on: { click: () => { location.hash = `#/t/${x.team}`; } } }, "Open console")));
    return;
  }
  if (!x.can_manage) {
    view.append(el("div", { class: "panel" }, el("h3", {}, "Can't manage this one"), el("p", { class: "note", style: "margin:0" }, x.why_not || "")));
    return;
  }
  if (catalog.read_only) {
    view.append(el("div", { class: "panel" }, el("h3", {}, "Manage with AI"), el("p", { class: "note" }, "This console was started read-only.")));
    return;
  }
  const panel = el("div", { class: "panel" },
    el("h3", {}, "Manage with AI"),
    el("p", {}, "An AI manager leads this session and reviews its work. You can step in at any time."),
    el("button", { type: "button", class: "btn primary", on: { click: () => panel.replaceWith(setupFlow(x, g)) } }, "Manage with AI"));
  view.append(panel);
}

function sessionDetails(x, g) {
  const d = el("details", { class: "details" });
  d.open = !!sessionDetails.open;
  d.addEventListener("toggle", () => { sessionDetails.open = d.open; });
  const copy = (text) => el("button", { type: "button", class: "link", on: { click: async () => {
    try { await navigator.clipboard.writeText(text); toast("Copied."); } catch (_) { toast("Couldn't copy — select it instead."); } } } }, "Copy");
  const row = (label, ...v) => [el("dt", {}, label), el("dd", {}, ...v)];
  const ctx = (c) => c ? [c.text, el("div", { class: "src" }, srcLine(c))] : el("span", { class: "unavail" }, "Unavailable");
  const others = (x.context_sources || []).map((c) => row(c.field === "title" ? "Also titled" : "Also described",
    c.text, el("div", { class: "src" }, srcLine(c))));
  const form = el("div");
  function editForm() {
    const t = el("input", { class: "field-1", value: (x.title && x.title.kind === "manual" ? x.title.text : "") || "", placeholder: "Conversation title", "aria-label": "Title", maxlength: "120" });
    const s = el("textarea", { class: "field", style: "min-height:80px", placeholder: "What is this session working on?", "aria-label": "Work summary", maxlength: "400" });
    s.value = x.summary && x.summary.kind === "manual" ? x.summary.text : "";
    const err = el("div", { class: "err", role: "alert" });
    const save = el("button", { type: "button", class: "btn primary" }, "Save");
    save.addEventListener("click", async () => {
      save.disabled = true;
      try {
        await api("/api/context", { key: x.context_key, title: t.value, summary: s.value });
        if (live(g)) { toast("Saved."); route(); }
      } catch (e) { if (live(g)) { err.textContent = e.message; save.disabled = false; } }
    });
    form.replaceChildren(el("div", { class: "edit" }, t, s,
      el("p", { class: "note" }, "This only describes the session. It doesn't give the relay any control over it."),
      el("div", { class: "dock-row" }, save, el("button", { type: "button", class: "link", on: { click: () => form.replaceChildren() } }, "Cancel")), err));
    t.focus();
  }
  d.append(el("summary", {}, "Details"), el("dl", {},
    row("Folder", x.cwd ? [el("span", { class: "mono" }, x.cwd), " ", copy(x.cwd)] : el("span", { class: "unavail" }, "Unknown")),
    row("Title", ctx(x.title)),
    row("Work summary", ctx(x.summary)),
    x.managed_goal ? row("Managed goal", ctx(x.managed_goal)) : "",
    others,
    row("Conversation", x.identity === "exact" ? "confirmed"
      : (IDENTITY[x.identity_note] || "not confirmed") + " — nothing you type is saved against a guessed conversation"),
    row("Session", el("span", { class: "mono" }, x.client_session_id || "—")),
    (x.limitations || []).length ? row("Limits", x.limitations.join("; ")) : ""),
    x.context_key && !catalog.read_only ? el("button", { type: "button", class: "link", on: { click: editForm } }, "Edit title & summary")
      : !x.context_key ? el("p", { class: "note" }, "Title and summary can be edited once this conversation is confirmed.") : "",
    form);
  return d;
}

function setupFlow(x, g) {
  const st = { who: "new", client: (catalog.new_session_clients || ["claude"])[0], personality: (catalog.personalities[0] || {}).id, existing: null, review: false };
  // Review loop: a new Codex reviewer, an inspectable handover, a fixed relay check and bounds.
  const rv = {
    handover: el("textarea", { class: "field", rows: 6, placeholder: "Architecture, scope (what's in and out), decisions so far, and the task.", "aria-label": "Handover" }),
    done: el("input", { class: "field-1", placeholder: "e.g. The relay check passes and the reviewer accepts.", "aria-label": "Done when", maxlength: "1000" }),
    check: el("input", { class: "field-1 mono", placeholder: "e.g. python3 -m pytest -q", "aria-label": "Check command" }),
    minutes: el("input", { class: "field-1 num", type: "number", min: "5", max: "240", value: "30", "aria-label": "Time limit in minutes" }),
    turns: el("input", { class: "field-1 num", type: "number", min: "2", max: "40", value: "10", "aria-label": "Turn budget" }),
  };
  const others = catalog.sessions.filter((s) => s.key !== x.key && s.can_manage && s.state !== "offline");
  const box = el("div", { class: "panel" });
  const goal = el("textarea", { class: "field", placeholder: "e.g. Add retry logic to the payment client and cover it with tests.", "aria-label": "Goal" });
  const err = el("div", { class: "err", role: "alert" });
  const start = el("button", { type: "button", class: "btn primary big" }, "Start");
  const cname = (c) => c === "codex" ? "Codex" : "Claude";

  function draw() {
    const whoRow = el("div", { class: "choices" },
      choice("A new session", "Pick its style below", st.who === "new", () => { st.who = "new"; draw(); }),
      choice("One of your sessions", others.length ? `${others.length} available — keeps its own style` : "None available", st.who === "existing",
             () => { if (others.length) { st.who = "existing"; draw(); } }));
    let detail;
    if (st.who === "new") {
      const clients = catalog.new_session_clients || [];
      detail = [
        clients.length > 1 ? el("div", { class: "seg", role: "group", "aria-label": "Kind of session" }, clients.map((c) =>
          el("button", { type: "button", "aria-pressed": String(st.client === c), on: { click: () => { st.client = c; draw(); } } }, cname(c)))) : "",
        el("div", { class: "step-label" }, "Its style"),
        el("div", { class: "choices" }, catalog.personalities.map((p) =>
          choice(p.name, p.blurb, st.personality === p.id, () => { st.personality = p.id; draw(); }))),
      ];
    } else {
      if (!st.existing && others[0]) st.existing = others[0].key;
      detail = [el("div", { class: "step-label" }, "Which one"),
        el("div", { class: "choices" }, others.map((o) => choice(o.name, `${o.client} · ${o.doing}`, st.existing === o.key, () => { st.existing = o.key; draw(); })))];
    }
    const handing = [x, st.who === "existing" ? others.find((o) => o.key === st.existing) : null]
      .filter((o) => o && o.route === "handoff");
    const handNote = handing.length ? el("p", { class: "note handoff-note" },
      `${handing.map((o) => o.name).join(" and ")} ${handing.length > 1 ? "are" : "is"} open in a terminal. After you press Start, close ${handing.length > 1 ? "them" : "it"} there `
      + `(Claude: type /exit; Codex: /quit). The relay then continues the same conversation. You can take it back any time from the team's Details.`,
      el("span", { class: "policy" }, " While the relay drives it: "
        + handing.map((o) => o.client === "Codex" ? "Codex works inside this folder with approvals off (workspace-write)"
          : "Claude can't ask for permission, so anything needing approval stops and is shown to you").join("; ") + ".")) : "";
    const note = st.who === "existing" ? "That session leads with its own history and style. Nothing new is started."
      : catalog.fake_mode ? `Starts a pretend ${cname(st.client)} manager (demo mode).`
      : `Starts a new ${cname(st.client)} session in ${home(x.cwd) || "the same folder"}. It uses your ${cname(st.client)} account.`;
    const canReview = st.who === "new" && st.client === "codex";
    if (!canReview) st.review = false;
    const reviewRows = !canReview ? [] : [
      el("label", { class: "check-row" }, el("input", { type: "checkbox", checked: st.review, on: { change: (e) => { st.review = e.target.checked; draw(); } } }),
        " Review loop: Codex reviews (read-only), the relay runs your check after each change, and only a passing check can be accepted"),
      ...(!st.review ? [] : [
        el("div", { class: "step-label" }, "Handover for the reviewer"), rv.handover,
        el("div", { class: "step-label" }, "Done when"), rv.done,
        el("div", { class: "step-label" }, "Check the relay runs (in this folder, no network, can't change files)"), rv.check,
        el("div", { class: "review-bounds" },
          el("label", {}, "Time limit (minutes) ", rv.minutes), el("label", {}, "Turns ", rv.turns)),
      ]),
    ];
    box.replaceChildren(
      el("h3", {}, `Who manages ${x.name}?`), whoRow, ...detail.filter(Boolean), ...reviewRows,
      el("div", { class: "step-label" }, "Towards what?"), goal,
      el("p", { class: "note", style: "margin:12px 0 16px" }, note), handNote, start, err);
  }
  function choice(title, sub, on, fn) {
    return el("button", { type: "button", class: "choice", "aria-pressed": String(on), on: { click: fn } }, el("b", {}, title), el("span", {}, sub));
  }
  start.addEventListener("click", async () => {
    err.textContent = "";
    if (!goal.value.trim()) { err.textContent = "Say what it should work towards."; goal.focus(); return; }
    start.disabled = true; start.textContent = "Starting…";
    try {
      const manager = st.who === "new" ? { new: { client: st.client, personality: st.personality } } : { existing: st.existing };
      const body = { key: x.key, manager, goal: goal.value };
      if (st.review) body.review = { handover: rv.handover.value, done_when: rv.done.value, check: { command: rv.check.value },
        deadline_min: parseInt(rv.minutes.value, 10), max_turns: parseInt(rv.turns.value, 10) };
      const r = await api("/api/manage", body);
      if (live(g)) location.hash = `#/t/${r.team}`;
    } catch (e) { if (live(g)) { err.textContent = e.message; start.disabled = false; start.textContent = "Start"; } }
  });
  draw();
  setTimeout(() => goal.focus(), 0);
  return box;
}

// ================================================================== console
function showConsole(pid, g) {
  const view = $("#view");
  const back = el("button", { type: "button", class: "back", on: { click: () => { location.hash = "#/"; } } }, icon("back"), "Sessions");
  const head = el("div", { class: "c-head" });
  const statusLine = el("div", { class: "status-line", role: "status", "aria-atomic": "true" });
  const question = el("div", { class: "question", hidden: true });
  const chat = el("ol", { class: "chat", "aria-label": "Conversation" });
  const summary = el("section", { class: "summary", "aria-label": "Summary" });
  // Summary first; the full conversation is one click away. The choice is per page visit.
  let tab = showConsole.tab || "summary";
  const tabs = el("div", { class: "seg tabs", role: "group", "aria-label": "View" });
  function paintTabs() {
    tabs.replaceChildren(...[["summary", "Summary"], ["conversation", "Conversation"]].map(([k, label]) =>
      el("button", { type: "button", "aria-pressed": String(tab === k), on: { click: () => {
        if (tab === k) return;
        tab = showConsole.tab = k; paintTabs(); applyTab(); if (k === "conversation") window.scrollTo({ top: document.body.scrollHeight });
      } } }, label)));
  }
  function applyTab() { summary.hidden = tab !== "summary"; chat.hidden = tab !== "conversation"; question.hidden = tab !== "conversation" || !question.dataset.on; }
  paintTabs();
  const details = el("details", { class: "details" });
  details.addEventListener("toggle", () => { showConsole.detailsOpen = details.open; });
  const dock = el("div", { class: "dock" });
  const dockIn = el("div", { class: "dock-in" });
  dock.append(dockIn);
  view.replaceChildren(back, head, statusLine, tabs, summary, question, chat, details);
  applyTab();
  document.body.append(dock);

  const expanded = new Set();
  let chatSig = "", dockSig = "", to = "follower";
  // Persistent composer: its draft and focus survive every refresh and state change.
  const text = el("textarea", { rows: 1, placeholder: "Say something…", "aria-label": "Your message" });
  text.addEventListener("input", () => {
    text.style.height = "auto"; text.style.height = Math.min(200, text.scrollHeight) + "px";
    text.style.overflowY = text.scrollHeight > 200 ? "auto" : "hidden";
  });
  text.addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) send(); });
  const sendBtn = el("button", { type: "button", class: "btn primary", on: { click: () => send() } }, "Send & hand back");

  async function act(path, body, okText) {
    try { await api(path, body || {}); if (okText && live(g)) toast(okText); if (live(g)) await refresh(); return true; }
    catch (e) { if (live(g)) toast(e.message); return false; }
  }
  async function send() {
    if (!text.value.trim()) { text.focus(); return; }
    sendBtn.disabled = true;
    const ok = await act(`/api/team/${pid}/say`, { text: text.value, to }, "Sent. Handed back.");
    if (ok && live(g)) { text.value = ""; text.style.height = "auto"; to = "follower"; }
    sendBtn.disabled = false;
  }
  let armed = 0;
  async function stopManaging() {
    if (Date.now() - armed > 4000) { armed = Date.now(); toast("Tap “Stop managing” again to confirm."); return; }
    const ok = await act(`/api/team/${pid}/release`, {}, "AI management stopped.");
    if (ok && live(g)) location.hash = "#/";  // only leave when it really stopped
  }
  const stopLink = () => el("button", { type: "button", class: "link danger", on: { click: stopManaging } }, "Stop managing");

  function paintDock(d) {
    const sig = JSON.stringify([d.actions, d.attention, d.can_send, d.finishing_turn, d.asker && d.asker.name, d.terminal, to,
      d.handoff_actions, (d.writers || []).map((w) => [w.state, w.reason]), d.unknown_outcome && d.unknown_outcome.turns]);
    if (sig === dockSig) { sendBtn.disabled = !d.can_send; return; }
    dockSig = sig;
    const hadFocus = document.activeElement === text;
    const A = new Set(d.actions);
    const H = new Set(d.handoff_actions || []);
    const rows = [];
    const pending = (d.writers || []).filter((w) => w.state === "awaiting_exit" || w.state === "relay_starting");
    const blocked = (d.writers || []).filter((w) => w.state === "blocked");
    const home_ = (d.writers || []).filter((w) => w.state === "released");
    if (!d.terminal && pending.length) {
      rows.push(el("p", { class: "dock-note" }, pending.map((w) => w.step ? `${w.name}: ${w.step}` : `${w.name}: ${w.words}.`).join(" ")));
      rows.push(el("div", { class: "dock-row" }, el("span"), el("button", { type: "button", class: "link danger",
        on: { click: () => act(`/api/team/${pid}/cancel-handoff`, {}, "Hand-off cancelled.") } }, "Cancel")));
      dockIn.replaceChildren(...rows);
      return;
    }
    const stillWithRelay = (d.writers || []).filter((w) => w.state === "relay_owned");
    if (d.unknown_outcome && (stillWithRelay.length || blocked.length)) {
      // A turn may have run and nobody knows what it did: nothing goes back until you say so.
      const u = d.unknown_outcome;
      rows.push(el("p", { class: "dock-note" }, `Turn ${u.turns.join(", ")} may have run; the relay doesn't know its outcome. `
        + `To give the conversations back anyway, confirm: “${u.statement}”`));
      rows.push(el("div", { class: "dock-row" },
        el("button", { type: "button", class: "btn primary", on: { click: () => act(`/api/team/${pid}/abandon-unknown`, { confirm: true }, "Recorded. You can give them back now.") } }, "Confirm and continue"),
        el("button", { type: "button", class: "link", on: { click: () => { location.hash = "#/"; } } }, "Back to sessions")));
      dockIn.replaceChildren(...rows);
      return;
    }
    if (d.terminal && (stillWithRelay.length || blocked.length)) {
      rows.push(el("p", { class: "dock-note" }, `This team has ended, but the relay still has ${[...stillWithRelay, ...blocked].map((w) => w.name).join(" and ")}. `
        + `Give ${stillWithRelay.length + blocked.length > 1 ? "them" : "it"} back to your terminal when you're ready.`));
      rows.push(el("div", { class: "dock-row" },
        ...stillWithRelay.map((w) => el("button", { type: "button", class: "btn primary", on: { click: () => act(`/api/team/${pid}/return`, { roles: [w.role] }, `${w.name} is back in your terminal.`) } }, `Give ${w.name} back`)),
        ...blocked.map((w) => el("button", { type: "button", class: "btn secondary", on: { click: () => act(`/api/team/${pid}/resolve-handoff`, { endpoint: w.endpoint }, null) } }, `Resolve ${w.name}`)),
        el("button", { type: "button", class: "link", on: { click: () => { location.hash = "#/"; } } }, "Back to sessions")));
      dockIn.replaceChildren(...rows);
      return;
    }
    if (!d.terminal && blocked.length) {
      rows.push(el("p", { class: "dock-note" }, blocked.map((w) => `${w.name}: ${w.reason || w.words}`).join(" ")));
      rows.push(el("div", { class: "dock-row" }, ...blocked.map((w) => el("button", { type: "button", class: "btn secondary",
        on: { click: () => act(`/api/team/${pid}/resolve-handoff`, { endpoint: w.endpoint }, null) } }, `Resolve ${w.name}`)), stopLink()));
      dockIn.replaceChildren(...rows);
      return;
    }
    if (!d.terminal && home_.length && H.has("hand_over_again")) {
      rows.push(el("p", { class: "dock-note" }, `${home_.map((w) => w.name).join(" and ")} ${home_.length > 1 ? "are" : "is"} back in your terminal. The AI waits until you hand it over again.`));
      rows.push(el("div", { class: "dock-row" }, el("button", { type: "button", class: "btn primary",
        on: { click: () => act(`/api/team/${pid}/handover`, {}, "Close it in its terminal; the relay will pick it up.") } }, "Hand over again"), stopLink()));
      dockIn.replaceChildren(...rows);
      return;
    }
    if (d.terminal) {
      rows.push(el("button", { type: "button", class: "btn secondary big", on: { click: () => { location.hash = "#/"; } } }, "Back to sessions"));
    } else if (A.has("send") || A.has("answer") || d.finishing_turn) {
      if (A.has("send") || d.finishing_turn) {
        rows.push(el("div", { class: "seg", role: "group", "aria-label": "Send to" },
          [["follower", d.session.name], ["leader", d.manager.name]].map(([r, n]) => el("button", { type: "button",
            "aria-pressed": String(to === r), on: { click: () => { to = r; paintDock(d); } } }, `To ${n}`))));
      }
      const target = A.has("answer") ? d.asker.name : (to === "leader" ? d.manager.name : d.session.name);
      text.placeholder = d.finishing_turn ? "Finishing the current turn — you can start typing…"
        : A.has("answer") ? `Answer ${target}…` : `Say something to ${target}…`;
      sendBtn.disabled = !d.can_send;
      rows.push(el("div", { class: "compose" }, text, sendBtn));
      const row = [];
      if (A.has("hand_back") || d.finishing_turn) row.push(el("button", { type: "button", class: "btn secondary", disabled: !A.has("hand_back") || d.finishing_turn,
        on: { click: () => act(`/api/team/${pid}/resume`, {}, "Handed back.") } }, "Hand back"));
      row.push(stopLink());
      rows.push(el("div", { class: "dock-row" }, row));
    } else if (A.has("send_again")) {
      rows.push(el("p", { class: "dock-note" }, d.attention === "timeout"
        ? `${d.session.name} took too long on the last turn. It may still finish.`
        : "The relay couldn't confirm the last message arrived. It may or may not have run."));
      rows.push(el("div", { class: "dock-row" },
        el("button", { type: "button", class: "btn primary", on: { click: () => act(`/api/team/${pid}/resolve`, { outcome: "send_again" }, "Sent again.") } }, "Send again"),
        stopLink()));
    } else if (A.has("more_turns")) {
      rows.push(el("p", { class: "dock-note" }, `They've used all ${d.turns[1]} turns.`));
      rows.push(el("div", { class: "dock-row" },
        el("button", { type: "button", class: "btn primary", on: { click: () => act(`/api/team/${pid}/resolve`, { outcome: "more_turns" }, "5 more turns allowed.") } }, "Allow 5 more turns"),
        stopLink()));
    } else if (A.has("intervene")) {
      if (d.attention === "approval") rows.push(el("p", { class: "dock-note" }, "Approve or decline it in that session's own window."));
      rows.push(el("button", { type: "button", class: "btn warn big", on: { click: () => act(`/api/team/${pid}/intervene`, {}, null) } }, "Intervene"));
    } else {
      rows.push(el("p", { class: "dock-note" }, d.reason || "This can't continue."));
      rows.push(el("div", { class: "dock-row" }, el("span"), stopLink()));
    }
    dockIn.replaceChildren(...rows);
    if (hadFocus || (A.has("send") && !d.finishing_turn && !dockIn.dataset.focused)) { text.focus(); dockIn.dataset.focused = "1"; }
  }

  function paint(d) {
    const s = d.terminal ? "done" : d.attention && d.attention !== "approval" ? "you" : d.working ? "working" : "";
    statusLine.dataset.s = s;
    statusLine.replaceChildren(el("span", { class: "dot", "aria-hidden": "true" }), d.handoff_line || d.line);
    head.replaceChildren(
      el("h1", { class: "title", style: "font-size:28px" }, d.goal || "Untitled"),
      el("div", { class: "c-pair" }, avatar(d.manager.client, d.manager.name), el("span", { class: "lbl" }, el("b", {}, d.manager.name)),
        el("span", { class: "arrow", "aria-hidden": "true" }, "→"), avatar(d.session.client, d.session.name), el("span", { class: "lbl" }, el("b", {}, d.session.name))),
      d.writers ? el("div", { class: "owners" }, d.writers.filter((w) => w.state).map((w) =>
        el("span", { class: "owner", "data-own": w.state }, el("b", {}, w.name), ` — ${w.words}`))) : "");
    question.dataset.on = d.asker ? "1" : "";
    if (d.asker) question.replaceChildren(el("b", {}, `${d.asker.name} asks you`), d.asker.text);
    paintSummary(d);
    applyTab();
    details.open = !!showConsole.detailsOpen;
    details.replaceChildren(el("summary", {}, "Details"), el("dl", {},
      el("dt", {}, "Leads"), el("dd", {}, d.manager.name, d.manager.personality ? ` · ${d.manager.personality} style` : ""),
      el("dt", {}, "Follows"), el("dd", {}, d.session.name),
      el("dt", {}, "Turns"), el("dd", {}, `${d.turns[0]} of ${d.turns[1]} used`),
      el("dt", {}, "Team"), el("dd", { class: "mono" }, d.id),
      d.summary && d.summary.folder ? [el("dt", {}, "Folder"), el("dd", { class: "mono" }, home(d.summary.folder), " ", copyBtn(d.summary.folder))] : "",
      d.summary && d.summary.rejected ? [el("dt", {}, "Progress"), el("dd", {}, `${d.summary.rejected} report(s) not shown (invalid); see Events`)] : ""),
      reviewDetails(d), handoffDetails(d));
    const sig = JSON.stringify([d.messages.map((m) => m.id), d.working, [...expanded], d.error]);
    if (sig !== chatSig) {
      chatSig = sig;
      const nearBottom = window.innerHeight + window.scrollY > document.body.scrollHeight - 220;
      const nameOf = (r) => r === "leader" ? d.manager.name : r === "follower" ? d.session.name : "You";
      const items = d.messages.map((m) => {
        if (m.brief) {
          const open = expanded.has(m.id);
          return el("li", { class: "msg brief" }, el("div", { class: "from" }, "Your goal, as given to the manager"),
            el("div", { class: `body${open ? "" : " clamp"}` }, open ? m.body : (d.goal || m.body)),
            el("button", { type: "button", class: "link", on: { click: () => { open ? expanded.delete(m.id) : expanded.add(m.id); chatSig = ""; paint(d); } } }, open ? "Less" : "Show full brief"));
        }
        const long = m.body.length > 500 || m.body.split("\n").length > 7;
        const open = expanded.has(m.id);
        return el("li", { class: `msg ${m.role}` },
          el("div", { class: "from" }, nameOf(m.role), " · ", ago(m.at)),
          el("div", { class: `body${long && !open ? " clamp" : ""}` }, m.body),
          long ? el("button", { type: "button", class: "link", on: { click: () => { open ? expanded.delete(m.id) : expanded.add(m.id); chatSig = ""; paint(d); } } }, open ? "Less" : "More") : "");
      });
      if (d.working && !d.terminal) items.push(el("li", { class: `typing${d.working === "follower" ? " r" : ""}`, "aria-hidden": "true" },
        el("i"), el("i"), el("i"), ` ${nameOf(d.working)}`));
      if (d.error) items.push(el("li", { class: "question" }, el("b", {}, "Couldn't run"), d.error));
      chat.replaceChildren(...items);
      if (nearBottom && tab === "conversation") window.scrollTo({ top: document.body.scrollHeight });
    }
    paintDock(d);
  }
  function noticeLines(ns) {
    const name = (c) => c === "sms" ? "Text" : "Message";
    return ns.map((n) => {
      const when = n.at ? new Date(n.at * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "";
      if (n.state === "delivered") return el("p", { class: "s-note" }, `${name(n.channel)} sent ${when}`);
      if (n.state === "reserved" || n.state === "dispatching") return el("p", { class: "s-note" }, `${name(n.channel)}: sending…`);
      if (n.state === "cancelled") return "";
      const uncertain = n.state === "uncertain";
      return el("p", { class: "s-note warn" }, uncertain ? `${name(n.channel)} may have been sent (${n.reason || "no answer"}). `
        : `${name(n.channel)} failed: ${n.reason || "not delivered"}. `,
        el("button", { type: "button", class: "link", on: { click: () => act("/api/notify", { action: "resend", key: n.key, acknowledge_duplicate: uncertain }, "Sending again.") } },
          uncertain ? "Send again (you may get it twice)" : "Retry"));
    });
  }

  let sumSig = "";
  function paintSummary(d) {
    const S = d.summary;
    if (!S) return;
    // Repaint only when something shown changed (times move once a minute), and never while
    // you're selecting text in it.
    const sig = JSON.stringify([S, d.notices, Math.floor(Date.now() / 60000)]);
    if (sig === sumSig) return;
    const sel = window.getSelection && window.getSelection();
    if (sel && !sel.isCollapsed && summary.contains(sel.anchorNode)) return;
    sumSig = sig;
    const by = (r) => `${r.by.name} reports`;
    const when = (r) => el("span", { class: "when" }, ` · ${ago(r.at)}`);
    const parts = [];
    parts.push(el("p", { class: "s-where" }, S.sessions.map((x) => `${x.name} (${x.client})`).join(" · "),
      S.folder ? ` · ${folderName(S.folder)}` : ""));  // the full path is in Details
    if (S.decision) {
      parts.push(el("div", { class: "s-card decision", role: "region", "aria-label": "Needs your decision" },
        el("h2", {}, "Needs your decision"),
        S.decision.who ? el("b", {}, `${S.decision.who} asks:`) : "",
        el("p", { class: "s-text" }, S.decision.text),
        el("p", { class: "s-how" }, S.decision.how),
        noticeLines(d.notices || [])));
    }
    const nowRows = [el("p", { class: "s-text" }, S.now.relay)];
    if (S.now.reported) nowRows.push(el("p", { class: `s-rep${S.now.reported.stale ? " stale" : ""}` },
      `${by(S.now.reported)}: ${S.now.reported.text}`, when(S.now.reported),
      S.now.reported.stale ? " — may be out of date" : ""));
    parts.push(el("div", { class: "s-card" }, el("h2", {}, "Happening now"), nowRows));
    parts.push(el("div", { class: "s-card" }, el("h2", {}, "What happened"),
      S.happened.length
        ? el("ul", { class: "s-list" }, S.happened.map((h) => el("li", {}, h.text, el("span", { class: "s-by" }, ` — ${by(h)}`), when(h))))
        : el("p", { class: "s-rep" }, "Not reported yet."),
      el("p", { class: "s-facts" }, S.facts.join(" "))));
    if (S.next) parts.push(el("p", { class: `s-next${S.next.stale ? " stale" : ""}` }, el("b", {}, "Next: "), S.next.text,
      el("span", { class: "s-by" }, ` — ${by(S.next)}`), when(S.next), S.next.stale ? " — may be out of date" : ""));
    const foot = [S.updated_at ? `Updated ${ago(S.updated_at)}` : "No updates yet"];
    parts.push(el("p", { class: "s-foot" }, foot.join(""),
      el("button", { type: "button", class: "link", on: { click: () => { tab = showConsole.tab = "conversation"; paintTabs(); applyTab(); window.scrollTo({ top: document.body.scrollHeight }); } } }, "Open the conversation")));
    if (S.runs.length > 1) {
      parts.push(el("p", { class: "s-runs" }, "Runs for this session: ", S.runs.map((r, i) => [i ? " · " : "",
        r.current ? el("b", {}, `${i + 1}. ${r.goal || "Untitled"} (this one)`)
          : el("a", { href: `#/t/${encodeURIComponent(r.id)}` }, `${i + 1}. ${r.goal || "Untitled"}`)])));
    }
    summary.replaceChildren(...parts);
  }

  function reviewDetails(d) {
    const r = d.review;
    if (!r) return "";
    return el("div", { class: "handoff" }, el("div", { class: "step-label" }, "Review loop"),
      el("dl", {},
        el("dt", {}, "Done when"), el("dd", {}, r.done_when),
        el("dt", {}, "Check"), el("dd", { class: "mono" }, r.check.argv.join(" ")),
        el("dt", {}, "Runs"), el("dd", {}, r.check.unavailable ? `Unavailable: ${r.check.unavailable}` : `isolated (${r.check.runner}), up to ${r.check.timeout_s} s`),
        el("dt", {}, "Time limit"), el("dd", {}, r.past_deadline ? "reached" : `${r.minutes_left} min left`),
        el("dt", {}, "Checks"), el("dd", {}, r.checks.length ? r.checks.map((c) => `#${c.revision_n}: ${c.status.replace("_", " ")}${c.latest ? " (latest)" : ""}`).join(" · ") : "none yet"),
        el("dt", {}, "Handover"), el("dd", {}, el("details", {}, el("summary", {}, `${r.handover.bytes} bytes · sha256 ${r.handover.sha256.slice(0, 12)}…`),
          el("div", { class: "body mono", style: "white-space:pre-wrap" }, r.handover.text),
          r.handover.sources.length ? el("div", { class: "note" }, "Sources: ", r.handover.sources.join(", ")) : ""))));
  }

  function handoffDetails(d) {
    const ws = (d.writers || []).filter((w) => w.state);
    if (!ws.length) return "";
    return el("div", { class: "handoff" }, el("div", { class: "step-label" }, "Terminals"),
      ws.map((w) => el("div", { class: "hrow", "data-own": w.state },
        el("b", {}, w.name), ` — ${w.words}`,
        w.resume ? [el("div", {}, "Continue it there: ", el("span", { class: "mono" }, w.resume), " ", copyBtn(w.resume))] : "",
        w.unknown_shutdown ? el("div", { class: "note" }, "The relay can't confirm its writer stopped, so don't reopen it yet.") : "",
        w.writer_process ? el("div", { class: "note mono" }, `Relay writer: pid ${w.writer_process.pid}, started ${
          w.writer_process.started_ms ? new Date(w.writer_process.started_ms).toLocaleString() : "unknown"} (${w.writer_process.started_ms || "?"})`) : "",
        (w.state === "relay_owned" || w.state === "blocked") && (d.handoff_actions || []).includes("back_to_terminal")
          ? el("div", {}, el("button", { type: "button", class: "btn secondary", on: { click: async () => {
              toast("Stopping both sessions and finishing the current turn…");
              const ok = await act(`/api/team/${pid}/return`, { roles: [w.role] }, null);
              if (ok && live(g)) toast(`${w.name} is back in your terminal. The other session waits.`);
            } } }, `Back to terminal`)) : "")),
      el("p", { class: "note" }, "Back to terminal stops both sessions first, lets the current turn finish, then hands that conversation back. The other one waits."));
  }
  // Polls and button-triggered refreshes overlap; never let an older response repaint
  // over a newer one (e.g. "running" after you pressed Intervene).
  let asked = 0, shown = 0;
  async function refresh() {
    const n = ++asked;
    try {
      const d = await api(`/api/console/${encodeURIComponent(pid)}`);
      if (live(g) && n > shown) { shown = n; paint(d); }
    } catch (e) { if (live(g)) toast(e.message); }
  }
  if (!catalog) loadCatalog(g).catch(() => {});
  every(g, 1500, refresh);
}

// A notification link carries only an opaque reference; resolve it here, after sign-in.
async function openDecision(ref, g) {
  $("#view").replaceChildren(el("p", { class: "note" }, "Opening the decision…"));
  try {
    const r = await api(`/api/decision/${encodeURIComponent(ref)}`);
    if (live(g)) location.replace(`#/t/${encodeURIComponent(r.team)}`);
  } catch (e) {
    if (live(g)) $("#view").replaceChildren(el("h1", { class: "title" }, "Nothing to decide here"),
      el("p", { class: "note" }, `${e.message}. `, el("a", { href: "#/" }, "See all sessions")));
  }
}

// ============================================================ notifications
function showNotifications(g) {
  const view = $("#view");
  const box = el("div", {});
  view.replaceChildren(el("h1", { class: "title" }, "Notifications"),
    el("p", { class: "note" }, "Decisions always show up here in Relay. Optionally, Relay can also message you when agents need a decision. "
      + "Everything is off until you set it up, and a message never answers anything: you answer here."), box);
  const CH = {
    webhook: { title: "Message (custom webhook)", fields: [["url", "HTTPS address", "https://…"]],
      note: "Posts {text, link, decision} as JSON to an HTTPS address you control. That's a custom format: Slack, Discord, ntfy and others need their own adapter." },
    sms: { title: "Text message (Twilio SMS)", fields: [["account_sid", "Account SID", "AC…"], ["auth_token", "Auth token", ""], ["from", "From number", "+1…"], ["to", "Your number", "+1…"]],
      note: "Uses your own Twilio account, which charges per message. The credentials stay on this computer, in a private file." },
  };
  async function act(body, ok) {
    try { const r = await api("/api/notify", body); if (live(g)) { if (ok) toast(ok); await load(); } return r; }
    catch (e) { if (live(g)) toast(e.message); return null; }
  }
  function card(name, cur) {
    const c = CH[name];
    const inputs = c.fields.map(([k, label, ph]) => el("input", { class: "field-1", "aria-label": label, placeholder: ph || label,
      type: k === "auth_token" ? "password" : "text", autocomplete: "off" }));
    const code = el("input", { class: "field-1 num", "aria-label": "Code", inputmode: "numeric", maxlength: "6", placeholder: "000000" });
    const state = !cur ? "Not set up" : cur.enabled ? "On" : cur.verified ? "Verified, off" : "Not verified";
    return el("section", { class: "s-card n-card", "aria-label": c.title },
      el("h2", {}, c.title), el("p", { class: "s-text" }, state, cur ? el("span", { class: "s-by" }, ` · ${cur.destination}`) : ""),
      el("p", { class: "note" }, c.note),
      el("div", { class: "n-fields" }, c.fields.map(([k, label], i) => el("label", {}, label, inputs[i]))),
      el("div", { class: "dock-row" },
        el("button", { type: "button", class: "btn secondary", on: { click: () => act({ action: "set_destination", channel: name,
          destination: Object.fromEntries(c.fields.map(([k], i) => [k, inputs[i].value.trim()])) }, "Saved. It's off until you verify it.") } }, "Save destination")),
      cur && !cur.verified ? el("div", { class: "dock-row" },
        el("button", { type: "button", class: "btn secondary", on: { click: async () => {
          const r = await act({ action: "send_code", channel: name }, null);
          if (r && live(g)) toast(r.sent ? "Code sent. Type it below." : `Couldn't send the code: ${r.reason}`); } } }, "Send a code"),
        code, el("button", { type: "button", class: "btn primary", on: { click: () => act({ action: "confirm", channel: name, code: code.value.trim() }, "Verified.") } }, "Verify")) : "",
      cur && cur.verified ? el("div", { class: "dock-row" },
        el("button", { type: "button", class: cur.enabled ? "btn secondary" : "btn primary",
          on: { click: () => act({ action: cur.enabled ? "disable" : "enable", channel: name }, cur.enabled ? "Turned off." : "Turned on.") } },
          cur.enabled ? "Turn off" : "Turn on")) : "");
  }
  function options(cfg) {
    const label = el("input", { class: "field-1", "aria-label": "Display label", value: cfg.display_label, maxlength: "40" });
    const q = el("input", { type: "checkbox", "aria-label": "Include the question", checked: cfg.question_text });
    const base = el("input", { class: "field-1", "aria-label": "Link address", value: cfg.link_base || "", placeholder: "https://… (optional)" });
    const after = el("input", { class: "field-1 num", type: "number", min: "5", max: "1440", "aria-label": "Remind after minutes", value: cfg.reminder.after_min || "" });
    const n = el("input", { class: "field-1 num", type: "number", min: "0", max: "3", "aria-label": "Reminders", value: String(cfg.reminder.max || 0) });
    return el("section", { class: "s-card n-card", "aria-label": "What messages say" }, el("h2", {}, "What messages say"),
      el("label", {}, "Label at the start of each message", label),
      el("label", { class: "check-row" }, q, " Include a short, redacted version of the question (off: only the kind of decision)"),
      el("label", {}, "Link address: an https address you've already set up to reach this computer. Relay doesn't make it reachable or secure; links carry only an opaque reference, never a token or ids.", base),
      el("div", { class: "review-bounds" }, el("label", {}, "Remind after (minutes, empty: never) ", after), el("label", {}, "At most ", n, " reminders")),
      el("div", { class: "dock-row" }, el("button", { type: "button", class: "btn secondary", on: { click: () => act({ action: "options",
        display_label: label.value, question_text: q.checked, link_base: base.value.trim(),
        reminder: { after_min: after.value ? parseInt(after.value, 10) : null, max: after.value ? parseInt(n.value, 10) : 0 } }, "Saved.") } }, "Save")));
  }
  async function load() {
    try {
      const cfg = await api("/api/notify");
      if (!live(g)) return;
      box.replaceChildren(card("webhook", cfg.channels.webhook), card("sms", cfg.channels.sms), options(cfg));
    } catch (e) { if (live(g)) toast(e.message); }
  }
  load();
}

// ================================================================== events
function showEvents(g) {
  const view = $("#view");
  view.className = "view wide";
  const filter = el("input", { type: "search", placeholder: "Filter — topic, event or text", "aria-label": "Filter events" });
  let paused = false;
  const liveBtn = el("button", { type: "button", class: "chip on", "aria-pressed": "true", on: { click: () => {
    paused = !paused; liveBtn.className = `chip${paused ? "" : " on"}`; liveBtn.setAttribute("aria-pressed", String(!paused)); liveBtn.textContent = paused ? "Paused" : "Live"; } } }, "Live");
  const box = el("div", { class: "events", role: "log", "aria-label": "Event bus" });
  view.replaceChildren(
    el("button", { type: "button", class: "back", on: { click: () => { location.hash = "#/"; } } }, icon("back"), "Sessions"),
    el("h1", { class: "title" }, "Events"),
    el("p", { class: "sub" }, "Every change the relay records, newest first. Select one to see the whole record."),
    el("div", { class: "ev-tools" }, filter, liveBtn), box);
  let events = [], sig = "";
  const open = new Set();
  function paint(force) {
    const q = filter.value.trim().toLowerCase();
    const rows = events.slice().reverse().filter((e) => !q || JSON.stringify(e).toLowerCase().includes(q)).slice(0, 400);
    const key = JSON.stringify([q, rows.map((e) => e.event_id), [...open]]);
    if (!force && key === sig) return;
    sig = key;
    const focused = document.activeElement && document.activeElement.dataset && document.activeElement.dataset.ev;
    box.replaceChildren(...(rows.length ? rows.map((e) => {
      const isOpen = open.has(e.event_id);
      return el("div", { class: `e-wrap${isOpen ? " open" : ""}` },
        el("button", { type: "button", class: "e", "data-ev": e.event_id, "aria-expanded": String(isOpen),
                       on: { click: () => { isOpen ? open.delete(e.event_id) : open.add(e.event_id); paint(true); } } },
          el("span", { class: "t" }, new Date(e.at).toLocaleTimeString([], { hour12: false })),
          el("span", { class: "topic", title: e.stream }, `${e.stream} #${e.seq}`),
          el("span", { class: "ty" }, e.type),
          el("span", { class: "d" }, JSON.stringify(e.data))),
        isOpen ? el("div", { class: "e-full" },
          el("dl", {},
            el("dt", {}, "stream"), el("dd", {}, e.stream),
            el("dt", {}, "sequence"), el("dd", {}, String(e.seq)),
            el("dt", {}, "event id"), el("dd", {}, e.event_id),
            el("dt", {}, "at"), el("dd", {}, e.at),
            el("dt", {}, "type"), el("dd", {}, e.type),
            el("dt", {}, "actor"), el("dd", {}, JSON.stringify(e.actor || {})),
            el("dt", {}, "redacted"), el("dd", {}, (e.redacted || []).length ? e.redacted.join(", ") : "nothing")),
          el("pre", {}, JSON.stringify(e.data, null, 2))) : "");
    }) : [el("div", { class: "e-empty" }, "No events.")]));
    if (focused) { const b = box.querySelector(`[data-ev="${CSS.escape(focused)}"]`); if (b) b.focus(); }
  }
  filter.addEventListener("input", () => paint(true));
  every(g, 1500, async () => {
    if (paused) return;
    try { const r = await api("/api/events"); if (live(g)) { events = r.events; paint(false); } }
    catch (e) { if (live(g)) toast(e.message); }
  });
}

// ================================================================== appearance
function themeIcon() {
  const dark = (document.documentElement.dataset.theme || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light")) === "dark";
  $("#theme").replaceChildren(icon(dark ? "sun" : "moon"));
}
try { const t = localStorage.getItem("relay-theme"); if (t) document.documentElement.dataset.theme = t; } catch (_) {}
$("#theme").addEventListener("click", () => {
  const cur = document.documentElement.dataset.theme || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  const next = cur === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem("relay-theme", next); } catch (_) {}
  themeIcon();
});
themeIcon();
route();
