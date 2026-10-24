"""Codex app-server adapter (UNVERIFIED — see docs/INTEGRATION-FEASIBILITY.md).

Written against the JSON Schema that `codex app-server generate-json-schema`
emits for codex-cli 0.156.1. It has not been run against a live Codex session;
live use requires explicit authorization and AGENT_RELAY_ALLOW_UNVERIFIED=1.

* launch: child `codex app-server` over stdio, `thread/start`, `turn/start`.
* attach: `codex app-server proxy` to the running shared daemon, then
  `thread/resume{threadId}`, which the schema documents as rejoining a running
  thread. Only threads loaded in that daemon (TUIs started with
  `--remote unix://`) are reachable; standalone TUIs are not.

Approval requests (server requests) are surfaced as `approval_pending` and never
answered by the relay.
"""

from __future__ import annotations

import json
import os
import queue
import re
import secrets
import shutil
import subprocess
import threading
import time
from typing import Any

from ..storage import InvalidInput, NotFound, RelayError, Unsupported
from .base import (CAPABILITIES, WriterBusy, Adapter, Connection, DeliveryUncertain, LifecycleEvent,
                   NotSubmitted, SessionInfo, WrongSession)

APPROVAL_METHODS = {"item/commandExecution/requestApproval", "item/fileChange/requestApproval",
                    "item/permissions/requestApproval", "applyPatchApproval", "execCommandApproval"}
QUESTION_METHODS = {"item/tool/requestUserInput", "mcpServer/elicitation/request"}


def _codex() -> str:
    exe = shutil.which("codex")
    if not exe:
        raise Unsupported("codex CLI not found on PATH")
    return exe


SANDBOX_FIELDS = {"type", "writableRoots", "networkAccess", "excludeSlashTmp", "excludeTmpdirEnvVar"}
# Omitted fields take the defaults documented in the installed 0.160.0 schema (unchanged in 0.162.1), and are
# then validated like any other value (so an omitted temp exclusion is rejected).
SANDBOX_DEFAULTS = {"writableRoots": [], "networkAccess": False, "excludeSlashTmp": False,
                    "excludeTmpdirEnvVar": False}


def _inside(path, folder: str) -> bool:
    if not isinstance(path, str) or not os.path.isabs(path):
        return False
    real = os.path.realpath(path)  # resolves `..` and symlinks: escapes are caught
    return real == folder or real.startswith(folder.rstrip("/") + "/")


# Codex 0.162: the legacy `sandbox` field alone
# can't prove that no user-defined permission profile is active; only the experimental
# `activePermissionProfile` provenance can, and the relay opts into the experimental API for
# exactly that. Built-in ids per route, as found in the 0.162.1 binary (`:workspace` is also the
# schema's own example). Missing, null, custom, extended or unknown provenance refuses.
# Codex 0.162.1 source: a typed legacy `sandbox` request is always answered with a null
# profile, and the built-in `:workspace` itself allows /tmp and $TMPDIR, which the legacy checks
# in effective_policy refuse. So the resume route can't pass today: it stays fail-closed until a
# supported contract is reviewed.
PROFILE_FOR = {"workspace-write": ":workspace", "read-only": ":read-only"}
REVIEWER_PROFILE = ":read-only"


# A redacted diagnostic of an answer the relay refused. Only shapes, schema field
# names, enum values from the 0.162.1 schema (anything else reads "other"), booleans and counts:
# never paths, ids of user-defined profiles, raw responses, tokens or conversation content.
RESPONSE_FIELDS = frozenset((
    "activePermissionProfile", "approvalPolicy", "approvalsReviewer", "collaborationMode", "cwd",
    "disabledPluginIds", "initialTurnsPage", "instructionSources", "itemsBackwardsCursor", "model",
    "modelProvider", "multiAgentMode", "reasoningEffort", "runtimeWorkspaceRoots", "sandbox",
    "serviceTier", "thread", "turnsBackwardsCursor"))
# In the 0.162.1 schemas these appear only in the experimental ThreadResume/StartResponse. If
# any is present, the experimental opt-in took effect for this answer.
EXPERIMENTAL_ONLY = ("activePermissionProfile", "initialTurnsPage", "multiAgentMode", "runtimeWorkspaceRoots")
_ENUMS = {"approvalPolicy": ("never", "on-request", "on-failure", "untrusted"),
          "approvalsReviewer": ("user", "auto_review", "guardian_subagent"),
          "sandbox_type": ("workspaceWrite", "readOnly", "dangerFullAccess", "externalSandbox")}
_BUILTIN_ID = re.compile(r"^:[a-z][a-z0-9_-]{0,31}$")


_ABSENT = object()


def _enum(kind: str, v: Any) -> Any:
    return v if v in _ENUMS[kind] else ("absent" if v is _ABSENT else "null" if v is None else "other")


def _shape(v: Any) -> str:
    return "absent" if v is _ABSENT else "null" if v is None else type(v).__name__


def provenance_diagnostic(res: Any, route: str) -> dict:
    """What the server's answer looked like, for diagnosing a refusal, redacted."""
    if not isinstance(res, dict):
        return {"route": route, "response": _shape(res)}
    prof = res.get("activePermissionProfile", _ABSENT)
    d: dict = {"route": route, "experimental_opt_in": True,
               "profile": ("absent" if prof is _ABSENT else "null" if prof is None else
                           "reported" if isinstance(prof, dict) and set(prof) <= {"id", "extends"}
                           and isinstance(prof.get("id"), str) else "malformed")}
    if d["profile"] == "malformed":
        d["profile_type"] = _shape(prof)
        if isinstance(prof, dict):
            d["profile_fields"] = sorted(k for k in prof if k in ("id", "extends"))
            d["profile_unknown_fields"] = len(set(prof) - {"id", "extends"})
    if d["profile"] == "reported":
        d["profile_id"] = prof["id"] if _BUILTIN_ID.match(prof["id"]) else "user-defined"
        d["profile_extends"] = _shape(prof.get("extends", _ABSENT)) if prof.get("extends") is None else "set"
    d["experimental_fields_present"] = [k for k in EXPERIMENTAL_ONLY if k in res]
    d["response_fields"] = sorted(k for k in res if k in RESPONSE_FIELDS)
    d["unknown_response_fields"] = len([k for k in res if k not in RESPONSE_FIELDS])
    sb = res.get("sandbox", _ABSENT)
    pol = {"approvalPolicy": _enum("approvalPolicy", res.get("approvalPolicy", _ABSENT)),
           "approvalsReviewer": _enum("approvalsReviewer", res.get("approvalsReviewer", _ABSENT)),
           "sandbox": _shape(sb)}
    if isinstance(sb, dict):
        pol["sandbox_type"] = _enum("sandbox_type", sb.get("type", _ABSENT))
        for k in ("networkAccess", "excludeSlashTmp", "excludeTmpdirEnvVar"):
            v = sb.get(k, _ABSENT)
            pol[k] = v if isinstance(v, bool) else _shape(v)
        roots = sb.get("writableRoots", _ABSENT)
        pol["writableRoots"] = len(roots) if isinstance(roots, list) else _shape(roots)
    rt = res.get("runtimeWorkspaceRoots", _ABSENT)
    pol["runtimeWorkspaceRoots"] = len(rt) if isinstance(rt, list) else _shape(rt)
    d["policy"] = pol
    return d


def builtin_profile(res: dict, route: str) -> dict:
    want = PROFILE_FOR[route]
    profile = res.get("activePermissionProfile")
    if not isinstance(profile, dict):
        raise WrongSession("Codex didn't report which permission profile is active, so the policy "
                           "in effect can't be verified")
    if set(profile) - {"id", "extends"} or not isinstance(profile.get("id"), str):
        raise WrongSession(f"unrecognized permission profile record {profile!r}")
    if profile.get("extends") is not None:
        raise WrongSession(f"the active permission profile extends {profile.get('extends')!r}")
    if profile["id"] != want:
        raise WrongSession(f"the active permission profile is {profile['id']!r}, not the built-in {want!r}")
    return {"id": want, "extends": None}


def user_reviews_approvals(res: dict) -> str:
    if res.get("approvalsReviewer") != "user":  # missing, null, auto_review, guardian_subagent, unknown
        raise WrongSession(f"approvals would be reviewed by {res.get('approvalsReviewer')!r}, not you")
    return "user"


def effective_policy(res: dict, cwd: str) -> dict:
    """The policy a resumed Codex thread actually runs with, validated as EXACTLY the
    bounded hand-off policy: approvals never; workspace-write confined to `cwd` (every
    writable/runtime root canonicalized inside it), no network, no /tmp or $TMPDIR
    exceptions, no user-defined permission profile. Anything else, including unknown
    fields or encodings, is refused (fail closed)."""
    folder = os.path.realpath(cwd)
    if res.get("approvalPolicy") != "never":
        raise WrongSession(f"approval policy in effect is {res.get('approvalPolicy')!r}, not 'never'")
    sb = res.get("sandbox")
    if not isinstance(sb, dict):
        raise WrongSession(f"unsupported sandbox encoding {sb!r}")
    extra = set(sb) - SANDBOX_FIELDS
    if extra:
        raise WrongSession(f"sandbox has fields the relay doesn't verify: {sorted(extra)}")
    sb = dict(SANDBOX_DEFAULTS, **sb)
    if sb["type"] != "workspaceWrite":
        raise WrongSession(f"sandbox in effect is {sb['type']!r}, not workspaceWrite")
    if sb["networkAccess"] is not False:
        raise WrongSession("network access is enabled")
    if sb["excludeSlashTmp"] is not True or sb["excludeTmpdirEnvVar"] is not True:
        raise WrongSession("the sandbox would also allow writes to /tmp or $TMPDIR")
    roots = sb["writableRoots"]
    if not isinstance(roots, list) or not all(_inside(r, folder) for r in roots):
        raise WrongSession(f"writable roots reach outside {folder}: {roots!r}")
    runtime = res.get("runtimeWorkspaceRoots", [])
    if not isinstance(runtime, list) or not all(_inside(r, folder) for r in runtime):
        raise WrongSession(f"workspace roots reach outside {folder}: {runtime!r}")
    reviewer = user_reviews_approvals(res)
    profile = builtin_profile(res, "workspace-write")
    return {"approvalPolicy": "never", "approvalsReviewer": reviewer,
            "sandbox": {**sb, "writableRoots": [os.path.realpath(r) for r in roots]},
            "runtimeWorkspaceRoots": [os.path.realpath(r) for r in runtime],
            "activePermissionProfile": profile}


def reviewer_policy(res: dict) -> dict:
    """The review loop's reviewer: approvals never, a read-only sandbox with no
    network, approvals reviewed by the user (never an automatic reviewer), and no
    user-defined permission profile. Verified from the server's answer before any prompt;
    anything else is refused (fail closed)."""
    if res.get("approvalPolicy") != "never":
        raise WrongSession(f"approval policy in effect is {res.get('approvalPolicy')!r}, not 'never'")
    user_reviews_approvals(res)
    sb = res.get("sandbox")
    if not isinstance(sb, dict) or set(sb) - {"type", "networkAccess"}:
        raise WrongSession(f"unsupported sandbox encoding {sb!r}")
    if sb.get("type") != "readOnly":
        raise WrongSession(f"sandbox in effect is {sb.get('type')!r}, not readOnly")
    if sb.get("networkAccess", False) is not False:
        raise WrongSession("network access is enabled")
    profile = builtin_profile(res, "read-only")
    return {"approvalPolicy": "never", "approvalsReviewer": "user",
            "sandbox": {"type": "readOnly", "networkAccess": False}, "activePermissionProfile": profile}


def local_thread(conversation: str, cwd: str, codex_home: str | None = None) -> dict:
    """Read-only check of Codex's own record for a conversation (id, folder, provenance)."""
    import sqlite3
    home = codex_home or os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")
    path = os.path.join(home, "state_5.sqlite")
    if not os.path.exists(path):
        raise InvalidInput("can't read Codex's local records to confirm the conversation")
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
        try:
            row = con.execute("select id, cwd, archived, thread_source, originator from threads where id=?",
                              (conversation,)).fetchone()
        finally:
            con.close()
    except sqlite3.Error as e:  # e.g. an older schema without `originator`: refuse, don't guess
        raise InvalidInput(f"can't read Codex's local record for this conversation ({e})")
    if not row:
        raise InvalidInput(f"Codex has no conversation {conversation[:8]}…")
    if os.path.realpath(row[1] or "") != os.path.realpath(cwd):
        raise InvalidInput(f"Codex conversation {conversation[:8]}… belongs to another folder")
    if row[2]:
        raise InvalidInput("that Codex conversation is archived")
    # Terminal provenance comes from `originator` (the client that created the thread):
    # only the Codex terminal app, started by a person. `source` is NOT used: a real
    # session started in Terminal.app was recorded with source="vscode" (observed
    # 2026-10-04), so it doesn't reliably mean "an editor chat". No originator → refused.
    if (row[3], row[4]) != ("user", "codex-tui"):
        raise InvalidInput(f"that Codex conversation wasn't started in the Codex terminal app "
                           f"(originator {row[4]!r}, thread source {row[3]!r}); "
                           "the relay only hands over terminal conversations")
    return {"id": row[0], "cwd": row[1]}


WRITER_BUSY = "already has an active writer"  # Codex's wording for a held conversation


class RpcError(RelayError):
    """The server answered with a JSON-RPC error: the request was definitively refused."""


def thread_activity(status: dict | None) -> str:
    """Map a Codex ThreadStatus to relay activity; anything unexpected is 'unknown'."""
    if not isinstance(status, dict):
        return "unknown"
    t = status.get("type")
    if t == "idle":
        return "idle"
    if t == "active":
        flags = status.get("activeFlags") or []
        return "waiting" if any(f in ("waitingOnApproval", "waitingOnUserInput") for f in flags) \
            else "busy"
    if t == "notLoaded":
        return "disconnected"
    return "unknown"  # systemError or a future status


class RpcProcess:
    def __init__(self, argv: list[str], cwd: str | None = None):
        self.proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, cwd=cwd, bufsize=0)
        self.q: queue.Queue = queue.Queue()
        self.responses: dict[int, dict] = {}
        self.cv = threading.Condition()
        self.next_id = 1
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self) -> None:
        for line in self.proc.stdout:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if "id" in msg and ("result" in msg or "error" in msg) and "method" not in msg:
                with self.cv:
                    self.responses[msg["id"]] = msg
                    self.cv.notify_all()
            else:
                self.q.put(msg)
        with self.cv:
            self.eof = True  # the server is gone: pending requests fail now, not at timeout
            self.cv.notify_all()
        self.q.put({"method": "__eof__"})

    eof = False

    def request(self, method: str, params: dict, timeout: float = 30.0) -> dict:
        rid = self.next_id
        self.next_id += 1
        try:
            self.proc.stdin.write((json.dumps({"jsonrpc": "2.0", "id": rid, "method": method,
                                               "params": params}) + "\n").encode())
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            raise NotSubmitted(f"{method}: app-server isn't running ({e})")
        deadline = time.monotonic() + timeout
        with self.cv:
            while rid not in self.responses:
                if self.eof:
                    raise DeliveryUncertain(f"{method}: app-server exited before answering")
                left = deadline - time.monotonic()
                if left <= 0:
                    raise DeliveryUncertain(f"{method}: no response within {timeout}s")
                self.cv.wait(left)
            msg = self.responses.pop(rid)
        if "error" in msg:
            raise RpcError(f"{method}: {msg['error'].get('message')}", rpc_code=msg["error"].get("code"),
                           rpc_message=msg["error"].get("message"))
        return msg["result"]

    def notify(self, method: str, params: dict | None = None) -> None:
        self.proc.stdin.write((json.dumps({"jsonrpc": "2.0", "method": method,
                                           **({"params": params} if params else {})}) + "\n").encode())
        self.proc.stdin.flush()

    def close(self) -> bool:
        """Stop the child; True only once its exit is confirmed (never assumed)."""
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    return False
        return self.proc.poll() is not None


def _refuse(rpc: "RpcProcess", e: Exception, started_ms) -> RelayError:
    """A launch that won't be used: close its child and record (on the error) which process
    it was and whether its exit was confirmed, so the caller keeps honest evidence.
    Anything that isn't already a relay error (e.g. a malformed answer) becomes a safe one:
    its text never carries the raw response or exception message."""
    exited = rpc.close()
    if not isinstance(e, RelayError):
        e = WrongSession("Codex's answer to the reviewer launch was malformed; the relay stopped it",
                         unexpected=type(e).__name__)
    e.details["child"] = {"pid": rpc.proc.pid, "started_ms": started_ms, "exit_confirmed": exited}
    return e


class CodexConnection(Connection):
    def __init__(self, rpc: RpcProcess, thread_id: str, mode: str,
                 initial_status: dict | None = None):
        self.rpc, self.client_session_id, self.mode = rpc, thread_id, mode
        self.connection_id = "c_" + secrets.token_hex(8)
        self.turn_to_request: dict[str, str] = {}
        self.final_text: dict[str, str] = {}
        # Activity comes only from the server: the initial thread status and later
        # thread/status/changed and turn notifications. Unknown fails closed.
        self.activity = thread_activity(initial_status)
        self.active_turn: str | None = None
        self.open_requests: dict = {}  # JSON-RPC server-request id -> turn id
        self.backlog: list[LifecycleEvent] = []  # translated while draining, not yet polled

    def _drain(self) -> None:
        """Apply queued notifications so status() reflects the server's latest view."""
        while True:
            try:
                msg = self.rpc.q.get_nowait()
            except queue.Empty:
                return
            ev = self._translate(msg)
            if ev:
                self.backlog.append(ev)

    def submit(self, text: str, request_id: str, *, accept_timeout: float = 30.0) -> dict:
        self._drain()
        if self.status() != "idle":
            raise NotSubmitted(f"thread is {self.status()}; not submitting")
        try:
            res = self.rpc.request("turn/start", {"threadId": self.client_session_id,
                                                  "input": [{"type": "text", "text": text}],
                                                  "clientUserMessageId": request_id}, accept_timeout)
        except RpcError as e:
            raise NotSubmitted(f"server refused the turn: {e}")
        turn_id = res["turn"]["id"]
        self.turn_to_request[turn_id] = request_id
        self.active_turn = turn_id
        self.activity = "busy"
        return {"client_turn_id": turn_id}

    def poll(self, timeout: float) -> list[LifecycleEvent]:
        out, deadline = self.backlog, time.monotonic() + timeout
        self.backlog = []
        while True:
            try:
                msg = self.rpc.q.get(timeout=max(0.0, min(0.05, deadline - time.monotonic())))
            except queue.Empty:
                if out or time.monotonic() >= deadline:
                    return out
                continue
            ev = self._translate(msg)
            if ev:
                out.append(ev)

    def _translate(self, msg: dict) -> LifecycleEvent | None:
        m, p = msg.get("method"), msg.get("params") or {}
        if m == "__eof__":
            self.activity = "disconnected"
            return LifecycleEvent("disconnected", text="app-server connection closed")
        if p.get("threadId") not in (None, self.client_session_id):
            return None
        tid = p.get("turnId") or (p.get("turn") or {}).get("id")
        rid = self.turn_to_request.get(tid)
        if m == "thread/status/changed":
            self.activity = thread_activity(p.get("status"))
            return None
        if m in APPROVAL_METHODS or m in QUESTION_METHODS:
            if "id" in msg:  # remember the native request so its resolution correlates
                self.open_requests[msg["id"]] = tid
            return LifecycleEvent("approval_pending" if m in APPROVAL_METHODS else "human_question",
                                  rid, tid, m, {"native_request_id": msg.get("id")})
        if m == "serverRequest/resolved":
            req = p.get("requestId")
            if req not in self.open_requests:
                return None  # not a request this connection saw
            tid = self.open_requests.pop(req)
            rid = self.turn_to_request.get(tid)
            if any(t == tid for t in self.open_requests.values()):
                return None  # other requests on this turn are still waiting
            return LifecycleEvent("approval_resolved", rid, tid, None, {"native_request_id": req})
        if m == "turn/started":
            self.active_turn = tid
            if self.activity == "idle":
                self.activity = "busy"
            return LifecycleEvent("turn_started", rid, tid)
        if m == "item/agentMessage/delta":
            return LifecycleEvent("delta", rid, tid, p.get("delta"))
        if m == "item/completed":
            item = p.get("item") or {}
            if item.get("type") == "agentMessage" and item.get("phase") != "commentary":
                self.final_text[tid] = item.get("text", "")
            elif item.get("type") == "userMessage":
                return LifecycleEvent("echo", rid, tid)
            return LifecycleEvent("tool", rid, tid, item.get("type"))
        if m == "turn/completed":
            if self.active_turn == tid:
                self.active_turn = None
                if self.activity in ("busy", "waiting"):
                    self.activity = "idle"
            self.open_requests = {k: v for k, v in self.open_requests.items() if v != tid}
            turn = p.get("turn") or {}
            if turn.get("status") != "completed":
                return LifecycleEvent("error", rid, tid, f"turn {turn.get('status')}: {turn.get('error')}")
            return LifecycleEvent("turn_completed", rid, tid, self.final_text.pop(tid, ""))
        if m == "error":
            return LifecycleEvent("error", rid, tid, json.dumps(p)[:500])
        return None

    def lookup(self, request_id: str) -> dict | None:
        raise Unsupported("reconciliation via thread/turns/list is not implemented yet")

    def status(self) -> str:
        if self.rpc.proc.poll() is not None:
            return "disconnected"
        self._drain()
        return self.activity

    def interrupt(self) -> None:
        if not self.active_turn:
            raise Unsupported("no active turn to interrupt")
        self.rpc.request("turn/interrupt", {"threadId": self.client_session_id,
                                            "turnId": self.active_turn})

    def detach(self) -> None:
        if self.mode == "attach":
            try:
                self.rpc.request("thread/unsubscribe", {"threadId": self.client_session_id}, 5)
            except RelayError:
                pass
        self.rpc.close()

    def close(self) -> None:
        if self.mode != "launch":
            raise Unsupported("refusing to close a session the relay did not launch")
        self.rpc.close()


class CodexAppServerAdapter(Adapter):
    name = "codex-app-server"
    verified = False

    def capabilities(self) -> dict[str, bool]:
        caps = {c: False for c in CAPABILITIES}
        caps.update(discover=True, attach_live=True, launch=True, submit=True, correlate=True,
                    turn_complete=True, approvals_visible=True, busy_state=True, interrupt=True,
                    detach=True)
        return caps

    def namespace(self) -> str:
        return "codex:" + os.path.realpath(os.path.expanduser("~/.codex"))

    def _init(self, rpc: RpcProcess, *, provenance: bool = False) -> None:
        """`provenance=True` (the scoped resume and reviewer routes only) opts into the
        experimental API solely to receive `activePermissionProfile`. No experimental method
        or request field is sent because of it."""
        params = {"clientInfo": {"name": "agent-relay", "version": "0.1.0"}}
        if provenance:
            params["capabilities"] = {"experimentalApi": True}
        rpc.request("initialize", params)
        rpc.notify("initialized")

    def _daemon(self) -> RpcProcess:
        sock = os.path.expanduser("~/.codex/app-server-control/app-server-control.sock")
        if not os.path.exists(sock):
            raise Unsupported("the shared Codex app-server daemon is not running; standalone Codex "
                              "TUIs cannot be attached (start Codex with --remote unix:// to share)")
        rpc = RpcProcess([_codex(), "app-server", "proxy"])
        self._init(rpc)
        return rpc

    def discover(self) -> list[SessionInfo]:
        rpc = self._daemon()
        out = []
        try:
            for t in rpc.request("thread/loaded/list", {}).get("data", []):
                try:
                    th = rpc.request("thread/read", {"threadId": t}).get("thread") or {}
                except RelayError:
                    th = {}
                out.append(SessionInfo(t, th.get("cwd"), thread_activity(th.get("status")), True,
                                       ["unverified adapter"],
                                       {"name": th.get("name"), "preview": th.get("preview")}))
        finally:
            rpc.close()
        return out

    def attach(self, client_session_id: str) -> Connection:
        rpc = self._daemon()
        try:
            # Prove the target is live right now; never reopen stored history instead.
            loaded = rpc.request("thread/loaded/list", {}).get("data", [])
            if client_session_id not in loaded:
                raise NotSubmitted(f"Codex thread {client_session_id} is not loaded in the shared "
                                   "daemon; refusing to reopen it from stored history")
            res = rpc.request("thread/resume", {"threadId": client_session_id, "excludeTurns": True})
            thread = res.get("thread") or {}
            if thread.get("id") != client_session_id:
                raise WrongSession(f"daemon returned thread {thread.get('id')}, expected "
                                   f"{client_session_id}")
        except Exception:
            rpc.close()
            raise
        conn = CodexConnection(rpc, client_session_id, "attach", thread.get("status"))
        conn.cwd = res.get("cwd") or thread.get("cwd")
        conn.confirmed_identity = {"client_session_id": thread["id"], "cwd": conn.cwd}
        return conn

    def resume(self, cwd: str, conversation: str, options: dict | None = None, *,
               on_spawn=None) -> Connection:
        """Continue an EXISTING Codex conversation as its only writer.

        Never thread/start or fork in its place. Before any model prompt:
        * the local record (read-only) must name this thread in this canonical folder,
          not archived, created by the Codex terminal app for a person (originator=codex-tui,
          thread_source=user), so app/IDE, exec and subagent conversations are refused;
        * no outside writer may be visible (`outside_writers()` -> list, or None = unknown);
        * the relay-owned stdio app-server is reported via `on_spawn` the moment it exists;
        * `thread/resume` must return this thread id, this folder, and the restricted
          policy actually in effect (approvalPolicy never, workspace-write sandbox)."""
        from ..context import process_start_ms
        opts = dict(options or {})
        cwd = os.path.realpath(cwd)
        local_thread(conversation, cwd, opts.get("codex_home") or self.options.get("codex_home"))
        outside = opts.get("outside_writers")
        if outside is not None:
            seen = outside()
            if seen is None:
                raise NotSubmitted("can't tell whether another Codex process has this conversation open")
            if seen:
                raise NotSubmitted("this conversation is open in another Codex process")
        rpc = RpcProcess([_codex(), "app-server"], cwd=cwd)
        if on_spawn is not None:
            on_spawn({"pid": rpc.proc.pid, "started_ms": process_start_ms(rpc.proc.pid)})
        try:
            self._init(rpc, provenance=True)
            try:
                res = rpc.request("thread/resume", {
                    "threadId": conversation, "cwd": cwd, "excludeTurns": True,
                    "approvalPolicy": "never", "approvalsReviewer": "user", "sandbox": "workspace-write",
                    # No `runtimeWorkspaceRoots`: it's an experimental request field (0.160, 0.162).
                    # The relay opts into the experimental API here only to receive the active
                    # profile's provenance, never to send experimental fields or methods (Codex,
                    # 2026-10-11). The workspace is `cwd`; any runtime roots the server reports are
                    # still verified to lie inside it (effective_policy).
                    # The intended bounded policy, requested explicitly (never inherited):
                    # no extra writable roots, no network, and no /tmp or $TMPDIR exceptions.
                    "config": {"sandbox_workspace_write": {"writable_roots": [], "network_access": False,
                                                           "exclude_slash_tmp": True,
                                                           "exclude_tmpdir_env_var": True}}})
            except RpcError as e:
                # Only the server's own refusal of THIS resume, worded exactly as Codex words
                # it ("thread <id> already has an active writer"), is retryable. Any other
                # error, even one whose text contains the phrase (a folder name, an identity
                # or policy mismatch, an uncertain delivery), stays fail-closed.
                if e.details.get("rpc_message") == f"thread {conversation} {WRITER_BUSY}":
                    raise WriterBusy(str(e)) from e
                raise
            thread = res.get("thread") or {}
            if thread.get("id") != conversation:
                raise WrongSession(f"app-server resumed thread {thread.get('id')}, expected {conversation}")
            got_cwd = res.get("cwd") or thread.get("cwd")
            if os.path.realpath(got_cwd or "") != cwd:
                raise WrongSession(f"app-server reports folder {got_cwd!r}, expected {cwd!r}")
            try:  # raises WrongSession unless exactly the bounded policy
                policy = effective_policy(res, cwd)
            except WrongSession as e:  # what the answer looked like, redacted
                e.details["diagnostic"] = provenance_diagnostic(res, "resume:workspace-write")
                raise
            if thread_activity(thread.get("status")) not in ("idle",):
                raise NotSubmitted(f"thread is {thread_activity(thread.get('status'))} after resume")
        except Exception:
            rpc.close()  # its exit is then verified by the caller, never assumed
            raise
        conn = CodexConnection(rpc, conversation, "launch", thread.get("status") or {"type": "idle"})
        conn.cwd = got_cwd
        conn.confirmed_identity = {"client_session_id": conversation, "cwd": got_cwd}
        conn.pid = rpc.proc.pid
        conn.policy = policy  # the complete verified effective policy, kept for inspection
        return conn

    def launch(self, cwd: str, options: dict | None = None, *, on_spawn=None) -> Connection:
        opts = dict(self.options.get("launch", {}), **(options or {}))
        reviewer = opts.get("policy") == "reviewer"
        rpc = RpcProcess([_codex(), "app-server"], cwd=cwd)
        from ..context import process_start_ms
        started_ms = process_start_ms(rpc.proc.pid)
        if on_spawn is not None:  # (the diagnostic) the child, and its handle, before any request
            on_spawn({"pid": rpc.proc.pid, "started_ms": started_ms, "proc": rpc.proc})
        if reviewer:
            # the named built-in `:read-only` profile,
            # selected through the stable `config.default_permissions`, INSTEAD OF the legacy
            # `sandbox` (which 0.162.1 answers with no profile). Never both. Fixed: other launch
            # options can't change it.
            params = {"cwd": cwd, "approvalPolicy": "never", "approvalsReviewer": "user",
                      "config": {"default_permissions": REVIEWER_PROFILE}}
        else:
            params = {"cwd": cwd, "approvalPolicy": opts.get("approvalPolicy", "never"),
                      "sandbox": opts.get("sandbox", "workspace-write")}
        res = None
        try:
            self._init(rpc, provenance=reviewer)
            res = rpc.request("thread/start", params)
            if not isinstance(res, dict) or not isinstance(res.get("thread"), dict):
                raise WrongSession("Codex's answer to the reviewer launch was malformed; the relay stopped it")
            thread = res["thread"]
            if not thread.get("id"):
                raise NotFound("thread/start returned no thread id")
            got_cwd = res.get("cwd") or thread.get("cwd")
            if reviewer and os.path.realpath(got_cwd or "") != os.path.realpath(cwd):
                raise WrongSession(f"app-server reports folder {got_cwd!r}, expected {cwd!r}")
            policy = reviewer_policy(res) if reviewer else None
        except Exception as e:
            err = _refuse(rpc, e, started_ms)
            if reviewer and isinstance(err, WrongSession):  # the redacted shape, if any
                err.details["diagnostic"] = provenance_diagnostic(res, "launch:reviewer")
            raise err from None
        conn = CodexConnection(rpc, thread["id"], "launch", thread.get("status") or {"type": "idle"})
        conn.cwd = res.get("cwd") or thread.get("cwd")
        conn.confirmed_identity = {"client_session_id": thread["id"], "cwd": conn.cwd}
        if policy is not None:
            conn.policy = policy  # the verified reviewer policy, kept for inspection
            conn.diagnostic = provenance_diagnostic(res, "launch:reviewer")  # evidence, redacted
        return conn
