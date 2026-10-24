"""Bootstrap candidate and diagnostic classification (after live
state probe #2).

- The probe candidate gains exactly two READ-ONLY literals: {home}/.claude.json and
  {home}/.claude/settings.json. They can be read, never written; other conversations, other
  client files and home dotfiles stay unreadable; the conversation's own write scope is unchanged.
- Unknown denials are split by the SHAPE of their target (absent, redacted, unsupported format,
  unlisted client item, path outside the classifier's roots); devices, runtime and home items get
  fixed diagnostic categories. Every one still stops the run and none is ever proposed.
- None of this enables the coding route.

The sandbox tests run a stand-in program under the REAL profile the probe launches with (skipped
where there is no sandbox). No Claude, model or user session.
"""

import json
import os
import subprocess
import sys
import unittest
from unittest import mock

import util  # noqa: F401
from test_probe_scripts import SP, FakeCollector, StateProbeBase

from relay import toolpolicy as T
from relay.adapters import claude as CL

SESSION = "1c219fc2-0000-4000-8000-000000000001"

ACCESS = r'''
import json, os, sys
out = {}
for name, path in json.loads(sys.argv[1]).items():
    try:
        with open(path) as f:
            f.read()
        out[name + ":read"] = "ok"
    except OSError:
        out[name + ":read"] = "denied"
    try:
        with open(path, "a") as f:
            f.write("X")
        out[name + ":write"] = "ok"
    except OSError:
        out[name + ":write"] = "denied"
for name, path in (("config_tmp", sys.argv[2] + ".tmp.1"), ("settings_new", sys.argv[3] + ".new")):
    try:
        open(path, "x").close()
        out[name + ":create"] = "ok"
    except OSError:
        out[name + ":create"] = "denied"
try:
    os.unlink(sys.argv[2])
    out["config:unlink"] = "ok"
except OSError:
    out["config:unlink"] = "denied"
print(json.dumps(out))
'''


@unittest.skipIf(sys.platform != "darwin" or not os.access(T.ImplementerSandbox.EXE, os.X_OK), "no sandbox")
class CandidateSandboxTests(unittest.TestCase):
    """The probe's candidate, resolved and rendered exactly as ImplementerSandbox.launch does."""

    def setUp(self):
        import tempfile
        self._d = tempfile.TemporaryDirectory()
        d = os.path.realpath(self._d.name)
        self.home = os.path.join(d, "home")
        self.ws = os.path.join(self.home, "relay-runs", "r1", "ws")
        self.tmp = os.path.join(d, "relay-impl-t")
        os.makedirs(self.ws)
        os.makedirs(self.tmp)
        slug = os.path.basename(os.path.dirname(CL.transcript_path(self.ws, SESSION)))
        self.own = os.path.join(self.home, ".claude", "projects", slug, SESSION + ".jsonl")
        self.files = {
            "config": os.path.join(self.home, ".claude.json"),
            "settings": os.path.join(self.home, ".claude", "settings.json"),
            "own_transcript": self.own,
            "other_conversation": os.path.join(self.home, ".claude", "projects", "-other", "x.jsonl"),
            "own_folder_other_file": os.path.join(os.path.dirname(self.own), "SECRET-other.jsonl"),
            "settings_local": os.path.join(self.home, ".claude", "settings.local.json"),
            "credentials": os.path.join(self.home, ".claude", ".credentials.json"),
            "plugins_index": os.path.join(self.home, ".claude", "plugins", "installed_plugins.json"),
            "gitconfig": os.path.join(self.home, ".gitconfig"),
            "ancestor_claude_md": os.path.join(self.home, "relay-runs", "r1", "CLAUDE.md"),
        }
        for p in self.files.values():
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w") as f:
                f.write("orig")

    def tearDown(self):
        self._d.cleanup()

    def run_under_candidate(self):
        state = T.StateAllowlist(SP.CANDIDATE).resolve(self.ws, SESSION, self.home)
        prof = T.profile(self.ws, state=state, tmp=self.tmp, install=[])
        r = subprocess.run([T.ImplementerSandbox.EXE, "-p", prof, sys.executable, "-I", "-c", ACCESS,
                            json.dumps(self.files), self.files["config"], self.files["settings"]],
                           capture_output=True, timeout=30, stdin=subprocess.DEVNULL, cwd=self.ws)
        self.assertEqual(r.returncode, 0, r.stderr.decode(errors="replace")[-300:])
        return state, json.loads(r.stdout.decode().strip().splitlines()[-1])

    def test_the_two_bootstrap_files_are_readable_but_never_writable(self):
        state, got = self.run_under_candidate()
        self.assertEqual(state["read_files"], [self.files["config"], self.files["settings"]])
        for name in ("config", "settings"):
            self.assertEqual((got[name + ":read"], got[name + ":write"]), ("ok", "denied"), name)
            with open(self.files[name]) as f:
                self.assertEqual(f.read(), "orig")  # unchanged
        # No write path next to them either: no temp/rename-style replacement, no removal.
        self.assertEqual((got["config_tmp:create"], got["settings_new:create"], got["config:unlink"]),
                         ("denied", "denied", "denied"))
        self.assertTrue(os.path.exists(self.files["config"]))

    def test_the_conversations_own_write_scope_is_unchanged(self):
        state, got = self.run_under_candidate()
        self.assertEqual(state["write_files"], [os.path.dirname(self.own), self.own])
        self.assertEqual((state["write_dirs"], state["read_dirs"]), ([], []))
        self.assertEqual((got["own_transcript:read"], got["own_transcript:write"]), ("ok", "ok"))

    def test_other_conversations_and_other_home_files_stay_inaccessible(self):
        _, got = self.run_under_candidate()
        for name in ("other_conversation", "own_folder_other_file", "settings_local", "credentials",
                     "plugins_index", "gitconfig", "ancestor_claude_md"):
            with self.subTest(name):
                self.assertEqual((got[name + ":read"], got[name + ":write"]), ("denied", "denied"))


class DiagnosticClassificationTests(unittest.TestCase):
    """Fixed categories only; each is still a stop; no raw path or log text in any output."""

    CTX = {"home": "/Users/u", "slug": "-Users-u-relay-runs-r1-ws", "session": "S1",
           "ws": "/Users/u/relay-runs/r1/ws", "tmp": "/t/relay-impl-x"}

    def c(self, op, target):
        got = SP.classify(op, target, self.CTX)
        self.assertNotIn("SECRET", json.dumps(got))
        return got

    def test_unknown_targets_are_split_by_shape_and_still_stop(self):
        for target, cat in (("", "unknown: no target"), ("   ", "unknown: no target"),
                            ("<private>", "unknown: redacted target"),
                            ("/Users/u/<private>", None),  # an absolute path: classified as a path
                            ("SECRET relative/path", "unknown: unsupported target format"),
                            ("/Users/u/.claude/SECRET-new-item", "unknown: unlisted client folder item"),
                            ("/SECRET/elsewhere", "unknown: path outside the classifier's roots")):
            with self.subTest(target=target):
                got = self.c("file-write-data", target)
                if cat is None:
                    continue
                self.assertEqual(got, (cat, None, "unknown"))

    def test_devices_get_fixed_categories_and_still_stop(self):
        for target, want in (("/dev/dtracehelper", ("device: dtracehelper", "/dev/dtracehelper")),
                             ("/dev/autofs_nowait", ("device: autofs_nowait", "/dev/autofs_nowait")),
                             ("/dev/ttys003", ("device: terminal", "/dev/ttys<n>")),
                             ("/dev/fd/7", ("device: fd", "/dev/fd/<n>")),
                             ("/dev/SECRET-disk", ("device: other", "/dev/<other>"))):
            with self.subTest(target):
                self.assertEqual(self.c("file-write-data", target), (*want, "stop"))

    def test_temp_and_var_db_observations_are_named_not_allowed(self):
        cases = (("/private/tmp/claude/SECRET", ("client default temp", "/private/tmp/claude/<...>")),
                 ("/private/tmp/claude", ("client default temp", "/private/tmp/claude")),
                 ("/private/tmp/SECRET", ("system: /private/tmp", "/private/tmp/<...>")),
                 ("/private/var/db/timezone/zoneinfo/SECRET", ("runtime: time zone data",
                                                               "/private/var/db/timezone/<...>")),
                 ("/private/var/db/SECRET", ("system: /private/var/db", "/private/var/db/<...>")),
                 ("/tmp/SECRET", ("system: /tmp", "/tmp/<...>")))
        for target, want in cases:
            with self.subTest(target):
                self.assertEqual(self.c("file-read-data", target), (*want, "stop"))

    def test_home_reads_get_fixed_categories_and_still_stop(self):
        cases = (("/Users/u/.CFUserTextEncoding", ("runtime: text encoding file", "{home}/.CFUserTextEncoding")),
                 ("/Users/u/.gitconfig", ("client: git config", "{home}/.gitconfig")),
                 ("/Users/u/.config/git/SECRET", ("client: git config", "{home}/.config/git/<...>")),
                 ("/Users/u/.mcp.json", ("client: MCP config", "{home}/.mcp.json")),
                 ("/Users/u/relay-runs/r1/CLAUDE.md", ("workspace ancestor: CLAUDE.md", "{ancestor}/CLAUDE.md")),
                 ("/Users/u/relay-runs/.claude/SECRET.json", ("workspace ancestor: .claude", "{ancestor}/.claude/<...>")),
                 ("/Users/u/CLAUDE.md", ("workspace ancestor: CLAUDE.md", "{ancestor}/CLAUDE.md")),
                 ("/Users/u/relay-runs/r1", ("workspace ancestor folder", "{ancestor}")),
                 ("/Users/u/relay-runs/SECRET-other-run/CLAUDE.md", ("home: other", "{home}/<other>")),
                 ("/Users/u/SECRET-Documents/x.pdf", ("home: other", "{home}/<other>")))
        for target, want in cases:
            with self.subTest(target):
                self.assertEqual(self.c("file-read-data", target), (*want, "stop"))

    def test_the_clients_own_areas_are_unchanged(self):
        self.assertEqual(self.c("file-write-data", "/Users/u/.claude.json"),
                         ("client config file", "{home}/.claude.json", "client"))
        self.assertEqual(self.c("file-read-data", "/Users/u/.claude/settings.json"),
                         ("client settings.json", "{home}/.claude/settings.json", "client"))
        self.assertEqual(self.c("file-read-data", "/Users/u/.claude/projects/-other/x.jsonl")[0],
                         "another conversation's folder")


class ProposalTests(unittest.TestCase):
    def test_diagnostics_and_bootstrap_writes_are_never_proposed(self):
        denials = {("device: dtracehelper", "/dev/dtracehelper", "file-write-data"): 1,
                   ("runtime: time zone data", "/private/var/db/timezone/<...>", "file-read-data"): 1,
                   ("runtime: text encoding file", "{home}/.CFUserTextEncoding", "file-read-data"): 1,
                   ("client: git config", "{home}/.gitconfig", "file-read-data"): 1,
                   ("workspace ancestor: CLAUDE.md", "{ancestor}/CLAUDE.md", "file-read-data"): 1,
                   ("client default temp", "/private/tmp/claude", "file-write-create"): 1,
                   ("unknown: no target", None, "file-write-data"): 2,
                   ("unknown: redacted target", None, "file-write-data"): 1,
                   ("client config file", "{home}/.claude.json", "file-write-data"): 3,
                   ("client settings.json", "{home}/.claude/settings.json", "file-write-data"): 1}
        prop = SP.propose(denials)
        self.assertEqual(prop["proven_by"], "")
        self.assertEqual(prop["read_files"], [])  # nothing beyond the client's own reads is proposed
        self.assertEqual(prop["write_files"], sorted(SP.CANDIDATE["write_files"]))
        self.assertEqual(len(prop["needs_review"]), len(denials))
        T.StateAllowlist({k: prop[k] for k in T.StateAllowlist.FIELDS})  # still a valid allowlist


class UnknownWriteStopTests(StateProbeBase):
    """End to end (fake client, real sandbox, fake collector): an unknown write still stops the
    run in turn 1, is recorded only by its shape, and is never proposed."""

    def test_each_unknown_write_shape_stops_the_run(self):
        for target, cat in (("", "unknown: no target"), ("<private>", "unknown: redacted target"),
                            ("SECRET:format", "unknown: unsupported target format"),
                            ("{home}/.claude/SECRET-item", "unknown: unlisted client folder item"),
                            ("/SECRET/place", "unknown: path outside the classifier's roots")):
            with self.subTest(cat):
                col = FakeCollector(raw=[{"op": "file-write-data", "target": target}])
                code, raw, prop = self.run_probe(collector=col)
                ev = json.loads(raw)
                self.assertEqual(code, 2)
                self.assertEqual(len(ev["turns"]), 1)  # no T2
                self.assertIn(f"denial outside the allowed observation areas: {cat}", ev["stops"])
                self.assertIn({"category": cat, "template": None, "op": "file-write-data", "count": 1},
                              ev["denials"])
                self.assertNotIn("SECRET", raw)
                self.assertNotIn("SECRET", json.dumps(prop))
                self.assertIn(f"{cat} (file-write-data)", prop["needs_review"])
                self.assertEqual(prop["write_files"], sorted(SP.CANDIDATE["write_files"]))
                self.assertEqual(ev["turns"][0]["cleanup"], {"exit_confirmed": True, "tmp_removed": True})


class CodingStaysDisabledTests(unittest.TestCase):
    def test_the_candidate_enables_nothing(self):
        self.assertNotIn("proven_by", SP.CANDIDATE)
        self.assertIsNone(T.StateAllowlist.pinned(SP.VERSION))  # nothing pinned for this version
        self.assertFalse(os.path.exists(os.path.join(T.STATE_DIR, f"claude-state-{SP.VERSION}.json")))
        with mock.patch("relay.adapters.claude._claude", return_value="/x/versions/" + SP.VERSION):
            why = CL.ClaudeStreamAdapter({}).coding_sandbox().available()
        self.assertIn("unavailable", why or "")

    def test_layer_one_controls_are_unchanged(self):
        argv = T.coding_argv("claude", session_id=SESSION)
        for flag in ("--restricted", "--strict-mcp-config"):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "acceptEdits")
        self.assertEqual(argv[argv.index("--tools") + 1], "Read,Edit,Write,Glob,Grep")
        self.assertEqual(SP.CANDIDATE["read_files"], ["{home}/.claude.json", "{home}/.claude/settings.json"])
        self.assertEqual((SP.CANDIDATE["read_dirs"], SP.CANDIDATE["write_dirs"]), ([], []))


if __name__ == "__main__":
    unittest.main()
