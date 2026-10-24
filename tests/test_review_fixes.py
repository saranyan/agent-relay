"""Regressions for defects found in an early review (R1-R7 and workflow gaps).

Each test asserts the corrected behaviour of a defect reproduced by
the original reproduction script.
"""

import json
import os
import queue
import signal
import subprocess
import sys
import time
from types import SimpleNamespace
from unittest import mock

from util import REPO, RelayTest, env_for, relay, relay_json

from relay.adapters.base import LifecycleEvent, NotSubmitted
from relay.adapters.codex import CodexAppServerAdapter, CodexConnection, RpcError
from relay.bus import Bus
from relay.controller import Controller, answer, reconcile
from relay.storage import Blocked, Conflict, InvalidInput, Root, actor

HUMAN = actor("human")


class StubConn:
    """In-memory recipient (as in the review probes)."""

    mode = "attach"
    connection_id = "c_stub"

    def __init__(self, status="idle"):
        self.calls, self.polls, self.events, self._status = 0, 0, [], status
        self.client_session_id = "stub"

    def status(self):
        return self._status

    def poll(self, timeout):
        self.polls += 1
        ev, self.events = self.events, []
        return ev

    def submit(self, text, request_id, **kw):
        self.calls += 1
        return {"client_turn_id": "ct_test"}

    def lookup(self, request_id):
        return None

    def detach(self):
        pass


class ReviewBase(RelayTest):
    def stub_pair(self, **kw):
        a, _ = self.bus.register_session(adapter="fake", client_session_id="leader", mode="attach",
                                         participant="codex", cwd=None, options={"home": self.home})
        z, _ = self.bus.register_session(adapter="fake", client_session_id="follower",
                                         mode="attach", participant="claude", cwd=None,
                                         options={"home": self.home})
        pid = self.bus.create_pair(leader=a["id"], follower=z["id"], scope="test", **kw)["id"]
        return pid, a["id"], z["id"]

    def stub_controller(self, pid, eids, status="idle"):
        c = Controller(self.bus, pid, poll_interval=0.01)
        c.acquire()
        self.controllers.append(c)
        stubs = {}
        for e in eids:
            stubs[e] = StubConn(status)
            c.connections[e] = stubs[e]
        return c, stubs


# ------------------------------------------------------------------ R1 pause
PAUSER = r"""
import sys; sys.path.insert(0, sys.argv[1])
from relay.storage import Root
from relay.bus import Bus
Bus(Root(sys.argv[2])).pause("race")
"""
CLAIM_ONE = r"""
import sys; sys.path.insert(0, sys.argv[1])
from relay.storage import Root, RelayError
from relay.bus import Bus
try:
    Bus(Root(sys.argv[2])).claim(sys.argv[3], "claude"); print("CLAIMED")
except RelayError as e:
    print("REFUSED", type(e).__name__)
"""


class GlobalPauseTests(ReviewBase):
    def test_new_pair_created_during_global_pause_dispatches_nothing(self):
        self.bus.pause("human pause before pair creation")
        pid, a, z = self.stub_pair()
        self.bus.start_pair(pid, body="test")
        c, stubs = self.stub_controller(pid, [a, z])
        for _ in range(3):
            c.step()
        self.assertEqual(stubs[z].calls, 0)
        self.assertEqual(self.bus.pair_state(pid)["status"], "paused")
        self.assertEqual(self.bus.pair_state(pid)["used"], 0)
        self.bus.resume()
        c.step()
        self.assertEqual(stubs[z].calls, 1)

    def test_pair_resume_cannot_bypass_global_pause(self):
        pid, a, z = self.stub_pair()
        self.bus.start_pair(pid, body="test")
        self.bus.pair_event(pid, "paused", {"reason": "pair"}, HUMAN)
        self.bus.pause("global")
        self.bus.pair_event(pid, "resumed", {}, HUMAN)  # individual resume only
        c, stubs = self.stub_controller(pid, [a, z])
        c.step()
        self.assertEqual(stubs[z].calls, 0)
        st = self.bus.pair_state(pid)
        self.assertTrue(st["global_paused"])
        self.assertFalse(st["paused"])
        with self.assertRaises(Blocked):
            self.bus.authorize_dispatch(pid, {"ctl": c.ctl, "turn": "u_x",
                                              "message": st["queued"]["message"], "to": "claude",
                                              "role": "follower", "request_id": "r_x"}, HUMAN)
        self.bus.resume()  # global resume does not lift an individual pause either
        self.bus.pair_event(pid, "paused", {"reason": "pair again"}, HUMAN)
        c.step()
        self.assertEqual(stubs[z].calls, 0)

    def test_pause_races_claims_from_processes(self):
        for _ in range(3):
            msgs = [self.bus.send(sender="codex", to="claude", kind="task", subject=f"s{i}",
                                  body="b")[0]["id"] for i in range(6)]
            procs = [subprocess.Popen([sys.executable, "-c", CLAIM_ONE, REPO, self.root.path, m],
                                      stdout=subprocess.PIPE, text=True) for m in msgs[:3]]
            procs.append(subprocess.Popen([sys.executable, "-c", PAUSER, REPO, self.root.path]))
            procs += [subprocess.Popen([sys.executable, "-c", CLAIM_ONE, REPO, self.root.path, m],
                                       stdout=subprocess.PIPE, text=True) for m in msgs[3:]]
            for p in procs:
                p.communicate(timeout=60)
            events = self.events("control")
            pause_seq = max(e.seq for e in events if e.type == "paused")
            self.assertFalse([e for e in events if e.type == "gate" and e.seq > pause_seq])
            for m in msgs:  # every recorded claim holds a gate committed before the pause
                for h in self.root.read_stream(f"msg-{m}"):
                    if h.type == "claimed":
                        self.assertLess(h.data["gate_seq"], pause_seq)
            self.bus.resume()

    def test_pause_races_dispatch_from_processes(self):
        for _ in range(4):
            pid, a, z = self.stub_pair()
            self.bus.start_pair(pid, body="go")
            c, stubs = self.stub_controller(pid, [a, z])
            p = subprocess.Popen([sys.executable, "-c", PAUSER, REPO, self.root.path])
            for _ in range(3):
                c.step()
            p.wait(timeout=30)
            events = self.events("control")
            pause_seq = max(e.seq for e in events if e.type == "paused")
            pair_events = self.events(f"pair-{pid}")
            for e in pair_events:
                if e.type == "dispatch_authorized":
                    self.assertLess(e.data["gate_seq"], pause_seq)
            self.assertLessEqual(stubs[z].calls, 1)
            c.shutdown()
            self.bus.resume()
            for e in (a, z):
                self.bus.session_event(e, "released", {"pair": pid}, HUMAN)
            self.bus.pair_event(pid, "stopped", {"reason": "next round"}, HUMAN)
            os.unlink(self.root.p("identities", Bus.identity_key("fake", {"home": self.home}, "leader") + ".json"))
            os.unlink(self.root.p("identities", Bus.identity_key("fake", {"home": self.home}, "follower") + ".json"))


# ------------------------------------------------------------- R2 identity
REGISTER = r"""
import sys, json; sys.path.insert(0, sys.argv[1])
from relay.storage import Root, RelayError
from relay.bus import Bus
b = Bus(Root(sys.argv[2]))
try:
    s, created = b.register_session(adapter="fake", client_session_id="same-physical",
                                    mode="attach", participant="codex", cwd=None,
                                    options={"home": sys.argv[3]})
    print(json.dumps({"id": s["id"], "created": created}))
except RelayError as e:
    print(json.dumps({"error": type(e).__name__}))
"""
PAIR = r"""
import sys, json; sys.path.insert(0, sys.argv[1])
from relay.storage import Root, RelayError
from relay.bus import Bus
try:
    print(Bus(Root(sys.argv[2])).create_pair(leader=sys.argv[3], follower=sys.argv[4], scope="t")["id"])
except RelayError as e:
    print("REFUSED", type(e).__name__)
"""


class IdentityTests(ReviewBase):
    def test_registration_race_then_competing_pairs(self):
        procs = [subprocess.Popen([sys.executable, "-c", REGISTER, REPO, self.root.path, self.home],
                                  stdout=subprocess.PIPE, text=True) for _ in range(8)]
        res = [json.loads(p.communicate(timeout=60)[0]) for p in procs]
        self.assertEqual(len({r["id"] for r in res}), 1, res)
        self.assertEqual(sum(r["created"] for r in res), 1)
        eid = res[0]["id"]
        f1 = self.launch_endpoint("claude")
        f2 = self.launch_endpoint("claude")
        procs = [subprocess.Popen([sys.executable, "-c", PAIR, REPO, self.root.path, eid, f],
                                  stdout=subprocess.PIPE, text=True) for f in (f1, f2)]
        outs = [p.communicate(timeout=60)[0].strip() for p in procs]
        self.assertEqual(sum(o.startswith("p_") for o in outs), 1, outs)
        self.assertEqual(len(self.bus.sessions()), 3)

    def test_interrupted_registration_recovers_same_endpoint(self):
        s, _ = self.bus.register_session(adapter="fake", client_session_id="x", mode="attach",
                                         participant="codex", cwd=None, options={"home": self.home})
        os.unlink(self.root.p("sessions", s["id"] + ".json"))  # died after identity, before record
        again, created = self.bus.register_session(adapter="fake", client_session_id="x",
                                                   mode="attach", participant="codex", cwd=None,
                                                   options={"home": self.home})
        self.assertEqual(again["id"], s["id"])
        self.assertFalse(created)

    def test_launched_identity_is_verified_not_placeholder(self):
        leader = self.launch_endpoint("codex", default={"finish_after": 1})
        rec, _ = self.bus.register_session(adapter="fake", client_session_id="pending-abc",
                                           mode="launch", participant="claude", cwd=self.tmp,
                                           options={"home": self.home})
        self.assertIsNone(self.bus.get_session(rec["id"])["verified_client_session_id"])
        pid = self.pair(leader, rec["id"])
        c = self.controller(pid)
        s = self.bus.get_session(rec["id"])
        self.assertEqual(s["verified_client_session_id"], c.connections[rec["id"]].client_session_id)
        self.assertFalse(s["verified_client_session_id"].startswith("pending-"))
        out = relay(self.tmp, "status").stdout
        self.assertIn(s["verified_client_session_id"], out)


# ------------------------------------------------------------- R4 start
class StartCrashTests(ReviewBase):
    def test_crash_before_and_after_start_commit(self):
        pid, a, z = self.stub_pair()
        orig = self.root.append

        def die_on_started(stream, validate, who):
            def v(events):
                d = validate(events)
                if d and d[0] == "started":
                    raise SystemExit("simulated process death before the Start commit")
                return d
            return orig(stream, v, who)

        with mock.patch.object(self.root, "append", die_on_started):
            with self.assertRaises(SystemExit):
                self.bus.start_pair(pid, body="test")
        self.assertFalse(self.bus.pair_state(pid)["started"])
        r1 = self.bus.start_pair(pid, body="test")  # retry: same durable initial message
        r2 = self.bus.start_pair(pid, body="test")  # crash after commit, retried again
        self.assertEqual(r1["initial_message"], r2["initial_message"])
        self.assertTrue(r2["already_started"])
        st = self.bus.pair_state(pid)
        self.assertEqual(st["queued"]["message"], r1["initial_message"])
        self.assertEqual(len(self.bus.all_messages()[0]), 1)
        c, stubs = self.stub_controller(pid, [a, z])
        for _ in range(3):
            c.step()
        self.assertEqual(stubs[z].calls, 1)  # delivered exactly once
        with self.assertRaises(Conflict):
            self.bus.start_pair(pid, body="different")


# ----------------------------------------------------------- R5 late
class LateResponseTests(ReviewBase):
    def test_production_loop_polls_after_timeout(self):
        pid, a, z = self.stub_pair()
        self.bus.start_pair(pid, body="test")
        c, stubs = self.stub_controller(pid, [a, z])
        c.step()
        t = self.bus.pair_state(pid)["inflight"]
        c.ev("turn_timed_out", turn=t["turn"])
        stubs[z].events = [LifecycleEvent("turn_completed", t["request_id"], "ct_test",
                                          'late\n```relay\n{"relay": 1, "turn_id": "%s", '
                                          '"action": "continue"}\n```' % t["turn"])]
        for _ in range(3):
            c.step()
        st = self.bus.pair_state(pid)
        self.assertEqual(len(st["late"]), 1)
        self.assertIsNone(st["queued"])  # recorded, not routed

    def test_late_response_through_real_loop_and_cli_foreground(self):
        lf = self.fake("codex-live", default={"finish_after": 1})
        leader = self.attach_endpoint("codex-live", "codex")
        follower = self.launch_endpoint("claude", script=[{"sleep": 6.5}])
        pid = self.pair(leader, follower, turn_timeout_s=5)
        self.bus.start_pair(pid, body="go")
        proc = subprocess.Popen([sys.executable, "-m", "relay", "pair", "run", pid,
                                 "--max-seconds", "60"], cwd=REPO, env=env_for(self.tmp),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.wait_until(lambda: self.bus.pair_state(pid)["late"], timeout=30)
            st = self.bus.pair_state(pid)
            self.assertEqual(st["status"], "waiting_human")
            self.assertIsNone(proc.poll())  # still in the foreground; launched child alive
            late = st["late"][0]["message"]
            relay_json(self.tmp, "pair", "reconcile", pid, "--outcome", "route-late", "--message",
                       late, "--reason", "reviewed late result")
            out, err = proc.communicate(timeout=60)
        finally:
            if proc.poll() is None:
                proc.kill()
        self.assertEqual(proc.returncode, 0, err)
        self.assertEqual(self.bus.pair_state(pid)["status"], "finished")
        self.assertIn("closed console-launched session", err)


# ------------------------------------------------ workflow: human answers
class HumanAnswerTests(ReviewBase):
    def test_question_answer_resumes_same_launched_pair_via_cli(self):
        leader = self.launch_endpoint("codex", default={"finish_after": 1})
        follower = self.launch_endpoint("claude", script=[{"action": "ask_human",
                                                           "text": "Which directory may I edit?"}])
        pid = self.pair(leader, follower)
        self.bus.start_pair(pid, body="go")
        proc = subprocess.Popen([sys.executable, "-m", "relay", "pair", "run", pid,
                                 "--max-seconds", "60"], cwd=REPO, env=env_for(self.tmp),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.wait_until(lambda: self.bus.pair_state(pid)["status"] == "waiting_human", 30)
            fsess = self.bus.get_session(follower)["verified_client_session_id"]
            body = os.path.join(self.tmp, "answer.txt")
            with open(body, "w") as f:
                f.write("Only src/payments_demo/. Continue.")
            _, r = relay_json(self.tmp, "pair", "answer", pid, "--body-file", body)
            self.assertEqual(r["to"], "follower")
            out, err = proc.communicate(timeout=60)
        finally:
            if proc.poll() is None:
                proc.kill()
        self.assertEqual(proc.returncode, 0, err)
        st = self.bus.pair_state(pid)
        self.assertEqual(st["status"], "finished")
        self.assertEqual(st["used"], 3)  # question turn + answer turn + leader review
        froms = [m["from"] for m in self.bus.thread(st["definition"]["thread_id"])["messages"]]
        self.assertEqual(froms, ["codex", "claude", "human", "claude", "codex"])
        with open(os.path.join(self.home, "sessions", fsess, "context.json")) as f:
            ctx = json.load(f)
        self.assertEqual(ctx["routed"], 2)  # same launched session answered both turns
        self.assertIn("recorded the text", ctx["turns"][1]["input"])

    def test_answer_requires_a_waiting_pair(self):
        pid, a, z = self.stub_pair()
        self.bus.start_pair(pid, body="go")
        with self.assertRaises(Conflict):
            answer(self.bus, pid, "hello")


# ------------------------------------------------ R6 malformed isolation
class MalformedIsolationTests(ReviewBase):
    def test_incomplete_and_wrong_type_records_are_isolated(self):
        good = self.bus.send(sender="codex", to="claude", kind="task", subject="good", body="g")[0]
        bad = {"m_partial": dict(good, id="m_partial"), "m_wrongtype": dict(good, id="m_wrongtype",
                                                                            subject=42),
               "m_badtime": dict(good, id="m_badtime", created_at=12345)}
        bad["m_partial"].pop("pair_id")
        for mid, env in bad.items():
            self.root.publish(("messages", f"{mid}.json"), json.dumps(env).encode())
        inbox = self.bus.inbox("claude")
        self.assertEqual([m["id"] for m in inbox["messages"]], [good["id"]])
        self.assertEqual({e["message"] for e in inbox["errors"]}, set(bad))
        for mid in bad:
            self.assertEqual(relay(self.tmp, "read", mid).returncode, 6)
        _, st = relay_json(self.tmp, "status")
        self.assertEqual(st["messages"], {"unclaimed": 1})
        self.assertEqual(relay(self.tmp, "thread", good["thread_id"]).returncode, 0)

    def test_malformed_session_pair_and_event_payload(self):
        pid, a, z = self.stub_pair()
        other_pid = self.pair(self.launch_endpoint("codex"), self.launch_endpoint("claude"))
        with open(self.root.p("sessions", "s_broken.json"), "w") as f:
            json.dump({"schema_version": 1, "id": "s_broken"}, f)
        with open(self.root.p("pairs", "p_broken.json"), "w") as f:
            json.dump({"schema_version": 1, "id": "p_broken"}, f)
        # a hash-valid event whose payload lacks required fields
        self.root.append(f"pair-{pid}", lambda ev: ("queued", {"nonsense": True}), HUMAN)
        _, st = relay_json(self.tmp, "status")
        by_pair = {p["pair"]: p for p in st["pairs"]}
        self.assertEqual(by_pair[pid]["status"], "untrusted")
        self.assertEqual(by_pair["p_broken"]["status"], "untrusted")
        self.assertEqual(by_pair[other_pid]["status"], "idle")  # independent work unaffected
        self.assertTrue(any(s["id"] == "s_broken" and s["state"]["state"] == "invalid"
                            for s in st["sessions"]))
        r = relay(self.tmp, "pair", "run", pid)
        self.assertEqual(r.returncode, 8)


# ---------------------------------------------- R3 / R7 Codex wire level
class FakeRpc:
    def __init__(self, responses=None):
        self.q = queue.Queue()
        self.proc = SimpleNamespace(poll=lambda: None)
        self.sent = []
        self.responses = responses or {}

    def request(self, method, params, timeout=30.0):
        self.sent.append((method, params))
        r = self.responses.get(method)
        if isinstance(r, Exception):
            raise r
        if callable(r):
            return r(params)
        return r if r is not None else {}

    def push(self, msg):
        self.q.put(msg)

    def close(self):
        pass


def note(method, **params):
    return {"jsonrpc": "2.0", "method": method, "params": dict(threadId="th1", **params)}


class CodexWireTests(ReviewBase):
    def conn(self, status={"type": "idle"}, **responses):
        rpc = FakeRpc({"turn/start": {"turn": {"id": "turn-routed", "status": "inProgress"}},
                       **responses})
        return rpc, CodexConnection(rpc, "th1", "attach", status)

    def test_external_activity_is_busy_and_submission_refused(self):
        rpc, c = self.conn()
        self.assertEqual(c.status(), "idle")
        rpc.push(note("thread/status/changed", status={"type": "active", "activeFlags": []}))
        self.assertEqual(c.status(), "busy")
        with self.assertRaises(NotSubmitted):
            c.submit("x", "r_1")
        self.assertFalse([m for m, _ in rpc.sent if m == "turn/start"])
        rpc.push(note("thread/status/changed", status={"type": "idle"}))
        self.assertEqual(c.status(), "idle")
        rpc.push(note("turn/started", turn={"id": "external-turn"}))
        self.assertEqual(c.status(), "busy")  # manual turn started in the TUI
        rpc.push(note("turn/completed", turn={"id": "external-turn", "status": "completed"}))
        self.assertEqual(c.status(), "idle")

    def test_initial_status_and_unknown_fail_closed(self):
        _, c = self.conn({"type": "active", "activeFlags": ["waitingOnApproval"]})
        self.assertEqual(c.status(), "waiting")
        _, c = self.conn({"type": "systemError"})
        self.assertEqual(c.status(), "unknown")
        _, c = self.conn(None)
        self.assertEqual(c.status(), "unknown")

    def test_submit_correlation_rpc_refusal_and_completion(self):
        rpc, c = self.conn()
        r = c.submit("hello", "r_1")
        self.assertEqual(r["client_turn_id"], "turn-routed")
        method, params = rpc.sent[-1]
        self.assertEqual(params["clientUserMessageId"], "r_1")
        self.assertEqual(c.status(), "busy")
        rpc.push(note("turn/started", turn={"id": "turn-routed"}))
        rpc.push(note("item/completed", turnId="turn-routed",
                      item={"type": "agentMessage", "phase": "commentary", "text": "thinking"}))
        rpc.push(note("item/completed", turnId="turn-routed",
                      item={"type": "agentMessage", "phase": "final_answer", "text": "FINAL"}))
        rpc.push(note("turn/completed", turn={"id": "turn-routed", "status": "completed"}))
        evs = c.poll(0.2)
        done = [e for e in evs if e.type == "turn_completed"]
        self.assertEqual([(e.request_id, e.text) for e in done], [("r_1", "FINAL")])
        self.assertEqual(c.status(), "idle")
        rpc2, c2 = self.conn(**{"turn/start": RpcError("turn/start: thread busy")})
        with self.assertRaises(NotSubmitted):
            c2.submit("x", "r_2")

    def test_interrupt_targets_the_active_turn(self):
        rpc, c = self.conn()
        c.submit("a", "r_1")
        rpc.push(note("turn/completed", turn={"id": "turn-routed", "status": "completed"}))
        c.poll(0.1)
        rpc.push(note("turn/started", turn={"id": "turn-later"}))
        c.status()
        c.interrupt()
        self.assertEqual(rpc.sent[-1], ("turn/interrupt", {"threadId": "th1", "turnId": "turn-later"}))

    def test_approval_resolution_correlates_by_request_id(self):
        rpc, c = self.conn()
        c.submit("a", "r_1")
        rpc.push({"jsonrpc": "2.0", "id": 123, "method": "item/commandExecution/requestApproval",
                  "params": {"threadId": "th1", "turnId": "turn-routed"}})
        rpc.push({"jsonrpc": "2.0", "id": 124, "method": "item/fileChange/requestApproval",
                  "params": {"threadId": "th1", "turnId": "turn-routed"}})
        rpc.push(note("serverRequest/resolved", requestId=123))
        evs = c.poll(0.2)
        self.assertEqual([e.type for e in evs], ["approval_pending", "approval_pending"])
        self.assertTrue(all(e.request_id == "r_1" for e in evs))
        rpc.push(note("serverRequest/resolved", requestId=124))
        evs = c.poll(0.2)
        self.assertEqual([(e.type, e.request_id, e.client_turn_id) for e in evs],
                         [("approval_resolved", "r_1", "turn-routed")])
        rpc.push(note("serverRequest/resolved", requestId=999))  # unknown request: ignored
        self.assertEqual(c.poll(0.1), [])

    def test_attach_requires_live_loaded_thread(self):
        ad = CodexAppServerAdapter({})
        rpc = FakeRpc({"thread/loaded/list": {"data": ["other"]}})
        with mock.patch.object(ad, "_daemon", return_value=rpc):
            with self.assertRaises(NotSubmitted):
                ad.attach("th1")
        self.assertNotIn("thread/resume", [m for m, _ in rpc.sent])
        rpc = FakeRpc({"thread/loaded/list": {"data": ["th1"]},
                       "thread/resume": {"thread": {"id": "th1", "status": {"type": "active",
                                                                              "activeFlags": []}},
                                         "cwd": "/w"}})
        with mock.patch.object(ad, "_daemon", return_value=rpc):
            c = ad.attach("th1")
        self.assertEqual(c.status(), "busy")
        self.assertEqual(c.cwd, "/w")

    def test_controller_does_not_submit_when_manual_turn_starts(self):
        pid, a, z = self.stub_pair()
        self.bus.start_pair(pid, body="go")
        c = Controller(self.bus, pid, poll_interval=0.01)
        c.acquire()
        self.controllers.append(c)
        rpc, conn = self.conn()
        c.connections[z] = conn
        c.connections[a] = StubConn()
        rpc.push(note("turn/started", turn={"id": "manual"}))  # user typed into Codex
        for _ in range(3):
            c.step()
        self.assertFalse([m for m, _ in rpc.sent if m == "turn/start"])
        self.assertEqual(self.bus.pair_state(pid)["used"], 0)
        rpc.push(note("turn/completed", turn={"id": "manual", "status": "completed"}))
        c.step()
        self.assertEqual([m for m, _ in rpc.sent if m == "turn/start"], ["turn/start"])


# ------------------------------------ recheck: client-confirmed identity (Claude)
class FakeStdin:
    def __init__(self, on_write):
        self.on_write, self.lines = on_write, []

    def write(self, data):
        line = json.loads(data.decode())
        self.lines.append(line)
        self.on_write(line)

    def flush(self):
        pass

    def close(self):
        pass


class FakeProc:
    def __init__(self):
        self.stdout = iter(())
        self.alive = True

    def poll(self):
        return None if self.alive else 0

    def wait(self, timeout=None):
        return 0


def claude_stream(requested="requested-session", replies=None):
    """StreamConnection over in-memory streams. `replies(line)` returns the messages
    the fake client emits after reading one prompt line."""
    from relay.adapters.claude import StreamConnection
    proc = FakeProc()
    conn = StreamConnection(proc, requested, start_reader=False)

    def on_write(line):
        for m in (replies(line) if replies else []):
            conn.q.put(m)

    proc.stdin = FakeStdin(on_write)
    return conn


def echo(line, sid="requested-session"):
    return dict(type="user", uuid=line["uuid"], session_id=sid, message=line["message"])


def result(sid="requested-session", text="done"):
    return {"type": "result", "subtype": "success", "is_error": False, "session_id": sid,
            "result": text}


class ClaudeIdentityWireTests(ReviewBase):
    def test_requested_id_is_not_verified_before_the_client_reports(self):
        rec, _ = self.bus.register_session(adapter="claude-stream", client_session_id="requested-session",
                                           mode="launch", participant="claude", cwd=self.tmp)
        s = self.bus.get_session(rec["id"])
        self.assertIsNone(s["verified_client_session_id"])
        self.assertIsNone(s["verified_cwd"])
        c = claude_stream()
        self.assertIsNone(c.confirmed_identity)

    def test_matching_init_confirms_identity_and_workspace(self):
        c = claude_stream(replies=lambda l: [
            {"type": "system", "subtype": "init", "session_id": "requested-session", "cwd": "/ws"},
            echo(l), {"type": "assistant", "session_id": "requested-session"}, result()])
        r = c.submit("hi", "r_1", accept_timeout=2)
        self.assertEqual(r["client_turn_id"], c.proc.stdin.lines[0]["uuid"])
        self.assertEqual(c.confirmed_identity, {"client_session_id": "requested-session", "cwd": "/ws"})
        evs = c.poll(0.1)
        self.assertEqual([e.type for e in evs], ["accepted", "delta", "turn_completed"])
        self.assertEqual(evs[-1].request_id, "r_1")
        self.assertEqual(c.status(), "idle")

    def test_mismatched_init_fails_closed_and_result_is_not_routed(self):
        c = claude_stream(replies=lambda l: [
            {"type": "system", "subtype": "init", "session_id": "different-session"},
            echo(l, "different-session"), result("different-session")])
        with self.assertRaises(Exception):  # acceptance is not trusted from the wrong session
            c.submit("hi", "r_1", accept_timeout=1)
        evs = c.poll(0.1)
        self.assertNotIn("turn_completed", [e.type for e in evs])
        self.assertTrue(any(e.type == "error" and e.data.get("identity_mismatch") for e in evs))
        self.assertIsNone(c.confirmed_identity)
        self.assertEqual(c.status(), "unknown")
        with self.assertRaises(NotSubmitted):
            c.submit("again", "r_2", accept_timeout=1)

    def test_mismatched_result_after_matching_init_is_rejected(self):
        c = claude_stream(replies=lambda l: [
            {"type": "system", "subtype": "init", "session_id": "requested-session"},
            echo(l), result("different-session")])
        c.submit("hi", "r_1", accept_timeout=2)
        evs = c.poll(0.1)
        self.assertEqual([e.type for e in evs], ["accepted", "error"])
        self.assertIn("different-session", evs[-1].text)

    def test_absent_init_confirms_from_matching_result_only(self):
        c = claude_stream(replies=lambda l: [echo(l), result()])
        c.submit("hi", "r_1", accept_timeout=2)
        self.assertEqual(c.confirmed_identity["client_session_id"], "requested-session")
        self.assertEqual([e.type for e in c.poll(0.1)], ["accepted", "turn_completed"])
        # no session id anywhere: nothing confirms it, so the result is not routed
        c2 = claude_stream(requested=None, replies=lambda l: [
            dict(type="user", uuid=l["uuid"], message=l["message"]),
            {"type": "result", "subtype": "success", "is_error": False, "result": "x"}])
        c2.submit("hi", "r_1", accept_timeout=2)
        evs = c2.poll(0.1)
        self.assertEqual([e.type for e in evs], ["accepted", "error"])
        self.assertIsNone(c2.confirmed_identity)

    def test_launch_without_requested_id_takes_the_reported_one(self):
        c = claude_stream(requested=None, replies=lambda l: [
            {"type": "system", "subtype": "init", "session_id": "client-chosen", "cwd": "/w"},
            echo(l, "client-chosen"), result("client-chosen")])
        c.submit("hi", "r_1", accept_timeout=2)
        self.assertEqual(c.confirmed_identity, {"client_session_id": "client-chosen", "cwd": "/w"})
        self.assertEqual(c.client_session_id, "client-chosen")

    def test_early_eof(self):
        c = claude_stream(replies=lambda l: [{"type": "__eof__"}])
        from relay.adapters.base import DeliveryUncertain
        with self.assertRaises(DeliveryUncertain):
            c.submit("hi", "r_1", accept_timeout=1)
        self.assertEqual([e.type for e in c.poll(0.05)], ["disconnected"])
        self.assertEqual(c.status(), "disconnected")
        self.assertIsNone(c.confirmed_identity)

    def test_ordering_messages_before_acceptance_are_kept_in_order(self):
        c = claude_stream(replies=lambda l: [
            {"type": "assistant", "session_id": "requested-session"},  # arrives before the echo
            echo(l),
            {"type": "system", "subtype": "init", "session_id": "requested-session"}, result()])
        c.submit("hi", "r_1", accept_timeout=2)
        self.assertEqual([e.type for e in c.poll(0.1)], ["delta", "accepted", "turn_completed"])

    def test_controller_records_confirmed_identity_and_stops_on_mismatch(self):
        pid, a, z = self.stub_pair()
        self.bus.start_pair(pid, body="go")
        c = Controller(self.bus, pid, poll_interval=0.01)
        c.acquire()
        self.controllers.append(c)
        c.connections[a] = StubConn()
        conn = claude_stream(requested="follower", replies=lambda l: [
            {"type": "system", "subtype": "init", "session_id": "someone-else"},
            echo(l, "someone-else"), result("someone-else")])
        c.connections[z] = conn
        for _ in range(4):
            c.step()
        st = self.bus.pair_state(pid)
        self.assertIn(st["status"], ("failed", "delivery_uncertain"))
        self.assertIsNone(st["last_message"])  # nothing routed
        self.assertIsNone(self.bus.get_session(z)["verified_client_session_id"])
        self.assertEqual(len(self.bus.all_messages()[0]), 1)

    def test_fake_endpoint_identity_comes_from_the_client_report(self):
        self.fake("codex-live")
        leader = self.attach_endpoint("codex-live", "codex")
        follower = self.launch_endpoint("claude")
        self.assertIsNone(self.bus.get_session(follower)["verified_client_session_id"])
        pid = self.pair(leader, follower)
        c = self.controller(pid)
        for e in (leader, follower):
            s = self.bus.get_session(e)
            self.assertEqual(s["verified_client_session_id"], c.connections[e].client_session_id)
            self.assertEqual(s["verified_cwd"], self.tmp)
