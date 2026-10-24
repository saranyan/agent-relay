"""Diagnostic exit and stderr observability.

The process status is read at the observed EOF BEFORE cleanup, then the final reaped status and
what the probe itself did; a probe kill is never reported as the client's own exit. stderr is an
opt-in pipe drained from spawn through the confirmed exit and reduced at once to a FIXED set of
classes: only counts and safe numbers/booleans reach the evidence, never the text.

Fake client under the REAL coding sandbox (skipped where there is none), fake collector. No
Claude, model or user session.
"""

import json
import os
import signal
import unittest
from unittest import mock

import util  # noqa: F401
from test_diagnostic_mode import FILE_DENIALS, DiagnosticBase
from test_probe_scripts import SP, FakeCollector

from relay import probes as PR
from relay import toolpolicy as T

SUMMARY_KEYS = {"captured", "classes", "lines", "bytes", "truncated", "eof", "capture_error",
                "structured", "structured_dropped"}  # (structured enums added by Codex's 2026-10-12 decision)
EXIT_KEYS = {"eof_observed", "status_before_cleanup", "adapter_terminated", "input_closed_by_probe",
             "probe_signals", "final_status", "cause"}


class SinkSpy:
    """Every StderrDiagnostics the probe creates, to check it was finished (drain ended, closed)."""

    def __init__(self):
        self.made = []
        spy = self

        class Spied(PR.StderrDiagnostics):
            def __init__(self, **kw):  # (the probe passes its role mapper)
                super().__init__(**kw)
                spy.made.append(self)
        self.patch = mock.patch.object(SP.PR, "StderrDiagnostics", Spied)

    def assert_finished(self, case):
        case.assertEqual(len(self.made), 1)
        sink = self.made[0]
        case.assertTrue(sink.started and sink.finished)
        case.assertFalse(sink._thread.is_alive())  # the drain ended ...
        case.assertTrue(sink._f.closed)             # ... and its read end was closed
        case.assertTrue(sink.eof)
        case.assertIsNone(sink.capture_error)


class ExitStatusTests(DiagnosticBase):
    def test_a_natural_nonzero_exit_before_init_is_recorded_before_cleanup(self):
        code, ev, raw = self.diag(FakeCollector(raw=FILE_DENIALS), exit_code=7,
                                  stderr_lines=["error: unknown option '--SECRET-flag'"])
        self.check_common(code, ev, raw, exit_code=3, termination="eof")
        self.check_clean(ev)
        self.assertEqual(ev["exit"], {"eof_observed": True, "status_before_cleanup": 7, "adapter_terminated": False,
                                      "input_closed_by_probe": True, "probe_signals": [], "final_status": 7,
                                      "cause": "natural_exit"})
        self.assertEqual(ev["exit"], ev["turns"][0]["exit"])
        self.assertEqual(ev["stderr"]["classes"], {"unsupported_option": 1})
        self.assertTrue(ev["stderr"]["captured"] and ev["stderr"]["eof"])
        self.assertIsNone(ev["init"])

    def test_a_probe_kill_is_labelled_as_the_probes_own_not_the_clients(self):
        # The client stays silent past the window and doesn't exit when its input is closed: the
        # probe kills it. Nothing was known before cleanup, so the cause is the probe's.
        code, ev, raw = self.diag(FakeCollector(), window=1.5, silent=60)
        self.check_common(code, ev, raw, exit_code=3, termination="window_elapsed")
        self.check_clean(ev)
        x = ev["exit"]
        self.assertEqual((x["eof_observed"], x["status_before_cleanup"]), (False, None))
        # The adapter's close() terminates after its own wait; stop_child may still kill before the
        # reap. Either way the final status is the probe's own signal, and is labelled so.
        self.assertEqual(x["probe_signals"][0], "SIGTERM")
        self.assertLessEqual(set(x["probe_signals"]), {"SIGTERM", "SIGKILL"})
        self.assertIn(-x["final_status"], {getattr(signal, n) for n in x["probe_signals"]})
        self.assertEqual(x["cause"], "probe_termination")

    def test_the_adapters_policy_termination_is_not_a_natural_exit(self):
        code, ev, raw = self.diag(FakeCollector(), init_tools=["Read", "Bash"], hang=30)
        self.check_common(code, ev, raw, exit_code=2, termination="hard_stop")
        x = ev["exit"]
        self.assertTrue(x["adapter_terminated"])
        self.assertEqual(x["cause"], "probe_termination")  # even though a status was read before cleanup

    def test_the_exit_record_has_only_the_fixed_fields(self):
        code, ev, raw = self.diag(FakeCollector(), exit_code=1)
        self.assertEqual(set(ev["exit"]), EXIT_KEYS)
        self.assertEqual(set(ev["stderr"]), SUMMARY_KEYS)


class StderrCategoryTests(DiagnosticBase):
    def test_secret_and_path_bearing_stderr_leaves_category_counts_only(self):
        lines = [f"EACCES: permission denied, open '{self.home}/.claude.json'",
                 "Invalid API key sk-ant-api03-SECRETSECRET · Please run /login",
                 f'SyntaxError: JSON Parse error in {self.home}/.claude/settings.json: {{"oauthAccount": "SECRET"}}',
                 "getaddrinfo ENOTFOUND api.SECRET.example",
                 "Human: SECRET transcript snippet",
                 f"dyld: Library not loaded: {self.home}/SECRET.dylib",
                 "", "   "]
        code, ev, raw = self.diag(FakeCollector(), exit_code=1, stderr_lines=lines)
        self.check_common(code, ev, raw, exit_code=3, termination="eof")  # (includes: no "SECRET")
        self.assertEqual(ev["stderr"]["classes"], {"permission_denied": 1, "auth_bootstrap": 1, "config_parse": 1,
                                                   "tls_network": 1, "unclassified": 1, "sandbox_runtime": 1})
        self.assertEqual(ev["stderr"]["lines"], 6)  # blank lines aren't counted
        for leak in (self.home, "sk-ant", "oauthAccount", "Human:", "snippet", "permission denied", "dyld",
                     "ENOTFOUND", "Library"):
            self.assertNotIn(leak, raw)
        # Since Codex's 2026-10-12 structured-field decision, "EACCES" may appear ONLY as the fixed
        # errno enum value of a structured record (turn + top level), never as error text.
        self.assertEqual(ev["stderr"]["structured"], [{"errno": "EACCES", "syscall": "open", "role": "client_config",
                                                       "count": 1, "correlation": "unknown"}])
        self.assertEqual(raw.count("EACCES"), 2)
        self.assertEqual(raw.count('"errno": "EACCES"'), 2)
        self.assertEqual(set(ev["stderr"]["classes"]) - set(PR.STDERR_CLASSES), set())

    def test_oversized_stderr_is_drained_without_hanging(self):
        # 5 MB is far beyond a pipe's buffer: an undrained child would block before its init.
        code, ev, raw = self.diag(FakeCollector(), stderr_bytes=5_000_000, hang=30)
        self.check_common(code, ev, raw, exit_code=3, termination="init_verified")
        self.check_clean(ev)
        s = ev["stderr"]
        self.assertEqual(s["bytes"], 5_000_000)  # every byte read ...
        self.assertTrue(s["truncated"])          # ... but only the first part classified
        self.assertLessEqual(s["lines"], PR.StderrDiagnostics.RETAIN_BYTES // 100 + 1)
        self.assertEqual(set(s["classes"]), {"unclassified"})
        self.assertTrue(s["eof"])
        self.assertLess(self.wall, 25.0)

    def test_the_taxonomy_is_fixed_and_unknown_text_stays_unclassified(self):
        samples = {"permission_denied": "Error: EPERM: operation not permitted, open '/x'",
                   "unsupported_option": "error: unknown option '--nope'",
                   "auth_bootstrap": "Not logged in · Please run /login",
                   "tls_network": "Error: unable to verify the first certificate",
                   "config_parse": "Failed to parse settings",
                   "sandbox_runtime": "Segmentation fault at address 0x0",
                   "unclassified": "zzz something never seen before"}
        self.assertEqual(set(samples), set(PR.STDERR_CLASSES))
        for cls, line in samples.items():
            with self.subTest(cls):
                self.assertEqual(PR.stderr_class(line), cls)


class OwnershipTests(DiagnosticBase):
    def run_failing(self, **kw):
        spy = SinkSpy()
        code, ev, raw = self.diag(patches=[spy.patch, *kw.pop("patches", [])], **kw)
        return spy, code, ev, raw

    def assert_failed_but_released(self, spy, code, ev, raw, error):
        self.assertEqual(code, 2)
        self.assertEqual((ev["termination"], ev["error"]), ("error" if error != "KeyboardInterrupt"
                                                           else "interrupted", error))
        self.check_clean(ev)
        self.assertTrue(self.col.stopped)
        spy.assert_finished(self)
        self.assertNotIn("SECRET", raw)
        self.assertFalse(os.path.exists(os.path.join(self.run_dir, f"claude-state-{SP.VERSION}.proposed.json")))

    def test_a_connection_constructor_failure_releases_the_child_and_the_drain(self):
        def broken(*a, **kw):
            raise RuntimeError("SECRET constructor failure")
        spy, code, ev, raw = self.run_failing(
            stderr_lines=["error: unknown option"], pre_init_sleep=5,
            patches=[mock.patch.object(SP.CL, "StreamConnection", broken)])
        self.assert_failed_but_released(spy, code, ev, raw, "RuntimeError")
        self.assertEqual(ev["turns"], [])  # it failed inside the launch: no turn exists

    def test_a_collector_exception_mid_window_is_cleaned_up(self):
        class Breaks(FakeCollector):
            calls = 0

            def snapshot(self):
                if "client" in self.pids:
                    Breaks.calls += 1
                    if Breaks.calls == 3:
                        raise RuntimeError("SECRET collector failure")
                return super().snapshot()
        spy, code, ev, raw = self.run_failing(collector=Breaks(raw=FILE_DENIALS), silent=30)
        self.assert_failed_but_released(spy, code, ev, raw, "RuntimeError")

    def test_an_interruption_mid_window_finishes_the_drain(self):
        real_scan = SP.Scanner.scan
        calls = {"n": 0}

        def scan(self_, ctxs, current):
            calls["n"] += 1
            if calls["n"] == 3:
                raise KeyboardInterrupt()
            return real_scan(self_, ctxs, current)
        spy, code, ev, raw = self.run_failing(collector=FakeCollector(), silent=30,
                                              stderr_lines=["Not logged in"],
                                              patches=[mock.patch.object(SP.Scanner, "scan", scan)])
        self.assert_failed_but_released(spy, code, ev, raw, "KeyboardInterrupt")
        self.assertEqual(spy.made[0].counts["auth_bootstrap"], 1)  # (in memory only; not in evidence)


class PermissionsUnchangedTests(DiagnosticBase):
    def test_stderr_is_opt_in_and_argv_and_profile_inputs_are_unchanged(self):
        seen = []
        real_launch, real_profile = T.ImplementerSandbox.launch, T.profile

        def launch(sb, argv, cwd, session, **kw):
            seen.append({"argv": ["{session}" if a == session else a for a in argv[1:]],
                         "sink": kw.get("stderr_sink") is not None, "session": session, "cwd": cwd})
            return real_launch(sb, argv, cwd, session, **kw)

        def profile(cwd, *, state, tmp, install, runtime=None, **kw):
            if not seen or "profile_inputs" in seen[-1]:  # the sandbox's own self-check, not a launch
                return real_profile(cwd, state=state, tmp=tmp, install=install, runtime=runtime, **kw)
            s = seen[-1]
            norm = lambda v: v.replace(s["session"], "{session}").replace(cwd, "{ws}")  # noqa: E731
            slug = os.path.basename(os.path.dirname(SP.CL.transcript_path(cwd, s["session"])))
            s["profile_inputs"] = ({k: sorted(norm(p).replace(slug, "{slug}") for p in v) for k, v in state.items()},
                                   install, runtime, sorted(kw))
            return real_profile(cwd, state=state, tmp=tmp, install=install, runtime=runtime, **kw)
        with mock.patch.object(T.ImplementerSandbox, "launch", launch), mock.patch.object(T, "profile", profile):
            self.run_probe(collector=FakeCollector(raw=[{"op": "file-read-data", "target": "{home}/x"}]))
            procs_normal = self.col.pids["client"]
            code, ev, raw = self.diag(FakeCollector(), exit_code=1)
        self.assertEqual(len(seen), 2)
        normal, diag = seen
        self.assertEqual((normal["sink"], diag["sink"]), (False, True))  # opt-in: diagnostic only
        self.assertEqual(normal["argv"], diag["argv"])
        self.assertEqual(normal["profile_inputs"], diag["profile_inputs"])  # the same sandbox rules
        self.assertEqual(len(procs_normal), 1)
        self.check_common(code, ev, raw, exit_code=3, termination="eof")

    def test_the_normal_probe_records_no_exit_or_stderr_fields(self):
        code, raw, prop = self.run_probe(collector=FakeCollector(raw=[{"op": "file-read-data", "target": "{home}/x"}]))
        ev = json.loads(raw)
        self.assertNotIn("stderr", ev)
        self.assertNotIn("exit", ev)
        for t in ev["turns"]:
            self.assertNotIn("stderr", t)
            self.assertNotIn("exit", t)


if __name__ == "__main__":
    unittest.main()
