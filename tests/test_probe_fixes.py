"""Codex rechecks of the probe scripts (2026-10-11): findings A and B-D.

Each test is written to fail on the scripts as they were before the fix. Fakes and mocks only:
no real Codex or Claude, no model, and no signal to any process this test didn't start."""

import json
import math
import os
import signal
import subprocess
import sys
import time
import unittest
from unittest import mock

import util  # noqa: F401
from test_probe_scripts import GOOD_CODEX, RP, SP, FakeCollector, StateProbeBase
from test_resume_writers import CodexBase

from relay.adapters.claude import StreamConnection


class FakeClock:
    """A deterministic monotonic clock: each read advances it by `step`."""

    def __init__(self, start=1000.0, step=0.0):
        self.now, self.step = start, step

    def __call__(self):
        v = self.now
        self.now += self.step
        return v

    def advance(self, s):
        self.now += s


# ---------------------------------------------------------------- A
class KillIdentityTests(unittest.TestCase):
    """A: unknown or unproven identity never authorizes a signal."""

    def attempt(self, recorded, observed):
        with mock.patch.object(RP, "process_start_ms", return_value=observed), \
                mock.patch.object(RP, "gone", return_value=False), \
                mock.patch.object(RP.os, "kill") as kill:
            ok = RP.kill_child({"pid": 12345, "started_ms": recorded})
        return ok, kill

    def test_missing_malformed_nonfinite_or_reused_identity_sends_nothing(self):
        cases = {"both missing (Codex's reproduction)": (None, None), "recorded missing": (None, 1.7e12),
                 "observed missing": (1.7e12, None), "malformed": ("1700000000000", 1.7e12),
                 "bool": (True, True), "nan": (math.nan, math.nan), "inf": (math.inf, math.inf),
                 "zero": (0, 0), "negative": (-5, -5), "reused pid": (1.7e12, 1.7e12 + 4000)}
        for why, (rec, obs) in cases.items():
            with self.subTest(why):
                ok, kill = self.attempt(rec, obs)
                self.assertFalse(ok)  # cleanup unconfirmed, honestly
                signals = [c for c in kill.call_args_list if c.args[1:] != (0,)]
                self.assertEqual(signals, [], why)

    def test_a_retained_handle_is_used_instead_of_the_pid(self):
        p = subprocess.Popen(["/bin/sleep", "30"])
        try:
            # Unknown recorded start: the pid path would refuse, so success proves the handle was used.
            with mock.patch.object(RP, "process_start_ms") as start_of:
                ok = RP.kill_child({"pid": p.pid, "started_ms": None, "proc": p})
            self.assertTrue(ok)
            start_of.assert_not_called()  # no pid-identity lookup at all
            self.assertIsNotNone(p.poll())
        finally:
            if p.poll() is None:
                p.kill()
                p.wait()


class ReviewerLifecycleTests(CodexBase):
    """A plus D for the reviewer: one monotonic deadline from the run's start, validated input,
    and cleanup that survives interruption."""

    def test_the_overall_deadline_includes_setup(self):
        clock = FakeClock()
        waited = []
        real_join = RP.threading.Thread.join

        def version():
            clock.advance(30)  # the precondition checks took 30 s
            return GOOD_CODEX()

        def join(th, timeout=None):
            if th.name == "reviewer-launch":
                waited.append(timeout)
            return real_join(th, timeout)
        with mock.patch.object(RP.threading.Thread, "join", join):
            code, ev = self.run_probe(deadline=90, version_of=version, clock=clock)
        self.assertEqual(code, 0)
        self.assertLessEqual(waited[0], 60.0)  # 90 from the run's start, not after setup

    def test_deadline_input_is_validated_before_anything_starts(self):
        for bad in ("0", "-1", "nan", "inf", "100000", "abc"):
            with self.subTest(bad=bad):
                code, ev = self.run_probe(deadline=bad)
                self.assertEqual((code, ev), (4, None))
                self.assertEqual(self.requests(), [])

    def test_an_interruption_still_stops_the_child_and_writes_evidence(self):
        real_join = RP.threading.Thread.join

        def join(th, timeout=None):
            if th.name == "reviewer-launch":
                raise KeyboardInterrupt
            return real_join(th, timeout)
        try:
            with mock.patch.object(RP.threading.Thread, "join", join):
                code, ev = self.run_probe(hang_start=30)
        except KeyboardInterrupt:
            self.fail("the interruption escaped the probe without cleanup or evidence")
        self.assertEqual(ev["outcome"], "interrupted")
        self.assertTrue(ev["cleanup"]["exit_confirmed"])
        self.assertLessEqual(ev["cleanup"]["seconds"], RP.CLEANUP_S + 1)
        with self.assertRaises(ProcessLookupError):
            os.kill(ev["child_pid"], 0)
        self.assertNotEqual(code, 0)

    def run_probe(self, deadline=30, version_of=GOOD_CODEX, clock=None, **sc):
        self.run_dir = os.path.realpath(os.path.join(self.tmp, "run-" + os.urandom(3).hex()))
        os.makedirs(self.run_dir)
        open(self.log, "w").close()
        self.scene(log_file=self.log, **dict({"start": {}}, **sc))
        kw = {"clock": clock} if clock else {}
        code = RP.main(["--run-dir", self.run_dir, "--deadline", str(deadline)], version_of=version_of, **kw)
        path = os.path.join(self.run_dir, RP.EVIDENCE)
        return code, (json.load(open(path)) if os.path.exists(path) else None)


# ---------------------------------------------------------------- B-D
class ProbeFixBase(StateProbeBase):
    def hello_scene(self, content="hi\n", **sc):
        """The resumed turn writes hello.txt (the workspace is known only inside the run)."""
        orig = SP.run_turn

        def run_turn(adapter, col, ctx, text, **kw):
            if kw.get("resume"):
                self.scene(**dict(sc, ops_resume=[{"tool": "Write", "path": os.path.join(ctx["ws"], "hello.txt"),
                                                   "text": content}]))
            return orig(adapter, col, ctx, text, **kw)
        return mock.patch.object(SP, "run_turn", run_turn)

    def evidence(self):
        return json.load(open(os.path.join(self.run_dir, SP.EVIDENCE)))


class AttributionTests(ProbeFixBase):
    """B: events count only for a positively identified process within its lifetime, by the
    log's own timestamp."""

    def test_an_old_event_with_the_clients_pid_is_not_its_own(self):
        col = FakeCollector(raw=[{"op": "file-read-data", "target": "{home}/.claude.json", "ts_offset": -3600}])
        with self.hello_scene():
            code, raw, _ = self.run_probe(collector=col)
        ev = json.loads(raw)
        self.assertEqual(code, 0, ev.get("stops"))
        self.assertNotIn("client config file", [d["category"] for d in ev["denials"]])
        self.assertGreaterEqual(ev["attribution"]["outside_lifetime"], 1)

    def test_an_event_without_a_source_timestamp_is_ambiguous_and_stops(self):
        col = FakeCollector(raw=[{"op": "file-read-data", "target": "{home}/.claude.json", "ts": None}])
        code, raw, _ = self.run_probe(collector=col)
        ev = json.loads(raw)
        self.assertEqual(code, 2)
        self.assertEqual(len(ev["turns"]), 1)
        self.assertTrue(any("timestamp" in s for s in ev["stops"]))

    def test_a_client_whose_start_cant_be_identified_gets_no_prompt(self):
        real = SP.process_start_ms
        col = FakeCollector()
        with mock.patch.object(SP, "process_start_ms",
                               side_effect=lambda pid: None if pid in col.pids.get("client", []) else real(pid)):
            code, raw, _ = self.run_probe(collector=col)
        ev = json.loads(raw)
        self.assertEqual(code, 2)
        self.assertEqual(ev["turns"][0]["outcome"], "identity_unknown")
        self.assertEqual(ev["model_turns"], 0)  # nothing was submitted
        self.assertTrue(ev["turns"][0]["cleanup"]["exit_confirmed"])

    def test_a_delayed_report_from_the_first_turn_counts_for_it_not_as_a_stranger(self):
        col = FakeCollector()
        orig = SP.run_turn

        def run_turn(adapter, c, ctx, text, **kw):
            if kw.get("resume"):  # T1's process has exited; its report arrives now, stamped in its life
                t1 = c.pids["client"][0]
                c.emit(t1, "file-read-data", os.path.join(ctx["home"], ".claude.json"), time.time() - 0.5)
                self.scene(ops_resume=[{"tool": "Write", "path": os.path.join(ctx["ws"], "hello.txt"), "text": "hi\n"}])
            return orig(adapter, c, ctx, text, **kw)
        with mock.patch.object(SP, "run_turn", run_turn), mock.patch.object(SP, "END_GRACE_S", 30.0):
            code, raw, _ = self.run_probe(collector=col)
        ev = json.loads(raw)
        self.assertEqual(code, 0, ev.get("stops"))
        self.assertIn("client config file", [d["category"] for d in ev["turns"][0]["denials"]])
        self.assertEqual(ev["turns"][1]["unattributed_denials"], 0)

    def test_reused_pids_and_lifetimes_are_matched_exactly(self):
        reg = SP.Attribution()
        reg.add(pid=500, start_ms=100_000, turn=1, role="client")
        reg.end(500, at=150.0)
        reg.add(pid=500, start_ms=200_000, turn=2, role="client")  # the same pid, reused later
        self.assertEqual(reg.owner(500, 120.0)["turn"], 1)
        self.assertEqual(reg.owner(500, 250.0)["turn"], 2)
        self.assertIsNone(reg.owner(500, 50.0))     # before either process
        self.assertIsNone(reg.owner(500, 1.0))      # Codex's reproduction: t=1 for a t=100 start
        self.assertIsNone(reg.owner(500, None))     # no source timestamp
        self.assertIsNone(reg.owner(501, 120.0))

    def test_the_canary_needs_its_own_identity_and_lifetime(self):
        col = FakeCollector(canary_ts_offset=-3600)  # a stale report with the canary's pid
        code, raw, _ = self.run_probe(collector=col)
        self.assertEqual(code, 4)
        self.assertEqual(json.loads(raw)["failure"], "no_attributed_denial")
        self.assertNotIn("client", col.pids)


class SecondTurnTests(ProbeFixBase):
    """C: success needs the verified resumed turn, its Write and the exact file, and full cleanup."""

    def test_completion_without_the_file_is_not_success(self):
        code, raw, _ = self.run_probe()  # Codex's reproduction: the resumed turn writes nothing
        ev = json.loads(raw)
        self.assertEqual(code, 2)
        self.assertNotEqual(ev["outcome"], "completed")
        self.assertFalse(ev["turns"][1]["ok"])

    def test_wrong_content_or_a_link_is_not_success(self):
        with self.hello_scene(content="bye\n"):
            code, raw, _ = self.run_probe()
        self.assertEqual(code, 2)
        outside = os.path.join(self.home, "outside-hi.txt")
        with open(outside, "w") as f:
            f.write("hi\n")
        orig = SP.run_turn

        def run_turn(adapter, col, ctx, text, **kw):
            if kw.get("resume"):  # the file appears as a link to content elsewhere
                os.symlink(outside, os.path.join(ctx["ws"], "hello.txt"))
                self.scene(ops_resume=[])
            return orig(adapter, col, ctx, text, **kw)
        with mock.patch.object(SP, "run_turn", run_turn):
            code, raw, _ = self.run_probe()
        self.assertEqual(code, 2)

    def test_a_resumed_client_reporting_another_conversation_is_not_success(self):
        with self.hello_scene(resume_reports="00000000-0000-0000-0000-000000000000"):
            code, raw, _ = self.run_probe()
        self.assertEqual(code, 2)
        self.assertFalse(json.loads(raw)["turns"][1]["identity_ok"])

    def test_incomplete_cleanup_is_not_success(self):
        import glob
        import shutil
        import tempfile
        before = set(glob.glob(os.path.join(tempfile.gettempdir(), "relay-impl-*")))
        with self.hello_scene(), mock.patch("relay.toolpolicy.cleanup_tmp", lambda p: None):
            code, raw, _ = self.run_probe()
        for left in set(glob.glob(os.path.join(tempfile.gettempdir(), "relay-impl-*"))) - before:
            shutil.rmtree(left, ignore_errors=True)  # what the patched-out cleanup left behind
        ev = json.loads(raw)
        self.assertNotEqual(code, 0)
        self.assertFalse(ev["turns"][0]["cleanup"]["tmp_removed"])

    def test_the_full_success_path_still_passes(self):
        with self.hello_scene():
            code, raw, _ = self.run_probe()
        ev = json.loads(raw)
        self.assertEqual((code, ev["outcome"]), (0, "completed"))
        self.assertTrue(all(t["ok"] for t in ev["turns"]))


class LifecycleTests(ProbeFixBase):
    """D: the live child is owned through every error path, and time is one monotonic budget."""

    def broken(self, where, exc=ValueError("/Users/someone/SECRET")):
        target = {"submit": (StreamConnection, "submit"), "poll": (StreamConnection, "poll"),
                  "classify": (SP, "classify")}[where]
        col = FakeCollector([("file-read-data", "{home}/.claude.json")])
        try:
            with mock.patch.object(*target, side_effect=exc):
                code, raw, _ = self.run_probe(collector=col)
        except BaseException as e:  # noqa: BLE001 - before the fix it escaped
            self.fail(f"{type(e).__name__} escaped the probe without cleanup or evidence")
        ev = json.loads(raw)
        return code, ev, raw, col

    def test_unexpected_errors_still_close_the_child_and_write_evidence(self):
        for where in ("submit", "poll", "classify"):
            with self.subTest(where):
                code, ev, raw, col = self.broken(where)
                self.assertNotEqual(code, 0)
                self.assertEqual(ev["error"], "ValueError")
                self.assertNotIn("SECRET", raw)  # the class only, never the message
                self.assertTrue(ev["cleanup"]["exit_confirmed"])
                self.assertTrue(ev["cleanup"]["tmp_removed"])
                self.assertTrue(col.stopped)
                for pid in col.pids.get("client", []):
                    with self.assertRaises(ProcessLookupError):
                        os.kill(pid, 0)

    def test_an_interruption_is_handled_the_same_way(self):
        code, ev, raw, col = self.broken("poll", KeyboardInterrupt())
        self.assertEqual(ev["outcome"], "interrupted")
        self.assertTrue(ev["cleanup"]["exit_confirmed"])
        self.assertNotEqual(code, 0)

    def test_the_turn_budget_starts_before_submission(self):
        # A client that accepts the prompt and then never answers; the clock moves 50 s per read.
        clock = FakeClock(step=50.0)
        seen = []
        real = StreamConnection.submit

        def submit(conn, text, rid, *, accept_timeout=30.0):
            seen.append(accept_timeout)
            return real(conn, text, rid, accept_timeout=min(accept_timeout, 5.0))
        with mock.patch.object(StreamConnection, "submit", submit):
            code, raw, _ = self.run_probe(clock=clock, hang=600)
        ev = json.loads(raw)
        t1 = ev["turns"][0]
        self.assertEqual(t1["outcome"], "timed_out")
        self.assertLessEqual(seen[0], SP.TURN_S)
        self.assertLessEqual(t1["elapsed_s"], SP.TURN_S + 50.0)  # within one clock step of the budget
        self.assertEqual(len(ev["turns"]), 1)
        self.assertTrue(t1["cleanup"]["exit_confirmed"])

    def test_deadline_input_is_validated(self):
        for bad in ("0", "-5", "nan", "inf", "99999", "x"):
            with self.subTest(bad=bad):
                self.run_dir = os.path.realpath(os.path.join(self.tmp, "run-" + os.urandom(3).hex()))
                os.makedirs(self.run_dir)
                code = SP.main(["--run-dir", self.run_dir, "--deadline", bad], version_of=lambda: SP.WANT_CLAUDE,
                               collector=FakeCollector(), home=self.home)
                self.assertEqual(code, 4)
                self.assertEqual(os.listdir(self.run_dir), [])

    def run_probe(self, collector=None, version_of=None, clock=None, **sc):
        self.run_dir = os.path.realpath(os.path.join(self.tmp, "run-" + os.urandom(3).hex()))
        os.makedirs(self.run_dir)
        self.scene(**sc)
        self.col = collector or FakeCollector()
        kw = {"clock": clock} if clock else {}
        code = SP.main(["--run-dir", self.run_dir, "--deadline", "600"],
                       version_of=version_of or (lambda: SP.WANT_CLAUDE), collector=self.col, home=self.home, **kw)
        path = os.path.join(self.run_dir, SP.EVIDENCE)
        raw = open(path).read() if os.path.exists(path) else None
        return code, raw, None


if __name__ == "__main__":
    unittest.main()
