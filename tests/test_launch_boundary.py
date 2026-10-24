"""Launch boundary.

Paired wall/monotonic stamps are taken immediately before and after the client's Popen. In the
startup-only diagnostic, an event still unknown after the ancestry check whose SOURCE time is
clearly before the before-Popen stamp (by more than PRE_CLIENT_MARGIN_S, every clock reading
consistent) is "pre_client" setup: left out of the client's safety and evidence, record kept.
At, near or after the bound, without a time, or after a clock change: unknown, and it stops.

Scanner tests use a scripted ledger and stamps; launch tests use a stand-in under the real
sandbox; probe tests use the fake client under the real sandbox. No Claude, model or session.
"""

import json
import os
import subprocess
import sys
import time
import unittest
from unittest import mock

import util  # noqa: F401
from test_probe_scripts import SP, FakeCollector
from test_process_attribution import BASE, CLIENT, LIVE, ledger
from test_startup_diagnostic import StartupBase

from relay import toolpolicy as T

STOP = "a client-like process this probe didn't start was denied"
BOUND = {"wall": CLIENT[1] / 1000 + 5.0, "mono": 1000.0}  # immediately before Popen
AFTER = {"wall": BOUND["wall"] + 0.004, "mono": BOUND["mono"] + 0.004}
RUN = {"wall": BOUND["wall"] - 3.0, "mono": BOUND["mono"] - 3.0}


def now_consistent():
    return {"wall": BOUND["wall"] + 1.0, "mono": BOUND["mono"] + 1.0}


class BoundaryScannerTests(unittest.TestCase):
    def scanner(self, events, *, bound=True, stamp=now_consistent, marks=None):
        led = ledger(BASE, LIVE, clock=[RUN["wall"]] + [BOUND["wall"] + 10 * i for i in range(1, 20)])
        led.snapshot(baseline=True)
        led.snapshot()
        col = FakeCollector()
        col.events = events
        attr = SP.Attribution()
        attr.add(CLIENT[0], CLIENT[1], 1, "client")
        sc = SP.Scanner(col, attr, RUN["wall"], diagnostic=True, ledger=led)
        if bound:
            sc.launch_bound, sc.clock_marks = BOUND, list(marks if marks is not None else [RUN, BOUND, AFTER])
        ctx = {"home": "/Users/someone", "slug": "s", "session": "x", "ws": "/Users/someone/r/ws", "tmp": None}
        with mock.patch.object(T, "stamp", stamp):
            sc.scan({1: ctx}, 1)
        return sc

    def ev(self, ts, pid=300, t=None, target="/Users/someone/.claude/plugins/SECRET"):
        return {"pid": pid, "name": "2.1.296", "op": "file-read-data", "target": target, "count": 2,
                "t": ts if t is None else t, "ts": ts}

    def timing(self, sc):
        return [(r["verdict"], r["reason"], r.get("timing")) for r in sc.attribution_record()["records"]]

    def test_an_unseen_event_clearly_before_spawn_is_pre_client_setup(self):
        sc = self.scanner([self.ev(BOUND["wall"] - 0.5)])
        self.assertEqual(sc.stops, [])
        self.assertEqual(sc.verdicts["pre_client"], 2)
        self.assertEqual(self.timing(sc), [("pre_client", "before_client_spawn", "before_bound")])
        self.assertEqual((sc.denials, sc.role_ops, sc.observed, sc.unattributed), ({}, {}, {}, {}))
        rec = sc.attribution_record()
        self.assertTrue(rec["launch_bound_used"] and rec["clock_consistent"])
        self.assertNotIn("SECRET", json.dumps(rec))

    def test_between_popen_entry_and_return_it_may_be_ours_and_stops(self):
        sc = self.scanner([self.ev((BOUND["wall"] + AFTER["wall"]) / 2)])
        self.assertEqual(sc.stops, [STOP])
        self.assertEqual(self.timing(sc), [("unknown", "no_identity", "at_or_after_bound")])

    def test_equal_just_before_and_just_after_the_bound_all_stop(self):
        for ts, why in ((BOUND["wall"], "at_or_after_bound"), (BOUND["wall"] + 1e-6, "at_or_after_bound"),
                        (BOUND["wall"] - 1e-6, "within_margin"),
                        (BOUND["wall"] - SP.PRE_CLIENT_MARGIN_S, "within_margin"),
                        (BOUND["wall"] + 0.5, "at_or_after_bound")):
            with self.subTest(ts - BOUND["wall"]):
                sc = self.scanner([self.ev(ts)])
                self.assertEqual(sc.stops, [STOP])
                self.assertEqual(self.timing(sc), [("unknown", "no_identity", why)])

    def test_a_missing_source_time_stops(self):
        sc = self.scanner([self.ev(None, t=BOUND["wall"] - 1.0)])
        self.assertEqual(sc.stops, [STOP])
        self.assertEqual(self.timing(sc), [("unknown", "missing_time", "missing_time")])
        sc = self.scanner([self.ev(None)], bound=False)  # in any mode: never silently dropped
        self.assertEqual(sc.stops, [STOP])
        self.assertEqual(sc.before_launch.__name__, "before_launch")
        bare = SP.Scanner(FakeCollector(), SP.Attribution(), 0.0)
        bare.launch_bound, bare.clock_marks = BOUND, [BOUND]
        with mock.patch.object(T, "stamp", now_consistent):
            self.assertEqual(bare.before_launch(None), (False, "missing_time"))
            self.assertEqual(bare.before_launch(float("nan")), (False, "missing_time"))

    def test_delivery_time_is_ignored_only_the_source_time_decides(self):
        late = self.scanner([self.ev(BOUND["wall"] - 0.5, t=BOUND["wall"] + 9.0)])  # delivered late
        self.assertEqual(late.verdicts["pre_client"], 2)
        early = self.scanner([self.ev(BOUND["wall"] + 0.2, t=BOUND["wall"] - 1.0)])  # odd receipt time
        self.assertEqual(early.stops, [STOP])

    def test_a_clock_change_fails_closed_and_stays_failed(self):
        jumped = lambda: {"wall": BOUND["wall"] + 2.0, "mono": BOUND["mono"] + 1.0}  # noqa: E731 wall moved 1 s
        sc = self.scanner([self.ev(BOUND["wall"] - 0.5)], stamp=jumped)
        self.assertEqual(sc.stops, [STOP])
        self.assertEqual(self.timing(sc), [("unknown", "no_identity", "clock_inconsistent")])
        self.assertFalse(sc.attribution_record()["clock_consistent"])
        with mock.patch.object(T, "stamp", now_consistent):  # sticky, even once readings agree again
            self.assertEqual(sc.before_launch(BOUND["wall"] - 0.5), (False, "clock_inconsistent"))
        moved_before = dict(RUN, wall=RUN["wall"] + 0.2)  # the run-start pair already disagreed
        sc = self.scanner([self.ev(BOUND["wall"] - 0.5)], marks=[moved_before, BOUND, AFTER])
        self.assertEqual(self.timing(sc), [("unknown", "no_identity", "clock_inconsistent")])
        broken = self.scanner([self.ev(BOUND["wall"] - 0.5)], marks=[{"wall": None, "mono": 1.0}, BOUND])
        self.assertEqual(broken.stops, [STOP])

    def test_owned_and_foreign_classification_is_unchanged(self):
        kid = CLIENT[1] + 1000
        led_states = (BASE, {**LIVE, 200: {"ppid": CLIENT[0], "start_ms": kid}})
        led = ledger(*led_states, clock=[RUN["wall"]] + [BOUND["wall"] + 10 * i for i in range(1, 20)])
        led.snapshot(baseline=True)
        led.snapshot()
        col = FakeCollector()
        ts = BOUND["wall"] + 0.5
        col.events = [self.ev(ts, pid=500), self.ev(ts, pid=200), self.ev(BOUND["wall"] - 0.5, pid=500)]
        attr = SP.Attribution()
        attr.add(CLIENT[0], CLIENT[1], 1, "client")
        sc = SP.Scanner(col, attr, RUN["wall"], diagnostic=True, ledger=led)
        sc.launch_bound, sc.clock_marks = BOUND, [RUN, BOUND, AFTER]
        ctx = {"home": "/Users/someone", "slug": "s", "session": "x", "ws": "/Users/someone/r/ws", "tmp": None}
        with mock.patch.object(T, "stamp", now_consistent):
            sc.scan({1: ctx}, 1)
        # a pre-existing process stays proven_foreign even before the bound; the child stays owned
        self.assertEqual(sc.verdicts, {"proven_owned": 2, "proven_foreign": 4, "pre_client": 0, "unknown": 0})
        self.assertEqual(sc.stops, [])

    def test_outside_startup_only_there_is_no_boundary_exclusion(self):
        sc = self.scanner([self.ev(BOUND["wall"] - 0.5)], bound=False)
        self.assertEqual(sc.stops, [STOP])
        self.assertFalse(sc.attribution_record()["launch_bound_used"])


class LaunchStampTests(unittest.TestCase):
    def setUp(self):
        self.sb = T.ImplementerSandbox(T.StateAllowlist(SP.CANDIDATE))
        if self.sb.available() is not None:
            self.skipTest("no coding sandbox on this machine")
        import tempfile
        self.d = os.path.realpath(tempfile.mkdtemp(prefix="bound-"))
        self.addCleanup(__import__("shutil").rmtree, self.d, True)
        self.home = os.path.join(self.d, "home")
        self.ws = os.path.join(self.home, "relay-runs", "x", "ws")
        os.makedirs(self.ws)

    def test_the_before_stamp_precedes_popen_and_the_after_stamp_follows_it(self):
        real, seen = subprocess.Popen, {}

        def popen(*a, **kw):
            seen["entry"] = time.monotonic()
            p = real(*a, **kw)
            seen["return"] = time.monotonic()
            return p
        hold = {}
        with mock.patch.object(T.subprocess, "Popen", popen):
            proc, ev = self.sb.launch(["/usr/bin/true"], self.ws, "s", claude="/usr/bin/true", home=self.home, hold=hold)
        proc.stdin.close()
        proc.wait(10)
        self.assertEqual(T.release_hold(hold), {"exit_confirmed": True, "tmp_removed": True})
        b, a = hold["spawn_before"], hold["spawn_after"]
        self.assertLessEqual(b["mono"], seen["entry"])
        self.assertLessEqual(seen["return"], a["mono"])
        self.assertLessEqual(b["wall"], a["wall"])
        self.assertEqual(ev["spawn"], {"before": b, "after": a})

    def test_a_failed_spawn_keeps_both_bounds_and_cleans_up(self):
        hold = {}
        with mock.patch.object(T.subprocess, "Popen", side_effect=OSError(2, "gone")):
            with self.assertRaises(OSError):
                self.sb.launch(["/usr/bin/true"], self.ws, "s", claude="/usr/bin/true", home=self.home, hold=hold)
        self.assertLessEqual(hold["spawn_before"]["mono"], hold["spawn_after"]["mono"])
        self.assertFalse(hold["spawning"])
        self.assertFalse(os.path.exists(hold["tmp"]))

    def test_the_policy_record_is_unchanged_by_launch_stamps(self):
        rec = T.policy_record(T.StateAllowlist(SP.CANDIDATE))
        self.assertEqual(set(rec), {"name", "tools", "permission_mode", "mcp_servers", "protected", "network",
                                    "temp_routing", "runtime_read", "state_allowlist", "argv", "sha256"})


class BoundCollector(FakeCollector):
    """Reports, once the launch bound is known, denials from a vanished pid at given offsets (s)
    from the before-Popen wall stamp (the collector sees every sandboxed process)."""

    def __init__(self, offsets, **kw):
        super().__init__(**kw)
        self.offsets, self.bound, self.after = list(offsets), None, None
        gone = subprocess.Popen([sys.executable, "-c", "pass"])
        gone.wait()
        self.gone = gone.pid

    def snapshot(self):
        if self.bound and self.offsets:
            for off in self.offsets:
                ts = self.bound["wall"] + off if off != "between" else (self.bound["wall"] + self.after["wall"]) / 2
                self.events.append({"pid": self.gone, "name": "2.1.296", "op": "file-read-data", "count": 1,
                                    "t": time.time(), "ts": ts, "target": "/private/tmp/SETUP-SECRET"})
            self.offsets = []
        return super().snapshot()


class ProbeBoundaryTests(StartupBase):
    def boundary_run(self, offsets, **sc):
        col = BoundCollector(offsets)
        real = T.ImplementerSandbox.launch

        def launch(sb, *a, hold=None, **kw):
            time.sleep(0.5)  # setup time between the run's start and the client's spawn
            try:
                return real(sb, *a, hold=hold, **kw)
            finally:
                col.bound, col.after = hold.get("spawn_before"), hold.get("spawn_after")
        return col, *self.startup(col, patches=[mock.patch.object(T.ImplementerSandbox, "launch", launch),
                                                *sc.pop("patches", [])], **sc)

    def check_phases(self, ev):
        ph = ev["phases"]
        order = ["run_start", "capture_ready", "canary_finished", "client_spawn_before", "client_spawn_after"]
        self.assertEqual(sorted(ph), sorted(order))  # (the evidence file is written with sorted keys)
        walls = [ph[k]["wall"] for k in order]
        monos = [ph[k]["mono"] for k in order]
        self.assertEqual(walls, sorted(walls))
        self.assertEqual(monos, sorted(monos))
        return ph

    def test_setup_events_before_spawn_are_excluded_and_the_run_completes(self):
        col, code, ev, raw = self.boundary_run([-0.3], exit_at_start=1)
        self.check_startup(code, ev, raw, exit_code=3, result="exit_before_input_unstructured")
        self.check_clean(ev)
        ph = self.check_phases(ev)
        self.assertEqual(ph["client_spawn_before"], col.bound)
        self.assertEqual(ev["turns"][0]["launch_bounds"], {"spawn_before": col.bound, "spawn_after": col.after})
        pa = ev["process_attribution"]
        self.assertEqual(pa["verdicts"], {"proven_owned": 0, "proven_foreign": 0, "pre_client": 1, "unknown": 0})
        self.assertEqual([(r["verdict"], r["reason"], r["timing"], r["pid"]) for r in pa["records"]],
                         [("pre_client", "before_client_spawn", "before_bound", col.gone)])
        self.assertTrue(pa["launch_bound_used"] and pa["clock_consistent"])
        self.assertEqual(ev["attribution"]["pre_client_excluded"], 1)
        self.assertEqual(ev["hard_stops"], [])
        self.assertNotIn("SETUP-SECRET", raw)
        self.assertNotIn("/private/tmp", raw)  # not this client's evidence

    def test_events_at_between_or_near_the_bound_stop_the_run(self):
        for offsets in ([0.0], ["between"], [-0.01], [0.3]):
            with self.subTest(offsets):
                col, code, ev, raw = self.boundary_run(offsets, window=2.0)
                self.check_startup(code, ev, raw, exit_code=2, result="stopped")
                self.check_clean(ev)
                self.assertEqual(ev["hard_stops"], [STOP])
                pa = ev["process_attribution"]
                self.assertEqual((pa["verdicts"]["pre_client"], pa["verdicts"]["unknown"]), (0, 1))

    def test_an_interrupted_spawn_keeps_truthful_bounds_and_cleans_up(self):
        def start(self_, f):
            raise KeyboardInterrupt()
        col, code, ev, raw = self.boundary_run([], patches=[mock.patch.object(SP.PR.StderrDiagnostics, "start", start)])
        self.assertEqual((code, ev["termination"], ev["error"]), (2, "interrupted", "KeyboardInterrupt"))
        self.assertEqual(self.submits, 0)
        self.check_clean(ev)
        ph = self.check_phases(ev)
        self.assertEqual((ph["client_spawn_before"], ph["client_spawn_after"]), (col.bound, col.after))


if __name__ == "__main__":
    unittest.main()
