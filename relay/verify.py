"""Relay-run verification for the review loop.

* One human-configured check, fixed at team creation: an argv list and a working folder
  inside the team's workspace. No shell, no model-selected commands, no interpolation.
* It runs in an enforced isolated policy (macOS `sandbox-exec`): no network, and no writes
  outside the workspace's declared output folder (`.relay-verify/`). The working folder alone
  is not confinement. Where the isolated runner is unavailable, verification is unavailable,
  and the workflow cannot auto-accept.
* Read scope is enforced by the same policy, not by parsing argv:
  the check may read file contents only from
    - project inputs: the workspace, which the fingerprint covers, minus the excluded folders
      (version control and caches), which are not fingerprinted and so are unreadable; and
    - its own output folder, emptied before every run, so nothing planted there is read back;
    - the trusted runtime (`RUNTIME_READ` plus the running interpreter's install): system
      tools, libraries and the language runtime with its installed packages. These are trusted
      as the machine's software, not fingerprinted, and are outside what the implementer may
      change. Anything else (home folders, temp folders, other projects) is unreadable.
  File metadata (existence, size, times) stays readable everywhere so paths resolve.
* Bounded runtime and output; the whole process tree is killed on timeout.
* Evidence: status, exit code, duration, a redacted bounded output tail, the config hash and
  a workspace fingerprint taken before AND after the check. If the workspace changed during
  the check, the result is "unknown", never "passed".
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
from typing import Any

from .protocol import SECRET_PATTERNS
from .storage import InvalidInput, Unsupported

OUTPUT_DIR = ".relay-verify"  # the check's only writable place; excluded from the fingerprint
# Never part of the fingerprint: version-control internals and interpreter caches (declared
# output artifacts). Everything else under the workspace counts, tracked or not.
DEFAULT_EXCLUDES = (OUTPUT_DIR, ".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".DS_Store")
MAX_FILES = 20000
MAX_BYTES = 512 * 1024 * 1024
MAX_ARGV = 64
MAX_ARG = 1024
TIMEOUT_RANGE = (1, 1800)
OUTPUT_KEEP = 4096        # stored tail of the output
OUTPUT_READ = 1024 * 1024  # read (and count) at most this much; the rest is drained and dropped


class FingerprintError(Exception):
    pass


def _inside(path: str, root: str) -> bool:
    return path == root or path.startswith(root + os.sep)


def _link(h, root: str, path: str, rel: str, excludes: tuple[str, ...]) -> None:
    """A symlink, file or directory, is part of the fingerprint by its target. A target whose
    content isn't fingerprinted where it lives fails closed: one outside the
    workspace, or one inside an excluded folder (the output folder, version control, caches)
    at any depth. Otherwise the target's content is fingerprinted where it lives (never
    followed here: no cycles)."""
    target = os.readlink(path)
    real = os.path.realpath(path)
    if not _inside(real, root):
        raise FingerprintError(f"{rel} links outside the workspace ({target}); its content can't be checked")
    to = os.path.relpath(real, root)
    if any(part in excludes for part in to.split(os.sep)):
        raise FingerprintError(f"{rel} links into an excluded folder ({target}); its content isn't checked")
    h.update(f"L\0{rel}\0{target}\0{to}\0".encode())


def fingerprint(root: str, excludes: tuple[str, ...] = DEFAULT_EXCLUDES) -> dict:
    """A content hash of the workspace: every regular file, and every file or directory
    symlink, under `root` except the excluded names (at any depth), by relative path, mode
    bits and content (links by target). Links leaving the workspace fail closed."""
    root = os.path.realpath(root)
    h = hashlib.sha256()
    files = total = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in excludes)
        for name in list(dirnames):  # directory symlinks: listed here, never walked into
            path = os.path.join(dirpath, name)
            if os.path.islink(path):
                dirnames.remove(name)
                files += 1
                _link(h, root, path, os.path.relpath(path, root), excludes)
        for name in sorted(filenames):
            if name in excludes:
                continue
            path = os.path.join(dirpath, name)
            rel = os.path.relpath(path, root)
            st = os.lstat(path)
            files += 1
            if files > MAX_FILES:
                raise FingerprintError(f"more than {MAX_FILES} files in the workspace")
            if stat.S_ISLNK(st.st_mode):
                _link(h, root, path, rel, excludes)
                continue
            if not stat.S_ISREG(st.st_mode):
                h.update(f"S\0{rel}\0{stat.S_IFMT(st.st_mode)}\0".encode())
                continue
            total += st.st_size
            if total > MAX_BYTES:
                raise FingerprintError("the workspace is too large to fingerprint")
            fh = hashlib.sha256()
            with open(path, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    fh.update(chunk)
            h.update(f"F\0{rel}\0{st.st_mode & 0o111:o}\0{fh.hexdigest()}\0".encode())
    return {"sha256": h.hexdigest(), "files": files, "excludes": list(excludes)}


def redact(text: str) -> str:
    for _label, pat in SECRET_PATTERNS:
        text = pat.sub("[redacted]", text)
    return text


def validate_spec(spec: Any, workspace: str) -> dict:
    """The check as configured by the human at team creation."""
    if not isinstance(spec, dict) or set(spec) - {"argv", "cwd", "timeout_s"}:
        raise InvalidInput("the check is {argv, cwd?, timeout_s?}")
    argv = spec.get("argv")
    if not isinstance(argv, list) or not 1 <= len(argv) <= MAX_ARGV or \
            not all(isinstance(a, str) and a and len(a) <= MAX_ARG and "\0" not in a for a in argv):
        raise InvalidInput(f"the check command is a list of 1-{MAX_ARGV} non-empty arguments")
    ws = os.path.realpath(workspace)
    cwd = os.path.realpath(os.path.join(ws, spec.get("cwd") or "."))
    if cwd != ws and not cwd.startswith(ws + "/"):
        raise InvalidInput("the check runs inside the workspace")
    if not os.path.isdir(cwd):
        raise InvalidInput("the check's folder doesn't exist")
    if any(c in ws for c in '"\\\n\r'):
        raise InvalidInput("the workspace path can't be used in the isolated runner's policy")
    # An early, friendly refusal only: the read scope itself is enforced by the runner's policy.
    for a in argv[1:]:  # the program itself may be a system tool; what it reads must be in scope
        if "/" in a and not a.startswith("-"):
            cand = os.path.realpath(os.path.join(cwd, a))
            if os.path.exists(cand) and not _inside(cand, ws):
                raise InvalidInput(f"the check reads {a}, outside the workspace; it couldn't be fingerprinted")
    t = spec.get("timeout_s", 300)
    if not isinstance(t, int) or not TIMEOUT_RANGE[0] <= t <= TIMEOUT_RANGE[1]:
        raise InvalidInput(f"the check's time limit is {TIMEOUT_RANGE[0]}-{TIMEOUT_RANGE[1]} seconds")
    return {"argv": list(argv), "cwd": cwd, "workspace": ws, "timeout_s": t}


# The trusted runtime: system software and language runtimes, readable by every check and not
# fingerprinted. Never a user's home or a temp folder. The running interpreter's own install
# is added (see runtime_read); everything under the workspace is a project input instead.
RUNTIME_READ = ("/System", "/usr", "/bin", "/sbin", "/Library", "/opt/homebrew",
                "/private/etc", "/private/var/db/timezone", "/private/var/db/dyld",
                "/private/var/select", "/private/var/db/xcode_select_link", "/dev")


def runtime_read() -> list[str]:
    """RUNTIME_READ plus the running Python's install (wherever it lives), deduplicated."""
    extra = {os.path.realpath(p) for p in (sys.prefix, sys.base_prefix, os.path.dirname(sys.executable))}
    out = list(RUNTIME_READ)
    for p in sorted(extra):
        if not any(_inside(p, r) for r in out) and p not in ("/", os.path.expanduser("~")):
            out.append(p)
    return out


def _rx(text: str) -> str:
    return "".join("\\" + c if c in ".^$*+?()[]{}|\\-" else c for c in text)


def _profile(workspace: str) -> str:
    out = os.path.join(workspace, OUTPUT_DIR)
    q = lambda p: json.dumps(p)  # a JSON string is a valid SBPL string literal for plain paths
    hidden = "|".join(_rx(x) for x in DEFAULT_EXCLUDES if x != OUTPUT_DIR)
    return "\n".join([
        "(version 1)",
        "(allow default)",
        "(deny network*)",
        "(deny file-write*)",
        f"(allow file-write* (subpath {q(out)}) (literal \"/dev/null\") (literal \"/dev/tty\")"
        " (regex #\"^/dev/fd/\"))",
        # Read scope: contents only from the trusted runtime and the workspace (project inputs);
        # metadata everywhere so paths resolve. Excluded folders aren't fingerprinted: unreadable.
        "(deny file-read-data)",
        "(allow file-read-data (literal \"/\") " + " ".join(f"(subpath {q(p)})" for p in runtime_read())
        + f" (subpath {q(workspace)}))",
        f"(deny file-read-data (regex #\"^{_rx(workspace)}/(.*/)?({hidden})(/|$)\"))",
    ])


def prepare_output(workspace: str) -> str:
    """A fresh, empty `<workspace>/.relay-verify/tmp`, made without following any symlink (this
    runs outside the sandbox). A pre-existing link or non-directory there is refused, and any
    earlier content is removed first: the folder isn't fingerprinted, so nothing left in it
    (by an earlier check or anyone else) may be read back by this one."""
    out = os.path.join(workspace, OUTPUT_DIR)
    try:
        st = os.lstat(out)
    except FileNotFoundError:
        st = None
    if st is not None:
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            raise FingerprintError(f"{OUTPUT_DIR} must be a plain folder, not a link")
        if not shutil.rmtree.avoids_symlink_attacks:
            raise FingerprintError(f"{OUTPUT_DIR} can't be emptied safely on this system")
        shutil.rmtree(out)  # removes links inside it, never what they point to
    try:
        os.mkdir(out, 0o700)  # never follows a link that appeared meanwhile: it fails instead
        path = os.path.join(out, "tmp")
        os.mkdir(path, 0o700)
    except FileExistsError:
        raise FingerprintError(f"{OUTPUT_DIR} changed while it was being prepared") from None
    return path


class SandboxRunner:
    """macOS sandbox-exec: no network, writes only under the workspace's `.relay-verify/`."""
    name = "macos-sandbox-exec"
    EXE = "/usr/bin/sandbox-exec"

    def __init__(self, exe: str | None = None):
        self.EXE = exe or self.EXE
        self._why: str | None = None
        self._probed = False

    def available(self) -> str | None:
        """None only after a probe PROVES isolation: a harmless command runs, the
        allowed output folder is writable, a workspace file is readable, and a workspace write,
        an outside write, an outside read, a read inside an excluded (unfingerprinted) folder
        and a loopback connection are all denied. A sandbox that can't start is unavailable."""
        if sys.platform != "darwin" or not os.access(self.EXE, os.X_OK):
            return "isolated verification needs macOS sandbox-exec"
        if not self._probed:
            try:
                self._why = self._probe()
            except (OSError, subprocess.SubprocessError) as e:
                self._why = f"the isolated runner couldn't be checked ({type(e).__name__})"
            self._probed = True
        return self._why

    def _probe(self) -> str | None:
        import socket
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            ws = os.path.realpath(d)
            out = prepare_output(ws)
            outside = os.path.join(os.path.dirname(ws), f"relay-probe-{os.getpid()}-{os.urandom(4).hex()}")

            def run(*argv):
                return subprocess.run([self.EXE, "-p", _profile(ws), *argv], cwd=ws, capture_output=True,
                                      timeout=15, stdin=subprocess.DEVNULL)
            r = run("/usr/bin/true")
            if r.returncode != 0:
                err = r.stderr.decode(errors="replace").strip().splitlines()[-1:] or [f"exit {r.returncode}"]
                return f"the isolated runner can't start here ({err[0][:120]})"
            r = run("/usr/bin/touch", os.path.join(out, "ok"))
            if r.returncode != 0 or not os.path.exists(os.path.join(out, "ok")):
                return "the isolated runner can't write its own output folder"
            with open(os.path.join(ws, "input.txt"), "w") as f:
                f.write("project input\n")
            r = run("/bin/cat", os.path.join(ws, "input.txt"))
            if r.returncode != 0 or b"project input" not in r.stdout:
                return "the isolated runner can't read the workspace"
            os.mkdir(os.path.join(ws, "__pycache__"))
            for hidden in (outside + "-read", os.path.join(ws, "__pycache__", "planted")):
                with open(hidden, "w") as f:
                    f.write("hidden input\n")
                try:
                    r = run("/bin/cat", hidden)
                finally:
                    os.unlink(hidden)
                if r.returncode == 0 or b"hidden input" in r.stdout:
                    return "the isolated runner didn't stop a read it must deny"
            for target in (os.path.join(ws, "denied"), outside):
                r = run("/usr/bin/touch", target)
                if os.path.exists(target):
                    os.unlink(target)
                    return "the isolated runner didn't stop a write it must deny"
                if r.returncode == 0:
                    return "the isolated runner reported a denied write as successful"
            srv = socket.socket()
            try:
                srv.bind(("127.0.0.1", 0))
                srv.listen(1)
                port = srv.getsockname()[1]
                r = run(sys.executable, "-c",
                        f"import socket; socket.create_connection(('127.0.0.1', {port}), timeout=3)")
                if r.returncode == 0:
                    return "the isolated runner didn't block network access"
            finally:
                srv.close()
        return None

    def config_hash(self, spec: dict) -> str:
        c = {"argv": spec["argv"], "cwd": spec["cwd"], "workspace": spec["workspace"],
             "timeout_s": spec["timeout_s"], "runner": self.name,
             "profile": hashlib.sha256(_profile(spec["workspace"]).encode()).hexdigest(),
             "excludes": list(DEFAULT_EXCLUDES)}
        return hashlib.sha256(json.dumps(c, sort_keys=True).encode()).hexdigest()

    def run(self, spec: dict) -> dict:
        why = self.available()
        if why:
            raise Unsupported(why)
        ws = spec["workspace"]
        tmp = prepare_output(ws)
        env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin:" + os.path.dirname(sys.executable),
               "HOME": os.path.join(ws, OUTPUT_DIR), "TMPDIR": tmp + "/", "LANG": "en_US.UTF-8",
               "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1"}
        t0 = time.monotonic()
        proc = subprocess.Popen([self.EXE, "-p", _profile(ws), *spec["argv"]], cwd=spec["cwd"], env=env,
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                start_new_session=True)
        buf = bytearray()
        seen = [0]

        def drain():
            for chunk in iter(lambda: proc.stdout.read(65536), b""):
                seen[0] += len(chunk)
                if len(buf) < OUTPUT_READ:
                    buf.extend(chunk)
                    if len(buf) > OUTPUT_READ:
                        del buf[OUTPUT_READ:]
        th = threading.Thread(target=drain, daemon=True)
        th.start()
        timed_out = False
        try:
            proc.wait(timeout=spec["timeout_s"])
        except subprocess.TimeoutExpired:
            timed_out = True
        finally:
            try:  # the whole process group, finished or not: nothing outlives the check
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            proc.wait()
            th.join(timeout=5)
            proc.stdout.close()
        text = redact(bytes(buf).decode("utf-8", "replace"))
        status = "timed_out" if timed_out else ("passed" if proc.returncode == 0 else "failed")
        return {"status": status, "exit": None if timed_out else proc.returncode,
                "duration_s": round(time.monotonic() - t0, 3), "output_tail": text[-OUTPUT_KEEP:],
                "output_bytes": seen[0], "truncated": seen[0] > OUTPUT_KEEP}


class UnavailableRunner:
    name = "unavailable"

    def __init__(self, why: str = "no isolated verification runner on this machine"):
        self.why = why

    def available(self) -> str | None:
        return self.why

    def config_hash(self, spec: dict) -> str:
        return ""

    def run(self, spec: dict) -> dict:
        raise Unsupported(self.why)


def default_runner():
    r = SandboxRunner()
    return r if r.available() is None else UnavailableRunner(r.available() or "")


def check(runner, spec: dict) -> dict:
    """Run the check for the current revision: fingerprint, run, fingerprint again. A change
    during the check, or anything the runner can't vouch for, is 'unknown', never 'passed'."""
    try:
        before = fingerprint(spec["workspace"])
    except (OSError, FingerprintError) as e:
        return {"status": "unknown", "reason": f"couldn't fingerprint the workspace: {e}"}
    try:
        res = runner.run(spec)
    except FingerprintError as e:  # e.g. the output folder is a link: nothing was run
        return {"status": "unknown", "reason": str(e)}
    try:
        after = fingerprint(spec["workspace"])
    except (OSError, FingerprintError) as e:
        return dict(res, status="unknown", reason=f"couldn't fingerprint the workspace: {e}",
                    fingerprint=before["sha256"])
    if after["sha256"] != before["sha256"]:
        return dict(res, status="unknown", reason="the workspace changed during the check",
                    fingerprint=None)
    return dict(res, fingerprint=before["sha256"], files=before["files"])
