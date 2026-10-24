"""Resume writers for hand-off, against SIMULATED clients only.

A fake `claude` (stream-json) and a fake `codex app-server` (JSON-RPC over stdio) are
small Python executables driven by a scenario file. They speak the protocol shapes the
real adapters parse; no real client, model or session is involved. Real hand-off stays
locked unless AGENT_RELAY_LIVE_HANDOFF_SCOPE names the exact folder (tested below)."""

import json
import os
import re
import sqlite3
import stat
import subprocess
import sys
import time
from unittest import mock

from util import RelayTest

from relay.adapters.base import DeliveryUncertain, NotSubmitted, WrongSession
from relay.adapters.claude import ClaudeStreamAdapter, resume_argv
from relay.adapters.codex import CodexAppServerAdapter, provenance_diagnostic
from relay.bus import Bus
from relay.context import process_start_ms
from relay.controller import Controller
from relay.handoff import Handoffs, same_process
from relay.storage import InvalidInput, actor, init_root

FAKE_CLAUDE = r'''
import json, os, re, sys
sc = json.load(open(os.environ["FAKE_SCENARIO"]))
open(sc["argv_file"], "w").write(json.dumps(sys.argv[1:]))
if sc.get("exit_immediately"):
    sys.exit(3)
for line in sys.stdin:
    msg = json.loads(line)
    text = msg["message"]["content"][0]["text"]
    m = re.search(r"relay turn id: (u_[0-9a-f]+)", text)
    out = []
    if not sc.get("no_init"):
        out.append({"type": "system", "subtype": "init", "session_id": sc.get("init_sid"), "cwd": sc.get("init_cwd")})
    if sc.get("exit_after_init"):
        for o in out: print(json.dumps(o), flush=True)
        sys.exit(4)
    out.append({"type": "user", "uuid": msg["uuid"], "session_id": sc.get("echo_sid")})
    out.append({"type": "assistant", "session_id": sc.get("result_sid")})
    block = {"relay": 1, "turn_id": m.group(1) if m else None, "action": "continue", "kind": "result", "subject": "reply"}
    res = sc.get("body", "CL-REPLY") + "\n\n```relay\n" + json.dumps(block) + "\n```\n"
    out.append({"type": "result", "subtype": "success", "is_error": False, "session_id": sc.get("result_sid"), "result": res})
    for o in out:
        print(json.dumps(o), flush=True)
'''

FAKE_CODEX = r'''
import json, os, re, sys
sc = json.load(open(os.environ["FAKE_SCENARIO"]))
log = open(sc["log_file"], "a")
if sc.get("exit_immediately"):
    sys.exit(3)
def send(o): print(json.dumps(o), flush=True)
experimental = False  # like 0.162.1: provenance fields only for clients that opted in
BUILTIN = (":read-only", ":workspace", ":danger-full-access")
def server_profile(p):
    """What 0.162.1 reports by itself (codex-rs at rust-v0.162.1, core/src/config/mod.rs
    and the app-server test cold_resume_permission_overrides_win_over_persisted_profile): a typed
    legacy `sandbox` selects legacy permissions, so provenance is None, serialized as null. A
    named profile (`permissions`, or `config.default_permissions`) is reported as itself."""
    if p.get("sandbox") is not None:
        return None
    name = p.get("permissions") or (p.get("config") or {}).get("default_permissions")
    return {"id": name, "extends": None} if name else {"id": ":workspace", "extends": None}
def provenance(r, result, p):
    result["approvalsReviewer"] = r.get("approvalsReviewer", "user")
    if "approvalsReviewer" in r and r["approvalsReviewer"] == "<absent>":
        del result["approvalsReviewer"]
    if experimental:
        # A scenario may override the answer (tests of the relay's acceptance logic, or of
        # other shapes); otherwise the server answers as 0.162.1 does.
        prof = r.get("activePermissionProfile", server_profile(p))
        if prof != "<absent>":
            result["activePermissionProfile"] = prof
for line in sys.stdin:
    msg = json.loads(line)
    log.write(json.dumps({"method": msg.get("method"), "params": msg.get("params")}) + "\n"); log.flush()
    if "id" not in msg:
        continue
    m, rid, p = msg["method"], msg["id"], msg.get("params") or {}
    if m in (sc.get("errors") or {}):  # refuse this method with exactly this message
        send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32600, "message": sc["errors"][m]}})
        continue
    if m == "initialize":
        experimental = bool((p.get("capabilities") or {}).get("experimentalApi"))
        send({"jsonrpc": "2.0", "id": rid, "result": {}})
    elif m == "thread/resume":
        if sc.get("exit_on_resume"):
            sys.exit(5)
        if sc.get("busy_first"):  # like the real server right after a terminal quits
            n = int(open(sc["busy_count_file"]).read()) if os.path.exists(sc["busy_count_file"]) else 0
            open(sc["busy_count_file"], "w").write(str(n + 1))
            if n < sc["busy_first"]:
                send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32600,
                      "message": "thread " + p["threadId"] + " already has an active writer"}})
                continue
        # Like the real 0.160 server: experimental-only fields need the experimentalApi capability.
        bad = [k for k in ("runtimeWorkspaceRoots",) if k in p]
        if bad:
            send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32600,
                  "message": f"thread/resume.{bad[0]} requires experimentalApi capability"}})
            continue
        r = sc["resume"]
        result = {"thread": {"id": r["id"], "cwd": r["cwd"], "status": r.get("status", {"type": "idle"})},
                  "cwd": r["cwd"], "approvalPolicy": r.get("approvalPolicy", "never"),
                  "sandbox": r.get("sandbox", {"type": "workspaceWrite", "writableRoots": [], "networkAccess": False,
                                               "excludeSlashTmp": True, "excludeTmpdirEnvVar": True}),
                  }
        if "runtimeWorkspaceRoots" in r and experimental:  # experimental-only: absent from stable responses
            result["runtimeWorkspaceRoots"] = r["runtimeWorkspaceRoots"]
        provenance(r, result, p)
        send({"jsonrpc": "2.0", "id": rid, "result": result})
    elif m == "thread/start" and sc.get("hang_start"):  # (the diagnostic's deadline) never answers
        import time; time.sleep(sc["hang_start"])
    elif m == "thread/start" and "start_raw" in sc:  # a malformed result, sent as is
        send({"jsonrpc": "2.0", "id": rid, "result": sc["start_raw"]})
    elif m == "thread/start" and "start" in sc:  # the reviewer launch, answered as configured
        r = sc["start"]
        if p.get("sandbox") is not None and p.get("permissions") is not None:  # as thread_processor.rs
            send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32600,
                  "message": "`permissions` cannot be combined with `sandbox`"}})
            continue
        named = p.get("permissions") or (p.get("config") or {}).get("default_permissions")
        legacy_ro = p.get("sandbox") == "read-only" or (p.get("sandbox") is None and named == ":read-only")
        result = {"thread": {"id": r.get("id", "th-new"), "cwd": r.get("cwd", p.get("cwd")), "status": {"type": "idle"}},
                  "cwd": r.get("cwd", p.get("cwd")), "approvalPolicy": r.get("approvalPolicy", p.get("approvalPolicy")),
                  "sandbox": r.get("sandbox", {"type": "readOnly", "networkAccess": False} if legacy_ro else
                                   {"type": "workspaceWrite", "writableRoots": [], "networkAccess": False})}
        provenance(r, result, p)
        send({"jsonrpc": "2.0", "id": rid, "result": result})
    elif m == "turn/start":
        tid = p["threadId"]
        text = p["input"][0]["text"]
        t = re.search(r"relay turn id: (u_[0-9a-f]+)", text)
        send({"jsonrpc": "2.0", "id": rid, "result": {"turn": {"id": "turn-1", "status": "inProgress"}}})
        send({"jsonrpc": "2.0", "method": "turn/started", "params": {"threadId": tid, "turn": {"id": "turn-1"}}})
        block = {"relay": 1, "turn_id": t.group(1) if t else None, "action": "continue", "kind": "task", "subject": "next"}
        send({"jsonrpc": "2.0", "method": "item/completed", "params": {"threadId": tid, "turnId": "turn-1",
              "item": {"type": "agentMessage", "text": "CX-REPLY\n\n```relay\n" + json.dumps(block) + "\n```\n"}}})
        send({"jsonrpc": "2.0", "method": "turn/completed", "params": {"threadId": tid, "turn": {"id": "turn-1", "status": "completed"}}})
    else:
        send({"jsonrpc": "2.0", "id": rid, "result": {}})
'''

CONV = "11111111-2222-3333-4444-555555555555"


class Sim(RelayTest):
    def setUp(self):
        super().setUp()
        self.folder = os.path.realpath(os.path.join(self.tmp, "scratch"))
        os.makedirs(self.folder)
        self.scenario = os.path.join(self.tmp, "scenario.json")
        self.env = mock.patch.dict(os.environ, {"FAKE_SCENARIO": self.scenario, "HOME": self.tmp})
        self.env.start()
        os.environ.pop("AGENT_RELAY_LIVE_HANDOFF_SCOPE", None)
        os.environ.pop("AGENT_RELAY_ALLOW_UNVERIFIED", None)

    def tearDown(self):
        self.env.stop()
        super().tearDown()

    def exe(self, name, body):
        path = os.path.join(self.tmp, name)
        with open(path, "w") as f:
            f.write(f"#!{sys.executable}\n{body}")
        os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
        return path

    def scene(self, **sc):
        with open(self.scenario, "w") as f:
            json.dump(sc, f)


# = Claude resume writer
class ClaudeBase(Sim):
    def setUp(self):
        super().setUp()
        self.claude = self.exe("claude", FAKE_CLAUDE)
        self.patch = mock.patch("relay.adapters.claude._claude", return_value=self.claude)
        self.patch.start()
        self.argv_file = os.path.join(self.tmp, "argv.json")
        tdir = os.path.join(self.tmp, ".claude", "projects", re.sub(r"[^A-Za-z0-9-]", "-", self.folder))  # Claude's observed folder encoding
        os.makedirs(tdir)
        open(os.path.join(tdir, f"{CONV}.jsonl"), "w").close()  # the conversation exists here

    def tearDown(self):
        self.patch.stop()
        super().tearDown()

    def good(self, **kw):
        sc = dict(argv_file=self.argv_file, init_sid=CONV, init_cwd=self.folder, echo_sid=CONV, result_sid=CONV)
        sc.update(kw)
        self.scene(**sc)

    def resume(self, spawned=None):
        return ClaudeStreamAdapter({}).resume(self.folder, CONV, on_spawn=(spawned.append if spawned is not None else None))

    def turn(self, conn, rid="r_1"):
        text = "[agent-relay handoff — relay turn id: u_0123456789abcdef]\nhello"
        try:
            conn.submit(text, rid, accept_timeout=5)
        except Exception as e:
            return type(e).__name__, []
        evs, end = [], time.time() + 5
        while time.time() < end and not any(e.type in ("turn_completed", "error", "disconnected") for e in evs):
            evs += conn.poll(0.2)
        return "submitted", evs


class ClaudeResumeTests(ClaudeBase):
    def test_resume_argv_never_starts_new_or_forks_and_states_the_restricted_policy(self):
        argv = resume_argv("claude", CONV)
        self.assertEqual(argv[1:4], ["-p", "--resume", CONV])
        for banned in ("--session-id", "--fork-session", "--continue"):
            self.assertNotIn(banned, argv)
        for flag in ("--restricted", "--strict-mcp-config", "--replay-user-messages"):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index("--permission-prompts") + 1], "none")
        self.assertEqual(argv[argv.index("--tools") + 1], "")  # no tools unless asked
        self.good()
        conn = self.resume()
        self.turn(conn)
        self.assertEqual(json.load(open(self.argv_file)), argv[1:])  # exactly what was run
        conn.close()

    def test_ordinary_launch_still_names_a_new_session(self):
        self.good()
        conn = ClaudeStreamAdapter({}).launch(self.folder, {"session_id": CONV})
        self.turn(conn)
        argv = json.load(open(self.argv_file))
        self.assertIn("--session-id", argv)
        self.assertNotIn("--resume", argv)
        conn.close()

    def test_unknown_conversation_or_folder_is_refused_before_any_process(self):
        spawned = []
        with self.assertRaises(InvalidInput):
            ClaudeStreamAdapter({}).resume(self.folder, "99999999-0000-0000-0000-000000000000",
                                           on_spawn=spawned.append)
        other = os.path.join(self.tmp, "elsewhere")
        os.makedirs(other)
        with self.assertRaises(InvalidInput):
            ClaudeStreamAdapter({}).resume(other, CONV, on_spawn=spawned.append)
        self.assertEqual(spawned, [])

    def test_provisional_until_the_client_reports_matching_id_and_folder(self):
        self.good()
        spawned = []
        conn = self.resume(spawned)
        self.assertEqual(spawned[0]["pid"], conn.pid)  # recorded the moment it exists
        self.assertTrue(same_process(spawned[0]))
        self.assertIsNone(conn.confirmed_identity)  # the requested id is NOT confirmation
        self.assertTrue(conn.identity_pending)
        how, evs = self.turn(conn)
        self.assertEqual(how, "submitted")
        self.assertEqual(conn.confirmed_identity, {"client_session_id": CONV, "cwd": self.folder})
        self.assertEqual(evs[-1].type, "turn_completed")
        self.assertIn("CL-REPLY", evs[-1].text)
        conn.close()

    def test_mismatched_id_forwards_nothing(self):
        other = "99999999-0000-0000-0000-000000000000"
        self.good(init_sid=other, echo_sid=other, result_sid=other)
        conn = self.resume()
        how, evs = self.turn(conn)
        self.assertNotEqual(how, "submitted")  # acceptance from the wrong session isn't trusted
        evs = conn.poll(0.5)
        self.assertNotIn("turn_completed", [e.type for e in evs])
        self.assertIsNotNone(conn.identity_error)
        self.assertEqual(conn.status(), "unknown")
        conn.close()

    def test_folder_mismatch_forwards_nothing(self):
        self.good(init_cwd="/somewhere/else")
        conn = self.resume()
        self.turn(conn)
        evs = conn.poll(0.5)
        self.assertNotIn("turn_completed", [e.type for e in evs])
        self.assertIn("folder", conn.identity_error)
        conn.close()

    def test_no_id_and_id_without_folder_forward_nothing(self):
        for sc in (dict(no_init=True, echo_sid=None, result_sid=None),  # never says who it is
                   dict(no_init=True)):                                  # id but never its folder
            self.good(**sc)
            conn = self.resume()
            how, evs = self.turn(conn)
            self.assertNotIn("turn_completed", [e.type for e in evs], sc)
            self.assertTrue(any(e.type == "error" and e.data.get("identity_mismatch") for e in evs), sc)
            conn.close()

    def test_early_exit_before_identity(self):
        for sc in (dict(exit_immediately=True), dict(exit_after_init=True)):
            self.good(**sc)
            conn = self.resume()
            how, evs = self.turn(conn)
            self.assertNotIn("turn_completed", [e.type for e in evs], sc)
            conn.proc.wait(timeout=5)
            self.assertEqual(conn.status(), "disconnected")


class ClaudeHandoffFlowTests(ClaudeBase):
    """Provisional writer inside a real controller: one counted first turn, quarantine."""

    def setUp(self):
        super().setUp()
        self.root = init_root(os.path.join(self.tmp, "flow-root"), ["*"])
        self.bus = Bus(self.root)

    def handed_off(self):
        os.environ["AGENT_RELAY_LIVE_HANDOFF_SCOPE"] = self.folder  # narrowly scoped opt-in
        h = Handoffs(self.bus, {}, lambda a, c: {"client_session_id": c,
                                                 "extra": {"writers": [], "relay_visible": False}})
        h.begin("claude-agents", CONV, self.folder)
        h.advance("claude-agents", CONV)
        st = h.start_writer("claude-agents", CONV)
        self.assertEqual(st["state"], "relay_owned")
        self.assertTrue(st["detail"]["provisional"])
        key = h.key("claude-agents", CONV)
        follower, _ = self.bus.register_session(adapter="claude-agents", client_session_id=CONV, mode="launch",
                                                participant="claude-cl", cwd=self.folder, options={"handoff": True})
        leader = self.launch_endpoint("codex", default={"finish_after": 50})
        pid = self.bus.create_pair(leader=leader, follower=follower["id"], scope="tests only", max_turns=4)["id"]
        self.bus.start_pair(pid, body="go", subject="go", to_leader=True)
        ctl = Controller(self.bus, pid, poll_interval=0.05, borrowed={follower["id"]: h.writers[key]},
                         fences=lambda: {key: h.fence(key)}, before_submit=lambda eid: h.check(key))
        ctl.acquire()
        ctl.connect_all()
        self.controllers.append(ctl)
        return h, key, pid, ctl, follower["id"]

    def run_until_follower_done(self, ctl, pid):
        end = time.time() + 20
        while time.time() < end:
            ctl.step()
            turns = [t for t in self.bus.pair_state(pid)["turns"] if t["role"] == "follower"]
            if turns and turns[0]["phase"] in ("completed", "failed", "uncertain", "timed_out"):
                return turns[0]
            time.sleep(0.05)
        raise AssertionError("follower turn didn't end")

    def follower_messages(self, pid):
        tid = self.bus.get_pair_def(pid)["thread_id"]
        return [m for m in self.bus.thread(tid)["messages"] if m["from"] == "claude-cl"]

    def test_scope_opt_in_is_required_and_global_bypass_does_not_unlock(self):
        h = Handoffs(self.bus, {}, lambda a, c: None)
        self.assertIn("locked", h.supported("claude-agents", self.folder))
        with mock.patch.dict(os.environ, {"AGENT_RELAY_ALLOW_UNVERIFIED": "1"}):
            self.assertIn("locked", h.supported("claude-agents", self.folder))
        with mock.patch.dict(os.environ, {"AGENT_RELAY_LIVE_HANDOFF_SCOPE": os.path.join(self.tmp, "other")}):
            self.assertIn("locked", h.supported("claude-agents", self.folder))
        with mock.patch.dict(os.environ, {"AGENT_RELAY_LIVE_HANDOFF_SCOPE": self.folder}):
            self.assertIsNone(h.supported("claude-agents", self.folder))
            self.assertIsNone(h.supported("codex-local", self.folder))

    def test_matching_first_turn_is_one_counted_turn_and_then_routed(self):
        self.good()
        h, key, pid, ctl, eid = self.handed_off()
        turn = self.run_until_follower_done(ctl, pid)
        self.assertEqual(turn["phase"], "completed")
        msgs = self.follower_messages(pid)
        self.assertEqual(len(msgs), 1)
        self.assertIn("CL-REPLY", msgs[0]["body"])
        st = self.bus.pair_state(pid)
        auth = [e for e in self.root.read_stream(f"pair-{pid}") if e.type == "dispatch_authorized"]
        self.assertEqual(st["used"], len(auth))  # no hidden bootstrap turn
        self.assertIsNone(h.check(key))
        self.assertTrue(h.own.get(key)["spawn"]["confirmed"])
        self.assertEqual(self.bus.get_session(eid)["verified_client_session_id"], CONV)

    def test_mismatch_withholds_output_stops_writer_and_blocks(self):
        other = "99999999-0000-0000-0000-000000000000"
        self.good(init_sid=other, echo_sid=other, result_sid=other)
        h, key, pid, ctl, eid = self.handed_off()
        writer = h.own.get(key)["detail"]["relay"]
        turn = self.run_until_follower_done(ctl, pid)
        self.assertNotEqual(turn["phase"], "completed")
        self.assertEqual(self.follower_messages(pid), [])  # nothing forwarded
        self.assertEqual(self.bus.pair_state(pid)["used"], 2)  # leader + the one counted follower turn
        why = h.check(key)
        self.assertIn("different conversation", why)
        self.assertEqual(h.own.get(key)["state"], "blocked")
        end = time.time() + 10
        while same_process(writer) and time.time() < end:
            time.sleep(0.1)
        self.assertFalse(same_process(writer))  # the writer was stopped
        self.assertIsNone(self.bus.get_session(eid)["verified_client_session_id"])

    def test_no_identity_withholds_output(self):
        self.good(no_init=True, echo_sid=None, result_sid=None)
        h, key, pid, ctl, eid = self.handed_off()
        turn = self.run_until_follower_done(ctl, pid)
        self.assertNotEqual(turn["phase"], "completed")
        self.assertEqual(self.follower_messages(pid), [])
        self.assertIn("different conversation", h.check(key) or "")

    def test_controller_quarantines_a_completion_without_confirmed_identity(self):
        """Defence in depth: even if a writer reported completion, an unconfirmed
        hand-off endpoint's result is withheld at capture."""
        self.good()
        h, key, pid, ctl, eid = self.handed_off()
        conn = h.writers[key]
        real_handle = conn._handle

        def forget_identity(msg):  # the client "completes" without ever confirming
            evs = real_handle(msg)
            conn.confirmed_identity = None
            return evs
        conn._handle = forget_identity
        turn = self.run_until_follower_done(ctl, pid)
        self.assertEqual(turn["phase"], "failed")
        self.assertIn("result withheld", turn["reason"])
        self.assertEqual(self.follower_messages(pid), [])


# = Codex resume writer
class CodexBase(Sim):
    def setUp(self):
        super().setUp()
        self.codex = self.exe("codex", FAKE_CODEX)
        self.patch = mock.patch("relay.adapters.codex._codex", return_value=self.codex)
        self.patch.start()
        self.log = os.path.join(self.tmp, "rpc.log")
        self.home = os.path.join(self.tmp, "codex-home")
        os.makedirs(self.home)
        db = sqlite3.connect(os.path.join(self.home, "state_5.sqlite"))
        db.execute("create table threads (id text, cwd text, archived integer, source text, "
                   "thread_source text, originator text)")
        rows = [(CONV, self.folder, 0, "cli", "user", "codex-tui"),
                ("a" * 8 + "-0000-0000-0000-000000000001", self.folder, 1, "cli", "user", "codex-tui"),
                ("a" * 8 + "-0000-0000-0000-000000000002", self.folder, 0, "vscode", "user", "an-editor-extension"),
                ("a" * 8 + "-0000-0000-0000-000000000003", self.folder, 0, "exec", "user", None),
                ("a" * 8 + "-0000-0000-0000-000000000004", "/elsewhere", 0, "cli", "user", "codex-tui"),
                ("a" * 8 + "-0000-0000-0000-000000000005", self.folder, 0, "cli", "subagent", "codex-tui"),
                ("a" * 8 + "-0000-0000-0000-000000000006", self.folder, 0, "cli", "user", None),
                # A real observed case: a Terminal.app session recorded as source=vscode.
                ("a" * 8 + "-0000-0000-0000-000000000007", self.folder, 0, "vscode", "user", "codex-tui")]
        db.executemany("insert into threads values (?,?,?,?,?,?)", rows)
        db.commit()
        db.close()

    def tearDown(self):
        self.patch.stop()
        super().tearDown()

    # 0.162.1 never reports a profile for the relay's legacy-sandbox request, so a
    # resume the relay accepts needs a server that does. These tests exercise the relay's own
    # verification against such a HYPOTHETICAL answer; `faithful()` is the real 0.162.1 path.
    REPORTED = {"id": ":workspace", "extends": None}

    def good(self, **resume):
        r = dict(id=CONV, cwd=self.folder, activePermissionProfile=self.REPORTED)
        r.update(resume)
        self.scene(log_file=self.log, resume=r)

    def faithful(self, **resume):
        r = dict(id=CONV, cwd=self.folder)
        r.update(resume)
        self.scene(log_file=self.log, resume=r)

    def adapter(self):
        return CodexAppServerAdapter({"codex_home": self.home})

    def requests(self):
        try:
            return [json.loads(l) for l in open(self.log)]
        except FileNotFoundError:
            return []


class CodexResumeTests(CodexBase):
    def test_resume_verifies_identity_folder_and_policy_before_any_prompt(self):
        self.good()
        seen_at_spawn = []
        conn = self.adapter().resume(self.folder, CONV, on_spawn=lambda p: seen_at_spawn.append((p, list(self.requests()))))
        proc, before = seen_at_spawn[0]
        self.assertEqual(before, [])  # recorded before any protocol exchange
        self.assertEqual(proc["pid"], conn.pid)
        methods = [r["method"] for r in self.requests()]
        self.assertEqual(methods[:3], ["initialize", "initialized", "thread/resume"])
        self.assertNotIn("thread/start", methods)
        self.assertNotIn("thread/fork", methods)
        params = next(r["params"] for r in self.requests() if r["method"] == "thread/resume")
        self.assertEqual((params["threadId"], params["approvalPolicy"], params["sandbox"], params["cwd"]),
                         (CONV, "never", "workspace-write", self.folder))
        self.assertEqual(conn.confirmed_identity, {"client_session_id": CONV, "cwd": self.folder})
        self.assertEqual(conn.policy, {  # the complete verified policy, kept for inspection
            "approvalPolicy": "never", "approvalsReviewer": "user",
            "sandbox": {"type": "workspaceWrite", "writableRoots": [], "networkAccess": False,
                        "excludeSlashTmp": True, "excludeTmpdirEnvVar": True},
            "runtimeWorkspaceRoots": [], "activePermissionProfile": {"id": ":workspace", "extends": None}})
        self.assertEqual(params["config"]["sandbox_workspace_write"],
                         {"writable_roots": [], "network_access": False, "exclude_slash_tmp": True,
                          "exclude_tmpdir_env_var": True})  # requested explicitly
        self.assertNotIn("runtimeWorkspaceRoots", params)  # experimental-only: never sent
        # A turn is correlated to this thread and its final text carries the trailer.
        r = conn.submit("[agent-relay handoff — relay turn id: u_0123456789abcdef]", "r_1", accept_timeout=5)
        self.assertEqual(r["client_turn_id"], "turn-1")
        evs, end = [], time.time() + 5
        while time.time() < end and not any(e.type == "turn_completed" for e in evs):
            evs += conn.poll(0.2)
        done = next(e for e in evs if e.type == "turn_completed")
        self.assertEqual(done.request_id, "r_1")
        self.assertIn("```relay", done.text)
        conn.close()
        conn.rpc.proc.wait(timeout=5)
        self.assertFalse(same_process(proc))  # cleanup: exit verified

    def test_server_answers_that_dont_match_are_refused_and_the_child_stopped(self):
        cases = {"id": dict(id="99999999-0000-0000-0000-000000000000"),
                 "folder": dict(cwd="/somewhere/else"),
                 "approval": dict(approvalPolicy="on-request"),
                 "sandbox": dict(sandbox={"type": "dangerFullAccess"}),
                 "busy": dict(status={"type": "active", "activeFlags": []})}
        for name, resume in cases.items():
            self.good(**resume)
            spawned = []
            with self.assertRaises((WrongSession, NotSubmitted), msg=name):
                self.adapter().resume(self.folder, CONV, on_spawn=spawned.append)
            end = time.time() + 5
            while same_process(spawned[0]) and time.time() < end:
                time.sleep(0.05)
            self.assertFalse(same_process(spawned[0]), name)

    def test_local_record_and_outside_writers_are_checked_before_spawning(self):
        self.good()
        spawned = []
        refused = {"unknown": "99999999-0000-0000-0000-000000000000",
                   "archived": "a" * 8 + "-0000-0000-0000-000000000001",
                   "not the terminal app (editor originator)": "a" * 8 + "-0000-0000-0000-000000000002",
                   "exec (no originator)": "a" * 8 + "-0000-0000-0000-000000000003",
                   "other folder": "a" * 8 + "-0000-0000-0000-000000000004",
                   "subagent": "a" * 8 + "-0000-0000-0000-000000000005",
                   "no originator recorded": "a" * 8 + "-0000-0000-0000-000000000006"}
        for name, tid in refused.items():
            with self.assertRaises(InvalidInput, msg=name):
                self.adapter().resume(self.folder, tid, on_spawn=spawned.append)
        for seen in ([{"pid": 1, "writer": "native"}], None):  # open elsewhere / can't tell
            with self.assertRaises(NotSubmitted):
                self.adapter().resume(self.folder, CONV, {"outside_writers": lambda s=seen: s},
                                      on_spawn=spawned.append)
        self.assertEqual(spawned, [])

    def test_terminal_app_conversation_is_accepted_whatever_its_source_field_says(self):
        """Codex 0.160 recorded a Terminal.app session as source=vscode,
        originator=codex-tui. Provenance is the originator, not the source field."""
        tid = "a" * 8 + "-0000-0000-0000-000000000007"
        self.good(id=tid)
        conn = self.adapter().resume(self.folder, tid)
        self.assertEqual(conn.confirmed_identity["client_session_id"], tid)
        conn.close()

    def test_early_exit_fails_fast(self):
        for sc in (dict(exit_immediately=True), dict(exit_on_resume=True)):
            self.scene(log_file=self.log, resume=dict(id=CONV, cwd=self.folder), **sc)
            t0 = time.time()
            with self.assertRaises((NotSubmitted, DeliveryUncertain)):
                self.adapter().resume(self.folder, CONV)
            self.assertLess(time.time() - t0, 10, sc)

    def test_default_launch_still_starts_a_new_thread(self):
        self.scene(log_file=self.log, resume=dict(id=CONV, cwd=self.folder))
        with mock.patch.object(CodexAppServerAdapter, "_init", lambda self, rpc, **kw: None):
            try:
                self.adapter().launch(self.folder)
            except Exception:
                pass  # the fake doesn't implement thread/start's answer; only the request matters
        self.assertIn("thread/start", [r["method"] for r in self.requests()])
        self.assertNotIn("thread/resume", [r["method"] for r in self.requests()])


class CodexReviewerLaunchTests(CodexBase):
    """The review loop's reviewer is launched read-only, approvals never, reviewed by
    you, no network; the answer is verified before any prompt and a mismatch stops the child."""

    def launch(self, **start):  # the server answers as 0.162.1 would, unless a test overrides a field
        self.scene(log_file=self.log, start=start)
        return self.adapter().launch(self.folder, {"policy": "reviewer", "sandbox": "danger-full-access"})

    def test_requests_and_verifies_the_reviewer_policy(self):
        conn = self.launch()
        req = next(r for r in self.requests() if r["method"] == "thread/start")["params"]
        # the named built-in profile instead of the legacy sandbox; never both.
        self.assertEqual((req["config"], req["approvalPolicy"], req["approvalsReviewer"]),
                         ({"default_permissions": ":read-only"}, "never", "user"))
        self.assertNotIn("sandbox", req)  # other launch options (sandbox=danger-full-access) can't widen it
        self.assertNotIn("permissions", req)  # the stable config route, not the experimental field
        init = next(r for r in self.requests() if r["method"] == "initialize")["params"]
        self.assertEqual(init["capabilities"], {"experimentalApi": True})  # for provenance only
        self.assertNotIn("runtimeWorkspaceRoots", req)
        self.assertEqual(conn.policy["sandbox"], {"type": "readOnly", "networkAccess": False})
        self.assertEqual(conn.policy["activePermissionProfile"], {"id": ":read-only", "extends": None})
        self.assertEqual((conn.diagnostic["profile"], conn.diagnostic["profile_id"]), ("reported", ":read-only"))
        conn.close()

    def test_every_failure_closes_the_child_with_evidence(self):
        import signal as _s
        cases = {"refused request": dict(errors={"thread/start": "boom"}),
                 "initialize refused": dict(errors={"initialize": "no"}),
                 "no thread id": dict(start={"id": ""}),
                 "wrong folder": dict(start={"cwd": "/elsewhere"}),
                 "legacy answer (null profile)": dict(start={"activePermissionProfile": None})}
        for why, sc in cases.items():
            with self.subTest(why):
                open(self.log, "w").close()
                self.scene(log_file=self.log, **dict({"start": {}}, **sc))
                with self.assertRaises(Exception) as cm:
                    self.adapter().launch(self.folder, {"policy": "reviewer"})
                child = cm.exception.details["child"]
                self.assertTrue(child["exit_confirmed"], why)
                with self.assertRaises(ProcessLookupError):
                    os.kill(child["pid"], 0)
                self.assertNotIn("turn/start", [r["method"] for r in self.requests()])

    def test_a_malformed_answer_is_a_safe_refusal_with_child_evidence(self):
        # Codex R2: a null result used to raise AttributeError with no child evidence.
        for raw in (None, [], "SECRET-sk-ant-" + "A" * 30, {"thread": None}, {"thread": "th-1"}, {}):
            with self.subTest(raw=raw):
                open(self.log, "w").close()
                self.scene(log_file=self.log, start_raw=raw)
                with self.assertRaises(WrongSession) as cm:
                    self.adapter().launch(self.folder, {"policy": "reviewer"})
                e = cm.exception
                self.assertIn("malformed", str(e))
                self.assertNotIn("SECRET", str(e) + json.dumps(e.details))  # never the raw answer
                self.assertTrue(e.details["child"]["exit_confirmed"])
                with self.assertRaises(ProcessLookupError):
                    os.kill(e.details["child"]["pid"], 0)
                self.assertNotIn("turn/start", [r["method"] for r in self.requests()])
        # Any other unexpected failure inside validation is normalized the same way.
        self.scene(log_file=self.log, start={})
        with mock.patch("relay.adapters.codex.reviewer_policy", side_effect=KeyError("/Users/x/secret")):
            with self.assertRaises(WrongSession) as cm:
                self.adapter().launch(self.folder, {"policy": "reviewer"})
        self.assertEqual(cm.exception.details["unexpected"], "KeyError")
        self.assertNotIn("/Users/x", str(cm.exception))
        self.assertTrue(cm.exception.details["child"]["exit_confirmed"])

    def test_a_wider_answer_is_refused_and_nothing_is_prompted(self):
        for start in ({"sandbox": {"type": "workspaceWrite"}}, {"approvalsReviewer": "auto_review"},
                      {"approvalsReviewer": "guardian_subagent"}, {"approvalsReviewer": "<absent>"},
                      {"approvalsReviewer": None}, {"approvalsReviewer": "someone-new"},
                      {"sandbox": {"type": "readOnly", "networkAccess": True}},
                      {"activePermissionProfile": "<absent>"}, {"activePermissionProfile": None},
                      {"activePermissionProfile": {"id": ":workspace"}},
                      {"activePermissionProfile": {"id": "mine"}},
                      {"activePermissionProfile": {"id": ":read-only", "extends": ":workspace"}},
                      {"activePermissionProfile": {"id": ":read-only", "extra": 1}}):
            with self.subTest(start=start):
                with self.assertRaises(WrongSession):
                    self.launch(**start)
                self.assertNotIn("turn/start", [r["method"] for r in self.requests()])


class CodexHandoffEvidenceTests(CodexBase):
    """Spawn evidence for Codex is current-episode only, and discovery can't see relay writers."""

    def handoffs(self):
        os.environ["AGENT_RELAY_LIVE_HANDOFF_SCOPE"] = self.folder
        self.root = init_root(os.path.join(self.tmp, "flow-root"), ["*"])
        self.bus = Bus(self.root)
        return Handoffs(self.bus, {"codex_home": self.home},
                        lambda a, c: {"client_session_id": c, "extra": {"writers": [], "relay_visible": False}})

    def to_starting(self, h):
        h.begin("codex-local", CONV, self.folder)
        return h.advance("codex-local", CONV)

    def test_repeated_cycles_and_crash_before_and_after_pid_record(self):
        self.good()
        h = self.handoffs()
        key = h.key("codex-local", CONV)
        for cycle in (1, 2):  # two full cycles: resume, verified return
            self.to_starting(h)
            st = h.start_writer("codex-local", CONV)
            self.assertEqual(st["state"], "relay_owned", cycle)
            self.assertFalse(st["detail"].get("provisional"))  # identity verified before any prompt
            h.begin_return(key)
            self.assertEqual(h.stop_writer(key)["state"], "released", cycle)
        ran = []
        for point, expect in (("recorded", "running"), ("launched", "unknown")):
            ran.append(point)
            self.to_starting(h)

            class Crash(Exception):
                pass

            def fault(p, point=point):
                if p == point:
                    raise Crash(p)
            h._fault = fault
            with self.assertRaises(Crash):
                h.start_writer("codex-local", CONV)
            h._fault = lambda p: None
            h.writers.pop(key, None)
            h.starting.clear()
            again = Handoffs(self.bus, {"codex_home": self.home},
                             lambda a, c: {"client_session_id": c, "extra": {"writers": [], "relay_visible": False}})
            self.assertEqual(again.relay_writer_status(key), expect, point)  # older episodes' dead pids don't count
            again.block(key, "interrupted")
            self.assertEqual(again.resolve(key)["state"], "blocked", point)
            # The orphaned app-server child: stop it the way its dying console would.
            import subprocess
            for line in subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True).stdout.splitlines():
                if self.codex in line and "app-server" in line:
                    os.kill(int(line.split()[0]), 9)
            time.sleep(0.5)
            if point == "recorded":  # this episode's own pid proves the exit
                self.assertEqual(again.resolve(key)["state"], "released")
            else:  # nothing in this episode can prove it, and discovery can't see relay writers
                self.assertEqual(again.resolve(key)["state"], "blocked")
        self.assertEqual(ran, ["recorded", "launched"])


class LaunchBoundaryOptInTests(ClaudeBase):
    """The exact-folder opt-in is rechecked at the launch boundary."""

    def setUp(self):
        super().setUp()
        self.root = init_root(os.path.join(self.tmp, "r4-root"), ["*"])
        self.bus = Bus(self.root)
        self.disc = lambda a, c: {"client_session_id": c, "extra": {"writers": [], "relay_visible": False}}

    def pending_then_restart(self, env):
        with mock.patch.dict(os.environ, {"AGENT_RELAY_LIVE_HANDOFF_SCOPE": self.folder}):
            Handoffs(self.bus, {}, self.disc).begin("claude-agents", CONV, self.folder)  # opted in here
        os.environ.pop("AGENT_RELAY_LIVE_HANDOFF_SCOPE", None)
        with mock.patch.dict(os.environ, env):
            again = Handoffs(self.bus, {}, self.disc)  # a restarted console with this environment
            again.advance("claude-agents", CONV)
            st = again.start_writer("claude-agents", CONV)
        return again, st

    def test_restarted_pending_handoff_never_launches_without_the_exact_opt_in(self):
        self.good()
        cases = {"scope unset": {},
                 "scope for another folder": {"AGENT_RELAY_LIVE_HANDOFF_SCOPE": os.path.join(self.tmp, "other")},
                 "broad ALLOW_UNVERIFIED only": {"AGENT_RELAY_ALLOW_UNVERIFIED": "1"}}
        for name, env in cases.items():
            with self.subTest(name):
                again, st = self.pending_then_restart(env)
                self.assertEqual(st["state"], "blocked", name)
                self.assertIn("Not started", st["reason"])
                self.assertFalse(st["spawn"]["intent"])  # not even an intent
                self.assertFalse(os.path.exists(self.argv_file))  # no process, so no prompt
                key = again.key("claude-agents", CONV)
                self.assertNotIn(key, again.writers)
                with mock.patch.dict(os.environ, env):
                    self.assertEqual(again.resolve(key)["state"], "released")  # cleanup needs no opt-in

    def test_exact_scope_still_launches(self):
        self.good()
        again, st = self.pending_then_restart({"AGENT_RELAY_LIVE_HANDOFF_SCOPE": self.folder})
        self.assertEqual(st["state"], "relay_owned")
        self.wait_until(lambda: os.path.exists(self.argv_file))  # the simulated client started
        again.writers[again.key("claude-agents", CONV)].close()


class CodexLaunchBoundaryOptInTests(CodexBase):
    def test_restarted_pending_codex_handoff_never_launches_without_the_opt_in(self):
        self.good()
        root = init_root(os.path.join(self.tmp, "r4c-root"), ["*"])
        bus = Bus(root)
        disc = lambda a, c: {"client_session_id": c, "extra": {"writers": [], "relay_visible": False}}
        with mock.patch.dict(os.environ, {"AGENT_RELAY_LIVE_HANDOFF_SCOPE": self.folder}):
            Handoffs(bus, {"codex_home": self.home}, disc).begin("codex-local", CONV, self.folder)
        os.environ.pop("AGENT_RELAY_LIVE_HANDOFF_SCOPE", None)
        with mock.patch.dict(os.environ, {"AGENT_RELAY_ALLOW_UNVERIFIED": "1"}):
            again = Handoffs(bus, {"codex_home": self.home}, disc)
            again.advance("codex-local", CONV)
            st = again.start_writer("codex-local", CONV)
        self.assertEqual(st["state"], "blocked")
        self.assertEqual(self.requests(), [])  # no app-server ever started



class ProvenanceDiagnosticTests(CodexBase):
    """A refused answer leaves a redacted record of its shape, never its content."""

    BOUNDED = {"type": "workspaceWrite", "writableRoots": [], "networkAccess": False,
               "excludeSlashTmp": True, "excludeTmpdirEnvVar": True}

    def answer(self, **kw):
        return dict({"thread": {"id": CONV, "cwd": self.folder}, "cwd": self.folder, "approvalPolicy": "never",
                     "approvalsReviewer": "user", "sandbox": dict(self.BOUNDED)}, **kw)

    def test_profile_shapes_are_told_apart(self):
        route = "resume:workspace-write"
        for prof, want in (("<absent>", {"profile": "absent"}), (None, {"profile": "null"}),
                           ("workspace", {"profile": "malformed", "profile_type": "str"}),
                           ([":workspace"], {"profile": "malformed", "profile_type": "list"}),
                           ({"id": 3}, {"profile": "malformed", "profile_type": "dict", "profile_fields": ["id"]}),
                           ({"id": ":workspace", "rules": []},
                            {"profile": "malformed", "profile_unknown_fields": 1}),
                           ({"id": ":workspace", "extends": None},
                            {"profile": "reported", "profile_id": ":workspace", "profile_extends": "null"}),
                           ({"id": ":workspace"}, {"profile": "reported", "profile_extends": "absent"}),
                           ({"id": ":read-only", "extends": "mine"}, {"profile": "reported", "profile_extends": "set"})):
            with self.subTest(prof=prof):
                res = self.answer() if prof == "<absent>" else self.answer(activePermissionProfile=prof)
                d = provenance_diagnostic(res, route)
                self.assertEqual({k: d[k] for k in want}, want)
                self.assertEqual(d["route"], route)

    def test_it_is_redacted(self):
        res = self.answer(activePermissionProfile={"id": "alice-wide-open", "extends": "/Users/alice/x"},
                          approvalsReviewer="robot-9000", runtimeWorkspaceRoots=[self.folder, "/Users/alice"],
                          sandbox=dict(self.BOUNDED, writableRoots=["/Users/alice/secret"], type="newKind"),
                          secretField="sk-ant-" + "A" * 40, model="gpt-x", instructionSources=["/Users/alice/AGENTS.md"])
        d = provenance_diagnostic(res, "resume:workspace-write")
        text = json.dumps(d)
        for leak in ("alice", self.folder, CONV, "sk-ant", "robot", "newKind", "secretField", "gpt-x"):
            self.assertNotIn(leak, text)
        self.assertEqual(d["profile_id"], "user-defined")
        self.assertEqual((d["policy"]["approvalsReviewer"], d["policy"]["sandbox_type"]), ("other", "other"))
        self.assertEqual((d["policy"]["writableRoots"], d["policy"]["runtimeWorkspaceRoots"]), (1, 2))
        self.assertEqual(d["unknown_response_fields"], 1)
        self.assertEqual(d["experimental_fields_present"], ["activePermissionProfile", "runtimeWorkspaceRoots"])

    def handoff(self):
        root = init_root(os.path.join(self.tmp, "diag-root-" + os.urandom(3).hex()), ["*"])
        disc = lambda a, c: {"client_session_id": c, "extra": {"writers": [], "relay_visible": False}}
        with mock.patch.dict(os.environ, {"AGENT_RELAY_LIVE_HANDOFF_SCOPE": self.folder}):
            h = Handoffs(Bus(root), {"codex_home": self.home}, disc)
            h.begin("codex-local", CONV, self.folder)
            h.advance("codex-local", CONV)
            return h, h.start_writer("codex-local", CONV)

    def test_team_b_refusal_is_reproduced_and_recorded_with_its_shape(self):
        # Team B (2026-10-11): every legacy check passed and the reviewer was the user; only the
        # provenance was missing. The faithful 0.162.1 answer is null; absent vs null wasn't
        # recorded live, so absent is reproduced too.
        for prof in ("<server>", "<absent>"):
            with self.subTest(prof=prof):
                open(self.log, "w").close()
                self.faithful() if prof == "<server>" else self.faithful(activePermissionProfile=prof)
                h, st = self.handoff()
                self.assertEqual(st["state"], "blocked")
                self.assertIn("didn't report which permission profile is active", st["reason"])
                self.assertNotIn("turn/start", [r["method"] for r in self.requests()])  # no prompt
                d = h.own.get(h.key("codex-local", CONV))["detail"]["diagnostic"]
                self.assertEqual(d["profile"], "absent" if prof == "<absent>" else "null")
                self.assertEqual(d["experimental_fields_present"],
                                 [] if prof == "<absent>" else ["activePermissionProfile"])
                self.assertEqual(d["policy"], {"approvalPolicy": "never", "approvalsReviewer": "user", "sandbox": "dict",
                                               "sandbox_type": "workspaceWrite", "networkAccess": False,
                                               "excludeSlashTmp": True, "excludeTmpdirEnvVar": True,
                                               "writableRoots": 0, "runtimeWorkspaceRoots": "absent"})
                self.assertNotIn(self.folder, json.dumps(d))
                self.assertEqual(h.resolve(h.key("codex-local", CONV))["state"], "released")

    def test_the_reviewer_refusal_carries_its_shape_too(self):
        self.scene(log_file=self.log, start={"activePermissionProfile": None})  # a server that won't say
        with self.assertRaises(WrongSession) as cm:
            self.adapter().launch(self.folder, {"policy": "reviewer"})
        d = cm.exception.details["diagnostic"]
        self.assertEqual((d["route"], d["profile"], d["policy"]["sandbox_type"]), ("launch:reviewer", "null", "readOnly"))

    def test_the_faithful_server_reports_named_profiles_and_never_legacy_ones(self):
        # The simulated server's own rule (not the relay's): only a named request is reported.
        self.faithful()
        conn_args = (self.folder, CONV)
        with self.assertRaises(WrongSession):
            self.adapter().resume(*conn_args)
        req = next(r for r in self.requests() if r["method"] == "thread/resume")["params"]
        self.assertEqual(req["sandbox"], "workspace-write")  # the relay's request is unchanged
        self.assertNotIn("permissions", req)  # no different live request is tried

    def test_a_refusal_before_the_profile_check_still_records_the_shape(self):
        open(self.log, "w").close()
        self.good(approvalsReviewer="auto_review")
        h, st = self.handoff()
        d = h.own.get(h.key("codex-local", CONV))["detail"]["diagnostic"]
        self.assertEqual((d["profile"], d["policy"]["approvalsReviewer"]), ("reported", "auto_review"))


class CodexEffectivePolicyTests(CodexBase):
    """The complete effective sandbox is validated, not just its type."""

    BOUNDED = {"type": "workspaceWrite", "writableRoots": [], "networkAccess": False,
               "excludeSlashTmp": True, "excludeTmpdirEnvVar": True}

    def refused(self, why, **resume):
        self.good(**resume)
        spawned = []
        with self.assertRaises(WrongSession, msg=why) as cm:
            self.adapter().resume(self.folder, CONV, on_spawn=spawned.append)
        end = time.time() + 5
        while same_process(spawned[0]) and time.time() < end:
            time.sleep(0.05)
        self.assertFalse(same_process(spawned[0]), why)  # the child was stopped
        self.assertNotIn("turn/start", [r["method"] for r in self.requests()])  # no prompt
        return str(cm.exception)

    def test_extra_permissions_under_the_same_type_are_refused(self):
        outside = os.path.realpath(os.path.join(self.tmp, "elsewhere"))
        os.makedirs(outside)
        inside = os.path.join(self.folder, "sub")
        os.makedirs(inside)
        link = os.path.join(self.folder, "link-out")
        os.symlink(outside, link)
        cases = {
            "outside writable root (Codex's reproduction)":
                dict(self.BOUNDED, writableRoots=[outside], networkAccess=True),
            "outside writable root": dict(self.BOUNDED, writableRoots=[outside]),
            "parent-path escape": dict(self.BOUNDED, writableRoots=[os.path.join(self.folder, "..", "elsewhere")]),
            "symlink escape": dict(self.BOUNDED, writableRoots=[link]),
            "relative root": dict(self.BOUNDED, writableRoots=["sub"]),
            "network enabled": dict(self.BOUNDED, networkAccess=True),
            "/tmp allowed": dict(self.BOUNDED, excludeSlashTmp=False),
            "$TMPDIR allowed": dict(self.BOUNDED, excludeTmpdirEnvVar=False),
            "temp exclusions omitted (defaults allow them)": {"type": "workspaceWrite"},
            "unknown field": dict(self.BOUNDED, readOnlyAccess={"type": "fullAccess"}),
            "string encoding": "workspace-write",
            "read-only type with network": {"type": "readOnly", "networkAccess": True},
        }
        for why, sandbox in cases.items():
            with self.subTest(why):
                self.refused(why, sandbox=sandbox)
        with self.subTest("runtime root outside"):
            self.refused("runtime root outside", runtimeWorkspaceRoots=[self.folder, outside])
        with self.subTest("user-defined permission profile"):
            self.refused("profile", activePermissionProfile={"id": "my-wide-profile", "extends": None})
        # Codex 0.162.1: provenance and the approvals reviewer are required.
        for why, kw in {
            "profile missing (stable answer)": dict(activePermissionProfile="<absent>"),
            "profile null": dict(activePermissionProfile=None),
            "built-in that extends": dict(activePermissionProfile={"id": ":workspace", "extends": "mine"}),
            "another built-in": dict(activePermissionProfile={"id": ":read-only", "extends": None}),
            "malformed profile": dict(activePermissionProfile={"id": 3}),
            "unknown profile field": dict(activePermissionProfile={"id": ":workspace", "rules": []}),
            "reviewer missing": dict(approvalsReviewer="<absent>"),
            "reviewer null": dict(approvalsReviewer=None),
            "auto reviewer": dict(approvalsReviewer="auto_review"),
            "guardian reviewer": dict(approvalsReviewer="guardian_subagent"),
            "unknown reviewer": dict(approvalsReviewer="robot"),
        }.items():
            with self.subTest(why):
                self.refused(why, **kw)

    def test_resume_requests_the_user_reviewer_and_opts_in_only_for_provenance(self):
        self.good()
        conn = self.adapter().resume(self.folder, CONV)
        init = next(r for r in self.requests() if r["method"] == "initialize")["params"]
        self.assertEqual(init["capabilities"], {"experimentalApi": True})
        req = next(r for r in self.requests() if r["method"] == "thread/resume")["params"]
        self.assertEqual((req["approvalPolicy"], req["approvalsReviewer"], req["sandbox"]),
                         ("never", "user", "workspace-write"))
        self.assertNotIn("runtimeWorkspaceRoots", req)
        self.assertEqual(sorted(set(r["method"] for r in self.requests())), ["initialize", "initialized", "thread/resume"])
        self.assertEqual((conn.policy["approvalsReviewer"], conn.policy["activePermissionProfile"]),
                         ("user", {"id": ":workspace", "extends": None}))
        conn.close()

    def test_bounded_policy_with_defaults_and_inside_roots_is_accepted(self):
        inside = os.path.join(self.folder, "sub")
        os.makedirs(inside)
        # Omitted writableRoots/networkAccess take the schema defaults ([] / false).
        self.good(sandbox={"type": "workspaceWrite", "excludeSlashTmp": True, "excludeTmpdirEnvVar": True},
                  activePermissionProfile={"id": ":workspace", "extends": None})
        conn = self.adapter().resume(self.folder, CONV)
        self.assertEqual(conn.policy["sandbox"]["networkAccess"], False)
        self.assertEqual(conn.policy["sandbox"]["writableRoots"], [])
        self.assertEqual(conn.policy["activePermissionProfile"], {"id": ":workspace", "extends": None})
        conn.close()
        self.good(sandbox=dict(self.BOUNDED, writableRoots=[inside]))
        conn = self.adapter().resume(self.folder, CONV)
        self.assertEqual(conn.policy["sandbox"]["writableRoots"], [os.path.realpath(inside)])
        conn.close()


class RealDiscoveryRouteTests(CodexBase):
    """Real discovered sessions reach the hand-off route only when scoped, and Codex only
    with an exact conversation id. Discovery is injected; nothing real is started."""

    def full_codex_home(self):
        """A simulated Codex home with every column codex-local discovery and resume read."""
        home = os.path.join(self.tmp, "codex-home-full")
        if os.path.isdir(home):
            return home
        os.makedirs(home)
        st = sqlite3.connect(os.path.join(home, "state_5.sqlite"))
        st.execute("create table threads (id text, cwd text, name text, title text, preview text, "
                   "first_user_message text, updated_at_ms integer, archived integer, source text, "
                   "thread_source text, originator text)")
        st.execute("insert into threads values (?,?,?,?,?,?,?,0,'vscode','user','codex-tui')",
                   (CONV, self.folder, "Scratch", None, None, None, int(time.time() * 1000)))
        st.commit()
        th = sqlite3.connect(os.path.join(home, "thread_history_1.sqlite"))
        th.execute("create table thread_turns (thread_id text, rollout_ordinal integer, status text)")
        th.execute("insert into thread_turns values (?,?,?)", (CONV, 1, "completed"))
        th.commit()
        return home

    def host(self, agents, procs):
        from relay.host import Host
        from relay.observer import Discovery
        root = init_root(os.path.join(self.tmp, "route-root-%d" % len(os.listdir(self.tmp))), ["*"])
        opts = {"codex_home": self.full_codex_home(), "_agents": agents, "_procs": procs, "read_titles": False}
        disc = Discovery(opts, ("claude-agents", "codex-local"), interval=60)
        disc.refresh_once()
        h = Host(Bus(root), disc, adapter_options=opts)
        self.addCleanup(h.shutdown)
        return h

    def rows(self, h):
        return {(x["client"], x.get("identity")): x for x in h.catalog()["sessions"]}

    def test_scoped_real_sessions_get_the_handoff_route(self):
        claude_id = "22222222-3333-4444-5555-666666666666"
        agents = [{"sessionId": claude_id, "cwd": self.folder, "pid": os.getpid(), "status": "idle",
                   "kind": "interactive", "startedAt": 1}]
        procs = [{"pid": os.getpid(), "started_ms": 1, "thread": CONV, "cwd": self.folder,
                  "forked_from": None, "argv": ["codex", "resume", CONV]},
                 {"pid": 1, "started_ms": 1, "thread": None, "cwd": self.folder,
                  "forked_from": None, "argv": ["codex"]}]
        os.environ.pop("AGENT_RELAY_LIVE_HANDOFF_SCOPE", None)
        locked = self.host(agents, procs)
        for x in locked.catalog()["sessions"]:
            self.assertNotEqual(x.get("route"), "handoff")  # no scope: locked
            self.assertFalse(x["can_manage"])
        with mock.patch.dict(os.environ, {"AGENT_RELAY_LIVE_HANDOFF_SCOPE": self.folder}):
            h = self.host(agents, procs)
            rows = h.catalog()["sessions"]
            claude = next(x for x in rows if x["client"] == "Claude")
            exact = next(x for x in rows if x["client"] == "Codex" and x["client_session_id"] == CONV)
            plain = next(x for x in rows if x["client"] == "Codex" and x["client_session_id"] != CONV)
            self.assertEqual((claude["route"], claude["ownership"]["state"], claude["can_manage"]),
                             ("handoff", "native", True))
            self.assertEqual((exact["route"], exact["can_manage"]), ("handoff", True))
            self.assertNotEqual(plain.get("route"), "handoff")  # a guess is never handed off
            self.assertFalse(plain["can_manage"])



class TranscriptPathTests(Sim):
    def test_folder_encoding_matches_what_claude_writes(self):
        """Pinned to a real folder name observed in ~/.claude/projects (2.1.x): '/', '.' and
        '_' all become '-'. A mismatch would refuse the resume (fail closed)."""
        from relay.adapters.claude import transcript_path
        cwd = "/private/var/folders/qq/yd27pvf96_n1c1vvjh8_s6l00000gn/T/ts-realagent-eqayz200/repo"
        self.assertEqual(os.path.basename(os.path.dirname(transcript_path(cwd, "x"))),
                         "-private-var-folders-qq-yd27pvf96-n1c1vvjh8-s6l00000gn-T-ts-realagent-eqayz200-repo")


class CodexWriterBusyRetryTests(CodexBase):
    """Codex refused `thread/resume` ("already has an active writer") for a
    while after its terminal quit. The relay waits and retries, bounded, instead of blocking."""

    def handoffs(self, name="busy-root"):
        os.environ["AGENT_RELAY_LIVE_HANDOFF_SCOPE"] = self.folder
        root = init_root(os.path.join(self.tmp, name), ["*"])
        self.bus = Bus(root)
        return Handoffs(self.bus, {"codex_home": self.home},
                        lambda a, c: {"client_session_id": c, "extra": {"writers": [], "relay_visible": False}})

    def busy_for(self, n):
        self.good()
        sc = json.load(open(self.scenario))
        sc["busy_first"] = n
        sc["busy_count_file"] = os.path.join(self.tmp, "busy-count")
        self.scene(**sc)

    def step(self, h):
        st = h.advance("codex-local", CONV)
        if st["state"] == "relay_starting":
            st = h.start_writer("codex-local", CONV)
        return st

    def test_busy_refusals_wait_and_retry_then_succeed(self):
        self.busy_for(2)
        h = self.handoffs()
        h.begin("codex-local", CONV, self.folder)
        st = self.step(h)
        self.assertEqual(st["state"], "awaiting_exit")  # not blocked
        self.assertIn("attempt 1 of 3", st["reason"])
        self.assertEqual(self.step(h).get("exit"), "settling")  # waits before retrying
        with mock.patch("relay.handoff.time.time", return_value=time.time() + 16):
            st = self.step(h)
        self.assertIn("attempt 2 of 3", st["reason"])
        with mock.patch("relay.handoff.time.time", return_value=time.time() + 40):
            st = self.step(h)
        self.assertEqual(st["state"], "relay_owned")
        self.assertNotIn("turn/start", [r["method"] for r in self.requests()])  # nothing was prompted while refused
        h.writers[h.key("codex-local", CONV)].close()

    def test_busy_retries_are_bounded(self):
        self.busy_for(10)
        h = self.handoffs()
        h.begin("codex-local", CONV, self.folder)
        st, t = None, time.time()
        for i in range(6):
            with mock.patch("relay.handoff.time.time", return_value=t + 20 * i):
                st = self.step(h)
            if st["state"] == "blocked":
                break
        self.assertEqual(st["state"], "blocked")
        self.assertIn("already has an active writer", st["reason"])
        self.assertEqual(sum(1 for r in self.requests() if r["method"] == "thread/resume"), 4)  # 1 + 3 retries

    def test_other_refusals_still_block_immediately(self):
        self.good(id="99999999-0000-0000-0000-000000000000")  # wrong thread back: not a busy refusal
        h = self.handoffs()
        h.begin("codex-local", CONV, self.folder)
        self.assertEqual(self.step(h)["state"], "blocked")

    # retry only the definitive, typed `thread/resume` busy refusal, never an error
    # whose text merely contains the phrase.
    def assert_blocks_at_once(self, h, expect_resumes):
        h.begin("codex-local", CONV, self.folder)
        st = self.step(h)
        self.assertEqual(st["state"], "blocked", st["reason"])
        self.assertFalse(st["detail"].get("busy_retries"))
        self.assertIsNone(st["detail"].get("retry_after"))
        methods = [r["method"] for r in self.requests()]
        self.assertEqual(methods.count("thread/resume"), expect_resumes)
        self.assertNotIn("turn/start", methods)
        return st

    def test_wrong_folder_containing_the_phrase_blocks_at_once(self):
        # Codex's exact reproduction: the server reports a folder named like the busy message.
        self.good(cwd="/tmp/already has an active writer")
        st = self.assert_blocks_at_once(self.handoffs(), 1)
        self.assertIn("reports folder", st["reason"])

    def test_phrase_in_other_rpc_refusals_blocks_at_once(self):
        cases = {
            "initialize refused": ({"initialize": "already has an active writer"}, 0),
            "another thread named": ({"thread/resume": "thread 99999999-0000-0000-0000-000000000000 "
                                                       "already has an active writer"}, 1),
            "extra wording": ({"thread/resume": f"thread {CONV} already has an active writer (fatal)"}, 1),
        }
        for name, (errors, resumes) in cases.items():
            with self.subTest(name):
                self.good()
                sc = json.load(open(self.scenario))
                sc["errors"] = errors
                self.scene(**sc)
                if os.path.exists(self.log):
                    os.remove(self.log)
                self.assert_blocks_at_once(self.handoffs(f"root-{len(name)}-{resumes}"), resumes)

    def test_untyped_errors_containing_the_phrase_block_at_once(self):
        # Identity, policy and uncertain-delivery errors stay fail-closed whatever their text.
        for exc in (WrongSession, DeliveryUncertain, NotSubmitted):
            with self.subTest(exc.__name__):
                self.good()
                h = self.handoffs(f"root-{exc.__name__}")

                def resume(cwd, conversation, options=None, *, on_spawn=None, exc=exc):
                    p = subprocess.Popen([sys.executable, "-c", "pass"])
                    on_spawn({"pid": p.pid, "started_ms": process_start_ms(p.pid)})
                    p.wait()  # the refused writer is verifiably gone, as in a busy refusal
                    raise exc(f"thread {conversation} already has an active writer")
                with mock.patch("relay.adapters.codex.CodexAppServerAdapter.resume", side_effect=resume):
                    h.begin("codex-local", CONV, self.folder)
                    st = self.step(h)
                self.assertEqual(st["state"], "blocked")
                self.assertFalse(st["detail"].get("busy_retries"))
