"""The implementer's coding policy.

Fake clients only. The fake `claude` below performs each scripted tool operation for real
(inside whatever sandbox it was started in), so the OS boundary is tested, not assumed."""

import json
import os
import re
import stat
import subprocess
import sys
import time
import unittest
from unittest import mock

import util  # noqa: F401
from test_resume_writers import CONV, Sim

from relay import toolpolicy as T
from relay.adapters.base import NotSubmitted
from relay.adapters.claude import ClaudeStreamAdapter, resume_argv
from relay.bus import Bus
from relay.handoff import Handoffs, same_process
from relay.prompts import handoff_prompt
from relay.storage import InvalidInput, Unsupported, init_root

STATE = T.StateAllowlist({"write_files": ["{home}/.claude/projects/{slug}/{session}.jsonl"]})
NEEDS_SANDBOX = unittest.skipIf(T.ImplementerSandbox(STATE).available() is not None,
                                "no coding sandbox on this machine")

# A stand-in for Claude Code under the coding argv. Each user message runs one scripted turn:
# init (first, as configured), the replay, one assistant message per op with its tool_use, the
# op performed for real, and a result whose text reports each op's outcome.
FAKE_CODER = r'''
import json, os, re, sys
sc = json.load(open(os.environ["FAKE_SCENARIO"]))
argv = sys.argv[1:]
sid = argv[argv.index("--resume") + 1] if "--resume" in argv else argv[argv.index("--session-id") + 1]
def out(o): print(json.dumps(o), flush=True)
def init():
    out({"type": "system", "subtype": "init", "session_id": sid, "cwd": os.getcwd(),
         "tools": sc.get("init_tools", argv[argv.index("--tools") + 1].split(",")),
         "permissionMode": sc.get("init_mode", argv[argv.index("--permission-mode") + 1]),
         "mcp_servers": sc.get("init_mcp", [])})
first = True
for line in sys.stdin:
    msg = json.loads(line)
    text = msg["message"]["content"][0]["text"]
    m = re.search(r"relay turn id: (u_[0-9a-f]+)", text)
    if first and sc.get("assistant_first"):
        out({"type": "assistant", "session_id": sid, "message": {"content": [{"type": "text", "text": "hi"}]}})
    if first and not sc.get("no_init"):
        init()
    first = False
    out({"type": "user", "uuid": msg["uuid"], "session_id": sid})
    results = []
    for op in sc.get("ops", []):
        out({"type": "assistant", "session_id": sid, "message": {"content": [
            {"type": "tool_use", "id": "t", "name": op["tool"], "input": {"file_path": op["path"], "content": "SECRET-CONTENT"}}]}})
        try:
            if op["tool"] in ("Write", "Edit"):
                with open(op["path"], "w") as f:
                    f.write(op.get("text", "x"))
            elif op["tool"] == "Read":
                open(op["path"]).read()
            results.append([op["label"], "ok"])
        except OSError as e:
            results.append([op["label"], "denied" if e.errno == 1 else f"errno {e.errno}"])
    state = os.path.join(sc["state_dir"], sid + ".jsonl")  # the client's own conversation record
    try:
        with open(state, "a") as f:
            f.write("{}\n")
        results.append(["own state", "ok"])
    except OSError as e:
        results.append(["own state", f"errno {e.errno}"])
    block = {"relay": 1, "turn_id": m.group(1) if m else None, "action": "continue", "kind": "result", "subject": "done"}
    out({"type": "result", "subtype": "success", "is_error": False, "session_id": sid,
         "permission_denials": sc.get("denials", []),
         "result": json.dumps(results) + "\n\n```relay\n" + json.dumps(block) + "\n```\n"})
'''


class PassThrough:
    """TEST ONLY: runs the fake client without an OS sandbox, for the client-protocol (P6)
    checks, which live in the stream connection, not the sandbox."""

    def __init__(self, state=STATE):
        self.state = state

    def available(self):
        return None

    def launch(self, argv, cwd, session, *, claude, home=None, runtime_expected=None, hold=None):
        proc = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, bufsize=0)
        if hold is not None:
            hold.update(proc=proc, spawning=True)
        return proc, {"policy": T.POLICY_NAME, "profile_sha256": "none (test)", "argv_sha256": "x",
                      "state_allowlist": self.state.digest(), "tmp": None}


class CoderBase(Sim):
    def setUp(self):
        super().setUp()
        self.home = os.path.realpath(self.tmp)  # HOME for this test (Sim sets it)
        inst = os.path.join(self.home, ".local", "share", "claude", "versions")
        os.makedirs(inst)
        self.claude = os.path.join(inst, "0.0.0-fake")
        with open(self.claude, "w") as f:
            f.write(f"#!{sys.executable}\n{FAKE_CODER}")
        os.chmod(self.claude, 0o755)
        self.patch = mock.patch("relay.adapters.claude._claude", return_value=self.claude)
        self.patch.start()
        # The fake reads its scenario; under the sandbox only the client's install is readable
        # outside the workspace, so the scenario lives beside the fake client.
        self.scenario = os.path.join(inst, "scenario.json")
        os.environ["FAKE_SCENARIO"] = self.scenario
        self.slug = re.sub(r"[^A-Za-z0-9-]", "-", self.folder)
        self.state_dir = os.path.join(self.home, ".claude", "projects", self.slug)
        os.makedirs(self.state_dir)
        open(os.path.join(self.state_dir, f"{CONV}.jsonl"), "w").close()  # the conversation exists
        with open(os.path.join(self.home, "secret.txt"), "w") as f:
            f.write("not for the implementer\n")
        self.record = T.policy_record(STATE)

    def tearDown(self):
        self.patch.stop()
        super().tearDown()

    def adapter(self, sandbox):
        return ClaudeStreamAdapter({"coding_sandbox": sandbox, "home_for_state": self.home})

    def run_turn(self, sandbox, **sc):
        self.scene(state_dir=self.state_dir, **sc)
        spawned = []
        conn = self.adapter(sandbox).resume(self.folder, CONV, {"writer_policy": self.record},
                                            on_spawn=spawned.append)
        try:
            conn.submit("relay turn id: u_" + "a" * 24, "r1")
        except Exception as e:  # noqa: BLE001 - surfaced to the test with what was seen
            return conn, spawned, e, conn.poll(0.5)
        events, end = [], time.time() + 20
        while time.time() < end and not any(e.type in ("turn_completed", "error", "disconnected") for e in events):
            events += conn.poll(0.2)
        return conn, spawned, None, events


class ArgvAndRecordTests(unittest.TestCase):
    def test_coding_argv_is_fixed_and_the_no_tools_route_is_unchanged(self):
        a = T.coding_argv("claude", resume=CONV)
        self.assertEqual(a[a.index("--tools") + 1], "Read,Edit,Write,Glob,Grep")
        self.assertEqual(a[a.index("--permission-mode") + 1], "acceptEdits")
        for flag in ("--restricted", "--strict-mcp-config"):
            self.assertIn(flag, a)
        self.assertEqual(a[a.index("--permission-prompts") + 1], "none")
        self.assertNotIn("--session-id", a)
        self.assertIn("--session-id", T.coding_argv("claude", session_id="s"))
        with self.assertRaises(InvalidInput):
            T.coding_argv("claude", resume=CONV, session_id="s")
        # no-tools hand-off: byte for byte what it was.
        self.assertEqual(resume_argv("claude", CONV), [
            "claude", "-p", "--resume", CONV, "--input-format", "stream-json", "--output-format", "stream-json",
            "--verbose", "--replay-user-messages", "--permission-prompts", "none", "--restricted",
            "--strict-mcp-config", "--tools", ""])

    def test_the_record_is_hashed_and_changes_with_the_policy(self):
        a = T.policy_record(STATE)
        self.assertEqual(a, T.policy_record(STATE))
        self.assertEqual((a["tools"], a["permission_mode"], a["mcp_servers"]), (list(T.CODING_TOOLS), "acceptEdits", []))
        other = T.StateAllowlist({"write_dirs": ["{home}/.claude"]})
        self.assertNotEqual(a["sha256"], T.policy_record(other)["sha256"])
        with mock.patch.object(T, "CODING_TOOLS", T.CODING_TOOLS + ("Bash",)):
            self.assertNotEqual(a["sha256"], T.policy_record(STATE)["sha256"])

    def test_state_allowlist_is_explicit_pinned_and_inside_home(self):
        self.assertIsNone(T.StateAllowlist.pinned("0.0.0-none"))  # not identified: route unavailable
        self.assertIn("state-path probe", T.ImplementerSandbox(None).available())
        for bad in ({"write_dirs": ["/etc"]}, {"write_dirs": "{home}"}, {"anything": []},
                    {"write_files": ["~/.claude.json"]}):
            with self.subTest(bad=bad), self.assertRaises(InvalidInput):
                T.StateAllowlist(bad)
        with self.assertRaises(InvalidInput):
            T.StateAllowlist({"write_dirs": ["{home}/../x"]}).resolve("/w", "s", "/Users/someone")
        r = STATE.resolve("/Users/someone/proj", "S1", "/Users/someone")
        self.assertEqual(r["write_files"], ["/Users/someone/.claude/projects/-Users-someone-proj/S1.jsonl"])

    def test_audit_records_names_and_relative_paths_only(self):
        e = T.audit_entry({"name": "Edit", "input": {"file_path": "/w/src/a.py", "old_string": "SECRET"}}, "/w")
        self.assertEqual(e, {"tool": "Edit", "path": "src/a.py", "kind": "use"})
        self.assertEqual(T.audit_entry({"name": "Read", "input": {"file_path": "/etc/passwd"}}, "/w")["path"],
                         "outside the workspace")
        self.assertEqual(T.audit_entry({"name": "Grep", "input": {"pattern": "token=.*"}}, "/w")["path"], None)


class RuntimeScopeTests(Sim):
    """Codex R1 (2026-10-11): ONE effective runtime list protects, is hashed, and is used."""

    def extra_runtime(self):
        rt = os.path.realpath(os.path.join(self.tmp, "extra-runtime"))
        os.makedirs(os.path.join(rt, "project"))
        real = T.V.runtime_read
        return rt, mock.patch.object(T.V, "runtime_read", lambda: real() + [rt])

    def test_a_workspace_inside_an_added_runtime_is_refused(self):
        rt, patch = self.extra_runtime()
        home = os.path.realpath(os.path.join(self.tmp, "home-r1"))
        os.makedirs(home)
        T.preflight(os.path.join(rt, "project"), [], home)  # not a runtime yet: fine
        with patch, self.assertRaises(InvalidInput) as cm:  # Codex's reproduction
            T.preflight(os.path.join(rt, "project"), [], home)
        self.assertIn("overlaps", str(cm.exception))
        with patch:
            self.assertIn(rt, T.effective_runtime())
            prof = T.profile(os.path.join(self.tmp, "ws"), state={f: [] for f in T.StateAllowlist.FIELDS},
                             tmp=os.path.join(self.tmp, "t"), install=[])
        self.assertIn(f"(subpath {json.dumps(rt)})", prof)  # the same list reaches the profile

    def test_the_effective_list_is_bound_into_the_team_record(self):
        before = T.policy_record(STATE)
        self.assertEqual(before["runtime_read"], T.effective_runtime())
        rt, patch = self.extra_runtime()
        with patch:
            after = T.policy_record(STATE)
        self.assertIn(rt, after["runtime_read"])
        self.assertNotEqual(before["sha256"], after["sha256"])

    def test_an_existing_team_is_refused_after_the_scope_changes(self):
        record = T.policy_record(STATE)
        rt, patch = self.extra_runtime()
        a = ClaudeStreamAdapter({"coding_sandbox": PassThrough(), "home_for_state": self.tmp})
        with patch, mock.patch("relay.adapters.claude._claude", return_value="/usr/bin/true"):
            with self.assertRaises(Unsupported) as cm:
                a._coding(self.folder, record, session_id="s")
        self.assertIn("isn't the one in force", str(cm.exception))
        sb = T.ImplementerSandbox(STATE)
        if sb.available() is None:  # the sandbox's own launch check, independent of the hash
            with patch, self.assertRaises(Unsupported) as cm:
                sb.launch(["/usr/bin/true"], self.folder, "s", claude="/usr/bin/true", home=self.tmp,
                          runtime_expected=record["runtime_read"])
            self.assertIn("runtime read scope changed", str(cm.exception))


class PreflightTests(Sim):
    def test_overlapping_or_hardlinked_workspaces_are_refused(self):
        home = os.path.realpath(self.tmp)
        inst = [os.path.join(home, ".local", "share", "claude")]
        for ws in (home, "/"):
            with self.subTest(ws=ws), self.assertRaises(InvalidInput):
                T.preflight(ws, inst, home)
        with self.assertRaises(InvalidInput):  # a workspace containing the client's install
            T.preflight(home + "/big", [os.path.join(home, "big", "claude")], home)
        os.makedirs(os.path.join(home, "big"))
        with self.assertRaises(InvalidInput):  # one inside the client's state
            T.preflight(os.path.join(home, ".claude"), inst, home)
        ws = self.folder
        T.preflight(ws, inst, home)
        outside = os.path.join(home, "dotfile")
        with open(outside, "w") as f:
            f.write("x")
        os.link(outside, os.path.join(ws, "innocent.txt"))  # a write here would change ~/dotfile
        with self.assertRaises(InvalidInput) as cm:
            T.preflight(ws, inst, home)
        self.assertIn("hard-linked", str(cm.exception))


@NEEDS_SANDBOX
class SandboxBoundaryTests(CoderBase):
    """P2/P4: the real OS sandbox decides; the fake client really tries each operation."""

    def test_writes_reads_and_aliases_are_bounded(self):
        ws, home = self.folder, self.home
        for d in (".git", "pkg", ".claude"):
            os.makedirs(os.path.join(ws, d))
        with open(os.path.join(ws, "notes.txt"), "w") as f:
            f.write("project input\n")
        os.symlink(".git", os.path.join(ws, "docs"))                  # an alias of a protected folder
        os.makedirs(os.path.join(home, "elsewhere"))
        os.symlink(os.path.join(home, "elsewhere"), os.path.join(ws, "out"))  # an escape
        other = os.path.join(self.state_dir, "OTHER-CONVERSATION.jsonl")
        open(other, "w").close()
        p = lambda *x: os.path.join(ws, *x)
        ops = [("src.py", "Write", p("src.py")), ("read project", "Read", p("notes.txt")),
               (".git/config", "Write", p(".git", "config")), (".GIT alias", "Write", p(".GIT", "HEAD")),
               ("nested CLAUDE.md", "Write", p("pkg", "CLAUDE.md")), ("AGENTS.md", "Write", p("AGENTS.md")),
               ("agents.md (case)", "Write", p("pkg", "agents.md")), (".claude/settings", "Write", p(".claude", "settings.json")),
               (".mcp.json", "Write", p(".mcp.json")), (".envrc", "Write", p(".envrc")),
               (".relay-verify", "Write", p(".relay-verify")), ("link to .git", "Write", p("docs", "hooks")),
               ("link out", "Write", p("out", "x")), ("home dotfile", "Write", os.path.join(home, ".zshrc")),
               ("client settings", "Write", os.path.join(home, ".claude", "settings.json")),
               ("other conversation", "Write", other), ("runtime", "Write", os.path.join(os.path.dirname(sys.executable), "planted")),
               ("client install", "Write", self.claude), ("read secret", "Read", os.path.join(home, "secret.txt")),
               ("read other conversation", "Read", other)]
        conn, spawned, err, events = self.run_turn(
            T.ImplementerSandbox(STATE), ops=[{"label": l, "tool": t, "path": x} for l, t, x in ops])
        self.assertIsNone(err)
        done = next(e for e in events if e.type == "turn_completed")
        got = dict(json.loads(done.text.split("\n\n")[0]))
        want_ok = {"src.py", "read project", "own state"}
        for label, outcome in got.items():
            with self.subTest(label):
                self.assertEqual(outcome, "ok" if label in want_ok else "denied")
        for gone in (os.path.join(home, ".zshrc"), p(".git", "config"), p(".git", "hooks"), p("AGENTS.md"),
                     os.path.join(home, "elsewhere", "x")):
            self.assertFalse(os.path.exists(gone), gone)
        # The audit names tools and workspace-relative paths only; never contents.
        audit = done.data["tool_audit"]
        self.assertEqual(audit[0], {"tool": "Write", "path": "src.py", "kind": "use"})
        self.assertIn({"tool": "Write", "path": "outside the workspace", "kind": "use"}, audit)
        self.assertNotIn("SECRET-CONTENT", json.dumps(audit))
        # Spawn evidence: the exact profile and argv, by hash, recorded before any prompt.
        self.assertEqual(spawned[0]["policy"], T.POLICY_NAME)
        self.assertEqual(len(spawned[0]["profile_sha256"]), 64)
        self.assertEqual(conn.argv[0], self.claude)
        conn.close()
        self.assertIsNotNone(conn.proc.poll())
        self.assertFalse(os.path.exists(conn.tmp))  # its private temp folder is removed

    def test_network_is_port_443_and_the_resolver_only(self):
        # Run the probe program under the real profile and read each network outcome.
        got = T.ImplementerSandbox(STATE).probe_results()
        self.assertEqual({k: got[k] for k in ("tcp_443", "resolver", "tcp_other", "udp_53", "listen")},
                         {"tcp_443": "allowed", "resolver": "allowed", "tcp_other": "denied",
                          "udp_53": "denied", "listen": "denied"})
        self.assertEqual(got, T.PROBE_EXPECT)  # every boundary, as the route requires


class ProbeTests(Sim):
    def fake_exe(self, body):
        path = os.path.join(self.tmp, "fake-sandbox-exec")
        with open(path, "w") as f:
            f.write("#!/bin/sh\n" + body)
        os.chmod(path, 0o755)
        return path

    @unittest.skipIf(sys.platform != "darwin", "macOS only")
    def test_a_sandbox_that_does_not_hold_is_unavailable(self):
        no_isolation = T.ImplementerSandbox(STATE, self.fake_exe('shift 2; exec "$@"\n'))
        self.assertIn("didn't hold", no_isolation.available())
        cant_start = T.ImplementerSandbox(STATE, self.fake_exe('echo "sandbox_apply: Operation not permitted" >&2; exit 71\n'))
        self.assertIn("can't start", cant_start.available())
        with self.assertRaises(Unsupported):
            cant_start.launch(["true"], self.folder, CONV, claude="/usr/bin/true")

    def test_the_route_is_unavailable_without_identified_client_state(self):
        sb = T.ImplementerSandbox(None)
        self.assertIn("state-path probe", sb.available())
        with self.assertRaises(Unsupported):
            sb.launch(["true"], self.folder, CONV, claude="/usr/bin/true")


class InitAndAuditTests(CoderBase):
    """P6: Claude's own init record, first, must match; any other tool stops and closes."""

    def violation(self, **sc):
        conn, _, err, events = self.run_turn(PassThrough(), **sc)
        end = time.time() + 5
        while conn.proc.poll() is None and time.time() < end:
            time.sleep(0.05)
        self.assertIsNotNone(conn.proc.poll(), "the writer was stopped")
        errs = [e for e in events if e.type == "error" and e.data.get("policy_mismatch")]
        self.assertTrue(errs, events)
        self.assertFalse([e for e in events if e.type == "turn_completed"])  # nothing forwarded
        self.assertEqual(conn.status(), "disconnected")
        with self.assertRaises(NotSubmitted):
            conn.submit("again", "r2")
        return errs[0].text

    def test_matching_init_then_allowed_tools_completes(self):
        conn, _, err, events = self.run_turn(PassThrough(), ops=[
            {"label": "a", "tool": "Write", "path": os.path.join(self.folder, "a.py")}])
        self.assertIsNone(err)
        done = next(e for e in events if e.type == "turn_completed")
        self.assertEqual(done.data["tool_audit"], [{"tool": "Write", "path": "a.py", "kind": "use"}])
        self.assertTrue(conn.policy_ok)
        conn.close()

    def test_mismatching_init_records_stop_the_writer(self):
        for why, sc in {"extra tool": dict(init_tools=list(T.CODING_TOOLS) + ["Bash"]),
                        "missing tool": dict(init_tools=["Read", "Edit"]),
                        "duplicate tool": dict(init_tools=list(T.CODING_TOOLS) + ["Read"]),
                        "wrong mode": dict(init_mode="bypassPermissions"),
                        "default mode": dict(init_mode="default"),
                        "an MCP server": dict(init_mcp=[{"name": "x", "status": "connected"}]),
                        "MCP not reported": dict(init_mcp=None)}.items():
            with self.subTest(why):
                self.violation(**sc)

    def test_init_must_come_first(self):
        self.assertIn("before the client's init record", self.violation(assistant_first=True))
        self.assertIn("before the client's init record", self.violation(no_init=True))

    def test_an_unexpected_tool_mid_turn_stops_the_writer(self):
        text = self.violation(ops=[{"label": "a", "tool": "Write", "path": os.path.join(self.folder, "a.py")},
                                   {"label": "b", "tool": "Bash", "path": os.path.join(self.folder, "b")}])
        self.assertIn("unexpected tool 'Bash'", text)
        self.assertIn("untrusted", text)  # edits already made are bounded but not trusted; no rollback claimed

    def test_denials_are_audited_not_sent_to_your_interface(self):
        conn, _, err, events = self.run_turn(PassThrough(), denials=[
            {"tool_name": "Write", "tool_input": {"file_path": os.path.join(self.folder, ".git", "config")}}])
        done = next(e for e in events if e.type == "turn_completed")
        self.assertIn({"tool": "Write", "path": ".git/config", "kind": "denied"}, done.data["tool_audit"])
        self.assertFalse([e for e in events if e.type == "approval_pending"])
        conn.close()

    def test_a_team_policy_that_is_not_the_one_in_force_never_starts(self):
        self.scene(state_dir=self.state_dir)
        stale = dict(self.record, sha256="0" * 64)
        with self.assertRaises(Unsupported):
            self.adapter(PassThrough()).resume(self.folder, CONV, {"writer_policy": stale})
        with self.assertRaises(Unsupported):  # unavailable route: nothing is started either
            self.adapter(T.ImplementerSandbox(None)).resume(self.folder, CONV, {"writer_policy": self.record})


class HandoffPolicyTests(CoderBase):
    """The policy is fixed for the hand-off episode, recorded with the spawn, and gone after."""

    def handoffs(self, sandbox):
        root = init_root(os.path.join(self.tmp, "root-" + os.urandom(3).hex()), ["*"])
        disc = lambda a, c: {"client_session_id": c, "extra": {"writers": [], "relay_visible": False}}
        with mock.patch.dict(os.environ, {"AGENT_RELAY_LIVE_HANDOFF_SCOPE": self.folder}):
            h = Handoffs(Bus(root), {"coding_sandbox": sandbox, "home_for_state": self.home}, disc)
            h.begin("claude-agents", CONV, self.folder, writer_policy=self.record)
            h.advance("claude-agents", CONV)
            return h, h.start_writer("claude-agents", CONV)

    def test_the_writer_starts_under_the_policy_and_recovers_after_a_violation(self):
        self.scene(state_dir=self.state_dir, ops=[{"label": "b", "tool": "Bash", "path": self.folder}])
        h, st = self.handoffs(PassThrough())
        key = h.key("claude-agents", CONV)
        self.assertEqual(st["state"], "relay_owned")
        self.assertEqual(st["detail"]["writer_policy"]["sha256"], self.record["sha256"])
        conn = h.writers[key]
        self.assertEqual(conn.policy, {"tools": list(T.CODING_TOOLS), "permission_mode": "acceptEdits"})
        try:
            conn.submit("relay turn id: u_" + "b" * 24, "r1")
        except Exception:  # noqa: BLE001 - the violation may surface during submission
            pass
        conn.poll(1.0)
        end = time.time() + 5
        while conn.proc.poll() is None and time.time() < end:
            time.sleep(0.05)
        self.assertIsNotNone(conn.proc.poll())  # stopped and closed
        why = h.check(key)  # the console's next check blocks it, honestly worded
        self.assertIn("broke the team's coding policy", why)
        self.assertNotIn(key, h.writers)
        # Resolving: the writer's exit is verified, the claim released, and the episode's
        # policy doesn't carry past it.
        st = h.resolve(key)
        self.assertEqual(st["state"], "released")
        self.assertNotIn("writer_policy", h.own.get(key)["detail"])

    def test_without_a_coding_route_the_writer_is_never_started(self):
        self.scene(state_dir=self.state_dir)
        h, st = self.handoffs(T.ImplementerSandbox(None))
        self.assertEqual(st["state"], "blocked")
        self.assertIn("state-path probe", st["reason"])
        self.assertEqual(h.writers, {})


class PromptTests(unittest.TestCase):
    def test_the_implementer_sees_its_previous_check_as_relay_data(self):
        pair = {"id": "p", "leader": {"participant": "r"}, "follower": {"participant": "i"},
                "scope": "s", "workflow": {"kind": "review_loop"}}
        msg = {"from": "r", "subject": "fix", "id": "m", "body": "Fix it.", "approval_refs": [], "evidence_refs": []}
        text = handoff_prompt(pair=pair, turn_id="u_1", role="follower", message=msg,
                              check={"status": "failed", "exit": 1, "duration_s": 0.2, "revision": "u_0",
                                     "output_tail": "AssertionError: 6 != 5", "previous": True})
        self.assertIn("Relay check of your previous revision (u_0)", text)
        self.assertIn("This is not a pass.", text)
        self.assertIn("gives no permission to change tools or scope", text)
        self.assertIn("AssertionError: 6 != 5", text)


if __name__ == "__main__":
    unittest.main()


class HostPolicyTests(unittest.TestCase):
    """At team creation: the policy is recorded, or the team isn't created (nothing begun)."""

    def test_the_implementer_must_be_claude_with_an_available_coding_route(self):
        from relay.host import Host
        h = Host.__new__(Host)  # only _implementer_policy and its hand-off adapter lookup
        h.handoffs = mock.Mock()
        with self.assertRaises(InvalidInput):
            h._implementer_policy({"adapter": "codex-local"})
        writer = ClaudeStreamAdapter({"coding_sandbox": T.ImplementerSandbox(None)})
        h.handoffs.writer_adapter.return_value = writer
        with self.assertRaises(Unsupported) as cm:
            h._implementer_policy({"adapter": "claude-agents"})
        self.assertIn("state-path probe", str(cm.exception))
        h.handoffs.begin.assert_not_called()
        writer.options["coding_sandbox"] = PassThrough()
        self.assertEqual(h._implementer_policy({"adapter": "claude-agents"}), T.policy_record(STATE))
