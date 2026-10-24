"""The reviewer-launch and Claude state-path probe scripts, against fakes only.

No real Codex or Claude, server, conversation or model is used. The state probe's sandbox is
the REAL coding sandbox where available; its log collector is a fake that reports scripted
denials against the real pids, plus one test of the real `log stream` capture check."""

import importlib.util
import json
import os
import re
import sys
import time
import unittest
from unittest import mock

import util  # noqa: F401
from test_resume_writers import CodexBase, Sim

from relay import toolpolicy as T

HERE = os.path.dirname(os.path.abspath(__file__))


def load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, "..", "scripts", f"{name}.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


RP = load("codex_reviewer_probe")
SP = load("claude_state_probe")
GOOD_CODEX = lambda: "codex-cli 0.162.1"
GOOD_CLAUDE = lambda: "2.1.296 (Claude Code)"


class ReviewerProbeTests(CodexBase):
    def run_probe(self, deadline=30, version_of=GOOD_CODEX, **sc):
        self.run_dir = os.path.realpath(os.path.join(self.tmp, "run-" + os.urandom(3).hex()))
        os.makedirs(self.run_dir)
        open(self.log, "w").close()
        self.scene(log_file=self.log, **dict({"start": {}}, **sc))
        code = RP.main(["--run-dir", self.run_dir, "--deadline", str(deadline)], version_of=version_of)
        path = os.path.join(self.run_dir, RP.EVIDENCE)
        ev = json.load(open(path)) if os.path.exists(path) else None
        return code, ev

    def methods(self):
        return [r["method"] for r in self.requests()]

    def test_accepted_with_the_exact_production_request_and_no_turn(self):
        code, ev = self.run_probe()
        self.assertEqual((code, ev["outcome"]), (0, "accepted"))
        self.assertEqual(self.methods(), ["initialize", "initialized", "thread/start"])  # no turn/start
        req = next(r["params"] for r in self.requests() if r["method"] == "thread/start")
        self.assertEqual(req, {"cwd": os.path.join(self.run_dir, "ws"), "approvalPolicy": "never",
                               "approvalsReviewer": "user", "config": {"default_permissions": ":read-only"}})
        self.assertEqual((ev["diagnostic"]["profile"], ev["diagnostic"]["profile_id"]), ("reported", ":read-only"))
        self.assertTrue(ev["child_exit_confirmed"])
        self.assertEqual((ev["turn_start_sent"], ev["model_turns"], ev["retries"]), (False, 0, 0))
        self.assertNotIn(self.run_dir, json.dumps(ev))  # no local paths

    def test_refusals_close_the_child_and_never_retry(self):
        for why, sc in {"null profile": dict(start={"activePermissionProfile": None}),
                        "malformed result": dict(start_raw=None),
                        "wrong folder": dict(start={"cwd": "/elsewhere"})}.items():
            with self.subTest(why):
                code, ev = self.run_probe(**sc)
                self.assertEqual((code, ev["outcome"]), (3, "refused"))
                self.assertTrue(ev["child_exit_confirmed"])
                self.assertEqual(self.methods().count("thread/start"), 1)  # once, never retried
                self.assertNotIn("turn/start", self.methods())
                self.assertNotIn("/elsewhere", json.dumps(ev))

    def test_the_deadline_stops_the_exact_child(self):
        code, ev = self.run_probe(deadline=5, hang_start=30)  # 5 s: the smallest valid deadline
        self.assertEqual((code, ev["outcome"]), (5, "timed_out"))
        self.assertTrue(ev["child_exit_confirmed"])
        with self.assertRaises(ProcessLookupError):
            os.kill(ev["child_pid"], 0)
        self.assertNotIn("turn/start", self.methods())

    def test_preconditions_refuse_before_anything_is_spawned(self):
        code, ev = self.run_probe(version_of=lambda: "codex-cli 0.163.0")
        self.assertEqual((code, ev), (4, None))
        self.assertEqual(self.requests(), [])  # no app-server ever started
        self.assertEqual(os.listdir(self.run_dir), [])
        used = os.path.realpath(os.path.join(self.tmp, "used"))
        os.makedirs(used)
        open(os.path.join(used, "old"), "w").close()
        open(self.log, "w").close()
        self.assertEqual(RP.main(["--run-dir", used], version_of=GOOD_CODEX), 4)
        self.assertEqual(self.requests(), [])


# ---------------------------------------------------------------- state probe
FAKE_PROBE_CODER = r'''
import json, os, re, sys
sc = json.load(open(os.environ["FAKE_SCENARIO"]))
argv = sys.argv[1:]
resume = "--resume" in argv
sid = argv[argv.index("--resume" if resume else "--session-id") + 1]
if resume and sc.get("resume_reports"):
    sid = sc["resume_reports"]  # a client that reports another conversation
if not resume and sc.get("fresh_reports"):
    sid = sc["fresh_reports"]
def out(o): print(json.dumps(o), flush=True)
home = os.environ["HOME"]
slug = re.sub(r"[^A-Za-z0-9-]", "-", os.getcwd())
for text in sc.get("stderr_lines", []):  # what a real client might print on stderr at startup
    text = text.replace("<HOME>", sc.get("home", home)).replace("<WS>", os.getcwd()).replace("<RUN>", os.path.dirname(os.getcwd()))
    text = text.replace("<CTMP>", os.environ.get("CLAUDE_CODE_TMPDIR", "<unset>"))  # its own temp root, as routed
    sys.stderr.write(text + "\n"); sys.stderr.flush()
if sc.get("stderr_bytes"):  # far more than a pipe holds: blocks unless the reader drains it
    n = sc["stderr_bytes"]
    while n > 0:
        k = min(n, 100)
        sys.stderr.write("x" * (k - 1) + "\n"); n -= k
    sys.stderr.flush()
if sc.get("spawn_child"):  # a child of its own, alive while it is (it reads this process's pipe)
    import subprocess
    kid = subprocess.Popen([sys.executable, "-I", "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE)
if sc.get("exit_at_start") is not None:
    sys.exit(sc["exit_at_start"])  # its own exit before reading any input
first = True
for line in sys.stdin:
    if sc.get("exit_on_input") is not None:
        sys.exit(sc["exit_on_input"])  # proof that something WAS written to its input
    msg = json.loads(line)
    if sc.get("exit_code") is not None:
        sys.exit(sc["exit_code"])  # its own (nonzero) exit before any init record
    if sc.get("exit_before_init"):
        sys.exit(0)            # EOF: the client ends before any init record
    if sc.get("silent"):
        import time
        time.sleep(sc["silent"])  # no output at all for this long
    if first and sc.get("pre_init_sleep"):
        import time
        time.sleep(sc["pre_init_sleep"])  # its init record comes later
    if first:
        out({"type": "system", "subtype": "init", "session_id": sid, "cwd": os.getcwd(),
             "tools": sc.get("init_tools", argv[argv.index("--tools") + 1].split(",")),
             "permissionMode": argv[argv.index("--permission-mode") + 1], "mcp_servers": [],
             "model": "SECRET-model-name", "apiKeySource": "none"})
    first = False
    for op in sc.get("ops_before_replay", []):  # recorded by the adapter while submit waits
        out({"type": "assistant", "session_id": sid, "message": {"content": [
            {"type": "tool_use", "id": "t", "name": op["tool"], "input": {"file_path": op["path"], "content": "SECRET-CONTENT"}}]}})
    out({"type": "user", "uuid": msg["uuid"], "session_id": sid})
    if sc.get("hang"):
        import time
        time.sleep(sc["hang"])
    for op in sc.get("ops_resume" if resume else "ops_fresh", []):
        out({"type": "assistant", "session_id": sid, "message": {"content": [
            {"type": "tool_use", "id": "t", "name": op["tool"], "input": {"file_path": op["path"], "content": "SECRET-CONTENT"}}]}})
        with open(op["path"], "w") as f:
            f.write(op["text"])
    d = os.path.join(home, ".claude", "projects", slug)   # the client's own conversation record
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, sid + ".jsonl"), "a") as f:
        f.write("{}\n")
    out({"type": "result", "subtype": "success", "is_error": False, "session_id": sid,
         "result": sc.get("reply", "PROBE-OK SECRET-REPLY")})
if sc.get("linger"):  # its input closed, but it doesn't exit on its own
    import time
    time.sleep(sc["linger"])
'''


class FakeCollector:
    """Reports the canary (if it can see) and the scripted denials against the client's real pid,
    each with a SOURCE timestamp ("ts", as the system log gives) and the receipt time ("t")."""

    def __init__(self, script=(), see_canary=True, stranger=None, canary_ts_offset=0.0, raw=(),
                 ready_after=0.0, attach_after=0.0, dies=False, canary_pid_offset=0, stop_ok=True,
                 interrupt_snapshot=False):
        """Capture model: `status()` is "ready" `ready_after` s after start
        (None: never; `dies`: the command exits instead). Reports are DELIVERED only for events
        at or after `attach_after` s from start: a header can precede the real attachment. The
        canary's reports follow the script's own read schedule (`reads`, `gap` from note_pid; a
        canary without a schedule makes one read), each delivered once its time has come."""
        self.events, self.script, self.see_canary, self.stranger = [], script, see_canary, stranger
        self.canary_ts_offset, self.raw = canary_ts_offset, raw
        self.ready_after, self.attach_after, self.dies = ready_after, attach_after, dies
        self.canary_pid_offset, self.stop_ok, self.interrupt_snapshot = canary_pid_offset, stop_ok, interrupt_snapshot
        self.started = self.stopped = False
        self.t0 = None
        self.pids = {}
        self.pending = []  # canary reports not yet due; delivered by APPENDING, as the real stream does

    def start(self):
        self.started = True
        self.t0 = time.time()

    def status(self):
        if self.dies:
            return "exited"
        if self.ready_after is None or time.time() - self.t0 < self.ready_after:
            return "starting"
        return "ready"

    def note_pid(self, role, pid, **info):
        now = time.time()
        self.pids.setdefault(role, []).append(pid)
        if role == "canary" and self.see_canary:
            reads, gap = info.get("reads"), info.get("gap", 0.0)
            targets = [os.path.join(info["target"], f"r{i}") for i in range(reads)] if reads else [info["target"]]
            for i, target in enumerate(targets):
                at = now + i * gap  # when the canary makes this read
                if at - (self.t0 or now) < self.attach_after:
                    continue  # the stream wasn't attached yet: this report is never delivered
                self.pending.append({"pid": pid + self.canary_pid_offset, "name": "python3", "op": "file-read-data",
                                     "target": target, "count": 1, "t": at, "ts": at + self.canary_ts_offset,
                                     "due": at})
        if role == "client":
            c = info["ctx"]
            fmt = lambda x: x.format(home=c["home"], slug=c["slug"], session=c["session"], ws=c["ws"])
            for op, target in self.script:
                self.events.append({"pid": pid, "name": "2.1.296", "op": op, "count": 2, "t": now, "ts": now,
                                    "target": fmt(target)})
            for r in self.raw:  # dicts: {op, target, ts_offset | ts: None, turn (1-based) or any}
                if r.get("turn") not in (None, len(self.pids["client"])):
                    continue
                self.events.append({"pid": pid, "name": "2.1.296", "op": r["op"], "count": 1, "t": now,
                                    "ts": None if r.get("ts", 0) is None else now + r.get("ts_offset", 0.0),
                                    "target": fmt(r["target"])})
            if self.stranger:
                self.events.append({"pid": 999999, "name": "claude", "op": "file-read-data", "count": 1,
                                    "target": "/x", "t": now, "ts": now})

    def emit(self, pid, op, target, ts):  # (tests) a delayed report, delivered later
        self.events.append({"pid": pid, "name": "2.1.296", "op": op, "count": 1, "t": time.time(),
                            "ts": ts, "target": target})

    def snapshot(self):
        if self.interrupt_snapshot:
            self.interrupt_snapshot = False
            raise KeyboardInterrupt()
        now = time.time()
        due = [e for e in self.pending if e["due"] <= now]
        self.pending = [e for e in self.pending if e["due"] > now]
        self.events.extend(due)
        return list(self.events)

    def stop(self):
        self.stopped = True
        return self.stop_ok


class StateProbeBase(Sim):
    def setUp(self):
        super().setUp()
        self.home = os.path.realpath(self.tmp)
        inst = os.path.join(self.home, ".local", "share", "claude", "versions")
        os.makedirs(inst)
        self.claude = os.path.join(inst, "2.1.296")
        with open(self.claude, "w") as f:
            f.write(f"#!{sys.executable}\n{FAKE_PROBE_CODER}")
        os.chmod(self.claude, 0o755)
        self.patch = mock.patch("relay.adapters.claude._claude", return_value=self.claude)
        self.patch.start()
        self.scenario = os.path.join(inst, "scenario.json")  # readable under the sandbox (install)
        os.environ["FAKE_SCENARIO"] = self.scenario
        os.makedirs(os.path.join(self.home, ".claude", "projects"))
        self.sandbox = T.ImplementerSandbox(T.StateAllowlist(SP.CANDIDATE))
        if self.sandbox.available() is not None:
            self.skipTest("no coding sandbox on this machine")

    def tearDown(self):
        self.patch.stop()
        super().tearDown()

    def run_probe(self, collector=None, version_of=GOOD_CLAUDE, **sc):
        self.run_dir = os.path.realpath(os.path.join(self.tmp, "run-" + os.urandom(3).hex()))
        os.makedirs(self.run_dir)
        self.scene(**sc)
        self.col = collector or FakeCollector()
        code = SP.main(["--run-dir", self.run_dir, "--deadline", "60"], version_of=version_of,
                       collector=self.col, home=self.home)
        path = os.path.join(self.run_dir, SP.EVIDENCE)
        raw = open(path).read() if os.path.exists(path) else None
        prop = os.path.join(self.run_dir, f"claude-state-{SP.VERSION}.proposed.json")
        return code, raw, (json.load(open(prop)) if os.path.exists(prop) else None)



class StateProbeTests(StateProbeBase):
    BOOTSTRAP = [("file-read-data", "{home}/.claude.json"), ("file-write-create", "{home}/.claude.json"),
                 ("file-write-create", "{home}/.claude/statsig/abc-SECRET"),
                 ("file-write-create", "{home}/.claude/todos/{session}-agent.json"),
                 ("file-write-data", "{home}/.claude/projects/{slug}/OTHER-SECRET.jsonl")]

    def test_fresh_then_resume_records_only_templates_and_proposes_minimum_paths(self):
        col = FakeCollector(self.BOOTSTRAP)
        code, raw, prop = self.run_probe_with_hello({"collector": col})
        ev = json.loads(raw)
        self.assertEqual(code, 0, ev.get("stops"))
        self.assertEqual([t["mode"] for t in ev["turns"]], ["fresh", "resume"])
        t1 = ev["turns"][0]
        self.assertEqual((t1["outcome"], t1["policy_ok"], t1["identity_ok"], t1["reply_has_probe_ok"]),
                         ("completed", True, True, True))
        self.assertEqual(sorted(t1["init"]["tools"]), sorted(T.CODING_TOOLS))
        self.assertEqual((t1["init"]["permissionMode"], t1["init"]["mcp_servers"]), ("acceptEdits", 0))
        self.assertIn("model", t1["init"]["keys"])                # key names only
        self.assertEqual(t1["trace"][0], "system/init")           # Claude's own record came first
        self.assertEqual(ev["model_turns"], 2)
        for t in ev["turns"]:
            self.assertEqual(t["cleanup"], {"exit_confirmed": True, "tmp_removed": True})
        self.assertTrue(ev["capture"]["established"])
        self.assertTrue(col.stopped)
        cats = {(d["category"], d["op"]): d for d in ev["denials"]}
        self.assertEqual(cats[("client config file", "file-read-data")]["template"], "{home}/.claude.json")
        self.assertEqual(cats[("client statsig", "file-write-create")]["template"], "{home}/.claude/statsig/<...>")
        self.assertEqual(cats[("client todos: this session", "file-write-create")]["count"], 4)  # 2 per turn
        self.assertIn(("own project folder: other item", "file-write-data"), cats)
        # Nothing raw: no local path, file names, message text, model name, prompt or content.
        for leak in (self.home, "SECRET", "Reply with exactly", "PROBE-OK SECRET", "hello.txt containing"):
            self.assertNotIn(leak, raw)
        # The proposal: exact reads separate from this conversation's writes; nothing broad.
        self.assertEqual(prop["proven_by"], "")
        self.assertEqual(prop["read_files"], ["{home}/.claude.json"])
        self.assertNotIn("{home}/.claude.json", prop["write_files"])
        self.assertEqual(prop["write_files"], sorted(SP.CANDIDATE["write_files"]))
        self.assertTrue(any("statsig" in x for x in prop["needs_review"]))
        self.assertTrue(any("other item" in x for x in prop["needs_review"]))
        self.assertEqual(prop["write_dirs"], [])
        self.assertTrue(ev["transcript_kept"])

    def test_resume_turn_creates_the_file_and_is_checked(self):
        sc = {}
        code, raw, _ = self.run_probe_with_hello(sc)
        ev = json.loads(raw)
        self.assertEqual(code, 0)
        self.assertTrue(ev["turns"][1]["hello_ok"])
        self.assertEqual(ev["turns"][1]["tool_audit"], [{"tool": "Write", "path": "hello.txt", "kind": "use"}])

    def run_probe_with_hello(self, sc):
        # The workspace path is only known inside the run, so the fake writes hello.txt relative to it.
        orig = SP.run_turn

        def run_turn(adapter, col, ctx, text, **kw):
            if kw.get("resume"):
                self.scene(ops_resume=[{"tool": "Write", "path": os.path.join(ctx["ws"], "hello.txt"), "text": "hi\n"}])
            return orig(adapter, col, ctx, text, **kw)
        with mock.patch.object(SP, "run_turn", run_turn):
            return self.run_probe(**sc)

    def test_stop_conditions_end_the_run_without_a_second_turn(self):
        cases = {"an unknown path": ([("file-read-data", "/weird/SECRET-place")],
                                     "unknown: path outside the classifier's roots"),
                 "elsewhere in home": ([("file-read-data", "{home}/Documents/SECRET.pdf")], "home: other"),
                 "a non-443 port": ([("network-outbound", "1.2.3.4:80")], "network port 80"),
                 "a keychain read": ([("file-read-data", "{home}/Library/Keychains/login.keychain-db")], "keychain files"),
                 "a system path": ([("file-read-data", "/private/var/db/SECRET")], "system: /private/var/db")}
        for why, (script, cat) in cases.items():
            with self.subTest(why):
                code, raw, prop = self.run_probe(collector=FakeCollector(script))
                ev = json.loads(raw)
                self.assertEqual(code, 2)
                self.assertEqual(len(ev["turns"]), 1)  # no T2
                self.assertTrue(ev["stops"])
                self.assertIn(cat, [d["category"] for d in ev["denials"]])
                self.assertNotIn("SECRET", raw)
                self.assertNotIn("1.2.3.4", raw)
                self.assertEqual(ev["turns"][0]["cleanup"]["exit_confirmed"], True)

    def test_a_denied_client_process_it_did_not_start_stops_the_run(self):
        code, raw, _ = self.run_probe(collector=FakeCollector(stranger=True))
        ev = json.loads(raw)
        self.assertEqual(code, 2)
        self.assertGreater(ev["turns"][0]["unattributed_denials"], 0)

    def test_t2_only_after_a_verified_successful_t1(self):
        for why, sc in {"policy mismatch": dict(init_tools=list(T.CODING_TOOLS) + ["Bash"]),
                        "no PROBE-OK": dict(reply="something else")}.items():
            with self.subTest(why):
                code, raw, _ = self.run_probe(**sc)
                ev = json.loads(raw)
                self.assertEqual(code, 2)
                self.assertEqual(len(ev["turns"]), 1)
                self.assertEqual(ev["model_turns"], 1)

    def test_no_capture_means_nothing_starts(self):
        code, raw, prop = self.run_probe(collector=FakeCollector(see_canary=False))
        self.assertEqual((code, prop), (4, None))
        ev = json.loads(raw)  # a capture-stage record, never a proposal
        self.assertEqual((ev["stage"], ev["outcome"], ev["capture"], ev["model_turns"]),
                         ("capture", "not_started", False, 0))
        self.assertEqual(os.listdir(os.path.join(self.home, ".claude", "projects")), [])  # no client ran
        self.assertTrue(self.col.stopped)

    def test_preconditions_refuse_before_anything_starts(self):
        code, raw, _ = self.run_probe(version_of=lambda: "2.1.297 (Claude Code)")
        self.assertEqual((code, raw), (4, None))
        self.assertFalse(self.col.started)
        os.rmdir(os.path.join(self.home, ".claude", "projects"))
        code, raw, _ = self.run_probe()
        self.assertEqual((code, raw), (4, None))  # it never creates the home's client folders
        self.assertFalse(os.path.exists(os.path.join(self.home, ".claude", "projects")))


class ClassifyTests(unittest.TestCase):
    CTX = {"home": "/Users/u", "slug": "-r-ws", "session": "S1", "ws": "/r/ws", "tmp": "/t/relay-impl-x"}

    def test_only_predefined_templates(self):
        c = lambda op, t: SP.classify(op, t, self.CTX)
        self.assertEqual(c("file-write-create", "/Users/u/.claude/projects/-r-ws/S1.jsonl")[1],
                         "{home}/.claude/projects/{slug}/{session}.jsonl")
        self.assertEqual(c("file-read-data", "/Users/u/.claude/projects/-other/x.jsonl")[0], "another conversation's folder")
        self.assertEqual(c("file-read-data", "/Users/u/.claude/unheard-of")[0], "unknown: unlisted client folder item")
        self.assertEqual(c("file-read-data", "/Users/u/Library/Caches/claude-cli-nodejs/x"),
                         ("client cache", "{home}/Library/Caches/claude-cli-nodejs/<...>", "client"))
        self.assertEqual(c("file-read-data", "/Users/u/Library/Caches/com.other.app/x")[2], "stop")
        self.assertEqual(c("network-outbound", "10.0.0.1:443"), ("network port 443", None, "client"))
        self.assertEqual(c("file-read-data", "relative")[0], "unknown: unsupported target format")


@unittest.skipIf(sys.platform != "darwin", "macOS only")
class RealCaptureTests(Sim):
    def test_the_real_collector_proves_capture_with_a_canary(self):
        """Production timing: no wait between starting the collector and establishing capture."""
        col = SP.LogCollector()
        col.start()
        try:
            cap = SP.establish_capture(col, os.path.realpath(self.tmp), SP.PR.Budget(30.0))
        finally:
            stopped = col.stop()
        self.assertIsNotNone(cap, "capture wasn't proven")
        self.assertTrue(cap["established"], f"capture wasn't proven: {cap.get('failure')}")
        self.assertGreaterEqual(cap["attributed_reads"], 1)
        self.assertTrue(cap["canary_exit_confirmed"] and cap["canary_files_removed"] and stopped)

    def test_the_parser_counts_duplicates_and_ignores_others(self):
        col = SP.LogCollector(cmd=[sys.executable, "-c", "import json\n"
                                   "for m in ['Sandbox: cat(42) deny(1) file-read-data /a', "
                                   "'3 duplicate reports for Sandbox: cat(42) deny(1) file-read-data /a', 'other']:\n"
                                   "    print(json.dumps({'eventMessage': m}))"])
        col.start()
        end = time.time() + 10
        while col.status() != "exited" and time.time() < end:  # bounded: until the fixture's output ends
            time.sleep(0.02)
        col.stop()
        self.assertEqual([(e["pid"], e["count"], e["target"]) for e in col.snapshot()], [(42, 1, "/a"), (42, 3, "/a")])


if __name__ == "__main__":
    unittest.main()
