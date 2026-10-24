"""The implementer's coding policy.

A separate, explicitly selected route: the no-tools hand-off (`resume_argv`, `--tools ""`)
is unchanged. The policy is fixed and hashed at team creation; neither model can widen it.

Layer 1, Claude Code's own controls (P1): `--restricted --strict-mcp-config --permission-prompts
none --permission-mode acceptEdits --tools Read,Edit,Write,Glob,Grep`. No command, web, MCP or
subagent tools.

Layer 2, an OS sandbox around the client process (macOS `sandbox-exec`), proven by a probe
before use; otherwise the route is unavailable:
- writes (P2): the workspace minus protected names at any depth (matched case-insensitively,
  since the disk is), a private temp folder for this launch, and the client's own state paths
  from an explicitly identified, version-pinned allowlist tied to this session. Never the
  trusted runtime, the client's install, other conversations, settings or credentials.
- content reads (P4): the workspace, the trusted runtime (effective_runtime), the client's
  install, the private temp folder and the allowlisted client state/bootstrap records. Nothing
  else in the home folder or other projects. Metadata stays readable so paths resolve.
- network (P3): outbound TCP 443 and the system resolver's socket only; no listening, no other
  ports or UDP. This is a port restriction, not confinement to the model's API host.
- temp routing: the child's TMPDIR and the client's own temp variables
  (TEMP_ROUTING) all name that launch's private folder, overriding inherited values. This is an
  environment setting for the child only; it grants no filesystem access.

Until a bounded probe identifies the client's state paths (CLAUDE_STATE), the route stays
unavailable: the relay never guesses which of `~/.claude` a turn writes.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any

from . import verify as V
from .storage import InvalidInput, Unsupported

POLICY_NAME = "claude-coding-v1"
CODING_TOOLS = ("Read", "Edit", "Write", "Glob", "Grep")
PERMISSION_MODE = "acceptEdits"
# P2: never writable at any depth of the workspace (file or folder, any letter case).
PROTECTED = (".git", ".claude", ".codex", ".agents", ".mcp.json", ".envrc", V.OUTPUT_DIR,
             "CLAUDE.md", "AGENTS.md")
NETWORK = {"outbound_tcp_ports": [443], "resolver": "/private/var/run/mDNSResponder",
           "listen": False, "note": "port restriction only; not confinement to one host"}
RESOLVER = NETWORK["resolver"]
# Temp routing. The installed client (2.1.296) takes its temp root from
# CLAUDE_CODE_TMPDIR and otherwise "/tmp" on macOS, where it ignores TMPDIR; its sandboxed commands
# use CLAUDE_CODE_TMPDIR, then CLAUDE_TMPDIR, then "/tmp/claude". Every name here is set for the
# child, overriding anything inherited, to the SAME canonical private folder. The client strips a
# trailing slash from its own names; TMPDIR keeps the macOS form with one.
TEMP_ROUTING = (("TMPDIR", "{tmp}/"), ("CLAUDE_CODE_TMPDIR", "{tmp}"), ("CLAUDE_TMPDIR", "{tmp}"))
AUDIT_MAX = 200  # tool events kept per turn

# Where the client's state paths, once a probe has identified them, are pinned (per version).
STATE_DIR = os.path.join(os.path.dirname(__file__), "policy")


def coding_argv(claude: str, *, resume: str | None = None, session_id: str | None = None) -> list[str]:
    """Layer 1. Exactly one of `resume` (hand-off of an existing conversation) or `session_id`
    (a fresh relay-launched conversation)."""
    if (resume is None) == (session_id is None):
        raise InvalidInput("the coding route needs exactly one conversation")
    who = ["--resume", resume] if resume else ["--session-id", session_id]
    return [claude, "-p", *who, "--input-format", "stream-json", "--output-format", "stream-json",
            "--verbose", "--replay-user-messages", "--restricted", "--strict-mcp-config",
            "--permission-prompts", "none", "--permission-mode", PERMISSION_MODE,
            "--tools", ",".join(CODING_TOOLS)]


def _ci(name: str) -> str:
    """A case-insensitive SBPL regex for a literal name."""
    out = []
    for c in name:
        if c.isalpha():
            out.append(f"[{c.lower()}{c.upper()}]")
        else:
            out.append(V._rx(c))
    return "".join(out)


class StateAllowlist:
    """The client's own state, as identified by the bounded state-path probe: path templates
    with {home}, {slug} (Claude's project folder name for the workspace) and {session}. Writes
    and reads are listed separately; nothing is implied by a parent folder."""

    FIELDS = ("write_files", "write_dirs", "read_files", "read_dirs")

    def __init__(self, spec: dict, source: str = "injected"):
        if not isinstance(spec, dict) or set(spec) - set(self.FIELDS) - {"claude_version", "proven_by"}:
            raise InvalidInput("unrecognized client state allowlist")
        for f in self.FIELDS:
            v = spec.get(f, [])
            if not isinstance(v, list) or not all(isinstance(x, str) and x.startswith("{home}/") for x in v):
                raise InvalidInput(f"client state {f} must be paths under {{home}}")
        self.spec, self.source = spec, source

    @classmethod
    def pinned(cls, version: str) -> "StateAllowlist | None":
        """The reviewed allowlist for this client version, or None (route unavailable)."""
        path = os.path.join(STATE_DIR, f"claude-state-{version}.json")
        try:
            with open(path) as f:
                spec = json.load(f)
        except (OSError, ValueError):
            return None
        if spec.get("claude_version") != version or not spec.get("proven_by"):
            return None
        return cls(spec, source=os.path.basename(path))

    def resolve(self, cwd: str, session: str, home: str | None = None) -> dict[str, list[str]]:
        from .adapters.claude import transcript_path
        home = os.path.realpath(home or os.path.expanduser("~"))
        slug = os.path.basename(os.path.dirname(transcript_path(cwd, session)))
        out = {}
        for f in self.FIELDS:
            paths = []
            for t in self.spec.get(f, []):
                p = os.path.normpath(t.format(home=home, slug=slug, session=session))
                if not V._inside(p, home) or p == home:
                    raise InvalidInput("a client state path left the home folder")
                paths.append(p)
            out[f] = paths
        return out

    def digest(self) -> str:
        return hashlib.sha256(json.dumps({f: self.spec.get(f, []) for f in self.FIELDS},
                                         sort_keys=True).encode()).hexdigest()


def effective_runtime() -> list[str]:
    """THE runtime read list: the static trusted runtime plus the running
    interpreter's install, canonical and sorted. The same list protects against overlap,
    is recorded and hashed in the team's policy, and is written into the profile."""
    return sorted({os.path.realpath(p) for p in V.runtime_read()})


def client_install(claude: str) -> list[str]:
    """The client's own install, readable (never writable): the launcher and its target folder."""
    real = os.path.realpath(claude)
    out = [os.path.dirname(real)]
    if os.path.abspath(claude) != real:
        out.append(os.path.abspath(claude))
    return out


def _sensitive_roots(home: str, install: list[str], runtime: list[str]) -> list[str]:
    return [os.path.join(home, x) for x in (".claude", ".claude.json", ".local", "Library", ".ssh",
                                            ".codex", ".config")] + install + runtime


def preflight(workspace: str, install: list[str], home: str | None = None,
              runtime: list[str] | None = None) -> None:
    """Refuse a workspace the sandbox couldn't bound: one that is (or contains) the home folder,
    the client's state or install, or the runtime; or one holding a file that is hard-linked
    elsewhere (a write through it would change the other name too)."""
    ws = os.path.realpath(workspace)
    home = os.path.realpath(home or os.path.expanduser("~"))
    if ws in ("/", home) or not os.path.isdir(ws):
        raise InvalidInput("the coding workspace must be a project folder, not the home folder or /")
    for r in _sensitive_roots(home, install, effective_runtime() if runtime is None else runtime):
        r = os.path.realpath(r)
        if V._inside(r, ws) or V._inside(ws, r):
            raise InvalidInput(f"the coding workspace overlaps {r}, which the implementer must not write")
    prot = {n.lower() for n in PROTECTED}
    count = 0
    for dirpath, dirnames, filenames in os.walk(ws):
        dirnames[:] = [d for d in dirnames if d.lower() not in prot]
        for name in filenames:
            if name.lower() in prot:
                continue
            count += 1
            if count > V.MAX_FILES:
                raise InvalidInput(f"more than {V.MAX_FILES} files in the coding workspace")
            st = os.lstat(os.path.join(dirpath, name))
            if stat.S_ISREG(st.st_mode) and st.st_nlink > 1:
                raise InvalidInput(f"{os.path.relpath(os.path.join(dirpath, name), ws)} is hard-linked "
                                   "elsewhere; the implementer could change the other copy through it")


def profile(workspace: str, *, state: dict[str, list[str]], tmp: str, install: list[str],
            runtime: list[str] | None = None) -> str:
    q = json.dumps
    ws = os.path.realpath(workspace)
    if any(c in p for p in [ws, tmp, *install, *sum(state.values(), [])] for c in '"\\\n\r'):
        raise InvalidInput("a path can't be used in the sandbox policy")
    prot = "|".join(_ci(n) for n in PROTECTED)
    w_files = " ".join(f"(literal {q(p)})" for p in state["write_files"])
    w_dirs = " ".join(f"(subpath {q(p)})" for p in state["write_dirs"])
    r_files = " ".join(f"(literal {q(p)})" for p in state["read_files"] + state["write_files"])
    r_dirs = " ".join(f"(subpath {q(p)})" for p in state["read_dirs"] + state["write_dirs"])
    runtime = effective_runtime() if runtime is None else runtime
    reads = " ".join(f"(subpath {q(p)})" for p in runtime + install)
    return "\n".join([
        "(version 1)",
        "(allow default)",
        # P3: outbound 443 and the system resolver only.
        "(deny network*)",
        '(allow network-outbound (remote tcp "*:443"))',
        f"(allow network-outbound (literal {q(RESOLVER)}))",
        # P2: writes.
        "(deny file-write*)",
        f"(allow file-write* (subpath {q(ws)}) (subpath {q(tmp)}) {w_files} {w_dirs}"
        ' (literal "/dev/null") (literal "/dev/tty") (regex #"^/dev/fd/"))',
        f'(deny file-write* (regex #"^{V._rx(ws)}/(.*/)?({prot})(/|$)"))',
        # P4: content reads.
        "(deny file-read-data)",
        f'(allow file-read-data (literal "/") {reads} (subpath {q(ws)}) (subpath {q(tmp)}) {r_files} {r_dirs})',
    ])


def stamp() -> dict:
    """A paired wall-clock and monotonic reading."""
    return {"wall": time.time(), "mono": time.monotonic()}


def child_env(tmp: str, base: dict | None = None) -> dict:
    """The child's environment: `base` (the relay's own, by default; never modified) with every
    TEMP_ROUTING name set to the canonical private folder `tmp`, whatever was inherited."""
    if not os.path.isabs(tmp) or os.path.realpath(tmp) != tmp or tmp == "/":
        raise InvalidInput("the private temp folder must be a canonical absolute path")
    env = dict(os.environ if base is None else base)
    for name, template in TEMP_ROUTING:
        env[name] = template.format(tmp=tmp)
    return env


def temp_routing_evidence(env: dict, tmp: str, base: dict | None = None) -> dict:
    """Per-launch evidence without paths or inherited values: the routing's hash, whether every
    routed name names this launch's folder, and which names were inherited and overridden."""
    base = os.environ if base is None else base
    same = all(env.get(n) == t.format(tmp=tmp) for n, t in TEMP_ROUTING)
    return {"sha256": hashlib.sha256(json.dumps(TEMP_ROUTING).encode()).hexdigest(),
            "names": [n for n, _ in TEMP_ROUTING], "all_private": same,
            "overridden_inherited": sorted(n for n, _ in TEMP_ROUTING if n in base)}


def policy_record(state: StateAllowlist | None) -> dict:
    """What a team is created with (P1: fixed and hashed); the per-launch profile is derived
    from it and recorded with the writer's spawn evidence."""
    rec = {"name": POLICY_NAME, "tools": list(CODING_TOOLS), "permission_mode": PERMISSION_MODE,
           "mcp_servers": [], "protected": list(PROTECTED), "network": NETWORK,
           "temp_routing": [list(r) for r in TEMP_ROUTING],
           "runtime_read": effective_runtime(), "state_allowlist": state.digest() if state else None,
           "argv": coding_argv("claude", resume="{conversation}")[1:]}
    rec["sha256"] = hashlib.sha256(json.dumps(rec, sort_keys=True).encode()).hexdigest()
    return rec


class ImplementerSandbox:
    """Launches the client under the coding policy. available() is None only after a probe
    proves, with a stand-in program under the same profile shape, every boundary: workspace
    writes and reads work, protected writes (and case aliases), outside writes, outside reads,
    other ports, UDP and listening are denied, and the resolver socket and port 443 are open."""

    EXE = "/usr/bin/sandbox-exec"

    def __init__(self, state: StateAllowlist | None, exe: str | None = None):
        self.state, self.EXE = state, exe or self.EXE
        self._why: str | None = None
        self._probed = False
        self._lock = threading.Lock()

    def available(self) -> str | None:
        if self.state is None:
            return ("the coding route is unavailable: the client's own state paths haven't been "
                    "identified by the bounded state-path probe yet")
        if sys.platform != "darwin" or not os.access(self.EXE, os.X_OK):
            return "the coding route needs macOS sandbox-exec"
        with self._lock:
            if not self._probed:
                try:
                    self._why = self._probe()
                except (OSError, subprocess.SubprocessError, InvalidInput) as e:
                    self._why = f"the coding sandbox couldn't be checked ({type(e).__name__})"
                self._probed = True
        return self._why

    def _probe(self) -> str | None:
        got = self.probe_results()
        if isinstance(got, str):
            return got
        bad = sorted(k for k, v in PROBE_EXPECT.items() if got.get(k) != v)
        return f"the coding sandbox didn't hold: {', '.join(bad)}" if bad else None

    def probe_results(self) -> dict | str:
        """Run the stand-in program under this route's profile; its outcome per boundary, or
        why it couldn't run. Recorded as evidence (no secrets: the probe's own files only)."""
        with tempfile.TemporaryDirectory() as d:
            d = os.path.realpath(d)
            home = os.path.join(d, "home")
            ws = os.path.join(home, "project")
            tmp = os.path.join(d, "tmp")
            for p in (ws, tmp, os.path.join(ws, ".git"), os.path.join(ws, ".claude"),
                      os.path.join(home, ".claude", "projects", "s")):
                os.makedirs(p)
            state = {"write_files": [], "write_dirs": [os.path.join(home, ".claude", "projects", "s")],
                     "read_files": [], "read_dirs": []}
            secret = os.path.join(home, "secret.txt")
            with open(secret, "w") as f:
                f.write("outside\n")
            with open(os.path.join(ws, "in.txt"), "w") as f:
                f.write("inside\n")
            prof = profile(ws, state=state, tmp=tmp, install=[])
            code = PROBE_CODE
            r = subprocess.run([self.EXE, "-p", prof, sys.executable, "-I", "-c", code, ws, tmp, home,
                                state["write_dirs"][0], secret], capture_output=True, timeout=30,
                               stdin=subprocess.DEVNULL, cwd=ws)
            if r.returncode != 0 and not r.stdout:
                err = r.stderr.decode(errors="replace").strip().splitlines()[-1:] or [f"exit {r.returncode}"]
                return f"the coding sandbox can't start here ({err[0][:120]})"
            try:
                return json.loads(r.stdout.decode().strip().splitlines()[-1])
            except (ValueError, IndexError):
                return "the coding sandbox probe gave no result"

    def launch(self, argv: list[str], cwd: str, session: str, *, claude: str,
               home: str | None = None, runtime_expected: list[str] | None = None,
               hold: dict | None = None, stderr_sink=None) -> tuple[subprocess.Popen, dict]:
        """`stderr_sink` (diagnostic use only, opt-in): an object with start(file)/finish(); the
        child's stderr is then a pipe it drains from spawn, owned through the hold like the child.
        Without it stderr stays discarded. Nothing about the argv or profile changes.

        `hold`: a dict the CALLER owns, filled as resources come into
        being: "tmp" as soon as the folder exists, "spawning" just before the child is started,
        "proc" as soon as it exists. A caller can therefore stop and clean up whatever exists,
        whatever fails or interrupts afterwards. On any failure here, this method itself stops
        the child (through its handle) and removes the folder once the exit is confirmed."""
        why = self.available()
        if why:
            raise Unsupported(why)
        install = client_install(claude)
        runtime = effective_runtime()
        if runtime_expected is not None and runtime != runtime_expected:
            raise Unsupported("the runtime read scope changed since this team was created; nothing was started")
        preflight(cwd, install, home, runtime)
        hold = {} if hold is None else hold
        hold["tmp"] = tempfile.mkdtemp(prefix="relay-impl-")
        try:
            tmp = hold["tmp"]
            real = os.path.realpath(tmp)
            state = self.state.resolve(cwd, session, home)
            prof = profile(cwd, state=state, tmp=real, install=install, runtime=runtime)
            env = child_env(real)
            hold["spawning"] = True
            # Launch bounds: "spawn_before" is taken immediately before
            # Popen, the earliest the child can exist; "spawn_after" once Popen returns or fails
            # (the child may already be running before then). Both stay in the hold whatever happens.
            hold["spawn_before"] = stamp()
            try:
                hold["proc"] = subprocess.Popen([self.EXE, "-p", prof, *argv], cwd=cwd, env=env,
                                                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                                stderr=subprocess.DEVNULL if stderr_sink is None else subprocess.PIPE,
                                                bufsize=0)
            except OSError:
                hold["spawning"] = False  # Popen's own failure: no child exists
                raise
            finally:
                hold["spawn_after"] = stamp()
            proc = hold["proc"]
            if stderr_sink is not None:
                hold["stderr"] = stderr_sink  # owned with the child: released together
                stderr_sink.start(proc.stderr)
            evidence = {"policy": POLICY_NAME, "profile_sha256": hashlib.sha256(prof.encode()).hexdigest(),
                        "argv_sha256": hashlib.sha256(json.dumps(argv).encode()).hexdigest(),
                        "state_allowlist": self.state.digest(), "tmp": tmp,
                        "temp_routing": temp_routing_evidence(env, real),
                        "spawn": {"before": hold["spawn_before"], "after": hold["spawn_after"]}}
        except BaseException:
            release_hold(hold)
            raise
        return proc, evidence


def cleanup_tmp(path: str | None) -> None:
    if path and os.path.basename(path).startswith("relay-impl-"):
        shutil.rmtree(path, ignore_errors=True)


def release_hold(hold: dict, grace: float = 5.0) -> dict:
    """Stop and clean up what a launch `hold` owns; says honestly what was confirmed.

    The child is stopped through its handle only. The folder is removed only once no child
    can be using it. A hold marked "spawning" without a handle (interrupted between the
    child's start and its recording) can't prove there is no child: exit is unconfirmed."""
    proc = hold.get("proc")
    if proc is not None:
        if proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass
        try:
            proc.wait(grace)
            confirmed = True
        except subprocess.TimeoutExpired:
            confirmed = False
        if confirmed:
            if hold.get("stderr") is not None:
                hold["stderr"].finish()  # drained to EOF (bounded), then its read end closed
            for f in (proc.stdin, proc.stdout):
                try:
                    if f is not None:
                        f.close()
                except OSError:
                    pass
    else:
        confirmed = not hold.get("spawning")
    tmp = hold.get("tmp")
    if tmp and confirmed:
        cleanup_tmp(tmp)
    return {"exit_confirmed": confirmed, "tmp_removed": not tmp or not os.path.exists(tmp)}


# The stand-in program the probe runs inside the profile. It prints one JSON line of outcomes.
PROBE_CODE = r'''
import json, os, socket, sys
ws, tmp, home, state_dir, secret = sys.argv[1:6]
out = {}
def ok(name, f):
    try:
        f(); out[name] = "allowed"
    except PermissionError:
        out[name] = "denied"
    except OSError as e:
        out[name] = "denied" if e.errno == 1 else f"error:{e.errno}"
def w(p):
    return lambda: open(p, "w").write("x")
ok("write_workspace", w(os.path.join(ws, "new.py")))
ok("read_workspace", lambda: open(os.path.join(ws, "in.txt")).read())
ok("write_tmp", w(os.path.join(tmp, "t")))
ok("write_state", w(os.path.join(state_dir, "conv.jsonl")))
ok("write_git", w(os.path.join(ws, ".git", "config")))
ok("write_git_case_alias", w(os.path.join(ws, ".GIT", "hooks")))
ok("write_nested_claude_md", lambda: (os.makedirs(os.path.join(ws, "pkg"), exist_ok=True),
                                      open(os.path.join(ws, "pkg", "claude.md"), "w").write("x")))
ok("write_settings", w(os.path.join(ws, ".claude", "settings.json")))
ok("write_home", w(os.path.join(home, "planted.txt")))
ok("write_runtime", w(os.path.join(os.path.dirname(sys.executable), "planted")))
ok("read_outside", lambda: open(secret).read())
ok("list_home", lambda: os.listdir(home))
def tcp(port):
    def f():
        try:
            socket.create_connection(("127.0.0.1", port), timeout=2).close()
        except ConnectionRefusedError:
            pass  # reached the network stack: not blocked by the sandbox
    return f
ok("tcp_443", tcp(443))
ok("tcp_other", tcp(8443))
ok("udp_53", lambda: socket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendto(b"x", ("127.0.0.1", 53)))
def listen():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); s.listen(1); s.close()
ok("listen", listen)
def resolver():
    u = socket.socket(socket.AF_UNIX); u.connect("/private/var/run/mDNSResponder"); u.close()
ok("resolver", resolver)
print(json.dumps(out))
'''
PROBE_EXPECT = {"write_workspace": "allowed", "read_workspace": "allowed", "write_tmp": "allowed",
                "write_state": "allowed", "write_git": "denied", "write_git_case_alias": "denied",
                "write_nested_claude_md": "denied", "write_settings": "denied", "write_home": "denied",
                "write_runtime": "denied", "read_outside": "denied", "list_home": "denied",
                "tcp_443": "allowed", "tcp_other": "denied", "udp_53": "denied", "listen": "denied",
                "resolver": "allowed"}


def audit_entry(block: dict, cwd: str | None, kind: str = "use") -> dict:
    """One tool event, recorded by name and workspace-relative path only (never inputs,
    patterns, file contents or results)."""
    inp = block.get("input") if isinstance(block.get("input"), dict) else {}
    raw = inp.get("file_path") or inp.get("path") or inp.get("notebook_path")
    path = None
    if isinstance(raw, str) and raw:
        full = os.path.realpath(os.path.join(cwd or "/", raw))
        path = os.path.relpath(full, os.path.realpath(cwd)) if cwd and V._inside(full, os.path.realpath(cwd)) \
            else "outside the workspace"
    name = block.get("name") if isinstance(block.get("name"), str) else "?"
    return {"tool": name[:64], "path": path, "kind": kind}
