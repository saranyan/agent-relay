"""Supplemental dry run and the fixes it needed,
against fast simulated interactive terminals, driven by the same probe script the plan
uses (scripts/handoff_probe.py).

A. Mid-turn writer failure: staged budget 2 (+2); the probe kills the relay's Claude
   writer while turn 4 is accepted and not ended. Blocked, no resend; the unknown
   outcome must be explicitly abandoned before anything is given back (R3).
B. Crash during an ownership transition: Back to terminal while turn 1 runs; the probe
   kills the CONSOLE (identified by its durable launch record) while ownership is
   `returning`. A restarted console never resends and gives nothing back until each
   writer's exit is verified AND the unknown outcome is acknowledged.
R1. The probe signals only with positive proof of identity.
R2. `relay pair detach` keeps claims while the relay may still hold a conversation,
    and can't race a concurrent Hand over again.
R3. Confirmed writer exit and a turn's outcome are separate facts.

Proves sequencing, verdicts and expected states only; nothing about real clients."""

import importlib.util
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from unittest import mock

from util import RelayTest, relay

from relay.bus import Bus
from relay.context import process_exited, process_start_ms
from relay.controller import abandon_unknown, reconcile
from relay.handoff import CLAIMS_LOCK, Handoffs, same_process
from relay.host import Host
from relay.observer import Discovery
from relay.protocol import ABANDON_STATEMENT
from relay.storage import Conflict, Root, actor, init_root

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("handoff_probe", os.path.join(HERE, "scripts", "handoff_probe.py"))
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)

TIMEOUT_S = 180
FIRST, EXTRA = 2, 2  # `relay init --max-turns 2`, then exactly one `extend --add 2`
PROBE_TURN = 4       # Claude's second relay turn (its first one confirms identity)


def sleeper(seconds=60):
    return subprocess.Popen([sys.executable, "-c", f"import time; time.sleep({seconds})"])


def gone_process():
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    started = probe.process_start_ms(p.pid)
    p.wait()
    return {"pid": p.pid, "started_ms": started or 1_700_000_000_000}


class Base(RelayTest):
    def setUp(self):
        super().setUp()
        self.root = init_root(os.path.join(self.tmp, "run", "root"), ["*"], max_turns=FIRST,
                              turn_timeout_s=TIMEOUT_S)
        self.bus = Bus(self.root)
        self.scratch = os.path.realpath(os.path.join(self.tmp, "run", "scratch"))
        os.makedirs(self.scratch)

    def terminal(self, sid, client, seed, **default):
        default.setdefault("natural", True)
        return self.fake(sid, interactive=True, seed_history=seed, client=client, cwd=self.scratch, default=default)

    def submits(self, pid):
        return [e.data["turn"] for e in self.root.read_stream(f"pair-{pid}") if e.type == "submit_attempted"]

    def cli(self, *args):
        return relay(self.tmp, *args, "--root", self.root.path)


class HostBase(Base):
    """In-process console (no tests here)."""

    def setUp(self):
        super().setUp()
        self.discovery = Discovery({"home": self.home}, ("fake",), interval=60)
        self.host = Host(self.bus, self.discovery, new_session_adapters={"claude": "fake", "codex": "fake"},
                         adapter_options={"home": self.home})

    def tearDown(self):
        self.host.shutdown()
        for c in list(self.host.handoffs.writers.values()):
            try:
                c.close()
            except Exception:
                pass
        super().tearDown()

    def tick_until(self, pred, timeout=30):
        end = time.time() + timeout
        while time.time() < end:
            self.host.handoff_tick()
            if pred():
                return
            time.sleep(0.05)
        raise AssertionError("condition not met in time: " + json.dumps(self.dump(), default=str)[:3000])

    def dump(self):
        return {pid: probe.status(self.bus, pid) for pid in self.bus.pair_ids()}

    def own(self, sid):
        h = self.host.handoffs
        return h.own.get(h.key("fake", sid))

    def team(self, cl_sleep=0.6, cx_sleep=0.1, close=("cl", "cx")):
        cl = self.terminal("cl", "Claude", 1, sleep=cl_sleep)
        cx = self.terminal("cx", "Codex", 1, sleep=cx_sleep, finish_after=1000)
        self.discovery.refresh_once()
        cat = {x["name"]: x for x in self.host.catalog()["sessions"]}
        pid = self.host.manage(cat["scratch · Claude"]["key"], {"existing": cat["scratch · Codex"]["key"]},
                               "Each turn, one short line with your own code word, then the relay block.")["team"]
        for sid, t in (("cl", cl), ("cx", cx)):
            if sid in close:
                t.stop()
        return pid, cl, cx

    def eid(self, pid, sid):
        return next(e for e, m in self.host._handoff_members(pid).items() if m[1] == sid)

    def uncertain_team(self):
        """A team whose follower turn ended uncertain (writer killed mid-turn), then stopped."""
        pid, _cl, _cx = self.team(cl_sleep=3, cx_sleep=0.05)
        self.tick_until(lambda: (self.bus.pair_state(pid).get("inflight") or {}).get("role") == "follower"
                        and self.bus.pair_state(pid)["inflight"]["phase"] == "accepted")
        os.kill(self.own("cl")["spawn"]["relay"]["pid"], signal.SIGKILL)
        self.tick_until(lambda: self.own("cl")["state"] == "blocked")
        st = self.bus.pair_state(pid)
        self.assertEqual(st["turns"][-1]["phase"], "uncertain")
        return pid

    def manage_again(self):
        self.discovery.refresh_once()
        cat = {x["name"]: x for x in self.host.catalog()["sessions"]}
        return self.host.manage(cat["scratch · Claude"]["key"], {"existing": cat["scratch · Codex"]["key"]},
                                "Each turn, one short line with your own code word, then the relay block.")["team"]

    def resume_shown(self):
        return [x["name"] for x in self.host.catalog()["sessions"]
                if (x.get("ownership") or {}).get("resume")]


# ===================================================================== A
class MidTurnWriterFailure(HostBase):
    def test_probe_catches_turn_4_and_unknown_outcome_must_be_abandoned(self):
        pid, _cl, _cx = self.team()
        d = self.bus.get_pair_def(pid)
        self.assertEqual((d["max_turns"], d["turn_timeout_s"]), (FIRST, TIMEOUT_S))
        # The probe's key derivation matches the console's.
        self.assertEqual(probe.members(self.bus, pid)["follower"]["key"], self.host.handoffs.key("fake", "cl"))

        verdict = {}
        t = threading.Thread(target=lambda: verdict.update(
            probe.midturn(Bus(Root(self.root.path)), pid, "follower", PROBE_TURN, 60)))
        t.start()
        self.tick_until(lambda: self.host.attention_kind(self.bus.pair_state(pid)) == "budget")
        self.assertEqual(self.bus.pair_state(pid)["used"], FIRST)  # checkpoint at 2 holds
        self.bus.pair_event(pid, "budget_extended", {"add": EXTRA}, actor("human", "cli"))
        reconcile(self.bus, pid, "continue", reason="checkpoint: probe stage")
        self.tick_until(lambda: not t.is_alive())
        self.assertEqual(verdict["verdict"], "observed", verdict)
        self.tick_until(lambda: self.own("cl")["state"] == "blocked")

        st = self.bus.pair_state(pid)
        turn4 = st["turns"][PROBE_TURN - 1]
        self.assertEqual(verdict["turn"], turn4["turn"])
        self.assertIn(turn4["phase"], ("uncertain", "failed"))  # never "completed"
        before = self.submits(pid)
        time.sleep(0.6)
        self.host.handoff_tick()
        self.assertEqual(self.submits(pid), before)  # no resend
        self.assertEqual(before.count(turn4["turn"]), 1)
        self.assertEqual(self.resume_shown(), [])
        self.assertFalse(same_process(verdict["killed"]))

        # Cleanup. Stop managing is not an acknowledgement: Resolve and Give back stay held.
        self.host.release(pid)
        if turn4["phase"] == "uncertain":
            with self.assertRaises(Conflict) as cm:
                self.host.resolve_handoff(pid, self.eid(pid, "cl"))
            self.assertIn("abandon-unknown", str(cm.exception))
            res = self.host.back_to_terminal(pid, ["leader"])
            self.assertEqual(res["sessions"][0]["state"], "blocked")  # writer stopped, exit verified...
            self.assertIsNone(res["sessions"][0]["resume"])           # ...but nothing given back
            self.assertFalse(same_process(self.own("cx")["detail"]["relay"]))
            self.assertEqual(self.resume_shown(), [])
            snap = probe.status(self.bus, pid)
            self.assertEqual(snap["unknown_turns"], [PROBE_TURN])
            self.assertTrue(all(s["claimed_by"] == pid for s in snap["sessions"].values()))  # claims kept
            # The explicit, recorded disposition:
            r = self.host.abandon_unknown(pid)
            self.assertEqual((r["abandoned"], r["statement"]), ([turn4["turn"]], ABANDON_STATEMENT))
            self.assertEqual(self.bus.pair_state(pid)["turns"][PROBE_TURN - 1]["phase"], "uncertain")  # not relabelled
            self.assertEqual(self.host.resolve_handoff(pid, self.eid(pid, "cx"))["state"], "released")
        else:
            self.assertEqual(self.host.back_to_terminal(pid, ["leader"])["sessions"][0]["state"], "released")
        self.assertEqual(self.host.resolve_handoff(pid, self.eid(pid, "cl"))["state"], "released")
        self.assertEqual(self.submits(pid), before)  # never resent
        snap = probe.status(self.bus, pid)
        for role in ("leader", "follower"):
            self.assertEqual(snap["sessions"][role]["ownership"], "released")
            self.assertFalse(snap["sessions"][role]["relay_writer_running"])
            self.assertIsNone(snap["sessions"][role]["claimed_by"])
        self.assertEqual(snap["unknown_turns"], [])

    def test_probe_never_counts_an_already_ended_turn(self):
        pid, _cl, _cx = self.team(cl_sleep=0.05, cx_sleep=0.05)
        self.tick_until(lambda: self.host.attention_kind(self.bus.pair_state(pid)) == "budget")
        killed = []
        res = probe.midturn(self.bus, pid, "follower", 2, 2, kill=lambda *a: killed.append(a))
        self.assertEqual(res["verdict"], "not_observed")
        self.assertEqual(killed, [])
        self.host.release(pid)


# ===================================================================== B
class ConsoleCrashDuringReturn(Base):
    """The console is a real `relay serve` process here, launched by the probe's `console`
    mode (durable pid + start time + argv), so it can really be killed."""

    def serve(self, tag):
        rec = os.path.join(self.tmp, "run", f"console-{tag}.json")
        log = open(os.path.join(self.tmp, "run", f"serve-{tag}.log"), "w")
        env = dict(os.environ, AGENT_RELAY_FAKE_HOME=self.home, PYTHONDONTWRITEBYTECODE="1")
        p = subprocess.Popen([sys.executable, os.path.join(HERE, "scripts", "handoff_probe.py"), "console",
                              "--root", self.root.path, "--console-record", rec, "--",
                              "--adapter", "fake", "--new-sessions", "fake", "--discovery-interval", "1",
                              "--port", "0"], env=env, stdout=log, stderr=subprocess.STDOUT)
        self.procs.append(p)
        log.close()
        for _ in range(600):  # a cold start on a slow CI runner can take well over 15 s
            with open(log.name) as f:
                text = f.read()
            if "http://127.0.0.1:" in text:
                url = text[text.index("http://127.0.0.1:"):].split()[0]
                port = int(url.split(":")[2].split("/")[0])
                return p, rec, port, url.split("t=")[1]
            time.sleep(0.1)
        raise AssertionError("console didn't start: " + open(log.name).read())

    def api(self, port, token, path, body=None, ok=True):
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers={
            "X-Relay-Token": token, "Origin": f"http://127.0.0.1:{port}", "Content-Type": "application/json"},
            data=json.dumps(body).encode() if body is not None else None, method="POST" if body is not None else "GET")
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if not ok:
                return {"status": e.code, **json.loads(e.read().decode())}
            raise AssertionError(f"{path}: {e.code} {e.read().decode()}")

    def setUp(self):
        super().setUp()
        self.procs = []

    def tearDown(self):
        for p in self.procs:
            if p.poll() is None:
                p.send_signal(signal.SIGINT)
                try:
                    p.wait(15)
                except subprocess.TimeoutExpired:
                    p.kill()
        super().tearDown()

    def wait(self, pred, timeout=30):
        end = time.time() + timeout
        while time.time() < end:
            v = pred()
            if v:
                return v
            time.sleep(0.05)
        raise AssertionError("condition not met in time: " + json.dumps(
            {p: probe.status(self.bus, p) for p in self.bus.pair_ids()}, default=str)[:3000])

    def test_crash_while_returning_never_resends_and_needs_acknowledgement(self):
        cl = self.terminal("cl", "Claude", 1, sleep=2.0)
        cx = self.terminal("cx", "Codex", 1, sleep=2.0, finish_after=1000)
        console, rec, port, token = self.serve("a")
        self.assertEqual(probe.console_identity(rec, self.root.path)["pid"], console.pid)  # exec kept the pid
        rows = self.wait(lambda: {s["name"]: s for s in self.api(port, token, "/api/sessions")["sessions"]}
                         if len(self.api(port, token, "/api/sessions")["sessions"]) >= 2 else None)
        team = self.api(port, token, "/api/manage", {
            "key": rows["scratch · Claude"]["key"], "manager": {"existing": rows["scratch · Codex"]["key"]},
            "goal": "Each turn, one short line with your own code word, then the relay block."})["team"]
        cl.stop(), cx.stop()

        crash_file = os.path.join(self.tmp, "run", "probe-crash.json")
        verdict = {}

        def run_probe():
            try:
                verdict.update(probe.crash_console(Bus(Root(self.root.path)), team, rec, self.root.path, 1, 60))
            except Exception as e:  # surfaced in the assertion below
                verdict.update(error=repr(e))
            probe.record(crash_file, verdict)
        t = threading.Thread(target=run_probe)
        t.start()
        # The operator's observable point: "… is working…" on turn 1, then Back to terminal.
        self.wait(lambda: (self.bus.pair_state(team).get("inflight") or {}).get("phase") == "accepted")
        try:
            self.api(port, token, f"/api/team/{team}/return", {"roles": ["leader", "follower"]})
        except Exception:
            pass  # the console is killed while it's settling this request
        t.join(30)
        self.assertEqual(verdict.get("verdict"), "observed", verdict)
        self.assertEqual((verdict["root"], verdict["team"]), (os.path.realpath(self.root.path), team))
        for role in ("leader", "follower"):  # each role's launch at the crash, by pid + start
            self.assertTrue(probe.valid_start(verdict["roles"][role]["writer"]["started_ms"]), verdict)
        console.wait(10)
        self.assertEqual(console.returncode, -signal.SIGKILL)
        inflight = verdict["turn"]
        before = self.submits(team)

        # Restart (same launcher, a new record). Nothing routes or is given back.
        console2, rec2, port, token = self.serve("b")
        with self.assertRaises(probe.Refused):  # the old record now names a dead process
            probe.console_identity(rec, self.root.path)

        def rows():
            return {s["name"]: s for s in self.api(port, token, "/api/sessions")["sessions"]
                    if s["name"].startswith("scratch")}
        r = self.wait(lambda: (lambda x: x if len(x) == 2 and all(v.get("ownership") for v in x.values()) else None)(rows()))
        for name, row in r.items():
            self.assertEqual(row["ownership"]["state"], "returning", name)
            self.assertTrue(row["ownership"]["orphaned"], name)
            self.assertNotIn("resume", row["ownership"], name)
        time.sleep(1.5)
        self.assertEqual(self.submits(team), before)  # the in-flight turn is never resent
        self.assertEqual(before.count(inflight), 1)

        # Recovery 1: Back to terminal can't settle the turn: both blocked, no resume command.
        res = self.api(port, token, f"/api/team/{team}/return", {"roles": ["leader", "follower"]})
        self.assertFalse(res["settled"])
        self.assertTrue(all(x["state"] == "blocked" and x["resume"] is None for x in res["sessions"]), res)

        # Recovery 2: a writer the crashed console left behind, only with the crash record.
        snap = probe.status(self.bus, team)
        for role in ("leader", "follower"):
            if snap["sessions"][role]["relay_writer_running"]:
                self.assertEqual(probe.stop_orphan(self.bus, self.root.path, team, role, crash_file)["verdict"], "stopped")

        # Recovery 3: Stop managing (dead lease cleared). Resolve is refused until the unknown
        # outcome is explicitly abandoned; then each is released with its exit verified.
        self.api(port, token, f"/api/team/{team}/release", {})
        d = self.bus.get_pair_def(team)
        eids = (d["leader"]["endpoint"], d["follower"]["endpoint"])
        refused = self.api(port, token, f"/api/team/{team}/resolve-handoff", {"endpoint": eids[0]}, ok=False)
        self.assertEqual(refused["status"], 409)
        self.assertIn("abandon-unknown", refused["error"])
        self.assertEqual(self.api(port, token, f"/api/team/{team}/abandon-unknown", {}, ok=False)["status"], 400)
        r = self.api(port, token, f"/api/team/{team}/abandon-unknown", {"confirm": True})
        self.assertEqual(r["abandoned"], [inflight])
        for eid in eids:
            self.assertEqual(self.api(port, token, f"/api/team/{team}/resolve-handoff", {"endpoint": eid})["state"],
                             "released")
        self.assertEqual(self.submits(team), before)  # still never resent
        snap = self.wait(lambda: (lambda s: s if all(x["ownership"] == "released" and not x["claimed_by"]
                                                     for x in s["sessions"].values()) else None)(
            probe.status(self.bus, team)), timeout=20)
        self.assertEqual(snap["abandoned_turns"], [1])
        self.assertEqual(self.bus.pair_state(team)["turns"][0]["phase"], "accepted")  # outcome still unknown
        for role in ("leader", "follower"):
            self.assertFalse(snap["sessions"][role]["relay_writer_running"])


# ==================================================================== G2
class DeadControllerLease(HostBase):
    """Stop managing after a console crash: the crashed console's controller lease is cleared
    only when its process is verifiably gone on this host; a live pid keeps it."""

    def owned_by(self, pid_of_owner):
        pid, cl, cx = self.team(cl_sleep=0.05, cx_sleep=0.05, close=())
        self.bus.pair_event(pid, "controller_acquired", {"ctl": "ctl_crashed", "pid": pid_of_owner,
                                                         "host": socket.gethostname()},
                            actor("human-directed", "ctl_crashed"))
        return pid, cl, cx

    def test_dead_owner_lease_is_cleared_and_stop_finishes(self):
        pid, cl, cx = self.owned_by(gone_process()["pid"])
        self.host.release(pid)
        st = self.bus.pair_state(pid)
        self.assertIsNone(st["owner"])
        self.assertEqual(st["terminal"], "stopped")
        rel = [e for e in self.root.read_stream(f"pair-{pid}") if e.type == "controller_released"]
        self.assertEqual(rel[-1].data, {"ctl": "ctl_crashed", "reason": "its process is gone"})
        cl.stop(), cx.stop()

    def test_live_owner_lease_is_kept(self):
        alive = sleeper()
        try:
            pid, cl, cx = self.owned_by(alive.pid)
            with self.assertRaises(Conflict):
                self.host.release(pid)
            self.assertEqual(self.bus.pair_state(pid)["owner"]["ctl"], "ctl_crashed")
            cl.stop(), cx.stop()
        finally:
            alive.kill()
            alive.wait()


# ==================================================================== R1
class StrictProbeIdentity(HostBase):
    def test_missing_malformed_and_reused_start_times_are_refused(self):
        alive = sleeper()
        try:
            started = probe.process_start_ms(alive.pid)
            self.assertIsNone(probe.strict_identity({"pid": alive.pid, "started_ms": started}))
            for bad in (None, "abc", True, 0, -5):
                self.assertIsNotNone(probe.strict_identity({"pid": alive.pid, "started_ms": bad}), bad)
            for bad_pid in (None, "12", True, 0, 1):
                self.assertIsNotNone(probe.strict_identity({"pid": bad_pid, "started_ms": started}), bad_pid)
            self.assertIn("pid reused", probe.strict_identity({"pid": alive.pid, "started_ms": started - 60_000}))
            with mock.patch.object(probe, "process_start_ms", return_value=None):
                self.assertIn("can't observe", probe.strict_identity({"pid": alive.pid, "started_ms": started}))
            self.assertIsNotNone(probe.strict_identity(gone_process()))
            # Conservative ownership semantics are unchanged: unknown start = "maybe the same".
            self.assertTrue(same_process({"pid": alive.pid, "started_ms": None}))
        finally:
            alive.kill()
            alive.wait()

    def test_an_exited_unreaped_writer_is_not_running(self):
        # A killed child stays a zombie until its parent reaps it; it still answers kill(pid, 0),
        # and on some macOS versions ps reports no start time for it. It can't write: gone.
        child = subprocess.Popen(["sleep", "30"])
        try:
            rec = {"pid": child.pid, "started_ms": process_start_ms(child.pid)}
            os.kill(child.pid, signal.SIGKILL)
            self.wait_until(lambda: process_exited(child.pid))
            self.assertFalse(same_process(rec))
            self.assertFalse(same_process(dict(rec, started_ms=None)))
        finally:
            child.wait()

    def test_midturn_refuses_without_a_verifiable_writer(self):
        # Codex's reproduction: writer started_ms None and the observed start time None.
        pid, _cl, _cx = self.team(cl_sleep=3, cx_sleep=0.05)
        self.tick_until(lambda: (self.bus.pair_state(pid).get("inflight") or {}).get("role") == "follower"
                        and self.bus.pair_state(pid)["inflight"]["phase"] == "accepted")
        real = self.own("cl")["spawn"]["relay"]
        killed = []
        with mock.patch.object(probe, "writer_of", return_value={"pid": real["pid"], "started_ms": None}), \
                mock.patch.object(probe, "process_start_ms", return_value=None):
            with self.assertRaises(probe.Refused):
                probe.midturn(self.bus, pid, "follower", 2, 5, kill=lambda *a: killed.append(a))
        with mock.patch.object(probe, "writer_of", return_value={"pid": real["pid"],
                                                                 "started_ms": real["started_ms"] - 60_000}):
            with self.assertRaises(probe.Refused):  # pid reuse
                probe.midturn(self.bus, pid, "follower", 2, 5, kill=lambda *a: killed.append(a))
        self.assertEqual(killed, [])
        self.assertTrue(same_process(real))
        self.host.release(pid)

    def console_record(self, proc_argv, rec_root, rec_argv=None):
        p = subprocess.Popen(proc_argv)
        self.addCleanup(lambda: (p.kill(), p.wait()))
        time.sleep(0.3)
        path = os.path.join(self.tmp, f"console-{p.pid}.json")
        with open(path, "w") as f:
            json.dump({"pid": p.pid, "started_ms": probe.process_start_ms(p.pid), "root": rec_root,
                       "argv": rec_argv or proc_argv}, f)
        return path

    def test_console_identity_is_exact(self):
        root = os.path.realpath(os.path.join(self.tmp, "probe-root"))
        other = root + "-other"
        code = "import time; time.sleep(30)"
        good = [sys.executable, "-c", code, "serve", "--root", root]
        path = self.console_record(good, root)
        self.assertEqual(probe.console_identity(path, root)["argv"], good)
        with self.assertRaises(probe.Refused):  # root-prefix mismatch: record is for another root
            probe.console_identity(path, other)
        # A process actually serving `<root>-other`, recorded as if it served `<root>`: spoof.
        spoof = self.console_record([sys.executable, "-c", code, "serve", "--root", other], root, rec_argv=good)
        with self.assertRaises(probe.Refused):
            probe.console_identity(spoof, root)
        # Some other command entirely under the recorded pid.
        cmd = self.console_record([sys.executable, "-c", code, "something-else"], root, rec_argv=good)
        with self.assertRaises(probe.Refused):
            probe.console_identity(cmd, root)
        with self.assertRaises(probe.Refused):  # no record at all
            probe.console_identity(os.path.join(self.tmp, "missing.json"), root)

    def test_stop_orphan_is_bound_to_the_crashed_launch(self):
        pid, _cl, _cx = self.team(cl_sleep=5, cx_sleep=5)
        self.tick_until(lambda: self.own("cl")["state"] == "relay_owned" and self.own("cx")["state"] == "relay_owned")
        writer = self.own("cl")["spawn"]["relay"]
        root = self.root.path
        killed = []
        no_kill = dict(kill=lambda *a: killed.append(a))
        orphaned = dict(parent_of=lambda _pid: 1)  # as after the console died (re-parented)
        binding = probe.crash_binding(self.bus, root, pid)  # captured at the crash
        self.assertEqual(binding["roles"]["follower"]["writer"], writer)

        def crash(path, killed_rec=None, verdict="observed", **change):
            rec = {"probe": "crash-console", "verdict": verdict, "killed": killed_rec or gone_process(),
                   **json.loads(json.dumps(binding))}
            for k, v in change.items():
                if k.startswith("follower_"):
                    rec["roles"]["follower"][k[len("follower_"):]] = v
                else:
                    rec[k] = v
            with open(path, "w") as f:
                json.dump(rec, f)
            return path
        p = os.path.join(self.tmp, "crash.json")
        # The interrupted return: the fence moves (returning -> blocked), the writer launch doesn't.
        key = self.host.handoffs.key("fake", "cl")
        self.host.handoffs.block(key, "test: interrupted return")
        self.assertNotEqual(self.own("cl")["gen"], binding["roles"]["follower"]["gen"])

        refusals = {
            "no record": None,
            "absent file": os.path.join(self.tmp, "absent.json"),
            "not an observed crash": crash(p + "1", verdict="not_observed"),
            "other root": crash(p + "2", root=root + "-other"),
            "other team": crash(p + "3", team="p_other"),
            "other conversation": crash(p + "4", follower_conversation="someone-else"),
            "no writer evidence": crash(p + "5", follower_writer=None),
            "malformed writer start": crash(p + "6", follower_writer={"pid": writer["pid"], "started_ms": None}),
            "an older launch": crash(p + "7", follower_writer={"pid": writer["pid"], "started_ms": writer["started_ms"] - 60_000}),
            # Codex's reproduction: another root/team's crash naming a different follower pid.
            "foreign crash": crash(p + "8", root="/tmp/probe-root-other", team="p_other",
                                   follower_writer={"pid": writer["pid"] + 1, "started_ms": writer["started_ms"]}),
        }
        for name, path in refusals.items():
            with self.subTest(name), self.assertRaises(probe.Refused):
                probe.stop_orphan(self.bus, root, pid, "follower", path, **no_kill, **orphaned)
        alive = sleeper()
        try:
            for name, rec in (("crashed console alive", {"pid": alive.pid, "started_ms": probe.process_start_ms(alive.pid)}),
                              ("alive, start unknown", {"pid": alive.pid, "started_ms": None})):
                with self.subTest(name), self.assertRaises(probe.Refused):
                    probe.stop_orphan(self.bus, root, pid, "follower", crash(p, rec), **no_kill, **orphaned)
        finally:
            alive.kill()
            alive.wait()
        with self.subTest("a live owner"), self.assertRaises(probe.Refused):  # real parent: this console
            probe.stop_orphan(self.bus, root, pid, "follower", crash(p), **no_kill)
        with self.subTest("parent unknown"), self.assertRaises(probe.Refused):
            probe.stop_orphan(self.bus, root, pid, "follower", crash(p), **no_kill, parent_of=lambda _p: None)
        self.assertEqual(killed, [])
        self.assertTrue(same_process(writer))
        # The bound, orphaned launch is stopped; nothing else is touched.
        res = probe.stop_orphan(self.bus, root, pid, "follower", crash(p), **orphaned)
        self.assertEqual((res["verdict"], res["writer"]), ("stopped", writer))
        self.assertTrue(probe.proven_gone(writer))  # exited (possibly not yet collected by its parent)
        self.assertTrue(same_process(self.own("cx")["spawn"]["relay"]))
        self.host.release(pid)

    def test_a_relaunched_writer_is_never_the_crashed_one(self):
        pid, _cl, _cx = self.team(cl_sleep=5, cx_sleep=5)
        self.tick_until(lambda: self.own("cl")["state"] == "relay_owned" and self.own("cx")["state"] == "relay_owned")
        binding = probe.crash_binding(self.bus, self.root.path, pid)
        # Recovery, then a new hand-over: a new launch episode with a new writer.
        self.host.back_to_terminal(pid, ["leader", "follower"])
        self.host.hand_over_again(pid)
        self.tick_until(lambda: self.own("cl")["state"] == "relay_owned")
        new = self.own("cl")["spawn"]["relay"]
        self.assertNotEqual(new, binding["roles"]["follower"]["writer"])
        key = self.host.handoffs.key("fake", "cl")
        self.host.handoffs.block(key, "test")
        path = os.path.join(self.tmp, "old-crash.json")
        with open(path, "w") as f:
            json.dump({"probe": "crash-console", "verdict": "observed", "killed": gone_process(), **binding}, f)
        killed = []
        with self.assertRaises(probe.Refused):
            probe.stop_orphan(self.bus, self.root.path, pid, "follower", path,
                              kill=lambda *a: killed.append(a), parent_of=lambda _p: 1)
        self.assertEqual(killed, [])
        self.assertTrue(same_process(new))
        self.host.release(pid)

    def test_unowned_needs_a_reparented_process(self):
        # A real orphan: its parent shell exits, so it's re-parented to launchd (pid 1).
        out = subprocess.run(["sh", "-c", "sleep 30 >/dev/null 2>&1 & echo $!"], capture_output=True, text=True)
        orphan = int(out.stdout.strip())
        try:
            time.sleep(0.3)
            self.assertIsNone(probe.unowned(orphan))
            child = sleeper()
            try:
                self.assertIn("live parent", probe.unowned(child.pid))  # parent: this process
            finally:
                child.kill()
                child.wait()
        finally:
            os.kill(orphan, signal.SIGKILL)


# ==================================================================== R2
class DetachKeepsReservations(HostBase):
    def claims(self, pid):
        d = self.bus.get_pair_def(pid)
        return [self.bus.get_session(d[r]["endpoint"])["state"]["pair"] for r in ("leader", "follower")]

    def stream_len(self, pid):
        return len(self.root.read_stream(f"pair-{pid}"))

    def test_half_handed_over_team_is_not_detached(self):
        # Codex's reproduction: only Claude's terminal closes; its relay writer starts while
        # Codex's terminal stays open (no controller yet); then `relay pair detach`.
        pid, _cl, cx = self.team(close=("cl",))
        self.tick_until(lambda: self.own("cl")["state"] == "relay_owned")
        self.assertEqual(self.own("cx")["state"], "awaiting_exit")
        n = self.stream_len(pid)
        r = self.cli("pair", "detach", pid)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("claims kept", r.stderr)
        self.assertIn("relay owned", r.stderr)
        self.assertEqual(self.claims(pid), [pid, pid])          # reservations intact
        self.assertEqual(self.stream_len(pid), n)               # nothing mutated (not even stopped)
        self.assertTrue(same_process(self.own("cl")["spawn"]["relay"]))
        self.host.cancel_handoff(pid)
        cx.stop()

    def test_blocked_and_interrupted_ownership_keep_claims(self):
        pid, _cl, _cx = self.team()
        self.tick_until(lambda: self.own("cl")["state"] == "relay_owned" and self.own("cx")["state"] == "relay_owned")
        self.host.release(pid)
        key = self.host.handoffs.key("fake", "cl")
        self.host.handoffs.block(key, "test: blocked")
        r = self.cli("pair", "detach", pid)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("blocked", r.stderr)
        self.assertEqual(self.claims(pid), [pid, pid])
        # An interrupted move (fence without evidence) reads as blocked: still held.
        st = self.own("cl")
        self.bus.fence(key, expect_gen=st["gen"], state="returning", token="t", who=actor("human"))
        self.assertTrue(self.own("cl").get("interrupted"))
        self.assertNotEqual(self.cli("pair", "detach", pid).returncode, 0)
        self.assertEqual(self.claims(pid), [pid, pid])

    def test_unknown_outcome_keeps_claims_until_abandoned(self):
        pid = self.uncertain_team()
        self.host.release(pid)  # the console's own detach keeps every claim
        self.assertEqual(self.claims(pid), [pid, pid])
        n = self.stream_len(pid)
        r = self.cli("pair", "detach", pid)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("abandon-unknown", r.stderr)
        self.assertEqual((self.claims(pid), self.stream_len(pid)), ([pid, pid], n))
        self.host.abandon_unknown(pid)
        self.assertEqual(self.host.resolve_handoff(pid, self.eid(pid, "cl"))["state"], "released")
        self.assertEqual(self.claims(pid), [pid, None])  # Claude released; Codex is still with the relay
        self.assertEqual(self.host.back_to_terminal(pid, ["leader"])["sessions"][0]["state"], "released")
        self.assertEqual(self.claims(pid), [None, None])  # released once nothing is held

    def test_detach_cannot_race_a_hand_over(self):
        pid, _cl, _cx = self.team()
        self.tick_until(lambda: self.own("cl")["state"] == "relay_owned" and self.own("cx")["state"] == "relay_owned")
        res = self.host.back_to_terminal(pid, ["leader", "follower"])
        self.assertTrue(all(x["state"] == "released" for x in res["sessions"]))
        result = {}
        real_begin = Handoffs.begin

        def slow_begin(h, *a, **k):
            if "proc" not in result:  # detach starts while Hand over again holds the lock
                result["proc"] = subprocess.Popen(
                    [sys.executable, "-m", "relay", "pair", "detach", pid, "--root", self.root.path],
                    cwd=HERE, env=dict(os.environ, PYTHONPATH=HERE), stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, text=True)
                time.sleep(1.5)
                result["running_during_begin"] = result["proc"].poll() is None
            return real_begin(h, *a, **k)
        with mock.patch.object(Handoffs, "begin", slow_begin):
            self.host.hand_over_again(pid)
        out, err = result["proc"].communicate(timeout=60)
        self.assertTrue(result["running_during_begin"])          # it waited for the lock
        self.assertNotEqual(result["proc"].returncode, 0, out)  # then saw the hand-over and refused
        self.assertIn("awaiting exit", err)
        self.assertEqual(self.claims(pid), [pid, pid])
        self.host.cancel_handoff(pid)

    def test_unattributable_ownership_record_fails_closed(self):
        # A member whose ownership record can't be tied to its conversation (an interrupted
        # first move left a fence with no evidence) is never read as "native": held.
        pid, _cl, _cx = self.team()
        self.tick_until(lambda: self.own("cl")["state"] == "relay_owned" and self.own("cx")["state"] == "relay_owned")
        self.host.back_to_terminal(pid, ["leader", "follower"])
        from relay import handoff
        keys, _ = handoff.ownership_keys(self.bus)
        cl_key = keys[("fake", "cl")]
        with mock.patch.object(handoff, "ownership_keys",
                               return_value=({k: v for k, v in keys.items() if v != cl_key}, [cl_key])):
            holds = handoff.team_holds(self.bus, pid, ending=True)
        self.assertTrue(any("can't be attributed" in h for h in holds), holds)
        self.assertEqual(handoff.team_holds(self.bus, pid, ending=True), [])  # attributable: released

    def test_released_team_detaches(self):
        pid, _cl, _cx = self.team()
        self.tick_until(lambda: self.own("cl")["state"] == "relay_owned" and self.own("cx")["state"] == "relay_owned")
        self.host.back_to_terminal(pid, ["leader", "follower"])
        r = self.cli("pair", "detach", pid)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.claims(pid), [None, None])


# ==================================================================== R3
class UnknownOutcomeDisposition(HostBase):
    def test_stop_alone_is_not_acknowledgement(self):
        pid = self.uncertain_team()
        self.host.release(pid)
        with self.assertRaises(Conflict):
            self.host.resolve_handoff(pid, self.eid(pid, "cl"))
        res = self.host.back_to_terminal(pid, ["leader"])
        self.assertEqual((res["sessions"][0]["state"], res["sessions"][0]["resume"]), ("blocked", None))
        self.assertEqual(self.resume_shown(), [])
        # Abandonment is recorded verbatim, only for unknown turns, never twice.
        abandon_unknown(self.bus, pid)
        with self.assertRaises(Conflict):
            abandon_unknown(self.bus, pid)
        with self.assertRaises(Conflict):  # no replay or continuation on the ended team
            reconcile(self.bus, pid, "requeue", reason="try again")
        self.assertEqual(self.host.resolve_handoff(pid, self.eid(pid, "cl"))["state"], "released")
        self.assertEqual(self.host.resolve_handoff(pid, self.eid(pid, "cx"))["state"], "released")
        self.assertEqual(sorted(self.resume_shown()), ["scratch · Claude", "scratch · Codex"])

    def test_acknowledgement_does_not_replace_verified_exit(self):
        pid = self.uncertain_team()
        self.host.release(pid)
        self.host.abandon_unknown(pid)
        key = self.host.handoffs.key("fake", "cl")
        with mock.patch.object(self.host.handoffs, "relay_writer_status", return_value="unknown"):
            st = self.host.resolve_handoff(pid, self.eid(pid, "cl"))
        self.assertEqual(st["state"], "blocked")  # exit unknown: still held
        self.assertEqual(self.host.handoffs.own.get(key)["state"], "blocked")
        self.assertNotIn("scratch · Claude", self.resume_shown())

    def test_abandon_ends_a_running_team_and_is_verbatim(self):
        pid = self.uncertain_team()  # team paused (blocked), not ended
        self.assertFalse(self.bus.pair_state(pid)["terminal"])
        t = self.bus.pair_state(pid)["turns"][-1]["turn"]
        with self.assertRaises(Conflict):  # the event itself needs an ended team
            self.bus.pair_event(pid, "outcome_abandoned", {"turn": t, "statement": ABANDON_STATEMENT}, actor("human"))
        r = self.cli("pair", "abandon-unknown", pid)  # without --confirm: nothing recorded
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(self.bus.pair_state(pid)["terminal"])
        r = self.cli("pair", "abandon-unknown", pid, "--confirm")
        self.assertEqual(r.returncode, 0, r.stderr)
        st = self.bus.pair_state(pid)
        self.assertEqual(st["terminal"], "stopped")
        self.assertEqual(st["turns"][-1]["abandoned"], ABANDON_STATEMENT)
        self.assertEqual(st["turns"][-1]["phase"], "uncertain")
        self.host.release(pid)


# ============================================================== live finding
class ManageAgainAfterEndedTeam(HostBase):
    """Supplemental live run (2026-10-10): after team A ended and gave both
    conversations back, Manage on the same rows failed with "session already registered
    as … for scratch-claude": the hand-off endpoint was registered under a NEW participant
    label. One conversation is one endpoint: a later team reuses it."""

    def test_a_second_team_reuses_the_conversations_endpoints(self):
        a, _cl, _cx = self.team()
        self.tick_until(lambda: self.own("cl")["state"] == "relay_owned" and self.own("cx")["state"] == "relay_owned")
        self.tick_until(lambda: self.host.attention_kind(self.bus.pair_state(a)) == "budget")
        self.host.release(a)
        for role in ("follower", "leader"):
            self.assertEqual(self.host.back_to_terminal(a, [role])["sessions"][0]["state"], "released")
        da = self.bus.get_pair_def(a)
        # Simulated terminals vanish from discovery once closed (real clients still list them),
        # so reopen them, as if resumed natively; the registration path is what's under test.
        cl = self.terminal("cl", "Claude", 0, sleep=0.6)
        cx = self.terminal("cx", "Codex", 0, sleep=0.1, finish_after=1000)
        b = self.manage_again()
        cl.stop(), cx.stop()
        db = self.bus.get_pair_def(b)
        for role in ("leader", "follower"):
            self.assertEqual(db[role]["endpoint"], da[role]["endpoint"])  # same endpoint, not a new alias
        self.tick_until(lambda: self.own("cl")["state"] == "relay_owned" and self.own("cx")["state"] == "relay_owned")
        self.tick_until(lambda: self.bus.pair_state(b)["used"] >= 1)  # team B really runs
        self.host.release(b)

    def test_a_conversation_still_held_by_an_ended_team_is_refused_without_releasing_it(self):
        a, _cl, _cx = self.team()
        self.tick_until(lambda: self.own("cl")["state"] == "relay_owned" and self.own("cx")["state"] == "relay_owned")
        self.host.release(a)  # ended, but the relay still holds both conversations
        da = self.bus.get_pair_def(a)
        self.terminal("cl", "Claude", 0, sleep=0.6)  # listed again (see above)
        self.terminal("cx", "Codex", 0, sleep=0.1, finish_after=1000)
        before = set(self.bus.pair_ids())
        with self.assertRaises(Conflict):
            self.manage_again()
        for role in ("leader", "follower"):  # team A's claims are untouched
            self.assertEqual(self.bus.get_session(da[role]["endpoint"])["state"]["pair"], a)
        self.assertEqual(set(self.bus.pair_ids()), before)  # no new team
        self.host.back_to_terminal(a, ["leader", "follower"])


class ManageRollback(HostBase):
    """Live finding #2: the failed Manage left BOTH conversations
    `awaiting_exit` (it began the hand-offs, then registration raised outside the cleanup).
    A failure after the hand-offs begin must undo exactly what that request did."""

    def ended_and_given_back(self):
        a, _cl, _cx = self.team()
        self.tick_until(lambda: self.own("cl")["state"] == "relay_owned" and self.own("cx")["state"] == "relay_owned")
        self.tick_until(lambda: self.host.attention_kind(self.bus.pair_state(a)) == "budget")
        self.host.release(a)
        for role in ("follower", "leader"):
            self.host.back_to_terminal(a, [role])
        self.terminal("cl", "Claude", 0, sleep=0.6)   # listed again (closed fakes aren't discovered)
        self.terminal("cx", "Codex", 0, sleep=0.1, finish_after=1000)
        return a

    def assert_nothing_stranded(self, before_pairs):
        self.assertEqual(set(self.bus.pair_ids()), before_pairs)  # no new team (or only a stopped one)
        for sid in ("cl", "cx"):
            self.assertIn(self.own(sid)["state"], ("native", "released"), sid)

    def test_registration_refused_after_both_begins(self):
        self.ended_and_given_back()
        before = set(self.bus.pair_ids())
        real = Host._handoff_endpoint
        calls = []

        def refuse_second(host, item):
            calls.append(item["name"])
            if len(calls) == 2:
                raise Conflict("injected: registration refused")
            return real(host, item)
        with mock.patch.object(Host, "_handoff_endpoint", refuse_second):
            with self.assertRaises(Conflict):
                self.manage_again()
        self.assertEqual(len(calls), 2)  # both hand-offs had begun; the second registration failed
        self.assert_nothing_stranded(before)

    def test_old_failure_then_corrected_retry(self):
        a = self.ended_and_given_back()
        da = self.bus.get_pair_def(a)
        before = set(self.bus.pair_ids())
        # The pre-fix registration (a NEW participant label), exactly as it failed live.
        from relay.adapters import get_adapter

        def old_endpoint(host, item):
            rec, _ = host.bus.register_session(
                adapter=item["adapter"], client_session_id=item["client_session_id"], mode="launch",
                participant=host._participant_for(item["name"]), cwd=item["cwd"],
                options=dict(host.options, handoff=True), info={"client": item["client"], "name": item["name"]},
                capabilities=get_adapter(item["adapter"], host.options).capabilities())
            return rec["id"]
        with mock.patch.object(Host, "_handoff_endpoint", old_endpoint):
            with self.assertRaises(Conflict) as cm:
                self.manage_again()
        self.assertIn("already registered", str(cm.exception))
        self.assert_nothing_stranded(before)  # the live run had both stuck in awaiting_exit here
        b = self.manage_again()                 # the corrected retry
        db = self.bus.get_pair_def(b)
        self.assertEqual((db["leader"]["endpoint"], db["follower"]["endpoint"]),
                         (da["leader"]["endpoint"], da["follower"]["endpoint"]))
        for sid in ("cl", "cx"):
            self.assertEqual(self.own(sid)["state"], "awaiting_exit")
        self.host.cancel_handoff(b)

    def test_two_consoles_rollback_never_touches_a_later_episode(self):
        """Codex follow-up (2026-10-10): the console lock is per process. While console 1's
        Manage fails, console 2 cancels and begins the leader's hand-off again; console 1's
        rollback must cancel only ITS episode and leave console 2's pending hand-off alone."""
        self.ended_and_given_back()
        other = Host(self.bus, self.discovery, new_session_adapters={"claude": "fake", "codex": "fake"},
                     adapter_options={"home": self.home})
        self.addCleanup(other.shutdown)
        real = Host._handoff_endpoint
        calls, theirs = [], {}

        def interleave(host, item):
            calls.append(item["name"])
            if len(calls) == 2:  # both begun by console 1; now console 2 acts on the leader's
                h = other.handoffs
                h.cancel(item["adapter"], item["client_session_id"])
                theirs.update(h.begin(item["adapter"], item["client_session_id"], item["cwd"]))
                raise Conflict("injected: registration refused")
            return real(host, item)
        with mock.patch.object(Host, "_handoff_endpoint", interleave):
            with self.assertRaises(Conflict):
                self.manage_again()
        self.assertIn(self.own("cl")["state"], ("native", "released"))  # console 1's own: cancelled
        cx = self.own("cx")
        self.assertEqual(cx["state"], "awaiting_exit")                   # console 2's: untouched
        self.assertEqual(cx["detail"]["episode"], theirs["detail"]["episode"])
        self.assertEqual(cx["gen"], theirs["gen"])
        other.handoffs.cancel("fake", "cx", episode=theirs["detail"]["episode"])

    def test_episode_guard_refuses_a_stale_episode(self):
        self.ended_and_given_back()
        h = self.host.handoffs
        first = h.begin("fake", "cx", self.own("cx")["detail"]["cwd"])["detail"]["episode"]
        h.cancel("fake", "cx")
        second = h.begin("fake", "cx", self.own("cx")["detail"]["cwd"])["detail"]["episode"]
        self.assertNotEqual(first, second)
        with self.assertRaises(Conflict):
            h.cancel("fake", "cx", episode=first)
        with self.assertRaises(Conflict):
            h.block(self.own("cx")["key"], "stale", episode=first)
        self.assertEqual(self.own("cx")["state"], "awaiting_exit")
        h.cancel("fake", "cx", episode=second)
        self.assertEqual(self.own("cx")["state"], "native")
        self.assertNotIn("episode", self.own("cx")["detail"])  # the episode ends with it

    def test_failure_after_the_team_exists_leaves_other_teams_alone(self):
        # An unrelated team keeps running throughout, with its own claims and writers.
        other = os.path.realpath(os.path.join(self.tmp, "other"))
        os.makedirs(other)
        self.fake("o1", cwd=other, client="Claude", default={"natural": True, "sleep": 0.3})
        self.fake("o2", cwd=other, client="Codex", default={"natural": True, "sleep": 0.3, "finish_after": 1000})
        self.ended_and_given_back()
        self.discovery.refresh_once()
        cat = {x["name"]: x for x in self.host.catalog()["sessions"]}
        o = self.host.manage(cat["other · Claude"]["key"], {"existing": cat["other · Codex"]["key"]}, "Keep going.")["team"]
        self.tick_until(lambda: self.bus.pair_state(o)["used"] >= 1)
        do = self.bus.get_pair_def(o)
        before = set(self.bus.pair_ids())
        with mock.patch.object(self.bus, "start_pair", side_effect=Conflict("injected: start refused")):
            with self.assertRaises(Conflict):
                self.manage_again()
        new = set(self.bus.pair_ids()) - before
        self.assertEqual(len(new), 1)  # it was created, then stopped by the rollback
        n = new.pop()
        self.assertEqual(self.bus.pair_state(n)["terminal"], "stopped")
        dn = self.bus.get_pair_def(n)
        for role in ("leader", "follower"):
            self.assertNotEqual(self.bus.get_session(dn[role]["endpoint"])["state"]["pair"], n)  # no claim left
        for sid in ("cl", "cx"):
            self.assertIn(self.own(sid)["state"], ("native", "released"))
        for role in ("leader", "follower"):  # the other team: still claimed and running
            self.assertEqual(self.bus.get_session(do[role]["endpoint"])["state"]["pair"], o)
        self.assertFalse(self.bus.pair_state(o)["terminal"])
        self.host.release(o)
