"""Process attribution.

A client-like sandbox denial from a pid the probe hasn't attributed is judged from pid, start time
and parent pid/start in bounded `ps` snapshots: proven_owned (a verified chain to this run's exact
client) is attributed to that turn; proven_foreign (the process or a verified ancestor was in the
baseline taken before any probe process) is left out of the run's evidence; anything else is
unknown and still stops the run. Only fixed name types and numbers are kept; nothing is signalled.

Ledger tests use a scripted process table; probe tests use the fake client under the real coding
sandbox, real `ps`, and the fake collector. No Claude, model or user session.
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
from test_startup_diagnostic import StartupBase

CLIENT = (100, 1_700_000_000_000)        # this run's client: (pid, start_ms)
PROBE = 50                                # the probe's own pid
S = 1_600_000_000_000                     # an older start (ms)


class Table:
    """A scripted process table: each call returns the next state (None: ps failed)."""

    def __init__(self, *states):
        self.states, self.calls = list(states), 0

    def __call__(self):
        self.calls += 1
        return self.states.pop(0) if len(self.states) > 1 else self.states[0]


def row(ppid, start):
    return {"ppid": ppid, "start_ms": start}


def ledger(*states, clock=None):
    t = iter(clock or [10.0 * (i + 1) for i in range(100)])
    led = SP.ProcessLedger(table=Table(*states), clock=lambda: next(t), self_pid=PROBE)
    return led


BASE = {1: row(0, S), PROBE: row(1, S), 500: row(1, S), 600: row(500, S + 1000)}
LIVE = {**BASE, CLIENT[0]: row(PROBE, CLIENT[1])}
TS = CLIENT[1] / 1000 + 1  # an event time after the client started


def judge(led, pid, ts=TS):
    return led.judge(pid, ts, {CLIENT: 1})[:2]


class LedgerTests(unittest.TestCase):
    def snap(self, *states, clock=None):
        led = ledger(*states, clock=clock or [TS - 100] + [TS + 10 * (i + 1) for i in range(50)])
        led.snapshot(baseline=True)
        for _ in states[1:]:
            led.snapshot()
        return led

    def test_a_preexisting_process_and_its_descendants_are_proven_foreign(self):
        later = {**LIVE, 700: row(600, S + 5000)}  # started after the baseline, from a baseline process
        led = self.snap(BASE, later)
        self.assertEqual(judge(led, 500), ("proven_foreign", "preexisting"))
        self.assertEqual(judge(led, 600), ("proven_foreign", "preexisting"))
        self.assertEqual(judge(led, 700), ("proven_foreign", "preexisting_ancestor"))

    def test_a_child_and_grandchild_of_this_runs_client_are_proven_owned(self):
        t = {**LIVE, 200: row(CLIENT[0], CLIENT[1] + 1000), 201: row(200, CLIENT[1] + 2000)}
        led = self.snap(BASE, t)
        self.assertEqual(led.judge(200, TS + 2, {CLIENT: 1}), ("proven_owned", "client_ancestry", (200, CLIENT[1] + 1000), 1))
        self.assertEqual(judge(led, 201, TS + 2), ("proven_owned", "client_ancestry"))

    def test_a_missed_or_vanished_child_without_proof_is_unknown(self):
        led = self.snap(BASE, LIVE)
        self.assertEqual(judge(led, 300), ("unknown", "no_identity"))  # never seen
        t = {**LIVE, 200: row(CLIENT[0], CLIENT[1] + 1000)}
        led = self.snap(BASE, t, LIVE)  # seen once, then gone
        self.assertEqual(judge(led, 200, TS + 100), ("unknown", "outside_known_lifetime"))  # after last seen

    def test_a_stale_or_reused_pid_is_never_foreign_or_owned(self):
        reused = {**LIVE, 500: row(1, CLIENT[1] + 3000)}  # pid 500 now names another process
        led = self.snap(BASE, reused)
        self.assertEqual(judge(led, 500, CLIENT[1] / 1000 + 3.5), ("unknown", "orphan_or_launchd_parent"))
        led = self.snap(BASE, LIVE, reused)
        # after the old identity's last sighting, only the reused (launchd-parented) one fits
        self.assertEqual(judge(led, 500, TS + 15), ("unknown", "orphan_or_launchd_parent"))
        # the baseline identity no longer covers an event after the pid was reused
        self.assertNotEqual(judge(led, 500, TS + 25)[0], "proven_foreign")

    def test_missing_starts_never_prove_anything(self):
        t = {**LIVE, 300: row(CLIENT[0], None), 301: row(999, CLIENT[1] + 1000)}
        led = self.snap(BASE, t)
        self.assertEqual(judge(led, 300), ("unknown", "missing_start"))
        self.assertEqual(judge(led, 301), ("unknown", "parent_unverified"))  # parent not in the table

    def test_wrong_or_unknown_parents_and_probe_children_stay_unknown(self):
        t = {**LIVE, 
            300: row(1, CLIENT[1] + 1000),                    # launchd: maybe an orphaned child
            301: row(PROBE, CLIENT[1] + 1000),                # the probe's own other child
            302: row(CLIENT[0], CLIENT[1] - 5000),            # "parent" started after it: not its parent
            303: row(CLIENT[0], CLIENT[1] + 1000)}
        led = self.snap(BASE, t)
        self.assertEqual(judge(led, 300), ("unknown", "orphan_or_launchd_parent"))
        self.assertEqual(judge(led, 301), ("unknown", "probe_child"))
        self.assertEqual(judge(led, 302, CLIENT[1] / 1000), ("unknown", "parent_unverified"))
        self.assertEqual(led.judge(303, TS, {(CLIENT[0], CLIENT[1] + 1): 1})[0], "unknown")  # another client start

    def test_reparenting_is_ambiguous(self):
        kid = CLIENT[1] + 1000
        led = self.snap(BASE, {**LIVE, 200: row(CLIENT[0], kid)}, {**LIVE, 200: row(1, kid)})
        self.assertEqual(judge(led, 200, TS), ("unknown", "reparented"))
        led = self.snap(BASE, {**LIVE, 200: row(1, kid)}, {**LIVE, 200: row(CLIENT[0], kid)})
        self.assertEqual(judge(led, 200, TS), ("unknown", "reparented"))

    def test_a_baseline_process_whose_parent_changed_is_not_proven_foreign(self):
        # 600 was in the baseline under 500, then shows launchd as its parent: ambiguous.
        changed = {**LIVE, 600: row(1, S + 1000)}
        led = self.snap(BASE, changed)
        self.assertEqual(judge(led, 600), ("unknown", "reparented"))
        led = self.snap(BASE, changed, BASE)  # (in either order)
        self.assertEqual(judge(led, 600), ("unknown", "reparented"))

    def test_a_later_child_of_a_changed_baseline_process_is_not_proven_foreign(self):
        later = {**LIVE, 600: row(1, S + 1000), 700: row(600, S + 5000)}
        led = self.snap(BASE, later)
        self.assertEqual(judge(led, 700), ("unknown", "reparented"))

    def test_a_stable_baseline_process_and_its_later_child_stay_proven_foreign(self):
        # The guard is kept: seen again unchanged (and a parent start learned late) is still stable.
        late = {**LIVE, 700: row(600, S + 5000)}
        led = self.snap(BASE, LIVE, late, late)
        self.assertEqual(judge(led, 600), ("proven_foreign", "preexisting"))
        self.assertEqual(judge(led, 700), ("proven_foreign", "preexisting_ancestor"))
        self.assertNotIn("reparented", led.ids[(600, S + 1000)])

    def test_without_a_baseline_nothing_is_foreign(self):
        led = ledger(None, LIVE, clock=[TS - 100, TS + 10])
        self.assertFalse(led.snapshot(baseline=True))  # ps failed: no baseline at all
        led.snapshot()
        self.assertEqual((led.baseline, led.errors), (False, 1))
        self.assertEqual(judge(led, 500), ("unknown", "orphan_or_launchd_parent"))
        self.assertEqual({judge(led, p)[0] for p in (500, 600, PROBE)}, {"unknown"})

    def test_name_types_are_fixed(self):
        cases = {"claude": "claude", "2.1.296": "claude", "/x/Claude Helper": "claude", "node": "node",
                 "rg": "rg", "something-claude-ish": "claude", "nodejs": "node", "rgx": "other_client_like",
                 None: "other_client_like"}
        for name, t in cases.items():
            self.assertEqual(SP.name_type(name), t)
            self.assertIn(t, SP.NAME_TYPES)

    def test_a_snapshot_is_bounded(self):
        seen = {}

        def run(cmd, **kw):
            seen.update(kw)
            raise subprocess.TimeoutExpired(cmd, kw["timeout"])
        with mock.patch.object(SP.subprocess, "run", run):
            self.assertIsNone(SP.ps_table())
        self.assertEqual(seen["timeout"], SP.PS_TIMEOUT_S)
        self.assertLessEqual(SP.PS_TIMEOUT_S, 2.0)
        table = SP.ps_table()  # the real one: this process, with a start equal to process_start_ms
        self.assertEqual(SP._ms(table[os.getpid()]["start_ms"]), SP._ms(SP.process_start_ms(os.getpid())))


class ScannerTests(unittest.TestCase):
    def scanner(self, led, events):
        col = FakeCollector()
        col.events = events
        attr = SP.Attribution()
        attr.add(CLIENT[0], CLIENT[1], 1, "client")
        sc = SP.Scanner(col, attr, CLIENT[1] / 1000 - 1, diagnostic=True, ledger=led)
        ctx = {"home": "/Users/someone", "slug": "s", "session": "x", "ws": "/Users/someone/r/ws", "tmp": None}
        sc.scan({1: ctx}, 1)
        return sc

    def ev(self, pid, target="/Users/someone/FOREIGN-SECRET", name="claude-SECRETNAME", ts=TS, count=3):
        return {"pid": pid, "name": name, "op": "file-read-data", "target": target, "count": count, "t": ts, "ts": ts}

    def test_foreign_is_left_out_unknown_stops_owned_is_attributed(self):
        t = {**LIVE, 200: row(CLIENT[0], CLIENT[1] + 1000)}
        led = ledger(BASE, t, clock=[TS - 100, TS + 10, TS + 20, TS + 30])
        led.snapshot(baseline=True)
        led.snapshot()
        sc = self.scanner(led, [self.ev(500), self.ev(200, "/Users/someone/.claude/plugins/p"), self.ev(300)])
        self.assertEqual(sc.verdicts, {"proven_owned": 3, "proven_foreign": 3, "pre_client": 0, "unknown": 3})
        self.assertEqual(sc.stops, ["a client-like process this probe didn't start was denied"])
        self.assertEqual(sc.unattributed, {1: 3})
        cats = {k[0] for k in sc.denials[1]}
        self.assertEqual(cats, {"client plugins"})  # the owned child's denial is this turn's evidence
        roles = set(sc.role_ops[1])
        self.assertEqual(roles, {("plugins", "read_data")})  # the foreign one is nowhere in it
        self.assertTrue(sc.attr.owner(200, TS))
        rec = sc.attribution_record()
        raw = json.dumps(rec)
        for leak in ("SECRET", "/Users", "plugins"):
            self.assertNotIn(leak, raw)
        by = {r["verdict"]: r for r in rec["records"]}
        self.assertEqual(by["proven_foreign"], {"verdict": "proven_foreign", "reason": "preexisting", "name_type": "claude",
                                                "pid": 500, "start_ms": S, "parent_pid": 1, "parent_start_ms": S,
                                                "turn": None, "count": 3, "first_event_ts": round(TS, 3),
                                                "last_event_ts": round(TS, 3)})
        self.assertEqual((by["proven_owned"]["parent_pid"], by["proven_owned"]["parent_start_ms"], by["proven_owned"]["turn"]),
                         CLIENT + (1,))
        self.assertEqual((by["unknown"]["reason"], by["unknown"]["start_ms"], by["unknown"]["parent_pid"]),
                         ("no_identity", None, None))

    def test_an_unidentified_pid_gets_one_event_driven_lookup_per_scan(self):
        led = ledger(BASE, LIVE, clock=[TS - 100] + [TS + 10 * i for i in range(1, 10)])
        led.snapshot(baseline=True)
        calls = led.table.calls
        self.scanner(led, [self.ev(300), self.ev(301), self.ev(302)])
        self.assertEqual(led.table.calls - calls, 1)

    def test_records_are_grouped_and_capped(self):
        led = ledger(BASE, clock=[TS - 100] + [TS + i for i in range(1, 10)])
        led.snapshot(baseline=True)
        events = [self.ev(500, count=1) for _ in range(5)] + [self.ev(10_000 + i, count=1) for i in range(40)]
        sc = self.scanner(led, events)
        rec = sc.attribution_record()
        self.assertEqual(len(rec["records"]), SP.ATTR_RECORDS_MAX)
        self.assertEqual(rec["records_dropped"], 45 - 4 - SP.ATTR_RECORDS_MAX)
        self.assertEqual(rec["records"][0]["count"], 5)
        self.assertEqual(sc.verdicts, {"proven_owned": 0, "proven_foreign": 5, "pre_client": 0, "unknown": 40})

    def test_without_a_ledger_every_unattributed_client_denial_is_unknown(self):
        sc = self.scanner(None, [self.ev(500)])
        self.assertEqual(sc.verdicts["unknown"], 3)
        self.assertTrue(sc.stops)


class Others(FakeCollector):
    """Also reports denials from given pids (the collector sees every sandboxed process), and
    optionally from the client's first child once it exists (found with the REAL ps)."""

    def __init__(self, others=(), child_target=None, **kw):
        super().__init__(**kw)
        self.others, self.child_target, self.child = list(others), child_target, None

    def snapshot(self):
        now = time.time()
        if self.others and self.pids.get("client"):
            for pid, name, target in self.others:
                self.events.append({"pid": pid, "name": name, "op": "file-read-data", "count": 1, "t": now,
                                    "ts": now, "target": target})
            self.others = []
        if self.child_target and self.child is None and self.pids.get("client"):
            kids = REAL_DESCENDANTS(self.pids["client"][0])
            if kids:
                self.child = min(kids)
                self.events.append({"pid": self.child, "name": "2.1.296", "op": "file-read-data", "count": 1,
                                    "t": now, "ts": now, "target": self.child_target})
        return super().snapshot()


REAL_DESCENDANTS = SP.descendants


class ProbeAttributionTests(StartupBase):
    def run_startup(self, col, **sc):
        self.signalled = []
        real_kill = os.kill

        def kill(pid, sig):
            self.signalled.append(pid)
            return real_kill(pid, sig)
        return self.startup(col, patches=[mock.patch.object(os, "kill", kill), *sc.pop("patches", [])], **sc)

    def test_a_preexisting_client_like_process_does_not_stop_the_run_and_is_never_signalled(self):
        other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])  # another app's process
        self.addCleanup(lambda: (other.kill(), other.wait()))
        col = Others(others=[(other.pid, "claude-OTHERAPP", "{home}/OTHER-APP-SECRET".format(home=self.home))])
        code, ev, raw = self.run_startup(col, exit_at_start=1)
        self.check_startup(code, ev, raw, exit_code=3, result="exit_before_input_unstructured")
        self.check_clean(ev)
        pa = ev["process_attribution"]
        self.assertEqual(pa["verdicts"], {"proven_owned": 0, "proven_foreign": 1, "pre_client": 0, "unknown": 0})
        self.assertTrue(pa["baseline"])
        self.assertEqual([(r["verdict"], r["reason"], r["name_type"], r["pid"]) for r in pa["records"]],
                         [("proven_foreign", "preexisting", "claude", other.pid)])
        self.assertEqual(ev["attribution"]["proven_foreign_excluded"], 1)
        self.assertEqual((ev["hard_stops"], ev["turns"][0]["unattributed_denials"]), ([], 0))
        self.assertNotIn("home: other", raw)  # its denial is not this run's evidence
        for leak in ("OTHERAPP", "OTHER-APP-SECRET"):
            self.assertNotIn(leak, raw)
        self.assertIsNone(other.poll())  # still running: nothing touched it
        self.assertNotIn(other.pid, self.signalled)

    def test_a_child_the_descendant_poll_missed_is_proven_owned_and_attributed(self):
        col = Others(child_target="{home}/.claude/plugins/installed_plugins.json")
        col.child_target = col.child_target.format(home=self.home)
        code, ev, raw = self.run_startup(col, window=2.0, spawn_child=True,
                                         patches=[mock.patch.object(SP, "descendants", lambda root: set())])
        self.check_startup(code, ev, raw, exit_code=3, result="waiting_for_input")
        self.check_clean(ev)
        self.assertIsNotNone(col.child)
        pa = ev["process_attribution"]
        self.assertEqual(pa["verdicts"], {"proven_owned": 1, "proven_foreign": 0, "pre_client": 0, "unknown": 0})
        r = pa["records"][0]
        self.assertEqual((r["verdict"], r["reason"], r["pid"], r["parent_pid"], r["turn"]),
                         ("proven_owned", "client_ancestry", col.child, col.pids["client"][0], 1))
        self.assertIn({"category": "client plugins", "count": 1, "op": "file-read-data",
                       "template": "{home}/.claude/plugins/<...>"}, ev["denials"])
        self.assertEqual(ev["hard_stops"], [])

    def test_a_vanished_process_without_proof_still_stops(self):
        gone = subprocess.Popen([sys.executable, "-c", "pass"])
        gone.wait()  # exited before the run: no identity can ever be proven for it
        col = Others(others=[(gone.pid, "2.1.296", "{home}/.claude/plugins/x".format(home=self.home))])
        code, ev, raw = self.run_startup(col, window=2.0)
        self.check_startup(code, ev, raw, exit_code=2, result="stopped")
        self.check_clean(ev)
        self.assertEqual(ev["hard_stops"], ["a client-like process this probe didn't start was denied"])
        self.assertEqual(ev["turns"][0]["unattributed_denials"], 1)
        pa = ev["process_attribution"]
        self.assertEqual(pa["verdicts"], {"proven_owned": 0, "proven_foreign": 0, "pre_client": 0, "unknown": 1})
        self.assertEqual([(r["verdict"], r["reason"], r["start_ms"]) for r in pa["records"]],
                         [("unknown", "no_identity", None)])
        self.assertNotIn(gone.pid, self.signalled)
        own = {p for pids in col.pids.values() for p in pids}  # the probe's own canary and client
        self.assertLessEqual(set(self.signalled), own, col.pids)


if __name__ == "__main__":
    unittest.main()
