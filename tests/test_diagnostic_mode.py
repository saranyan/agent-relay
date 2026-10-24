"""Diagnostic-only mode: one fresh conversation, ONE prompt, no
second turn, unchanged scope, at most DIAG_S seconds of observation from submission. Attributed
OS-denied file operations are recorded (still denied); hard safety failures stop at once.

Fake client under the REAL coding sandbox (skipped where there is none), fake collector. No
Claude, model or user session.
"""

import json
import os
import time
import unittest
from unittest import mock

import util  # noqa: F401
from test_probe_scripts import SP, FakeCollector, StateProbeBase

from relay import toolpolicy as T
from relay.adapters.claude import StreamConnection

FILE_DENIALS = [{"op": "file-write-data", "target": ""},                      # unknown: no target
                {"op": "file-write-data", "target": "/dev/dtracehelper"},
                {"op": "file-read-data", "target": "{home}/.CFUserTextEncoding"},
                {"op": "file-read-data", "target": "/private/var/db/timezone/zoneinfo/SECRET"},
                {"op": "file-read-data", "target": "{home}/SECRET-Documents/x"}]


class DiagnosticBase(StateProbeBase):
    def diag(self, collector=None, *, window=None, patches=(), **sc):
        """Run main --diagnostic-only; count submissions and client processes."""
        self.run_dir = os.path.realpath(os.path.join(self.tmp, "run-" + os.urandom(3).hex()))
        os.makedirs(self.run_dir)
        self.scene(**sc)
        self.col = collector or FakeCollector()
        self.submits = 0
        real_submit = StreamConnection.submit

        def submit(conn, *a, **kw):
            self.submits += 1
            return real_submit(conn, *a, **kw)
        ctx = [mock.patch.object(StreamConnection, "submit", submit), *patches]
        if window is not None:
            ctx.append(mock.patch.object(SP, "DIAG_S", window))
        for p in ctx:
            p.start()
        try:
            t0 = time.monotonic()
            code = SP.main(["--run-dir", self.run_dir, "--deadline", "60", "--diagnostic-only"],
                           version_of=lambda: SP.WANT_CLAUDE, collector=self.col, home=self.home)
            self.wall = time.monotonic() - t0
        finally:
            for p in reversed(ctx):
                p.stop()
        with open(os.path.join(self.run_dir, SP.EVIDENCE)) as f:
            raw = f.read()
        return code, json.loads(raw), raw

    def check_common(self, code, ev, raw, *, exit_code, termination):
        self.assertEqual(code, exit_code, ev.get("hard_stops"))
        self.assertTrue(ev["diagnostic_only"])
        self.assertEqual(ev["termination"], termination)
        self.assertEqual(len(ev["turns"]), 1)                      # never a T2
        self.assertEqual(ev["turns"][0]["mode"], "fresh")          # never a resume
        self.assertEqual(self.submits, 1)                          # exactly one submission
        self.assertEqual(ev["submitted_attempts"], 1)
        self.assertEqual(len(self.col.pids.get("client", [])), 1)  # exactly one client process
        self.assertFalse(ev["turns"][0]["ok"])                     # never a successful turn
        self.assertEqual(ev["candidate"], SP.CANDIDATE)            # scope unchanged
        self.assertEqual(ev["candidate_policy"], SP.CANDIDATE)
        self.assertEqual(ev["candidate_sha256"], T.StateAllowlist(SP.CANDIDATE).digest())
        self.assertEqual(ev["turns"][0]["policy_evidence"]["state_allowlist"], ev["candidate_sha256"])
        self.assertFalse(os.path.exists(os.path.join(self.run_dir, f"claude-state-{SP.VERSION}.proposed.json")))
        self.assertNotIn("SECRET", raw)
        self.assertTrue(self.col.stopped)

    def check_clean(self, ev):
        self.assertEqual(ev["cleanup"], {"exit_confirmed": True, "tmp_removed": True})
        self.assertTrue(ev["collector_exit_confirmed"])


class ObservationTests(DiagnosticBase):
    def test_file_denials_are_observed_and_the_run_continues_to_a_later_init(self):
        col = FakeCollector(raw=FILE_DENIALS)
        code, ev, raw = self.diag(col, pre_init_sleep=0.8, hang=30)  # init, then the model "thinks"
        self.check_common(code, ev, raw, exit_code=3, termination="init_verified")
        self.check_clean(ev)
        self.assertEqual(ev["hard_stops"], [])
        self.assertTrue(ev["startup_verified"])
        self.assertEqual(ev["turns"][0]["outcome"], "init_observed")
        self.assertEqual(sorted(ev["init"]["tools"]), sorted(T.CODING_TOOLS))
        cats = {(d["category"], d["op"]) for d in ev["observed_denials"]}
        self.assertEqual(cats, {("unknown: no target", "file-write-data"),
                                ("device: dtracehelper", "file-write-data"),
                                ("runtime: text encoding file", "file-read-data"),
                                ("runtime: time zone data", "file-read-data"),
                                ("home: other", "file-read-data")})
        self.assertEqual(ev["outcome"], "diagnostic_recorded")

    def test_file_denials_then_eof_before_init(self):
        code, ev, raw = self.diag(FakeCollector(raw=FILE_DENIALS), exit_before_init=True)
        self.check_common(code, ev, raw, exit_code=3, termination="eof")
        self.check_clean(ev)
        self.assertFalse(ev["startup_verified"])
        self.assertIsNone(ev["init"])
        self.assertEqual(len(ev["observed_denials"]), len(FILE_DENIALS))

    def test_a_silent_client_is_stopped_at_the_window(self):
        code, ev, raw = self.diag(FakeCollector(raw=FILE_DENIALS), window=1.5, silent=30)
        self.check_common(code, ev, raw, exit_code=3, termination="window_elapsed")
        self.check_clean(ev)
        self.assertLess(ev["turns"][0]["elapsed_s"], 5.0)
        self.assertEqual(ev["turns"][0]["window_s"], 1.5)
        self.assertLess(self.wall, 30.0)

    def test_the_window_is_thirty_seconds_and_inside_the_turn_budget(self):
        self.assertEqual(SP.DIAG_S, 30.0)
        self.assertLess(SP.DIAG_S, SP.TURN_S)


class HardStopTests(DiagnosticBase):
    def assert_hard_stop(self, code, ev, raw, reason):
        self.check_common(code, ev, raw, exit_code=2, termination="hard_stop")
        self.check_clean(ev)
        self.assertIn(reason, ev["hard_stops"])
        self.assertEqual(ev["stops"], ev["hard_stops"])
        self.assertFalse(ev["startup_verified"])

    def test_a_forbidden_network_denial_stops_at_once(self):
        col = FakeCollector(script=[("network-outbound", "1.2.3.4:80")], raw=FILE_DENIALS)
        code, ev, raw = self.diag(col, pre_init_sleep=5)
        self.assert_hard_stop(code, ev, raw, "denial outside the allowed observation areas: network port 80")
        self.assertLess(ev["turns"][0]["elapsed_s"], 4.0)  # didn't wait for the late init
        self.assertNotIn("1.2.3.4", raw)

    def test_a_non_file_denial_outside_the_areas_still_stops(self):
        col = FakeCollector(raw=[{"op": "mach-lookup", "target": "com.SECRET.service"}])
        code, ev, raw = self.diag(col, pre_init_sleep=5)
        self.assert_hard_stop(code, ev, raw, "denial outside the allowed observation areas: "
                                             "unknown: unsupported target format")

    def test_a_report_without_a_timestamp_stops_at_once(self):
        col = FakeCollector(raw=[{"op": "file-read-data", "target": "{home}/x", "ts": None}])
        code, ev, raw = self.diag(col, pre_init_sleep=5)
        self.assert_hard_stop(code, ev, raw, "a report for this probe's process had no usable source timestamp")

    def test_an_unidentified_client_like_process_stops_at_once(self):
        code, ev, raw = self.diag(FakeCollector(stranger=True), pre_init_sleep=5)
        self.assert_hard_stop(code, ev, raw, "a client-like process this probe didn't start was denied")

    def test_an_init_policy_mismatch_stops_at_once(self):
        code, ev, raw = self.diag(FakeCollector(), init_tools=["Read", "Bash"])
        self.assert_hard_stop(code, ev, raw, "client identity or init policy mismatch")
        self.assertEqual(ev["turns"][0]["outcome"], "stopped")

    def test_a_client_reporting_another_conversation_stops_at_once(self):
        code, ev, raw = self.diag(FakeCollector(), fresh_reports="00000000-0000-0000-0000-000000000000")
        self.assert_hard_stop(code, ev, raw, "client identity or init policy mismatch")

    def test_any_tool_use_stops_at_once(self):
        # The init would normally end the window first; it's held unverified here so the tool
        # use that follows is observed, and the hard stop must still win.
        def run_turn(adapter, col, ctx, text, **kw):
            self.scene(ops_fresh=[{"tool": "Write", "path": os.path.join(ctx["ws"], "x.txt"), "text": "x"}])
            return orig(adapter, col, ctx, text, **kw)
        orig = SP.run_turn
        code, ev, raw = self.diag(FakeCollector(), patches=[
            mock.patch.object(SP, "run_turn", run_turn),
            mock.patch.object(SP, "startup_verified", lambda conn, ctx: False)])
        self.assert_hard_stop(code, ev, raw, "a tool was used in the diagnostic turn")

    def test_a_tool_event_recorded_before_verified_startup_still_stops(self):
        # Regression: the init is verified AND the adapter has already recorded a tool
        # use (it arrived while submit waited for the replay), so the window's normal end and
        # the tool record coincide. startup_verified is NOT patched: the safety check must run
        # before the verified-startup exit and win.
        code, ev, raw = self.diag(FakeCollector(), ops_before_replay=[
            {"tool": "Read", "path": "x.txt"}], hang=30)
        # assert_hard_stop's checks, except startup: here it WAS verified, and the record says so
        self.check_common(code, ev, raw, exit_code=2, termination="hard_stop")
        self.check_clean(ev)
        self.assertEqual(ev["hard_stops"], ["a tool was used in the diagnostic turn"])
        self.assertEqual(ev["stops"], ev["hard_stops"])
        self.assertTrue(ev["startup_verified"] and ev["turns"][0]["startup_verified"])
        self.assertEqual(ev["turns"][0]["outcome"], "stopped")


class CleanupAndInterruptTests(DiagnosticBase):
    def test_unconfirmed_client_cleanup_is_exit_5(self):
        real = SP.PR.stop_child

        def stop_child(**kw):
            real(**kw)  # (test hygiene: the client IS reaped) ...
            return "--session-id" not in kw["proc"].args  # only the CLIENT's exit is unconfirmed
        made = []
        real_mkdtemp = T.tempfile.mkdtemp

        def mkdtemp(*a, **kw):
            path = real_mkdtemp(*a, **kw)
            if os.path.basename(path).startswith("relay-impl-"):
                made.append(path)  # the client's private temp folder
            return path
        try:
            code, ev, raw = self.diag(FakeCollector(raw=FILE_DENIALS), pre_init_sleep=0.3, hang=30,
                                      patches=[mock.patch.object(SP.PR, "stop_child", stop_child),
                                               mock.patch.object(T.tempfile, "mkdtemp", mkdtemp)])
            self.assertEqual(len(made), 1)
            self.assertTrue(os.path.isdir(made[0]))  # kept, not removed: the exit is unconfirmed
        finally:
            for t in made:
                T.cleanup_tmp(t)  # (test hygiene)
        self.check_common(code, ev, raw, exit_code=5, termination="init_verified")
        self.assertEqual(ev["outcome"], "cleanup_unconfirmed")
        self.assertFalse(ev["cleanup"]["exit_confirmed"])

    def test_an_interruption_mid_window_is_recorded_and_cleaned_up(self):
        real_scan = SP.Scanner.scan
        calls = {"n": 0}

        def scan(self_, ctxs, current):
            calls["n"] += 1
            if calls["n"] == 3:
                raise KeyboardInterrupt()
            return real_scan(self_, ctxs, current)
        code, ev, raw = self.diag(FakeCollector(raw=FILE_DENIALS), silent=30,
                                  patches=[mock.patch.object(SP.Scanner, "scan", scan)])
        self.assertEqual(code, 2)
        self.assertEqual((ev["termination"], ev["outcome"], ev["error"]), ("interrupted", "interrupted",
                                                                           "KeyboardInterrupt"))
        self.check_clean(ev)
        self.assertEqual(self.submits, 1)
        self.assertFalse(os.path.exists(os.path.join(self.run_dir, f"claude-state-{SP.VERSION}.proposed.json")))


class DefaultModeUnchangedTests(DiagnosticBase):
    def test_without_the_flag_a_file_denial_still_stops_the_turn(self):
        col = FakeCollector(raw=[{"op": "file-read-data", "target": "{home}/.CFUserTextEncoding"}])
        code, raw, prop = self.run_probe(collector=col)
        ev = json.loads(raw)
        self.assertEqual(code, 2)
        self.assertIn("denial outside the allowed observation areas: runtime: text encoding file", ev["stops"])
        self.assertNotIn("diagnostic_only", ev)
        self.assertNotIn("observed_denials", ev)
        self.assertIsNotNone(prop)  # the normal probe still writes its (unaccepted) proposal

    def test_both_modes_launch_with_the_same_argv_and_scope(self):
        seen = []
        real = T.ImplementerSandbox.launch

        def launch(sb, argv, cwd, session, **kw):
            seen.append((["{session}" if a == session else a for a in argv[1:]], sb.state.digest(),
                         sb.state.resolve(cwd, session, kw.get("home"))))
            return real(sb, argv, cwd, session, **kw)
        with mock.patch.object(T.ImplementerSandbox, "launch", launch):
            self.run_probe(collector=FakeCollector(raw=[{"op": "file-read-data", "target": "{home}/x"}]))
            self.diag(FakeCollector(raw=[{"op": "file-read-data", "target": "{home}/x"}]))
        self.assertEqual(len(seen), 2)
        (argv1, dig1, st1), (argv2, dig2, st2) = seen
        self.assertEqual(argv1, argv2)  # the same flags, tools, mode and MCP settings
        for flag in ("--restricted", "--strict-mcp-config"):
            self.assertIn(flag, argv2)
        self.assertEqual(dig1, dig2)
        self.assertEqual({k: len(v) for k, v in st1.items()}, {k: len(v) for k, v in st2.items()})
        self.assertEqual(st1["read_files"], st2["read_files"])  # the two bootstrap literals, both modes

if __name__ == "__main__":
    unittest.main()
