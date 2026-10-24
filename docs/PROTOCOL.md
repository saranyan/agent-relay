# Agent relay protocol (schema version 1)

This document is normative for the relay bus, the pair controller and session
adapters. `README.md` explains how to use it; `INTEGRATION-FEASIBILITY.md` records
what real clients can and cannot do.

## 1. Layers

1. **Durable bus** (`relay/storage.py`, `relay/bus.py`): immutable messages,
   append-only event streams, idempotency records. It never executes message
   content, never invokes a model and never starts a process.
2. **Session adapters** (`relay/adapters/`): discovery, attach, launch, submit and
   lifecycle events, each behind an explicit capability flag. An unsupported
   operation raises `Unsupported`; adapters translate real client events and never
   invent acknowledgments.
3. **Foreground controller** (`relay/controller.py`, `relay console`): binds a
   pair, enforces order and limits, authorizes dispatches, calls adapters,
   records results and stops at boundaries. It runs only while a human keeps it in
   the foreground.

## 2. Relay root

```text
<root>/
  config.json                 immutable after init: schema, participants, limits
  messages/<message-id>.json  immutable envelopes
  idempotency/<sha256>.json   immutable: key -> full envelope + content hash
  identities/<sha256>.json    immutable: physical client session -> its one endpoint
  sessions/<endpoint-id>.json immutable endpoint registrations
  pairs/<pair-id>.json        immutable pair definitions (roles, scope, limits)
  streams/<stream>/<seq>.json append-only event streams (authoritative state)
  evidence/<turn-id>.txt      captured raw responses that could not be published
  views/                      disposable caches rebuilt from streams
  tmp/                        staging for atomic publication
  locks/                      reserved (no advisory locks are used)
```

The root comes from `--root` or `AGENT_RELAY_ROOT`; nothing is created in the home
directory implicitly. Directories are created `0700`, files `0600` (umask `077`).

**Authoritative records** are `config.json`, `messages/`, `idempotency/`,
`sessions/`, `pairs/` and `streams/`. Everything in `views/` (including
`views/control.json`) is a cache that `relay rebuild` regenerates by replaying
streams; readers never trust a view over its stream.

### 2.1 Names and paths

Every path component the relay writes is generated internally and must match
`^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$` with no `..`. User-supplied IDs are validated
against the same pattern before use. Every existing component below the root is
checked with `lstat`; a symlink anywhere inside the root is refused, and files are
opened with `O_NOFOLLOW`. The root itself is resolved once with `realpath`.

### 2.2 Atomic, exclusive publication

`publish(path, bytes)`:

1. write to `tmp/<random>` (same filesystem), `flush`, `fsync`
   (`F_FULLFSYNC` on macOS when `durability=full`);
2. `link(tmp, path)` — fails with `EEXIST` if `path` exists, so publication is
   atomic **and** first-writer-wins;
3. `fsync` the target directory, unlink the temp file.

Readers only look at final names, so they never see a partial record. A crash
leaves at most an orphan in `tmp/`, which `relay rebuild` reports and removes only
when older than one hour.

### 2.3 Event streams

A stream is a directory of `00000001.json`, `00000002.json`, … Each event:

```json
{"schema_version": 1, "stream": "pair-p_…", "seq": 7, "event_id": "e_…",
 "type": "dispatch_authorized", "at": "2026-09-23T21:00:00.000Z",
 "actor": {"kind": "controller", "id": "ctl_…"}, "data": {…},
 "prev_sha256": "<sha256 of event 6 file bytes>"}
```

Append = read the stream, fold it into a state, validate the transition, then
publish `seq+1`. If another writer won `seq+1` (`EEXIST`) the writer re-reads,
**re-validates against the new state** and retries (bounded). Each stream is
therefore a linearizable per-object log; this is the only arbitration mechanism.
There are no advisory locks, so a crashed writer can never hold a lock.

`prev_sha256` chains events. A gap, a malformed event, an unknown
`schema_version` or a hash mismatch makes the stream **untrusted**: it is
reported, nothing is deleted, and any pair depending on it fails closed
(`untrusted` status, no dispatch) until a human inspects it. Other streams keep
working.

Streams: `control` (global pause and authorization gates), `msg-<id>` (manual-mode delivery),
`pair-<id>` (pair lifecycle and turns), `session-<endpoint-id>` (reservation).

**Ordering across streams.** There are no locks to deadlock on; ordering only
matters for crash visibility. An operation touching several objects writes in
this order: immutable definition → `session-*` streams (sorted by id) →
`pair-*` → `control`. `pair create` publishes `pairs/<id>.json`, reserves both
sessions (rolling back the first if the second is taken), then appends
`created`; a crash in between leaves a visible `incomplete` pair and a
reservation a human can release with `relay sessions release`.

**Global pause and gates.** The `control` stream is the single serialization
point for global pause. Every pair dispatch and every manual claim first
appends a `gate{kind, pair|message, …}` event to `control`; a gate is refused
once `paused` is committed. The gate is the authorization point: work whose gate
precedes the pause is shown as in flight; nothing can be authorized after it.
Only then is `dispatch_authorized{gate_seq}` or `claimed{gate_seq}` appended to
the object's own stream (which may still refuse it: pair pause, budget, lost
race — an unused gate authorizes nothing). Global pause and per-pair pause are
independent flags: `relay resume` never lifts a pair pause and `relay pair
resume` never bypasses a global pause.

## 3. Identifiers

| ID | Meaning | Generated by |
|---|---|---|
| participant (`codex`, `claude`) | coordination label from `config.json`; **not** authentication | init |
| endpoint `s_…` | one registered client session (attach or launch) | relay |
| client session ID | the client's own session/thread ID | client |
| connection `c_…` | one adapter connection to an endpoint | adapter |
| controller `ctl_…` | one controller process's ownership of a pair | controller |
| pair `p_…`, thread `t_…`, turn `u_…`, message `m_…`, event `e_…` | | relay |

All generated IDs are `<prefix>_<ms-timestamp hex><random hex>`. Timestamps are
informational; causality is `reply_to`, `turn_id` and stream order.

## 4. Message envelope

```json
{
  "schema_version": 1, "id": "m_…", "pair_id": null, "thread_id": "t_…",
  "turn_id": null, "reply_to": null, "from": "codex", "to": "claude",
  "sender_session": null, "recipient_session": null,
  "created_at": "UTC ISO-8601", "kind": "task", "subject": "…", "body": "…",
  "scope": "…", "approval_refs": [], "evidence_refs": []
}
```

* `kind` ∈ `task result question status review blocked approval_request`. There
  is no `approval` kind.
* `from` may be the pseudo-participant `human` only for a pair's initial request
  to the leader; `human` never receives messages.
* Limits: body ≤ 64 KiB UTF-8, subject ≤ 200 chars, scope ≤ 4 KiB, ≤ 32 refs per
  list, each ref `{"ref": "<path or URL ≤ 1 KiB>", "sha256": optional}`. Over-limit
  input is **rejected** with exit code 6, never truncated.
* References are inert strings. The relay never opens, fetches or expands them;
  `relay thread` marks `m_…` references that do not resolve as `unresolved`.
* A secret-shape scan (private keys, common token prefixes) warns on send and
  makes the controller stop for review. It is a heuristic, not a guarantee.
* Unknown fields or `schema_version` ≠ 1 → the message is reported invalid and
  skipped; other messages are unaffected.

**Idempotency.** `send --idempotency-key K` hashes `(from, K)` into
`idempotency/<sha256>.json`, which stores the canonical content hash (all fields
except `id`, `created_at` and a relay-generated `thread_id`) and the full
envelope. Publishing the idempotency
record is the arbitration point: the same key with identical content returns the
existing message (re-publishing it if a crash left it unpublished); different
content fails with exit code 4. Parallel callers are arbitrated by `link`.

## 5. Manual-mode delivery (`msg-<id>` stream)

Used by `claim/ack/complete/fail/release/recover` for messages **without** a
`pair_id`. Messages that belong to a pair are owned by that pair's controller and
refuse manual claims (the pair stream is their authority).

```text
unclaimed --claim(token)--> claimed --ack(received)--> received
claimed|received --complete(result_id)--> completed      (terminal)
claimed|received --fail(reason)--> failed                (terminal)
claimed|received --release(reason)--> unclaimed          (by token holder)
claimed|received --recover(reason)--> unclaimed          (human; recorded as abandoned)
```

* `claim` first appends a `gate` to `control` (refused while globally paused),
  then records `claimed{gate_seq}`; a claim whose gate preceded a pause is
  displayed as *in flight*, never hidden.
* Every transition after `claim` requires the winning token; stale tokens,
  illegal transitions and a second terminal event are rejected (exit 4).
* `read`, `inbox`, `thread` and `status` never append events.
* Completion is the receiver's report, not proof of the work.

## 6. Sessions (`session-<endpoint-id>` stream)

```text
registered --reserve(pair)--> reserved --release--> registered
reserved --connected(conn)--> connected --disconnected--> reserved
any --closed--> closed   (launched child exited or human removed it)
```

One physical client session maps to exactly one endpoint:
`identities/<sha256(adapter, adapter namespace, client session id)>.json` is
published exclusively before the endpoint record (and records it, so an
interrupted registration is completed on retry). The adapter supplies a
canonical namespace (for example the resolved fake home or Codex home), so
differently spelled options cannot create an alias. **Requested vs confirmed identity.** The registered (or requested) client
session ID is never shown as verified. An identity is confirmed only by a
`session-<id>` event `identity_confirmed{client_session_id, cwd}`, which the
controller appends when the adapter reports what the client itself said: the
fake client's own info file, Codex's `thread/resume`/`thread/start` response,
or Claude's `system/init` or first session-bearing stream message (which may
arrive only after the first prompt; until then the endpoint is shown as
"requested; not yet confirmed"). A reported ID that differs from the requested
one fails closed: the connection reports `unknown`, its results become
identity errors and are never routed, and the pair stops. A client-assigned ID
(requested `pending-…`) is bound through `identities/` like a registration. An endpoint is
reserved by at most one pair at a time. `mode` is `attach`
(pre-existing, user-owned session: **never killed** by the relay) or `launch`.
A launch endpoint is only a registration until a controller runs the pair; the
controller starts the child, records `connected`, and on exit closes it
gracefully (stdin closed / stop request, then SIGTERM, then SIGKILL) and
records `closed`. A launch endpoint that was ever connected is never launched
again: a new process would be a different session, so a human must register a
new endpoint and pair. Reconnection to an attach endpoint must report the same
client session ID recorded at registration; a mismatch is `wrong_session` and
stops the pair.

## 7. Pairs and turns (`pair-<id>` stream)

A pair definition (immutable) names leader and follower endpoints, participants,
thread, scope, `max_turns` (default 10) and `turn_timeout_s` (default 900).

### 7.1 Pair events

| event | precondition | effect |
|---|---|---|
| `created` | first event | status `idle` |
| `controller_acquired{ctl, pid, host}` | no live owner, or `takeover` by human with dead/declared-stale owner | owner = ctl |
| `controller_released{ctl}` | owner = ctl | owner = none |
| `started{initial_message, mode, to, role}` | not started | one commit records Start **and** the initial queue entry; retrying Start with the same initial message returns the existing Start |
| `dispatch_authorized{turn, message, to, role, request_id, gate_seq, timeout_at}` | control gate committed; owner; pair not paused/stopped/finished; nothing in flight; budget left | turn `authorized` → status `delivering` |
| `submit_attempted{turn, request_id}` | turn authorized | turn `submitting` |
| `accepted{turn, client_turn_id}` | turn submitting | status `working` |
| `response_captured{turn, message, action}` | turn accepted | turn `completed` |
| `turn_failed{turn, reason, status?, evidence?}` | turn in flight | status `failed` or `waiting_human` (malformed/oversized/secret-shaped output, not delivered) |
| `delivery_uncertain{turn, reason}` | turn authorized/submitting | status `delivery_uncertain` |
| `turn_timed_out{turn}` | turn in flight | status `waiting_human` (client may still be working) |
| `queued{message, to}` | after a `continue` response | status `queued` |
| `client_waiting{turn, reason}` | turn in flight | client shows an approval/question in its own UI (`reason: null` clears it) |
| `human_needed{reason, status?, message?, routed?}` | not terminal | status `waiting_human` (or `disconnected`) |
| `late_response{turn, message, action}` | turn timed out earlier | response recorded, not routed |
| `finished{message}` | leader `finish` | terminal |
| `paused{by, reason}` / `resumed{by}` | not terminal | pause flag |
| `stopped{by, reason}` | not terminal | terminal: no more routing |
| `detached{endpoint}` | | endpoint connection released |
| `budget_extended{by, add}` | human | `max_turns += add` |
| `reconciled{outcome, turn, requeue?, drop_queue?}` | attention set, nothing in flight | human resolution: `continue` (clear attention, route any captured response), `requeue` (resend the same handoff as a new turn; the earlier attempt may have run), `drop` (clear queue), `route-late` (queue a late response), `answer` (queue a new `human` message, via `relay pair answer`) |

### 7.2 Normal flow

```text
handoff message (durable) -> queued -> dispatch_authorized -> submit_attempted
  -> accepted -> [streaming, not recorded as handoffs] -> response_captured
  -> queued (next handoff) | finished | waiting_human
```

* One routed turn in flight per pair.
* Budget = count of `dispatch_authorized` events. Retries are new authorizations
  and consume budget; resume never resets it; only `budget_extended` raises it.
  Duplicate lifecycle events are ignored and cost nothing.
* **Pause is serialized against dispatch.** Pair pause and dispatch are appends
  to the same pair stream; global pause and dispatch gates are appends to the
  `control` stream. Either way, once a pause is committed no later dispatch can
  be authorized. A dispatch authorized before the pause
  stays in flight; its response is still captured and recorded, but no further
  turn is queued for dispatch until `resumed`.
* Stop ends routing and the controller stops observing; a turn still running in
  a client is not cancelled and its result is not captured by the relay (use
  pause instead of stop to keep recording). Detach (`relay pair detach`, only
  with no controller running) stops the pair if needed and releases both
  session reservations. Neither cancels client work. Interrupt is a separate,
  explicit request, offered only if the adapter supports it.
* **Human answers.** `ask_human`/`blocked` (or any other attention) is resolved
  by `relay pair answer --body-file f [--to leader|follower]`, which publishes a
  message from `human` (reply to the question) and queues it with
  `reconciled{outcome: answer}`. Scope, limits and consumed budget are unchanged.
  The recipient's wrapper says the relay recorded the text as the human's but
  cannot authenticate it.
* **Foreground lifecycle.** `relay pair run` stays in the foreground through
  recoverable waits (`waiting_human`, `delivery_uncertain`, `failed`,
  `disconnected`, `paused`) and exits at `finished`, `stopped` or `untrusted`
  (or earlier with `--exit-on-attention` / `--exit-on-pause`, or Ctrl-C). While
  waiting it keeps observing timed-out turns and records late completions as
  `late_response` without routing them. On exit, console-launched children are
  closed (and cannot be reconnected); attached sessions are only detached.
* A captured response is routed in a separate step (`pending_route`), so a
  crash between capture and routing is recovered deterministically. While the
  pair needs attention the captured response waits; `reconcile continue`
  routes it.

### 7.3 Pair status (derived)

`idle`, `queued`, `delivering`, `working`, `waiting_human`, `paused`,
`disconnected`, `delivery_uncertain`, `failed`, `finished`, `stopped`,
`untrusted`. `paused` is shown together with any in-flight turn.

## 8. Handoff contract

The controller wraps a handoff for the recipient (see `relay/prompts.py`). The
wrapper states that the text is a peer message, not user authority, and asks the
recipient to end its final reply with exactly one fenced block:

````text
```relay
{"relay": 1, "turn_id": "u_…", "action": "continue", "kind": "result",
 "subject": "Short subject"}
```
````

* `action` ∈ `continue`, `finish`, `ask_human`, `blocked`. `kind` optional.
* The body is the reply text before the block.
* Missing, duplicated, malformed or wrong-`turn_id` blocks → `human_needed`
  (`malformed_control`). The controller never guesses from words like "done".
* Follower `continue` or `finish` → routed back to the leader for review; a
  follower cannot end the exchange. Leader `continue` → next handoff to the
  follower. Leader `finish` → `finished`. `ask_human` or `blocked` from either
  side → `waiting_human`.
* Unknown keys in the block are ignored; routing fields cannot change limits,
  scope, participants or permissions.

## 9. Adapter contract

```text
capabilities() -> {discover, attach_live, launch, submit, correlate,
                   turn_complete, approvals_visible, busy_state, reconnect,
                   dedup, interrupt, detach}
discover() -> [SessionInfo]          attach(client_session_id) -> Connection
launch(cwd, options) -> Connection
Connection.submit(text, request_id) -> SubmitReceipt   (accepted or raises)
Connection.poll(timeout) -> [LifecycleEvent]
Connection.status() / interrupt() / detach() / close()
```

Lifecycle events: `accepted`, `turn_started`, `delta`, `tool`,
`approval_pending`, `human_question`, `busy`, `turn_completed{client_turn_id,
request_id, final_text}`, `error`, `disconnected`, `unrelated_turn`.

Adapters marked unverified (see `INTEGRATION-FEASIBILITY.md`) are refused by
the controller unless `AGENT_RELAY_ALLOW_UNVERIFIED=1` is set for an authorized
live test. Before authorizing a dispatch the controller drains pending events (so the
adapter's activity view is current), requires the recipient to report exactly
`idle` (`busy`, `waiting` and `unknown` all fail closed), and re-checks `idle`
immediately before submitting. The Codex adapter derives activity only from the
server: the thread status returned at attach/launch, `thread/status/changed`,
and `turn/started`/`turn/completed` (including turns started by a person in the
TUI). A server-side race remains: a person can start a turn between the check
and `turn/start`; the server then refuses (reported as *not submitted*) or, per
its schema, may steer the active turn — this needs the live check listed in
`INTEGRATION-FEASIBILITY.md`. Native approval requests are tracked by JSON-RPC
request id; the waiting flag clears only when every open request on that turn
has a `serverRequest/resolved`.

Only a `turn_completed` correlated with the dispatched request/turn becomes a
handoff. Deltas, tool output, reasoning, echoes of submitted input, duplicate
completions and turns started by someone typing in the client are never
forwarded; an unrelated turn while a routed turn is pending stops the pair
(`ambiguous_attribution`).

## 10. Crash and recovery semantics

| crash point | on restart |
|---|---|
| before message publication | nothing visible; idempotent send may be retried |
| during Start | the initial message is idempotent (`start:<pair>`) and `started` carries the queue entry, so a retried Start either commits once or returns the existing Start; never a started pair with nothing queued |
| after publication, before claim/queue | message visible, unclaimed/queued |
| after `dispatch_authorized`, before `submit_attempted` | no submission happened → `turn_failed{not_submitted}`, requeue only with human `reconcile` |
| after `submit_attempted`, before `accepted` | **`delivery_uncertain`**; if the adapter supports `dedup` + lookup by request ID it reconciles, otherwise a human must reconcile. Never auto-resubmitted. |
| after `accepted`, before capture | reattach and keep waiting if the adapter can look up the turn; else `delivery_uncertain` |
| after capture, before `response_captured` | response message is published with idempotency key = turn ID, so replay is safe |
| controller dies holding ownership | stale owner shown; `pair takeover` records the human decision |

Restart rebuilds status from streams and shows the recovery state. It never
resumes routing on its own and never attaches to a different session.

## 11. Authority limits

* Participant labels, endpoint IDs, leader status and same-user files are not
  tamper-resistant identity. Anything that runs as the same OS user can write the
  root. The hash chain detects corruption, not a malicious same-user writer.
* Pressing Start authorizes routing within the shown scope and limits only. A
  message that says "approved" authorizes nothing; `approval_refs` are claims the
  recipient must verify in its own authorized context or take to the human.
* Native approval prompts stay in the client. The controller never answers them;
  an `approval_pending` event puts the pair in `waiting_human`.
* The relay executes nothing from message bodies. Delivery is at least once with
  durable deduplication where the adapter allows it. Exactly-once inference,
  task execution or external effects are not claimed.
