"""Test helpers: temporary roots, fake clients and CLI subprocesses.

Every test writes only inside its own temporary directory.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)

from relay.bus import Bus  # noqa: E402
from relay.controller import Controller  # noqa: E402
from relay.storage import Root, init_root  # noqa: E402

os.environ.setdefault("AGENT_RELAY_DURABILITY", "fsync")


def env_for(tmp: str) -> dict:
    env = dict(os.environ)
    env.update(AGENT_RELAY_ROOT=os.path.join(tmp, "root"), AGENT_RELAY_FAKE_HOME=os.path.join(tmp, "home"),
               PYTHONPATH=REPO, AGENT_RELAY_DURABILITY="fsync")
    env.pop("AGENT_RELAY_ALLOW_UNVERIFIED", None)
    return env


def relay(tmp: str, *args: str, input: str | None = None, timeout: float = 60) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "relay", *args], cwd=REPO, env=env_for(tmp),
                          input=input, capture_output=True, text=True, timeout=timeout)


def relay_json(tmp: str, *args: str, input: str | None = None, ok: bool = True):
    r = relay(tmp, *args, "--json", input=input)
    if ok and r.returncode != 0:
        raise AssertionError(f"relay {args} failed ({r.returncode}): {r.stdout} {r.stderr}")
    return r, (json.loads(r.stdout) if r.stdout.strip() else None)


class FakeSession:
    """A pre-existing, user-owned fake client process (for attach mode)."""

    def __init__(self, tmp: str, sid: str, *, script=None, default=None, seed_history: int = 0,
                 name: str = "", client: str = "", summary: str = "", title: str = "", cwd: str = "",
                 interactive: bool = False):
        self.home = os.path.join(tmp, "home")
        self.sid = sid
        self.dir = os.path.join(self.home, "sessions", sid)
        os.makedirs(self.home, exist_ok=True)
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "relay.adapters.fake_client", "--home", self.home,
             "--session-id", sid, "--cwd", cwd or tmp, "--script", json.dumps(script or []),
             "--default", json.dumps(default or {}), "--seed-history", str(seed_history),
             "--name", name, "--client", client, "--summary", summary, "--title", title]
            + (["--interactive"] if interactive else []),
            cwd=REPO, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # Reap it when it exits, as a real session's own terminal would, so a crashed
        # fake session is really gone (not a zombie that still looks alive).
        threading.Thread(target=self.proc.wait, daemon=True).start()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                with open(os.path.join(self.dir, "info.json")) as f:
                    if json.load(f)["pid"] == self.proc.pid:
                        return
            except (FileNotFoundError, ValueError):
                pass
            time.sleep(0.02)
        raise RuntimeError("fake session did not start")

    def context(self) -> dict:
        with open(os.path.join(self.dir, "context.json")) as f:
            return json.load(f)

    def manual_turn(self, text: str = "user typed something") -> None:
        with open(os.path.join(self.dir, "manual", f"{time.time_ns()}.txt"), "w") as f:
            f.write(text)

    def alive(self) -> bool:
        return self.proc.poll() is None

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            self.proc.wait(timeout=5)


class RelayTest(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="relay-test-"))
        self.root = init_root(os.path.join(self.tmp, "root"), ["codex", "claude"])
        self.bus = Bus(self.root)
        self.fakes: list[FakeSession] = []
        self.controllers: list[Controller] = []
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home, exist_ok=True)
        # Never the user's real notification settings, in-process or in children.
        os.environ["AGENT_RELAY_NOTIFY_CONFIG"] = os.path.join(self.tmp, "notify.json")

    def tearDown(self):
        for c in self.controllers:
            try:
                c.shutdown()
            except Exception:
                pass
        for f in self.fakes:
            f.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------------ helpers
    def fake(self, sid: str, **kw) -> FakeSession:
        f = FakeSession(self.tmp, sid, **kw)
        self.fakes.append(f)
        return f

    def attach_endpoint(self, sid: str, participant: str) -> str:
        rec, _ = self.bus.register_session(adapter="fake", client_session_id=sid, mode="attach",
                                           participant=participant, cwd=self.tmp,
                                           options={"home": self.home})
        return rec["id"]

    def launch_endpoint(self, participant: str, *, script=None, default=None, sid=None) -> str:
        import uuid
        rec, _ = self.bus.register_session(
            adapter="fake", client_session_id=sid or str(uuid.uuid4()), mode="launch",
            participant=participant, cwd=self.tmp,
            options={"home": self.home, "launch": {"script": script or [], "default": default or {}}})
        return rec["id"]

    def pair(self, leader: str, follower: str, **kw) -> str:
        return self.bus.create_pair(leader=leader, follower=follower, scope="tests only", **kw)["id"]

    def controller(self, pair_id: str, connect: bool = True, takeover: bool = False,
                   force: bool = False) -> Controller:
        c = Controller(self.bus, pair_id, poll_interval=0.05)
        c.acquire(takeover=takeover, force=force)
        if connect:
            c.connect_all()
        self.controllers.append(c)
        return c

    def state(self, pair_id: str) -> dict:
        return self.bus.pair_state(pair_id)

    def events(self, stream: str) -> list:
        return self.root.read_stream(stream)

    def wait_until(self, pred, timeout=15.0, interval=0.05):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if pred():
                return True
            time.sleep(interval)
        raise AssertionError("condition not met in time")
