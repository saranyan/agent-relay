"""Launch cleanup: the coding writer and its private temp
folder are owned from the moment each exists, before the connection can be built.

Fakes only: the fake client runs under the REAL coding sandbox (skipped where there is none). No
real Claude, model or user session. Every test reaps any child it saw, whatever the outcome.
"""

import json
import os
import subprocess
import unittest
from unittest import mock

import util  # noqa: F401
from test_probe_fixes import ProbeFixBase

from relay import toolpolicy as T
from relay.adapters import claude as CL
from relay.adapters.claude import StreamConnection

SP = __import__("test_probe_scripts").SP


class Spy:
    """Records the fake client's processes (started through the sandbox) and every relay-impl
    temp folder, without changing what they do."""

    def __init__(self, claude):
        self.claude, self.procs, self.tmps = claude, [], []
        self._popen, self._mkdtemp = subprocess.Popen, T.tempfile.mkdtemp

    def __enter__(self):
        spy = self

        class Popen(self._popen):
            def __init__(p, args, *a, **kw):
                super().__init__(args, *a, **kw)
                if isinstance(args, list) and spy.claude in args and args[0] == T.ImplementerSandbox.EXE:
                    spy.procs.append(p)

        def mkdtemp(*a, **kw):
            path = self._mkdtemp(*a, **kw)
            if os.path.basename(path).startswith("relay-impl-"):
                self.tmps.append(path)
            return path
        self.patches = [mock.patch.object(T.subprocess, "Popen", Popen),
                        mock.patch.object(T.tempfile, "mkdtemp", mkdtemp)]
        for p in self.patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self.patches:
            p.stop()

    def alive(self):
        return [p for p in self.procs if p.poll() is None]

    def reap(self):
        """Test hygiene: stop anything still running and remove leftover folders."""
        for p in self.procs:
            if p.poll() is None:
                p.kill()
            p.wait(5)
        for t in self.tmps:
            T.cleanup_tmp(t)


def fail_construction(exc, on_call=1):
    """StreamConnection.__init__ raises `exc` on its `on_call`-th coding construction."""
    real = StreamConnection.__init__
    calls = {"n": 0}

    def init(self, proc, *a, **kw):
        if kw.get("policy") is not None:
            calls["n"] += 1
            if calls["n"] == on_call:
                raise exc
        return real(self, proc, *a, **kw)
    return mock.patch.object(StreamConnection, "__init__", init)


def interrupt_after_spawn(spy, on_spawn=1):
    """KeyboardInterrupt INSIDE the sandbox's launch: after the child exists, before launch
    returns (while the evidence is being built)."""
    real = T.StateAllowlist.digest
    done = {"n": 0}

    def digest(self):
        if len(spy.procs) == on_spawn and done["n"] < on_spawn and spy.procs[-1].poll() is None:
            done["n"] += 1
            raise KeyboardInterrupt()
        return real(self)
    return mock.patch.object(T.StateAllowlist, "digest", digest)


class ProbeLaunchOwnershipTests(ProbeFixBase):
    """The state probe, end to end, on the fresh (turn 1) and resume (turn 2) routes."""

    def probe(self, *patchers, hello=False):
        with Spy(self.claude) as spy:
            try:
                ctx = list(patchers) + ([self.hello_scene()] if hello else [])
                for p in ctx:
                    p.start()
                try:
                    code, raw, _ = self.run_probe()
                finally:
                    for p in ctx:
                        p.stop()
                alive = spy.alive()
                kept = [t for t in spy.tmps if os.path.exists(t)]
                return code, json.loads(raw), raw, spy, alive, kept
            finally:
                spy.reap()

    def check_owned(self, code, ev, raw, spy, alive, kept, *, turns, submitted):
        self.assertTrue(spy.procs, "the fake writer should have been started")
        self.assertEqual(alive, [], "a writer was left running")
        self.assertEqual(kept, [], "a private temp folder was left behind")
        self.assertTrue(ev["cleanup"]["exit_confirmed"])
        self.assertTrue(ev["cleanup"]["tmp_removed"])
        self.assertNotIn("SECRET", raw)
        self.assertNotEqual(code, 0)
        self.assertEqual(len(ev["turns"]), turns, "no next turn after a failed launch")
        self.assertEqual(ev["model_turns"], submitted)

    def test_fresh_constructor_failure(self):
        r = self.probe(fail_construction(ValueError("/Users/someone/SECRET"), on_call=1))
        self.check_owned(*r, turns=0, submitted=0)
        self.assertEqual(r[1]["error"], "ValueError")
        self.assertEqual(len(r[3].procs), 1)

    def test_resume_constructor_failure(self):
        r = self.probe(fail_construction(ValueError("/Users/someone/SECRET"), on_call=2), hello=True)
        self.check_owned(*r, turns=1, submitted=1)
        self.assertEqual(r[1]["error"], "ValueError")
        self.assertEqual(len(r[3].procs), 2)

    def test_fresh_interruption_between_spawn_and_return(self):
        code, ev, raw, spy, alive, kept = self.probe_interrupted(on_spawn=1)
        self.check_owned(code, ev, raw, spy, alive, kept, turns=0, submitted=0)
        self.assertEqual(ev["outcome"], "interrupted")

    def test_resume_interruption_between_spawn_and_return(self):
        code, ev, raw, spy, alive, kept = self.probe_interrupted(on_spawn=2)
        self.check_owned(code, ev, raw, spy, alive, kept, turns=1, submitted=1)
        self.assertEqual(ev["outcome"], "interrupted")

    def test_constructor_interruption_on_both_routes(self):
        for on_call, turns in ((1, 0), (2, 1)):
            with self.subTest(route="fresh" if on_call == 1 else "resume"):
                r = self.probe(fail_construction(KeyboardInterrupt(), on_call=on_call), hello=True)
                self.check_owned(*r, turns=turns, submitted=turns)
                self.assertEqual(r[1]["outcome"], "interrupted")

    def test_an_unprovable_exit_is_reported_as_unconfirmed(self):
        """The writer won't confirm its exit: the evidence must say so (exit 5), and its folder
        is kept rather than removed from under a process that may still use it."""
        real_wait = subprocess.Popen.wait
        with Spy(self.claude) as spy:
            def wait(p, timeout=None):
                if p in spy.procs:
                    raise subprocess.TimeoutExpired(p.args, timeout)
                return real_wait(p, timeout)
            try:
                with fail_construction(ValueError("x"), on_call=1), \
                        mock.patch.object(subprocess.Popen, "wait", wait):
                    code, raw, _ = self.run_probe()
            finally:
                spy.reap()
        ev = json.loads(raw)
        self.assertEqual(code, 5)
        self.assertFalse(ev["cleanup"]["exit_confirmed"])
        self.assertFalse(ev["cleanup"]["tmp_removed"])
        self.assertEqual(ev["model_turns"], 0)

    def probe_interrupted(self, on_spawn):
        with Spy(self.claude) as spy:
            try:
                patches = [interrupt_after_spawn(spy, on_spawn)]
                if on_spawn == 2:
                    patches.append(self.hello_scene())
                for p in patches:
                    p.start()
                try:
                    code, raw, _ = self.run_probe()
                finally:
                    for p in patches:
                        p.stop()
                return code, json.loads(raw), raw, spy, spy.alive(), [t for t in spy.tmps if os.path.exists(t)]
            finally:
                spy.reap()


class AdapterLaunchOwnershipTests(ProbeFixBase):
    """The adapter alone (as the hand-off uses it, with no caller hold): a failure between spawn
    and return stops the writer and removes its folder before the error propagates."""

    def setUp(self):
        super().setUp()
        self.ws = os.path.realpath(os.path.join(self.tmp, "ws"))
        os.makedirs(self.ws)
        self.session = "0f5f2c1e-1111-4222-8333-444455556666"
        self.scene()
        self.adapter = CL.ClaudeStreamAdapter({"coding_sandbox": self.sandbox, "home_for_state": self.home})
        self.record = T.policy_record(self.sandbox.state)

    def start(self, route):
        if route == "fresh":
            return self.adapter.launch(self.ws, {"writer_policy": self.record, "session_id": self.session})
        path = CL.transcript_path(self.ws, self.session)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "w").close()
        return self.adapter.resume(self.ws, self.session, {"writer_policy": self.record})

    def test_constructor_failure_and_interruption_on_both_routes(self):
        for route in ("fresh", "resume"):
            for exc in (ValueError("boom"), KeyboardInterrupt()):
                with self.subTest(route=route, exc=type(exc).__name__):
                    with Spy(self.claude) as spy:
                        try:
                            with fail_construction(exc):
                                with self.assertRaises(type(exc)):
                                    self.start(route)
                            self.assertEqual(len(spy.procs), 1)
                            self.assertEqual(spy.alive(), [], "a writer was left running")
                            self.assertEqual([t for t in spy.tmps if os.path.exists(t)], [])
                        finally:
                            spy.reap()

    def test_a_caller_hold_sees_the_child_and_folder_before_return(self):
        hold = {}
        with Spy(self.claude) as spy:
            try:
                with fail_construction(ValueError("boom")):
                    with self.assertRaises(ValueError):
                        self.adapter.launch(self.ws, {"writer_policy": self.record, "session_id": self.session},
                                            hold=hold)
                self.assertIs(hold.get("proc"), spy.procs[0])
                self.assertEqual(hold.get("tmp"), spy.tmps[0])
                self.assertEqual(T.release_hold(hold), {"exit_confirmed": True, "tmp_removed": True})
            finally:
                spy.reap()


class ReleaseHoldTests(unittest.TestCase):
    def test_a_start_without_a_recorded_handle_is_unconfirmed_and_keeps_the_folder(self):
        tmp = T.tempfile.mkdtemp(prefix="relay-impl-")
        try:
            r = T.release_hold({"tmp": tmp, "spawning": True})
            self.assertEqual(r, {"exit_confirmed": False, "tmp_removed": False})
            self.assertTrue(os.path.isdir(tmp))
            r = T.release_hold({"tmp": tmp})  # failed before the child was started
            self.assertEqual(r, {"exit_confirmed": True, "tmp_removed": True})
        finally:
            T.cleanup_tmp(tmp)


if __name__ == "__main__":
    unittest.main()
