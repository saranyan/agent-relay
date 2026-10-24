"""Supplemental setup (scripts/handoff_probe_setup.sh): a failed prerequisite halts setup
before `relay init`, writes no env file (and removes a stale one), so nothing that
follows `&&` (sourcing the env, launching a client) runs. Fake clients only."""

import os
import stat
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SETUP = os.path.join(HERE, "scripts", "handoff_probe_setup.sh")


class SetupHalts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="handoff-setup-")
        self.bin = os.path.join(self.tmp, "bin")
        self.runs = os.path.join(self.tmp, "runs")
        os.makedirs(self.bin)
        self.marker = os.path.join(self.tmp, "init-called")
        self.exe("relay", f'#!/bin/sh\necho "$@" > "{self.marker}"\nmkdir -p "$3"\n')
        self.versions()

    def tearDown(self):
        subprocess.run(["rm", "-rf", self.tmp])

    def exe(self, name, body):
        p = os.path.join(self.bin, name)
        with open(p, "w") as f:
            f.write(body)
        os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC)

    def versions(self, claude="2.1.296 (Claude Code)", codex="codex-cli 0.162.1"):
        self.exe("claude", f'#!/bin/sh\necho "{claude}"\n')
        self.exe("codex", f'#!/bin/sh\necho "{codex}"\n')

    def run_setup(self):
        # As pasted: setup, then source the env, then a client launch, chained with &&.
        env = dict(os.environ, PATH=f"{self.bin}:/usr/bin:/bin", RELAY_PROBE_RUNS_DIR=self.runs,
                   RELAY_PROBE_RELAY=os.path.join(self.bin, "relay"))
        return subprocess.run(["bash", "-c", f'bash "{SETUP}" && source "{self.runs}/supplement.env" '
                                             f'&& echo "LAUNCHED in $SCRATCH"'],
                              env=env, capture_output=True, text=True, timeout=60)

    def assert_halted(self, r, why):
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertIn("STOP:", r.stderr)
        self.assertIn(why, r.stderr)
        self.assertNotIn("LAUNCHED", r.stdout)            # nothing after setup ran
        self.assertFalse(os.path.exists(self.marker))     # init never reached
        self.assertFalse(os.path.exists(os.path.join(self.runs, "supplement.env")))

    def stale_env(self):
        os.makedirs(self.runs, exist_ok=True)
        with open(os.path.join(self.runs, "supplement.env"), "w") as f:
            f.write("export RUN=/old SCRATCH=/old/scratch ROOT=/old/root\n")

    def test_good_setup_initialises_and_writes_the_env(self):
        r = self.run_setup()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("LAUNCHED in ", r.stdout)
        with open(self.marker) as f:
            self.assertIn("--max-turns 2 --turn-timeout 180", f.read())
        with open(os.path.join(self.runs, "supplement.env")) as f:
            self.assertIn("SCRATCH=", f.read())

    def test_changed_claude_version_halts(self):
        self.stale_env()
        self.versions(claude="2.1.297 (Claude Code)")
        self.assert_halted(self.run_setup(), "not the reviewed 2.1.296")

    def test_changed_codex_version_halts(self):
        self.stale_env()
        self.versions(codex="codex-cli 0.162.2")
        self.assert_halted(self.run_setup(), "not the reviewed codex-cli 0.162.1")

    def test_missing_client_halts(self):
        self.stale_env()
        os.remove(os.path.join(self.bin, "codex"))
        self.assert_halted(self.run_setup(), "codex")

    def test_prepopulated_run_folder_halts(self):
        full = os.path.join(self.tmp, "already-used")
        os.makedirs(full)
        with open(os.path.join(full, "leftover"), "w") as f:
            f.write("x")
        self.exe("mktemp", f'#!/bin/sh\necho "{full}"\n')  # as if the "fresh" folder weren't empty
        self.stale_env()
        self.assert_halted(self.run_setup(), "is not empty")
