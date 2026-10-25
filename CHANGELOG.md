# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Changed

- **Launch is verified live.** Codex 0.163.0 (`codex-app-server`) led Claude Code
  2.1.310 (`claude-stream`) through a bounded 2-turn run. Sessions the relay starts
  no longer need `AGENT_RELAY_ALLOW_UNVERIFIED=1`. Attach and hand-off are still
  gated. Adapters now declare `verified_modes`.

### Fixed

- **A killed, unreaped writer counted as running.** It now counts as gone.
- **Flaky tests on GitHub's macOS runners.**

## [0.1.0] - 2026-10-24

The first public release: an experimental developer preview.

### Added

- **Durable bus:** immutable messages and append-only, hash-chained event
  streams, published atomically and rebuildable from disk.
- **Foreground pair controller:** routes turns between a leader and a follower,
  with one turn in flight, a turn budget, a per-turn timeout, pause/resume, and a
  stop for human review at every boundary.
- **Interfaces:** an interactive terminal console (`relay console`) and a
  scriptable CLI with JSON output and documented exit codes.
- **Web console (`relay serve`):** loopback only, with a one-time token. It
  covers the session list, Manage with AI with Reviewer, Planner and Pair
  programmer styles, plain-language summaries, Intervene, hand-over of open
  terminals, the review loop, decision notifications, and the raw event stream.
- **Session discovery and context:** read-only discovery of running Claude Code
  (`claude-agents`) and Codex (`codex-local`) sessions, with descriptive titles
  and summaries.
- **Real-client adapters:** for Claude Code (`claude-stream`) and Codex
  (`codex-app-server`). These are **unverified** and gated behind
  `AGENT_RELAY_ALLOW_UNVERIFIED=1`.
- **Fake clients, demos and tests:** fake clients, `scripts/walkthrough.sh`,
  `scripts/console-demo.sh`, and a test suite of 520 tests.

[Unreleased]: https://github.com/saranyan/agent-relay/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/saranyan/agent-relay/releases/tag/v0.1.0
