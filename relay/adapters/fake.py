"""Adapter for the fake client (relay/adapters/fake_client.py).

It exercises every adapter capability, so the controller, tests and walkthrough
can run with no real client. Passing these tests does NOT show that a real
client can be attached or keeps its context.
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import time
import uuid

from ..storage import InvalidInput, NotFound, Unsupported, pid_alive
from .base import (CAPABILITIES, Adapter, Connection, DeliveryUncertain, LifecycleEvent,
                   NotSubmitted, SessionInfo, WrongSession)

RAW_TO_EVENT = {"accepted": "accepted", "turn_started": "turn_started", "delta": "delta",
                "tool": "tool", "approval_pending": "approval_pending",
                "approval_resolved": "approval_resolved", "question": "human_question",
                "turn_completed": "turn_completed", "error": "error",
                "user_message": "echo"}


def _home(options: dict) -> str:
    home = options.get("home") or os.environ.get("AGENT_RELAY_FAKE_HOME")
    if not home:
        raise InvalidInput("fake adapter needs a home directory (--fake-home or AGENT_RELAY_FAKE_HOME)")
    return os.path.realpath(home)


def live_writers(home: str, sid: str) -> list[dict]:
    """Processes currently writing this fake conversation: pid, start time, native/relay.

    A marker counts only while its pid is alive with the same start time, so a stale
    marker from a killed process (or a reused pid) is never mistaken for a writer."""
    from ..context import process_start_ms, valid_start
    out = []
    d = os.path.join(home, "sessions", sid, "writers")
    for name in sorted(os.listdir(d)) if os.path.isdir(d) else []:
        try:
            with open(os.path.join(d, name)) as f:
                w = json.load(f)
        except (OSError, ValueError):
            continue
        if not pid_alive(w.get("pid", 0)):
            continue
        now = process_start_ms(w["pid"])
        if valid_start(now) and valid_start(w.get("started_ms")) and abs(now - w["started_ms"]) < 2000:
            out.append(w)
    return out


def read_info(home: str, sid: str) -> dict | None:
    try:
        with open(os.path.join(home, "sessions", sid, "info.json")) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return None


class FakeConnection(Connection):
    def __init__(self, home: str, sid: str, mode: str, proc: subprocess.Popen | None = None,
                 from_start: bool = False):
        self.home, self.client_session_id, self.mode, self.proc = home, sid, mode, proc
        self.connection_id = "c_" + secrets.token_hex(8)
        self.dir = os.path.join(home, "sessions", sid)
        self.events_path = os.path.join(self.dir, "events.jsonl")
        # Attach starts at the current end: old turns are never replayed.
        self.offset = 0 if from_start else self._size()
        self.pending: list[LifecycleEvent] = []
        self.detached = False
        self._disconnected_reported = False
        info = read_info(home, sid)
        if not info or info.get("session_id") != sid:
            raise WrongSession(f"fake session {sid} not found or mismatched")
        self.pid = info["pid"]
        # The fake client reports its own identity and workspace in info.json.
        self.confirmed_identity = {"client_session_id": info["session_id"], "cwd": info.get("cwd")}

    def _size(self) -> int:
        try:
            return os.path.getsize(self.events_path)
        except FileNotFoundError:
            return 0

    def _read_new(self) -> list[LifecycleEvent]:
        out = []
        try:
            with open(self.events_path, "rb") as f:
                f.seek(self.offset)
                data = f.read()
        except FileNotFoundError:
            return out
        end = data.rfind(b"\n")
        if end < 0:
            return out
        self.offset += end + 1
        for line in data[:end + 1].splitlines():
            try:
                raw = json.loads(line)
            except ValueError:
                out.append(LifecycleEvent("error", text="malformed client event"))
                continue
            etype = RAW_TO_EVENT.get(raw.get("type"), "unknown")
            out.append(LifecycleEvent(etype, raw.get("request_id"), raw.get("client_turn_id"),
                                      raw.get("text"), {k: v for k, v in raw.items()
                                                        if k not in ("type", "text")}))
        return out

    def alive(self) -> bool:
        if self.proc is not None:
            return self.proc.poll() is None
        return pid_alive(self.pid)

    def status(self) -> str:
        if not self.alive():
            return "disconnected"
        info = read_info(self.home, self.client_session_id) or {}
        return info.get("status", "unknown")

    def submit(self, text: str, request_id: str, *, accept_timeout: float = 10.0) -> dict:
        if self.detached:
            raise NotSubmitted("connection detached")
        st = self.status()
        if st == "disconnected":
            raise NotSubmitted("client session is not running")
        if st in ("busy", "waiting"):
            raise NotSubmitted(f"client session is {st}")
        tmp = os.path.join(self.dir, f".{request_id}.tmp")
        with open(tmp, "w") as f:
            json.dump({"request_id": request_id, "text": text}, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, os.path.join(self.dir, "inbox", f"{request_id}.json"))
        deadline = time.monotonic() + accept_timeout
        while time.monotonic() < deadline:
            batch = self._read_new()
            self.pending.extend(batch)  # keep everything for poll(); nothing is dropped
            for ev in batch:
                if ev.type == "accepted" and ev.request_id == request_id:
                    return {"client_turn_id": ev.client_turn_id,
                            "duplicate": bool(ev.data.get("duplicate"))}
            if not self.alive():
                break
            time.sleep(0.02)
        raise DeliveryUncertain("request written but the client did not confirm acceptance")

    def poll(self, timeout: float) -> list[LifecycleEvent]:
        deadline = time.monotonic() + timeout
        while True:
            evs = self.pending + self._read_new()
            self.pending = []
            if evs:
                return evs
            if not self.alive() and not self._disconnected_reported:
                self._disconnected_reported = True
                return [LifecycleEvent("disconnected", text="client process exited")]
            left = deadline - time.monotonic()
            if left <= 0:
                return []
            time.sleep(min(0.02, left))

    def lookup(self, request_id: str) -> dict | None:
        res: dict = {}
        try:
            with open(self.events_path) as f:
                for line in f:
                    try:
                        raw = json.loads(line)
                    except ValueError:
                        continue
                    if raw.get("request_id") != request_id:
                        continue
                    if raw["type"] == "accepted":
                        res["client_turn_id"] = raw.get("client_turn_id")
                    elif raw["type"] == "turn_completed":
                        res["completed_text"] = raw.get("text")
        except FileNotFoundError:
            pass
        if not res and os.path.exists(os.path.join(self.dir, "inbox", f"{request_id}.json")):
            res["pending_in_client_inbox"] = True
        return res or None

    def interrupt(self) -> None:
        raise Unsupported("fake client has no interrupt")

    def detach(self) -> None:
        self.detached = True

    def close(self) -> None:
        if self.mode != "launch" or self.proc is None:
            raise Unsupported("refusing to close a session the relay did not launch")
        with open(os.path.join(self.dir, f"stop-{self.proc.pid}"), "w"):
            pass  # only this process stops; another writer of the conversation is untouched
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()


class FakeAdapter(Adapter):
    name = "fake"
    verified = True  # the fake contract is fully specified by this repository

    def namespace(self) -> str:
        return "fake:" + _home(self.options)

    def capabilities(self) -> dict[str, bool]:
        caps = {c: True for c in CAPABILITIES}
        caps["interrupt"] = False
        return caps

    def discover(self) -> list[SessionInfo]:
        home = _home(self.options)
        out = []
        base = os.path.join(home, "sessions")
        for sid in sorted(os.listdir(base)) if os.path.isdir(base) else []:
            info = read_info(home, sid)
            if not info:
                continue
            alive = pid_alive(info["pid"]) and info.get("status") != "exited"
            out.append(SessionInfo(sid, info.get("cwd"), info.get("status", "unknown") if alive
                                   else "exited", alive, [] if alive else ["process not running"],
                                   {"pid": info["pid"], "started_ms": info.get("started_ms"), "turns": info.get("turns"),
                                    "writers": live_writers(home, sid), "interactive": bool(info.get("interactive")),
                                    "client": info.get("client"), "name": info.get("name"),
                                    "summary": info.get("summary"), "title": info.get("title"),
                                    "title_source": "client metadata (demo)" if info.get("title") else None,
                                    "summary_source": "client metadata (demo)" if info.get("summary") else None}))
        return out

    def attach(self, client_session_id: str) -> Connection:
        home = _home(self.options)
        info = read_info(home, client_session_id)
        if not info:
            raise NotFound(f"no fake session {client_session_id}")
        if not pid_alive(info["pid"]) or info.get("status") == "exited":
            raise NotSubmitted(f"fake session {client_session_id} is not running")
        if info.get("interactive"):
            raise Unsupported("this pretend terminal can't be driven while it's open; hand it off instead")
        return FakeConnection(home, client_session_id, "attach")

    def resume(self, cwd: str, conversation: str, options: dict | None = None, *,
               on_spawn=None) -> Connection:
        home = _home(self.options)
        if not os.path.exists(os.path.join(home, "sessions", conversation, "context.json")):
            raise NotFound(f"no existing fake conversation {conversation} to resume")
        return self.launch(cwd, dict(options or {}, session_id=conversation), on_spawn=on_spawn)

    def launch(self, cwd: str, options: dict | None = None, *, on_spawn=None) -> Connection:
        home = _home(self.options)
        opts = dict(self.options.get("launch", {}), **(options or {}))
        sid = opts.get("session_id") or str(uuid.uuid4())
        # Resuming an existing conversation (hand-off): read only what happens from now on,
        # never replay its earlier turns as if they were new.
        resuming = os.path.exists(os.path.join(home, "sessions", sid, "context.json"))
        if resuming:  # the same pretend model continues the conversation
            prev = read_info(home, sid) or {}
            for k in ("default", "script", "client", "name", "title", "summary"):
                if not opts.get(k) and prev.get(k):
                    opts[k] = prev[k]
        os.makedirs(os.path.join(home, "sessions", sid), exist_ok=True)
        log = open(os.path.join(home, "sessions", sid, "client.log"), "ab")
        proc = subprocess.Popen(
            [sys.executable, "-m", "relay.adapters.fake_client", "--home", home, "--session-id", sid,
             "--cwd", cwd, "--script", json.dumps(opts.get("script", [])),
             "--default", json.dumps(opts.get("default", {})), "--watch-stdin",
             "--client", str(opts.get("client") or ""), "--name", str(opts.get("name") or ""),
             "--title", str(opts.get("title") or ""), "--summary", str(opts.get("summary") or ""),
             "--writer", "relay"],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=log,
            cwd=os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
        log.close()
        if on_spawn is not None:
            from ..context import process_start_ms
            on_spawn({"pid": proc.pid, "started_ms": process_start_ms(proc.pid)})
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            info = read_info(home, sid)
            if info and info.get("pid") == proc.pid:
                return FakeConnection(home, sid, "launch", proc, from_start=not resuming)
            if proc.poll() is not None:
                break
            time.sleep(0.02)
        proc.kill()
        raise NotSubmitted("fake client failed to start")
