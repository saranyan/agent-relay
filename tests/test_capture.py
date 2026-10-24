"""Capture contract: bounded collector readiness, ONE canary
with a fixed number of denied reads, proof only by an attributed denial, and a redacted record
on every capture-stage end.

The canary is the REAL sandboxed process (skipped where there is no sandbox); the collector is
either the fake (scripted delivery) or the real LogCollector running a fixture command in place
of `log stream`. No Claude, model or user session.
"""

import contextlib
import json
import os
import sys
import time
import unittest
from unittest import mock

import util  # noqa: F401
from test_probe_scripts import SP, FakeCollector, StateProbeBase

HEADER = 'Filtering the log data using "sender == \\"Sandbox\\""'


def fixture(code):
    """A stand-in for `log stream`: a tiny Python program (its output is the fixture)."""
    return SP.LogCollector(cmd=[sys.executable, "-I", "-c", code])


class CaptureStageTests(StateProbeBase):
    """End to end through main: the capture stage decides whether anything of Claude starts."""

    def capture_run(self, col, **patches):
        with mock.patch.multiple(SP, **patches) if patches else contextlib.nullcontext():
            code, raw, prop = self.run_probe(collector=col)
        self.assertIsNotNone(raw, "no evidence was written")
        return code, json.loads(raw), raw, prop

    def assert_nothing_started(self, code, ev, raw, prop, col, failure, *, exit_code=4, captured=False,
                               files_kept=False):
        self.assertEqual(code, exit_code)
        self.assertEqual((ev["stage"], ev["outcome"], ev["capture"]), ("capture", "not_started", captured))
        self.assertEqual(ev["failure"], failure)
        self.assertEqual((ev["claude_processes"], ev["prompts"], ev["model_turns"]), (0, 0, 0))
        self.assertEqual(ev["deadline_s"], 60.0)
        self.assertGreaterEqual(ev["elapsed_s"], 0.0)
        self.assertNotIn("client", col.pids)  # zero Claude clients
        self.assertEqual(os.listdir(os.path.join(self.home, ".claude", "projects")), [])
        self.assertIsNone(prop)  # never a proposed allowlist from a capture-only failure
        for secret in (self.home, self.run_dir, "Filtering"):
            self.assertNotIn(secret, raw)  # no paths, no raw header/log text
        self.assertEqual(os.path.exists(os.path.join(self.run_dir, ".canary")), files_kept)
        self.assertTrue(col.stopped)

    def test_delayed_attachment_misses_one_read_but_the_bounded_canary_is_seen(self):
        # The header comes at once, but reports are delivered only from 0.6 s after start: a single
        # read at the canary's start is lost; later reads of the same canary are attributed.
        col = FakeCollector(ready_after=0.0, attach_after=0.6)
        code, ev, raw, prop = self.capture_run(col)
        self.assertTrue(ev["capture"]["established"])
        self.assertGreaterEqual(ev["capture"]["attributed_reads"], 1)
        self.assertEqual(len(col.pids["canary"]), 1)  # ONE canary process
        self.assertIn("client", col.pids)  # only now may the probe go on to Claude

    def test_a_collector_that_never_gets_ready_starts_nothing(self):
        col = FakeCollector(ready_after=None)
        code, ev, raw, prop = self.capture_run(col, READY_S=0.5)
        self.assert_nothing_started(code, ev, raw, prop, col, "collector_not_ready")
        self.assertFalse(ev["canary"]["started"])

    def test_a_dead_collector_starts_nothing(self):
        col = FakeCollector(dies=True)
        code, ev, raw, prop = self.capture_run(col)
        self.assert_nothing_started(code, ev, raw, prop, col, "collector_exited")
        self.assertFalse(ev["canary"]["started"])

    def test_no_denial_wrong_identity_or_wrong_time_starts_nothing(self):
        for name, col in (("no denial", FakeCollector(see_canary=False)),
                          ("another pid", FakeCollector(canary_pid_offset=1)),
                          ("stale time", FakeCollector(canary_ts_offset=-3600)),
                          ("future time", FakeCollector(canary_ts_offset=3600))):
            with self.subTest(name):
                code, ev, raw, prop = self.capture_run(col)
                self.assert_nothing_started(code, ev, raw, prop, col, "no_attributed_denial")
                self.assertTrue(ev["canary"]["started"])
                self.assertEqual(ev["canary"]["attributed_reads"], 0)
                self.assertEqual(ev["canary"]["reads_planned"], SP.CANARY_READS)
                self.assertEqual(ev["cleanup"], {"canary_exit_confirmed": True, "canary_files_removed": True,
                                                 "collector_exit_confirmed": True})

    def test_an_interruption_during_capture_is_recorded_and_cleaned_up(self):
        col = FakeCollector(interrupt_snapshot=True)
        try:
            code, ev, raw, prop = self.capture_run(col)
        except BaseException as e:  # noqa: BLE001 - before the fix it escaped
            self.fail(f"{type(e).__name__} escaped the capture stage")
        self.assert_nothing_started(code, ev, raw, prop, col, "interrupted")
        self.assertEqual(ev["error"], "KeyboardInterrupt")
        self.assertTrue(ev["cleanup"]["canary_exit_confirmed"] and ev["cleanup"]["canary_files_removed"])

    def test_unconfirmed_collector_cleanup_is_exit_5_not_clean(self):
        col = FakeCollector(see_canary=False, stop_ok=False)
        code, ev, raw, prop = self.capture_run(col)
        self.assert_nothing_started(code, ev, raw, prop, col, "no_attributed_denial", exit_code=5)
        self.assertFalse(ev["cleanup"]["collector_exit_confirmed"])


class CanaryCleanupGateTests(StateProbeBase):
    """Capture is proven, but Claude may start only after the canary's exit is confirmed AND its
    files are removed. Otherwise: no Claude process, the uncertainty kept in
    the capture record, exit 5."""

    capture_run = CaptureStageTests.capture_run
    assert_nothing_started = CaptureStageTests.assert_nothing_started

    def test_unconfirmed_canary_exit_starts_no_claude(self):
        real = SP.PR.stop_child

        def stop_child(**kw):
            real(**kw)  # (test hygiene: the canary IS reaped) ...
            return False  # ... but the probe is told its exit couldn't be confirmed
        with mock.patch.object(SP.PR, "stop_child", stop_child):
            code, ev, raw, prop = self.capture_run(FakeCollector())
        self.assert_nothing_started(code, ev, raw, prop, col=self.col, failure="canary_cleanup_unconfirmed",
                                    exit_code=5, captured=True)
        self.assertEqual(ev["cleanup"], {"canary_exit_confirmed": False, "canary_files_removed": True,
                                         "collector_exit_confirmed": True})
        self.assertGreaterEqual(ev["canary"]["attributed_reads"], 1)

    def test_canary_files_left_behind_start_no_claude(self):
        with mock.patch.object(SP.shutil, "rmtree", lambda *a, **kw: None):
            code, ev, raw, prop = self.capture_run(FakeCollector())
        try:
            self.assert_nothing_started(code, ev, raw, prop, col=self.col, failure="canary_cleanup_unconfirmed",
                                        exit_code=5, captured=True, files_kept=True)
            self.assertEqual(ev["cleanup"], {"canary_exit_confirmed": True, "canary_files_removed": False,
                                             "collector_exit_confirmed": True})
        finally:
            import shutil
            shutil.rmtree(os.path.join(self.run_dir, ".canary"), ignore_errors=True)

    def test_confirmed_cleanup_lets_the_probe_go_on(self):
        code, ev, raw, prop = self.capture_run(FakeCollector())
        self.assertTrue(ev["capture"]["established"])
        self.assertIn("client", self.col.pids)


class CollectorChannelTests(StateProbeBase):
    """The real LogCollector's startup parsing, on fixture commands standing in for `log stream`
    (which prints its header on stdout; checked on the installed macOS, 2026-10-12)."""

    def test_the_stdout_header_means_ready_and_is_not_kept(self):
        col = fixture(f"import time\nprint({HEADER!r}, flush=True)\ntime.sleep(30)")
        col.start()
        try:
            budget = SP.PR.Budget(10.0)
            while col.status() == "starting" and not budget.expired():
                time.sleep(0.02)
            self.assertEqual(col.status(), "ready")
            self.assertEqual(col.snapshot(), [])  # the header is status only, never an event
        finally:
            self.assertTrue(col.stop())

    def test_the_header_alone_never_proves_capture(self):
        col = fixture(f"import time\nprint({HEADER!r}, flush=True)\ntime.sleep(30)")
        col.start()
        try:
            cap = SP.establish_capture(col, self.tmp, SP.PR.Budget(30.0))
        finally:
            col.stop()
        self.assertFalse(cap["established"])
        self.assertEqual(cap["failure"], "no_attributed_denial")
        self.assertTrue(cap["canary_started"] and cap["canary_exit_confirmed"] and cap["canary_files_removed"])

    def test_a_command_that_exits_or_never_reports_is_not_ready(self):
        for name, code, patch, failure in (
                ("exits", "pass", {}, "collector_exited"),
                ("header on stderr only", f"import sys, time\nprint({HEADER!r}, file=sys.stderr, flush=True)\n"
                                          "time.sleep(30)", {"READY_S": 0.5}, "collector_not_ready"),
                ("silent", "import time\ntime.sleep(30)", {"READY_S": 0.5}, "collector_not_ready")):
            with self.subTest(name):
                col = fixture(code)
                col.start()
                try:
                    with mock.patch.multiple(SP, **patch) if patch else contextlib.nullcontext():
                        cap = SP.establish_capture(col, self.tmp, SP.PR.Budget(30.0))
                finally:
                    self.assertTrue(col.stop())
                self.assertEqual((cap["established"], cap["failure"]), (False, failure))
                self.assertFalse(cap["canary_started"])


if __name__ == "__main__":
    unittest.main()
