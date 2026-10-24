"""Hand-off: fake interactive terminals handed to the relay
and back. Covers ownership fencing, pair-wide return, races at the submit boundary,
partial hand-off, crash/restart boundaries and unknown shutdown. Fake clients only;
real adapters stay locked."""

import os
import threading
import time
from unittest import mock

from util import RelayTest

from relay.adapters.codex import CodexAppServerAdapter
from relay.bus import Bus
from relay.handoff import Handoffs, Ownership
from relay.host import Host
from relay.observer import Discovery
from relay.storage import Blocked, Conflict, actor, init_root


class Base(RelayTest):
    def setUp(self):
        super().setUp()
        self.root = init_root(os.path.join(self.tmp, "console-root"), ["*"])
        self.bus = Bus(self.root)
        self.discovery = Discovery({"home": self.home}, ("fake",), interval=60)
        self.host = self.new_host()
        self.proj = os.path.realpath(os.path.join(self.tmp, "Projects", "agent-relay"))
        os.makedirs(self.proj)

    def new_host(self):
        return Host(self.bus, self.discovery, new_session_adapters={"claude": "fake", "codex": "fake"},
                    adapter_options={"home": self.home})

    def tearDown(self):
        self.doCleanups()  # second consoles shut down while their relay root still exists
        self.host.shutdown()
        for c in list(self.host.handoffs.writers.values()):
            try:
                c.close()
            except Exception:
                pass
        super().tearDown()

    # ------------------------------------------------------------ helpers
    def terminal(self, sid, client, **default):
        """An open interactive terminal (no live attach) with two earlier turns."""
        default.setdefault("natural", True)
        return self.fake(sid, interactive=True, seed_history=2, client=client, cwd=self.proj, default=default)

    def reopen(self, sid, client, **default):
        default.setdefault("natural", True)
        return self.fake(sid, interactive=True, client=client, cwd=self.proj, default=default)

    def cat(self, host=None):
        self.discovery.refresh_once()
        return {x["name"]: x for x in (host or self.host).catalog()["sessions"]}

    def manage_pair(self, follower="agent-relay · Claude", manager="agent-relay · Codex"):
        c = self.cat()
        return self.host.manage(c[follower]["key"], {"existing": c[manager]["key"]}, "Tidy the README.")

    def tick_until(self, pred, timeout=20, host=None):
        end = time.time() + timeout
        while time.time() < end:
            (host or self.host).handoff_tick()
            if pred():
                return True
            time.sleep(0.1)
        h = (host or self.host).handoffs
        seen = {}
        for sid in ("cl", "cx"):  # say where it stopped, so a timing failure can be diagnosed
            try:
                st = h.own.get(h.key("fake", sid))
                seen[sid] = {"state": st["state"], "gen": st["gen"], "reason": st.get("reason"),
                             "retry_after": (st["detail"] or {}).get("retry_after"),
                             "exit": h.exit_status("fake", sid)}
            except Exception as e:  # pragma: no cover - diagnostics only
                seen[sid] = repr(e)
        raise AssertionError(f"condition not met in time; hand-offs: {seen}")

    def own(self, sid, host=None):
        h = (host or self.host).handoffs
        return h.own.get(h.key("fake", sid))

    def members(self, pid):
        return self.host._handoff_members(pid)


def _ctx(home, sid):
    import json
    with open(os.path.join(home, "sessions", sid, "context.json")) as f:
        return json.load(f)


class RouteTests(Base):
    def test_interactive_terminals_are_hand_off_only_and_real_attach_needs_exclusive_input(self):
        self.terminal("cl", "Claude")
        x = self.cat()["agent-relay"]
        self.assertEqual((x["route"], x["ownership"]["state"], x["can_manage"]), ("handoff", "native", True))
        # A Codex app-server attach would only be an idle check followed by a submit: not a lock.
        caps = CodexAppServerAdapter({}).capabilities()
        self.assertTrue(caps["attach_live"])
        self.assertFalse(caps["exclusive_input"])
        self.assertFalse(caps["handoff"])  # real hand-off stays locked
        item = {"state": "idle", "adapter": "codex-app-server", "endpoint": None, "client": "Codex"}
        ok, why = self.host._manageable(item)
        self.assertFalse(ok)
        self.assertIn("someone typing", why)


class LifecycleTests(Base):
    def test_full_cycle_hand_off_route_return_and_again(self):
        cl = self.terminal("cl", "Claude", sleep=0.5)
        cx = self.terminal("cx", "Codex", finish_after=50, sleep=0.3)
        r = self.manage_pair()
        pid = r["team"]
        self.assertEqual({h["client"] for h in r["handoff"]}, {"Claude", "Codex"})
        self.assertIn("/exit", next(h["step"] for h in r["handoff"] if h["client"] == "Claude"))
        # Both terminals are still open: nothing starts, nothing is sent.
        self.host.handoff_tick()
        self.assertEqual((self.own("cl")["state"], self.own("cx")["state"]), ("awaiting_exit", "awaiting_exit"))
        self.assertFalse(self.host._hosted_alive(pid))
        cl.stop()  # the person types /exit in each terminal
        self.host.handoff_tick()
        self.assertEqual(self.own("cl")["state"], "relay_owned")
        self.assertFalse(self.host._hosted_alive(pid))  # waits for BOTH before routing
        cx.stop()
        self.tick_until(lambda: self.host._hosted_alive(pid))
        self.tick_until(lambda: _ctx(self.home, "cl")["routed"] >= 1)
        # The same conversations continued: earlier turns are still there.
        self.assertEqual(sum(1 for t in _ctx(self.home, "cl")["turns"] if t["request_id"] is None), 2)

        # Back to terminal for the follower: pair-wide stop, settle, verified exit, claims kept.
        res = self.host.back_to_terminal(pid, ["follower"])
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["sessions"][0]["resume"], "claude --resume cl")
        self.assertEqual(self.own("cl")["state"], "released")
        self.assertEqual(self.own("cx")["state"], "relay_owned")  # the manager's conversation stays as is
        st = self.bus.pair_state(pid)
        self.assertTrue(st["paused"])
        self.assertIsNone(st["inflight"])
        used = st["used"]
        eid_cl = next(e for e, m in self.members(pid).items() if m[1] == "cl")
        self.assertEqual(self.bus.get_session(eid_cl)["state"]["pair"], pid)  # claim kept
        routed = _ctx(self.home, "cl")["routed"]
        time.sleep(1.0)
        self.host.handoff_tick()
        self.assertEqual(_ctx(self.home, "cl")["routed"], routed)  # nobody submits to a returned follower
        self.assertEqual(self.bus.pair_state(pid)["used"], used)

        # The person uses it natively, then hands it over again: history and budget carry on.
        cl2 = self.reopen("cl", "Claude", sleep=0.5)
        cl2.manual_turn("a native turn while it was back in the terminal")
        self.wait_until(lambda: len(_ctx(self.home, "cl")["turns"]) > routed + 2)
        self.host.hand_over_again(pid)
        self.host.handoff_tick()
        self.assertEqual(self.own("cl")["state"], "awaiting_exit")
        cl2.stop()
        self.tick_until(lambda: self.host._hosted_alive(pid) and not self.bus.pair_state(pid)["paused"])
        self.tick_until(lambda: _ctx(self.home, "cl")["routed"] > routed)
        st = self.bus.pair_state(pid)
        self.assertGreaterEqual(st["used"], used)  # budget not reset
        reqs = [t["request_id"] for t in _ctx(self.home, "cl")["turns"] if t["request_id"]]
        self.assertEqual(len(reqs), len(set(reqs)))  # nothing sent twice
        gens = [h["gen"] for h in self.own("cl")["history"]]
        self.assertEqual(gens, sorted(gens))
        self.assertEqual(len(gens), len(set(gens)))


class RaceTests(Base):
    def owned_pair(self, **cl_default):
        cl = self.terminal("cl", "Claude", **cl_default)
        cx = self.terminal("cx", "Codex", finish_after=50)
        pid = self.manage_pair()["team"]
        cl.stop(), cx.stop()
        self.tick_until(lambda: self.own("cl")["state"] == "relay_owned" and self.own("cx")["state"] == "relay_owned")
        return pid

    def test_native_writer_appearing_between_gate_and_submit_is_caught_before_sending(self):
        pid = self.owned_pair(sleep=0.2)
        real = self.bus.authorize_dispatch
        injected = []

        def gate_then_terminal_opens(pair_id, data, who):
            ev = real(pair_id, data, who)
            if data.get("role") == "follower" and not injected:
                injected.append(self.reopen("cl", "Claude"))  # exactly at the boundary
            return ev
        before = _ctx(self.home, "cl")["routed"]
        with mock.patch.object(self.bus, "authorize_dispatch", side_effect=gate_then_terminal_opens):
            self.tick_until(lambda: injected and self.own("cl")["state"] == "blocked")
            self.tick_until(lambda: self.bus.pair_state(pid)["paused"])
        self.assertEqual(_ctx(self.home, "cl")["routed"], before)  # nothing was sent to it
        fails = [e for e in self.events(f"pair-{pid}") if e.type == "turn_failed"]
        self.assertIn("not sent to the follower", fails[-1].data["reason"])
        self.assertIn("also opened in a terminal", self.own("cl")["reason"])

    def test_native_writer_appearing_mid_turn_is_detected_and_routing_stops(self):
        """Detection after the fact is recovery, not prevention (cooperative hand-off)."""
        pid = self.owned_pair(sleep=1.5)
        self.tick_until(lambda: (self.bus.pair_state(pid).get("inflight") or {}).get("role") == "follower")
        self.reopen("cl", "Claude")
        self.tick_until(lambda: self.own("cl")["state"] == "blocked")
        self.tick_until(lambda: self.bus.pair_state(pid)["paused"])
        self.wait_until(lambda: not self.bus.pair_state(pid)["inflight"], timeout=20)
        n = len([e for e in self.events(f"pair-{pid}") if e.type == "dispatch_authorized"])
        time.sleep(1.0)
        self.host.handoff_tick()
        self.assertEqual(len([e for e in self.events(f"pair-{pid}") if e.type == "dispatch_authorized"]), n)

    def test_return_landing_between_gate_and_submit_requeues_the_unsent_turn(self):
        """Back to terminal fenced after a dispatch passed its gate: that turn is refused
        before sending, goes back on the queue uncounted, and is sent once after re-hand-off."""
        pid = self.owned_pair()
        real = self.bus.authorize_dispatch
        hit = []

        def gate_then_return(pair_id, data, who):
            ev = real(pair_id, data, who)
            if data.get("role") == "leader" and not hit and self.bus.pair_state(pair_id)["used"] >= 2:
                hit.append(data["message"])
                self.host.handoffs.begin_return(self.host.handoffs.key("fake", "cl"))
            return ev
        with mock.patch.object(self.bus, "authorize_dispatch", side_effect=gate_then_return):
            self.tick_until(lambda: hit and not self.bus.pair_state(pid)["inflight"])
        st = self.bus.pair_state(pid)
        self.assertIsNone(st["attention"])
        self.assertEqual(st["queued"]["message"], hit[0])  # still waiting, not dropped
        failed = [e for e in self.events(f"pair-{pid}") if e.type == "turn_failed"][-1]
        self.assertTrue(failed.data["requeue"])
        used = st["used"]
        self.assertEqual(used, len([e for e in self.events(f"pair-{pid}") if e.type == "accepted"]))

    def test_requeue_is_refused_once_a_submit_was_attempted(self):
        pid = self.owned_pair(sleep=3)
        self.tick_until(lambda: (self.bus.pair_state(pid).get("inflight") or {}).get("phase") == "accepted")
        infl = self.bus.pair_state(pid)["inflight"]
        owner = self.bus.pair_state(pid)["owner"]["ctl"]
        with self.assertRaisesRegex(Conflict, "is accepted, expected authorized"):
            self.bus.pair_event(pid, "turn_failed", {"turn": infl["turn"], "requeue": True, "ctl": owner,
                                                      "reason": "pretend it was never sent"}, actor("controller", owner))

    def test_stale_generation_cannot_pass_the_gate(self):
        own = Ownership(self.bus)
        who = actor("test")
        own.move("k1", expect_gen=0, state="awaiting_exit")
        own.move("k1", expect_gen=1, state="relay_starting")
        st = own.move("k1", expect_gen=2, state="relay_owned", token="own_a")
        fence = {"k1": {"gen": st["gen"], "token": "own_a"}}
        self.bus.gate("dispatch", {"pair": "p", "turn": "u_1", "fences": fence}, who)  # current: passes
        own.move("k1", expect_gen=st["gen"], state="returning")
        with self.assertRaises(Blocked):
            self.bus.gate("dispatch", {"pair": "p", "turn": "u_2", "fences": fence}, who)
        with self.assertRaises(Blocked):  # a forged token doesn't help either
            self.bus.gate("dispatch", {"pair": "p", "turn": "u_3",
                                       "fences": {"k1": {"gen": st["gen"] + 1, "token": "own_b"}}}, who)

    def test_concurrent_hand_off_requests_one_wins(self):
        self.terminal("cl", "Claude")
        h1 = Handoffs(self.bus, {"home": self.home}, self.host._discover_one)
        h2 = Handoffs(self.bus, {"home": self.home}, self.host._discover_one)
        barrier, out = threading.Barrier(2), []
        real_get = Ownership.get

        def slow_get(own, key):  # both read "native" before either moves
            v = real_get(own, key)
            if threading.current_thread().name.startswith("h") and not getattr(threading.current_thread(), "seen", 0):
                threading.current_thread().seen = 1
                barrier.wait(5)
            return v

        def go(h):
            try:
                h.begin("fake", "cl", self.proj)
                out.append("ok")
            except Conflict:
                out.append("conflict")
        with mock.patch.object(Ownership, "get", slow_get):
            ts = [threading.Thread(target=go, args=(h,), name=f"h{i}") for i, h in enumerate((h1, h2))]
            [t.start() for t in ts]
            [t.join(10) for t in ts]
        self.assertEqual(sorted(out), ["conflict", "ok"])
        self.assertEqual(self.own("cl")["gen"], 1)


class FailureTests(Base):
    def test_partial_hand_off_then_cancel_releases_only_what_it_holds(self):
        cl = self.terminal("cl", "Claude")
        self.terminal("cx", "Codex")  # its terminal is never closed
        pid = self.manage_pair()["team"]
        cl.stop()
        self.tick_until(lambda: self.own("cl")["state"] == "relay_owned")
        self.assertEqual(self.own("cx")["state"], "awaiting_exit")
        self.assertFalse(self.host._hosted_alive(pid))
        self.assertEqual([e for e in self.events(f"pair-{pid}") if e.type == "dispatch_authorized"], [])
        writer = self.own("cl")["detail"]["relay"]
        res = self.host.cancel_handoff(pid)
        states = {self.members(pid)[x["endpoint"]][1]: x["state"] for x in res["sessions"]}
        self.assertEqual(states, {"cl": "released", "cx": "native"})
        from relay.handoff import same_process
        self.assertFalse(same_process(writer))  # the relay's writer really exited
        self.assertTrue(self.bus.pair_state(pid)["terminal"])

    def test_wrong_conversation_from_the_client_blocks_and_stops_the_writer(self):
        cl = self.terminal("cl", "Claude")
        h = self.host.handoffs
        h.begin("fake", "cl", self.proj)
        cl.stop()
        h.advance("fake", "cl")
        from relay.adapters.fake import FakeAdapter
        real = FakeAdapter.launch

        def wrong(adapter, cwd, options=None, **kw):
            conn = real(adapter, cwd, options, **kw)
            conn.confirmed_identity = {"client_session_id": "someone-else", "cwd": cwd}
            return conn
        with mock.patch.object(FakeAdapter, "launch", wrong):
            st = h.start_writer("fake", "cl")
        self.assertEqual(st["state"], "blocked")
        self.assertIn("different conversation", st["reason"])
        self.assertNotIn(h.key("fake", "cl"), h.writers)

    def test_unknown_shutdown_keeps_the_claim_and_gives_no_resume_instruction(self):
        cl = self.terminal("cl", "Claude")
        cx = self.terminal("cx", "Codex", finish_after=50)
        pid = self.manage_pair()["team"]
        cl.stop(), cx.stop()
        self.tick_until(lambda: self.host._hosted_alive(pid))
        real_stop = Handoffs.stop_writer  # a writer that ignores the stop: its exit can't be verified
        with mock.patch.object(Handoffs, "stop_writer",
                               lambda h, key, **k: real_stop(h, key, still_running=lambda r: True)):
            res = self.host.back_to_terminal(pid, ["follower"])
        s = res["sessions"][0]
        self.assertEqual((s["state"], s["resume"]), ("blocked", None))
        self.assertEqual(self.own("cl")["detail"]["shutdown"], "unknown")
        eid = s["endpoint"]
        self.assertEqual(self.bus.get_session(eid)["state"]["pair"], pid)  # claim retained
        x = self.cat()["agent-relay · Claude"]
        self.assertNotIn("resume", x["ownership"])

    def test_unsettled_turn_blocks_return(self):
        cl = self.terminal("cl", "Claude", sleep=5)
        cx = self.terminal("cx", "Codex", finish_after=50)
        pid = self.manage_pair()["team"]
        cl.stop(), cx.stop()
        # A turn the follower has accepted and is working on (not merely authorized: that
        # one would still be refused before sending and go back on the queue).
        self.tick_until(lambda: (self.bus.pair_state(pid).get("inflight") or {}).get("role") == "follower"
                        and self.bus.pair_state(pid)["inflight"]["phase"] == "accepted")
        res = self.host.back_to_terminal(pid, ["follower"], settle_s=0.5)
        self.assertFalse(res["settled"])
        self.assertEqual(self.own("cl")["state"], "blocked")
        self.assertIsNone(res["sessions"][0]["resume"])

    def test_relay_writer_dying_mid_turn_blocks_without_replay(self):
        cl = self.terminal("cl", "Claude", sleep=3)
        cx = self.terminal("cx", "Codex", finish_after=50)
        pid = self.manage_pair()["team"]
        cl.stop(), cx.stop()
        self.tick_until(lambda: (self.bus.pair_state(pid).get("inflight") or {}).get("role") == "follower")
        os.kill(self.own("cl")["detail"]["relay"]["pid"], 9)
        self.tick_until(lambda: self.own("cl")["state"] == "blocked")
        self.wait_until(lambda: self.bus.pair_state(pid)["status"] in ("disconnected", "delivery_uncertain",
                                                                       "waiting_human", "failed", "paused"))
        submits = [e for e in self.events(f"pair-{pid}") if e.type == "submit_attempted"]
        time.sleep(1.0)
        self.host.handoff_tick()
        self.assertEqual(len([e for e in self.events(f"pair-{pid}") if e.type == "submit_attempted"]), len(submits))


class RestartTests(Base):
    def test_console_restart_never_resumes_and_resolve_needs_a_verified_stop(self):
        cl = self.terminal("cl", "Claude")
        cx = self.terminal("cx", "Codex", finish_after=50)
        pid = self.manage_pair()["team"]
        cl.stop(), cx.stop()
        self.tick_until(lambda: self.host._hosted_alive(pid))
        self.host._stop_hosted(pid)  # the old console stops routing (its writers still run)
        old_writers = dict(self.host.handoffs.writers)
        again = self.new_host()  # a new console on the same relay root
        try:
            x = self.cat(again)["agent-relay · Claude"]
            self.assertEqual(x["ownership"]["state"], "relay_owned")
            self.assertTrue(x["ownership"]["orphaned"])
            again.handoff_tick()
            self.assertEqual(self.own("cl", again)["state"], "blocked")
            self.assertFalse(again._hosted_alive(pid))  # nothing restarts on its own
            self.assertTrue(self.bus.pair_state(pid)["paused"])
            eid = next(e for e, m in again._handoff_members(pid).items() if m[1] == "cl")
            r = again.resolve_handoff(pid, eid)  # the old writer is still running: can't release
            self.assertEqual(r["state"], "blocked")
            for c in old_writers.values():
                c.close()  # the old console's writers exit (as they do when a console dies)
            self.host.handoffs.writers.clear()
            r = again.resolve_handoff(pid, eid)
            self.assertEqual(r["state"], "released")
        finally:
            again.shutdown()

    def test_interrupted_transition_reads_as_blocked_and_cannot_dispatch(self):
        own = Ownership(self.bus)
        own.move("k2", expect_gen=0, state="awaiting_exit")
        own.move("k2", expect_gen=1, state="relay_starting")
        # Crash between the fence and its evidence: the fence says relay_owned ...
        self.bus.fence("k2", expect_gen=2, state="relay_owned", token="own_x", who=actor("test"))
        st = own.get("k2")
        self.assertEqual(st["state"], "blocked")  # ... but it reads as blocked
        self.assertTrue(st["interrupted"])
        h = Handoffs(self.bus, {"home": self.home}, lambda a, c: None)
        self.assertIsNone(h.fence("k2"))  # so no dispatch is ever decided under it


# ============================================================ review round 2
def _relay_writer_pids(home, sid):
    from relay.adapters.fake import live_writers
    return [w["pid"] for w in live_writers(home, sid) if w["writer"] == "relay"]


class CommitBoundaryTests(Base):
    """R1: the final submit commitment and ownership moves are totally ordered (control stream)."""

    def owned_pair(self, **cl_default):
        cl = self.terminal("cl", "Claude", **cl_default)
        cx = self.terminal("cx", "Codex", finish_after=50)
        pid = self.manage_pair()["team"]
        cl.stop(), cx.stop()
        self.tick_until(lambda: self.host._hosted_alive(pid))
        return pid

    def other_console(self):
        """A second console on the same relay root: shares no locks or memory with the first."""
        return Handoffs(Bus(self.root), {"home": self.home}, self.host._discover_one)

    def return_lands_before_commit(self, *, dispatch_role, returned_sid):
        pid = self.owned_pair(sleep=0.3)
        real = self.bus.commit_submit
        fired, sent_before = [], {}

        def commit(pair_id, turn, fences, who):
            st = self.bus.pair_state(pair_id)
            if not fired and st["inflight"]["role"] == dispatch_role and st["used"] >= 2:
                fired.append(turn)
                sent_before.update(cl=_ctx(self.home, "cl")["routed"], cx=_ctx(self.home, "cx")["routed"])
                other = self.other_console()
                self.bus.pair_event(pair_id, "paused", {"reason": "Returning to the terminal"}, actor("human"))
                other.begin_return(other.key("fake", returned_sid))  # after every check, before the commit
            return real(pair_id, turn, fences, who)
        with mock.patch.object(self.bus, "commit_submit", side_effect=commit):
            self.tick_until(lambda: fired and not self.bus.pair_state(pid)["inflight"])
        time.sleep(0.5)
        self.assertEqual({"cl": _ctx(self.home, "cl")["routed"], "cx": _ctx(self.home, "cx")["routed"]},
                         sent_before)  # nothing reached either session
        ev = next(e for e in self.events(f"pair-{pid}") if e.type == "turn_failed" and e.data["turn"] == fired[0])
        self.assertTrue(ev.data["requeue"])
        self.assertIn("ownership changed", ev.data["reason"])
        self.assertFalse(any(e.type == "submit_attempted" and e.data["turn"] == fired[0]
                             for e in self.events(f"pair-{pid}")))
        self.assertEqual(self.bus.pair_state(pid)["queued"]["role"], dispatch_role)  # waits, not dropped

    def test_return_of_follower_before_managers_commit_sends_nothing(self):
        # Codex's reproducer: the manager must not reach anyone once the follower is returning.
        self.return_lands_before_commit(dispatch_role="leader", returned_sid="cl")

    def test_return_of_leader_before_followers_commit_sends_nothing(self):
        self.return_lands_before_commit(dispatch_role="follower", returned_sid="cx")

    def test_return_after_last_check_but_before_commit_sends_nothing(self):
        """The exact R1 boundary: the final ownership check passes, then the return lands."""
        pid = self.owned_pair(sleep=0.3)
        real_check = Handoffs.check
        fired = []

        last = self.host.handoffs.key("fake", "cl")  # before_submit checks leader, then follower: cl is last

        def check_then_return(h, key):
            res = real_check(h, key)
            st = self.bus.pair_state(pid)
            in_dispatch = threading.current_thread().name == f"relay-{pid}"  # the controller, not the tick
            if res is None and key == last and in_dispatch and not fired and st.get("inflight") \
                    and st["inflight"]["role"] == "leader" and st["inflight"]["phase"] == "authorized" \
                    and st["used"] >= 2:
                fired.append(st["inflight"]["turn"])
                before.append(_ctx(self.home, "cx")["routed"])
                self.other_console().begin_return(self.host.handoffs.key("fake", "cl"))
            return res
        before = []
        with mock.patch.object(Handoffs, "check", check_then_return):
            self.tick_until(lambda: fired and not self.bus.pair_state(pid)["inflight"])
        time.sleep(0.5)
        self.assertEqual(_ctx(self.home, "cx")["routed"], before[0])  # the manager got nothing more
        turns = {t["turn"]: t for t in self.bus.pair_state(pid)["turns"]}
        self.assertEqual(turns[fired[0]]["phase"], "failed")

    def test_commit_before_return_means_the_return_waits_for_that_turn(self):
        pid = self.owned_pair(sleep=1.0)
        real = self.bus.commit_submit
        fired, result = [], {}

        def commit_then_return(pair_id, turn, fences, who):
            ev = real(pair_id, turn, fences, who)  # committed first ...
            st = self.bus.pair_state(pair_id)
            if not fired and st["inflight"]["role"] == "follower":
                fired.append(turn)
                t = threading.Thread(target=lambda: result.update(self.host.back_to_terminal(pid, ["follower"])))
                t.start()
                time.sleep(0.3)  # ... then the return is fenced while the send proceeds
            return ev
        with mock.patch.object(self.bus, "commit_submit", side_effect=commit_then_return):
            self.tick_until(lambda: result.get("settled") is not None, timeout=40)
        self.assertTrue(result["settled"])
        self.assertEqual(result["sessions"][0]["state"], "released")
        turns = {t["turn"]: t for t in self.bus.pair_state(pid)["turns"]}
        self.assertEqual(turns[fired[0]]["phase"], "completed")  # delivered and settled, not requeued
        self.assertFalse(any(e.type == "turn_failed" and e.data["turn"] == fired[0] for e in self.events(f"pair-{pid}")))

    def test_commit_refuses_stale_and_missing_ownership(self):
        own = Ownership(self.bus)
        own.move("k3", expect_gen=0, state="awaiting_exit")
        own.move("k3", expect_gen=1, state="relay_starting")
        st = own.move("k3", expect_gen=2, state="relay_owned", token="own_c")
        good = {"k3": {"gen": st["gen"], "token": "own_c"}}
        self.bus.commit_submit("p", "u_1", good, actor("test"))
        own.move("k3", expect_gen=st["gen"], state="returning")
        with self.assertRaises(Blocked):
            self.bus.commit_submit("p", "u_2", good, actor("test"))
        from relay.storage import InvalidInput
        with self.assertRaises(InvalidInput):
            self.bus.commit_submit("p", "u_3", {}, actor("test"))


class SpawnCrashTests(Base):
    """R2: a crash anywhere in starting the relay's writer never releases a live writer."""

    def crash_at(self, point):
        """Crash while starting the manager's (cx) writer: members start leader first."""
        cl = self.terminal("cl", "Claude")
        cx = self.terminal("cx", "Codex", finish_after=50)
        pid = self.manage_pair()["team"]
        cl.stop(), cx.stop()

        class Crash(Exception):
            pass

        def fault(p):
            if p == point:
                raise Crash(point)
        self.host.handoffs._fault = fault
        with self.assertRaises(Crash):
            self.host.handoff_tick()
        # The console dies here. Its writer process may already exist.
        self.host.handoffs.writers.clear()
        self.host.handoffs.starting.clear()
        again = self.new_host()
        self.addCleanup(again.shutdown)
        return pid, again

    def check_recovery(self, pid, again, sid):
        again.handoff_tick()
        own = self.own(sid, again)
        self.assertEqual(own["state"], "blocked")
        eid = next(e for e, m in again._handoff_members(pid).items() if m[1] == sid)
        c = again.console(pid)
        self.assertIn("resolve_handoff", c["handoff_actions"])  # recovery is reachable
        w = next(x for x in c["writers"] if x["endpoint"] == eid)
        self.assertNotIn("resume", w)  # never a resume instruction while a writer may run
        self.wait_until(lambda: _relay_writer_pids(self.home, sid))  # the launched writer runs
        live = _relay_writer_pids(self.home, sid)
        r = again.resolve_handoff(pid, eid)
        self.assertEqual(r["state"], "blocked")  # can't release while it runs
        self.assertIn("still running", r["reason"])
        self.assertEqual(self.bus.get_session(eid)["state"]["pair"], pid)  # claim kept
        for p in live:
            os.kill(p, 9)
        self.wait_until(lambda: not _relay_writer_pids(self.home, sid))
        r = again.resolve_handoff(pid, eid)
        self.assertEqual(r["state"], "released")

    def test_crash_after_child_creation_before_process_record(self):
        pid, again = self.crash_at("launched")
        self.assertIsNone(self.own("cx", again)["spawn"]["relay"])  # no pid was ever recorded ...
        self.assertTrue(self.own("cx", again)["spawn"]["intent"])   # ... but the intent was
        self.check_recovery(pid, again, "cx")

    def test_crash_after_process_record(self):
        pid, again = self.crash_at("recorded")
        self.assertTrue(self.own("cx", again)["spawn"]["relay"]["pid"])
        self.check_recovery(pid, again, "cx")

    def test_crash_after_writer_map_insertion_before_ownership_evidence(self):
        pid, again = self.crash_at("held")
        self.assertEqual(self.own("cx", again)["state"], "relay_starting")
        self.check_recovery(pid, again, "cx")

    def test_crash_between_fence_and_evidence_of_relay_owned(self):
        cl = self.terminal("cl", "Claude")
        cx = self.terminal("cx", "Codex", finish_after=50)
        pid = self.manage_pair()["team"]
        cl.stop(), cx.stop()
        real_move = Ownership.move

        def fence_only(own, key, *, expect_gen, state, token=None, **detail):
            if state == "relay_owned":
                own.bus.fence(key, expect_gen=expect_gen, state=state, token=token, who=actor("test"))
                raise RuntimeError("crash after the fence, before its evidence")
            return real_move(own, key, expect_gen=expect_gen, state=state, token=token, **detail)
        with mock.patch.object(Ownership, "move", fence_only), self.assertRaises(RuntimeError):
            self.host.handoff_tick()
        self.host.handoffs.writers.clear()
        again = self.new_host()
        self.addCleanup(again.shutdown)
        self.assertTrue(self.own("cx", again)["interrupted"])
        self.assertIsNone(again.handoffs.fence(again.handoffs.key("fake", "cx")))
        self.check_recovery(pid, again, "cx")

    def test_unknown_launch_outcome_is_never_released(self):
        """Intent recorded, no process recorded, and discovery can't see writers: unknown."""
        pid, again = self.crash_at("launched")
        again.handoff_tick()
        eid = next(e for e, m in again._handoff_members(pid).items() if m[1] == "cx")
        for p in _relay_writer_pids(self.home, "cx"):
            os.kill(p, 9)
        with mock.patch.object(again.handoffs, "discover", lambda a, c: None):
            r = again.resolve_handoff(pid, eid)
        self.assertEqual(r["state"], "blocked")
        self.assertIn("can't confirm", r["reason"])


class LifecycleEndTests(Base):
    """R3: completion, Stop managing, console shutdown and restart never strand writers."""

    def running_team(self, **cx_default):
        cl = self.terminal("cl", "Claude", sleep=0.3)
        cx = self.terminal("cx", "Codex", **cx_default)
        pid = self.manage_pair()["team"]
        cl.stop(), cx.stop()
        self.tick_until(lambda: self.host._hosted_alive(pid))
        return pid

    def writer_pids(self):
        return {sid: self.own(sid)["detail"]["relay"]["pid"] for sid in ("cl", "cx")}

    def last_writer(self, sid):
        """The writer of the most recent launch episode, from ownership history (a released
        conversation carries no current writer: R2 scopes process evidence to its episode)."""
        return [h["relay"] for h in self.own(sid)["history"] if h.get("relay")][-1]

    def assert_given_back(self, pid, res):
        from relay.handoff import same_process
        self.assertTrue(res["ok"], res)
        for sid in ("cl", "cx"):
            self.assertEqual(self.own(sid)["state"], "released")
            self.assertNotIn("relay", self.own(sid)["detail"])
            self.assertFalse(same_process(self.last_writer(sid)))  # exit verified
        d = self.bus.get_pair_def(pid)
        for role in ("leader", "follower"):
            self.assertIsNone(self.bus.get_session(d[role]["endpoint"])["state"]["pair"])  # claims let go

    def test_stop_managing_keeps_writers_claimed_and_back_to_terminal_reachable(self):
        pid = self.running_team(finish_after=50)
        self.tick_until(lambda: _ctx(self.home, "cl")["routed"] >= 1)
        r = self.host.release(pid)
        self.assertEqual(len(r["still_with_relay"]), 2)
        self.assertTrue(self.bus.pair_state(pid)["terminal"])
        from relay.handoff import same_process
        self.assertTrue(all(same_process(self.own(s)["detail"]["relay"]) for s in ("cl", "cx")))
        d = self.bus.get_pair_def(pid)
        self.assertEqual(self.bus.get_session(d["follower"]["endpoint"])["state"]["pair"], pid)  # claim kept
        c = self.host.console(pid)
        self.assertIn("back_to_terminal", c["handoff_actions"])
        row = self.cat()["agent-relay · Claude"]
        self.assertEqual(row["handoff_team"], pid)  # the row leads back to the team
        from relay.protocol import unknown_turns
        if not unknown_turns(self.bus.pair_state(pid)):
            self.assert_given_back(pid, self.host.back_to_terminal(pid, ["leader", "follower"]))
            return
        # Stopped mid-turn: stopping isn't an acknowledgement of what that turn
        # did. Writers are stopped (exit verified) but nothing is given back until the human
        # records the abandonment; then Resolve gives both back.
        held = self.host.back_to_terminal(pid, ["leader", "follower"])
        self.assertFalse(held["ok"])
        self.assertTrue(all(x["state"] == "blocked" and x["resume"] is None for x in held["sessions"]), held)
        self.assertTrue(all(not same_process(self.last_writer(s)) for s in ("cl", "cx")))
        self.assertEqual(self.bus.get_session(d["follower"]["endpoint"])["state"]["pair"], pid)  # claim kept
        self.host.abandon_unknown(pid)
        for eid in (d["leader"]["endpoint"], d["follower"]["endpoint"]):
            self.assertEqual(self.host.resolve_handoff(pid, eid)["state"], "released")
        self.assert_given_back(pid, {"ok": True})

    def test_completed_team_leaves_conversations_with_the_relay_until_given_back(self):
        pid = self.running_team(finish_after=2)
        self.wait_until(lambda: self.bus.pair_state(pid)["status"] == "finished", timeout=40)
        self.wait_until(lambda: not self.host._hosted_alive(pid))
        self.assertEqual((self.own("cl")["state"], self.own("cx")["state"]), ("relay_owned", "relay_owned"))
        d = self.bus.get_pair_def(pid)
        self.assertEqual(self.bus.get_session(d["leader"]["endpoint"])["state"]["pair"], pid)
        self.assertIn("back_to_terminal", self.host.console(pid)["handoff_actions"])
        res = self.host.back_to_terminal(pid, ["follower"])
        self.assertEqual(res["sessions"][0]["state"], "released")
        self.assertEqual(self.bus.get_session(d["leader"]["endpoint"])["state"]["pair"], pid)  # still has cx
        self.assert_given_back(pid, self.host.back_to_terminal(pid, ["leader"]))

    def test_console_shutdown_gives_writers_back_and_restart_resumes_nothing(self):
        pid = self.running_team(finish_after=50)
        self.tick_until(lambda: _ctx(self.home, "cl")["routed"] >= 1)
        pids = self.writer_pids()
        self.host.shutdown()
        from relay.handoff import same_process
        for sid in ("cl", "cx"):
            self.assertEqual(self.own(sid)["state"], "released")
            self.assertFalse(same_process(self.last_writer(sid)))
            self.assertEqual(self.last_writer(sid)["pid"], pids[sid])
        st = self.bus.pair_state(pid)
        self.assertTrue(st["paused"])
        self.assertFalse(st["terminal"])
        again = self.new_host()
        self.addCleanup(again.shutdown)
        again.handoff_tick()
        self.assertFalse(again._hosted_alive(pid))  # nothing restarts on its own
        self.assertIn("hand_over_again", again.console(pid)["handoff_actions"])
        again.hand_over_again(pid)
        self.tick_until(lambda: again._hosted_alive(pid) and not self.bus.pair_state(pid)["paused"], host=again)

    def test_restart_after_stop_managing_blocks_then_resolves_once_writers_exit(self):
        pid = self.running_team(finish_after=50)
        self.host.release(pid)
        old = dict(self.host.handoffs.writers)
        again = self.new_host()
        self.addCleanup(again.shutdown)
        again.handoff_tick()
        self.assertEqual((self.own("cl", again)["state"], self.own("cx", again)["state"]), ("blocked", "blocked"))
        c = again.console(pid)
        self.assertIn("resolve_handoff", c["handoff_actions"])
        eid = next(e for e, m in again._handoff_members(pid).items() if m[1] == "cl")
        self.assertEqual(again.resolve_handoff(pid, eid)["state"], "blocked")  # the old writer still runs
        for conn in old.values():
            conn.close()
        self.host.handoffs.writers.clear()
        for e in again._handoff_members(pid):
            self.assertEqual(again.resolve_handoff(pid, e)["state"], "released")
        d = self.bus.get_pair_def(pid)
        self.assertIsNone(self.bus.get_session(d["follower"]["endpoint"])["state"]["pair"])


class RepeatedLaunchTests(Base):
    """R2 (repeat): evidence from an earlier launch never proves a later launch absent."""

    def second_cycle_crash(self, point):
        cl = self.terminal("cl", "Claude", sleep=0.2)
        cx = self.terminal("cx", "Codex", finish_after=50)
        pid = self.manage_pair()["team"]
        cl.stop(), cx.stop()
        self.tick_until(lambda: _ctx(self.home, "cl")["routed"] >= 1)
        res = self.host.back_to_terminal(pid, ["follower"])
        self.assertEqual(res["sessions"][0]["state"], "released")
        old = self.own("cl")["history"][-2]["relay"]  # the first episode's (now dead) writer

        class Crash(Exception):
            pass

        def fault(p):
            if p == point:
                raise Crash(p)
        # This test drives every tick itself: stop the console's background ticker first, or it
        # can run the whole second launch before the fault is installed (seen: relay_owned at
        # gen 8 with no crash, about 1 run in 25).
        self.host._ticker_stop.set()
        if self.host._ticker:
            self.host._ticker.join(5)
        self.host.handoffs._fault = fault
        with mock.patch.object(self.host, "_kick", lambda: None):
            self.host.hand_over_again(pid)
            with self.assertRaises(Crash):
                self.tick_until(lambda: False, timeout=10)
        self.host.handoffs.writers.pop(self.host.handoffs.key("fake", "cl"), None)
        self.host.handoffs.starting.clear()
        again = self.new_host()
        self.addCleanup(again.shutdown)
        again.handoffs.discover = lambda a, c: None  # discovery unavailable after restart
        return pid, again, old

    def assert_held(self, pid, again, eid, why):
        st = self.own("cl", again)
        self.assertEqual(st["state"], "blocked")
        self.assertEqual(self.bus.get_session(eid)["state"]["pair"], pid)  # claim kept
        w = next(x for x in again.console(pid)["writers"] if x["endpoint"] == eid)
        self.assertNotIn("resume", w)  # no resume command while the new writer may live
        self.assertIn(why, st["reason"] if why != "unknown" else "unknown")

    def test_second_cycle_crash_before_pid_record_with_discovery_unavailable(self):
        pid, again, old = self.second_cycle_crash("launched")
        from relay.handoff import same_process
        self.assertFalse(same_process(old))  # the old writer is dead ...
        st = self.own("cl", again)
        self.assertTrue(st["spawn"]["intent"])
        self.assertIsNone(st["spawn"]["relay"])  # ... and the new one was never recorded
        self.assertNotIn("relay", st["detail"])  # old evidence isn't carried into this episode
        self.wait_until(lambda: _relay_writer_pids(self.home, "cl"))  # the unrecorded writer is running
        new_live = _relay_writer_pids(self.home, "cl")
        key = again.handoffs.key("fake", "cl")
        self.assertEqual(again.handoffs.relay_writer_status(key), "unknown")
        again.handoff_tick()
        eid = next(e for e, m in again._handoff_members(pid).items() if m[1] == "cl")
        r = again.resolve_handoff(pid, eid)
        self.assertEqual(r["state"], "blocked")
        self.assertIn("can't confirm", r["reason"])
        ret = again.back_to_terminal(pid, ["follower"])  # the Stop-writer path, too
        self.assertEqual(ret["sessions"][0]["state"], "blocked")
        self.assertIsNone(ret["sessions"][0]["resume"])
        self.assert_held(pid, again, eid, "unknown")
        # Even after the new writer exits, nothing from THIS episode proves it while
        # discovery stays unavailable; once discovery works again, recovery proceeds.
        for p in new_live:
            os.kill(p, 9)
        self.wait_until(lambda: not _relay_writer_pids(self.home, "cl"))
        self.assertEqual(again.resolve_handoff(pid, eid)["state"], "blocked")
        again.handoffs.discover = again._discover_one
        self.assertEqual(again.resolve_handoff(pid, eid)["state"], "released")

    def test_second_cycle_crash_after_pid_record_recovers_from_current_evidence(self):
        pid, again, old = self.second_cycle_crash("recorded")
        st = self.own("cl", again)
        new = st["spawn"]["relay"]
        self.assertNotEqual(new["pid"], old["pid"])
        key = again.handoffs.key("fake", "cl")
        self.assertEqual(again.handoffs.relay_writer_status(key), "running")  # the new pid is alive
        again.handoff_tick()
        eid = next(e for e, m in again._handoff_members(pid).items() if m[1] == "cl")
        self.assertEqual(again.resolve_handoff(pid, eid)["state"], "blocked")
        os.kill(new["pid"], 9)
        self.wait_until(lambda: not _relay_writer_pids(self.home, "cl"))
        # Discovery still unavailable: this episode's own recorded process proves the exit.
        self.assertEqual(again.handoffs.relay_writer_status(key), "gone")
        self.assertEqual(again.resolve_handoff(pid, eid)["state"], "released")

    def test_old_episode_pid_does_not_count_as_current_evidence(self):
        own = Ownership(self.bus)
        own.move("k9", expect_gen=0, state="awaiting_exit", adapter="fake", conversation="none")
        own.move("k9", expect_gen=1, state="relay_starting")
        own.note("k9", "spawn_intent")
        own.note("k9", "spawned", relay={"pid": 999999, "started_ms": 1})
        own.move("k9", expect_gen=2, state="relay_owned", token="t1", relay={"pid": 999999, "started_ms": 1})
        own.move("k9", expect_gen=3, state="returning")
        own.move("k9", expect_gen=4, state="released")
        own.move("k9", expect_gen=5, state="awaiting_exit")
        own.move("k9", expect_gen=6, state="relay_starting")
        own.note("k9", "spawn_intent")
        st = own.get("k9")
        self.assertNotIn("relay", st["detail"])
        self.assertEqual(st["spawn"], {"intent": True, "relay": None})
        h = Handoffs(self.bus, {"home": self.home}, lambda a, c: None)
        self.assertEqual(h.relay_writer_status("k9"), "unknown")



class ConcurrentConsoleTests(Base):
    def race(self, key, winner):
        """B decides from a stale view; `winner` moves the record just before B's move runs."""
        real_move, fired = Ownership.move, []

        def move(own, k, **kw):
            if not fired:
                fired.append(1)
                winner()
            return real_move(own, k, **kw)
        return mock.patch.object(Ownership, "move", move)

    def test_two_consoles_blocking_the_same_orphan_both_succeed(self):
        """Found by a saved full-suite log: two consoles noticed the same orphaned start;
        the slower one's conditional block was refused and raised out of its tick."""
        from relay.storage import Conflict
        own = Ownership(self.bus)
        own.move("k5", expect_gen=0, state="awaiting_exit", adapter="fake", conversation="none")
        own.move("k5", expect_gen=1, state="relay_starting")
        a = Handoffs(self.bus, {"home": self.home}, lambda x, y: None)
        b = Handoffs(self.bus, {"home": self.home}, lambda x, y: None)
        with self.race("k5", lambda: Ownership.move(own, "k5", expect_gen=2, state="blocked",
                                                     reason="console A saw it first")):
            st = b.block("k5", "console B too")  # its move loses to A's: not an error
        self.assertEqual(st["state"], "blocked")
        self.assertEqual(own.get("k5")["reason"], "console A saw it first")
        # A different winning state is still a real conflict.
        own.move("k6", expect_gen=0, state="awaiting_exit", adapter="fake", conversation="none")
        own.move("k6", expect_gen=1, state="relay_starting")
        own.move("k6", expect_gen=2, state="relay_owned", token="t")
        with self.race("k6", lambda: Ownership.move(own, "k6", expect_gen=3, state="returning")), \
                self.assertRaises(Conflict):
            b.block("k6", "stale view of a state that moved on")
