"""Hand-off: the relay becomes the only writer of an existing conversation, then gives it back.


Who may write a conversation is recorded twice, on purpose:

* the authoritative **fence** in the control stream (`Bus.fence`): a conditional
  move from one generation to the next. Dispatch gates live in the same stream and
  name the generation and owner token they were decided under, so a return, block or
  second writer committed first refuses every later dispatch (the same mechanism as
  global pause);
* a per-conversation **ownership stream** (`own-<key>`) with the evidence for each
  move: bound native process (pid + start time), relay writer process, reasons.

The fence is written first. If the process dies before the ownership stream records
the same generation, the conversation reads as `blocked` ("interrupted transition")
until a human resolves it.

The key is the endpoint identity key (adapter + canonical client namespace +
conversation id). Identity registration makes the conversation one endpoint; it
does NOT give the relay exclusive control over an outside native writer. Hand-off
is therefore cooperative: the person closes their terminal, the relay checks that
the bound process is gone and that no other native writer is visible, and keeps
checking while it owns the conversation. A native writer that starts later is
*detected* (and routing stops), not *prevented*.
"""

from __future__ import annotations

import os
import secrets
import threading
import time
from typing import Any, Callable

from .adapters import get_adapter
from .adapters.base import Adapter, WriterBusy
from .bus import Bus
from .protocol import ABANDON_STATEMENT, unknown_turns
from .context import process_exited, process_start_ms, valid_start
from .storage import Conflict, InvalidInput, NotFound, RelayError, Unsupported, actor, pid_alive

WHO = actor("relay", "handoff")
# Discovery adapter -> the adapter that can resume that conversation as the only writer.
WRITER_FOR = {"claude-agents": "claude-stream", "codex-local": "codex-app-server"}
# Codex refuses `thread/resume` (adapter raises WriterBusy) while another writer still holds the thread; seen
# live right after a terminal quit. Retried a bounded number of times, then blocked.
# Serializes releasing a team's claims with handing its conversations over again,
# across processes (a console and `relay pair detach`).
CLAIMS_LOCK = "handoff-claims"
BUSY_RETRIES = 3
BUSY_RETRY_S = 15


def same_process(rec: dict | None) -> bool:
    """Is the process this record describes still running (same pid AND start time)?"""
    if not rec or not rec.get("pid") or not pid_alive(rec["pid"]) or process_exited(rec["pid"]):
        return False
    now = process_start_ms(rec["pid"])
    if not (valid_start(now) and valid_start(rec.get("started_ms"))):
        return True  # alive and can't rule out that it's the same process: assume it is
    return abs(now - rec["started_ms"]) < 2000


class Ownership:
    """Read and move one conversation's ownership (fence first, then evidence)."""

    def __init__(self, bus: Bus):
        self.bus = bus

    @staticmethod
    def stream(key: str) -> str:
        return f"own-{key[:48]}"

    def get(self, key: str) -> dict[str, Any]:
        fence = self.bus.fences().get(key) or {"gen": 0, "state": "native", "token": None}
        events = self.bus.root.read_stream(self.stream(key))
        history = [e for e in events if e.type == "ownership"]
        last = history[-1].data if history else {}
        # Launch evidence for the current episode: a durable intent is written BEFORE the
        # relay launches a writer, and the writer's process AFTER. A restart that finds an
        # intent without a process must not infer that no writer exists.
        spawn = {"intent": False, "relay": None}
        for e in events:
            if e.type == "ownership" and e.data.get("state") in ("relay_starting", "awaiting_exit",
                                                                  "released", "native"):
                spawn = {"intent": False, "relay": None}
            elif e.type == "evidence" and e.data.get("kind") == "spawn_intent":
                spawn["intent"] = True
            elif e.type == "evidence" and e.data.get("kind") == "spawned":
                spawn["relay"] = e.data.get("relay")
            elif e.type == "evidence" and e.data.get("kind") == "identity_confirmed":
                spawn["confirmed"] = True
        out = {"key": key, "gen": fence["gen"], "state": fence["state"], "token": fence.get("token"),
               "detail": last, "spawn": spawn, "history": [dict(e.data, at=e.at) for e in history]}
        if fence["gen"] and last.get("gen") != fence["gen"]:
            # The fence moved but its evidence was never recorded: a crash between the two.
            out.update(state="blocked", interrupted=True, fence_state=fence["state"],
                       reason="An ownership change was interrupted. Check both terminals, then resolve.")
        else:
            out["reason"] = last.get("reason")
        return out

    CARRY = ("adapter", "conversation", "cwd", "show", "relay", "episode", "writer_policy")  # kept unless replaced
    NEW_EPISODE = ("awaiting_exit", "relay_starting", "released", "native")  # no relay writer carried in
    # Each hand-off (begin) is one episode with its own id, carried until the conversation is
    # native/released again. A caller that began an episode can act on exactly that episode,
    # never on a later one another console began after it.
    END_EPISODE = ("released", "native")

    def note(self, key: str, kind: str, **data) -> None:
        """Durable evidence that isn't an ownership move (e.g. launch intent, launched process)."""
        gen = self.get(key)["gen"]
        self.bus.root.append(self.stream(key), lambda events: ("evidence", dict(data, key=key, kind=kind,
                                                                                 gen=gen)), WHO)

    def move(self, key: str, *, expect_gen: int, state: str, token: str | None = None,
             **detail) -> dict:
        prev = self.get(key)["detail"]
        carry = self.CARRY
        if state in self.NEW_EPISODE:
            # A writer's process evidence belongs to its own launch episode. It stays in
            # history, but never carries into a new episode, where an old dead pid could
            # otherwise "prove" that a newly launched writer is gone.
            carry = tuple(k for k in carry if k != "relay")
        if state in self.END_EPISODE:  # the episode's id and its writer policy end with it
            carry = tuple(k for k in carry if k not in ("episode", "writer_policy"))
        detail = dict({k: prev[k] for k in carry if k in prev}, **detail)
        ev = self.bus.fence(key, expect_gen=expect_gen, state=state, token=token, who=WHO)
        gen = ev.data["gen"]
        self.bus.root.append(self.stream(key),
                             lambda events: ("ownership", dict(detail, key=key, gen=gen, state=state,
                                                               token=token)), WHO)
        return self.get(key)


class Handoffs:
    """Hand-off lifecycle for conversations, driven by the console host.

    Relay writers launched here are owned here: the controller borrows their
    connections for routing and never closes them; only `stop_writer` does, and only
    a verified process exit releases ownership.
    """

    def __init__(self, bus: Bus, adapter_options: dict, discover: Callable[[str, str], dict | None]):
        self.bus, self.options = bus, adapter_options
        self.own = Ownership(bus)
        self.discover = discover  # (adapter, conversation id) -> fresh discovery record or None
        self.writers: dict[str, Any] = {}  # key -> relay writer Connection (this console only)
        self.starting: set[str] = set()    # keys this console moved to relay_starting itself
        self.lock = threading.RLock()

    # ------------------------------------------------------------- helpers
    def key(self, adapter: str, conversation: str) -> str:
        return Bus.identity_key(adapter, self.options, conversation)

    def writer_adapter(self, adapter: str):
        """The adapter that resumes a conversation discovered by `adapter`."""
        return get_adapter(WRITER_FOR.get(adapter, adapter), self.options)

    def supported(self, adapter: str, cwd: str | None = None) -> str | None:
        """None if hand-off is available for this conversation, else the reason it isn't.

        Verified writers (the fake) declare `handoff`. An unverified real writer is
        available ONLY inside one explicitly opted-in folder for a bounded live run:
        AGENT_RELAY_LIVE_HANDOFF_SCOPE must name exactly this conversation's folder.
        The global AGENT_RELAY_ALLOW_UNVERIFIED does not unlock hand-off."""
        try:
            a = self.writer_adapter(adapter)
        except RelayError as e:
            return str(e)
        if type(a).resume is Adapter.resume:
            return "Hand-off isn't available for this kind of session yet."
        if a.verified:
            return None if a.capabilities().get("handoff") else "Hand-off isn't available for this kind of session yet."
        scope = os.environ.get("AGENT_RELAY_LIVE_HANDOFF_SCOPE")
        if scope and cwd and os.path.realpath(scope) == os.path.realpath(cwd):
            return None
        return "Hand-off for this client hasn't been verified live, so it's locked."

    @staticmethod
    def natives(found: dict | None) -> list[dict] | None:
        """Live native writers of a conversation, or None if that can't be determined."""
        if found is None:
            return None
        writers = (found.get("extra") or {}).get("writers")
        if writers is None:
            return None
        return [w for w in writers if w.get("writer") == "native"]

    @staticmethod
    def relay_writers(found: dict | None) -> list[dict] | None:
        """Live relay-owned writers of a conversation, or None if that can't be determined."""
        if found is None or (found.get("extra") or {}).get("relay_visible") is False:
            return None  # this discovery can't see relay writers: never proof of absence
        writers = (found.get("extra") or {}).get("writers")
        if writers is None:
            return None
        return [w for w in writers if w.get("writer") == "relay"]

    def relay_writer_status(self, key: str, *, still_running: Callable[[dict], bool] = same_process,
                            held: bool = True) -> str:
        """'running' | 'gone' | 'unknown' for the relay's own writer of a conversation.

        Recorded processes are checked by pid + start time. Discovery is consulted too,
        so a writer launched before its process could be recorded (crash) is still found.
        With a launch intent but no recorded process and no discovery, it's unknown."""
        if held and key in self.writers:
            return "running"
        st = self.own.get(key)
        spawn = st["spawn"]  # evidence of the CURRENT launch episode only
        recorded = [r for r in (st["detail"].get("relay"), spawn.get("relay")) if r]
        if any(still_running(r) for r in recorded):
            return "running"
        d = st["detail"]
        found = self.relay_writers(self.discover(d.get("adapter"), d.get("conversation"))) \
            if d.get("adapter") and d.get("conversation") else None
        if found:
            return "running"
        if found is None and spawn.get("intent") and not spawn.get("relay"):
            # This episode launched (or may have) a writer whose process was never recorded,
            # and nothing can see writers: unknown, whatever older episodes recorded.
            return "unknown"
        return "gone"

    def _fault(self, point: str) -> None:
        """Crash-injection hook for tests (no-op in normal use)."""

    def state(self, adapter: str, conversation: str) -> dict:
        with self.lock:  # never observe a transition half-done (e.g. mid stop_writer)
            return self._state(self.key(adapter, conversation))

    def block_if_orphaned(self, key: str, reason: str) -> dict:
        """Block a conversation the relay owns on paper but no writer here backs (atomic)."""
        with self.lock:
            st = self._state(key)
            return self.block(key, reason) if st.get("orphaned") else st

    def _state(self, key: str) -> dict:
        st = self.own.get(key)
        held = key in self.writers or (st["state"] == "relay_starting" and key in self.starting)
        if st["state"] in ("relay_starting", "relay_owned", "returning") and not held:
            # Ownership says the relay writes, but this console holds no writer for it
            # (console restart, or the writer was started by another console).
            st = dict(st, orphaned=True,
                      reason="The relay's writer for this conversation isn't running in this console.")
        return st

    # ---------------------------------------------------------- transitions
    def begin(self, adapter: str, conversation: str, cwd: str, *, writer_policy: dict | None = None) -> dict:
        """Native -> awaiting_exit, binding the native process(es) seen right now. A
        `writer_policy` is fixed for this episode: the writer is
        started under exactly it, or not at all."""
        why = self.supported(adapter, cwd)
        if why:
            raise Unsupported(why)
        key = self.key(adapter, conversation)
        with self.lock:
            st = self.own.get(key)
            if st["state"] not in ("native", "released"):
                raise Conflict(f"this conversation is {st['state'].replace('_', ' ')}")
            found = self.discover(adapter, conversation)
            natives = self.natives(found)
            if natives is None:
                raise Conflict("can't tell which process is writing this conversation right now")
            extra = (found or {}).get("extra") or {}
            show = {k: extra.get(k) for k in ("client", "name", "title", "summary") if extra.get(k)}
            return self.own.move(key, expect_gen=st["gen"], state="awaiting_exit",
                                 adapter=adapter, conversation=conversation,
                                 cwd=os.path.realpath(cwd), bound=natives, show=show,
                                 episode=secrets.token_hex(8),
                                 **({"writer_policy": writer_policy} if writer_policy else {}),
                                 reason="waiting for the conversation to be closed in its terminal")

    @staticmethod
    def _episode_check(st: dict, episode: str | None) -> None:
        if episode is not None and (st["detail"] or {}).get("episode") != episode:
            raise Conflict("a later hand-off owns this conversation now")

    def cancel(self, adapter: str, conversation: str, *, episode: str | None = None) -> dict:
        """awaiting_exit -> native. With `episode`, only that hand-off: the check and the
        move are one conditional fence move, so a later episode begun by another console in
        between is never cancelled."""
        key = self.key(adapter, conversation)
        with self.lock:
            st = self.own.get(key)
            if st["state"] != "awaiting_exit":
                raise Conflict("nothing to cancel")
            self._episode_check(st, episode)
            return self.own.move(key, expect_gen=st["gen"], state="native", reason="hand-off cancelled")

    def exit_status(self, adapter: str, conversation: str) -> str:
        """'gone' | 'running' | 'unknown' for the native side of a pending hand-off."""
        st = self.own.get(self.key(adapter, conversation))
        bound = st["detail"].get("bound") or []
        if any(same_process(b) for b in bound):
            return "running"
        natives = self.natives(self.discover(adapter, conversation))
        if natives is None:
            return "unknown"  # discovery failed or can't see writers: never assume absence
        return "running" if natives else "gone"

    def advance(self, adapter: str, conversation: str) -> dict:
        """awaiting_exit -> relay_starting once the native writer is verifiably gone."""
        key = self.key(adapter, conversation)
        with self.lock:
            st = self.own.get(key)
            if st["state"] != "awaiting_exit":
                return st
            retry_at = st["detail"].get("retry_after")
            if retry_at and time.time() < retry_at:
                return dict(st, exit="settling")  # the client asked us to wait (see start_writer)
            status = self.exit_status(adapter, conversation)
            if status != "gone":
                return dict(st, exit=status)
            self.starting.add(key)
            return self.own.move(key, expect_gen=st["gen"], state="relay_starting",
                                 busy_retries=st["detail"].get("busy_retries") or 0,
                                 reason="native writer gone; starting the relay's writer")

    def start_writer(self, adapter: str, conversation: str) -> dict:
        """relay_starting -> relay_owned (same conversation confirmed) or blocked."""
        key = self.key(adapter, conversation)
        try:
            return self._start_writer(key, adapter, conversation)
        finally:
            self.starting.discard(key)

    def _start_writer(self, key: str, adapter: str, conversation: str) -> dict:
        with self.lock:
            st = self.own.get(key)
            if st["state"] != "relay_starting":
                raise Conflict(f"this conversation is {st['state'].replace('_', ' ')}")
            cwd = st["detail"].get("cwd")
            # The opt-in is checked HERE, at the launch boundary, before any intent or
            # process: a pending hand-off reloaded by a console that isn't opted in (or is
            # opted in for another folder) is no authorization to launch.
            # Return/stop/resolve never need the opt-in.
            why = self.supported(adapter, cwd)
            if why:
                return self.block(key, f"Not started: {why} Resolve or cancel it; nothing was launched.")
            # Recheck right before connecting: someone may have reopened it meanwhile.
            if self.exit_status(adapter, conversation) != "gone":
                return self.block(key, "The conversation was reopened in a terminal before the relay "
                                       "started. Close it there, then try again.")
            # Durable intent first: from here on a writer may exist even if we crash
            # before learning its process, so recovery must look for it, not assume.
            self.own.note(key, "spawn_intent")
            spawned: dict = {}

            def on_spawn(proc: dict) -> None:
                self._fault("launched")  # (tests) the writer exists, not yet recorded
                self.own.note(key, "spawned", relay=proc)
                spawned.update(proc)
                self._fault("recorded")
            if st["detail"].get("writer_policy") and \
                    not getattr(self.writer_adapter(adapter), "supports_writer_policy", False):
                return self.block(key, "Not started: this client has no route for the team's coding "
                                       "policy, and the relay never starts it without one.")
            try:
                conn = self.writer_adapter(adapter).resume(
                    cwd, conversation, dict(st["detail"].get("show") or {},
                                            outside_writers=lambda: self.natives(self.discover(adapter, conversation)),
                                            **({"writer_policy": st["detail"]["writer_policy"]}
                                               if st["detail"].get("writer_policy") else {})),
                    on_spawn=on_spawn)
            except RelayError as e:
                # The adapter stops any child it started; its exit is verified like any other.
                retries = int(st["detail"].get("busy_retries") or 0)
                # Typed, not text: only the adapter's definitive resume refusal is retryable.
                if isinstance(e, WriterBusy) and retries < BUSY_RETRIES and spawned \
                        and not same_process(spawned):
                    # The client itself says another writer still holds the conversation (Codex
                    # releases its lock some seconds after a terminal quits). Nothing was sent
                    # and the refused writer is verifiably gone: wait and try again, bounded.
                    self.starting.discard(key)
                    return self.own.move(key, expect_gen=st["gen"], state="awaiting_exit",
                                         busy_retries=retries + 1,
                                         retry_after=time.time() + BUSY_RETRY_S,
                                         reason=f"The client still has the conversation open ({e}); "
                                                f"trying again in {BUSY_RETRY_S} s "
                                                f"(attempt {retries + 1} of {BUSY_RETRIES})")
                return self.block(key, f"The relay's writer didn't start: {e}",
                                  **({"relay": dict(spawned)} if spawned else {}),
                                  # the adapter's redacted shape of a refused answer
                                  **({"diagnostic": e.details["diagnostic"]}
                                     if isinstance(e.details.get("diagnostic"), dict) else {}))
            proc = dict(spawned) or {"pid": getattr(conn, "pid", None),
                                     "started_ms": process_start_ms(conn.pid) if getattr(conn, "pid", None) else None}
            if not spawned:
                self.own.note(key, "spawned", relay=proc)
            ident = getattr(conn, "confirmed_identity", None) or {}
            pending = not ident and getattr(conn, "identity_pending", False)
            if not pending and (ident.get("client_session_id") != conversation or
                                os.path.realpath(ident.get("cwd") or "") != cwd):
                self._stop(conn)
                return self.block(key, "The client opened a different conversation or folder than the "
                                       "one handed off, so the relay stopped it.", relay=proc)
            if self.exit_status(adapter, conversation) != "gone":  # a native writer appeared during launch
                self._stop(conn)
                return self.block(key, "A terminal reopened this conversation while the relay was "
                                       "starting. The relay stopped its writer.", relay=proc)
            token = "own_" + secrets.token_hex(8)
            self.writers[key] = conn
            self._fault("held")
            return self.own.move(key, expect_gen=st["gen"], state="relay_owned", token=token,
                                 relay=proc, provisional=bool(pending),
                                 reason="the relay is the only writer" + (
                                     "; its first turn confirms which conversation it is" if pending else ""))

    def check(self, key: str) -> str | None:
        """While relay-owned: None if still the only writer, else why not (and block)."""
        with self.lock:
            st = self.own.get(key)
            if st["state"] != "relay_owned":
                return f"this conversation is {st['state'].replace('_', ' ')}"
            conn = self.writers.get(key)
            if conn is None:
                return self.block(key, "The relay's writer isn't running in this console.")["reason"]
            d = st["detail"]
            if getattr(conn, "policy_error", None):  # the adapter already stopped it
                self.writers.pop(key, None)
                self._stop(conn)
                return self.block(key, "The client broke the team's coding policy "
                                       f"({conn.policy_error}). The relay stopped it and routed nothing; "
                                       "edits it may have made stayed inside the sandbox but are untrusted.")["reason"]
            if getattr(conn, "identity_error", None):
                # The client reported another conversation/folder: its output was withheld;
                # stop it (exit verified later by status checks) and keep ownership blocked.
                self.writers.pop(key, None)
                self._stop(conn)
                return self.block(key, "The client reported a different conversation or folder "
                                       f"({conn.identity_error}). Its output was withheld and the "
                                       "relay stopped it.")["reason"]
            if d.get("provisional") and not st["spawn"].get("confirmed"):
                ident = getattr(conn, "confirmed_identity", None) or {}
                if ident.get("client_session_id") == d.get("conversation") and \
                        os.path.realpath(ident.get("cwd") or "") == d.get("cwd"):
                    self.own.note(key, "identity_confirmed", client_session_id=ident["client_session_id"],
                                  cwd=ident.get("cwd"))
            if not same_process(d.get("relay")):
                return self.block(key, "The relay's writer stopped unexpectedly.")["reason"]
            natives = self.natives(self.discover(d.get("adapter"), d.get("conversation")))
            mine = (d.get("relay") or {}).get("pid")
            natives = None if natives is None else [w for w in natives if w.get("pid") != mine]
            if natives is None:
                return "can't confirm the relay is still the only writer"  # don't block, don't send
            if natives:
                return self.block(key, "This conversation was also opened in a terminal. Two writers "
                                       "can't share a conversation; close one.")["reason"]
            return None

    def fence(self, key: str) -> dict | None:
        st = self.own.get(key)
        if st["state"] != "relay_owned":
            return None
        return {"gen": st["gen"], "token": st["token"]}

    def block(self, key: str, reason: str, *, episode: str | None = None, **detail) -> dict:
        with self.lock:
            st = self.own.get(key)
            self._episode_check(st, episode)
            if st["state"] == "blocked" and st.get("fence_state", "blocked") == "blocked":
                return st  # (an interrupted transition is recorded as a real block below)
            if st.get("fence_state", st["state"]) in ("native", "released"):
                return st  # nothing of the relay's left to block (e.g. a return just finished)
            try:
                return self.own.move(key, expect_gen=st["gen"], state="blocked",
                                     **detail, reason=reason)
            except Conflict:
                # Another console (or thread) moved it first, e.g. both noticed the same
                # orphan. If it's now blocked or no longer the relay's, that's the outcome we
                # wanted; anything else is a real conflict and is raised.
                now = self.own.get(key)
                if now["state"] in ("blocked", "native", "released"):
                    return now
                raise

    def begin_return(self, key: str) -> dict:
        """relay_owned/blocked -> returning. Fenced first: no dispatch passes after this."""
        with self.lock:
            st = self.own.get(key)
            if st["state"] == "returning":
                return st
            if st["state"] not in ("relay_owned", "blocked") or st.get("interrupted"):
                raise Conflict(f"this conversation is {st['state'].replace('_', ' ')}")
            return self.own.move(key, expect_gen=st["gen"], state="returning",
                                 reason="returning to the terminal")

    def stop_writer(self, key: str, *, still_running: Callable[[dict], bool] = same_process,
                    hold: str | None = None) -> dict:
        """returning -> released only after the relay writer's exit is verified.

        If the exit can't be verified, ownership stays with the relay (blocked) and no
        resume instruction is given: telling someone to reopen it could make two writers.
        `hold`: the exit is verified but something else is unresolved (an unknown turn
        outcome): stay blocked with that reason, never released."""
        with self.lock:
            st = self.own.get(key)
            if st["state"] != "returning":
                raise Conflict(f"this conversation is {st['state'].replace('_', ' ')}")
            conn = self.writers.pop(key, None)
            if conn is not None:
                self._stop(conn)
            status = self.relay_writer_status(key, still_running=still_running, held=False)
            if status != "gone":
                if conn is not None:
                    self.writers[key] = conn
                return self.block(key, "The relay asked its writer to stop but can't confirm it "
                                       "exited. Don't reopen the conversation yet.",
                                  shutdown="unknown")
            if hold:
                return self.block(key, hold, shutdown="verified")
            return self.own.move(key, expect_gen=st["gen"], state="released",
                                 reason="back in your terminal")

    def resolve(self, key: str) -> dict:
        """A human clears `blocked`: allowed only when no relay writer is verifiably running."""
        with self.lock:
            st = self.own.get(key)
            if st["state"] != "blocked":
                raise Conflict("nothing to resolve")
            if st.get("interrupted"):
                st = self.block(key, st["reason"])
            if key in self.writers:
                return self.begin_return(key)  # our writer: return it properly (exit verified)
            status = self.relay_writer_status(key, held=False)
            if status == "running":
                return dict(st, reason="A relay writer for this conversation is still running (from an "
                                       "earlier console). It stops when that console exits; then resolve again.")
            if status == "unknown":
                return dict(st, reason="The relay may have started a writer before it was interrupted and "
                                       "can't confirm it's gone. Don't reopen the conversation yet.")
            return self.own.move(key, expect_gen=st["gen"], state="released",
                                 reason="resolved by you; no relay writer is running")

    @staticmethod
    def _stop(conn) -> None:
        try:
            conn.close()
        except Exception:  # the exit is verified separately; never trust close() alone
            pass


def resume_command(client: str, conversation: str) -> str:
    """What the person types to continue natively (shown only after a verified release)."""
    if (client or "").lower() == "codex":
        return f"codex resume {conversation}"
    return f"claude --resume {conversation}"


__all__ = ["Handoffs", "Ownership", "resume_command", "same_process", "NotFound", "InvalidInput"]


def unknown_hold(st: dict) -> str | None:
    """Why a team's conversations can't be given back yet: a turn whose outcome is unknown
    and that no human has disposed of. None when nothing is unknown."""
    unknown = unknown_turns(st)
    if not unknown:
        return None
    nums = [str(i + 1) for i, t in enumerate(st["turns"]) if t in unknown]
    return (f"Turn {', '.join(nums)} may have run; nobody knows its outcome. Before the conversations "
            f"are given back, record that explicitly: `relay pair abandon-unknown {st['pair_id']} --confirm` "
            f"(\"{ABANDON_STATEMENT}\"). Nothing is resent.")


def ownership_keys(bus: Bus) -> tuple[dict[tuple[str, str], str], list[str]]:
    """From durable records alone (no adapter options or environment): (adapter,
    conversation) -> ownership key, plus keys whose evidence names no conversation
    (an interrupted first move) and so can't be attributed: callers fail closed on those."""
    own = Ownership(bus)
    found, unattributed = {}, []
    for key in bus.fences():
        o = own.get(key)
        det = o["detail"] or {}
        if det.get("adapter") and det.get("conversation"):
            found[(det["adapter"], det["conversation"])] = key
        elif o["state"] != "native":
            unattributed.append(key)
    return found, unattributed


def team_holds(bus: Bus, pid: str, *, ending: bool = False) -> list[str]:
    """Reasons a hand-off team's claims must stay held (empty: safe to release). The same
    retention rule as the console: any conversation not back with its terminal (native or
    released with its writer's exit verified), or an unacknowledged unknown turn outcome.
    `ending`: judge as if the team had already ended (a turn still in flight then counts)."""
    st = bus.pair_state(pid)
    if ending and not st.get("terminal"):
        st = dict(st, terminal="stopped")
    d = st["definition"]
    own = Ownership(bus)
    keys, unattributed = ownership_keys(bus)
    out, handoff = [], False
    for role in ("leader", "follower"):
        s = bus.get_session(d[role]["endpoint"])
        if not (s.get("options") or {}).get("handoff"):
            continue
        handoff = True
        key = keys.get((s["adapter"], s["client_session_id"]))
        if key is None:
            if unattributed:
                out.append(f"an ownership record can't be attributed to a conversation "
                           f"({len(unattributed)} interrupted); resolve it in the console first")
            continue  # never handed over: native
        o = own.get(key)
        if o["state"] not in ("native", "released"):
            name = (s.get("info") or {}).get("name") or s["id"]
            out.append(f"{name} is {o['state'].replace('_', ' ')}: {o.get('reason') or ''}".rstrip(": "))
    hold = unknown_hold(st) if handoff else None
    if hold:
        out.append(hold)
    return out
