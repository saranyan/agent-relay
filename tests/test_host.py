"""Session console (relay serve): catalog, manage with AI, intervene/say/resume/release,
and the guards on its action endpoints. Fake sessions only."""

import http.client
import json
import os
import threading
import time

from util import RelayTest

from relay.bus import Bus
from relay.host import Host
from relay.observer import Discovery
from relay.server import ObserverServer
from relay.storage import init_root


class HostTests(RelayTest):
    def setUp(self):
        super().setUp()
        # A console root uses open participant labels (one per session).
        self.root = init_root(os.path.join(self.tmp, "console-root"), ["*"])
        self.bus = Bus(self.root)
        self.discovery = Discovery({"home": self.home}, ("fake",), interval=60)
        self.host = Host(self.bus, self.discovery, new_session_adapters={"claude": "fake", "codex": "fake"},
                         adapter_options={"home": self.home})
        self.srv = ObserverServer(self.bus, self.discovery, 0, host=self.host)
        threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True).start()
        self.base = f"127.0.0.1:{self.srv.server_port}"

    def tearDown(self):
        self.host.shutdown()
        self.srv.shutdown()
        self.srv.server_close()
        super().tearDown()

    # ------------------------------------------------------------ helpers
    def session(self, sid, name, client, **default):
        default.setdefault("natural", True)
        return self.fake(sid, default=default, seed_history=2, name=name, client=client)

    def call(self, method, path, body=None, *, token=True, origin=True, ctype="application/json", raw=None):
        c = http.client.HTTPConnection("127.0.0.1", self.srv.server_port, timeout=15)
        h = {"Host": self.base}
        if token:
            h["X-Relay-Token"] = self.srv.token
        if origin:
            h["Origin"] = f"http://{self.base}"
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        if data is not None:
            h["Content-Type"] = ctype
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        out = r.read()
        c.close()
        return r.status, (json.loads(out) if out else None)

    def catalog(self):
        self.discovery.refresh_once()
        return {s["name"]: s for s in self.host.catalog()["sessions"]}

    def wait(self, pred, timeout=20):
        end = time.time() + timeout
        while time.time() < end:
            if pred():
                return True
            time.sleep(0.1)
        raise AssertionError("timed out")

    # ------------------------------------------------------------ catalog
    def test_catalog_lists_sessions_in_plain_terms(self):
        self.session("s1", "payments-api", "Claude")
        self.session("s2", "web-app", "Codex")
        cat = self.catalog()
        self.assertEqual(set(cat), {"payments-api", "web-app"})
        p = cat["payments-api"]
        self.assertEqual((p["client"], p["state"], p["doing"]), ("Claude", "idle", "Idle"))
        self.assertEqual(p["managed_by"], {"kind": "none"})
        self.assertTrue(p["can_manage"])
        names = [x["name"] for x in self.host.catalog()["personalities"]]
        self.assertEqual(names, ["Reviewer", "Planner", "Pair programmer"])

    # ------------------------------------------------------------ actions
    def test_action_guards(self):
        self.session("s1", "payments-api", "Claude")
        key = self.catalog()["payments-api"]["key"]
        body = {"key": key, "manager": {"new": {"client": "claude"}}, "goal": "x"}
        self.assertEqual(self.call("POST", "/api/manage", body, token=False)[0], 403)
        self.assertEqual(self.call("POST", "/api/manage", body, origin=False)[0], 403)
        self.assertEqual(self.call("POST", "/api/manage", raw=b"key=x", ctype="text/plain")[0], 415)
        self.assertEqual(self.call("POST", "/api/manage", raw=b"[" * 70000)[0], 413)
        self.assertEqual(self.call("POST", "/api/manage", raw=b"{nope")[0], 400)
        self.assertEqual(self.call("POST", "/api/sessions", body)[0], 405)  # reading endpoint
        self.assertEqual(self.call("POST", "/api/manage", dict(body, goal=" "))[0], 400)
        self.host.read_only = True
        self.assertEqual(self.call("POST", "/api/manage", body)[0], 405)
        self.assertEqual(self.bus.pair_ids(), [])  # nothing was created by any refused call

    def test_manage_with_new_manager_then_intervene_say_and_release(self):
        follower = self.session("s1", "payments-api", "Claude", sleep=1.5)
        key = self.catalog()["payments-api"]["key"]
        st, r = self.call("POST", "/api/manage", {"key": key, "goal": "Retry on timeouts.",
                                                  "manager": {"new": {"client": "codex", "personality": "planner"}}})
        self.assertEqual(st, 200, r)
        pid = r["team"]
        self.assertEqual(r["manager"], "Planner")
        cat = self.catalog()
        self.assertEqual(cat["payments-api"]["managed_by"]["kind"], "ai")
        self.assertEqual(cat["payments-api"]["managed_by"]["manager"], "Planner")
        self.assertEqual(cat["Planner"]["managed_by"], {"kind": "manager", "managing": "payments-api"})
        brief = self.host.console(pid)["messages"][0]
        self.assertTrue(brief["brief"])
        self.assertIn("break the goal into", brief["body"])  # the planner's style reached the manager

        # wait until the session (follower) is working on a turn, then step in
        self.wait(lambda: self.host.console(pid)["working"] == "follower")
        st, r = self.call("POST", f"/api/team/{pid}/intervene", {})
        self.assertEqual(st, 200)
        c = self.host.console(pid)
        self.assertTrue(c["in_control"])
        self.assertTrue(c["finishing_turn"])
        st, r = self.call("POST", f"/api/team/{pid}/say", {"text": "Use backoff."})
        self.assertEqual(st, 409)  # the running turn finishes first
        self.wait(lambda: self.host.console(pid)["can_send"])
        sent_before = follower.context()["routed"]
        st, r = self.call("POST", f"/api/team/{pid}/say", {"text": "Use exponential backoff, max 3 tries."})
        self.assertEqual(st, 200, r)
        self.assertEqual(r["to"], "follower")
        self.wait(lambda: follower.context()["routed"] == sent_before + 1)
        msgs = self.host.console(pid)["messages"]
        mine = [m for m in msgs if m["role"] == "you" and not m["brief"]]
        self.assertEqual(mine[-1]["body"], "Use exponential backoff, max 3 tries.")
        self.assertFalse(self.host.console(pid)["in_control"])  # handed back

        st, r = self.call("POST", f"/api/team/{pid}/release", {})
        self.assertEqual(st, 200)
        cat = self.catalog()
        self.assertEqual(cat["payments-api"]["managed_by"], {"kind": "none"})
        self.assertNotIn("Planner", cat)  # the manager the relay started is closed and hidden
        self.assertTrue(follower.alive())  # your session keeps running

    def test_hand_back_without_saying_anything(self):
        self.session("s1", "payments-api", "Claude", sleep=1.0)
        key = self.catalog()["payments-api"]["key"]
        _, r = self.call("POST", "/api/manage", {"key": key, "goal": "Tidy the README.",
                                                 "manager": {"new": {"client": "claude"}}})
        pid = r["team"]
        self.call("POST", f"/api/team/{pid}/intervene", {})
        self.wait(lambda: not self.host.console(pid)["finishing_turn"])
        st, _ = self.call("POST", f"/api/team/{pid}/resume", {})
        self.assertEqual(st, 200)
        self.assertFalse(self.host.console(pid)["in_control"])
        self.wait(lambda: self.host.console(pid)["terminal"], timeout=40)
        self.assertEqual(self.host.console(pid)["status"], "finished")
        self.assertEqual(self.catalog()["payments-api"]["state"], "done")

    def test_manage_with_existing_session_as_manager(self):
        self.session("s1", "payments-api", "Claude")
        self.session("s2", "infra-scripts", "Codex", finish_after=1)
        cat = self.catalog()
        st, r = self.call("POST", "/api/manage", {"key": cat["payments-api"]["key"], "goal": "Add a health check.",
                                                  "manager": {"existing": cat["infra-scripts"]["key"]}})
        self.assertEqual(st, 200, r)
        self.assertEqual(r["manager"], "infra-scripts")
        self.wait(lambda: self.host.console(r["team"])["terminal"], timeout=30)
        self.assertNotIn("Reviewer", self.catalog())  # nothing new was started

    def test_console_names_the_chosen_existing_manager_unambiguously(self):
        self.session("s1", "payments-api", "Claude", sleep=30)
        self.session("s2", "agent-relay", "Codex", sleep=30)
        self.session("s3", "agent-relay", "Claude", sleep=30)
        cat = self.catalog()
        st, r = self.call("POST", "/api/manage", {"key": cat["payments-api"]["key"], "goal": "Add a health check.",
                                                  "manager": {"existing": cat["agent-relay · Codex"]["key"]}})
        self.assertEqual(st, 200, r)
        self.assertEqual(r["manager"], "agent-relay · Codex")
        c = self.host.console(r["team"])  # before any further catalog refresh
        self.assertEqual((c["manager"]["name"], c["manager"]["client"]), ("agent-relay · Codex", "Codex"))
        self.assertEqual(c["session"]["name"], "payments-api")

    def test_cannot_manage_what_cannot_be_driven(self):
        self.session("s1", "payments-api", "Claude")
        cat = self.catalog()
        st, r = self.call("POST", "/api/manage", {"key": "d:fake:nope", "goal": "x",
                                                  "manager": {"new": {"client": "claude"}}})
        self.assertEqual(st, 404)
        st, r = self.call("POST", "/api/manage", {"key": cat["payments-api"]["key"], "goal": "x",
                                                  "manager": {"existing": cat["payments-api"]["key"]}})
        self.assertEqual(st, 409)  # a session can't manage itself
        st, r = self.call("POST", "/api/manage", {"key": cat["payments-api"]["key"], "goal": "x",
                                                  "manager": {"new": {"client": "claude", "personality": "boss"}}})
        self.assertEqual(st, 400)
