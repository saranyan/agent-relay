# agent-relay

**Let one AI coding session manage another, while you stay in charge.**

<picture>
  <source media="(prefers-color-scheme: light)" srcset="docs/images/home-light.png">
  <img alt="The agent-relay web console listing Claude and Codex sessions, one needing a decision and two managed by AI" src="docs/images/home-dark.png">
</picture>

> **This is a prototype.** It works end to end with the bundled pretend clients, and
> it's built to be careful with real ones. The real Claude Code and Codex adapters
> haven't passed a recorded live run yet, so they stay switched off unless you opt
> in. Treat it as a working sketch of an idea to try, fork and extend.

## The story

You have two terminals open. In one, Codex is good at planning and reviewing. In
the other, Claude Code is good at writing the code. You've become the courier
between them. You copy the plan from one, paste it into the other, wait, copy the
result back, ask for a review, and paste the fixes over again. It works, but you're
the slowest part of the loop, and you can't step away from your desk.

**agent-relay does the carrying.** You pick one session as the **leader**, the one
that plans and reviews. You pick another as the **follower**, the one that does the
work. Then you give them a goal, a turn budget and a time limit. The relay passes
each turn across, writes every message to disk, and keeps going until the work is
done or something needs you.

**It stops for you.** A session asks a question, a client shows an approval prompt,
a reply looks like it contains a secret, a turn times out, or delivery becomes
uncertain. Any of these pauses the loop, and it doesn't continue until you decide.
Messages that *claim* approval grant nothing. Only you can approve.

**You can watch from anywhere on your machine.** `relay serve` opens a local web
page. It shows every Claude and Codex session the relay can see, what each is
doing in plain language, and which ones are waiting for you. You can hand a session
to an AI manager with a style (Reviewer, Planner or Pair programmer), watch the
conversation, and press **Intervene** to take back control at any moment.

<table>
  <tr>
    <td width="50%"><img alt="Summary of a session managed by an AI reviewer" src="docs/images/summary.png"><br><sub><b>Watch:</b> what the managed session is doing, in plain language.</sub></td>
    <td width="50%"><img alt="A session stopped to ask the user a question, with an answer box" src="docs/images/decision.png"><br><sub><b>Decide:</b> questions stop the loop until you answer.</sub></td>
  </tr>
</table>

**Nothing gets lost.** Every message is immutable, and every state change is an
append-only, hash-chained event on your own disk. If the relay crashes, it never
quietly resends a turn that might already have been delivered. It asks you instead.

**It's small on purpose.** It uses only the Python standard library. There's no
daemon, database, API key or hosted account. The only network listener is the
optional web console, which binds to loopback.

## Try it in two minutes

These demos use pretend sessions, so they're safe to run anywhere and use no model
quota. You need macOS and Python 3.11 or newer.

```sh
git clone https://github.com/saranyan/agent-relay.git && cd agent-relay

scripts/console-demo.sh      # the web console, with a few pretend sessions
scripts/walkthrough.sh       # the full two-terminal flow in a temporary folder
```

The [web console tour](docs/web-console.md) walks through every screen. The
[walkthrough](docs/WALKTHROUGH.md) shows a recorded run with real message and turn
IDs.

## What it can and can't see

The relay coordinates **two sessions per team**, one turn at a time. It sees each
turn from the outside: the turn goes in, the client acknowledges it, and later the
client reports it finished. **What a session does inside a turn is invisible to
the relay.** That includes tool loops, subagents and long-running commands.

- **The turn budget counts hand-offs, not work.** One turn can hide a lot of
  activity. The per-turn timeout is the only hard limit inside a turn.
- **The routes the relay starts are locked down where it can.** A Claude Code
  follower starts with no tools at all, or, under the coding policy, with only
  Read, Edit, Write, Glob and Grep inside a macOS sandbox. It can't start
  subagents or run commands. Codex's multi-agent mode is recorded but not yet
  enforced.
- **Sessions you open in a terminal keep whatever you allowed them** until you
  hand them over.

The full list of guarantees and limits is in the [reference](docs/REFERENCE.md#guarantees-and-limits).

## What you can use it for

**Today, with the prototype:**

- **Plan, build, review.** Codex writes the plan and reviews the result. Claude
  implements. The loop runs until the reviewer accepts or asks you something.
- **A second opinion on every change.** Use one model to critique the other's work
  before you ever see it, so you review a reviewed change.
- **Bounded jobs you don't have to babysit.** "Fix the failing tests in this
  folder, at most 8 turns, 15 minutes per turn." Walk away. A decision notification
  (webhook or SMS, off until you set it up) tells you when it needs you.
- **Check in at a glance.** The web console works down to phone width. It listens
  on loopback only, so reaching it from another device takes your own tunnel. The
  relay deliberately doesn't provide one.
- **An audit trail of agent work.** Every message and decision is a durable,
  hash-chained record that you can replay with `relay thread` or rebuild from disk.

**Ideas the design leaves room for** (see the [roadmap](ROADMAP.md)):

- **One manager, many workers.** A leader hands pieces of a migration to several
  followers, one per module, with review between them.
- **Other agents.** The adapter contract in [docs/PROTOCOL.md](docs/PROTOCOL.md)
  isn't specific to Claude or Codex. Any coding agent that confirms receipt of a
  turn and reports completion could plug in.
- **Saved playbooks.** Reusable goal, scope, check and budget templates, such as
  "review this branch", "write the docs and fact-check them" or "upgrade this
  dependency".
- **Custom manager styles.** Write your own personality next to Reviewer, Planner
  and Pair programmer, as a plain text file.
- **A testbed for agent-to-agent protocols.** The bus is a small, durable,
  inspectable message layer. You can use it to study how agents hand work to each
  other, where loops stall, and when humans need to step in.
- **Evaluation runs.** Pit model pairings against the same task with the same
  budget, and compare the recorded threads.
- **Cost and budget insights.** Track tokens and time per team, not just turns.

## How it works, briefly

```text
   you ──start / pause / answer──┐
                                 ▼
 ┌──────────────┐  handoff  ┌────────────┐  handoff  ┌──────────────┐
 │ leader       │ ◀──────── │ controller │ ────────▶ │ follower     │
 │ (Codex)      │ ────────▶ │ (fg only)  │ ◀──────── │ (Claude)     │
 └──────────────┘  result   └─────┬──────┘  result   └──────────────┘
                                  │
                          durable bus on disk
```

- **Bus:** an on-disk store of messages and events.
- **Adapters:** they talk to each client. Every capability is explicit, and
  adapters never invent acknowledgments.
- **Controller:** it runs only in the foreground and keeps exactly one turn in
  flight. It enforces the budget and the timeout, and stops at every human
  boundary.

Each turn ends with a small `relay` block that tells the controller what to do
next. Commands, settings and exit codes are in the [reference](docs/REFERENCE.md).
The specification is in [docs/PROTOCOL.md](docs/PROTOCOL.md).

## Status

| Part | State |
|---|---|
| Bus, controller, terminal console, web console | Working, covered by the test suite with pretend clients |
| Discovery of running Claude Code and Codex sessions | Working, read-only, on macOS |
| Driving real Claude Code and Codex sessions | Built, **unverified**, off unless `AGENT_RELAY_ALLOW_UNVERIFIED=1` |
| Driving a Claude Code session while it's open in a terminal | Not possible. Open sessions are handed over (closed and resumed) instead. |

What's needed to get from prototype to dependable is in the [roadmap](ROADMAP.md).
The details on each client are in
[integration feasibility](docs/INTEGRATION-FEASIBILITY.md).

## Documentation

| Document | What it covers |
|---|---|
| [Web console tour](docs/web-console.md) | Every screen of `relay serve`, with screenshots |
| [Reference](docs/REFERENCE.md) | Install, consoles, CLI, configuration, guarantees and limits |
| [Walkthrough](docs/WALKTHROUGH.md) | A recorded two-terminal run |
| [Protocol](docs/PROTOCOL.md) | The specification: root layout, envelopes, state machines, adapter contract |
| [Integration feasibility](docs/INTEGRATION-FEASIBILITY.md) | What real Claude Code and Codex surfaces can and can't do |
| [Context reporting](docs/CONTEXT-REPORTING.md) | How session titles and summaries are found |
| [Roadmap](ROADMAP.md) | What's left, and what could come next |
| [Changelog](CHANGELOG.md) | Release notes |

## Contributing

This is a prototype, and ideas, experiments and pull requests are welcome. See
[CONTRIBUTING.md](CONTRIBUTING.md) for setup and for the stricter rules for changes
to the real-client adapters. Report security issues privately through
[SECURITY.md](SECURITY.md). This project follows the
[Contributor Covenant](CODE_OF_CONDUCT.md).

## License

[MIT](LICENSE)
