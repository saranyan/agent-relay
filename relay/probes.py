"""Shared discipline for the bounded live probes.

- A process is identified by its pid AND a valid start time; unknown or unproven identity
  never authorizes a signal (finding A).
- Time is one monotonic budget from the run's defined start, passed through every blocking
  phase; cleanup has its own separate, reported bound (finding D).
"""

from __future__ import annotations

import argparse
import math
import os
import re
import select
import signal
import subprocess
import threading
import time
from typing import Callable

from .context import process_start_ms


def valid_start(v) -> bool:
    """A usable process start time: a finite, positive number (never a bool or a string)."""
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v > 0


def same_process(pid, recorded, *, start_of: Callable = process_start_ms) -> bool:
    """True only when pid is valid and its observed start equals the recorded, valid start."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1 or not valid_start(recorded):
        return False
    observed = start_of(pid)
    return valid_start(observed) and observed == recorded


def deadline_arg(lo: float, hi: float):
    """argparse type: a finite number of seconds within [lo, hi]."""
    def parse(text: str) -> float:
        try:
            v = float(text)
        except ValueError:
            raise argparse.ArgumentTypeError("the deadline is a number of seconds") from None
        if not math.isfinite(v) or not lo <= v <= hi:
            raise argparse.ArgumentTypeError(f"the deadline must be between {lo:g} and {hi:g} seconds")
        return v
    return parse


class Budget:
    """An absolute monotonic deadline."""

    def __init__(self, seconds: float, clock: Callable[[], float] = time.monotonic, start: float | None = None):
        self.clock = clock
        self.end = (clock() if start is None else start) + seconds

    def remaining(self) -> float:
        return max(0.0, self.end - self.clock())

    def expired(self) -> bool:
        return self.remaining() <= 0.0

    def within(self, seconds: float) -> "Budget":
        """A nested budget: at most `seconds`, and never past this one."""
        b = Budget(0, self.clock)
        b.end = min(self.clock() + seconds, self.end)
        return b


def stop_child(*, proc: subprocess.Popen | None = None, pid: int | None = None, started_ms=None,
               grace: float = 5.0, kill: Callable = os.kill, start_of: Callable = process_start_ms) -> bool:
    """Stop a child this probe started; True only once its exit is confirmed.

    With its Popen handle (preferred): the handle refers to exactly that child until it is
    reaped, so no pid can be confused. Without one, a pid is signalled only after its identity
    is proven (valid, equal start times); otherwise nothing is sent and the result is False
    (cleanup unconfirmed)."""
    if proc is not None:
        if proc.poll() is None:
            proc.kill()
        try:
            proc.wait(grace)
        except subprocess.TimeoutExpired:
            return False
        return True
    if not same_process(pid, started_ms, start_of=start_of):
        return False
    try:
        kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return True
    end = time.monotonic() + grace
    while time.monotonic() < end:
        try:
            kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            pass
        observed = start_of(pid)
        if valid_start(observed) and observed != started_ms:
            return True  # the pid now belongs to another process: ours is gone
        time.sleep(0.05)
    return False


# ------------------------------------------------- stderr diagnostics
# A FIXED taxonomy: each stderr line is put in the first class whose pattern matches, or in
# "unclassified". Only class counts and safe numbers/booleans are ever kept: never the text,
# paths, credentials, config contents or transcript snippets. Order matters: a denied open of a
# .json file is a permission problem, not a parse error; a parse error that echoes config content
# (an auth field, say) is still a parse error.
STDERR_CLASSES = ("permission_denied", "unsupported_option", "config_parse", "auth_bootstrap",
                  "tls_network", "sandbox_runtime", "unclassified")
_STDERR_RULES = tuple((cls, re.compile(rx, re.I)) for cls, rx in (
    ("permission_denied", r"\beperm\b|\beacces\b|operation not permitted|permission denied"),
    ("unsupported_option", r"unknown option|unrecognized option|unknown argument|unexpected argument|"
                           r"too many arguments|missing required argument|invalid (option|argument|choice)|"
                           r"argument missing|^\s*usage:|^\s*error: option"),
    ("config_parse", r"parse error|failed to parse|could not parse|syntaxerror|unexpected token|"
                     r"invalid json|malformed|invalid (settings|config)"),
    ("auth_bootstrap", r"not logged in|please (log|sign) ?in|/login\b|authenticat|unauthori[sz]ed|"
                       r"api key|oauth|credential|keychain|\b401\b|\b403\b"),
    ("tls_network", r"enotfound|econnrefused|econnreset|etimedout|eai_again|getaddrinfo|certificate|"
                    r"\btls\b|\bssl\b|fetch failed|socket hang up|network"),
    ("sandbox_runtime", r"sandbox|dtrace|segmentation fault|sigsegv|sigabrt|sigbus|sigill|abort|panic|"
                        r"\bdyld\b|library not loaded|out of memory|illegal instruction|uncaught|fatal"),
))


def stderr_class(line: str) -> str:
    for cls, rx in _STDERR_RULES:
        if rx.search(line):
            return cls
    return "unclassified"


# Structured native permission errors: from a line, only a FIXED errno
# enum, a FIXED syscall enum and a predefined path ROLE (from the caller's in-memory mapper) are
# kept. The quoted target itself is mapped and dropped at once; an unknown target stays "unknown".
ERRNOS = ("EACCES", "EPERM", "unknown")
SYSCALLS = ("open", "scandir", "opendir", "readdir", "readlink", "realpath", "access", "stat", "lstat",
            "mkdir", "write", "unlink", "rename", "rmdir", "copyfile", "unknown")
PATH_ROLES = ("home_root", "workspace", "run_folder", "relay_runs_folder", "workspace_ancestor_other",
              "client_state_dir", "client_config", "client_settings", "plugins",
              # temp: this run's private folder, the client's default per-uid
              # folder, a system temp root itself, and anything else in system temp.
              "run_private_temp", "client_default_temp", "system_temp_root", "system_temp_other",
              "device", "other", "unknown")
_ERRNO_NUM = {"13": "EACCES", "-13": "EACCES", "1": "EPERM", "-1": "EPERM"}
_STRERROR = {"permission denied": "EACCES", "operation not permitted": "EPERM"}
# Node/Bun: "EACCES: permission denied, scandir '/x'" (optionally "Error: ..." in front)
_RX_NODE = re.compile(r'''\b(EACCES|EPERM)\b(?::[^,]*)?,\s*([A-Za-z_]+)\s+(['"`])(.*?)\3''')
# Python: "[Errno 13] Permission denied: '/x'"
_RX_PY = re.compile(r'''\[Errno (-?\d+)\][^:'"]*:\s*(['"])(.*?)\2''')
# Shell/BSD: "/x: Operation not permitted" or "cannot open '/x': Permission denied"
_RX_BSD = re.compile(r'''(['"]?)(/[^'"\s:]*)\1:\s*(Operation not permitted|Permission denied)''', re.I)
# Inspected error properties, one per line: code: "EACCES" / syscall: "open" / path: "/x" / errno: -13
_RX_PROP = re.compile(r'''^\s*(code|syscall|path|errno)\s*:\s*(['"`]?)(.*?)\2\s*,?\s*$''')
_RX_BARE = re.compile(r"\b(EACCES|EPERM)\b")


def _enum(v, allowed) -> str:
    return v if v in allowed else "unknown"


class StderrDiagnostics:
    """Opt-in, bounded, category-and-enum-only processing of a child's stderr (diagnostic use only).

    `start(f)` drains the pipe on its own thread from spawn until EOF (or until `finish` asks it
    to stop after the child's exit is confirmed). Only the first RETAIN_BYTES are classified, in
    memory and line by line; everything after is still read and discarded, counted in bytes
    only, so a noisy child can never block on a full pipe. The text itself is never kept: each
    line is reduced to its class at once."""

    RETAIN_BYTES = 64 * 1024  # classified; the rest is drained and counted only
    LINE_MAX = 4096           # a longer line is classified on its first LINE_MAX bytes
    MAX_LINES = 1000

    MAX_STRUCTURED = 32  # distinct (errno, syscall, role) entries kept; more are only counted
    PROP_LINES = 8       # property lines merge into the error record started this many lines before

    def __init__(self, role_of=None):
        """`role_of(path) -> one of PATH_ROLES`: the caller's in-memory mapper (it never stores
        the path). Without one, every target's role is "unknown"."""
        self.role_of = role_of
        self.structured: dict[tuple, int] = {}
        self.structured_dropped = 0
        self._pending: dict | None = None
        self.counts = dict.fromkeys(STDERR_CLASSES, 0)
        self.bytes = self.lines = 0
        self.truncated = self.eof = self.started = self.finished = False
        self.capture_error: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._f = None

    def start(self, f) -> None:
        self._f, self.started = f, True
        self._thread = threading.Thread(target=self._run, args=(f.fileno(),), daemon=True)
        self._thread.start()

    def _line(self, raw: bytes) -> None:
        if self.lines >= self.MAX_LINES:
            self.truncated = True
            return
        text = raw[:self.LINE_MAX].decode("utf-8", errors="replace").strip()
        if text:
            self.lines += 1
            self.counts[stderr_class(text)] += 1
            self._structure(text)

    def _role(self, path: str | None) -> str:
        if not path or self.role_of is None:
            return "unknown"
        try:
            return _enum(self.role_of(path), PATH_ROLES)
        except Exception:  # noqa: BLE001 - a mapper failure is just an unknown role
            return "unknown"

    def _flush(self) -> None:
        p, self._pending = self._pending, None
        if p is None or p["errno"] == "unknown":
            return  # no EACCES/EPERM was named: not a permission error (e.g. stray properties)
        key = (p["errno"], p["syscall"], p["role"])
        if key in self.structured or len(self.structured) < self.MAX_STRUCTURED:
            self.structured[key] = self.structured.get(key, 0) + 1
        else:
            self.structured_dropped += 1

    def _start(self, errno: str, syscall: str, path: str | None) -> None:
        self._flush()
        self._pending = {"errno": _enum(errno, ERRNOS), "syscall": _enum(syscall, SYSCALLS),
                         "role": self._role(path), "age": 0}

    def _structure(self, text: str) -> None:
        """One line -> at most one structured record (errno, syscall, role); the text is dropped."""
        if self._pending is not None:
            self._pending["age"] += 1
            if self._pending["age"] > self.PROP_LINES:
                self._flush()
        m = _RX_NODE.search(text)
        if m:
            return self._start(m.group(1), m.group(2).lower(), m.group(4))
        m = _RX_PY.search(text)
        if m:
            return self._start(_ERRNO_NUM.get(m.group(1), "unknown"), "unknown", m.group(3))
        m = _RX_BSD.search(text)
        if m:
            return self._start(_STRERROR[m.group(3).lower()], "unknown", m.group(2))
        m = _RX_PROP.match(text)
        if m:
            key, val = m.group(1), m.group(3)
            field, got = {"code": ("errno", lambda: _enum(val, ERRNOS)),
                          "errno": ("errno", lambda: _ERRNO_NUM.get(val, "unknown")),
                          "syscall": ("syscall", lambda: _enum(val.lower(), SYSCALLS)),
                          "path": ("role", lambda: self._role(val))}[key]
            got = got()
            p = self._pending
            if p is not None and p[field] not in ("unknown", got):
                self._flush()  # it contradicts the open record: another error's properties
                p = None
            if p is None:
                p = self._pending = {"errno": "unknown", "syscall": "unknown", "role": "unknown", "age": 0}
            if p[field] == "unknown":
                p[field] = got
            return None
        m = _RX_BARE.search(text)
        if m:
            return self._start(m.group(1), "unknown", None)
        return None

    def _run(self, fd: int) -> None:
        buf, kept = b"", 0
        try:
            while not self._stop.is_set():
                if not select.select([fd], [], [], 0.1)[0]:
                    continue
                chunk = os.read(fd, 65536)
                if not chunk:
                    self.eof = True
                    break
                self.bytes += len(chunk)
                take = chunk[:max(0, self.RETAIN_BYTES - kept)]
                kept += len(take)
                if len(take) < len(chunk):
                    self.truncated = True
                buf += take
                *done, buf = buf.split(b"\n")
                for raw in done:
                    self._line(raw)
                if len(buf) > self.LINE_MAX:  # an over-long line: classify its start, drop the rest
                    self._line(buf)
                    buf, self.truncated = b"", True
            if buf and (self.eof or kept >= self.RETAIN_BYTES):
                self._line(buf)
        except Exception as e:  # noqa: BLE001 - recorded as a class name only
            self.capture_error = type(e).__name__
        finally:
            buf = b""
            try:
                self._flush()  # a record still collecting property lines
            except Exception as e:  # noqa: BLE001
                self.capture_error = self.capture_error or type(e).__name__

    def finish(self, grace: float = 2.0) -> None:
        """After the child's exit is confirmed: let the drain reach EOF (bounded), then stop it
        and close the read end. Idempotent; never raises."""
        if self.finished:
            return
        self.finished = True
        t = self._thread
        if t is not None:
            t.join(grace)
            if t.is_alive():  # a descendant may still hold the write end: stop reading
                self._stop.set()
                t.join(1.0)
            if t.is_alive():
                self.capture_error = self.capture_error or "drain_still_running"
                return  # the thread still uses the descriptor: leave it open, recorded
        if self._f is not None:
            try:
                self._f.close()
            except OSError:
                pass

    def summary(self) -> dict:
        """The only stderr evidence: class counts and safe numbers/booleans."""
        return {"captured": self.started, "classes": {k: v for k, v in self.counts.items() if v},
                "lines": self.lines, "bytes": self.bytes, "truncated": self.truncated,
                "eof": self.eof, "capture_error": self.capture_error,
                "structured": [{"errno": e, "syscall": c, "role": r, "count": n}
                               for (e, c, r), n in sorted(self.structured.items())],
                "structured_dropped": self.structured_dropped}
