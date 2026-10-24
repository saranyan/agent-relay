"""Storage: atomic publication, safe paths, stream integrity, views (coverage 1, 6, 12)."""

import json
import os
import stat
import subprocess
import sys

from util import REPO, RelayTest, relay, relay_json

from relay.storage import (Conflict, InvalidInput, NotFound, Root, StreamUntrusted, check_name,
                           init_root)

WRITER = r"""
import sys
sys.path.insert(0, sys.argv[1])
from relay.storage import Root
from relay.bus import Bus
b = Bus(Root(sys.argv[2]))
for i in range(int(sys.argv[3])):
    b.send(sender="codex", to="claude", kind="status", subject=f"w{sys.argv[4]}-{i}", body="x" * 2000)
"""


class StorageTests(RelayTest):
    def test_parallel_writers_unique_and_complete(self):
        procs = [subprocess.Popen([sys.executable, "-c", WRITER, REPO, self.root.path, "15", str(w)])
                 for w in range(8)]
        for p in procs:
            self.assertEqual(p.wait(timeout=60), 0)
        msgs, errors = self.bus.all_messages()
        self.assertEqual(errors, [])
        self.assertEqual(len(msgs), 120)
        self.assertEqual(len({m["id"] for m in msgs}), 120)
        self.assertEqual({m["subject"] for m in msgs}, {f"w{w}-{i}" for w in range(8) for i in range(15)})
        self.assertEqual(os.listdir(self.root.p("tmp")), [])  # no partial files left behind

    def test_publish_is_exclusive(self):
        self.assertTrue(self.root.publish(("views", "x.json"), b"1"))
        self.assertFalse(self.root.publish(("views", "x.json"), b"2"))
        self.assertEqual(self.root.read_bytes("views", "x.json"), b"1")

    def test_owner_only_permissions(self):
        env, _, _ = self.bus.send(sender="codex", to="claude", kind="task", subject="s", body="b")
        mode = stat.S_IMODE(os.stat(self.root.p("messages", env["id"] + ".json")).st_mode)
        self.assertEqual(mode & 0o077, 0)
        self.assertEqual(stat.S_IMODE(os.stat(self.root.p("messages")).st_mode) & 0o077, 0)

    def test_invalid_names_rejected(self):
        for bad in ["../x", "a/b", "", ".hidden", "x" * 200, "a\x00b", "..", "é"]:
            with self.assertRaises(InvalidInput, msg=bad):
                check_name(bad)
        with self.assertRaises(InvalidInput):
            self.bus.get_message("../../etc/passwd")
        r = relay(self.tmp, "read", "../config")
        self.assertEqual(r.returncode, 6)

    def test_symlink_escapes_refused(self):
        outside = os.path.join(self.tmp, "outside")
        os.makedirs(outside)
        with open(os.path.join(outside, "m_evil.json"), "w") as f:
            json.dump({"schema_version": 1}, f)
        os.symlink(os.path.join(outside, "m_evil.json"), self.root.p("messages") + "/m_evil.json")
        with self.assertRaises(InvalidInput):
            self.bus.get_message("m_evil")
        msgs, errors = self.bus.all_messages()
        self.assertEqual(msgs, [])
        self.assertEqual(len(errors), 1)
        # a symlinked stream directory is refused too
        os.symlink(outside, self.root.p("streams") + "/msg-m_x")
        with self.assertRaises(InvalidInput):
            self.root.read_stream("msg-m_x")
        with self.assertRaises(InvalidInput):
            self.root.append("msg-m_x", lambda ev: ("claimed", {}), {})
        self.assertEqual(sorted(os.listdir(outside)), ["m_evil.json"])  # nothing written outside

    def test_root_requires_init_and_explicit_path(self):
        with self.assertRaises(NotFound):
            Root(os.path.join(self.tmp, "nope"))
        r = subprocess.run([sys.executable, "-m", "relay", "status"], cwd=REPO,
                           env={k: v for k, v in os.environ.items() if k != "AGENT_RELAY_ROOT"}
                           | {"PYTHONPATH": REPO}, capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)
        with self.assertRaises(Conflict):
            init_root(self.root.path, ["codex", "other"])

    def test_stream_damage_is_untrusted_and_isolated(self):
        a, _, _ = self.bus.send(sender="codex", to="claude", kind="task", subject="a", body="a")
        b, _, _ = self.bus.send(sender="codex", to="claude", kind="task", subject="b", body="b")
        tok = self.bus.claim(a["id"], "claude")["token"]
        self.bus.ack(a["id"], tok)
        self.bus.claim(b["id"], "claude")
        path = self.root.p("streams", f"msg-{a['id']}", "00000001.json")
        os.chmod(path, 0o600)
        data = json.load(open(path))
        data["data"]["by"] = "codex"  # tamper with history
        with open(path, "w") as f:
            json.dump(data, f)
        with self.assertRaises(StreamUntrusted):
            self.bus.msg_state(a["id"])
        self.assertEqual(self.bus.msg_state(b["id"])["state"], "claimed")  # unaffected
        st = self.bus.status()
        self.assertTrue(any(a["id"] in str(e) for e in st["errors"]))
        self.assertTrue(os.path.exists(path))  # evidence is never deleted
        r = relay(self.tmp, "claim", a["id"], "--as", "claude")
        self.assertEqual(r.returncode, 8)

    def test_stream_gap_and_unknown_schema(self):
        m, _, _ = self.bus.send(sender="codex", to="claude", kind="task", subject="a", body="a")
        self.bus.claim(m["id"], "claude")
        d = self.root.p("streams", f"msg-{m['id']}")
        with open(os.path.join(d, "00000003.json"), "w") as f:
            f.write("{}")
        with self.assertRaises(StreamUntrusted):
            self.bus.msg_state(m["id"])

    def test_malformed_and_unknown_version_messages_do_not_hide_valid(self):
        good, _, _ = self.bus.send(sender="codex", to="claude", kind="task", subject="ok", body="b")
        with open(self.root.p("messages", "m_bad1.json"), "w") as f:
            f.write("{not json")
        env = dict(good, id="m_bad2", schema_version=99)
        with open(self.root.p("messages", "m_bad2.json"), "w") as f:
            json.dump(env, f)
        msgs, errors = self.bus.all_messages()
        self.assertEqual([m["id"] for m in msgs], [good["id"]])
        self.assertEqual({e["message"] for e in errors}, {"m_bad1", "m_bad2"})
        _, inbox = relay_json(self.tmp, "inbox", "--for", "claude")
        self.assertEqual([m["id"] for m in inbox["messages"]], [good["id"]])
        self.assertEqual(len(inbox["errors"]), 2)
        self.assertTrue(os.path.exists(self.root.p("messages", "m_bad1.json")))

    def test_rebuild_views_from_streams(self):
        self.bus.pause("check")
        os.makedirs(self.root.p("views"), exist_ok=True)
        with open(self.root.p("views", "control.json"), "w") as f:
            f.write('{"paused": false}')  # a stale/wrong cache
        _, out = relay_json(self.tmp, "rebuild")
        self.assertIn("views/control.json", out["rebuilt"])
        with open(self.root.p("views", "control.json")) as f:
            self.assertTrue(json.load(f)["paused"])
        _, st = relay_json(self.tmp, "status")
        self.assertTrue(st["control"]["paused"])
