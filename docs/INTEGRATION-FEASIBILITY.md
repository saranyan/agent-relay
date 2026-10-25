# Integration feasibility: attaching to and launching Claude and Codex sessions

Status: **stage 1 feasibility report (a snapshot).** Nothing here was verified
with a live model turn. No prompt was sent to any real session, no daemon was
started, and no credential or transcript was read.

> **What has changed since this snapshot.** The capability matrix below records
> what was observed at the time and is kept as history. Where it differs from the
> current code, the current state is:
>
> - **Standalone Codex sessions are discoverable, read-only.** The `codex-local`
>   adapter lists running Codex terminals from the process table and Codex's local
>   state. It can't drive them, and it marks identities that are inferred rather
>   than confirmed as uncertain. See [CONTEXT-REPORTING.md](CONTEXT-REPORTING.md).
> - **Claude titles come from transcript bytes.** `claude-agents` reads the tail
>   of each session's transcript, which contains message content, but extracts
>   only `ai-title` records. Message content is never stored or shown.
> - **Verification levels differ.** The Python test suite is run independently.
>   Browser checks of the web console were run and reported by an agent. The demos
>   use simulated sessions. **Launch is verified live:** on 2026-10-25, Codex
>   0.163.0 led Claude Code 2.1.310 through a bounded 2-turn run. Attach and
>   hand-off are still unverified.

## Versions and method

| Client | Version | Binary |
|---|---|---|
| Claude Code | 2.1.281 | `~/.local/bin/claude` |
| Codex CLI | codex-cli 0.156.1 | `~/.local/bin/codex` |

Evidence tags:

- **[H]** local `--help` output
- **[S]** schema from `codex app-server generate-json-schema` (with `--experimental` for experimental methods)
- **[O]** local read-only observation
- **[D]** official documentation: code.claude.com/docs/en/{agent-view, sessions, headless, remote-control, cross-session-messaging, channels, channels-reference, agent-sdk}; learn.chatgpt.com/docs/{app-server, non-interactive-mode}; the github.com/openai/codex app-server-daemon README, PR #39092 and issue #45976
- **[I]** inference, not verified

### Local observations [O]

- `claude agents --json` listed 3 interactive sessions with `sessionId`, `cwd`, `pid`, `kind`, `name` and `status` (all `busy`, including this builder session).
- `codex app-server daemon version` failed with `app-server-control.sock` missing, so the shared Codex daemon is not running.
- Two standalone `codex` TUI processes are running (codex 0.153.3 and 0.156.1 code-mode hosts).

## Capability matrix

Key: ✅ supported, documented and seen locally · ⚠️ partial or docs-only · ❌ unsupported · ❓ unknown.

Columns:
1. Discover and identify
2. Live attach with context preserved
3. Launch
4. Submit and correlate
5. Authoritative turn completion
6. Approvals, questions, busy state and errors
7. Reconnect and deduplication
8. Interrupt and detach

| Surface | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 |
|---|---|---|---|---|---|---|---|---|
| **Claude interactive TUI** | ✅ `claude agents --json` [H,O] | ❌ No documented way to deliver a turn from another process. `--resume <id>` on a running session starts a **second writer** on the same transcript [D] | ✅ `claude` | ⚠️ Per-session inbox socket (`CLAUDE_CODE_MESSAGING_SOCKET`): wire format undocumented, no receipt ID [D] | ⚠️ Only an idle `status`, or a `Stop` hook [I] | ⚠️ `status: waiting` plus `waitingFor` [D] | ❌ | ❌ from outside |
| **Claude background (`--bg` / `attach`)** | ✅ id and state [H,D] | ⚠️ `claude attach <id>` joins the same live process, but only as a human terminal [H,D] | ✅ `claude --bg` [H] | ❌ No CLI or API to send. `--resume --bg` on a running session "starts a copy" [H] | ⚠️ `state: done` [D] | ✅ `state: blocked` plus `waitingFor` [D] | ⚠️ Agent-view replies are human-only [D] | ✅ `claude stop`; detaching never stops the session [H,D] |
| **Claude `-p` stream-json** | ✅ `system/init` returns `session_id` [D] | ❌ The relay owns this process; resume is a continuation, not an attach [D] | ✅ `--input-format stream-json --output-format stream-json`, `--session-id` [H] | ✅ `--replay-user-messages` echoes the user message `uuid` back as acceptance [H] | ✅ A `result` message ends each turn [D] | ⚠️ `--permission-prompts none` denies and reports `permission_denials`; `host` needs an SDK host [H,D] | ⚠️ SIGTERM leaves the turn unfinished [D] | ⚠️ SDK `interrupt()` [D] |
| **Claude Remote Control** | ⚠️ | ✅ Same live session, for claude.ai and the mobile app [D] | ✅ `--remote-control` [H] | ❌ No third-party API [D] | ❌ | ❌ | – | ⚠️ |
| **Claude channels (research preview)** | – | ✅ Pushes into the running session [D] | ⚠️ Needs a `--channels` launch flag with an allowlisted plugin | ⚠️ `notifications/claude/channel`: no acknowledgment, no turn ID [D] | ❌ No completion event | ✅ Permission relay (`request_id`, allow/deny) [D] | ❌ | ❌ |
| **Codex TUI (standalone)** | ⚠️ History only, after the fact (*superseded: now read-only discovery through `codex-local`*) | ❌ Its embedded server is isolated to that one client [D]. `codex resume` and `fork` start new processes [H] | ✅ `codex` | ❌ [I] | ❌ | ❌ | ❌ | ❌ |
| **Codex TUI on the shared daemon (`--remote unix://`)** | ✅ `thread/loaded/list` [S] | ✅ `thread/resume{threadId}`: "If thread_id identifies a running thread, app-server rejoins that thread" [S] | ✅ `codex --remote unix://`, with the daemon started [H] | ✅ as the next row | ✅ | ✅ | ⚠️ | ✅ |
| **`codex app-server` (stdio or daemon proxy)** | ✅ `thread/list`, `thread/loaded/list` [S] | ✅ as above [S] | ✅ `thread/start{cwd, approvalPolicy, sandbox}` [S] | ✅ `turn/start{threadId, input, clientUserMessageId}` returns `turn.id` [S] | ✅ `turn/completed{turn.status: completed\|interrupted\|failed}`, separate from `item/*/delta` [S] | ✅ Server requests `item/*/requestApproval` and `item/tool/requestUserInput`, then `serverRequest/resolved`; `thread/status/changed` [S] | ⚠️ `thread/turns/list` can reconcile; `clientUserMessageId` dedup semantics are undocumented [S,I] | ✅ `turn/interrupt`; `thread/unsubscribe` [S] |
| **`codex exec --json`** | ✅ `thread.started` [D] | ❌ `exec resume` reloads from disk [H] | ✅ | ⚠️ One prompt per process | ✅ `turn.completed` / `turn.failed` [D] | ❌ Policy is fixed up front | ❌ | ⚠️ Signal only |
| **`codex queue` (experimental)** | – | ⚠️ `--thread <id>` [H] | – | ⚠️ `thread/queue/add` returns `queuedSubmission.id` [S] | ❌ No consumption receipt (issue #45976) | ❌ | ⚠️ | – |

## What the relay implements now

| Adapter | Status | Modes | Notes |
|---|---|---|---|
| `fake` | verified (repository contract) | attach, launch | Fake terminal clients used by tests and the walkthrough. Proves the relay logic only, not real-client behaviour. |
| `claude-agents` | verified, locally, for discovery only | discover | Lists running Claude sessions with their limits. Titles are parsed from the transcript tail (only `ai-title` records are extracted). `sessions attach` refuses with exit code 9 and never substitutes resume, fork or launch. |
| `codex-local` | verified, locally, for discovery only | discover | Read-only listing of running standalone Codex terminals. A plain `codex resume` process is matched by inference and shown as uncertain. Archived threads are excluded. On an unrecognized layout it reports an error and lists nothing. |
| `claude-stream` | launch **verified** (2.1.310) | launch | `claude -p` stream-json child with `--replay-user-messages` for acceptance, `result` for completion and `--permission-prompts none` (the relay never answers a prompt). |
| `codex-app-server` | launch **verified** (0.163.0); attach unverified | launch (stdio), attach (daemon via `codex app-server proxy`) | `turn/start` plus `turn/completed`. Activity comes only from server status (initial thread status, `thread/status/changed`, external `turn/started`/`turn/completed`); unknown means no dispatch. Attach first checks `thread/loaded/list` and refuses a thread that isn't live, rather than reopening stored history. Approvals are tracked by JSON-RPC request ID and never answered. Launch defaults to `approvalPolicy: never` and `sandbox: workspace-write`, because a headless child has no native approval UI. Wire-level contract tests use recorded notification shapes; launch passed a live run on 2026-10-25. |

The controller refuses modes that haven't been verified live (today: attach) unless
`AGENT_RELAY_ALLOW_UNVERIFIED=1` is set. That should happen only after you pick the sessions and authorize a live test.

## Surface by surface

**Attach mode.**
- **Codex:** feasible only for threads loaded in the shared app-server daemon. Standalone Codex TUIs cannot be attached (they can now be *listed* read-only through `codex-local`). Making a Codex session attachable means starting it with `codex --remote unix://` and the daemon running, which changes how you start Codex.
- **Claude Code 2.1.281:** there is **no verified adapter that satisfies this relay's contract** (targeted submission with a receipt, plus a correlated completed-turn signal) for a running session. Live-session messaging does exist in other forms; the gap is that none is shown to meet this contract. The relay does **not** fall back to `--resume`, which the docs say interleaves two writers into one transcript. The candidates are:
  - (a) the cross-session inbox socket: undocumented format, no receipt;
  - (b) channels: research preview, needs a launch flag and gives no completion signal;
  - (c) a human in agent view.

  None meets the relay's bar for automatic live attach.

**Launch mode.**
- **Claude:** feasible through stream-json. Correlation and completion are documented; permission prompts cannot be answered natively in a headless child.
- **Codex:** feasible through `codex app-server`, with the strongest lifecycle contract of any surface.

**Mixed pairs** work in the relay today with any combination of fake attach and launch. For real clients the realistic near-term mixes are:

| Pair | Status |
|---|---|
| Codex attach (daemon) + Claude launch | ✅ feasible |
| Codex launch + Claude launch | ✅ feasible |
| Claude attach + anything | ❌ blocked |

**Manual bridge fallback.** The manual-mode bus commands (`send`, `inbox`, `claim`, `reply`, `complete`) work with any session a human is driving. This is clearly a fallback. It does not satisfy automatic live attach.

## Unresolved gaps: decisions needed

1. **Claude live attach.** Choose one:
   - (a) accept launch-only for Claude, where the relay owns a `claude -p` stream-json child;
   - (b) authorize an experiment with the cross-session inbox socket or channels, labelled unverified, with no receipt or completion guarantee;
   - (c) keep Claude attach as manual bridge only.

   The relay never quietly reduces scope, so this needs your explicit choice.
2. **Codex attach prerequisite.** Is it acceptable to start Codex as `codex --remote unix://` with the shared daemon (`codex app-server daemon start`)? This is a visible change to how Codex is started, not a hidden setup step.
3. **Approvals in launched children.** A headless child cannot show a native approval prompt. The defaults are: Claude denies anything that would prompt (`--permission-prompts none`), and Codex uses `approvalPolicy: never` inside a `workspace-write` sandbox. Alternatives are pre-authorized tool allowlists set in each client's own settings, or attaching instead of launching.
4. **Experimental Codex APIs** (`thread/queue/*`) are not used. The relay uses stable `turn/start` and `turn/completed`.
5. **Codex reconciliation after a controller crash.** `thread/turns/list` lookup is not implemented, so a crash mid-turn is reported as *delivery uncertain* for the human to resolve. `clientUserMessageId` is sent, but it is not relied on for deduplication.
6. **Codex steer versus error.** Docs say `turn/start` during an active turn errors; the schema hints it may steer. The relay never submits while the thread reports an active turn, and this needs a live check.
7. **Multiple subscribers and approvals (Codex attach).** When the TUI and the relay are both subscribed, the TUI answers approvals natively and the relay only observes `serverRequest/resolved`. This needs a live check.

## Live acceptance plan (after authorization)

In an isolated scratch workspace, with a harmless bounded task (for example, "create `hello.txt` containing one line, then report"), `max_turns` 4, timeout 5 minutes:

1. **Codex attach.** Start `codex --remote unix://` with the daemon, give it one prior exchange, register it with `relay sessions attach --adapter codex-app-server`, and pair it with a launched Claude (`claude-stream`). Check that the original thread ID is unchanged, that the prior exchange is still in context, and that no old turn was replayed.
2. **Launch.** Pair two launched sessions and check targeted delivery, completion, pause with a turn in flight, and child cleanup.
3. Record client versions and observed limits in this file. Copy no transcripts or credentials.

Step 2 passed reply-only on 2026-10-25 (Codex 0.163.0, Claude Code 2.1.310): both sessions confirmed, 2 turns delivered and completed, the leader finished, both children exited, the folder stayed empty. Pause with a turn in flight and a file task are still to do. Step 1 (attach) is still **unverified**.
