"""Claude Code adapters (see docs/INTEGRATION-FEASIBILITY.md).

* `claude-agents`: read-only discovery through `claude agents --json`. Claude Code
  2.1.281 documents no receipted way for another process to submit a turn to a
  running interactive or background session, so live attach is unsupported.
  The relay does not substitute `--resume` (it starts a second writer on the
  same transcript) or fork.
* `claude-stream` (UNVERIFIED): launches `claude -p` with stream-json input and
  output and owns that process. Not yet run live; requires explicit authorization
  and AGENT_RELAY_ALLOW_UNVERIFIED=1.
"""

from __future__ import annotations

import json
import os
import queue
import secrets
import shutil
import subprocess
import threading
import time

from ..storage import RelayError, Unsupported
from .base import (CAPABILITIES, Adapter, Connection, DeliveryUncertain, LifecycleEvent,
                   NotSubmitted, SessionInfo)


def latest_ai_title(path: str, tail: int = 262144) -> str | None:
    """Claude Code's own conversation title (`ai-title` record), read from the end of
    the transcript. Only that record is parsed; message content is never read out."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - tail))
            chunk = f.read()
    except OSError:
        return None
    for line in reversed(chunk.splitlines()):
        if b'"ai-title"' not in line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("type") == "ai-title" and isinstance(rec.get("aiTitle"), str):
            return rec["aiTitle"][:200]
    return None


def transcript_path(cwd: str, session_id: str) -> str:
    """Where Claude Code keeps a session's transcript (observed layout, 2.1.x)."""
    import re
    # Observed on 2.1.x: every character other than a letter, digit or '-' becomes '-'
    # (e.g. "/private/var/folders/qq/yd27pvf96_n1c..." -> "-private-var-folders-qq-yd27pvf96-n1c...").
    proj = re.sub(r"[^A-Za-z0-9-]", "-", cwd)
    return os.path.join(os.path.expanduser("~/.claude/projects"), proj, f"{session_id}.jsonl")


def _claude() -> str:
    exe = shutil.which("claude")
    if not exe:
        raise Unsupported("claude CLI not found on PATH")
    return exe


class ClaudeAgentsAdapter(Adapter):
    name = "claude-agents"
    verified = True  # discovery only; verified against Claude Code 2.1.281

    def capabilities(self) -> dict[str, bool]:
        caps = {c: False for c in CAPABILITIES}
        caps.update(discover=True, busy_state=True)
        return caps

    def writers_for(self, conversation: str) -> list[dict] | None:
        """Native Claude sessions writing `conversation` (from `claude agents`), or None if
        the listing isn't available. A successful listing without it means no native writer."""
        try:
            rows = self._rows()
        except RelayError:
            return None
        return [{"pid": r.get("pid"), "started_ms": r.get("startedAt"), "writer": "native",
                 "kind": r.get("kind")}
                for r in rows if isinstance(r, dict) and str(r.get("sessionId")) == conversation]

    def _rows(self) -> list:
        if self.options.get("_agents") is not None:  # injected by tests
            return self.options["_agents"]
        try:
            res = subprocess.run([_claude(), "agents", "--json"], capture_output=True, text=True,
                                 timeout=20, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            raise RelayError("`claude agents --json` timed out")
        if res.returncode != 0:
            raise RelayError(f"`claude agents --json` failed with exit code {res.returncode}")
        try:
            rows = json.loads(res.stdout)
        except ValueError:
            raise RelayError("`claude agents --json` returned invalid JSON")
        return rows if isinstance(rows, list) else []

    def discover(self) -> list[SessionInfo]:
        rows = self._rows()
        out = []
        for r in rows if isinstance(rows, list) else []:
            if not isinstance(r, dict) or "sessionId" not in r:
                continue
            lim = ["no documented receipted prompt-submission interface for a running session",
                   "--resume would start a second process on the same transcript (not attach)"]
            title = None
            if r.get("cwd") and self.options.get("read_titles", True):
                title = latest_ai_title(transcript_path(r["cwd"], str(r["sessionId"])))
            out.append(SessionInfo(str(r["sessionId"]), r.get("cwd"), str(r.get("status", "unknown")),
                                   False, lim, {"pid": r.get("pid"), "kind": r.get("kind"),
                                                "client": "Claude", "auto_name": r.get("name"),
                                                "interactive": True,  # an open terminal: hand-off only
                                                "started_ms": r.get("startedAt"),
                                                "title": title,
                                                "title_source": "Claude's conversation title" if title else None}))
        return out


class StreamConnection(Connection):
    """One console-launched `claude -p` stream-json child.

    Identity is requested with `--session-id` but counts as confirmed only when the
    client itself reports it (`system/init`, or a later session-bearing message).
    Any report of a different session marks the connection failed: its results are
    turned into errors, never routed, and no further prompt is submitted.
    """

    def __init__(self, proc: subprocess.Popen, requested_session_id: str | None,
                 *, start_reader: bool = True, expected_cwd: str | None = None,
                 policy: dict | None = None):
        self.proc, self.mode = proc, "launch"
        # Under the coding policy, Claude's own init record must come first and
        # match the fixed tools, permission mode and (no) MCP servers; every tool event is
        # audited, and any tool outside the policy stops and closes the writer.
        self.policy = policy
        self.policy_ok = policy is None
        self.policy_error: str | None = None
        self.audit: list[dict] = []
        self.tmp: str | None = None
        self.stderr_diag = None  # (diagnostic probes) an opt-in, category-only stderr drain
        # Evidence for probes: the init record's shape and the order of message
        # types. Never message text, tool inputs or results.
        self.init_summary: dict | None = None
        self.trace: list[str] = []
        # Resume (hand-off): the folder the conversation must belong to. Identity is then
        # confirmed only when the client reports BOTH this session id and this folder.
        self.expected_cwd = os.path.realpath(expected_cwd) if expected_cwd else None
        self.requested = requested_session_id
        self.client_session_id = requested_session_id or f"pending-{secrets.token_hex(8)}"
        self.connection_id = "c_" + secrets.token_hex(8)
        self.q: queue.Queue = queue.Queue()
        self.backlog: list[LifecycleEvent] = []
        self.current: str | None = None
        self.pending_uuid: str | None = None
        self.identity_error: str | None = None
        self.eof = False
        self.cwd: str | None = None
        if start_reader:
            threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self) -> None:
        for line in self.proc.stdout:
            try:
                self.q.put(json.loads(line))
            except ValueError:
                continue
        self.q.put({"type": "__eof__"})

    # ------------------------------------------------------------ identity
    def _check_session(self, sid, where: str) -> bool:
        """Validate a session id reported by the client; True if consistent."""
        if sid is None:
            return self.identity_error is None
        expected = self.requested or (self.confirmed_identity or {}).get("client_session_id")
        if expected and sid != expected:
            if not self.identity_error:
                self.identity_error = f"{where} reported session {sid}, expected {expected}"
            return False
        if self.identity_error:
            return False
        if self.confirmed_identity is None:
            self.confirmed_identity = {"client_session_id": sid, "cwd": self.cwd}
            self.client_session_id = sid
        return True

    # ------------------------------------------------------------- policy
    def _violation(self, why: str) -> list[LifecycleEvent]:
        """Stop and close the writer: nothing it produced is forwarded or accepted. Edits it
        may already have made stayed inside the OS sandbox's bounds, but are untrusted."""
        if not self.policy_error:
            self.policy_error = why
            self.identity_error = self.identity_error or f"coding policy check failed: {why}"
            try:
                self.proc.terminate()
            except OSError:
                pass
        return [LifecycleEvent("error", self.current, None, f"coding policy check failed: {why}; "
                               "any edits this turn stayed within the sandbox but are untrusted; "
                               "nothing routed", {"identity_mismatch": True, "policy_mismatch": True,
                                                  "tool_audit": list(self.audit)})]

    def _check_init(self, msg: dict) -> str | None:
        want_tools = sorted(self.policy["tools"])
        tools = msg.get("tools")
        if not isinstance(tools, list) or sorted(x for x in tools if isinstance(x, str)) != want_tools \
                or len(tools) != len(want_tools):
            return f"the client reported tools {tools!r}, not exactly {want_tools}"
        if msg.get("permissionMode") != self.policy["permission_mode"]:
            return f"the client reported permission mode {msg.get('permissionMode')!r}"
        if msg.get("mcp_servers") != []:
            return "the client reported MCP servers" if msg.get("mcp_servers") else \
                "the client didn't report its MCP servers"
        return None

    def _audit_tools(self, msg: dict) -> str | None:
        from ..toolpolicy import AUDIT_MAX, audit_entry
        content = (msg.get("message") or {}).get("content")
        for block in content if isinstance(content, list) else []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                entry = audit_entry(block, self.expected_cwd or self.cwd)
                if len(self.audit) < AUDIT_MAX:
                    self.audit.append(entry)
                if entry["tool"] not in self.policy["tools"]:
                    return f"unexpected tool {entry['tool']!r}"
        return None

    # --------------------------------------------------------- translation
    def _handle(self, msg: dict) -> list[LifecycleEvent]:
        t, rid = msg.get("type"), self.current
        if len(self.trace) < 200:
            self.trace.append(t if t != "system" else f"system/{msg.get('subtype')}")
        if t == "__eof__":
            self.eof = True
            return [LifecycleEvent("disconnected", text="claude process exited")]
        if self.policy_error:
            return []  # already stopped: nothing more is translated or forwarded
        is_init = t == "system" and msg.get("subtype") == "init"
        if is_init and self.init_summary is None:
            mcp = msg.get("mcp_servers")
            self.init_summary = {
                "keys": sorted(k for k in msg if isinstance(k, str))[:60],
                "tools": [x for x in msg.get("tools") or [] if isinstance(x, str)][:60],
                "permissionMode": msg.get("permissionMode") if isinstance(msg.get("permissionMode"), str) else None,
                "mcp_servers": len(mcp) if isinstance(mcp, list) else None,
                "session_matches": msg.get("session_id") == self.requested,
                "cwd_matches": bool(self.expected_cwd) and os.path.realpath(msg.get("cwd") or "") == self.expected_cwd}
        if self.policy is not None and not self.policy_ok and not is_init:
            return self._violation(f"a {t or 'untyped'} message came before the client's init record")
        if is_init:
            self.cwd = msg.get("cwd") or self.cwd
            if self.expected_cwd and os.path.realpath(self.cwd or "") != self.expected_cwd:
                if not self.identity_error:
                    self.identity_error = f"system/init reported folder {self.cwd!r}, expected {self.expected_cwd!r}"
                return [LifecycleEvent("error", rid, None, f"identity mismatch: {self.identity_error}",
                                       {"identity_mismatch": True})]
            if not self._check_session(msg.get("session_id"), "system/init"):
                return [LifecycleEvent("error", rid, None, f"identity mismatch: {self.identity_error}",
                                       {"identity_mismatch": True})]
            if self.confirmed_identity is not None and self.cwd:
                self.confirmed_identity["cwd"] = self.cwd
            if self.policy is not None and not self.policy_ok:
                why = self._check_init(msg)
                if why:
                    return self._violation(why)
                self.policy_ok = True
            return []
        if t == "user":
            if self.pending_uuid and msg.get("uuid") == self.pending_uuid:
                self.pending_uuid = None
                if not self._check_session(msg.get("session_id"), "replayed user message"):
                    return [LifecycleEvent("error", rid, None,
                                           f"identity mismatch: {self.identity_error}",
                                           {"identity_mismatch": True})]
                return [LifecycleEvent("accepted", rid, msg.get("uuid"))]
            return []  # tool results and other user-role echoes are not handoffs
        if t == "assistant":
            if not self._check_session(msg.get("session_id"), "assistant message"):
                return []
            if self.policy is not None:
                why = self._audit_tools(msg)
                if why:
                    return self._violation(why)
            return [LifecycleEvent("delta", rid, None, "assistant message")]
        if t == "result":
            self.current = None
            if not self._check_session(msg.get("session_id"), "result"):
                return [LifecycleEvent("error", rid, None, f"identity mismatch: {self.identity_error}; "
                                       "result not routed", {"identity_mismatch": True})]
            if self.confirmed_identity is None:
                self.identity_error = "client never reported its session id"
                return [LifecycleEvent("error", rid, None, "client never reported its session id; "
                                       "result not routed", {"identity_mismatch": True})]
            if self.expected_cwd and os.path.realpath(self.cwd or "") != self.expected_cwd:
                self.identity_error = self.identity_error or "client never reported its folder"
                return [LifecycleEvent("error", rid, None, f"{self.identity_error}; result not routed",
                                       {"identity_mismatch": True})]
            if msg.get("is_error"):
                return [LifecycleEvent("error", rid, None, str(msg.get("subtype")))]
            out = []
            denials = msg.get("permission_denials") or []
            data = {}
            if self.policy is not None:
                from ..toolpolicy import AUDIT_MAX, audit_entry
                for d in denials if isinstance(denials, list) else []:
                    if isinstance(d, dict) and len(self.audit) < AUDIT_MAX:
                        self.audit.append(audit_entry({"name": d.get("tool_name"), "input": d.get("tool_input")},
                                                      self.expected_cwd or self.cwd, kind="denied"))
                data["tool_audit"], self.audit = self.audit, []
                # Under the coding policy a denial is the policy working, not a question for
                # the human's own interface: it's audited, and the turn completes normally.
            elif denials:
                out.append(LifecycleEvent("approval_pending", rid, None,
                                          f"{len(denials)} permission request(s) denied"))
            out.append(LifecycleEvent("turn_completed", rid, None, msg.get("result") or "", data))
            return out
        return []

    @property
    def identity_pending(self) -> bool:
        """Provisional writer: identity not yet reported (it is, with the first prompt)."""
        return self.confirmed_identity is None and self.identity_error is None

    # ------------------------------------------------------------------ api
    def submit(self, text: str, request_id: str, *, accept_timeout: float = 30.0) -> dict:
        if self.identity_error:
            raise NotSubmitted(f"session identity failed: {self.identity_error}")
        if self.current or self.eof:
            raise NotSubmitted("a turn is already running" if self.current else "process exited")
        msg_uuid = "u-" + secrets.token_hex(12)
        line = {"type": "user", "uuid": msg_uuid,
                "message": {"role": "user", "content": [{"type": "text", "text": text}]}}
        self.current, self.pending_uuid = request_id, msg_uuid
        try:
            self.proc.stdin.write((json.dumps(line) + "\n").encode())
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            self.current = self.pending_uuid = None
            raise NotSubmitted(f"could not write to claude: {e}")
        deadline = time.monotonic() + accept_timeout
        while time.monotonic() < deadline:
            try:
                msg = self.q.get(timeout=0.05)
            except queue.Empty:
                continue
            evs = self._handle(msg)
            self.backlog.extend(evs)  # keep order; poll() returns these first
            if any(e.type == "accepted" for e in evs):
                return {"client_turn_id": msg_uuid}
            if any(e.type in ("disconnected", "error") for e in evs):
                break
        raise DeliveryUncertain("claude did not replay the prompt as accepted"
                                + (f" ({self.identity_error})" if self.identity_error else ""))

    def poll(self, timeout: float) -> list[LifecycleEvent]:
        out, self.backlog = self.backlog, []
        deadline = time.monotonic() + timeout
        while True:
            try:
                msg = self.q.get(timeout=max(0.0, min(0.05, deadline - time.monotonic())))
            except queue.Empty:
                if out or time.monotonic() >= deadline:
                    return out
                continue
            out.extend(self._handle(msg))

    def status(self) -> str:
        if self.eof or self.proc.poll() is not None:
            return "disconnected"
        if self.identity_error:
            return "unknown"  # fail closed: never dispatch to a mismatched session
        return "busy" if self.current else "idle"

    def interrupt(self) -> None:
        raise Unsupported("interrupt over stream-json is not implemented")

    def close(self) -> None:
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.terminate()
        if self.tmp:
            from ..toolpolicy import cleanup_tmp
            if self.proc.poll() is not None:
                cleanup_tmp(self.tmp)


def resume_argv(claude: str, conversation: str, *, no_tools: bool = True) -> list[str]:
    """`claude -p` continuing an EXISTING conversation (hand-off).

    `--resume ID` only: never `--session-id` (that names a NEW conversation) and never
    `--fork-session` (a copy). Restricted policy, stated explicitly rather than inherited:
    `--restricted` drops the code-running tools and ignores user/project/local settings
    (so no broader approval rules carry over) and confines file tools to the folder;
    `--strict-mcp-config` loads no MCP servers; `--permission-prompts none` denies and
    reports anything that would ask; `--tools ""` (default) removes all tools."""
    argv = [claude, "-p", "--resume", conversation, "--input-format", "stream-json",
            "--output-format", "stream-json", "--verbose", "--replay-user-messages",
            "--permission-prompts", "none", "--restricted", "--strict-mcp-config"]
    if no_tools:
        argv += ["--tools", ""]
    return argv


class ClaudeStreamAdapter(Adapter):
    name = "claude-stream"
    verified = False
    supports_writer_policy = True  # coding route (toolpolicy)

    def resume(self, cwd: str, conversation: str, options: dict | None = None, *,
               on_spawn=None, hold: dict | None = None) -> Connection:
        """Continue an existing Claude conversation as its only writer.

        The requested id and folder are checked against Claude's own records first
        (read-only). The writer is PROVISIONAL until the client reports the same id and
        folder, which Claude does only once the first prompt runs: that first prompt is a
        normal, counted relay turn, and nothing it produces is routed before then."""
        from ..context import process_start_ms, verify_identity
        opts = dict(options or {})
        cwd = os.path.realpath(cwd)
        verify_identity("claude", conversation, cwd)  # raises InvalidInput: nothing is started
        if opts.get("writer_policy") is not None:  # the separately reviewed coding route
            return self._coding(cwd, opts["writer_policy"], resume=conversation, on_spawn=on_spawn, hold=hold)
        argv = resume_argv(_claude(), conversation, no_tools=opts.get("no_tools", True))
        proc = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, bufsize=0)
        if on_spawn is not None:
            on_spawn({"pid": proc.pid, "started_ms": process_start_ms(proc.pid)})
        conn = StreamConnection(proc, conversation, expected_cwd=cwd)
        conn.pid, conn.argv = proc.pid, argv
        return conn

    def capabilities(self) -> dict[str, bool]:
        caps = {c: False for c in CAPABILITIES}
        caps.update(launch=True, submit=True, correlate=True, turn_complete=True,
                    approvals_visible=True, busy_state=True)
        return caps

    # ------------------------------------------------- coding policy (D5)
    def coding_sandbox(self):
        from ..toolpolicy import ImplementerSandbox, StateAllowlist
        sb = self.options.get("coding_sandbox")  # (tests) an injected sandbox
        if sb is None:
            version = os.path.basename(os.path.realpath(_claude()))
            sb = ImplementerSandbox(StateAllowlist.pinned(version))
        return sb

    def _coding(self, cwd: str, record: dict, *, resume: str | None = None, session_id: str | None = None,
                on_spawn=None, hold: dict | None = None, stderr_sink=None) -> Connection:
        """The coding route: the fixed argv under the proven OS sandbox. The team's policy record
        must be exactly today's: a policy can't change under a team (P1).

        `hold`: the caller's dict, filled by the sandbox with the temp
        folder and child handle from the moment each exists. Until the connection is returned,
        any failure or interruption here stops that child and removes its folder."""
        from ..context import process_start_ms
        from ..toolpolicy import coding_argv, policy_record, release_hold
        sb = self.coding_sandbox()
        why = sb.available()
        if why:
            raise Unsupported(why)
        if not isinstance(record, dict) or record.get("sha256") != policy_record(sb.state)["sha256"]:
            raise Unsupported("this team's coding policy isn't the one in force now; nothing was started")
        argv = coding_argv(_claude(), resume=resume, session_id=session_id)
        hold = {} if hold is None else hold
        proc, evidence = sb.launch(argv, cwd, resume or session_id, claude=_claude(),
                                   home=self.options.get("home_for_state"),
                                   runtime_expected=record.get("runtime_read"), hold=hold,
                                   **({"stderr_sink": stderr_sink} if stderr_sink is not None else {}))
        try:
            if on_spawn is not None:
                on_spawn(dict({"pid": proc.pid, "started_ms": process_start_ms(proc.pid)},
                              **{k: v for k, v in evidence.items() if k != "tmp"}))
            conn = StreamConnection(proc, resume or session_id, expected_cwd=cwd,
                                    policy={"tools": record["tools"], "permission_mode": record["permission_mode"]})
            conn.pid, conn.argv, conn.tmp, conn.policy_evidence = proc.pid, argv, evidence["tmp"], evidence
            conn.stderr_diag = stderr_sink
        except BaseException:
            release_hold(hold)
            raise
        return conn

    def launch(self, cwd: str, options: dict | None = None, *, on_spawn=None,
               hold: dict | None = None) -> Connection:
        opts = dict(self.options.get("launch", {}), **(options or {}))
        if opts.get("writer_policy") is not None:  # a fresh relay-launched implementer (D5)
            return self._coding(os.path.realpath(cwd), opts["writer_policy"],
                                session_id=opts.get("session_id") or str(__import__("uuid").uuid4()),
                                on_spawn=on_spawn, hold=hold, stderr_sink=opts.get("stderr_sink"))
        sid = opts.get("session_id")
        argv = [_claude(), "-p", "--input-format", "stream-json", "--output-format", "stream-json",
                "--verbose", "--replay-user-messages",
                # No human can answer a prompt in a headless child, and the relay never
                # answers one: anything that would prompt is denied and reported.
                "--permission-prompts", "none"]
        if sid:
            argv += ["--session-id", sid]
        if opts.get("permission_mode"):
            argv += ["--permission-mode", opts["permission_mode"]]
        proc = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, bufsize=0)
        return StreamConnection(proc, sid)
