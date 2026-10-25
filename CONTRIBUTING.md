# Contributing to agent-relay

Thanks for helping. This project drives other people's AI coding sessions, so it
values correctness and honesty over features. Please read this page before
opening a pull request.

## Development setup

```sh
git clone https://github.com/saranyan/agent-relay.git && cd agent-relay
python3 --version                                    # 3.11 or newer
python3 -m unittest discover -s tests -p 'test_*.py' # full suite
```

There are no dependencies to install. The relay uses only the Python standard
library, and that's deliberate. Please don't add runtime dependencies. If a change
seems to need one, open an issue first.

Useful commands:

| Command | What it does |
|---|---|
| `bin/relay --help` | Run the CLI from the checkout |
| `scripts/walkthrough.sh` | Two-terminal demo with fake clients |
| `scripts/console-demo.sh` | Web console with pretend sessions |
| `node scripts/browser-flow.mjs <link> <dir>` | End-to-end browser check of the console (Node 22 and Chrome) |
| `node scripts/screenshots.mjs <link> docs/images` | Regenerate the documentation screenshots |
| `python3 -m unittest discover -s tests -p 'test_handoff*.py'` | Run one area of the suite |

The full suite takes roughly 10 minutes on a laptop, because many tests drive real
subprocesses and timeouts. While you iterate, run the files for the area you're
changing, and run the full suite before you push.

## What a good pull request looks like

- **Tests come with the change.** Every behaviour change needs a test that fails
  without it. Tests use fake clients and temporary relay roots; see
  `tests/util.py`. Never point a test at your real `~/.claude`, `~/.codex` or
  relay root.
- **The protocol stays in sync.** If you change envelopes, events or state
  machines, update [docs/PROTOCOL.md](docs/PROTOCOL.md) in the same pull request.
- **Docs match behaviour.** User-visible changes update the README or the
  relevant page in `docs/`, including screenshots when the console changes.
- **The style matches.** Plain-language UI text, comments only where the code
  can't speak for itself, and fail-closed defaults.

## Changes to real-client adapters

The adapters in `relay/adapters/claude.py` and `relay/adapters/codex.py` control
live sessions on someone's machine. For changes to them:

1. **Say how it was verified.** Name the client version and say what you ran.
   Fake-client tests are required, but they don't prove real-client behaviour.
2. **Don't mark an adapter verified** (`verified = True`) without a recorded,
   bounded live run that a maintainer can reproduce.
3. **Never answer approval prompts or widen permissions** on the user's behalf,
   and never resend after uncertain delivery.
4. **Live probes run only in scratch workspaces.** The bounded live probes in
   `scripts/` (`handoff_probe.py`, `claude_state_probe.py`,
   `codex_reviewer_probe.py`) must never be pointed at your real working
   sessions.

## Reporting bugs

Open an issue with the relay version (`relay --version`), your OS, your Python
version, and the Claude Code or Codex version if it's involved. Include the steps
to reproduce it. `relay status --json` and the relevant `relay thread <id>`
output are very helpful. **Remove secrets, tokens and private transcript text
before you paste anything.**

Security problems go through [SECURITY.md](SECURITY.md), not public issues.

## Code of conduct

Everyone taking part in this project is expected to follow the
[Code of Conduct](CODE_OF_CONDUCT.md).
