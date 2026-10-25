# Security policy

## Reporting a vulnerability

Please **don't** open a public issue for security problems. Report them
privately through GitHub's **Report a vulnerability** button on this repository
(Security → Advisories). Include what an attacker can do, the steps to
reproduce it, and the affected version (`relay --version`).

You can expect an acknowledgement within a week. Please give us a reasonable
amount of time to release a fix before you disclose the issue publicly.

## Supported versions

agent-relay is pre-1.0. Only the latest release receives fixes.

## Security model

The relay is designed for **one person, on one machine**. Please read these
boundaries before you report something. A report that shows the relay crossing
one of them is exactly what we want to hear about.

**In scope**

- **Web console:** anything reachable from another machine or another browser
  origin. `relay serve` binds to loopback, requires the one-time token in a
  header, and checks the page's own origin on every action.
- **Message content:** anything that causes the relay to execute message
  content, answer a client's approval prompt, or widen a session's permissions.
- **Delivery:** resending after uncertain delivery, or two writers driving one
  conversation.
- **Data exposure:** leaks of secrets, tokens or transcript content into logs,
  notifications, the events page or published records beyond what's documented.
- **Notifications:** sends to an unverified destination.

**Out of scope, by design**

- **Same-user processes:** they can read and write the relay root. Participant
  labels and the hash chain are not authentication against a malicious
  same-user process. See "Guarantees and limits" in the [reference](docs/REFERENCE.md#guarantees-and-limits).
- **Model output:** what a model writes. Messages that claim approval are
  treated as data and grant nothing, but the relay doesn't judge whether a model's
  work is correct.
- **Secret scanning:** it's a heuristic warning, and missing a secret isn't a
  vulnerability by itself.
