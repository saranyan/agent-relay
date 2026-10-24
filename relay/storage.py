"""Relay root, safe paths, atomic exclusive publication and append-only streams.

Nothing in this module interprets message content. See docs/PROTOCOL.md sections 2-3.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import socket
import stat
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable

SCHEMA_VERSION = 1
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SUBDIRS = ("messages", "idempotency", "identities", "sessions", "pairs", "streams", "evidence",
           "views", "tmp", "locks")
MAX_RECORD_BYTES = 256 * 1024
APPEND_RETRIES = 64


class RelayError(Exception):
    """Base error. `code` is the CLI exit code."""

    code = 1

    def __init__(self, message: str, **details: Any):
        super().__init__(message)
        self.details = details


class UsageError(RelayError):
    code = 2


class NotFound(RelayError):
    code = 3


class Conflict(RelayError):
    code = 4


class Blocked(RelayError):
    """Refused because of control state (pause, stop, budget)."""

    code = 5


class InvalidInput(RelayError):
    code = 6


class TimedOut(RelayError):
    code = 7


class Untrusted(RelayError):
    """History that cannot be trusted; affected objects fail closed."""

    code = 8


class Unsupported(RelayError):
    """An adapter cannot perform the requested operation."""

    code = 9


def now_iso() -> str:
    t = time.time()
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + f".{int(t * 1000) % 1000:03d}Z"


def new_id(prefix: str) -> str:
    return f"{prefix}_{int(time.time() * 1000):x}{secrets.token_hex(6)}"


def check_name(name: Any, what: str = "id") -> str:
    if not isinstance(name, str) or not NAME_RE.match(name) or ".." in name:
        raise InvalidInput(f"invalid {what}: {name!r}")
    return name


def canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@dataclass
class Event:
    stream: str
    seq: int
    type: str
    data: dict
    actor: dict
    at: str
    event_id: str
    raw_sha256: str


class StreamUntrusted(Untrusted):
    pass


class Root:
    """An initialized relay root. All filesystem access goes through here."""

    def __init__(self, path: str, *, create: bool = False):
        if not path:
            raise UsageError("relay root required: pass --root or set AGENT_RELAY_ROOT")
        os.umask(0o077)
        path = os.path.abspath(path)
        if create:
            os.makedirs(path, mode=0o700, exist_ok=True)
        if not os.path.isdir(path):
            raise NotFound(f"relay root does not exist: {path}")
        self.path = os.path.realpath(path)
        if create:
            for sub in SUBDIRS:
                d = os.path.join(self.path, sub)
                if os.path.islink(d):
                    raise InvalidInput(f"refusing symlinked directory in root: {sub}")
                os.makedirs(d, mode=0o700, exist_ok=True)
        elif not os.path.exists(os.path.join(self.path, "config.json")):
            raise NotFound(f"not an initialized relay root (run `relay init`): {self.path}")
        self.durability = os.environ.get("AGENT_RELAY_DURABILITY", "full")
        self._config: dict | None = None

    # ----------------------------------------------------------------- paths
    def p(self, *parts: str) -> str:
        """Join validated components under the root, refusing any symlink."""
        cur = self.path
        for part in parts:
            check_name(part, "path component")
            cur = os.path.join(cur, part)
            try:
                st = os.lstat(cur)
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(st.st_mode):
                raise InvalidInput(f"symlink inside relay root refused: {os.path.relpath(cur, self.path)}")
        real_parent = os.path.realpath(os.path.dirname(cur))
        if real_parent != self.path and not real_parent.startswith(self.path + os.sep):
            raise InvalidInput("path escapes relay root")
        return cur

    def ensure_dir(self, *parts: str) -> str:
        d = self.p(*parts)
        try:
            os.mkdir(d, 0o700)
        except FileExistsError:
            if not stat.S_ISDIR(os.lstat(d).st_mode):
                raise InvalidInput(f"not a directory: {os.path.relpath(d, self.path)}")
        return d

    def lock(self, name: str, timeout: float = 20.0):
        """Exclusive cross-process lock on `locks/<name>` (a context manager).

        For the few multi-stream decisions that must not interleave across processes
        (e.g. releasing a team's claims vs. handing its conversations over again)."""
        import contextlib
        import fcntl

        @contextlib.contextmanager
        def held():
            self.ensure_dir("locks")
            fd = os.open(self.p("locks", check_name(name, "lock")), os.O_RDWR | os.O_CREAT, 0o600)
            try:
                deadline = time.time() + timeout
                while True:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.time() > deadline:
                            raise Conflict(f"busy: another relay process holds {name}; try again")
                        time.sleep(0.02)
                yield
            finally:
                os.close(fd)  # closing the descriptor releases the lock
        return held()

    # ------------------------------------------------------------ primitives
    def _fsync_fd(self, fd: int) -> None:
        if self.durability == "none":
            return
        if self.durability == "full" and hasattr(__import__("fcntl"), "F_FULLFSYNC"):
            import fcntl

            try:
                fcntl.fcntl(fd, fcntl.F_FULLFSYNC)
                return
            except OSError:
                pass
        os.fsync(fd)

    def _fsync_dir(self, d: str) -> None:
        if self.durability == "none":
            return
        fd = os.open(d, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def publish(self, parts: Iterable[str], data: bytes) -> bool:
        """Atomically create a file; return False if it already exists."""
        parts = tuple(parts)
        if len(data) > MAX_RECORD_BYTES:
            raise InvalidInput(f"record too large ({len(data)} bytes)")
        target = self.p(*parts)
        tmp = self.p("tmp", f"{secrets.token_hex(12)}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                self._fsync_fd(f.fileno())
            try:
                os.link(tmp, target)
            except FileExistsError:
                return False
            self._fsync_dir(os.path.dirname(target))
            return True
        finally:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass

    def read_bytes(self, *parts: str) -> bytes:
        path = self.p(*parts)
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            raise NotFound(f"not found: {'/'.join(parts)}")
        except OSError as e:  # ELOOP from O_NOFOLLOW
            raise InvalidInput(f"cannot open {'/'.join(parts)}: {e.strerror}")
        with os.fdopen(fd, "rb") as f:
            if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
                raise InvalidInput(f"not a regular file: {'/'.join(parts)}")
            data = f.read(MAX_RECORD_BYTES + 1)
        if len(data) > MAX_RECORD_BYTES:
            raise InvalidInput(f"record too large: {'/'.join(parts)}")
        return data

    def read_json(self, *parts: str) -> Any:
        data = self.read_bytes(*parts)
        try:
            return json.loads(data)
        except (ValueError, UnicodeDecodeError) as e:
            raise InvalidInput(f"malformed JSON in {'/'.join(parts)}: {e}")

    def listdir(self, *parts: str) -> list[str]:
        d = self.p(*parts)
        try:
            names = os.listdir(d)
        except FileNotFoundError:
            return []
        return sorted(n for n in names if NAME_RE.match(n))

    def exists(self, *parts: str) -> bool:
        return os.path.lexists(self.p(*parts))

    def write_view(self, name: str, obj: Any) -> None:
        """Replace a disposable cache file (views/ is never authoritative)."""
        target = self.p("views", name)
        tmp = self.p("tmp", f"{secrets.token_hex(12)}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(json.dumps(obj, indent=2, sort_keys=True).encode())
        os.replace(tmp, target)

    # --------------------------------------------------------------- config
    @property
    def config(self) -> dict:
        if self._config is None:
            cfg = self.read_json("config.json")
            if not isinstance(cfg, dict) or cfg.get("schema_version") != SCHEMA_VERSION:
                raise Untrusted("config.json has an unsupported schema_version")
            self._config = cfg
        return self._config

    # -------------------------------------------------------------- streams
    def read_stream(self, stream: str) -> list[Event]:
        """Read and verify a whole stream. Raises StreamUntrusted on damage."""
        check_name(stream, "stream")
        names = self.listdir("streams", stream)
        events: list[Event] = []
        prev = None
        for i, name in enumerate(names, start=1):
            if name != f"{i:08d}.json":
                raise StreamUntrusted(f"stream {stream}: unexpected entry {name!r} at position {i}")
            raw = self.read_bytes("streams", stream, name)
            try:
                obj = json.loads(raw)
            except ValueError:
                raise StreamUntrusted(f"stream {stream}: malformed event {name}")
            if not isinstance(obj, dict) or obj.get("schema_version") != SCHEMA_VERSION:
                raise StreamUntrusted(f"stream {stream}: unsupported event schema in {name}")
            if obj.get("seq") != i or obj.get("stream") != stream or obj.get("prev_sha256") != prev:
                raise StreamUntrusted(f"stream {stream}: broken sequence or hash chain at {name}")
            if not isinstance(obj.get("type"), str) or not isinstance(obj.get("data"), dict):
                raise StreamUntrusted(f"stream {stream}: invalid event body in {name}")
            prev = sha256(raw)
            events.append(Event(stream, i, obj["type"], obj["data"], obj.get("actor") or {},
                                obj.get("at", ""), obj.get("event_id", ""), prev))
        return events

    def append(self, stream: str, validate: Callable[[list[Event]], tuple[str, dict] | None],
               actor: dict) -> Event | None:
        """Optimistically append one event.

        `validate(events)` sees the current stream and returns (type, data) to
        append, or None for a no-op (e.g. an idempotent duplicate). It raises to
        refuse. On a lost race the stream is re-read and `validate` runs again.
        """
        check_name(stream, "stream")
        self.ensure_dir("streams", stream)
        for _ in range(APPEND_RETRIES):
            events = self.read_stream(stream)
            decision = validate(events)
            if decision is None:
                return None
            etype, data = decision
            seq = len(events) + 1
            record = {
                "schema_version": SCHEMA_VERSION,
                "stream": stream,
                "seq": seq,
                "event_id": new_id("e"),
                "type": etype,
                "at": now_iso(),
                "actor": actor,
                "data": data,
                "prev_sha256": events[-1].raw_sha256 if events else None,
            }
            raw = json.dumps(record, sort_keys=True, indent=1).encode()
            if self.publish(("streams", stream, f"{seq:08d}.json"), raw):
                return Event(stream, seq, etype, data, actor, record["at"], record["event_id"], sha256(raw))
            time.sleep(0.001 * (1 + secrets.randbelow(5)))
        raise Conflict(f"stream {stream}: too much contention, try again")


def init_root(path: str, participants: list[str], *, max_turns: int = 10,
              turn_timeout_s: int = 900) -> Root:
    if participants == ["*"]:
        pass  # open: each session gets its own validated label (used by the session console)
    else:
        for name in participants:
            check_name(name, "participant")
        if len(set(participants)) != len(participants) or len(participants) < 2:
            raise InvalidInput("need at least two distinct participants (or '*' for open labels)")
    root = Root(path, create=True)
    cfg = {
        "schema_version": SCHEMA_VERSION,
        "created_at": now_iso(),
        "participants": participants,
        "limits": {"max_turns": max_turns, "turn_timeout_s": turn_timeout_s},
    }
    if not root.publish(("config.json",), json.dumps(cfg, indent=2).encode()):
        existing = root.config
        if existing.get("participants") != participants:
            raise Conflict("root already initialized with different participants",
                           participants=existing.get("participants"))
    return root


def actor(kind: str, ident: str | None = None, **extra: Any) -> dict:
    a = {"kind": kind, "pid": os.getpid(), "host": socket.gethostname()}
    if ident:
        a["id"] = ident
    a.update(extra)
    return a
