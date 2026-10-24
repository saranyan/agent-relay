"""D3/D4: the isolated check runner and the workspace fingerprint."""

import os
import sys
import tempfile
import unittest

import util  # noqa: F401  (puts the repository on sys.path)
from relay import verify as V
from relay.storage import InvalidInput, Unsupported

RUNNER = V.SandboxRunner()
NEEDS_SANDBOX = unittest.skipIf(RUNNER.available() is not None, "no isolated runner on this machine")


class Workspace(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.ws = os.path.realpath(self._t.name)
        os.makedirs(os.path.join(self.ws, "tests"))
        self.write("src.py", "def add(a, b):\n    return a + b\n")
        self.write("tests/test_add.py", "from src import add\n\ndef test():\n    assert add(1, 2) == 3\n")

    def tearDown(self):
        self._t.cleanup()

    def write(self, rel, text):
        with open(os.path.join(self.ws, rel), "w") as f:
            f.write(text)

    def spec(self, *argv, **kw):
        return V.validate_spec(dict({"argv": list(argv)}, **kw), self.ws)


class FingerprintTests(Workspace):
    def test_covers_source_tests_config_and_untracked_files(self):
        a = V.fingerprint(self.ws)["sha256"]
        for rel in ("src.py", "tests/test_add.py", "setup.cfg", "notes-untracked.txt"):
            self.write(rel, "changed\n")
            b = V.fingerprint(self.ws)["sha256"]
            self.assertNotEqual(a, b, rel)
            a = b
        os.chmod(os.path.join(self.ws, "src.py"), 0o755)  # mode bits count too
        self.assertNotEqual(a, V.fingerprint(self.ws)["sha256"])

    def test_declared_outputs_are_excluded(self):
        a = V.fingerprint(self.ws)["sha256"]
        for d in (V.OUTPUT_DIR, "__pycache__", ".git", "tests/__pycache__"):
            os.makedirs(os.path.join(self.ws, d), exist_ok=True)
            self.write(os.path.join(d, "x"), "artifact")
        self.assertEqual(a, V.fingerprint(self.ws)["sha256"])


class SymlinkTests(Workspace):
    """Codex R1 (2026-10-11): directory links were invisible to the fingerprint."""

    def test_directory_link_swaps_change_the_fingerprint(self):
        for name, text in (("one", "X = 1\n"), ("two", "X = 2\n")):
            os.makedirs(os.path.join(self.ws, name))
            self.write(f"{name}/source.py", text)
        os.symlink("one", os.path.join(self.ws, "lib"))
        a = V.fingerprint(self.ws)
        os.unlink(os.path.join(self.ws, "lib"))
        os.symlink("two", os.path.join(self.ws, "lib"))  # Codex's reproduction, inside the workspace
        self.assertNotEqual(a["sha256"], V.fingerprint(self.ws)["sha256"])

    def test_links_leaving_the_workspace_fail_closed(self):
        ext = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(ext, ignore_errors=True))
        with open(os.path.join(ext, "source.py"), "w") as f:
            f.write("X = 1\n")
        for link, target in (("lib", ext), ("one.py", os.path.join(ext, "source.py")), ("up", "..")):
            p = os.path.join(self.ws, link)
            os.symlink(target, p)
            with self.subTest(link=link), self.assertRaises(V.FingerprintError):
                V.fingerprint(self.ws)
            os.unlink(p)

    def test_links_into_excluded_folders_fail_closed(self):
        # Codex R1 recheck: ws/lib -> .relay-verify/generated, then the source changed: same hash.
        for folder in (V.OUTPUT_DIR + "/generated", "tests/__pycache__", ".git/hooks", "a/.pytest_cache/x"):
            os.makedirs(os.path.join(self.ws, folder))
            self.write(f"{folder}/source.py", "value = 1\n")
            for link, target in (("lib", folder), ("one.py", f"{folder}/source.py")):
                p = os.path.join(self.ws, link)
                os.symlink(target, p)
                with self.subTest(target=target), self.assertRaises(V.FingerprintError) as e:
                    V.fingerprint(self.ws)
                self.assertIn("excluded folder", str(e.exception))
                os.unlink(p)
        os.symlink("tests", os.path.join(self.ws, "t"))  # an ordinary in-scope folder is fine
        V.fingerprint(self.ws)

    def test_a_link_cycle_inside_the_workspace_is_not_followed(self):
        os.makedirs(os.path.join(self.ws, "a"))
        os.symlink("..", os.path.join(self.ws, "a", "back"))  # resolves to the workspace itself
        self.assertEqual(V.fingerprint(self.ws)["files"], 3)

    def test_inputs_outside_the_workspace_are_refused(self):
        ext = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(ext, ignore_errors=True))
        with open(os.path.join(ext, "t.py"), "w") as f:
            f.write("")
        with self.assertRaises(InvalidInput):
            self.spec("python3", os.path.join(ext, "t.py"))
        with self.assertRaises(InvalidInput):  # the same file, by a relative path
            self.spec("python3", os.path.relpath(os.path.join(ext, "t.py"), self.ws))
        self.spec("python3", "tests/test_add.py", "-q")  # in scope is fine

    def test_a_linked_output_folder_is_refused_before_anything_follows_it(self):
        ext = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(ext, ignore_errors=True))
        os.symlink(ext, os.path.join(self.ws, V.OUTPUT_DIR))
        with self.assertRaises(V.FingerprintError):
            V.prepare_output(self.ws)
        self.assertEqual(os.listdir(ext), [])  # nothing was created through the link


@NEEDS_SANDBOX
class ReadScopeTests(Workspace):
    """Codex R1 recheck: reads are confined by the runner's policy, not by parsing argv."""

    def run_check(self, *argv, **kw):
        return V.check(RUNNER, self.spec(*argv, **kw))

    def outside(self, text):
        d = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        with open(os.path.join(d, "outside.py"), "w") as f:
            f.write(text)
        return os.path.join(d, "outside.py")

    def test_a_hidden_outside_read_fails(self):
        ext = self.outside("print('outside code ran')\n")
        for argv in ([sys.executable, "-c", f"exec(open({ext!r}).read())"],          # Codex's reproduction
                     [sys.executable, "-c", "import sys; open(sys.argv[1].split('=', 1)[1]).read()",
                      f"--config={ext}"],                                           # bypasses argv parsing
                     ["/bin/sh", "-c", f"cat {ext}"]):                              # a shell string
            with self.subTest(argv=argv[-1]):
                r = self.run_check(*argv)
                self.assertEqual(r["status"], "failed", r["output_tail"])
                self.assertNotIn("outside code ran", r["output_tail"])
        home = os.path.expanduser("~")  # a user's home is never a trusted runtime
        r = self.run_check(sys.executable, "-c", f"import os; os.listdir({home!r})")
        self.assertEqual(r["status"], "failed")

    def test_unfingerprinted_folders_inside_the_workspace_are_unreadable(self):
        for folder in ("__pycache__", "tests/__pycache__", ".git", ".pytest_cache"):
            os.makedirs(os.path.join(self.ws, folder), exist_ok=True)
            self.write(f"{folder}/planted.py", "print('planted code ran')\n")
            with self.subTest(folder=folder):
                r = self.run_check(sys.executable, "-c", f"exec(open({folder + '/planted.py'!r}).read())")
                self.assertEqual(r["status"], "failed", r["output_tail"])
                self.assertNotIn("planted code ran", r["output_tail"])

    def test_the_output_folder_starts_empty_every_run(self):
        os.makedirs(os.path.join(self.ws, V.OUTPUT_DIR))
        self.write(f"{V.OUTPUT_DIR}/planted.py", "print('planted code ran')\n")
        r = self.run_check(sys.executable, "-c", f"exec(open({V.OUTPUT_DIR + '/planted.py'!r}).read())")
        self.assertEqual(r["status"], "failed", r["output_tail"])
        self.assertNotIn("planted code ran", r["output_tail"])
        self.assertFalse(os.path.exists(os.path.join(self.ws, V.OUTPUT_DIR, "planted.py")))

    def test_project_inputs_and_the_runtime_stay_readable(self):
        r = self.run_check(sys.executable, "-c", "import json, sqlite3, ssl; from src import add; "
                           "assert add(1, 2) == 3; print(open('tests/test_add.py').read()[:4])")
        self.assertEqual(r["status"], "passed", r["output_tail"])
        r = self.run_check("/bin/sh", "-c", "ls / >/dev/null && /usr/bin/env true")
        self.assertEqual(r["status"], "passed", r["output_tail"])

    def test_the_trust_boundary_is_explicit_and_in_the_config_hash(self):
        rt = V.runtime_read()
        self.assertFalse(any(p == os.path.expanduser("~") or os.path.expanduser("~").startswith(p + "/")
                             for p in rt if p not in ("/",)), rt)
        self.assertFalse(any(p.startswith(("/tmp", "/private/tmp", "/private/var/folders")) for p in rt), rt)
        with __import__("unittest").mock.patch.object(V, "RUNTIME_READ", V.RUNTIME_READ + ("/Users",)):
            widened = RUNNER.config_hash(self.spec("true"))
        self.assertNotEqual(widened, RUNNER.config_hash(self.spec("true")))


class ProbeTests(Workspace):
    """Codex R2 (2026-10-11): a sandbox that can't start must read as unavailable."""

    def fake_exe(self, body):
        path = os.path.join(self.ws, "fake-sandbox-exec")
        with open(path, "w") as f:
            f.write("#!/bin/sh\n" + body)
        os.chmod(path, 0o755)
        return path

    @unittest.skipIf(sys.platform != "darwin", "macOS only")
    def test_startup_denial_is_unavailable(self):
        r = V.SandboxRunner(self.fake_exe('echo "sandbox-exec: sandbox_apply: Operation not permitted" >&2; exit 71\n'))
        why = r.available()
        self.assertIsNotNone(why)
        self.assertIn("can't start", why)
        with self.assertRaises(Unsupported):
            r.run(self.spec("true"))

    @unittest.skipIf(sys.platform != "darwin", "macOS only")
    def test_a_runner_that_does_not_isolate_is_unavailable(self):
        r = V.SandboxRunner(self.fake_exe('shift 2; exec "$@"\n'))  # runs the command with no sandbox
        self.assertIn("didn't stop a read", r.available())  # the first denial it tries

    @NEEDS_SANDBOX
    def test_the_real_runner_proves_isolation(self):
        self.assertIsNone(V.SandboxRunner().available())


class SpecTests(Workspace):
    def test_fixed_argv_inside_the_workspace_only(self):
        for bad in ({"argv": "pytest -q"}, {"argv": []}, {"argv": ["a\0b"]}, {"argv": ["x"], "cwd": ".."},
                    {"argv": ["x"], "cwd": "/tmp"}, {"argv": ["x"], "timeout_s": 0},
                    {"argv": ["x"], "timeout_s": 99999}, {"argv": ["x"], "shell": True}):
            with self.subTest(bad=bad), self.assertRaises(InvalidInput):
                V.validate_spec(bad, self.ws)
        s = self.spec("python3", "-m", "pytest", cwd="tests", timeout_s=60)
        self.assertEqual(s["cwd"], os.path.join(self.ws, "tests"))

    def test_unavailable_runner_never_runs(self):
        r = V.UnavailableRunner("no sandbox here")
        self.assertEqual(r.available(), "no sandbox here")
        with self.assertRaises(Unsupported):
            V.check(r, self.spec("true"))


@NEEDS_SANDBOX
class RunnerTests(Workspace):
    def run_check(self, *argv, **kw):
        return V.check(RUNNER, self.spec(*argv, **kw))

    def test_pass_and_fail_are_bound_to_the_fingerprint(self):
        ok = self.run_check(sys.executable, "-c", "from src import add; assert add(1, 2) == 3; print('1 passed')")
        self.assertEqual((ok["status"], ok["exit"]), ("passed", 0))
        self.assertEqual(ok["fingerprint"], V.fingerprint(self.ws)["sha256"])
        self.assertIn("1 passed", ok["output_tail"])
        self.write("src.py", "def add(a, b):\n    return a - b\n")
        bad = self.run_check(sys.executable, "-c", "from src import add; assert add(1, 2) == 3")
        self.assertEqual((bad["status"], bad["exit"]), ("failed", 1))
        self.assertNotEqual(bad["fingerprint"], ok["fingerprint"])

    def test_no_writes_outside_the_output_folder_and_no_network(self):
        outside = os.path.join(os.path.dirname(self.ws), f"relay-escape-{os.getpid()}")
        for code in (f"open({outside!r}, 'w')", "open('src.py', 'a').write('#')",
                     "import socket; socket.create_connection(('127.0.0.1', 9), timeout=2)",
                     "import socket; socket.create_connection(('1.1.1.1', 80), timeout=2)"):
            with self.subTest(code=code):
                r = self.run_check(sys.executable, "-c", code)
                self.assertEqual(r["status"], "failed", r["output_tail"])
        self.assertFalse(os.path.exists(outside))
        ok = self.run_check(sys.executable, "-c", "import os; open(os.environ['TMPDIR'] + 'scratch', 'w').write('x')")
        self.assertEqual(ok["status"], "passed")  # its own scratch space works

    def test_timeout_kills_the_process_tree(self):
        marker = os.path.join(self.ws, V.OUTPUT_DIR, "child-alive")
        code = ("import subprocess, sys; subprocess.Popen([sys.executable, '-c', "
                f"\"import time; time.sleep(3); open({marker!r}, 'w')\"]); time_ = __import__('time'); time_.sleep(60)")
        r = self.run_check(sys.executable, "-c", code, timeout_s=1)
        self.assertEqual((r["status"], r["exit"]), ("timed_out", None))
        import time
        time.sleep(4)
        self.assertFalse(os.path.exists(marker), "a child of the check outlived it")

    def test_output_is_bounded_and_redacted(self):
        r = self.run_check(sys.executable, "-c", "print('x' * 100000); print('key sk-ant-' + 'A' * 40)")
        self.assertLessEqual(len(r["output_tail"]), V.OUTPUT_KEEP)
        self.assertTrue(r["truncated"])
        self.assertNotIn("sk-ant-", r["output_tail"])
        self.assertIn("[redacted]", r["output_tail"])

    def test_a_change_during_the_check_is_unknown_not_passed(self):
        # Simulated: the check can't write the workspace, so change it from outside mid-run.
        import threading
        import time
        t = threading.Timer(0.5, lambda: self.write("src.py", "# edited during the check\n"))
        t.start()
        r = self.run_check(sys.executable, "-c", "import time; time.sleep(1.5)")
        t.join()
        self.assertEqual(r["status"], "unknown")
        self.assertIsNone(r["fingerprint"])

    def test_config_hash_covers_the_command(self):
        a = RUNNER.config_hash(self.spec("python3", "-m", "pytest"))
        self.assertNotEqual(a, RUNNER.config_hash(self.spec("python3", "-m", "pytest", "-x")))
        self.assertNotEqual(a, RUNNER.config_hash(self.spec("python3", "-m", "pytest", timeout_s=30)))


if __name__ == "__main__":
    unittest.main()
