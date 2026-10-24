# Session context: discovery audit and proposed reporting contract

Status: **the audit, the context display and the revised reporting contract (section 5) are implemented, following Codex's decisions in section 4. Reporting is descriptive only; real control adapters stay locked.**

## 1. Read-only discovery audit (2026-10-02, this machine)

**Method.**
- The process table and the open-file table.
- Codex's local SQLite opened with `mode=ro`. Only the `id`, `cwd`, `name`, `updated_at` and `archived` columns and turn statuses are queried, never `first_user_message` or `preview`.
- The last 256 KB of each running Claude session's transcript.

**Corrected wording.** The Claude title reader does **read transcript bytes**, and that tail contains message content. It parses only `ai-title` records out of it. **No message content is extracted, stored or shown**, but it would be wrong to say the bytes aren't read.

Credentials weren't read. Nothing was started, resumed or sent.

**Clients:** Claude Code 2.1.x and codex-cli 0.160.0.

| | Running Claude sessions | Running standalone Codex sessions |
|---|---|---|
| Found | 2/2 via `claude agents --json` | 2/2 via `ps` (interactive `codex` processes) |
| Identity | `sessionId`: exact | `codex resume <id>`: **exact** (1 of 2). Plain `codex` / `codex resume`: **likely**, as the newest thread updated in the process's folder since it started (1 of 2) |
| Folder | `cwd` | `lsof -d cwd`; an exact match uses the thread's own `cwd` (e.g. `AI-ledger/truck-stop`) |
| Title | `ai-title` record, the last one in the transcript tail. Claude's own title | `threads.name` in `~/.codex/state_5.sqlite` (33 of 81 threads named) |
| Activity | `status` busy/idle | latest `thread_turns.status` (in progress means busy) |
| Work summary | **none available** | **none available.** `preview` / `first_user_message` are the first prompt, so not used |
| Can manage | **no**: open in a terminal, no driving interface | **no**: standalone TUI; only threads on the shared app-server could be driven (unverified, locked) |

**Finding: titles don't separate two sessions in the same folder.** The two agent-relay conversations (Codex as architect, Claude as implementer) are titled "Build context from BRIEF" and "Build context from the BRIEF". Both titles derive from the same first prompt. A real *work summary* therefore needs:
- work the relay recorded (for managed sessions),
- the session's own report (section 3),
- or your manual edit.

The console now shows "Work summary unavailable" until one exists.

**The earlier conclusion was wrong.** "Standalone Codex sessions are undiscoverable" was true only of the `codex-app-server` adapter. A read-only method exists: the new `codex-local` adapter. Its limits are:
- a plain `codex resume` process is matched by inference;
- archived threads are excluded;
- `ps`/`lsof`/schema layouts are as observed on macOS with codex-cli 0.160. Other versions may change the schema; on failure the adapter reports an error and lists nothing, and never guesses.

## 2. What the console shows (implemented)

Each Home row shows:
1. **folder · client** — for a relay-started manager, its role instead, e.g. "Reviewer · Claude";
2. the **work summary**, or else the **title**, or else *Work summary unavailable*;
3. the folder path · who manages it, with activity on the right.

Details shows:
- the full folder path, with Copy;
- the title and summary, each with **source and age**, and *may be out of date* after 6 hours;
- every other known value ("Also titled…");
- the session ID and the limits.

Search covers folder, title and summary.

**Precedence:**

| Field | Order |
|---|---|
| Title | **your edit, pinned until cleared** → a report that attaches (section 5) → the client's own title |
| Summary | **your edit, pinned until cleared** → a report that attaches → client metadata |
| Managed goal | shown **separately**, as "Goal: …", only when there's no summary or title; it never replaces yours |

Nothing is discarded; lower-ranked values appear in Details. Context lives in its own append-only `ctx-<hash>` stream, keyed by `client:session-id`. It is independent of endpoints, reservations and pairs, so describing a session never grants control over it. Only sessions currently listed (running) get context shown; saved history alone doesn't list a session.

## 3. Original proposal (superseded by sections 4 and 5)

**Goal.** An ordinary running Claude or Codex session states what it's working on, at no extra model cost and without becoming relay-controlled.

**Command.** One short shell command the session runs as part of work it's already doing:

```sh
relay context report --root <relay-root> \
  --client claude --session "$CLAUDE_CODE_SESSION_ID" --cwd "$PWD" \
  --title "Dashboard work" --summary "Implementing the dashboard and control refinements"
```

**Identity**
- *Claude:* `CLAUDE_CODE_SESSION_ID` is set in every shell a Claude Code session runs (verified here).
- *Codex:* an equivalent variable is **not yet verified**. Until it is, `--session` must be supplied, and the report is matched only when it equals a discovered `exact` thread.
- A report is a **claim**. It attaches to a listed session only when `(client, session)` matches one currently discovered **and** `cwd` matches that session's folder. Otherwise it's kept as *unmatched* and not shown on Home.

**Storage**
- An append-only `ctx-<hash>` event with `source: "report"`, plus the reporter's pid and host.
- Limits: title ≤ 120 characters, summary ≤ 400, single line; no more than one report per session per 10 seconds; larger ones are rejected, not truncated.

**When it runs**
- (a) The session itself, when its work changes. Ask it in your prompt or project instructions, e.g. a line in `CLAUDE.md`/`AGENTS.md`. It costs one shell call inside a turn already happening.
- (b) Optionally, a client **hook** the user installs explicitly, such as a Claude Code `Stop` hook. That reports with no model involvement at all, but it's a persistent configuration change, so it's opt-in and never installed by the relay.

**Guarantees**
- A report never registers an endpoint, reserves a session, creates a pair, or changes what the console may do.
- It's descriptive only. The page labels it "from the session itself · <age>".
- It may be stale or wrong, so *may be out of date* still applies.
- Same-user files are not authentication (as elsewhere in this relay).

**Open questions for Codex**
1. Is cwd-plus-exact-id matching enough, or should reports also require the reporting process to be a descendant of the discovered session's pid?
2. Should a report be able to override your manual summary, or should yours win until you clear it?
3. Is the Codex identity variable worth verifying now? That needs a no-model check inside a Codex shell.

## 4. Codex review decisions (2026-10-02)

The context UI direction is accepted. Reporting can be implemented after the
identity corrections below; this is not approval for real relay control.

1. **Reports are claims, not authenticated self-reports.** Match canonical cwd and
   exact conversation identity. Record whether process ancestry corroborates the
   claim, when available; only a corroborated report may say "from the session
   itself". Otherwise label it "reported context; reporter not verified". Neither
   path grants control. Unmatched claims stay out of the live session list.
2. **Manual values stay pinned per field until cleared.** A later report must not
   silently replace the user's title or summary. Keep report/client alternatives
   in Details. Show a managed goal separately from the pinned conversation summary.
3. **Codex identity:** `CODEX_THREAD_ID` is present in this Codex shell (checked
   without printing its value or making a model call). Verify correspondence to
   the local thread record and canonical folder before adopting it; presence alone
   is not proof. No server, resume or new model session is needed for this check.

### Required identity corrections

- Two plain Codex processes in one folder currently choose the same newest thread.
  Reproduction with two injected processes yields two discovery records but one
  `client_session_id`; `Host.catalog` then drops one through its dictionary key.
  Keep uncertain process identities separate (PID plus process start time), store
  candidate thread IDs separately, and never persist manual context to a guessed
  conversation ID. Show uncertainty on the row, not just in a title source.
- `codex fork <id>` names the source conversation, not the fork's confirmed current
  identity. It must not take the exact-resume path. Add a regression case.
- Unknown turn-history statuses must yield unknown activity. The current adapter
  classifies every non-running value as idle. Whitelist known completion states.
- Correct the audit's "never read prompts/message content" claim: the Claude title
  reader loads a transcript tail, which contains message bytes, and extracts title
  records. State that content is not surfaced; do not claim the bytes weren't read.

Test colliding candidates, candidate changes, fork source IDs, unknown activity,
manual/report precedence and unverified reports before reporting completion.

## 5. Implemented reporting contract (after section 4)

**Command**

```sh
relay context report --root <relay-root> --client claude|codex \
  [--session <id>] [--cwd <folder>] [--title "…"] [--summary "…"]
```

**Identity**
- The session ID comes from `--session`, or from `CLAUDE_CODE_SESSION_ID` / `CODEX_THREAD_ID` inside the session.
- **Before use, it's verified read-only** against the client's own records for that canonical folder:
  - **Claude:** the conversation's transcript file exists for that folder.
  - **Codex:** `threads.id` exists, and `realpath(threads.cwd)` equals the folder.
- If the ID can't be confirmed, the command fails (exit 6) and nothing is stored. A variable being present is not proof.

**Storage and limits**
- Each report is an append-only `ctx-<hash>` event with `source: report`, the canonical folder, and the reporter's process ancestry (PID and start time for each level).
- Title ≤ 120 characters; summary ≤ 400, single line; one report per conversation per 10 seconds.
- A report registers nothing, reserves nothing, pairs nothing, and changes no permission.

**How a report is shown**

| Report matches a listed session with… | Shown as |
|---|---|
| no confirmed identity, or a different folder | not shown; unmatched claims stay out of the list |
| the same confirmed identity and canonical folder, but ancestry doesn't reach the session's process | **"reported context; reporter not verified"** |
| the same, **and** the ancestry contains the session's own PID with the same start time (±2 s) | **"from the session itself"** |

Same PID with a different start time means PID reuse, and doesn't corroborate.

**Uncertain Codex processes**
- A plain `codex` / `codex resume` keeps a process identity (`pid-<pid>-<start>`), with at most one *likely* candidate thread.
- Two processes guessing the same thread are both *ambiguous* and claim nothing.
- `codex fork <id>` records the fork's source and never matches it.
- Nothing you type is saved against a guessed conversation; the edit isn't offered until the conversation is confirmed.

**Proposed for Codex review (implemented, tested, easy to remove).** A *likely* candidate becomes confirmed when a report **about that candidate thread** is corroborated by this very process's ancestry. Without this, a running Codex session's verified report could never attach to its row.

**Verified on this machine (2026-10-02)**
- This Claude session ran `relay context report --client claude` (identity from `CLAUDE_CODE_SESSION_ID`, verified).
- Its row then showed **"from the session itself"**, corroborated by the process chain.
- Codex's `CODEX_THREAD_ID` path is implemented and tested against a Codex-shaped database, but **not yet run from Codex's own shell**. That needs one `relay context report --client codex …` from the Codex session. It's a local command with no model call.

## 6. Review round R1/R2 and a known discovery gap (2026-10-02)

**R1: start-time evidence is required.**
- A report counts as "from the session itself" only when **both** of these are present, valid and agree (±2 s):
  - the discovered session's start time;
  - the matching ancestor's start time.
- If either is missing, zero, negative or not a number, the report is *reporter not verified*, and it can't confirm a likely Codex row.
- Fake sessions now record their start time from `ps`, as real ones do.

**R2: the rate limit is atomic.** The "one report per 10 s" check now runs inside the stream's serialized append. A writer that loses the race re-checks against the winner's event, so two concurrent reports can't both be accepted.

**Known gap: Codex sessions hosted by the Codex app-server aren't listed.**
- **Codex's real report.** Run from Codex's review shell, the report verified (its `CODEX_THREAD_ID` exists, and the folder matches). Its recorded ancestry is shell → `codex` app-server worker (35723) → app-server daemon (6153, `~/.codex/packages/app-server-daemon/…`).
- **What `codex-local` lists.** Only interactive `codex` terminal processes. Here those are 43821 and 3480, and neither is in that chain. The *likely* guess for 3480 was a different conversation.
- **Why PID matching can't work here.** One app-server process hosts many conversations. Its PID and start time can't say which conversation reported, so PID corroboration can't identify the conversation under the app-server.
- **Decision.** This environment is **not yet supported for live-row association.** The report stays stored but unshown, and association is not loosened to hide the gap. A saved report is never listed as a live session.
- **Next.** Mapping app-server-hosted conversations needs its own investigation: a read-only, per-conversation signal from the app-server, not process ancestry.
