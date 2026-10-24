#!/usr/bin/env python3
"""The bounded Claude Code state-path probe.

Identifies which of the client's own files a coding-policy conversation needs, by running it
under the REAL coding sandbox with a fixed candidate state allowlist and observing what the
sandbox denies. Bounds:

- one NEW conversation (relay-generated `--session-id`), never an existing one; T1 starts it
  fresh, T2 resumes it, and T2 runs only if T1 completed with Claude's own init record verified
  (tools, mode, no MCP, session, folder) and no stop condition. At most 2 model turns, 180 s
  each, and an overall deadline.
- the profile is fixed for the whole run: never widened, nothing retried, nothing in the home
  folder created or changed by this script.
- the sandbox log collector must PROVE capture first: it must report ready (bounded), then a
  denial from the probe's own canary must arrive, attributed by pid + start time + in-lifetime
  source timestamp. The startup header alone proves nothing. If capture can't be established, nothing is started: an absence
  of denials is never treated as proof of access.
- log records are parsed in memory and never saved. Only predefined path templates/categories,
  the operation and bounded counts are persisted. A path outside the predefined templates is
  "unknown", and stops the run. So do a denial outside the client's own areas, a denied network
  port other than 443, an unexpected tool, and a denial from a client process this script
  couldn't attribute to its own child.
- process attribution: such a client-like denial is judged from pid, start
  time and parent pid/start in bounded `ps` snapshots (a baseline before any probe process, one
  per poll, one lookup per scan). Proven owned (a verified chain to this run's exact client) is
  attributed to that turn; proven foreign (the process or a verified ancestor was in the
  baseline, the event inside its seen lifetime) is left out of this run's evidence and counted;
  anything unknown still stops the run. Only fixed name types and numbers are kept; nothing is
  ever signalled for this.
- launch boundary (startup-only): paired wall/monotonic stamps are taken
  immediately before and after the client's Popen, and kept as phase markers with run start,
  capture ready and canary finished. An event still unknown whose source time is clearly before
  the before-Popen stamp (by more than PRE_CLIENT_MARGIN_S, with every clock reading consistent)
  is "pre_client" setup, left out of the client's safety and evidence with its record kept; at,
  near or after it, without a time, or after a clock change, it stays unknown and stops.
- no message text, tool input, file content or credential is recorded: booleans, hashes,
  key names and counts only.
- cleanup: the client's stdin is closed and its exit confirmed (killed if needed), its private
  temp folder removed; the scratch transcript is kept and listed.

Output in the run folder: `probe-state.json` (evidence) and `claude-state-<version>.proposed.json`
(a proposal with `proven_by` empty, for separate review). Nothing is pinned or enabled.

Diagnostic-only mode (`--diagnostic-only`): the same version, candidate scope,
CLI restrictions, capture and cleanup gates, but ONE fresh conversation and ONE prompt (T1), never
a resume or T2, observed for at most DIAG_S seconds from submission. It ends earlier on a verified
init, the turn's completion, EOF or an interruption. Attributed OS-denied FILE operations are
recorded as diagnostic observations (they stay denied) instead of ending the turn; every hard
safety stop still ends it at once: ambiguous attribution or timestamps, an unidentified client
process, identity or init-policy mismatch, any tool use, a forbidden network or other non-file
denial. Nothing is routed or proven: no proposal is written, `proven_by` stays empty, and an
observed init is startup evidence only.

Diagnostic exit and stderr observability: the client's stderr is an opt-in pipe
drained from spawn through its confirmed exit, reduced at once to a FIXED set of classes
(PR.STDERR_CLASSES); only class counts and safe numbers/booleans are kept, never the text. The
process status is read at the observed EOF BEFORE any cleanup (after at most DIAG_EXIT_WAIT_S for
a natural exit), then the final reaped status and what the probe itself did (closed the input,
sent SIGTERM/SIGKILL; the adapter's own policy termination) are recorded, so a probe kill is never
reported as the client's own exit. `cause` is natural_exit, probe_termination or unknown.

Structured stderr: native permission errors are reduced in memory to a fixed
errno (PR.ERRNOS), syscall (PR.SYSCALLS) and path ROLE (PR.PATH_ROLES, mapped from this run's own
context; the path is dropped). An error is "matched" only when an attributed OS denial in the same
turn names the same known role with an operation that syscall needs; otherwise "unknown".

Startup-only mode (`--startup-only`): the same gates, argv and sandbox, one fresh client, but NO
prompt: nothing is written to its input, which stays open. Observed for at most STARTUP_S seconds
from launch, then stopped and reaped. `startup_result` says what the start alone showed; a client
still waiting for input is `waiting_for_input`, which is inconclusive (`conclusive: false`).

Exit codes: 0 both turns completed, 2 stopped by a rule (evidence written), 3 diagnostic
observation recorded (diagnostic-only; never a success), 4 preconditions not met (nothing
started), 5 cleanup couldn't be confirmed.

Usage (live use needs the user's specific authorization):
  RUN=$(mktemp -d ~/relay-runs/claude-state.XXXXXX)
  python3 scripts/claude_state_probe.py --run-dir "$RUN"
  python3 scripts/claude_state_probe.py --run-dir "$RUN" --diagnostic-only
  python3 scripts/claude_state_probe.py --run-dir "$RUN" --startup-only
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from relay import probes as PR  # noqa: E402
from relay import toolpolicy as T  # noqa: E402
from relay.adapters import claude as CL  # noqa: E402
from relay.context import process_start_ms  # noqa: E402
from relay.storage import RelayError, now_iso  # noqa: E402

WANT_CLAUDE = "2.1.296 (Claude Code)"
VERSION = "2.1.296"
T1 = "Reply with exactly PROBE-OK. Do not use any tools."
T2 = "Create a file named hello.txt in the current folder containing exactly: hi"
TURN_S = 180.0
DIAG_S = 30.0  # diagnostic-only: the observation window, from the prompt's submission
DIAG_ACCEPT_S = 0.2  # diagnostic-only: submit's own wait for the replay; the loop does the rest
DIAG_EXIT_WAIT_S = 1.0  # diagnostic-only: after EOF, how long a natural exit is waited for (no signal)
STARTUP_S = 5.0  # startup-only: how long the client is observed from launch, with NO prompt sent
DEADLINE_RANGE = (30.0, 1800.0)
EVIDENCE = "probe-state.json"
# The fixed candidate: this conversation's transcript and, so a fresh conversation can create
# it, exactly its project folder (a literal: creating the folder, not writing inside it).
# Read-only bootstrap literals (after live probe #2): the client's config and
# user settings files, readable and never writable. A PROBE candidate only: not pinned, never
# `proven_by`, and no permission to apply hooks/settings (`--restricted` is unchanged).
CANDIDATE = {"write_files": ["{home}/.claude/projects/{slug}", "{home}/.claude/projects/{slug}/{session}.jsonl"],
             "read_files": ["{home}/.claude.json", "{home}/.claude/settings.json"],
             "read_dirs": [], "write_dirs": []}
CLIENT_TOPS = ("settings.json", "settings.local.json", "statsig", "todos", "session-env", "shell-snapshots",
               "debug", "cache", "plugins", "ide", "telemetry", "file-history", "paste-cache", "sessions",
               "state", "backups", "history.jsonl", "skills", "downloads", "chrome", ".credentials.json",
               "mcp-needs-auth-cache.json", ".last-cleanup", ".last-update-result.json", "agents", "commands",
               "local", "projects")
SAFE = re.compile(r"^[A-Za-z0-9._ -]{1,80}$")
LINE = re.compile(r"^(?:(\d+) duplicate reports? for )?Sandbox: (.+?)\((\d+)\) deny\(\d+\) (\S+)(?: (.*))?$")


# ------------------------------------------------------------------ the log collector
def source_ts(rec: dict) -> float | None:
    """The log's own event time (epoch seconds), or None if it's missing or unparseable."""
    raw = rec.get("timestamp")
    if not isinstance(raw, str):
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d %H:%M:%S.%f%z").timestamp()
    except ValueError:
        return None


# `log stream` prints this status line on STDOUT before any record (checked on the installed
# macOS, 2026-10-12). It means the command is running, NOT that a denial will be seen: only an
# attributed canary denial proves capture.
READY_HEADER = "Filtering the log data"


class LogCollector:
    """`log stream` of sandbox reports, parsed in memory only (never written anywhere). Each
    event keeps the log's SOURCE timestamp (`ts`); receipt time (`t`) is never used to decide
    which process an event belongs to.

    status(): "ready" once the startup header (or any record) has been read, "exited" if the
    command ended or its output closed first, else "starting". Only the status is kept: never
    the header, an error or any raw line."""

    def __init__(self, cmd=None):
        self.cmd = cmd or ["/usr/bin/log", "stream", "--style", "ndjson", "--predicate", 'sender == "Sandbox"']
        self.events: list[dict] = []
        self.lock = threading.Lock()
        self.proc = None
        self.ready = threading.Event()
        self.eof = False

    def start(self) -> None:
        self.proc = subprocess.Popen(self.cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     stdin=subprocess.DEVNULL, text=True)
        threading.Thread(target=self._read, daemon=True).start()

    def status(self) -> str:
        if self.ready.is_set() and not self.eof:
            return "ready"
        if self.eof or self.proc is None or self.proc.poll() is not None:
            return "exited"
        return "starting"

    def _read(self) -> None:
        try:
            self._lines()
        finally:
            self.eof = True

    def _lines(self) -> None:
        for line in self.proc.stdout:
            if not self.ready.is_set() and line.startswith(READY_HEADER):
                self.ready.set()
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict):
                self.ready.set()  # a record also shows the stream is running
            m = LINE.match(rec.get("eventMessage") or "") if isinstance(rec, dict) else None
            if m:
                with self.lock:
                    # "N duplicate reports for …" stands for N more denials of the same kind.
                    self.events.append({"count": int(m.group(1)) if m.group(1) else 1,
                                        "name": m.group(2), "pid": int(m.group(3)), "op": m.group(4),
                                        "target": m.group(5) or "", "ts": source_ts(rec), "t": time.time()})

    def note_pid(self, role: str, pid: int, **info) -> None:  # (fakes use this to address their events)
        pass

    def snapshot(self) -> list[dict]:
        with self.lock:
            return list(self.events)

    def stop(self) -> bool:
        """Stop the command; True only once its exit is confirmed."""
        if self.proc is None:
            return True
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                try:
                    self.proc.wait(5)
                except subprocess.TimeoutExpired:
                    return False
        return True


READY_S = 10.0       # the collector's readiness bound (within the run's budget)
CAPTURE_S = 15.0     # the canary's whole window (within the run's budget)
CANARY_READS = 8     # ONE canary process, a fixed number of denied reads ...
CANARY_GAP = 0.25    # ... this far apart: bounded setup, never a retry of anything
CANARY_TAIL = 0.5    # it lives this long after its last read
CANARY_LATE = 2.0    # reports may arrive this long after the canary exits
# Each read targets its own file, so the reports are distinct (not coalesced as duplicates).
CANARY_CODE = ("import os, sys, time\nd, n, gap, tail = sys.argv[1], int(sys.argv[2]), float(sys.argv[3]), "
               "float(sys.argv[4])\nfor i in range(n):\n    try:\n        open(os.path.join(d, 'r%d' % i)).read()\n"
               "    except OSError:\n        pass\n    time.sleep(gap)\ntime.sleep(tail)\n")


def establish_capture(col, run_dir: str, budget: PR.Budget, cap: dict | None = None) -> dict:
    """Prove capture, filling `cap` as it goes so an interruption
    still leaves an honest record:

    1. the collector reports ready within READY_S (never past the run's budget); a collector
       that exits or never gets ready fails here, before any canary;
    2. ONE canary process makes CANARY_READS denied reads, CANARY_GAP apart;
    3. capture is proven only by a report from THAT process: same pid, a valid start time, op
       file-read-data on one of its own files, and a source timestamp within its life.

    The canary is always reaped and its files removed; both are reported. Returns `cap`, with
    `established` and, on failure, a `failure` category."""
    cap = {} if cap is None else cap
    cap.update(established=False, failure=None, canary_started=False, canary_exit_confirmed=True,
               canary_files_removed=True, attributed_reads=0)
    ready_by = budget.within(READY_S)
    while True:
        st = col.status()
        if st == "ready":
            cap["ready_at"] = T.stamp()
            break
        if st == "exited":
            cap["failure"] = "collector_exited"
            return cap
        if ready_by.expired():
            cap["failure"] = "collector_not_ready"
            return cap
        time.sleep(0.05)
    d = os.path.realpath(os.path.join(run_dir, ".canary"))
    os.mkdir(d, 0o700)
    cap["canary_files_removed"] = False
    names = {os.path.join(d, f"r{i}") for i in range(CANARY_READS)}
    for n in names:
        with open(n, "w") as f:
            f.write("canary\n")
    p = None
    try:
        prof = f'(version 1)(allow default)(deny file-read-data (subpath {json.dumps(d)}))'
        p = subprocess.Popen(["/usr/bin/sandbox-exec", "-p", prof, sys.executable, "-I", "-c", CANARY_CODE, d,
                              str(CANARY_READS), str(CANARY_GAP), str(CANARY_TAIL)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cap["canary_started"], cap["canary_exit_confirmed"] = True, False
        start = process_start_ms(p.pid)
        if not PR.valid_start(start):
            cap["failure"] = "canary_identity_unknown"  # no identity, no evidence
            return cap
        col.note_pid("canary", p.pid, target=d, reads=CANARY_READS, gap=CANARY_GAP)
        wait = budget.within(CAPTURE_S)
        ended = None
        while True:
            if ended is None and p.poll() is not None:
                ended = time.time()
            upper = (ended if ended is not None else time.time()) + 1.0
            seen = {e["target"] for e in col.snapshot()
                    if e["pid"] == p.pid and e["op"] == "file-read-data"
                    and os.path.realpath(e["target"]) in names
                    and PR.valid_start(e.get("ts")) and start / 1000.0 <= e["ts"] <= upper}
            if seen:
                cap.update(established=True, attributed_reads=len(seen))
                return cap
            if col.status() == "exited":
                cap["failure"] = "collector_exited"
                return cap
            if wait.expired() or (ended is not None and time.time() > ended + CANARY_LATE):
                cap["failure"] = "no_attributed_denial"
                return cap
            time.sleep(0.1)
    finally:
        if p is not None:
            cap["canary_exit_confirmed"] = PR.stop_child(proc=p, grace=5.0)
        shutil.rmtree(d, ignore_errors=True)
        cap["canary_files_removed"] = not os.path.exists(d)
        cap["canary_done_at"] = T.stamp()


def capture_evidence(cap: dict, *, started_at: str, deadline: float, elapsed: float, failure,
                     collector_stopped: bool) -> dict:
    """The record for a run that ended in the capture stage: nothing of Claude was started. No
    raw log line, header, error message or path is kept; categories and booleans only."""
    rec = {"probe": "claude-state-path", "started_at": started_at, "claude_version": VERSION,
           "stage": "capture", "outcome": "not_started", "capture": bool(cap.get("established")),
           "failure": "interrupted" if isinstance(failure, KeyboardInterrupt) else
                      "error" if failure is not None else (cap.get("failure") or "unknown"),
           "claude_processes": 0, "prompts": 0, "model_turns": 0,
           "deadline_s": deadline, "elapsed_s": round(elapsed, 2),
           "canary": {"started": bool(cap.get("canary_started")), "reads_planned": CANARY_READS,
                      "attributed_reads": int(cap.get("attributed_reads") or 0)},
           "cleanup": {"canary_exit_confirmed": bool(cap.get("canary_exit_confirmed", True)),
                       "canary_files_removed": bool(cap.get("canary_files_removed", True)),
                       "collector_exit_confirmed": bool(collector_stopped)}}
    if failure is not None:
        rec["error"] = type(failure).__name__  # the class only
    if cap.get("failure") and failure is not None:
        rec["capture_failure"] = cap["failure"]
    return rec


# ------------------------------------------------------------------ classification
def classify(op: str, target: str, ctx: dict) -> tuple[str, str | None, str]:
    """(category, template, area). Only predefined templates are ever returned; anything else is
    ("unknown", None, "unknown"). area: "client" (an observation), "workspace", "temp" or "stop"."""
    if op.startswith("network"):
        m = re.search(r":(\d+)$", target)
        if m:
            port = int(m.group(1))
            return (f"network port {port}", None, "client" if port == 443 else "stop")
        if target == T.RESOLVER:
            return ("network resolver", None, "client")
        return ("network other", None, "stop")
    if not target.startswith("/"):
        return (_target_shape(target), None, "unknown")
    p = os.path.normpath(target)
    home, slug, session = ctx["home"], ctx["slug"], ctx["session"]
    ws, tmp = ctx["ws"], ctx["tmp"]
    if tmp and (p == tmp or p.startswith(tmp + "/")):
        return ("private temp", "{tmp}/<...>", "temp")
    if p == ws or p.startswith(ws + "/"):
        rel = os.path.relpath(p, ws)
        return ("workspace", "{ws}/" + (rel if rel in ("README.md", "hello.txt", ".") else "<other>"), "workspace")
    proj = os.path.join(home, ".claude", "projects")
    mine = os.path.join(proj, slug)
    if p == os.path.join(mine, session + ".jsonl"):
        return ("own transcript", "{home}/.claude/projects/{slug}/{session}.jsonl", "client")
    if p == mine:
        return ("own project folder", "{home}/.claude/projects/{slug}", "client")
    if p.startswith(mine + "/"):
        rest = p[len(mine) + 1:]
        if rest.startswith(session):
            return ("own session item", "{home}/.claude/projects/{slug}/{session}<...>", "client")
        return ("own project folder: other item", "{home}/.claude/projects/{slug}/<other>", "client")
    if p == proj:
        return ("projects folder", "{home}/.claude/projects", "client")
    if p.startswith(proj + "/"):
        return ("another conversation's folder", "{home}/.claude/projects/<other>", "client")
    claude_dir = os.path.join(home, ".claude")
    if p == claude_dir:
        return ("client folder", "{home}/.claude", "client")
    if p.startswith(claude_dir + "/"):
        top, _, rest = p[len(claude_dir) + 1:].partition("/")
        if top in CLIENT_TOPS:
            if session in rest:
                return (f"client {top}: this session", f"{{home}}/.claude/{top}/<...{{session}}...>", "client")
            return (f"client {top}", f"{{home}}/.claude/{top}" + ("/<...>" if rest else ""), "client")
        return ("unknown: unlisted client folder item", None, "unknown")
    cfg = os.path.join(home, ".claude.json")
    if p == cfg:
        return ("client config file", "{home}/.claude.json", "client")
    if p.startswith(cfg + "."):
        kind = "backup" if ".backup" in p else "lock" if p.endswith(".lock") else "temp" if ".tmp" in p else "other"
        return (f"client config {kind}", "{home}/.claude.json." + kind, "client")
    lib = os.path.join(home, "Library")
    for sub, label in (("Caches", "client cache"), ("Application Support", "client support"),
                       ("Preferences", "client preferences")):
        base = os.path.join(lib, sub)
        if p.startswith(base + "/"):
            name, _, rest = p[len(base) + 1:].partition("/")
            if SAFE.match(name) and re.search(r"claude|anthropic", name, re.I):
                return (label, f"{{home}}/Library/{sub}/{name}" + ("/<...>" if rest else ""), "client")
            return (f"{label}: not the client's", f"{{home}}/Library/{sub}/<other>", "stop")
    if p.startswith(os.path.join(lib, "Keychains") + "/"):
        return ("keychain files", "{home}/Library/Keychains/<...>", "stop")
    if p == home or p.startswith(home + "/"):
        return _home_diagnostic(p, home, ws)
    return _system_diagnostic(p)


# Diagnostic categories (after live probe #2). They NAME what was denied more
# precisely; they never allow anything: every one of them is still a stop, never a proposal.
# Each fixed name is backed by static evidence (collected offline from the installed client): the installed
# client's own runtime lists, or the system libraries.
HOME_RUNTIME = {  # exact home-relative names -> category
    ".CFUserTextEncoding": "runtime: text encoding file",   # system libraries (dyld cache)
    ".gitconfig": "client: git config", ".config/git": "client: git config",
    ".npmrc": "client: npm config", ".npm/_logs": "client: npm logs",
    ".zshrc": "client: shell startup", ".bashrc": "client: shell startup",
    ".config/claude": "client: XDG config", ".mcp.json": "client: MCP config",
}
# Project-config names the client looks for in the workspace's enclosing folders.
ANCESTOR_ITEMS = ("CLAUDE.md", "CLAUDE.local.md", ".mcp.json", ".claude", ".git")
# The client's own runtime device list (static: /dev/null … /dev/autofs_nowait).
DEVICES = ("null", "zero", "random", "urandom", "stdin", "stdout", "stderr", "tty", "dtracehelper",
           "autofs_nowait")


def _target_shape(target: str) -> str:
    """Why a non-path target couldn't be classified, by its SHAPE only (never its text): absent,
    redacted by the log, or some other format the parser doesn't understand."""
    t = target.strip()
    if not t:
        return "unknown: no target"
    if "<private>" in t:
        return "unknown: redacted target"
    return "unknown: unsupported target format"


def _home_diagnostic(p: str, home: str, ws: str) -> tuple[str, str | None, str]:
    rel = os.path.relpath(p, home)
    for name, cat in HOME_RUNTIME.items():
        if rel == name or rel.startswith(name + "/"):
            return (cat, "{home}/" + name + ("/<...>" if rel != name else ""), "stop")
    # The workspace's enclosing folders inside the home folder (deepest first; home included).
    anc = os.path.dirname(ws)
    while anc == home or anc.startswith(home + "/"):
        if p == anc:
            return ("workspace ancestor folder", "{ancestor}", "stop")
        if p.startswith(anc + "/"):
            first, _, more = p[len(anc) + 1:].partition("/")
            if first in ANCESTOR_ITEMS and not (anc == home and first == ".claude"):
                return (f"workspace ancestor: {first}", "{ancestor}/" + first + ("/<...>" if more else ""), "stop")
            break  # the deepest enclosing folder decides; anything else there is "home: other"
        if anc == home:
            break
        anc = os.path.dirname(anc)
    return ("home: other", "{home}/<other>", "stop")


def _system_diagnostic(p: str) -> tuple[str, str | None, str]:
    for tmp in ("/private/tmp/claude", "/tmp/claude"):  # the client's default temp folder
        if p == tmp or p.startswith(tmp + "/"):
            return ("client default temp", "/private/tmp/claude" + ("/<...>" if p != tmp else ""), "stop")
    for tz in ("/private/var/db/timezone", "/var/db/timezone"):  # runtime time-zone data
        if p == tz or p.startswith(tz + "/"):
            return ("runtime: time zone data", "/private/var/db/timezone/<...>", "stop")
    if p.startswith("/dev/"):
        name = p[len("/dev/"):]
        if name in DEVICES:
            return (f"device: {name}", p, "stop")
        if name.startswith("fd/"):
            return ("device: fd", "/dev/fd/<n>", "stop")
        if re.fullmatch(r"ttys\d{1,4}", name):
            return ("device: terminal", "/dev/ttys<n>", "stop")
        return ("device: other", "/dev/<other>", "stop")
    for root in ("/private/var/folders", "/private/var/db", "/private/etc", "/Library", "/System", "/usr",
                 "/private/tmp", "/Users", "/Volumes", "/opt", "/tmp", "/var", "/etc"):
        if p == root or p.startswith(root + "/"):
            return (f"system: {root}", root + "/<...>", "stop")
    return ("unknown: path outside the classifier's roots", None, "unknown")


# ------------------------------------------------------------------ path roles (diagnostic)
# A target is reduced IN MEMORY to one predefined role (PR.PATH_ROLES) from this
# run's own context; the path itself is never kept. A role names where an error or denial was,
# it never becomes an allowance.
_ALIASES = (("/tmp", "/private/tmp"), ("/var", "/private/var"), ("/etc", "/private/etc"))
TEMP_ROOT = os.path.realpath(tempfile.gettempdir())
# The installed client's own default temp folders on macOS when its temp variables are unset:
# "<CLAUDE_CODE_TMPDIR or /tmp>/claude-<uid>", and "/tmp/claude" for its sandboxed commands.
CLIENT_DEFAULT_TEMP = (f"/private/tmp/claude-{os.getuid()}", "/private/tmp/claude")


def path_role(path: str, ctx: dict) -> str:
    if not isinstance(path, str) or not path:
        return "unknown"
    home, ws = ctx["home"], ctx["ws"]
    if path == "~" or path.startswith("~/"):
        path = home + path[1:]
    if not path.startswith("/"):
        return "unknown"
    p = os.path.normpath(path)
    for short, real in _ALIASES:
        if p == short or p.startswith(short + "/"):
            p = real + p[len(short):]
    claude = os.path.join(home, ".claude")
    run = os.path.dirname(ws)
    if p.startswith("/dev/"):
        return "device"
    if p == os.path.join(home, ".claude.json"):
        return "client_config"
    if p == os.path.join(claude, "settings.json"):
        return "client_settings"
    if p == os.path.join(claude, "plugins") or p.startswith(os.path.join(claude, "plugins") + "/"):
        return "plugins"
    if p == claude:
        return "client_state_dir"
    if p == ws or p.startswith(ws + "/"):
        return "workspace"
    if p == home:  # the exact home literal wins over a structural role it may also fill
        return "home_root"
    if p == run:
        return "run_folder"
    if p == os.path.dirname(run):
        return "relay_runs_folder"
    if ws.startswith(p.rstrip("/") + "/"):
        return "workspace_ancestor_other"
    tmp = ctx.get("tmp")  # THIS run's private folder only (canonical); never a name heuristic
    if tmp and (p == tmp or p.startswith(tmp + "/")):
        return "run_private_temp"
    if any(p == d or p.startswith(d + "/") for d in CLIENT_DEFAULT_TEMP):
        return "client_default_temp"
    if p in ("/private/tmp", "/private/var/folders", TEMP_ROOT):
        return "system_temp_root"
    if p.startswith("/private/tmp/") or p.startswith("/private/var/folders/"):
        return "system_temp_other"
    return "other"


def turn_tmp(hold: dict) -> str | None:
    """This turn's private folder, canonical, from its launch hold (memoized: the folder is gone
    by the time late lines are mapped, and a removed path no longer resolves the same way)."""
    if "_tmp_real" not in hold and hold.get("tmp"):
        hold["_tmp_real"] = os.path.realpath(hold["tmp"])
    return hold.get("_tmp_real")


def op_class(op: str) -> str:
    """A sandbox operation's class: what a syscall would need."""
    if op == "file-read-metadata":
        return "read_metadata"
    if op.startswith("file-read"):
        return "read_data"
    if op.startswith("file-write"):
        return "write"
    return "other"


# The OS operation classes a syscall can be denied under (macOS sandbox).
SYSCALL_OPS = {"open": {"read_data", "write"}, "scandir": {"read_data"}, "opendir": {"read_data"},
               "readdir": {"read_data"}, "readlink": {"read_metadata"}, "realpath": {"read_metadata"},
               "access": {"read_metadata"}, "stat": {"read_metadata"}, "lstat": {"read_metadata"},
               "mkdir": {"write"}, "write": {"write"}, "unlink": {"write"}, "rename": {"write"},
               "rmdir": {"write"}, "copyfile": {"read_data", "write"}}


def correlate(structured: list[dict], role_ops: dict) -> list[dict]:
    """Each structured error with `correlation`: "matched" only when an attributed OS denial in
    the same turn names the same KNOWN role with an operation that syscall needs; otherwise
    "unknown". A match is diagnostic evidence, never permission to allow the target."""
    out = []
    for s in structured:
        ops = SYSCALL_OPS.get(s["syscall"], set())
        hit = s["role"] not in ("other", "unknown") and any((s["role"], o) in role_ops for o in ops)
        out.append(dict(s, correlation="matched" if hit else "unknown"))
    return out


# ------------------------------------------------------------------ process identity
def descendants(root: int) -> set[int]:
    try:
        out = subprocess.run(["/bin/ps", "-A", "-o", "pid=,ppid="], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return set()
    kids: dict[int, list[int]] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            kids.setdefault(int(parts[1]), []).append(int(parts[0]))
    seen, todo = set(), [root]
    while todo:
        p = todo.pop()
        for c in kids.get(p, []):
            if c not in seen:
                seen.add(c)
                todo.append(c)
    return seen


CLIENT_NAMES = re.compile(r"claude|^rg$|^2\.\d+\.\d+$|node", re.I)
END_GRACE_S = 2.0  # how long after a process's observed exit its own reports may still be stamped


class Attribution:
    """The probe's own processes, each a positive identity (pid + valid start time) with a
    lifetime. An event belongs to exactly one identity whose pid matches and whose lifetime
    contains the event's SOURCE timestamp; anything else isn't evidence."""

    def __init__(self):
        self.ids: list[dict] = []
        self.unknown_identity = 0

    def add(self, pid: int, start_ms, turn: int, role: str) -> bool:
        if not PR.valid_start(start_ms):
            self.unknown_identity += 1
            return False
        self.ids.append({"pid": pid, "start_s": start_ms / 1000.0, "end": None, "turn": turn, "role": role})
        return True

    def end(self, pid: int, at: float) -> None:
        for i in reversed(self.ids):
            if i["pid"] == pid and i["end"] is None:
                i["end"] = at
                return

    def has_pid(self, pid: int) -> bool:
        return any(i["pid"] == pid for i in self.ids)

    def owner(self, pid: int, ts) -> dict | None:
        if not PR.valid_start(ts):
            return None
        hits = [i for i in self.ids if i["pid"] == pid and i["start_s"] <= ts
                and (i["end"] is None or ts <= i["end"] + END_GRACE_S)]
        return hits[0] if len(hits) == 1 else None


# ------------------------------------------------------------------ process ledger
# A client-like denial from a pid this probe hasn't attributed is judged from
# privacy-safe process identities: pid, start time (ps lstart, 1 s resolution, the same source as
# process_start_ms) and parent pid/start, from bounded `ps` snapshots. Never names, argv, cwd,
# environment or paths. Verdicts:
#   proven_owned   a verified parent chain reaches one of THIS run's clients (exact pid+start)
#   proven_foreign the process itself, or a verified ancestor, was seen in the baseline taken
#                  before ANY probe process started (so it can't descend from our client), and
#                  the event falls inside the identity's observed lifetime
#   unknown        anything else: no identity, missing start, pid outside a known lifetime,
#                  reparenting, launchd/orphan parent, an unverified link, a child of the probe
#                  itself. Unknown still stops the run; no process is ever signalled for this.
VERDICTS = ("proven_owned", "proven_foreign", "pre_client", "unknown")
# Launch boundary, startup-only: an event still unknown after the ancestry check
# whose SOURCE time is clearly before the stamp taken immediately before the client's Popen can't
# come from this client or its descendants. It is left out of the client's safety and evidence as
# "pre_client" (setup), with its record kept. Within the margin, at or after the bound, without a
# usable time, or once the wall clock has moved against the monotonic clock: unknown, and it stops.
PRE_CLIENT_MARGIN_S = 0.05  # source times have microsecond resolution; this is far wider
CLOCK_TOL_S = 0.05          # allowed change of (wall - monotonic) between any two readings
NAME_TYPES = ("claude", "node", "rg", "other_client_like")
PS_TIMEOUT_S = 2.0      # one snapshot's bound: metadata never adds unbounded waiting
LEDGER_MAX_IDS = 20000  # identities kept in memory (a process table is far smaller)
LEDGER_DEPTH = 64       # ancestry walk bound
ATTR_RECORDS_MAX = 32   # persisted record groups; more are counted as dropped


def name_type(name) -> str:
    """The fixed type of a client-like process name (the name itself is never kept)."""
    n = os.path.basename(name or "") if isinstance(name, str) else ""
    if re.search(r"claude", n, re.I) or re.fullmatch(r"\d+\.\d+\.\d+", n):
        return "claude"
    if re.search(r"node", n, re.I):
        return "node"
    if n == "rg":
        return "rg"
    return "other_client_like"


def ps_table() -> dict | None:
    """{pid: {"ppid", "start_ms"}} for every process, or None if ps failed (bounded)."""
    try:
        out = subprocess.run(["/bin/ps", "-A", "-o", "pid=,ppid=,lstart="], capture_output=True, text=True,
                             timeout=PS_TIMEOUT_S).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    rows = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 7 or not parts[0].isdigit() or not parts[1].isdigit():
            continue
        try:
            start = time.mktime(time.strptime(" ".join(parts[2:7]), "%a %b %d %H:%M:%S %Y")) * 1000
        except ValueError:
            start = None
        rows[int(parts[0])] = {"ppid": int(parts[1]), "start_ms": start}
    return rows


def _ms(v) -> int | None:
    return int(round(v)) if PR.valid_start(v) else None


class ProcessLedger:
    """Process identities seen in bounded snapshots, each with the last time it was seen alive
    (the snapshot's START: a lower bound), its parent identity and whether it was in the baseline."""

    def __init__(self, table=ps_table, clock=time.time, self_pid: int | None = None):
        self.table, self.clock = table, clock
        self.self_pid = os.getpid() if self_pid is None else self_pid
        self.ids: dict[tuple, dict] = {}
        self.nostart: set[int] = set()
        self.baseline = False
        self.snapshots = self.errors = 0

    def snapshot(self, baseline: bool = False) -> bool:
        t0 = self.clock()
        rows = self.table()
        if rows is None:
            self.errors += 1
            return False
        for pid, r in rows.items():
            start = _ms(r.get("start_ms"))
            if start is None:
                self.nostart.add(pid)
                continue
            parent = rows.get(r["ppid"])
            pstart = _ms(parent.get("start_ms")) if parent else None
            i = self.ids.get((pid, start))
            if i is None:
                if len(self.ids) >= LEDGER_MAX_IDS:
                    self.errors += 1
                    continue
                self.ids[(pid, start)] = {"last": t0, "ppid": r["ppid"], "pstart": pstart, "baseline": baseline}
                continue
            i["last"] = max(i["last"], t0)
            if (i["ppid"], i["pstart"]) != (r["ppid"], pstart):
                if i["ppid"] == r["ppid"] and i["pstart"] is None:
                    i["pstart"] = pstart  # a parent start learned later, same parent pid
                elif not (i["ppid"] == r["ppid"] and pstart is None):
                    i["reparented"] = True  # its parent changed: ancestry is ambiguous
        self.snapshots += 1
        self.baseline = self.baseline or baseline
        return True

    def identify(self, pid, ts) -> tuple[tuple | None, str]:
        hits = [k for k, i in self.ids.items() if k[0] == pid and k[1] / 1000.0 <= ts <= i["last"]]
        if len(hits) == 1:
            return hits[0], "identified"
        if hits:
            return None, "pid_reuse_ambiguous"
        if any(k[0] == pid for k in self.ids):
            return None, "outside_known_lifetime"  # a stale or reused pid
        return None, "missing_start" if pid in self.nostart else "no_identity"

    def judge(self, pid, ts, clients: dict) -> tuple[str, str, tuple | None, int | None]:
        """(verdict, reason, identity, turn). `clients`: {(pid, start_ms): turn} for this run."""
        k, why = self.identify(pid, ts)
        if k is None:
            return "unknown", why, None, None
        node = k
        for _ in range(LEDGER_DEPTH):
            if node in clients:
                return "proven_owned", "client_ancestry", k, clients[node]
            i = self.ids[node]
            if node[0] == self.self_pid:
                return "unknown", "probe_child", k, None
            if i.get("reparented"):  # before the baseline shortcut: a changed parent is never proof
                return "unknown", "reparented", k, None
            if i["baseline"]:
                return "proven_foreign", "preexisting" if node == k else "preexisting_ancestor", k, None
            if i["ppid"] <= 1:
                return "unknown", "orphan_or_launchd_parent", k, None
            parent = (i["ppid"], i["pstart"])
            if i["pstart"] is None or parent not in self.ids or parent[1] > node[1]:
                return "unknown", "parent_unverified", k, None
            node = parent
        return "unknown", "ancestry_too_deep", k, None


class Scanner:
    """Reads the collector's events once each, crediting each denial to the turn of the
    process that caused it (so a delayed report lands on its own turn)."""

    def __init__(self, col, attr: Attribution, run_wall_start: float, diagnostic: bool = False,
                 ledger: ProcessLedger | None = None):
        self.col, self.attr, self.run_wall_start = col, attr, run_wall_start
        self.diagnostic = diagnostic
        self.ledger = ledger  # None: every unattributed client-like denial is unknown (as before)
        self.verdicts = {v: 0 for v in VERDICTS}
        self.records: dict[tuple, dict] = {}
        self.records_dropped = 0
        # Startup-only launch boundary: T.stamp() from immediately before the client's Popen, and
        # every paired reading so far (run start, before and after spawn); a clock change is sticky.
        self.launch_bound: dict | None = None
        self.clock_marks: list[dict] = []
        self.clock_ok = True
        self.seen = 0
        self.denials: dict[int, dict] = {}
        # Diagnostic-only: attributed FILE denials outside the client's areas, recorded instead
        # of stopping (still denied). Everything else that would stop still does.
        self.observed: dict[int, dict] = {}
        self.unattributed: dict[int, int] = {}
        # Diagnostic: (path role, operation class) of each attributed file denial, per turn; the
        # target is mapped in memory and dropped.
        self.role_ops: dict[int, dict] = {}
        self.stops: list[str] = []
        self.counts = {"outside_lifetime": 0, "missing_timestamp": 0}

    def stop(self, why: str) -> None:
        if why not in self.stops:
            self.stops.append(why)

    def judge(self, e: dict, ts: float, current: int) -> dict | None:
        """An unattributed client-like denial: the owner identity once proven owned (adopted as
        a descendant of that turn's client), else None, recorded: proven foreign is left out of
        this run's evidence; unknown stops the run, exactly as before."""
        clients = {(i["pid"], int(round(i["start_s"] * 1000))): i["turn"]
                   for i in self.attr.ids if i["role"] == "client"}
        verdict, why, k, turn = ("unknown", "no_ledger", None, None) if self.ledger is None else \
            self.ledger.judge(e["pid"], ts, clients)
        if self.ledger is not None and why in ("no_identity", "outside_known_lifetime", "missing_start") \
                and not getattr(self, "_looked", False):
            self._looked = True
            if self.ledger.snapshot():  # event-driven lookup while it may still exist
                verdict, why, k, turn = self.ledger.judge(e["pid"], ts, clients)
        timing = None
        if verdict == "unknown" and self.launch_bound is not None:
            early, timing = self.before_launch(ts)
            if early:
                verdict, why = "pre_client", "before_client_spawn"
        self.verdicts[verdict] += e["count"]
        i = self.ledger.ids.get(k) if (k and self.ledger is not None) else None
        key = (verdict, why, name_type(e["name"]), e["pid"], k[1] if k else None,
               i["ppid"] if i else None, i["pstart"] if i else None, turn, timing)
        r = self.records.get(key)
        if r is None:
            if len(self.records) >= ATTR_RECORDS_MAX:
                self.records_dropped += 1
            else:
                self.records[key] = r = {"verdict": verdict, "reason": why, "name_type": key[2], "pid": key[3],
                                         "start_ms": key[4], "parent_pid": key[5], "parent_start_ms": key[6],
                                         "turn": turn, "count": 0, "first_event_ts": round(ts, 3),
                                         "last_event_ts": round(ts, 3)}
                if timing is not None:
                    r["timing"] = timing
        if r is not None:
            r["count"] += e["count"]
            r["first_event_ts"] = min(r["first_event_ts"], round(ts, 3))
            r["last_event_ts"] = max(r["last_event_ts"], round(ts, 3))
        if verdict == "proven_owned":
            if self.attr.owner(e["pid"], ts) is None:
                self.attr.add(e["pid"], k[1], turn, "descendant")
            who = self.attr.owner(e["pid"], ts)
            if who is not None:
                return who
            self.verdicts[verdict] -= e["count"]
            self.verdicts["unknown"] += e["count"]  # (adoption didn't make it unambiguous)
        if verdict in ("proven_foreign", "pre_client"):
            return None  # not this client's: left out of its evidence, and never signalled
        self.unattributed[current] = self.unattributed.get(current, 0) + e["count"]
        self.stop("a client-like process this probe didn't start was denied")
        return None

    def untimed(self, e: dict, current: int) -> None:
        """A client-like report from an unattributed pid with no usable source time (Codex
        2026-10-13): never placed before the run or the launch; unknown, and it stops."""
        self.verdicts["unknown"] += e["count"]
        key = ("unknown", "missing_time", name_type(e["name"]), e["pid"], None, None, None, None, "missing_time")
        r = self.records.get(key)
        if r is None:
            if len(self.records) >= ATTR_RECORDS_MAX:
                self.records_dropped += 1
            else:
                self.records[key] = r = {"verdict": "unknown", "reason": "missing_time", "name_type": key[2],
                                         "pid": e["pid"], "start_ms": None, "parent_pid": None,
                                         "parent_start_ms": None, "turn": None, "count": 0,
                                         "first_event_ts": None, "last_event_ts": None, "timing": "missing_time"}
        if r is not None:
            r["count"] += e["count"]
        self.unattributed[current] = self.unattributed.get(current, 0) + e["count"]
        self.stop("a client-like process this probe didn't start was denied")

    def before_launch(self, ts) -> tuple[bool, str]:
        """(clearly before the launch bound?, timing reason). Decided on the raw source time, never
        a rounded one, and only while every paired clock reading agrees with the bound's."""
        b = self.launch_bound
        base = b["wall"] - b["mono"]
        for m in [*self.clock_marks, T.stamp()]:
            if not (PR.valid_start(m.get("wall")) and isinstance(m.get("mono"), (int, float))) or \
                    abs((m["wall"] - m["mono"]) - base) > CLOCK_TOL_S:
                self.clock_ok = False
        if not self.clock_ok:
            return False, "clock_inconsistent"
        if not PR.valid_start(ts):
            return False, "missing_time"
        if ts < b["wall"] - PRE_CLIENT_MARGIN_S:
            return True, "before_bound"
        return False, "within_margin" if ts < b["wall"] else "at_or_after_bound"

    def attribution_record(self) -> dict:
        led = self.ledger
        return {"verdicts": dict(self.verdicts), "records": list(self.records.values()),
                "launch_bound_used": self.launch_bound is not None, "clock_consistent": self.clock_ok,
                "records_dropped": self.records_dropped,
                "baseline": bool(led and led.baseline), "snapshots": led.snapshots if led else 0,
                "snapshot_errors": led.errors if led else 0}

    def scan(self, ctxs: dict[int, dict], current: int) -> None:
        evs = self.col.snapshot()
        self._looked = False  # at most one event-driven ps lookup per scan
        for e in evs[self.seen:]:
            self.seen += 1
            ts = e.get("ts")
            if not PR.valid_start(ts):
                if self.attr.has_pid(e["pid"]):
                    self.counts["missing_timestamp"] += e["count"]
                    self.stop("a report for this probe's process had no usable source timestamp")
                elif CLIENT_NAMES.search(e["name"]):
                    self.untimed(e, current)  # can't be placed before or after anything: unknown
                continue
            who = self.attr.owner(e["pid"], ts)
            if who is None:
                if self.attr.has_pid(e["pid"]):
                    self.counts["outside_lifetime"] += e["count"]  # an older or later process with that pid
                elif ts >= self.run_wall_start and CLIENT_NAMES.search(e["name"]):
                    who = self.judge(e, ts, current)
                if who is None:
                    continue
            cat, tpl, area = classify(e["op"], e["target"], ctxs[who["turn"]])
            if self.diagnostic and e["op"].startswith("file-"):
                ro = self.role_ops.setdefault(who["turn"], {})
                k = (path_role(e["target"], ctxs[who["turn"]]), op_class(e["op"]))
                ro[k] = ro.get(k, 0) + e["count"]
            d = self.denials.setdefault(who["turn"], {})
            key = (cat, tpl, e["op"])
            d[key] = d.get(key, 0) + e["count"]
            if area in ("unknown", "stop"):
                if self.diagnostic and e["op"].startswith("file-"):
                    o = self.observed.setdefault(who["turn"], {})
                    o[key] = o.get(key, 0) + e["count"]
                else:
                    self.stop(f"denial outside the allowed observation areas: {cat}")


def track_descendants(conn, attr: Attribution, scanner: Scanner, n: int, known: set[int]) -> None:
    """New descendants get a positive identity, or the run stops; vanished ones get an end."""
    if conn.proc.poll() is not None:
        return
    now_kids = descendants(conn.pid)
    for pid in now_kids - known:
        if not attr.add(pid, process_start_ms(pid), n, "descendant"):
            scanner.stop("a client process's start couldn't be identified")
    for pid in known - now_kids - {conn.pid}:
        attr.end(pid, time.time())
    known.clear()
    known.update(now_kids | {conn.pid})


def startup_verified(conn, ctx: dict) -> bool:
    """The client's own init record arrived, passed the policy check and named this conversation
    and folder. Startup evidence only: never a successful coding turn."""
    ident = conn.confirmed_identity or {}
    return bool(conn.init_summary is not None and conn.policy_ok
                and ident.get("client_session_id") == ctx["session"]
                and os.path.realpath(ident.get("cwd") or "") == ctx["ws"])


def diagnostic_safety(conn, events: list, scanner: Scanner) -> None:
    """Diagnostic hard stops the adapter may already hold: a recorded tool use (its live audit, or
    one handed over with a result or error) and an identity or init-policy failure (an error event
    or the connection's own recorded identity error)."""
    if getattr(conn, "audit", None) or any((e.data or {}).get("tool_audit") for e in events):
        scanner.stop("a tool was used in the diagnostic turn")
    if getattr(conn, "identity_error", None) or any(
            e.type == "error" and (e.data or {}).get(k) for e in events
            for k in ("identity_mismatch", "policy_mismatch")):
        scanner.stop("client identity or init policy mismatch")


def run_turn(adapter, col, ctx: dict, text: str, *, resume: bool, record: dict, budget: PR.Budget,
             attr: Attribution, scanner: Scanner, ctxs: dict, n: int, live: list, clock,
             holds: list | None = None, diagnostic: bool = False,
             startup_only: bool = False, phases: dict | None = None) -> tuple[dict, object]:
    """One turn. The turn's budget starts HERE, before launch and submission, and never runs past
    the run's. The live client is appended to `live` the moment it exists, so the caller owns it
    through every error path.

    Launch ownership: a `hold` is appended to `holds` BEFORE the adapter
    is called; the sandbox fills it with the temp folder and the child's handle as each comes into
    being. It moves to `live` only once the connection is returned; until then the caller's
    `release_holds` stops whatever it owns, so a failure or interruption between the child's
    start and the connection's return can't leave a writer unowned."""
    diagnostic = diagnostic or startup_only  # startup-only is diagnostic, with no prompt at all
    out: dict = {"mode": "startup" if startup_only else "resume" if resume else "fresh", "outcome": None,
                 "turn_budget_s": TURN_S}
    t0 = clock()
    turn_end = min(t0 + TURN_S, budget.end)  # anchored to the turn's own start
    hold: dict = {}
    holds = [] if holds is None else holds
    holds.append(hold)
    # The stderr drain starts inside launch, before ctx["tmp"] names this turn's folder (it may
    # still name an earlier turn's), so its roles use THIS turn's folder from the hold.
    sink = PR.StderrDiagnostics(role_of=lambda p: path_role(p, dict(ctx, tmp=turn_tmp(hold)))) \
        if diagnostic and not resume else None
    if sink is not None:
        out["_stderr_sink"] = sink  # (popped by main once the client's cleanup is done)
    try:
        try:
            if resume:
                conn = adapter.resume(ctx["ws"], ctx["session"], {"writer_policy": record}, hold=hold)
            else:
                conn = adapter.launch(ctx["ws"], {"writer_policy": record, "session_id": ctx["session"],
                                                  **({"stderr_sink": sink} if sink is not None else {})},
                                      hold=hold)
        finally:  # the launch bounds, truthfully, whether the launch succeeded, failed or was interrupted
            for k in ("spawn_before", "spawn_after"):
                if hold.get(k) is not None and phases is not None:
                    phases.setdefault(f"client_{k}", hold[k])
            out["launch_bounds"] = {k: hold.get(k) for k in ("spawn_before", "spawn_after")}
            if startup_only and hold.get("spawn_before") is not None and scanner.launch_bound is None:
                scanner.launch_bound = hold["spawn_before"]  # (startup-only: one client, its earliest birth)
                scanner.clock_marks += [hold["spawn_before"]] + ([hold["spawn_after"]] if hold.get("spawn_after") else [])
    except RelayError as e:
        out.update(outcome="not_started", error_class=type(e).__name__, submitted=False,
                   spawned=bool(hold.get("spawning")))
        return out, None  # the hold stays with the caller, who releases and reports it
    live.append(conn)
    holds.remove(hold)  # ownership transferred: the connection now holds the child and folder
    ctx["tmp"] = os.path.realpath(conn.tmp) if conn.tmp else None
    ctxs[n] = dict(ctx)
    col.note_pid("client", conn.pid, ctx=ctx)
    out["policy_evidence"] = {k: v for k, v in (getattr(conn, "policy_evidence", None) or {}).items() if k != "tmp"}
    if not attr.add(conn.pid, process_start_ms(conn.pid), n, "client"):
        out.update(outcome="identity_unknown", submitted=False, policy_ok=False, identity_ok=False)
        scanner.stop("the client's start couldn't be identified; nothing was sent")
        return out, conn
    known: set[int] = {conn.pid}
    events = []
    end = turn_end
    if startup_only:  # NOTHING is written to the client's input; it stays open until cleanup
        out["submitted"], accepted = False, False
        end = min(turn_end, clock() + STARTUP_S)
        out["window_s"] = STARTUP_S
    else:
        out["submitted"] = True
        if diagnostic:  # the window runs from submission, inside the turn's and the run's budgets
            end = min(turn_end, clock() + DIAG_S)
            out["window_s"] = DIAG_S
        try:
            # Diagnostic: submit doesn't wait out the window for the replay (nothing is scanned while
            # it waits); the one prompt stays pending and its acceptance arrives through poll().
            conn.submit(text, f"probe-{out['mode']}",
                        accept_timeout=min(DIAG_ACCEPT_S, max(0.1, end - clock())) if diagnostic
                        else max(0.1, end - clock()))
            accepted = True
        except RelayError as e:
            out["submit_error"] = type(e).__name__
            accepted = False
    while True:
        now = clock()  # one read decides both "stop now?" and the recorded elapsed time
        ending = now >= end or bool(scanner.stops) or \
            any(e.type in ("turn_completed", "error", "disconnected") for e in events) or \
            (diagnostic and startup_verified(conn, ctx))
        if ending and diagnostic and not scanner.stops:
            # Before ANY normal diagnostic exit: take what the adapter already holds (events kept
            # while submit waited, lines already queued; no waiting) and run the safety check on
            # it, so a recorded tool use or identity error can't be outrun by the exit.
            events += conn.poll(0.0)
            diagnostic_safety(conn, events, scanner)
        if ending:
            break
        events += conn.poll(min(0.2, max(0.01, end - now)))
        track_descendants(conn, attr, scanner, n, known)
        if scanner.ledger is not None:
            scanner.ledger.snapshot()  # identities and parents while they exist (bounded)
        scanner.scan(ctxs, n)
        if diagnostic:
            diagnostic_safety(conn, events, scanner)
    out["elapsed_s"] = round(now - t0, 2)
    scanner.scan(ctxs, n)
    done = next((e for e in events if e.type == "turn_completed"), None)
    errs = [e for e in events if e.type == "error"]
    out["outcome"] = ("stopped" if scanner.stops else "completed" if done else
                      "policy_mismatch" if any(e.data.get("policy_mismatch") for e in errs) else
                      "error" if errs else "disconnected" if any(e.type == "disconnected" for e in events)
                      else "timed_out")
    out["init"] = conn.init_summary
    out["trace"] = conn.trace[:200]
    out["policy_ok"] = bool(conn.policy_ok)
    ident = conn.confirmed_identity or {}
    out["identity_ok"] = ident.get("client_session_id") == ctx["session"] and \
        os.path.realpath(ident.get("cwd") or "") == ctx["ws"]
    out["reply_has_probe_ok"] = bool(done and "PROBE-OK" in (done.text or ""))
    out["tool_audit"] = (done.data.get("tool_audit") if done else None) or []
    out["permission_denials"] = sum(1 for a in out["tool_audit"] if a.get("kind") == "denied")
    if diagnostic:
        # The process status BEFORE any cleanup: at an observed EOF a natural exit gets a short,
        # bounded wait (no signal is sent); otherwise whatever it is right now.
        eof = bool(conn.eof) or any(e.type == "disconnected" for e in events)
        status = conn.proc.poll()
        if status is None and eof:
            try:
                status = conn.proc.wait(max(0.0, min(DIAG_EXIT_WAIT_S, budget.end - clock())))
            except subprocess.TimeoutExpired:
                status = None
        out["exit"] = {"eof_observed": eof, "status_before_cleanup": status,
                       "adapter_terminated": bool(conn.policy_error)}
        out["prompt_accepted"] = accepted or any(e.type == "accepted" for e in events)
        if out["prompt_accepted"]:
            out.pop("submit_error", None)  # the short wait expired, but the replay came later
        out["startup_verified"] = startup_verified(conn, ctx)
        out["termination"] = ("hard_stop" if scanner.stops else "turn_completed" if done
                              else "init_verified" if out["startup_verified"] else "client_error" if errs
                              else "eof" if any(e.type == "disconnected" for e in events)
                              else "window_elapsed" if now >= end and end < turn_end and end < budget.end
                              else "deadline")
        if out["termination"] == "init_verified" and out["outcome"] == "timed_out":
            out["outcome"] = "init_observed"  # ended by the window rule, not a timeout
        if startup_only and out["termination"] == "window_elapsed":
            out["outcome"] = "waiting"  # still running, with no input: nothing more is learned
    return out, conn


def close_conn(conn, live: list, attr: Attribution, scanner: Scanner | None = None, ctxs=None, n: int = 0,
               exit_record: dict | None = None) -> dict:
    """Stop one client: close, confirm exit (kill if needed), remove its private temp folder. Each
    step is bounded; the result says honestly what was confirmed.

    `exit_record` (diagnostic): completed with what the PROBE did here and the final reaped
    status, and the exit's cause; the stderr drain is finished only once the exit is confirmed."""
    if conn is None:
        return {"exit_confirmed": True, "tmp_removed": True}
    sent: list[str] = []
    if exit_record is not None:  # record each signal the probe ACTUALLY sends (close may terminate)
        proc = conn.proc
        for name, sig in (("terminate", "SIGTERM"), ("kill", "SIGKILL")):
            def send(real=getattr(proc, name), sig=sig):
                if proc.poll() is None and sig not in sent:
                    sent.append(sig)
                return real()
            setattr(proc, name, send)
    try:
        conn.close()
    except Exception:  # noqa: BLE001 - the process handle below still decides
        pass
    confirmed = PR.stop_child(proc=conn.proc, grace=5.0)
    sink = getattr(conn, "stderr_diag", None)
    if sink is not None and confirmed:
        sink.finish()
    if exit_record is not None:
        before = exit_record.get("status_before_cleanup")
        exit_record.update(input_closed_by_probe=True, probe_signals=list(sent),
                           final_status=conn.proc.returncode if confirmed else None)
        exit_record["cause"] = ("probe_termination" if exit_record.get("adapter_terminated") or
                                (before is None and sent) else
                                "natural_exit" if before is not None else "unknown")
    attr.end(conn.pid, time.time())
    if conn.tmp and confirmed:
        T.cleanup_tmp(conn.tmp)
    if conn in live:
        live.remove(conn)
    if scanner is not None:
        time.sleep(0.5)  # reports still in flight from the exit
        scanner.scan(ctxs, n)
    return {"exit_confirmed": bool(confirmed), "tmp_removed": not conn.tmp or not os.path.exists(conn.tmp)}


def release_holds(holds: list) -> list[dict]:
    """Stop and clean up every launch hold not transferred to a connection (see run_turn)."""
    out = []
    for hold in list(holds):
        r = T.release_hold(hold)
        r["spawned"] = bool(hold.get("spawning"))
        out.append(r)
        holds.remove(hold)
    return out


def hello_ok(ws: str) -> bool:
    """The resumed turn's result: a regular file (not a link), inside the workspace, with
    exactly the requested content."""
    path = os.path.join(ws, "hello.txt")
    try:
        st = os.lstat(path)
    except OSError:
        return False
    if not stat.S_ISREG(st.st_mode) or os.path.realpath(path) != path or st.st_size > 64:
        return False
    with open(path, "rb") as f:
        return f.read() in (b"hi", b"hi\n")


# ------------------------------------------------------------------ main
def claude_version() -> str:
    out = subprocess.run([CL._claude(), "--version"], capture_output=True, text=True, timeout=20)
    return out.stdout.strip()


def preconditions(run_dir: str, home: str, version_of) -> str | None:
    if not os.path.isdir(run_dir):
        return "the run folder doesn't exist (create it with mktemp -d)"
    if os.listdir(run_dir):
        return "the run folder isn't empty: use a brand-new one"
    got = version_of()
    if got != WANT_CLAUDE:
        return f"Claude Code is {got!r}, not the reviewed {WANT_CLAUDE!r}"
    if not os.path.isdir(os.path.join(home, ".claude", "projects")):
        return "Claude's projects folder doesn't exist; this probe creates nothing in the home folder"
    return None


def propose(denials: dict) -> dict:
    """Minimum exact paths from what was denied: this conversation's writable state separately
    from bootstrap/auth reads. Whole trees, other conversations, credentials and settings are
    never proposed; anything without an exact template is listed for review instead."""
    writes, reads, review = set(CANDIDATE["write_files"]), set(), set()
    never = ("another conversation", "settings", "credentials", "keychain", "client config backup",
             "client config lock", "client config temp", "client config other")
    for (cat, tpl, op), n in denials.items():
        # Only the client's own state can be proposed: diagnostic categories (devices, runtime,
        # home dotfiles, workspace ancestors) name a denial, they never become an allowance.
        own = tpl is not None and (tpl == "{home}/.claude.json" or tpl.startswith("{home}/.claude/"))
        exact = own and "<" not in tpl and not any(x in cat for x in never)
        if exact and op.startswith("file-write") and ("{session}" in tpl or tpl.endswith("{slug}")):
            writes.add(tpl)  # this conversation's own state only
        elif exact and op.startswith("file-read"):
            reads.add(tpl)   # a bootstrap/auth read, readable but never writable
        else:
            review.add(f"{cat} ({op})")
    return {"claude_version": VERSION, "proven_by": "", "write_files": sorted(writes), "write_dirs": [],
            "read_files": sorted(reads), "read_dirs": [], "needs_review": sorted(review)}


def startup_result(t1: dict, stops: list, failure) -> str:
    """What a zero-prompt start showed. Waiting for input is INCONCLUSIVE, never a pass."""
    x, st = t1.get("exit") or {}, t1.get("stderr") or {}
    if stops or failure is not None or not t1:
        return "stopped"
    if t1.get("startup_verified"):
        return "init_before_input"
    if x.get("cause") == "natural_exit":
        return "structured_error_before_input" if st.get("structured") else "exit_before_input_unstructured"
    if t1.get("termination") == "window_elapsed":
        return "waiting_for_input"
    return "inconclusive"


class _CaptureFailed(Exception):
    """Internal: capture wasn't proven; main records the capture-stage evidence."""


def main(argv=None, *, version_of=claude_version, collector=None, sandbox=None, home=None,
         clock=time.monotonic, ledger: ProcessLedger | None = None) -> int:
    t_start = clock()  # the run's defined start: ONE monotonic budget from here
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--deadline", type=PR.deadline_arg(*DEADLINE_RANGE), default=600.0,
                    help=f"overall seconds from the run's start, {DEADLINE_RANGE[0]:g}-{DEADLINE_RANGE[1]:g} (default 600)")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--diagnostic-only", action="store_true",
                      help=f"one fresh prompt, no second turn, file denials observed for at most {DIAG_S:g} s")
    mode.add_argument("--startup-only", action="store_true",
                      help=f"one fresh client, NO prompt, observed for at most {STARTUP_S:g} s from launch")
    try:
        a = ap.parse_args(argv)
    except SystemExit:
        return 4
    budget = PR.Budget(a.deadline, clock, start=t_start)
    run_dir = os.path.realpath(os.path.expanduser(a.run_dir))
    home = os.path.realpath(home or os.path.expanduser("~"))
    why = preconditions(run_dir, home, version_of)
    if why:
        print(json.dumps({"outcome": "not_started", "why": why}))
        return 4
    ws = os.path.join(run_dir, "ws")
    os.mkdir(ws, 0o700)
    with open(os.path.join(ws, "README.md"), "w") as f:
        f.write("Scratch workspace for the Claude state-path probe.\n")
    session = str(uuid.uuid4())
    slug = os.path.basename(os.path.dirname(CL.transcript_path(ws, session)))
    if os.path.exists(os.path.join(home, ".claude", "projects", slug)):
        print(json.dumps({"outcome": "not_started", "why": "this workspace already has client state"}))
        return 4
    state = T.StateAllowlist(dict(CANDIDATE, claude_version=VERSION), source="probe candidate")
    sb = sandbox or T.ImplementerSandbox(state)
    if sb.available() is not None:
        print(json.dumps({"outcome": "not_started", "why": sb.available()}))
        return 4
    col = collector or LogCollector()
    run_stamp = T.stamp()
    run_wall_start = run_stamp["wall"]
    attr = Attribution()
    diag = a.diagnostic_only or a.startup_only
    # The baseline: every process identity BEFORE any probe process (collector, canary, client)
    # starts. Only what is in it, or descends from it, can ever be proven foreign.
    ledger = ledger or ProcessLedger()
    ledger.snapshot(baseline=True)
    scanner = Scanner(col, attr, run_wall_start, diagnostic=diag, ledger=ledger)
    scanner.clock_marks.append(run_stamp)
    phases: dict = {"run_start": run_stamp}  # fixed phase markers: paired wall/monotonic readings
    live: list = []
    holds: list = []  # launch holds not yet transferred to a connection (run_turn)
    ev = {"probe": "claude-state-path", "started_at": now_iso(), "claude_version": VERSION,
          "session": session, "workspace": "<run>/ws", "candidate": CANDIDATE, "deadline_s": a.deadline,
          "candidate_sha256": state.digest(), "turns": [], "denials": [], "stops": []}
    if diag:  # nothing routed, accepted or proven; the candidate is kept with the observations
        ev.update(diagnostic_only=True, window_s=DIAG_S, candidate_policy=CANDIDATE)
    if a.startup_only:  # and not even a prompt: the client's start alone
        ev.update(startup_only=True, window_s=STARTUP_S, prompts=0)
    failure = None
    cleanups: list[dict] = []
    cap: dict = {}  # filled by establish_capture as it goes
    stage = "capture"  # until an attributed canary denial proves capture, nothing of Claude starts
    collector_stopped = True
    try:
        try:
            col.start()
        except OSError:
            cap["failure"] = "collector_error"
        else:
            establish_capture(col, run_dir, budget, cap)
        if not cap.get("established"):
            raise _CaptureFailed()
        # Capture proven, but Claude starts only once the canary is positively gone and its files
        # removed: an unconfirmed cleanup stays in the capture record, exit 5.
        if not (cap.get("canary_exit_confirmed") is True and cap.get("canary_files_removed") is True):
            cap["failure"] = "canary_cleanup_unconfirmed"
            raise _CaptureFailed()
        stage = "turns"
        ev["capture"] = {"established": True, "attributed_reads": cap["attributed_reads"]}
        record = T.policy_record(state)
        ev["policy_sha256"] = record["sha256"]
        adapter = CL.ClaudeStreamAdapter({"coding_sandbox": sb, "home_for_state": home})
        ctx = {"home": home, "slug": slug, "session": session, "ws": ws, "tmp": None}
        ctxs: dict[int, dict] = {}
        plan = ((T1, False),) if diag else ((T1, False), (T2, True))  # diagnostic: never a T2
        for n, (text, resume) in enumerate(plan, 1):
            if budget.expired():
                scanner.stop("overall deadline")
                break
            out, conn = run_turn(adapter, col, ctx, text, resume=resume, record=record, budget=budget,
                                 attr=attr, scanner=scanner, ctxs=ctxs, n=n, live=live, clock=clock, holds=holds,
                                 diagnostic=diag, startup_only=a.startup_only, phases=phases)
            out["n"] = n
            out["cleanup"] = close_conn(conn, live, attr, scanner, ctxs, n, exit_record=out.get("exit"))
            cleanups.append(out["cleanup"])
            for r in release_holds(holds):  # a launch that failed after its child started
                cleanups.append(r)
                out["cleanup"] = {"exit_confirmed": out["cleanup"]["exit_confirmed"] and r["exit_confirmed"],
                                  "tmp_removed": out["cleanup"]["tmp_removed"] and r["tmp_removed"]}
            sink = out.pop("_stderr_sink", None)
            if sink is not None:  # categories and enums only, after the client's confirmed exit
                out["stderr"] = sink.summary()
                ro = scanner.role_ops.get(n, {})  # including reports that arrived during cleanup
                out["stderr"]["structured"] = correlate(out["stderr"]["structured"], ro)
                out["denied_roles"] = [{"role": r, "op_class": o, "count": min(k, 10000)}
                                       for (r, o), k in sorted(ro.items())]
            out["unattributed_denials"] = scanner.unattributed.get(n, 0)
            if n == 2:
                out["hello_ok"] = hello_ok(ws)
            clean = out["cleanup"]["exit_confirmed"] and out["cleanup"]["tmp_removed"]
            if diag:
                out["ok"] = False  # a diagnostic observation is never a successful turn
                ev["turns"].append(out)
                break
            verified = out["outcome"] == "completed" and out.get("policy_ok") and out.get("identity_ok")
            if n == 1:
                out["ok"] = bool(verified and out["reply_has_probe_ok"] and clean)
            else:  # the resumed turn must have DONE the task, verifiably
                wrote = {"tool": "Write", "path": "hello.txt", "kind": "use"} in out["tool_audit"]
                out["ok"] = bool(verified and wrote and out["hello_ok"] and clean)
            ev["turns"].append(out)
            if not out["ok"] or scanner.stops:
                break  # T2 only after a verified, successful T1; no retries
    except _CaptureFailed:
        pass
    except BaseException as e:  # noqa: BLE001 - Ctrl-C or anything unexpected: still clean up
        failure = e
        ev["outcome"] = "interrupted" if isinstance(e, KeyboardInterrupt) else "error"
        ev["error"] = type(e).__name__  # the class only: never a message (it may hold paths)
    finally:
        # Every client still alive is stopped here, whatever happened above; bounded per step.
        for conn in list(live):
            cleanups.append(close_conn(conn, live, attr))
        cleanups.extend(release_holds(holds))  # launches interrupted before their connection existed
        collector_stopped = col.stop() is True
    if stage == "capture":  # nothing of Claude was started; record that, redacted, and stop
        rec = capture_evidence(cap, started_at=ev["started_at"], deadline=a.deadline,
                               elapsed=clock() - t_start, failure=failure, collector_stopped=collector_stopped)
        code = 4 if all(rec["cleanup"].values()) else 5  # cleanup uncertainty is never "clean"
        blob = json.dumps(rec, indent=2, sort_keys=True)
        with open(os.path.join(run_dir, EVIDENCE), "x") as f:
            f.write(blob)
        print(json.dumps({"outcome": rec["outcome"], "stage": "capture", "failure": rec["failure"],
                          "evidence": os.path.join(run_dir, EVIDENCE),
                          "sha256": hashlib.sha256(blob.encode()).hexdigest()}))
        return code
    ev["collector_exit_confirmed"] = collector_stopped
    for i, out in enumerate(ev["turns"]):  # final per-turn denials, including late reports
        out["denials"] = [{"category": c, "template": t, "op": o, "count": min(k, 10000)}
                          for (c, t, o), k in sorted(scanner.denials.get(i + 1, {}).items(),
                                                     key=lambda x: (x[0][0], x[0][2]))]
    totals: dict = {}
    for d in scanner.denials.values():
        for k, v in d.items():
            totals[k] = totals.get(k, 0) + v
    ev["denials"] = [{"category": c, "template": t, "op": o, "count": min(k, 10000)}
                     for (c, t, o), k in sorted(totals.items(), key=lambda x: (x[0][0], x[0][2]))]
    ev["stops"] = list(scanner.stops)
    if diag:
        ev["hard_stops"] = list(scanner.stops)
        obs: dict = {}
        for d in scanner.observed.values():
            for k, v in d.items():
                obs[k] = obs.get(k, 0) + v
        ev["observed_denials"] = [{"category": c, "template": t, "op": o, "count": min(k, 10000)}
                                  for (c, t, o), k in sorted(obs.items(), key=lambda x: (x[0][0], x[0][2]))]
        t1 = ev["turns"][0] if ev["turns"] else {}
        ev["submitted_attempts"] = sum(1 for t in ev["turns"] if t.get("submitted"))
        ev["init"] = t1.get("init")
        ev["startup_verified"] = bool(t1.get("startup_verified"))
        ev["exit"], ev["stderr"] = t1.get("exit"), t1.get("stderr")
        ev["denied_roles"] = t1.get("denied_roles", [])
        if a.startup_only:
            ev["startup_result"] = startup_result(t1, scanner.stops, failure)
            ev["conclusive"] = ev["startup_result"] in ("structured_error_before_input",
                                                         "exit_before_input_unstructured", "init_before_input")
        ev["termination"] = ("interrupted" if isinstance(failure, KeyboardInterrupt) else "error"
                             if failure is not None else t1.get("termination") or
                             ("hard_stop" if scanner.stops else "not_started"))
    ev["attribution"] = dict(scanner.counts, unknown_identity=attr.unknown_identity,
                             proven_foreign_excluded=scanner.verdicts["proven_foreign"],
                             pre_client_excluded=scanner.verdicts["pre_client"])
    for k in ("ready_at", "canary_done_at"):
        if cap.get(k) is not None:
            phases[{"ready_at": "capture_ready", "canary_done_at": "canary_finished"}[k]] = cap[k]
    ev["phases"] = {k: phases[k] for k in ("run_start", "capture_ready", "canary_finished",
                                           "client_spawn_before", "client_spawn_after") if k in phases}
    ev["process_attribution"] = scanner.attribution_record()
    ev["model_turns"] = sum(1 for t in ev["turns"] if t.get("submitted"))
    ev["cleanup"] = {"exit_confirmed": all(c["exit_confirmed"] for c in cleanups),
                     "tmp_removed": all(c["tmp_removed"] for c in cleanups)}
    ev["transcript_kept"] = "{home}/.claude/projects/{slug}/{session}.jsonl" if os.path.exists(
        os.path.join(home, ".claude", "projects", slug, session + ".jsonl")) else None
    success = (not diag and failure is None and not scanner.stops and len(ev["turns"]) == 2
               and all(t["ok"] for t in ev["turns"]))
    if not (ev["cleanup"]["exit_confirmed"] and ev["cleanup"]["tmp_removed"] and collector_stopped):
        code = 5
    elif diag:
        code = 3 if failure is None and not scanner.stops and ev["turns"] else 2
    else:
        code = 0 if success else 2
    ev.setdefault("outcome", {0: "completed", 2: "stopped", 3: "diagnostic_recorded",
                              5: "cleanup_unconfirmed"}[code])
    if failure is None and not diag:  # a diagnostic run proposes nothing
        with open(os.path.join(run_dir, f"claude-state-{VERSION}.proposed.json"), "x") as f:
            json.dump(propose(totals), f, indent=2, sort_keys=True)
    blob = json.dumps(ev, indent=2, sort_keys=True)
    with open(os.path.join(run_dir, EVIDENCE), "x") as f:
        f.write(blob)
    print(json.dumps({"outcome": ev["outcome"], "evidence": os.path.join(run_dir, EVIDENCE),
                      "sha256": hashlib.sha256(blob.encode()).hexdigest()}))
    return code


if __name__ == "__main__":
    sys.exit(main())
