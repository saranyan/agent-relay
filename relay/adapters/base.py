"""Session adapter contract (docs/PROTOCOL.md 9).

Adapters translate real client behaviour. They never invent acknowledgments:
`submit` returns only after the client itself reported acceptance, and raises
`DeliveryUncertain` when it cannot tell whether the client received the input.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..storage import RelayError, Unsupported

CAPABILITIES = ("discover", "attach_live", "launch", "submit", "correlate", "turn_complete",
                "approvals_visible", "busy_state", "reconnect", "dedup", "interrupt", "detach",
                # exclusive_input: the client itself serializes input, so a relay turn can never
                # merge into (steer) a turn someone else started between the idle check and the
                # submit. Live-attach management requires it.
                "exclusive_input",
                # handoff: the relay can continue the same conversation as its only writer
                # (resume by confirmed id) after the native writer exits, and give it back.
                "handoff")


class DeliveryUncertain(RelayError):
    """Submission may or may not have reached the client."""

    code = 4


class NotSubmitted(RelayError):
    """Definitely not delivered (e.g. busy, refused before sending)."""

    code = 5


class WrongSession(RelayError):
    code = 4


class WriterBusy(NotSubmitted):
    """The client definitively refused to resume because another writer still holds the
    conversation. Raised only by an adapter's resume, for that exact refusal; nothing was sent."""


@dataclass
class SessionInfo:
    client_session_id: str
    cwd: str | None
    activity: str  # idle | busy | waiting | unknown
    attachable: bool
    limitations: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class LifecycleEvent:
    type: str  # accepted turn_started delta tool approval_pending human_question busy
    #            turn_completed error disconnected unrelated_turn
    request_id: str | None = None
    client_turn_id: str | None = None
    text: str | None = None
    data: dict[str, Any] = field(default_factory=dict)


class Connection:
    connection_id: str
    client_session_id: str  # the identity the relay requested or targeted
    mode: str  # attach | launch
    # Set only from the client's own report: {"client_session_id": ..., "cwd": ...}.
    # None means the client has not (yet) confirmed who it is.
    confirmed_identity: dict | None = None

    def submit(self, text: str, request_id: str, *, accept_timeout: float = 30.0) -> dict:
        raise Unsupported("submit not supported")

    def poll(self, timeout: float) -> list[LifecycleEvent]:
        raise Unsupported("event observation not supported")

    def lookup(self, request_id: str) -> dict | None:
        """Reconcile after restart: {'accepted': id?, 'completed': text?} or None if unknown."""
        raise Unsupported("turn lookup not supported")

    def status(self) -> str:
        return "unknown"

    def interrupt(self) -> None:
        raise Unsupported("interrupt not supported")

    def detach(self) -> None:
        """Stop observing. Never closes a user-owned session."""

    def close(self) -> None:
        """Close a console-launched session. Attach connections must refuse."""
        raise Unsupported("close not supported for this connection")


class Adapter:
    name = "base"
    verified = False  # True only after a verified client contract (docs/INTEGRATION-FEASIBILITY.md)

    def __init__(self, options: dict | None = None):
        self.options = options or {}

    def capabilities(self) -> dict[str, bool]:
        return {c: False for c in CAPABILITIES}

    def namespace(self) -> str:
        """Canonical identity namespace: which physical client installation or server
        the session IDs belong to. Used to make one physical session one endpoint."""
        return self.name

    def discover(self) -> list[SessionInfo]:
        raise Unsupported(f"{self.name}: discovery not supported")

    def attach(self, client_session_id: str) -> Connection:
        raise Unsupported(f"{self.name}: live attach not supported")

    def launch(self, cwd: str, options: dict | None = None) -> Connection:
        raise Unsupported(f"{self.name}: launch not supported")

    def resume(self, cwd: str, conversation: str, options: dict | None = None, *,
               on_spawn=None) -> Connection:
        """Continue an EXISTING conversation as its only writer (hand-off). Separate from
        launch: never starts a new conversation or a fork in its place. `on_spawn(proc)`
        is called with {pid, started_ms} the moment the writer process exists, before
        any protocol exchange, so the caller can record it durably."""
        raise Unsupported(f"{self.name}: resuming an existing conversation isn't supported")
