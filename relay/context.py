"""Session context: a conversation's title and short work summary, with source and time.

Context is description only. It is stored in its own append-only stream per
session (`ctx-<hash>`), separate from endpoints, reservations and pairs, so saving
or reporting context never grants the ability to drive a session.

Sources (see docs/CONTEXT-REPORTING.md):
* `client`  — metadata the client keeps itself (Claude's conversation title, a
              Codex thread name), discovered read-only;
* `relay`   — work the relay itself recorded (a managed session's goal and turn);
* `report`  — a claim made through `relay context report`. It attaches only to a
              listed session with the same exact identity and canonical folder, and
              says "from the session itself" only when process ancestry corroborates it;
* `manual`  — what you typed in the console. Pinned per field until you clear it.
"""

from __future__ import annotations

import hashlib
import os
import time
from typing import Any

from .storage import Conflict, InvalidInput, RelayError, actor

MAX_TITLE = 120
MAX_SUMMARY = 400
SOURCES = ("manual", "report")  # sources stored here; client/relay context is derived live
STALE_AFTER_S = 6 * 3600
REPORT_EVERY_S = 10  # at most one report per conversation per 10 s


def context_key(client: str, session_id: str) -> str:
    """Stable identity of a conversation across discovery and registration."""
    key = f"{(client or 'unknown').lower()}:{session_id}"
    if not session_id or len(key) > 300 or any(c in key for c in "\n\r\0"):
        raise InvalidInput("bad session identity")
    return key


def _stream(key: str) -> str:
    return "ctx-" + hashlib.sha256(key.encode()).hexdigest()[:40]


def _clean(value: Any, limit: int, what: str, one_line: bool) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidInput(f"{what} must be text")
    v = " ".join(value.split()) if one_line else value.strip()
    if len(v) > limit:
        raise InvalidInput(f"keep the {what} under {limit} characters")
    return v


class ContextStore:
    def __init__(self, bus):
        self.bus = bus

    def save(self, key: str, *, title: str | None = None, summary: str | None = None,
             source: str = "manual", by: dict | None = None, cwd: str | None = None,
             reporter: dict | None = None) -> dict:
        """Record a title and/or summary. An empty string clears that field.

        A `report` also records the folder it claims and the reporting process's
        ancestry, so the console can decide whether (and how) to show it."""
        if source not in SOURCES:
            raise InvalidInput("unknown context source")
        data = {"key": key, "source": source}
        if source == "report":
            if not cwd:
                raise InvalidInput("a report needs the folder it is about")
            data["cwd"] = os.path.realpath(cwd)
            data["reporter"] = reporter or {}
        t = _clean(title, MAX_TITLE, "title", True)
        s = _clean(summary, MAX_SUMMARY, "summary", True)
        if t is None and s is None:
            raise InvalidInput("give a title or a summary")
        if t is not None:
            data["title"] = t
        if s is not None:
            data["summary"] = s
        def decide(events):
            # Runs inside the stream's append: a writer that lost the race re-runs this
            # against the winner's event, so two reports can't both pass the limit.
            if source == "report":
                last = max((_epoch(e.at) for e in events if e.type == "context"
                            and e.data.get("key") == key and e.data.get("source") == "report"), default=0)
                if last and time.time() - last < REPORT_EVERY_S:
                    raise Conflict("reported too recently; try again in a few seconds")
            return "context", data
        self.bus.root.append(_stream(key), decide, by or actor("human", "console"))
        return self.get(key)

    def get(self, key: str) -> dict:
        """Latest value per (source, field), with the time it was set."""
        out: dict[str, dict] = {}
        try:
            events = self.bus.root.read_stream(_stream(key))
        except RelayError:
            return out
        for e in events:
            d = e.data
            if e.type != "context" or d.get("key") != key:
                continue
            src = out.setdefault(d["source"], {})
            src["_at"] = e.at
            for field in ("title", "summary"):
                if field in d:
                    src[field] = {"text": d[field] or None, "at": e.at, "cwd": d.get("cwd"),
                                  "reporter": d.get("reporter")}
        return out


def _epoch(iso: str) -> float:
    import calendar
    try:
        return calendar.timegm(time.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S"))
    except (ValueError, TypeError):
        return 0.0


def process_start_ms(pid: int) -> float | None:
    """When a process started (ms since epoch, 1 s resolution), or None if unknown."""
    import subprocess
    try:
        out = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True,
                             text=True, timeout=5).stdout.split()
        return time.mktime(time.strptime(" ".join(out[:5]), "%a %b %d %H:%M:%S %Y")) * 1000
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None


def process_exited(pid: int) -> bool:
    """True if the process has exited but not been reaped (a zombie): it can't write anything."""
    import subprocess
    try:
        out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True,
                             text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return False
    return out.startswith("Z")


def process_ancestry(limit: int = 40) -> list[dict]:
    """This process and its ancestors: pid and start time (to guard against pid reuse)."""
    import subprocess
    chain, pid = [], os.getpid()
    for _ in range(limit):
        try:
            out = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True,
                                 text=True, timeout=5).stdout.split()
        except (OSError, subprocess.TimeoutExpired):
            break
        if not out:
            break
        chain.append({"pid": pid, "started_ms": process_start_ms(pid)})
        ppid = int(out[0])
        if ppid <= 1 or ppid == pid:
            break
        pid = ppid
    return chain


def valid_start(v) -> bool:
    """A usable process start time: a positive number (not None, not a bool)."""
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0


def verify_identity(client: str, session: str, cwd: str, *, codex_home: str | None = None) -> None:
    """Check, read-only, that the client's own records know this conversation in this
    folder before a report may use it. Presence of an environment variable alone is
    not proof. Raises InvalidInput when it can't be confirmed."""
    cwd = os.path.realpath(cwd)
    if client == "claude":
        from .adapters.claude import transcript_path
        if not os.path.exists(transcript_path(cwd, session)):
            raise InvalidInput(f"can't confirm Claude conversation {session[:8]}… in {cwd}")
        return
    if client == "codex":
        import sqlite3
        home = codex_home or os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")
        path = os.path.join(home, "state_5.sqlite")
        if not os.path.exists(path):
            raise InvalidInput("can't read Codex's local records to confirm the conversation")
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
        try:
            row = con.execute("select cwd from threads where id=?", (session,)).fetchone()
        finally:
            con.close()
        if not row:
            raise InvalidInput(f"Codex has no conversation {session[:8]}…")
        if os.path.realpath(row[0] or "") != cwd:
            raise InvalidInput(f"Codex conversation {session[:8]}… belongs to another folder")
        return
    raise InvalidInput("client must be claude or codex")
