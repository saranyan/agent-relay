# Roadmap

agent-relay is at **0.1.0, an experimental developer preview**. The core works
end to end, and the relay launching real Codex and Claude Code sessions and
having them drive each other is verified live. Attaching to sessions you already
have open, and handing them over, aren't verified yet.

This page has two parts:
- **To do:** what's needed to reach a release people can use with real sessions.
- **Future updates:** what we'd like to build after that.

Items aren't promises or dates. If you want to work on one, open an issue first.

## To do

### Make real sessions work (blocks 0.2)

- [x] **Accept the launch adapters with a bounded live run.** Done 2026-10-25: Codex
  0.163.0 led Claude Code 2.1.310 for 2 turns in a scratch folder, with clean
  cleanup. Launch no longer needs `AGENT_RELAY_ALLOW_UNVERIFIED`.
- [ ] **Run a small file task live.** The acceptance run was reply-only. Repeat it
  with a bounded file change, and with pause while a turn is in flight.
- [ ] **Validate Codex attach.** Attach to a Codex session on the shared
  app-server and check four things: the original context is preserved, external
  terminal turns show as busy, the idle-check/send race is handled, and approval
  prompts are observed but never answered.
- [ ] **Prove the hand-off round trip.** Take an interactive session, have AI
  manage it, then return it to its terminal, live. The same conversation must
  resume with its context, with no duplicate messages and no concurrent writers.
- [ ] **Run the review loop live on a real task.** Use a Codex reviewer with a
  Claude implementer under the reviewed coding policy: instruction →
  implementation → relay check → review → fixes → acceptance.
- [ ] **Support Codex 0.162+ permission profiles.** Settle how the relay verifies
  a resumed thread's permission profile on current Codex versions, where the
  legacy sandbox fields are reported differently.
- [ ] **Bound Codex fan-out.** The Claude routes start with no subagent or command
  tools, but Codex's `multiAgentMode` is only recorded. Make the Codex launch path
  turn it off, or refuse to start when it can't be confirmed off.
- [ ] **Reconcile Codex crashes from evidence.** After a crash, use the client's
  own turn history to decide whether a turn was delivered, instead of always
  stopping for a human.

### Open-source readiness

- [ ] **Run CI on GitHub.** The macOS workflow in `.github/workflows/tests.yml`
  hasn't run on GitHub-hosted runners yet. Fix anything that depends on the
  author's machine.
- [ ] **Speed up the test suite.** The full suite takes about 10 minutes. Make
  slow timeouts configurable in tests and split a fast default run from the slow
  subprocess tests.
- [ ] **Replace the copied client code in a fixture.**
  `tests/fixtures/claude-temp-routing.txt` quotes short pieces of the minified
  Claude Code client. Replace them with a paraphrase plus the binary's hash.
- [ ] **Redact the host name on the Events page.** Event records show the
  machine's host name in `actor.host`. Add an option to hide it, so screenshots
  and screen shares don't reveal it.
- [ ] **Publish to PyPI.** Make `pip install agent-relay` work, and add a release
  workflow.
- [ ] **Test on Linux.** Discovery (`codex-local`, `claude-agents`) relies on
  macOS `ps`/`lsof` output. Make it portable, or document clearly that it's
  macOS only.

## Future updates

### Workflows

- **More than one follower.** A manager hands work to several sessions, with
  deterministic review loops between them.
- **Custom manager styles.** Define your own personality beside Reviewer,
  Planner and Pair programmer, as a plain text file in the relay root.
- **Saved playbooks.** Reusable goal, scope, check and budget templates for
  common jobs, such as "fix failing tests" or "review this branch".
- **Budget insights.** Show turns, time and tokens used per team, and where loops
  stall. Today the turn budget counts hand-offs, not the work inside a turn.
- **Evaluation runs.** Run several model pairings on the same task with the same
  budget, and compare the recorded threads.

### Clients

- **Live attach for Claude Code.** Drive an already-open Claude Code terminal
  once Claude Code offers an interface with a submission receipt and a
  correlated completion event. Until then, open terminals are handed over (closed
  and resumed), not attached.
- **More clients.** The adapter contract in [docs/PROTOCOL.md](docs/PROTOCOL.md)
  isn't specific to Claude or Codex. Other coding agents could plug in if they
  expose a turn receipt and a completion event.

### Console

- **More notification channels.** Slack, email and desktop notifications, with
  the same verify-first, kind-only-by-default rules as the existing webhook and
  SMS channels.
- **Searchable history.** Search past teams and conversations, not just live
  sessions.
- **Diff view.** Show the files a turn changed beside the result, as reported by
  the session and checked by the relay.

### Explicitly not planned

- **Remote or multi-user use.** The relay trusts everything running as the same
  OS user. Using it across machines or users would need a separately designed,
  authenticated transport. We won't loosen the loopback-only console to get
  there.
- **Answering approval prompts for you.** Native approval prompts stay native.
