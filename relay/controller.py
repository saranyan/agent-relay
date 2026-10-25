"""Foreground pair controller: bounded leader/follower routing (docs/PROTOCOL.md 7-10).

The controller only runs while a human keeps it in the foreground. It submits
turns to user-selected sessions through adapters, records every step in the
pair stream before acting on it, and stops at every boundary that needs a human.
It never executes message content, answers approval prompts or creates agents.
"""

from __future__ import annotations

import os
import socket
import time
from typing import Callable

from . import protocol as P
from . import verify as V
from .adapters import get_adapter
from .adapters.base import Connection, DeliveryUncertain, NotSubmitted, WrongSession
from .bus import Bus
from .prompts import handoff_prompt
from .storage import (Conflict, RelayError, StreamUntrusted, Unsupported, actor, new_id,
                      pid_alive, sha256)

STOP_STATUSES = frozenset({"finished", "stopped", "waiting_human", "delivery_uncertain", "failed",
                           "disconnected", "untrusted"})
EVIDENCE_LIMIT = 200 * 1024


class Controller:
    def __init__(self, bus: Bus, pair_id: str, *, log: Callable[[str], None] | None = None,
                 poll_interval: float = 0.2, borrowed: dict | None = None,
                 fences: Callable[[], dict] | None = None,
                 before_submit: Callable[[str], str | None] | None = None,
                 check_runner=None):
        self.bus, self.pair_id = bus, pair_id
        self.check_runner = check_runner  # review loop; default: the isolated runner
        # Hand-off writers belong to the hand-off, not to this controller: it routes
        # through them but never launches, detaches or closes them.
        self.borrowed: dict[str, Connection] = dict(borrowed or {})
        # Ownership fences ({key: {gen, token}}) every dispatch must still hold, checked
        # atomically in the control-stream gate; and a last check right before submit.
        self.fences = fences
        self.before_submit = before_submit
        self.d = bus.get_pair_def(pair_id)
        self.ctl = new_id("ctl")
        self.log = log or (lambda s: None)
        self.poll_interval = poll_interval
        self.connections: dict[str, Connection] = {}
        self.caps: dict[str, dict] = {}
        self.submitted_here: set[str] = set()
        self.captured: set[str] = set()
        self.owned = False
        self._ambiguity_reported: set[str] = set()
        self._last_wait = None
        self._confirmed: set[str] = set()

    # ---------------------------------------------------------- ownership
    def who(self) -> dict:
        return actor("controller", self.ctl)

    def ev(self, etype: str, **data):
        data.setdefault("ctl", self.ctl)
        return self.bus.pair_event(self.pair_id, etype, data, self.who())

    def acquire(self, takeover: bool = False, force: bool = False) -> None:
        st = self.bus.pair_state(self.pair_id)
        owner = st.get("owner")
        data = {"ctl": self.ctl, "pid": os.getpid(), "host": socket.gethostname()}
        if owner:
            same_host = owner.get("host") == socket.gethostname()
            dead = same_host and owner.get("pid") and not pid_alive(owner["pid"])
            if not takeover:
                hint = " (its process is gone; use --takeover)" if dead else ""
                raise Conflict(f"pair owned by controller {owner['ctl']}{hint}")
            if not dead and not force:
                raise Conflict(f"owner {owner['ctl']} may still be running (pid {owner.get('pid')} on "
                               f"{owner.get('host')}); refuse takeover without --force")
            data["takeover_from"] = owner["ctl"]
        self.bus.pair_event(self.pair_id, "controller_acquired", data, actor("human-directed", self.ctl))
        self.owned = True

    def release(self) -> None:
        if self.owned:
            try:
                self.bus.pair_event(self.pair_id, "controller_released", {"ctl": self.ctl}, self.who())
            except RelayError:
                pass
            self.owned = False

    # -------------------------------------------------------- connections
    def endpoint(self, role: str) -> str:
        return self.d[role]["endpoint"]

    def connect_all(self) -> None:
        for role in ("leader", "follower"):
            eid = self.endpoint(role)
            if eid not in self.connections:
                self.connect(eid)

    def connect(self, eid: str) -> Connection:
        if eid in self.borrowed:
            return self._use_borrowed(eid)
        s = self.bus.get_session(eid)
        state = s["state"]
        if state["state"] == "closed":
            raise WrongSession(f"endpoint {eid} is closed ({state['closed']})")
        if state["pair"] != self.pair_id:
            raise Conflict(f"endpoint {eid} is not reserved for this pair")
        adapter = get_adapter(s["adapter"], s["options"])
        if not (adapter.verified or s["mode"] in adapter.verified_modes) \
                and os.environ.get("AGENT_RELAY_ALLOW_UNVERIFIED") != "1":
            raise Unsupported(f"adapter {adapter.name} ({s['mode']}) is not verified against a live client; live "
                              "use needs explicit authorization and AGENT_RELAY_ALLOW_UNVERIFIED=1")
        self.caps[eid] = adapter.capabilities()
        if s["mode"] == "attach":
            conn = adapter.attach(s["client_session_id"])
            if conn.client_session_id != s["client_session_id"]:
                raise WrongSession(f"adapter attached to {conn.client_session_id}, expected "
                                   f"{s['client_session_id']}")
        else:
            prior = self.bus.root.read_stream(f"session-{eid}")
            if any(e.type == "connected" for e in prior):
                raise WrongSession(
                    f"endpoint {eid} was launched by an earlier controller; relaunching would create "
                    "a different session. Register a new launch endpoint and pair instead.")
            opts = {}
            if not s["client_session_id"].startswith("pending-"):
                opts["session_id"] = s["client_session_id"]
            conn = adapter.launch(s["cwd"] or os.getcwd(), opts)
        self.bus.session_event(eid, "connected", {"connection": conn.connection_id,
                                                  "requested_client_session_id": conn.client_session_id,
                                                  "ctl": self.ctl}, self.who())
        self.connections[eid] = conn
        try:
            self.record_identity(eid, conn)
        except (WrongSession, Conflict):
            conn.detach() if conn.mode == "attach" else conn.close()
            del self.connections[eid]
            raise
        self.log(f"connected {eid} ({s['mode']}, {s['adapter']}, session {conn.client_session_id})")
        return conn

    def _use_borrowed(self, eid: str) -> Connection:
        s = self.bus.get_session(eid)
        if s["state"]["pair"] != self.pair_id:
            raise Conflict(f"endpoint {eid} is not reserved for this pair")
        conn = self.borrowed[eid]
        self.caps[eid] = get_adapter(s["adapter"], s["options"]).capabilities()
        self.bus.session_event(eid, "connected", {"connection": conn.connection_id,
                                                  "requested_client_session_id": conn.client_session_id,
                                                  "ctl": self.ctl, "handoff": True}, self.who())
        self.connections[eid] = conn
        self.record_identity(eid, conn)
        self.log(f"connected {eid} (hand-off writer for {conn.client_session_id})")
        return conn

    def record_identity(self, eid: str, conn: Connection) -> None:
        """Persist the identity the client itself reported, once it has reported it.

        A launch may only learn its identity after the first prompt (Claude emits
        `system/init` then); until then the endpoint is shown as unconfirmed.
        """
        ident = getattr(conn, "confirmed_identity", None)
        if not ident or eid in self._confirmed:
            return
        s = self.bus.get_session(eid)
        cid = ident["client_session_id"]
        expected = s["client_session_id"]
        if not expected.startswith("pending-") and cid != expected:
            raise WrongSession(f"client reported session {cid}, expected {expected}")
        if eid in self.borrowed and os.path.realpath(ident.get("cwd") or "") != os.path.realpath(s.get("cwd") or "/"):
            raise WrongSession(f"client reported folder {ident.get('cwd')!r}, expected {s.get('cwd')!r}")
        if cid != expected:  # the client assigned the identity; bind it to this endpoint only
            winner = self.bus.claim_identity(s["adapter"], s["options"], cid, eid)
            if winner != eid:
                raise WrongSession(f"client session {cid} is already registered as {winner}")
        self.bus.session_event(eid, "identity_confirmed",
                               {"client_session_id": cid, "cwd": ident.get("cwd")}, self.who())
        self._confirmed.add(eid)
        self.log(f"{eid}: client confirmed session {cid}" +
                 (f" (cwd {ident['cwd']})" if ident.get("cwd") else ""))

    def shutdown(self) -> None:
        """Detach attached sessions (never closed); close console-launched children."""
        for eid, conn in list(self.connections.items()):
            try:
                if eid in self.borrowed:  # the hand-off keeps its writer; only routing stops
                    self.bus.session_event(eid, "disconnected", {"connection": conn.connection_id,
                                                                 "reason": "controller exited; the "
                                                                 "hand-off keeps its writer"},
                                           self.who())
                elif conn.mode == "launch":
                    conn.close()
                    self.bus.session_event(eid, "closed", {"reason": "controller exited; launched "
                                                           "child closed"}, self.who())
                else:
                    conn.detach()
                    self.bus.session_event(eid, "disconnected", {"connection": conn.connection_id,
                                                                 "reason": "controller exited"},
                                           self.who())
            except RelayError as e:
                self.log(f"shutdown {eid}: {e}")
            except Exception as e:  # pragma: no cover - best effort cleanup
                self.log(f"shutdown {eid}: {e!r}")
        self.connections.clear()
        self.release()

    # --------------------------------------------------------------- loop
    def run(self, *, exit_on=STOP_STATUSES, max_seconds: float | None = None,
            on_tick: Callable[[dict], bool] | None = None) -> str:
        deadline = None if max_seconds is None else time.monotonic() + max_seconds
        idle_sleep = 0.05
        while True:
            status, busy = self.step()
            if on_tick is not None and on_tick(self.bus.pair_state(self.pair_id)) is False:
                return status
            if status in exit_on and not busy:
                return status
            if deadline is not None and time.monotonic() > deadline:
                return status
            if busy:
                idle_sleep = 0.05
            else:
                time.sleep(idle_sleep)
                idle_sleep = min(idle_sleep * 2, 1.0)

    def step(self) -> tuple[str, bool]:
        """One controller iteration. Returns (status, work_in_progress)."""
        try:
            st = self.bus.pair_state(self.pair_id)
        except StreamUntrusted as e:
            self.log(f"pair history untrusted: {e}")
            return "untrusted", False
        if not st.get("owner") or st["owner"]["ctl"] != self.ctl:
            raise Conflict("controller ownership lost")
        if st["terminal"]:
            return st["status"], False
        for eid, conn in self.connections.items():
            try:
                self.record_identity(eid, conn)
            except (WrongSession, Conflict) as e:
                self._confirmed.add(eid)  # report once; the endpoint stays unconfirmed
                self.bus.pair_event(self.pair_id, "human_needed", {
                    "status": "failed", "reason": f"identity check failed for {eid}: {e}"},
                    self.who())
                self.log(f"identity check failed for {eid}: {e}; routing stopped")
                return "failed", False
        self.observe_unresolved(st)
        if st["inflight"]:
            self.handle_inflight(st)
            return self.bus.pair_state(self.pair_id)["status"], True
        if self.workflow() and self.review_gates(st):
            return self.bus.pair_state(self.pair_id)["status"], True
        if st["pending_route"]:
            if st["attention"]:
                return st["status"], False
            self.route(st["pending_route"])
            return self.bus.pair_state(self.pair_id)["status"], True
        if st["queued"] and not st["paused"] and not st["global_paused"] and not st["attention"]:
            progressed = self.dispatch(st)
            return self.bus.pair_state(self.pair_id)["status"], progressed
        return st["status"], False

    # ------------------------------------------------------------ routing
    def route(self, pr: dict) -> None:
        decision = P.route(pr["role"], pr["action"])
        if self.workflow():
            if decision == "to_leader" and not self.run_check(pr["turn"]):
                return
            if decision == "finish":
                return self.accept_or_stop(pr)
        if decision in ("to_leader", "to_follower"):
            role = "leader" if decision == "to_leader" else "follower"
            self.ev("queued", message=pr["message"], to=self.d[role]["participant"], role=role)
            self.log(f"queued {pr['message']} for the {role}")
        elif decision == "finish":
            self.ev("finished", message=pr["message"])
            self.log(f"leader finished the exchange ({pr['message']})")
        else:
            self.ev("human_needed", reason=f"{pr['role']} replied {pr['action']}; see "
                    f"{pr['message']}", message=pr["message"], turn=pr["turn"], routed=True)
            self.log(f"{pr['role']} needs the human ({pr['action']}); routing stopped")

    def dispatch(self, st: dict) -> bool:
        q = st["queued"]
        role = q["role"]
        eid = self.endpoint(role)
        conn = self.connections.get(eid)
        if conn is None:
            self.ev("human_needed", status="disconnected", reason=f"{role} endpoint not connected")
            return False
        # Consume pending notifications first so the adapter's view of activity is
        # current; activity that finished before this dispatch is not ours.
        for ev in conn.poll(0):
            if ev.type == "turn_completed":
                self.handle_unrouted_completion(ev, role)
        cstat = conn.status()
        if cstat == "disconnected":
            self.ev("human_needed", status="disconnected", reason=f"{role} session is not running")
            self.bus.session_event(eid, "disconnected", {"connection": conn.connection_id},
                                   self.who())
            return False
        if cstat != "idle":  # busy, waiting or unknown: fail closed, do not submit
            if self._last_wait != (role, cstat):
                self.log(f"waiting: {role} session is {cstat}; not dispatching")
                self._last_wait = (role, cstat)
            return False
        self._last_wait = None
        turn = new_id("u")
        request_id = "r_" + turn[2:]
        try:
            data = {"ctl": self.ctl, "turn": turn, "message": q["message"], "to": q["to"], "role": role,
                    "request_id": request_id, "timeout_at": time.time() + self.d["turn_timeout_s"]}
            if self.fences is not None:
                data["fences"] = self.fences()  # raises if a conversation isn't relay-owned
            self.bus.authorize_dispatch(self.pair_id, data, self.who())
        except RelayError as e:  # global/pair pause, budget or a race: nothing was sent
            self.log(f"dispatch not authorized: {e}")
            if "budget" in str(e):
                self.ev("human_needed", reason=str(e))
            return False
        if conn.status() != "idle":  # revalidate immediately before submission
            self.ev("turn_failed", turn=turn, status="waiting_human",
                    reason=f"{role} session became busy before submission; nothing was sent")
            return True
        why = self.before_submit(eid) if self.before_submit else None
        if why:  # e.g. a terminal reopened the conversation after the gate: don't send.
            # Nothing reached the client, so the message stays queued for when the
            # conversation is the relay's again (the hand-off pauses the team meanwhile).
            self.ev("turn_failed", turn=turn, status="waiting_human", requeue=True,
                    reason=f"not sent to the {role}: {why}")
            return True
        msg = self.bus.get_message(q["message"])
        check = None
        if self.workflow() and role == "leader" and msg.get("turn_id"):
            check = next((c for c in reversed(self.bus.pair_state(self.pair_id)["checks"])
                          if c["revision"] == msg["turn_id"]), None)
        elif self.workflow() and role == "follower":
            # the implementer sees the relay's check of its own previous revision,
            # labelled as relay data (never permission). No extra model turn.
            st = self.bus.pair_state(self.pair_id)
            mine = [x["turn"] for x in st["turns"] if x["role"] == "follower" and x["turn"] != turn]
            if mine:
                check = next((dict(c, previous=True) for c in reversed(st["checks"])
                              if c["revision"] == mine[-1]), None)
        prompt = handoff_prompt(pair=self.d, turn_id=turn, role=role, message=msg, check=check)
        if self.fences is not None:
            # Linearize with ownership moves: after this commit the turn counts as sent
            # (a return waits for it); if a return was fenced first, nothing is sent.
            try:
                self.bus.commit_submit(self.pair_id, turn, data["fences"], self.who())
            except RelayError as e:
                self.ev("turn_failed", turn=turn, status="waiting_human", requeue=True,
                        reason=f"not sent to the {role}: {e}")
                return True
        self.ev("submit_attempted", turn=turn, request_id=request_id)
        self.submitted_here.add(turn)
        self.log(f"turn {turn}: submitting {msg['id']} to the {role}")
        try:
            receipt = conn.submit(prompt, request_id)
        except NotSubmitted as e:
            self.ev("turn_failed", turn=turn, status="waiting_human",
                    reason=f"not delivered: {e}")
            return True
        except DeliveryUncertain as e:
            self.ev("delivery_uncertain", turn=turn, reason=str(e))
            return True
        except Exception as e:  # anything else: we cannot prove what the client saw
            self.ev("delivery_uncertain", turn=turn, reason=f"adapter error: {e!r}")
            return True
        self.ev("accepted", turn=turn, client_turn_id=receipt.get("client_turn_id"))
        self.log(f"turn {turn}: accepted by the {role} (client turn {receipt.get('client_turn_id')})")
        return True

    # ----------------------------------------------------------- in flight
    def handle_inflight(self, st: dict) -> None:
        turn = st["inflight"]
        eid = self.endpoint(turn["role"])
        conn = self.connections.get(eid)
        if turn["turn"] not in self.submitted_here:
            self.recover_inflight(turn, conn, eid)
            return
        if time.time() > (turn.get("timeout_at") or float("inf")):
            self.ev("turn_timed_out", turn=turn["turn"], reason="turn timeout")
            self.log(f"turn {turn['turn']} timed out; the client may still be working")
            return
        for ev in conn.poll(self.poll_interval):
            self.handle_event(turn, ev, eid)
            cur = self.bus.pair_state(self.pair_id)["inflight"]
            if not cur or cur["turn"] != turn["turn"]:
                turn = cur
                if not turn:
                    break
            else:
                turn = cur

    def observe_unresolved(self, st: dict) -> None:
        """Keep a bounded watch on timed-out turns so late completions are recorded.

        Runs whether or not dispatch is allowed. Late completions are captured as
        `late_response` evidence and never routed automatically.
        """
        late_done = {x["turn"] for x in st["late"]}
        busy_role = st["inflight"]["role"] if st["inflight"] else None
        for turn in st["turns"]:
            if turn["phase"] != "timed_out" or turn["turn"] in late_done or turn["role"] == busy_role:
                continue
            conn = self.connections.get(self.endpoint(turn["role"]))
            if conn is None:
                continue
            for ev in conn.poll(0):
                if ev.type == "turn_completed":
                    self.handle_unrouted_completion(ev, turn["role"])

    def handle_unrouted_completion(self, ev, role: str) -> None:
        old = self._turn_by_request(ev.request_id)
        if old and old["phase"] == "timed_out":
            self.capture(old, ev.text or "", late=True)
        elif not old:
            self.log(f"ignored a {role} turn that was not routed by the relay")

    def recover_inflight(self, turn: dict, conn: Connection | None, eid: str) -> None:
        t = turn["turn"]
        if turn["phase"] == "authorized":
            self.ev("turn_failed", turn=t, status="waiting_human",
                    reason="a previous controller stopped before submitting; nothing was sent")
            return
        if conn is None:
            self.ev("delivery_uncertain", turn=t, reason="recipient not connected after restart")
            return
        try:
            found = conn.lookup(turn["request_id"])
        except Unsupported:
            self.ev("delivery_uncertain", turn=t,
                    reason="controller restarted and the adapter cannot look up the turn")
            return
        if found is None:
            self.ev("delivery_uncertain", turn=t, reason="controller restarted; the client has no "
                    "record of this request. Not resubmitted automatically.")
            return
        self.submitted_here.add(t)
        if turn["phase"] == "submitting" and found.get("client_turn_id"):
            self.ev("accepted", turn=t, client_turn_id=found["client_turn_id"])
        if "completed_text" in found:
            self.capture(self.bus.pair_state(self.pair_id)["inflight"], found["completed_text"])
        else:
            self.log(f"turn {t}: reconciled after restart; still waiting for completion")

    def handle_event(self, turn: dict, ev, eid: str) -> None:
        t = turn["turn"]
        mine = ev.request_id == turn["request_id"] or (
            ev.client_turn_id is not None and ev.client_turn_id == turn.get("client_turn_id"))
        if ev.type == "accepted":
            if mine and turn["phase"] == "submitting":
                self.ev("accepted", turn=t, client_turn_id=ev.client_turn_id)
            return
        if ev.type in ("delta", "tool", "turn_started", "echo"):
            if mine and ev.type != "echo":
                self.log(f"  [{turn['role']} progress] {ev.type}: {(ev.text or '')[:60]!r}")
            return
        if ev.type in ("approval_pending", "human_question"):
            if mine:
                self.ev("client_waiting", turn=t, reason=f"{ev.type} in the {turn['role']}'s own "
                        "interface; answer it there",
                        # The client's own id for this approval; without one, the event's stream
                        # position identifies it (never the relay's turn request, which every
                        # approval in a turn shares).
                        request=(ev.data or {}).get("native_request_id"))
                self.log(f"turn {t}: client is waiting for the human ({ev.type})")
            return
        if ev.type == "approval_resolved":
            if mine:
                self.ev("client_waiting", turn=t, reason=None)
            return
        if ev.type == "turn_completed":
            if mine:
                self.capture(turn, ev.text or "", audit=(ev.data or {}).get("tool_audit"))
                return
            old = self._turn_by_request(ev.request_id)
            if old and old["phase"] == "timed_out":
                self.capture(old, ev.text or "", late=True)
                return
            if old:
                return  # duplicate completion of an already captured turn
            key = f"{t}:{ev.client_turn_id}"
            if key not in self._ambiguity_reported:
                self._ambiguity_reported.add(key)
                self.bus.pair_event(self.pair_id, "human_needed", {
                    "reason": "ambiguous_attribution: an unrelated turn completed in the "
                              f"{turn['role']} session while routed turn {t} was pending",
                    "turn": t}, self.who())
                self.log(f"turn {t}: unrelated turn in the recipient session; routing stopped")
            return
        if ev.type == "disconnected":
            self.bus.session_event(eid, "disconnected", {"connection": self.connections[eid].connection_id,
                                                         "reason": ev.text}, self.who())
            try:
                found = self.connections[eid].lookup(turn["request_id"])
            except Unsupported:
                found = None
            if found and "completed_text" in found:
                self.capture(turn, found["completed_text"])
            else:
                self.ev("delivery_uncertain", turn=t, reason=f"client disconnected: {ev.text}")
            return
        if ev.type == "error":
            if ev.data.get("policy_mismatch"):  # stopped and closed by the adapter
                self.ev("turn_failed", turn=t, status="failed", reason=ev.text,
                        tool_audit=ev.data.get("tool_audit") or [])
                self.log(f"turn {t}: {ev.text}")
            elif ev.data.get("identity_mismatch"):
                self.ev("turn_failed", turn=t, status="failed",
                        reason=f"client identity check failed: {ev.text}")
                self.log(f"turn {t}: {ev.text}; nothing routed")
            elif mine or ev.request_id is None:
                self.ev("turn_failed", turn=t, reason=f"client error: {ev.text}")

    def _turn_by_request(self, request_id):
        if request_id is None:
            return None
        for turn in self.bus.pair_state(self.pair_id)["turns"]:
            if turn["request_id"] == request_id:
                return turn
        return None

    # -------------------------------------------------------------- capture
    def _evidence(self, turn_id: str, text: str) -> str:
        raw = text.encode()
        note = ""
        if len(raw) > EVIDENCE_LIMIT:
            note = f"\n\n[relay: evidence truncated for storage; full size {len(raw)} bytes, " \
                   f"sha256 {sha256(raw)}]"
            raw = raw[:EVIDENCE_LIMIT]
        name = f"{turn_id}.txt"
        self.bus.root.publish(("evidence", name), raw + note.encode())
        return f"evidence/{name}"

    def capture(self, turn: dict, text: str, late: bool = False, audit: list | None = None) -> None:
        t = turn["turn"]
        if t in self.captured:
            return
        role = turn["role"]
        peer_role = "follower" if role == "leader" else "leader"
        size = len(text.encode())
        eid = self.endpoint(role)
        if eid in self.borrowed:
            # A hand-off writer may start provisional (Claude reports its session only once
            # the first prompt runs). Nothing it produced is stored or routed until the
            # client itself has confirmed the exact conversation AND folder.
            why = None
            try:
                self.record_identity(eid, self.connections[eid])
            except (WrongSession, Conflict) as e:
                why = f"identity check failed: {e}"
            if why is None and eid not in self._confirmed:
                why = "the client never confirmed which conversation and folder it is"
            if why:
                self.captured.add(t)
                self.ev("turn_failed", turn=t, status="failed", reason=f"{why}; result withheld")
                self.log(f"turn {t}: {why}; result withheld")
                return

        def fail(reason: str, evidence: bool = True):
            self.captured.add(t)
            ev = self._evidence(t, text) if evidence else None
            if late:
                self.bus.pair_event(self.pair_id, "human_needed",
                                    {"reason": f"late response to {t}: {reason}", "turn": t},
                                    self.who())
            else:
                self.ev("turn_failed", turn=t, status="waiting_human", reason=reason, evidence=ev)
            self.log(f"turn {t}: {reason}; stopped for review")

        try:
            body, block = P.parse_response(text, t)
        except P.ControlError as e:
            return fail(f"malformed_control: {e}")
        if len(body.encode()) > P.MAX_BODY:
            return fail(f"response too large ({size} bytes > {P.MAX_BODY}); not truncated or routed")
        warns = P.secret_warnings(body, block["subject"])
        if warns:
            return fail(f"possible secret in response ({', '.join(warns)}); not stored or routed — "
                        f"inspect it in the client (sha256 {sha256(text.encode())[:16]})",
                        evidence=False)
        kind = {"ask_human": "question", "blocked": "blocked"}.get(block["action"]) or block["kind"] \
            or ("result" if role == "follower" else "task")
        sender = self.bus.get_session(self.endpoint(role))
        recipient = self.bus.get_session(self.endpoint(peer_role))
        env, _, _ = self.bus.send(
            sender=self.d[role]["participant"], to=self.d[peer_role]["participant"], kind=kind,
            subject=block["subject"] or f"{role} {block['action']}", body=body,
            thread_id=self.d["thread_id"], reply_to=turn["message"], scope=self.d["scope"],
            pair_id=self.pair_id, turn_id=t, sender_session=_label(sender),
            recipient_session=_label(recipient), idempotency_key=f"turn:{t}")
        self.captured.add(t)
        if late:
            self.bus.pair_event(self.pair_id, "late_response",
                                {"turn": t, "message": env["id"], "action": block["action"]},
                                self.who())
            self.log(f"late response to timed-out turn {t} recorded as {env['id']} (not routed)")
            return
        self.ev("response_captured", turn=t, message=env["id"], action=block["action"],
                accepted=bool(block.get("accepted")), **({"tool_audit": audit} if audit is not None else {}))
        self.log(f"turn {t}: {role} replied {block['action']} -> message {env['id']}")
        self.record_progress(t, role, block.get("progress"))

    # ------------------------------------------------------- review loop
    DEADLINE = "Time limit reached: no more prompts, checks or retries for this team."

    def workflow(self) -> dict | None:
        wf = self.d.get("workflow")
        return wf if wf and wf.get("kind") == "review_loop" else None

    def review_gates(self, st: dict) -> bool:
        """Before routing or dispatch: settle a check a crash interrupted (unknown, never
        passed), and enforce the persisted deadline (no automatic extension)."""
        run = st.get("check_running")
        if run and not getattr(self, "_checking", False):
            self.ev("check_finished", revision=run["revision"], status="unknown",
                    reason="the relay stopped during the check; its result is unknown",
                    config=self.workflow()["check_config"])
            return True
        if (st["pending_route"] or st["queued"]) and not st["attention"] and self.past_deadline():
            self.ev("human_needed", reason=self.DEADLINE, status="waiting_human")
            self.log("time limit reached; routing stopped")
            return True
        return False

    def past_deadline(self) -> bool:
        return time.time() >= float(self.workflow()["deadline_ts"])

    def run_check(self, revision: str) -> bool:
        """The relay's own check of the implementer's revision, before review. Returns
        False when routing must stop (the time limit)."""
        st = self.bus.pair_state(self.pair_id)
        if any(c["revision"] == revision for c in st["checks"]):
            return True
        if self.past_deadline():
            self.ev("human_needed", reason=self.DEADLINE, status="waiting_human")
            return False
        wf = self.workflow()
        self.ev("check_started", revision=revision)
        self._checking = True
        try:
            runner = self.check_runner or V.default_runner()
            why = runner.available()
            if why:
                res = {"status": "unavailable", "reason": why}
            elif runner.config_hash(wf["check"]) != wf["check_config"]:
                res = {"status": "unavailable", "reason": "the check's command or isolation changed since the team "
                                                          "was created"}
            else:
                res = V.check(runner, wf["check"])
        except Exception as e:  # never "passed" by accident
            res = {"status": "unknown", "reason": f"the check couldn't run ({type(e).__name__}: {e})"[:300]}
        finally:
            self._checking = False
        self.ev("check_finished", revision=revision, config=wf["check_config"],
                **{k: res.get(k) for k in P.CHECK_FIELDS if k != "config"})
        self.log(f"relay check of {revision}: {res['status']}")
        return True

    def acceptance_problem(self, pr: dict) -> tuple[str | None, dict]:
        if not pr.get("accepted"):
            return "the reviewer finished without accepting the work", {}
        st = self.bus.pair_state(self.pair_id)
        impl = [t for t in st["turns"] if t["role"] == "follower"]
        if not impl:
            return "nothing has been implemented yet", {}
        last = impl[-1]
        if last["phase"] != "completed":
            return "the implementer's latest turn didn't complete", {}
        chk = next((c for c in reversed(st["checks"]) if c["revision"] == last["turn"]), None)
        if not chk:
            return "the relay has no check of the latest revision", {}
        if chk["status"] != "passed":
            return f"the relay's check of the latest revision is {chk['status'].replace('_', ' ')}", {}
        wf = self.workflow()
        if chk.get("config") != wf["check_config"] or not chk.get("fingerprint"):
            return "the passing check doesn't match this team's check", {}
        try:
            now = V.fingerprint(wf["check"]["workspace"])["sha256"]
        except (OSError, V.FingerprintError) as e:
            return f"the workspace can't be fingerprinted now ({e})", {}
        if now != chk["fingerprint"]:
            return "the workspace changed after the passing check", {}
        return None, {"revision": last["turn"], "check_seq": chk["seq"], "fingerprint": now}

    def accept_or_stop(self, pr: dict) -> None:
        why, ok = self.acceptance_problem(pr)
        if why:
            self.ev("human_needed", reason=f"Not accepted: {why}.", message=pr["message"], turn=pr["turn"],
                    routed=True)
            self.log(f"acceptance refused: {why}")
            return
        self.ev("accepted_work", **ok)
        self.ev("finished", message=pr["message"])
        self.log(f"reviewer accepted revision {ok['revision']} with the relay's passing check")

    def record_progress(self, t: str, role: str, raw) -> None:
        """Optional manager progress. Recorded after the response; a missing or
        invalid report is a diagnostic only and never changes routing."""
        if raw is None:
            return
        try:
            if role != "leader":
                raise P.ProgressError("only the manager reports progress")
            ok = P.validate_progress(raw)
            self.ev("progress_reported", turn=t, **ok)
        except P.ProgressError as e:
            self.ev("progress_rejected", turn=t, reason=str(e))
            self.log(f"turn {t}: progress not recorded ({e})")
        except RelayError as e:  # e.g. the team ended meanwhile: progress is optional
            self.log(f"turn {t}: progress not recorded ({e})")


def _label(session: dict) -> str | None:
    cid = session.get("client_session_id") or ""
    safe = "".join(c if c.isalnum() or c in "._-" else "-" for c in cid)[:120]
    return safe if safe and safe[0].isalnum() else None


def answer(bus: Bus, pair_id: str, body: str, *, to_role: str | None = None,
           subject: str | None = None, reply_to: str | None = None) -> dict:
    """Human answer to a question/blocked/attention state: continue the same pair.

    The answer is a new durable message from `human`, queued for one role. Budget,
    scope and limits are unchanged; dispatching it consumes one turn.
    """
    from .bus import HUMAN
    st = bus.pair_state(pair_id)
    d = st["definition"]
    att = st.get("attention")
    if st.get("terminal"):
        raise Conflict(f"pair is {st['terminal']}")
    if not att or st.get("inflight"):
        raise Conflict("the pair is not waiting for the human")
    asked = bus.get_message(att["message"]) if att.get("message") else None
    if to_role is None:
        if asked is None:
            raise RelayError("say who receives the answer (--to leader|follower)")
        to_role = bus.role_of(d, asked["from"])
    if to_role not in ("leader", "follower"):
        raise RelayError("--to must be leader or follower")
    m, _, _ = bus.send(sender=HUMAN, to=d[to_role]["participant"], kind="task",
                       subject=subject or "Answer from the human", body=body,
                       thread_id=d["thread_id"],
                       reply_to=asked["id"] if asked else (reply_to or st.get("last_message")),
                       scope=d["scope"], pair_id=pair_id)
    data = {"outcome": "answer", "reason": "human answered", "turn": att.get("turn"),
            "drop_queue": True, "requeue": {"message": m["id"], "to": m["to"], "role": to_role}}
    bus.pair_event(pair_id, "reconciled", data, actor("human"))
    return {"message": m["id"], "to": to_role}


def abandon_unknown(bus: Bus, pair_id: str, who: dict | None = None) -> dict:
    """Record the human's explicit disposition of every unknown turn outcome: management
    ends and the conversations may be given back; those turns may have run and will not
    be resent. Their outcome stays unknown in history (never relabelled)."""
    from .protocol import ABANDON_STATEMENT, unknown_turns
    who = who or actor("human")
    st = bus.pair_state(pair_id)
    if not unknown_turns(st):
        raise Conflict("no turn has an unknown outcome to acknowledge")
    if not st.get("terminal"):
        bus.pair_event(pair_id, "stopped", {"reason": "ended to give back with an unknown turn outcome"}, who)
    done = []
    for t in unknown_turns(bus.pair_state(pair_id)):
        bus.pair_event(pair_id, "outcome_abandoned", {"turn": t["turn"], "statement": ABANDON_STATEMENT}, who)
        done.append(t["turn"])
    return {"abandoned": done, "statement": ABANDON_STATEMENT}


def reconcile(bus: Bus, pair_id: str, outcome: str, *, message: str | None = None,
              reason: str = "") -> dict:
    """Human resolution of a pair that needs attention (docs/PROTOCOL.md 7.1)."""
    st = bus.pair_state(pair_id)
    d = st["definition"]
    att = st.get("attention")
    if not att:
        raise Conflict("pair does not need reconciliation")
    data: dict = {"outcome": outcome, "reason": reason, "turn": att.get("turn")}
    if outcome == "continue":
        pass
    elif outcome == "requeue":
        turn = next((t for t in st["turns"] if t["turn"] == att.get("turn")), None)
        if not turn:
            raise Conflict("no turn to requeue")
        data["requeue"] = {"message": turn["message"], "to": turn["to"], "role": turn["role"]}
        data["note"] = "the earlier attempt may have run; this is a human decision to resend"
    elif outcome == "drop":
        data["drop_queue"] = True
    elif outcome == "route-late":
        late = next((x for x in st["late"] if x["message"] == message), None)
        if not late:
            raise Conflict(f"{message} is not a recorded late response")
        m = bus.get_message(message)
        role = bus.role_of(d, m["to"])
        data["requeue"] = {"message": message, "to": m["to"], "role": role}
    else:
        raise RelayError(f"unknown outcome {outcome}")
    bus.pair_event(pair_id, "reconciled", data, actor("human"))
    return data
