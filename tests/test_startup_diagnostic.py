"""Structured stderr fields and the startup-only diagnostic.

Native permission errors are reduced IN MEMORY to a fixed errno, a fixed syscall and a predefined
path role; the target text is never kept. An error is "matched" only when an attributed OS denial
names the same known role with an operation that syscall needs.

Startup-only: one fresh client under the same gates, argv and sandbox, with NOTHING written to its
input, observed for at most STARTUP_S seconds; a client still waiting is inconclusive.

Parser and role tests need nothing; probe tests use the fake client under the REAL coding sandbox
(skipped where there is none) and the fake collector; the syscall table runs a stand-in program
under the actual profile. No Claude, model or user session.
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import util  # noqa: F401
from test_diagnostic_mode import DiagnosticBase
from test_probe_scripts import SP, FakeCollector

from relay import probes as PR
from relay import toolpolicy as T
from relay.adapters import claude as CL
from relay.adapters.claude import StreamConnection

HOME, WS = "/Users/someone", "/Users/someone/relay-runs/claude-state.AbC/ws"
CTX = {"home": HOME, "ws": WS}


def parse(*lines, role_of=lambda p: SP.path_role(p, CTX)):
    d = PR.StderrDiagnostics(role_of=role_of)
    for line in lines:
        d._line(line.encode())
    d._flush()
    return d.summary()


def entries(summary):
    return [(s["errno"], s["syscall"], s["role"], s["count"]) for s in summary["structured"]]


class StructuredParseTests(unittest.TestCase):
    def test_native_formats_give_errno_syscall_and_role(self):
        cases = [
            (f"EPERM: operation not permitted, scandir '{HOME}'", ("EPERM", "scandir", "home_root")),
            (f"Error: EACCES: permission denied, open '{HOME}/.claude.json'", ("EACCES", "open", "client_config")),
            (f"EPERM: operation not permitted, lstat `{HOME}/relay-runs`", ("EPERM", "lstat", "relay_runs_folder")),
            (f"[Errno 1] Operation not permitted: '{os.path.dirname(WS)}'", ("EPERM", "unknown", "run_folder")),
            (f"[Errno 13] Permission denied: '{HOME}/.claude/settings.json'", ("EACCES", "unknown", "client_settings")),
            (f"ls: {HOME}/.claude: Operation not permitted", ("EPERM", "unknown", "client_state_dir")),
            (f"cannot open '{HOME}/.claude/plugins/installed_plugins.json': Permission denied",
             ("EACCES", "unknown", "plugins")),
            ("uncaught EPERM somewhere", ("EPERM", "unknown", "unknown")),
        ]
        for line, want in cases:
            with self.subTest(line):
                self.assertEqual(entries(parse(line)), [(*want, 1)])

    def test_an_inspected_property_block_is_one_record(self):
        # Bun/Node print the error, then its properties: one error, counted once.
        got = parse(f"EPERM: operation not permitted, scandir '{HOME}'", f'    path: "{HOME}",',
                    '  syscall: "scandir",', "    errno: -1,", '     code: "EPERM"')
        self.assertEqual(entries(got), [("EPERM", "scandir", "home_root", 1)])
        # properties alone (no head line) still make a record
        got = parse('code: "EACCES"', 'syscall: "mkdir"', f"path: '{WS}/x'")
        self.assertEqual(entries(got), [("EACCES", "mkdir", "workspace", 1)])

    def test_a_contradicting_property_starts_another_record(self):
        got = parse(f"{HOME}: Operation not permitted", 'code: "EACCES"', 'syscall: "lstat"', f'path: "{HOME}"')
        self.assertEqual(entries(got), [("EACCES", "lstat", "home_root", 1), ("EPERM", "unknown", "home_root", 1)])

    def test_properties_far_from_their_error_are_not_merged(self):
        got = parse(f"EPERM: operation not permitted, open '{HOME}'", *["noise"] * 9, 'syscall: "scandir"')
        self.assertEqual(entries(got), [("EPERM", "open", "home_root", 1)])  # the stray property names no error

    def test_directory_and_file_operations_stay_distinct(self):
        got = parse(f"EPERM: operation not permitted, scandir '{os.path.dirname(WS)}'",
                    f"EPERM: operation not permitted, open '{os.path.dirname(WS)}'",
                    f"EPERM: operation not permitted, open '{HOME}/.claude.json'")
        self.assertEqual(entries(got), [("EPERM", "open", "client_config", 1), ("EPERM", "open", "run_folder", 1),
                                        ("EPERM", "scandir", "run_folder", 1)])

    def test_unsupported_errors_syscalls_and_targets_stay_unknown(self):
        got = parse("ENOENT: no such file or directory, open '/x'",           # not a permission error
                    "EPERM: operation not permitted, chown '/Users/other/SECRET'",  # unknown syscall, other path
                    "EACCES: permission denied, open 'relative/SECRET'",      # not absolute
                    "EACCES: permission denied, open '~/.claude.json'")       # home shorthand is mapped
        self.assertEqual(entries(got), [("EACCES", "open", "client_config", 1), ("EACCES", "open", "unknown", 1),
                                        ("EPERM", "unknown", "other", 1)])

    def test_no_text_path_or_token_survives(self):
        got = parse(f"EACCES: permission denied, open '{HOME}/SECRET-dir/sk-ant-api03-SECRET'",
                    f'path: "{HOME}/.claude/.credentials.json"', '{"oauthAccount": "SECRET"}')
        blob = json.dumps(got)
        for leak in ("SECRET", HOME, "sk-ant", "credentials", "oauthAccount", "/"):
            self.assertNotIn(leak, blob)
        self.assertEqual(set(got), {"captured", "classes", "lines", "bytes", "truncated", "eof", "capture_error",
                                    "structured", "structured_dropped"})
        for s in got["structured"]:
            self.assertIn(s["errno"], PR.ERRNOS)
            self.assertIn(s["syscall"], PR.SYSCALLS)
            self.assertIn(s["role"], PR.PATH_ROLES)

    def test_a_failing_mapper_gives_an_unknown_role(self):
        def boom(p):
            raise ValueError(p)
        got = parse(f"EPERM: operation not permitted, open '{HOME}'", role_of=boom)
        self.assertEqual(entries(got), [("EPERM", "open", "unknown", 1)])
        self.assertIsNone(got["capture_error"])
        got = parse(f"EPERM: operation not permitted, open '{HOME}'", role_of=lambda p: "MADE-UP")
        self.assertEqual(entries(got), [("EPERM", "open", "unknown", 1)])

    def test_distinct_entries_are_capped_and_the_rest_counted(self):
        lines = [f"EPERM: operation not permitted, {c} '{HOME}'" for c in PR.SYSCALLS[:-1]] * 3
        got = parse(*lines, role_of=lambda p: "home_root")
        self.assertEqual(len(got["structured"]), len(PR.SYSCALLS) - 1)
        with mock.patch.object(PR.StderrDiagnostics, "MAX_STRUCTURED", 3):
            got = parse(*lines, role_of=lambda p: "home_root")
        self.assertEqual(len(got["structured"]), 3)
        self.assertEqual(sum(s["count"] for s in got["structured"]) + got["structured_dropped"], len(lines))

    def test_only_the_retained_part_is_parsed_when_truncated(self):
        r, w = os.pipe()
        d = PR.StderrDiagnostics(role_of=lambda p: "home_root")
        with mock.patch.object(PR.StderrDiagnostics, "RETAIN_BYTES", 200):
            d.start(os.fdopen(r, "rb", buffering=0))
            head = f"EPERM: operation not permitted, scandir '{HOME}'\n".encode()
            os.write(w, head + b"x" * 300 + b"\n" + f"EACCES: permission denied, open '{HOME}'\n".encode())
            os.close(w)
            d.finish()
        s = d.summary()
        self.assertTrue(s["truncated"] and s["eof"])
        self.assertEqual(entries(s), [("EPERM", "scandir", "home_root", 1)])  # the later error wasn't parsed
        self.assertEqual(s["bytes"], len(head) + 301 + len(f"EACCES: permission denied, open '{HOME}'\n"))


class PathRoleTests(unittest.TestCase):
    def test_each_predefined_role(self):
        run = os.path.dirname(WS)
        cases = {HOME: "home_root", WS: "workspace", WS + "/a/b": "workspace", run: "run_folder",
                 os.path.dirname(run): "relay_runs_folder", "/Users": "workspace_ancestor_other",
                 "/": "workspace_ancestor_other", HOME + "/.claude": "client_state_dir",
                 HOME + "/.claude.json": "client_config", HOME + "/.claude/settings.json": "client_settings",
                 HOME + "/.claude/plugins": "plugins", HOME + "/.claude/plugins/installed_plugins.json": "plugins",
                 # (CTX has no private folder: another relay-impl folder is NOT this run's)
                 os.path.join(SP.TEMP_ROOT, "relay-impl-abc", "x"): "system_temp_other",
                 "/tmp/claude": "client_default_temp", "/private/var/folders/zz/T/other": "system_temp_other",
                 "/dev/dtracehelper": "device",
                 HOME + "/.claude/projects/x": "other", HOME + "/Documents": "other", "/etc/hosts": "other",
                 "": "unknown", "relative": "unknown", None: "unknown"}
        for path, role in cases.items():
            with self.subTest(path):
                self.assertEqual(SP.path_role(path, CTX), role)
        self.assertLessEqual(set(cases.values()), set(PR.PATH_ROLES))

    def test_correlation_needs_the_same_known_role_and_a_matching_operation(self):
        ro = {("home_root", "read_data"): 1, ("run_folder", "read_metadata"): 2, ("other", "read_data"): 1}
        s = lambda e, c, r: {"errno": e, "syscall": c, "role": r, "count": 1}  # noqa: E731
        got = SP.correlate([s("EPERM", "scandir", "home_root"),    # listing is read_data: matched
                            s("EPERM", "scandir", "run_folder"),   # only metadata was denied there
                            s("EPERM", "stat", "run_folder"),      # stat is metadata: matched
                            s("EPERM", "unknown", "home_root"),    # syscall unknown
                            s("EPERM", "open", "other"),           # role not known
                            s("EPERM", "open", "plugins")], ro)    # no denial for that role
        self.assertEqual([g["correlation"] for g in got],
                         ["matched", "unknown", "matched", "unknown", "unknown", "unknown"])
        self.assertEqual(SP.op_class("file-read-metadata"), "read_metadata")
        self.assertEqual(SP.op_class("file-read-data"), "read_data")
        self.assertEqual(SP.op_class("file-write-create"), "write")
        self.assertEqual(SP.op_class("network-outbound"), "other")


class ProfileSyscallTests(unittest.TestCase):
    """The actual profile, with the probe's candidate, rendered as a live run would (same layout:
    home/relay-runs/<run>/ws), and a stand-in making each syscall: what is denied, and how."""

    STAND_IN = r'''
import errno, json, os, sys
out = {}
def run(f):
    try:
        f(); return "ok"
    except OSError as e:
        return errno.errorcode.get(e.errno, str(e.errno))
for role, p in json.loads(sys.argv[1]).items():
    out[role] = {"stat": run(lambda: os.stat(p)), "scandir": run(lambda: list(os.scandir(p))),
                 "open": run(lambda: os.close(os.open(p, os.O_RDONLY)))}
print(json.dumps(out))
'''

    def test_listing_a_folder_above_the_workspace_is_eperm_but_its_metadata_is_readable(self):
        if T.ImplementerSandbox(T.StateAllowlist(SP.CANDIDATE)).available() is not None:
            self.skipTest("no coding sandbox on this machine")
        with tempfile.TemporaryDirectory() as d:
            d = os.path.realpath(d)
            home = os.path.join(d, "home")
            run = os.path.join(home, "relay-runs", "claude-state.X")
            ws, tmp = os.path.join(run, "ws"), os.path.join(d, "relay-impl-t")
            for p in (ws, tmp, os.path.join(home, ".claude", "plugins")):
                os.makedirs(p)
            for f in (os.path.join(home, ".claude.json"), os.path.join(home, ".claude", "settings.json")):
                with open(f, "w") as fh:
                    fh.write("{}")
            state = T.StateAllowlist(SP.CANDIDATE).resolve(ws, "00000000-0000-4000-8000-000000000000", home)
            prof = T.profile(ws, state=state, tmp=tmp, install=[])
            targets = {"home_root": home, "relay_runs_folder": os.path.dirname(run), "run_folder": run,
                       "client_state_dir": os.path.join(home, ".claude"),
                       "plugins": os.path.join(home, ".claude", "plugins"), "workspace": ws,
                       "client_config": os.path.join(home, ".claude.json"),
                       "client_settings": os.path.join(home, ".claude", "settings.json")}
            r = subprocess.run([T.ImplementerSandbox.EXE, "-p", prof, sys.executable, "-I", "-c", self.STAND_IN,
                                json.dumps(targets)], capture_output=True, timeout=30, stdin=subprocess.DEVNULL, cwd=ws)
            self.assertEqual(r.returncode, 0, r.stderr.decode(errors="replace")[-300:])
            got = json.loads(r.stdout.decode().strip().splitlines()[-1])
        for role in ("home_root", "relay_runs_folder", "run_folder", "client_state_dir", "plugins"):
            with self.subTest(role):
                self.assertEqual(got[role], {"stat": "ok", "scandir": "EPERM", "open": "EPERM"})
        self.assertEqual(got["workspace"], {"stat": "ok", "scandir": "ok", "open": "ok"})
        for role in ("client_config", "client_settings"):  # the two granted literals: readable files
            self.assertEqual((got[role]["stat"], got[role]["open"]), ("ok", "ok"))


class StartupBase(DiagnosticBase):
    def startup(self, collector=None, *, window=None, patches=(), **sc):
        """Run main --startup-only; count submissions (there must be none)."""
        self.run_dir = os.path.realpath(os.path.join(self.tmp, "run-" + os.urandom(3).hex()))
        os.makedirs(self.run_dir)
        self.window = SP.STARTUP_S if window is None else window
        self.scene(home=self.home, exit_on_input=42, **sc)  # any input at all -> the client exits 42
        self.col = collector or FakeCollector()
        self.submits = 0

        def submit(conn, *a, **kw):
            self.submits += 1
            raise AssertionError("startup-only must never submit")
        ctx = [mock.patch.object(StreamConnection, "submit", submit), *patches]
        if window is not None:
            ctx.append(mock.patch.object(SP, "STARTUP_S", window))
        for p in ctx:
            p.start()
        try:
            t0 = time.monotonic()
            code = SP.main(["--run-dir", self.run_dir, "--deadline", "60", "--startup-only"],
                           version_of=lambda: SP.WANT_CLAUDE, collector=self.col, home=self.home)
            self.wall = time.monotonic() - t0
        finally:
            for p in reversed(ctx):
                p.stop()
        with open(os.path.join(self.run_dir, SP.EVIDENCE)) as f:
            raw = f.read()
        return code, json.loads(raw), raw

    def check_startup(self, code, ev, raw, *, exit_code, result):
        self.assertEqual(code, exit_code, ev.get("hard_stops"))
        self.assertTrue(ev["startup_only"] and ev["diagnostic_only"])
        self.assertEqual(ev["startup_result"], result)
        self.assertEqual((ev["prompts"], ev["submitted_attempts"], ev["model_turns"], self.submits), (0, 0, 0, 0))
        self.assertEqual(ev["window_s"], self.window)  # (5 s unless a test shortens it)
        self.assertEqual(len(ev["turns"]), 1)
        t = ev["turns"][0]
        self.assertEqual((t["mode"], t["submitted"], t["ok"]), ("startup", False, False))
        self.assertNotEqual((ev.get("exit") or {}).get("final_status"), 42)  # nothing reached its input
        self.assertEqual(len(self.col.pids.get("client", [])), 1)
        self.assertEqual(ev["candidate"], SP.CANDIDATE)
        self.assertEqual(ev["candidate_sha256"], T.StateAllowlist(SP.CANDIDATE).digest())
        self.assertFalse(os.path.exists(os.path.join(self.run_dir, f"claude-state-{SP.VERSION}.proposed.json")))
        for leak in ("SECRET", self.home):
            self.assertNotIn(leak, raw)
        self.assertTrue(self.col.stopped)

    def check_clean(self, ev):
        self.assertEqual(ev["cleanup"], {"exit_confirmed": True, "tmp_removed": True})
        self.assertTrue(ev["collector_exit_confirmed"])


class StartupOnlyTests(StartupBase):
    def test_an_early_natural_error_is_structured_and_matched_to_its_denial(self):
        col = FakeCollector(raw=[{"op": "file-read-data", "target": "{home}"},
                                 {"op": "file-write-data", "target": "/dev/dtracehelper"}])
        code, ev, raw = self.startup(col, exit_at_start=1,
                                     stderr_lines=["EPERM: operation not permitted, scandir '<HOME>'"])
        self.check_startup(code, ev, raw, exit_code=3, result="structured_error_before_input")
        self.check_clean(ev)
        self.assertTrue(ev["conclusive"])
        self.assertEqual(ev["termination"], "eof")
        self.assertEqual((ev["exit"]["cause"], ev["exit"]["status_before_cleanup"], ev["exit"]["probe_signals"]),
                         ("natural_exit", 1, []))
        self.assertEqual(ev["stderr"]["structured"], [{"errno": "EPERM", "syscall": "scandir", "role": "home_root",
                                                       "count": 1, "correlation": "matched"}])
        self.assertEqual(ev["denied_roles"], [{"role": "device", "op_class": "write", "count": 1},
                                              {"role": "home_root", "op_class": "read_data", "count": 1}])

    def test_a_structured_error_without_a_matching_denial_stays_unknown(self):
        code, ev, raw = self.startup(FakeCollector(raw=[{"op": "file-read-metadata", "target": "{home}"}]),
                                     exit_at_start=1, stderr_lines=["EPERM: operation not permitted, open '<RUN>'"])
        self.check_startup(code, ev, raw, exit_code=3, result="structured_error_before_input")
        self.assertEqual([(s["role"], s["correlation"]) for s in ev["stderr"]["structured"]],
                         [("run_folder", "unknown")])

    def test_an_exit_without_a_structured_error(self):
        code, ev, raw = self.startup(exit_at_start=2, stderr_lines=["Not logged in · Please run /login"])
        self.check_startup(code, ev, raw, exit_code=3, result="exit_before_input_unstructured")
        self.assertEqual(ev["stderr"]["classes"], {"auth_bootstrap": 1})
        self.assertEqual(ev["stderr"]["structured"], [])

    def test_a_client_waiting_for_input_is_inconclusive_and_cleaned_up(self):
        # It reads its input and gets nothing; closing that input at cleanup ends it (status 0,
        # not 42: nothing was ever written).
        code, ev, raw = self.startup(FakeCollector(raw=[{"op": "file-read-data", "target": "{home}"}]), window=1.0)
        self.check_startup(code, ev, raw, exit_code=3, result="waiting_for_input")
        self.check_clean(ev)
        self.assertFalse(ev["conclusive"])
        self.assertEqual(ev["termination"], "window_elapsed")
        self.assertEqual(ev["turns"][0]["outcome"], "waiting")
        x = ev["exit"]
        self.assertEqual((x["eof_observed"], x["status_before_cleanup"], x["final_status"], x["probe_signals"],
                          x["cause"]), (False, None, 0, [], "unknown"))
        self.assertLess(self.wall, 20.0)

    def test_a_waiting_client_that_ignores_its_closed_input_is_stopped_and_labelled(self):
        code, ev, raw = self.startup(window=1.0, linger=60)
        self.check_startup(code, ev, raw, exit_code=3, result="waiting_for_input")
        self.check_clean(ev)
        x = ev["exit"]
        self.assertEqual((x["status_before_cleanup"], x["probe_signals"][0], x["cause"]),
                         (None, "SIGTERM", "probe_termination"))
        self.assertLess(x["final_status"], 0)

    def test_the_window_is_five_seconds(self):
        self.assertEqual(SP.STARTUP_S, 5.0)

    def test_a_hard_stop_still_stops(self):
        col = FakeCollector(script=[("network-outbound", "1.2.3.4:80")])
        code, ev, raw = self.startup(col, window=3.0)
        self.check_startup(code, ev, raw, exit_code=2, result="stopped")
        self.assertIn("denial outside the allowed observation areas: network port 80", ev["hard_stops"])
        self.assertFalse(ev["conclusive"])
        self.check_clean(ev)

    def test_an_interruption_is_recorded_and_cleaned_up(self):
        real_scan = SP.Scanner.scan
        calls = {"n": 0}

        def scan(self_, ctxs, current):
            calls["n"] += 1
            if calls["n"] == 3:
                raise KeyboardInterrupt()
            return real_scan(self_, ctxs, current)
        code, ev, raw = self.startup(patches=[mock.patch.object(SP.Scanner, "scan", scan)])
        self.assertEqual((code, ev["termination"], ev["error"]), (2, "interrupted", "KeyboardInterrupt"))
        self.assertEqual(self.submits, 0)
        self.check_clean(ev)

    def test_both_modes_cannot_be_combined(self):
        self.run_dir = os.path.realpath(os.path.join(self.tmp, "run-both"))
        os.makedirs(self.run_dir)
        code = SP.main(["--run-dir", self.run_dir, "--startup-only", "--diagnostic-only"],
                       version_of=lambda: SP.WANT_CLAUDE, collector=FakeCollector(), home=self.home)
        self.assertEqual(code, 4)
        self.assertEqual(os.listdir(self.run_dir), [])  # nothing started, nothing written


class StartupScopeUnchangedTests(StartupBase):
    def test_startup_only_launches_with_the_same_argv_and_profile_inputs(self):
        seen = []
        real_launch, real_profile = T.ImplementerSandbox.launch, T.profile

        def launch(sb, argv, cwd, session, **kw):
            seen.append({"argv": ["{session}" if a == session else a for a in argv[1:]], "session": session,
                         "sink": kw.get("stderr_sink") is not None})
            return real_launch(sb, argv, cwd, session, **kw)

        def profile(cwd, *, state, tmp, install, runtime=None):
            if not seen or "inputs" in seen[-1]:  # the sandbox's own self-check, not a launch
                return real_profile(cwd, state=state, tmp=tmp, install=install, runtime=runtime)
            s = seen[-1]
            slug = os.path.basename(os.path.dirname(CL.transcript_path(cwd, s["session"])))
            norm = lambda v: v.replace(s["session"], "{session}").replace(cwd, "{ws}").replace(slug, "{slug}")  # noqa: E731
            s["inputs"] = ({k: sorted(norm(p) for p in v) for k, v in state.items()}, install, runtime)
            return real_profile(cwd, state=state, tmp=tmp, install=install, runtime=runtime)
        with mock.patch.object(T.ImplementerSandbox, "launch", launch), mock.patch.object(T, "profile", profile):
            self.diag(FakeCollector(), exit_code=1)
            self.startup(FakeCollector(), exit_at_start=1)
        self.assertEqual(len(seen), 2)
        diag, start = seen
        self.assertEqual(diag["argv"], start["argv"])
        self.assertEqual(diag["inputs"], start["inputs"])
        self.assertTrue(diag["sink"] and start["sink"])
        for flag in ("--restricted", "--strict-mcp-config"):
            self.assertIn(flag, start["argv"])


if __name__ == "__main__":
    unittest.main()
