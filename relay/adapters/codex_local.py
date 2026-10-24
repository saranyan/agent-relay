"""Observe-only discovery of standalone Codex sessions (codex-local).

Read-only sources, verified against codex-cli 0.160 on macOS:
* the process table (`ps`) for running `codex` TUIs and their start time and
  arguments (`codex resume <thread-id>` names its thread exactly);
* the open-file table (`lsof -d cwd`) for each process's working folder;
* Codex's own local database (`~/.codex/state_5.sqlite`, opened read-only) for the
  thread's name, folder and update time, and `thread_history_1.sqlite` for whether
  its latest turn is still in progress.

Never read: prompts (`first_user_message`, `preview`), transcripts or credentials.
Never done: starting a server, resuming a thread, or sending anything. These
sessions are visible only; the relay cannot drive them.
"""

from __future__ import annotations

import os
import re
import sqlite3
import subprocess
import time

from ..storage import RelayError
from .base import CAPABILITIES, Adapter, SessionInfo

UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
SKIP = {"app-server", "exec-server", "mcp", "login", "logout", "doctor", "completion", "features",
        "update", "debug", "sandbox", "apply", "cloud", "plugin", "remote-control", "agents"}
LIMITS = ["view only: a Codex session running in its own terminal can't be driven by the relay"]


def _iso(ms: float | None) -> str | None:
    if not ms:
        return None
    t = ms / 1000
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + f".{int(ms) % 1000:03d}Z"


BUSY = {"inProgress"}
DONE = {"completed", "failed", "interrupted"}  # observed in codex-cli 0.160; anything else is unknown


def turn_activity(status: str | None) -> str:
    if status in BUSY:
        return "busy"
    if status in DONE:
        return "idle"
    return "unknown"


def running_codex() -> list[dict]:
    """Interactive `codex` processes: pid, start time (epoch ms), argv.

    Only `codex resume <id>` names the running conversation exactly. `codex fork <id>`
    names the *source* conversation (the fork gets a new id), so it is recorded as
    `forked_from`, never as the process's own thread.
    """
    try:
        out = subprocess.run(["ps", "-axo", "pid=,lstart=,command="], capture_output=True,
                             text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    procs = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 7:
            continue
        pid, lstart, argv = parts[0], " ".join(parts[1:6]), parts[6:]
        if os.path.basename(argv[0]) != "codex":
            continue
        if len(argv) > 1 and argv[1] in SKIP:
            continue
        try:
            started = time.mktime(time.strptime(lstart, "%a %b %d %H:%M:%S %Y")) * 1000
        except ValueError:
            started = None
        sub = argv[1] if len(argv) > 1 else None
        named = argv[2] if len(argv) > 2 and UUID.match(argv[2]) else None
        procs.append({"pid": int(pid), "started_ms": started, "argv": argv,
                      "thread": named if sub == "resume" else None,
                      "forked_from": named if sub == "fork" else None})
    return procs


def process_cwd(pid: int) -> str | None:
    try:
        out = subprocess.run(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"], capture_output=True,
                             text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    for line in out.splitlines():
        if line.startswith("n"):
            return line[1:]
    return None


class CodexLocalAdapter(Adapter):
    name = "codex-local"
    verified = True  # observe-only; read-only sources checked locally

    def capabilities(self) -> dict[str, bool]:
        caps = {c: False for c in CAPABILITIES}
        caps.update(discover=True, busy_state=True)
        return caps

    def namespace(self) -> str:
        return "codex:" + os.path.realpath(self._home())

    def _home(self) -> str:
        return self.options.get("codex_home") or os.path.expanduser("~/.codex")

    def _db(self, name: str):
        path = os.path.join(self._home(), name)
        if not os.path.exists(path):
            return None
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
        con.execute("pragma query_only = 1")
        return con

    def writers_for(self, conversation: str) -> list[dict] | None:
        """Native Codex processes writing `conversation`, or None if that can't be ruled out.

        Only `codex resume <id>` names its conversation. A plain `codex` / `codex resume`
        in the same folder could be writing it, so its presence makes the answer unknown
        (never absence). `codex fork <id>` writes a NEW conversation and doesn't count."""
        procs = self.options.get("_procs")
        procs = running_codex() if procs is None else procs
        try:
            state = self._db("state_5.sqlite")
            row = state.execute("select cwd from threads where id=?", (conversation,)).fetchone()
        except sqlite3.Error:
            return None
        if not row:
            return None
        folder = os.path.realpath(row[0] or "")
        out = []
        for p in procs:
            if p.get("thread") == conversation:
                out.append({"pid": p["pid"], "started_ms": p.get("started_ms"), "writer": "native"})
            elif p.get("thread") is None and p.get("forked_from") is None:
                cwd = p.get("cwd") if "cwd" in p else process_cwd(p["pid"])
                if cwd is None or os.path.realpath(cwd) == folder:
                    return None  # an unnamed codex in that folder (or unknown folder): can't rule out
        return out

    def discover(self) -> list[SessionInfo]:
        """One record per running process. Only an exact `resume <id>` gives the
        conversation's identity; anything else keeps the process's own identity
        (pid + start time) and at most a labelled *candidate* thread."""
        procs = self.options.get("_procs")  # injected by tests
        procs = running_codex() if procs is None else procs
        if not procs:
            return []
        try:
            state = self._db("state_5.sqlite")
            hist = self._db("thread_history_1.sqlite")
        except sqlite3.Error as e:
            raise RelayError(f"can't read Codex's local state: {e}")
        found = []
        try:
            for p in procs:
                cwd = p.get("cwd") or process_cwd(p["pid"])
                row, match = None, "none"
                if state is not None and p.get("thread"):
                    row = state.execute("select id, cwd, name, updated_at_ms from threads where id=?",
                                        (p["thread"],)).fetchone()
                    match = "exact" if row else "none"
                elif state is not None and cwd:
                    # newest thread in this folder active since the process started; for a fork,
                    # never its source conversation
                    row = state.execute(
                        "select id, cwd, name, updated_at_ms from threads where cwd=? and archived=0 "
                        "and updated_at_ms>=? and id!=? order by updated_at_ms desc limit 1",
                        (cwd, p.get("started_ms") or 0, p.get("forked_from") or "")).fetchone()
                    match = "likely" if row else "none"
                found.append((p, cwd, row, match))
            # Two uncertain processes guessing the same thread: neither may claim it.
            guesses: dict = {}
            for p, cwd, row, match in found:
                if match == "likely":
                    guesses[row[0]] = guesses.get(row[0], 0) + 1
            out = []
            for p, cwd, row, match in found:
                if match == "likely" and guesses[row[0]] > 1:
                    match = "ambiguous"
                status = None
                if row and match in ("exact", "likely") and hist is not None:
                    last = hist.execute("select status from thread_turns where thread_id=? "
                                        "order by rollout_ordinal desc limit 1", (row[0],)).fetchone()
                    status = last[0] if last else None
                title = row[2] if row and row[2] and match in ("exact", "likely") else None
                lim = list(LIMITS)
                if match == "exact":
                    sid, activity = row[0], turn_activity(status)
                else:
                    sid = f"pid-{p['pid']}-{int(p.get('started_ms') or 0)}"
                    # a single likely candidate's activity is shown, flagged unconfirmed on the row;
                    # an ambiguous or missing match gives no activity at all
                    activity = turn_activity(status) if match == "likely" else "unknown"
                    lim.append({"likely": "conversation not confirmed: matched by folder and recency",
                                "ambiguous": "conversation not confirmed: several Codex processes here",
                                "none": "no Codex conversation matched this process"}[match])
                if p.get("forked_from"):
                    lim.append("forked from another conversation; the fork's own id is not confirmed")
                extra = {"client": "Codex", "pid": p["pid"], "started_ms": p.get("started_ms"),
                         "match": match, "identity": "exact" if match == "exact" else "uncertain",
                         # An open terminal: hand-off is its only route, and only once its
                         # conversation is known exactly (`codex resume <id>`), never on a guess.
                         "interactive": match == "exact",
                         "candidate_thread": row[0] if row and match == "likely" else None,
                         "candidate_activity": turn_activity(status) if match == "likely" else None,
                         "forked_from": p.get("forked_from"),
                         "title": title, "title_updated": _iso(row[3]) if row and title else None,
                         "title_source": ("Codex thread name" + (" (likely match, not confirmed)"
                                          if match == "likely" else "")) if title else None}
                out.append(SessionInfo(sid, (row[1] if match == "exact" and row[1] else cwd),
                                       activity, False, lim, extra))
            return out
        finally:
            for con in (state, hist):
                if con is not None:
                    con.close()
