"""Temp routing.

The installed client takes its temp root from CLAUDE_CODE_TMPDIR, otherwise "/tmp" on macOS (it
ignores TMPDIR there); its sandboxed commands use CLAUDE_CODE_TMPDIR, then CLAUDE_TMPDIR, then
"/tmp/claude" (tests/fixtures/claude-temp-routing.txt). The sandboxed child gets all three names, overriding
anything inherited, pointing at the SAME canonical private folder. Filesystem scope is unchanged:
the private folder works, and sibling or system temp content, protected config and other
conversations stay blocked.

Boundary tests run a stand-in program through the real ImplementerSandbox.launch (the real
profile and environment); the probe tests use the fake client. No Claude, model or user session.
"""

import errno
import hashlib
import itertools
import json
import mmap
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import util  # noqa: F401
from test_probe_scripts import SP, FakeCollector
from test_startup_diagnostic import StartupBase

from relay import toolpolicy as T
from relay.storage import InvalidInput

ROUTED = ("TMPDIR", "CLAUDE_CODE_TMPDIR", "CLAUDE_TMPDIR")
UID = os.getuid()
CLIENT = "/Users/saranyan/.local/share/claude/versions/2.1.296"
EVIDENCE = os.path.join(os.path.dirname(__file__), "fixtures", "claude-temp-routing.txt")
SESSION = "00000000-0000-4000-8000-000000000000"


def client_choice(env: dict) -> dict:
    """The installed client's temp selection on macOS, as the binary's fragments read (checked
    against the binary below): its per-uid folder, and its sandboxed commands' TMPDIR."""
    root = env.get("CLAUDE_CODE_TMPDIR") or "/tmp"
    root = root.rstrip("/") if len(root) > 1 else root
    return {"per_uid": os.path.join(root, f"claude-{UID}"),
            "commands": env.get("CLAUDE_CODE_TMPDIR") or env.get("CLAUDE_TMPDIR") or "/tmp/claude"}


def relay_impl() -> set:
    return {n for n in os.listdir(tempfile.gettempdir()) if n.startswith("relay-impl-")}


class RoutingPolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="routing-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_every_routed_name_names_the_private_folder_whatever_was_inherited(self):
        base = {"TMPDIR": "/private/tmp/", "CLAUDE_CODE_TMPDIR": "/tmp/elsewhere", "CLAUDE_TMPDIR": "/tmp",
                "PATH": "/usr/bin", "TMP": "/tmp"}
        before = dict(base)
        env = T.child_env(self.tmp, base)
        self.assertEqual((env["TMPDIR"], env["CLAUDE_CODE_TMPDIR"], env["CLAUDE_TMPDIR"]),
                         (self.tmp + "/", self.tmp, self.tmp))
        self.assertEqual((env["PATH"], env["TMP"]), ("/usr/bin", "/tmp"))  # nothing else changes
        self.assertEqual(base, before)  # the caller's environment is never modified
        self.assertEqual(T.child_env(self.tmp, {})["CLAUDE_CODE_TMPDIR"], self.tmp)  # nothing inherited

    def test_the_relay_environment_is_not_modified(self):
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_TMPDIR": "/tmp/x"}):
            env = T.child_env(self.tmp)
            self.assertEqual(os.environ["CLAUDE_CODE_TMPDIR"], "/tmp/x")
        self.assertEqual(env["CLAUDE_CODE_TMPDIR"], self.tmp)

    def test_only_a_canonical_absolute_folder_is_accepted(self):
        link = os.path.join(self.tmp, "link")
        os.symlink(self.tmp, link)
        for bad in ("relative", "/", link, self.tmp + "/", self.tmp + "/../" + os.path.basename(self.tmp)):
            with self.subTest(bad), self.assertRaises(InvalidInput):
                T.child_env(bad, {})

    def test_the_client_selection_lands_in_the_private_folder_for_any_inherited_values(self):
        values = (None, "", "/tmp", "/private/tmp/", "/tmp/claude", "/Users/x/elsewhere")
        for combo in itertools.product(values, repeat=3):
            base = {n: v for n, v in zip(ROUTED, combo) if v is not None}
            with self.subTest(base):
                got = client_choice(T.child_env(self.tmp, base))
                self.assertEqual(got["per_uid"], os.path.join(self.tmp, f"claude-{UID}"))
                self.assertEqual(got["commands"], self.tmp)
        # Without routing an inherited CLAUDE_CODE_TMPDIR would win, and TMPDIR alone changes nothing.
        self.assertEqual(client_choice({"CLAUDE_CODE_TMPDIR": "/tmp/elsewhere"})["per_uid"],
                         f"/tmp/elsewhere/claude-{UID}")
        self.assertEqual(client_choice({"TMPDIR": self.tmp + "/"})["per_uid"], f"/tmp/claude-{UID}")

    def test_the_selection_fragments_are_in_the_installed_binary(self):
        with open(EVIDENCE) as f:
            ev = f.read()
        frags = [line.split(": ", 1)[1] for line in ev.splitlines() if line.startswith("- found=yes: ")]
        self.assertEqual(len(frags), 7)
        self.assertNotIn("found=NO", ev)
        if not os.path.exists(CLIENT):
            self.skipTest("the reviewed client isn't installed here")
        with open(CLIENT, "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as m:
            digest = hashlib.sha256(m).hexdigest()
            self.assertIn(digest, ev)  # the same binary the evidence was read from
            for frag in frags:
                with self.subTest(frag[:40]):
                    self.assertNotEqual(m.find(frag.encode()), -1)

    def test_evidence_names_routing_without_paths_or_inherited_values(self):
        base = {"CLAUDE_CODE_TMPDIR": "/tmp/SECRET-inherited", "TMPDIR": "/x/"}
        env = T.child_env(self.tmp, base)
        ev = T.temp_routing_evidence(env, self.tmp, base)
        self.assertEqual(ev, {"sha256": hashlib.sha256(json.dumps(T.TEMP_ROUTING).encode()).hexdigest(),
                              "names": list(ROUTED), "all_private": True,
                              "overridden_inherited": ["CLAUDE_CODE_TMPDIR", "TMPDIR"]})
        raw = json.dumps(ev)
        for leak in (self.tmp, "SECRET", "/x/"):
            self.assertNotIn(leak, raw)
        self.assertFalse(T.temp_routing_evidence(dict(env, CLAUDE_TMPDIR="/tmp"), self.tmp, base)["all_private"])

    def test_the_routing_is_part_of_the_hashed_policy(self):
        rec = T.policy_record(None)
        self.assertEqual(rec["temp_routing"], [["TMPDIR", "{tmp}/"], ["CLAUDE_CODE_TMPDIR", "{tmp}"],
                                               ["CLAUDE_TMPDIR", "{tmp}"]])
        with mock.patch.object(T, "TEMP_ROUTING", T.TEMP_ROUTING[:1]):
            self.assertNotEqual(T.policy_record(None)["sha256"], rec["sha256"])


class SandboxBase(unittest.TestCase):
    def setUp(self):
        self.sb = T.ImplementerSandbox(T.StateAllowlist(SP.CANDIDATE))
        if self.sb.available() is not None:
            self.skipTest("no coding sandbox on this machine")
        tempfile.gettempdir()  # (the relay's own temp root, fixed before any test env change)
        before = relay_impl()
        # registered first, so it runs after every other cleanup: no private folder left behind
        self.addCleanup(lambda: self.assertEqual(relay_impl(), before))
        self.d = os.path.realpath(tempfile.mkdtemp(prefix="routing-"))
        self.addCleanup(shutil.rmtree, self.d, True)
        self.home = os.path.join(self.d, "home")
        self.ws = os.path.join(self.home, "relay-runs", "claude-state.X", "ws")
        os.makedirs(self.ws)
        with open(os.path.join(self.ws, "README.md"), "w") as f:
            f.write("seed\n")

    def launch(self, argv, **kw):
        hold = {}
        proc, ev = self.sb.launch(argv, self.ws, SESSION, claude="/usr/bin/true", home=self.home, hold=hold, **kw)
        return proc, ev, hold


class TempBoundaryTests(SandboxBase):
    """The stand-in does what the client does with its temp root, then tries the boundaries."""

    STAND_IN = r'''
import errno, json, os, sys
t = json.loads(sys.argv[1])
out = {"env": {n: os.environ.get(n) for n in ("TMPDIR", "CLAUDE_CODE_TMPDIR", "CLAUDE_TMPDIR")}}
def run(f):
    try:
        f(); return "ok"
    except OSError as e:
        return errno.errorcode.get(e.errno, str(e.errno))
def wr(p):
    def f():
        with open(p, "w") as fh: fh.write("x")
    return f
def rd(p):
    def f():
        with open(p) as fh: fh.read()
    return f
root = os.environ.get("CLAUDE_CODE_TMPDIR") or "/tmp"
mine = os.path.join(root, "claude-%d" % os.getuid())
def own():  # the client's own check: create 0700, then open it as a directory, no symlink
    os.makedirs(mine, mode=0o700, exist_ok=True)
    os.close(os.open(mine, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW))
out["own_dir"] = run(own)
out["own_write"] = run(wr(os.path.join(mine, "f")))
out["own_read"] = run(rd(os.path.join(mine, "f")))
cmd = os.environ.get("CLAUDE_CODE_TMPDIR") or os.environ.get("CLAUDE_TMPDIR") or "/tmp/claude"
out["commands_write"] = run(wr(os.path.join(cmd, "cmd")))
for k, p in t["read"].items(): out["read:" + k] = run(rd(p))
for k, p in t["write"].items(): out["write:" + k] = run(wr(p))
for k, p in t["list"].items(): out["list:" + k] = run(lambda p=p: os.listdir(p))
print(json.dumps(out))
'''

    def test_private_temp_works_and_every_other_temp_config_and_conversation_stays_blocked(self):
        sibling = tempfile.mkdtemp(prefix="relay-impl-")  # another launch's folder (not ours)
        self.addCleanup(shutil.rmtree, sibling, True)
        with open(os.path.join(sibling, "s"), "w") as f:
            f.write("other launch\n")
        system = tempfile.mkdtemp(prefix="relay-temp-routing-", dir="/private/tmp")  # test-owned
        self.addCleanup(shutil.rmtree, system, True)
        with open(os.path.join(system, "s"), "w") as f:
            f.write("system temp\n")
        claude = os.path.join(self.home, ".claude")
        other = os.path.join(claude, "projects", "-other-conversation")
        os.makedirs(other)
        for p in (os.path.join(self.home, ".claude.json"), os.path.join(claude, "settings.json")):
            with open(p, "w") as f:
                f.write("{}")
        targets = {"read": {"sibling": os.path.join(sibling, "s"), "system": os.path.join(system, "s")},
                   "write": {"sibling": os.path.join(sibling, "new"), "system": os.path.join(system, "new"),
                             "system_root": "/private/tmp/relay-temp-routing-should-not-exist",
                             "client_config": os.path.join(self.home, ".claude.json"),
                             "client_settings": os.path.join(claude, "settings.json"),
                             "other_conversation": os.path.join(other, "x.jsonl")},
                   "list": {"system_root": "/private/tmp", "sibling": sibling}}
        hostile = {"CLAUDE_CODE_TMPDIR": system, "CLAUDE_TMPDIR": "/private/tmp", "TMPDIR": "/private/tmp/"}
        with mock.patch.dict(os.environ, hostile):
            proc, ev, hold = self.launch([sys.executable, "-I", "-c", self.STAND_IN, json.dumps(targets)])
        try:
            proc.stdin.close()
            got = json.loads(proc.stdout.read().decode().strip().splitlines()[-1])
            proc.wait(10)
            real = os.path.realpath(hold["tmp"])
            self.assertTrue(os.path.isfile(os.path.join(real, f"claude-{UID}", "f")))
            self.assertTrue(os.path.isfile(os.path.join(real, "cmd")))
        finally:
            done = T.release_hold(hold)
        self.assertEqual(done, {"exit_confirmed": True, "tmp_removed": True})
        self.assertEqual(got["env"], {"TMPDIR": real + "/", "CLAUDE_CODE_TMPDIR": real, "CLAUDE_TMPDIR": real})
        for k in ("own_dir", "own_write", "own_read", "commands_write"):
            self.assertEqual(got[k], "ok", k)
        blocked = {k: v for k, v in got.items() if k.startswith(("read:", "write:", "list:"))}
        self.assertEqual(set(blocked.values()), {"EPERM"}, blocked)
        self.assertEqual(len(blocked), 10)
        # Nothing leaked out: the hostile root and the system root gained nothing.
        self.assertEqual(sorted(os.listdir(system)), ["s"])
        self.assertFalse(os.path.exists("/private/tmp/relay-temp-routing-should-not-exist"))
        self.assertEqual(sorted(os.listdir(sibling)), ["s"])
        with open(os.path.join(self.home, ".claude.json")) as f:
            self.assertEqual(f.read(), "{}")
        self.assertEqual(os.listdir(other), [])
        r = ev["temp_routing"]
        self.assertEqual((r["all_private"], r["overridden_inherited"]), (True, sorted(ROUTED)))
        self.assertNotIn(real, json.dumps(r))


class LaunchCleanupTests(SandboxBase):
    def test_a_failure_building_the_environment_removes_the_folder_and_starts_nothing(self):
        def boom(*a, **k):
            raise InvalidInput("no")
        with mock.patch.object(T, "child_env", boom), mock.patch.object(T.subprocess, "Popen") as popen:
            with self.assertRaises(InvalidInput):
                self.launch(["/usr/bin/true"])
        popen.assert_not_called()

    def test_a_child_that_cant_be_started_removes_the_folder(self):
        with mock.patch.object(T.subprocess, "Popen", side_effect=OSError(errno.ENOENT, "gone")):
            with self.assertRaises(OSError):
                self.launch(["/usr/bin/true"])

    def test_an_interruption_after_the_child_starts_stops_it_and_removes_the_folder(self):
        seen = {}

        class Interrupting:
            def start(self, f):
                raise KeyboardInterrupt()

            def finish(self):
                seen["finished"] = True
        holder = {}
        real_launch = T.ImplementerSandbox.launch

        def launch(sb, *a, hold=None, **kw):
            holder["hold"] = hold
            return real_launch(sb, *a, hold=hold, **kw)
        with mock.patch.object(T.ImplementerSandbox, "launch", launch):
            with self.assertRaises(KeyboardInterrupt):
                self.launch([sys.executable, "-I", "-c", "import time; time.sleep(60)"], stderr_sink=Interrupting())
        proc = holder["hold"]["proc"]
        proc.stderr.close()  # (this test sink never took the pipe)
        self.assertIsNotNone(proc.poll())  # stopped through its handle
        self.assertTrue(seen.get("finished"))

    def test_a_folder_the_client_made_inside_is_removed_with_it(self):
        code = ("import os; d=os.path.join(os.environ['CLAUDE_CODE_TMPDIR'], 'claude-%d' % os.getuid()); "
                "os.makedirs(os.path.join(d, 'deep'), mode=0o700); open(os.path.join(d, 'deep', 'f'), 'w').close()")
        proc, ev, hold = self.launch([sys.executable, "-I", "-c", code])
        proc.stdin.close()
        self.assertEqual(proc.wait(10), 0)
        self.assertTrue(os.path.isdir(os.path.join(os.path.realpath(hold["tmp"]), f"claude-{UID}", "deep")))
        self.assertEqual(T.release_hold(hold), {"exit_confirmed": True, "tmp_removed": True})


class TempRoleTests(unittest.TestCase):
    TMP = "/private/var/folders/zz/T/relay-impl-THIS"
    CTX = {"home": "/Users/someone", "ws": "/Users/someone/relay-runs/claude-state.AbC/ws", "tmp": TMP}

    def test_the_four_temp_roles(self):
        cases = {self.TMP: "run_private_temp", self.TMP + f"/claude-{UID}/x": "run_private_temp",
                 f"/tmp/claude-{UID}": "client_default_temp", f"/private/tmp/claude-{UID}/a": "client_default_temp",
                 "/tmp/claude": "client_default_temp", "/tmp": "system_temp_root", "/private/tmp": "system_temp_root",
                 SP.TEMP_ROOT: "system_temp_root", "/private/var/folders": "system_temp_root",
                 "/private/var/folders/zz/T/relay-impl-OTHER/x": "system_temp_other",
                 self.TMP + "-sibling": "system_temp_other", "/tmp/claude-0": "system_temp_other",
                 "/private/tmp/other": "system_temp_other"}
        for path, role in cases.items():
            with self.subTest(path):
                self.assertEqual(SP.path_role(path, self.CTX), role)

    def test_this_runs_folder_comes_from_the_turns_own_hold_not_a_stale_context(self):
        new = os.path.realpath(tempfile.mkdtemp(prefix="relay-impl-"))
        stale = dict(self.CTX, tmp=self.TMP)  # an earlier turn's folder, still in ctx at launch
        hold = {"tmp": new}
        role = lambda p: SP.path_role(p, dict(stale, tmp=SP.turn_tmp(hold)))  # noqa: E731 (as run_turn maps)
        self.assertEqual(role(new + "/claude-1"), "run_private_temp")
        self.assertEqual(role(self.TMP), "system_temp_other")
        os.rmdir(new)
        self.assertEqual(role(new), "run_private_temp")  # memoized: still this run's once removed
        self.assertIsNone(SP.turn_tmp({}))


class StartupRoutingTests(StartupBase):
    def test_startup_only_routes_temp_and_labels_errors_by_temp_role(self):
        default = f"/private/tmp/claude-{UID}"
        col = FakeCollector(raw=[{"op": "file-read-data", "target": default}])
        hostile = {"CLAUDE_CODE_TMPDIR": "/private/tmp", "CLAUDE_TMPDIR": "/tmp/claude"}
        with mock.patch.dict(os.environ, hostile):
            code, ev, raw = self.startup(col, exit_at_start=1, stderr_lines=[
                "EPERM: operation not permitted, open '<CTMP>/claude-501'",
                f"EPERM: operation not permitted, open '/tmp/claude-{UID}'"])
        self.check_startup(code, ev, raw, exit_code=3, result="structured_error_before_input")
        self.check_clean(ev)
        got = {(s["role"], s["correlation"]) for s in ev["stderr"]["structured"]}
        self.assertEqual(got, {("run_private_temp", "unknown"), ("client_default_temp", "matched")})
        r = ev["turns"][0]["policy_evidence"]["temp_routing"]
        self.assertEqual((r["all_private"], r["names"]), (True, list(ROUTED)))
        self.assertLessEqual({"CLAUDE_CODE_TMPDIR", "CLAUDE_TMPDIR"}, set(r["overridden_inherited"]))
        for leak in ("relay-impl-", f"claude-{UID}", "<CTMP>"):
            self.assertNotIn(leak, raw)


if __name__ == "__main__":
    unittest.main()
