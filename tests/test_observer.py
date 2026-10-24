"""M1 read-only observer: access guards, state model, no side effects, discovery."""

import http.client
import json
import os
import threading

from util import REPO, RelayTest

from relay.observer import Discovery, Timeline, build_state
from relay.server import ObserverServer
from relay.storage import actor


class ObserverTests(RelayTest):
    def setUp(self):
        super().setUp()
        self.discovery = Discovery({"home": self.home}, ("fake",), interval=60)
        self.srv = ObserverServer(self.bus, self.discovery, 0)
        threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.1},
                         daemon=True).start()
        self.port = self.srv.server_port

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        super().tearDown()

    def req(self, path, *, method="GET", token=True, host=None, header=True):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {"Host": host or f"127.0.0.1:{self.port}"}
        if token and header:
            headers["X-Relay-Token"] = self.srv.token
        if token and not header:
            path += ("&" if "?" in path else "?") + "t=" + self.srv.token
        c.request(method, path, headers=headers)
        r = c.getresponse()
        body = r.read()
        c.close()
        return r, body

    def state(self):
        r, body = self.req("/api/state")
        self.assertEqual(r.status, 200)
        return json.loads(body)

    # ------------------------------------------------------------- guards
    def test_access_guards(self):
        self.assertEqual(self.req("/api/state", token=False)[0].status, 403)
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request("GET", "/api/state", headers={"X-Relay-Token": "wrong"})
        self.assertEqual(c.getresponse().status, 403)
        self.assertEqual(self.req("/api/state", host=f"evil.example:{self.port}")[0].status, 403)
        for m in ("POST", "PUT", "DELETE", "PATCH"):
            self.assertEqual(self.req("/api/state", method=m)[0].status, 405)
        self.assertEqual(self.srv.server_address[0], "127.0.0.1")
        r, body = self.req("/", header=False)
        self.assertEqual(r.status, 200)
        self.assertIn(self.srv.token.encode(), body)  # asset links carry the token
        self.assertIn("default-src 'none'", r.getheader("Content-Security-Policy"))
        self.assertEqual(r.getheader("Cache-Control"), "no-store")
        self.assertIsNone(r.getheader("Access-Control-Allow-Origin"))
        self.assertEqual(self.req("/api/thread/..%2F..%2Fconfig")[0].status, 400)

    def test_page_script_renders_text_only(self):
        with open(os.path.join(REPO, "relay", "web", "app.js")) as f:
            js = f.read()
        for risky in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
            self.assertNotIn(risky, js)

    # -------------------------------------------------------------- state
    def test_state_reports_teams_needs_and_sessions_without_writing(self):
        lf = self.fake("codex-live", default={"finish_after": 1})
        leader = self.attach_endpoint("codex-live", "codex")
        follower = self.launch_endpoint("claude", script=[{"action": "ask_human",
                                                           "text": "Which directory?"}])
        pid = self.pair(leader, follower)
        self.bus.start_pair(pid, body="go")
        c = self.controller(pid)
        c.run(max_seconds=20)
        spare = self.launch_endpoint("codex")
        self.discovery.refresh_once()

        before = {s: len(self.root.listdir("streams", s)) for s in self.root.listdir("streams")}
        s1, s2 = self.state(), self.state()
        after = {s: len(self.root.listdir("streams", s)) for s in self.root.listdir("streams")}
        self.assertEqual(before, after)  # observing never appends an event
        self.assertTrue(s1["read_only"])

        team = next(t for t in s2["teams"] if t["id"] == pid)
        self.assertEqual(team["status"], "waiting_human")
        self.assertEqual([m["role"] for m in team["members"]], ["leader", "follower"])
        need = next(n for n in s2["needs_you"] if n.get("team") == pid)
        self.assertEqual(need["kind"], "waiting_human")
        self.assertIn(f"relay pair answer {pid}", need["commands"][0])
        by_id = {x["id"]: x for x in s2["sessions"]}
        self.assertEqual(by_id[leader]["team"], pid)
        self.assertEqual(by_id[leader]["verified_client_session_id"], "codex-live")
        self.assertIsNone(by_id[spare]["team"])
        self.assertIsNone(by_id[spare]["verified_client_session_id"])  # never launched
        types = [e["type"] for e in s2["timeline"]]
        for t in ("created", "started", "dispatch_authorized", "accepted", "response_captured"):
            self.assertIn(t, types)
        self.assertFalse(any("ctl" in e["data"] or "token" in e["data"] for e in s2["timeline"]))

        r, body = self.req(f"/api/thread/{team['thread_id']}")
        msgs = json.loads(body)["messages"]
        self.assertEqual([m["from"] for m in msgs], ["codex", "claude"])

    def test_discovery_separates_observed_from_managed(self):
        self.fake("unmanaged-1", seed_history=2)
        self.fake("managed-1")
        self.attach_endpoint("managed-1", "codex")
        self.discovery.refresh_once()
        s = self.state()
        self.assertEqual([o["client_session_id"] for o in s["observed"]], ["unmanaged-1"])
        self.assertIsNone(s["discovery"]["fake"]["error"])
        broken = Discovery({}, ("fake", "claude-agents-missing-adapter"), interval=60)
        broken.refresh_once()  # errors are recorded, never raised
        self.assertTrue(all(v["error"] for v in broken.snapshot().values()))

    def test_global_pause_and_untrusted_pair_surface_in_needs_you(self):
        self.bus.pause("founder review")
        leader = self.launch_endpoint("codex")
        follower = self.launch_endpoint("claude")
        pid = self.pair(leader, follower)
        self.root.append(f"pair-{pid}", lambda ev: ("queued", {"nonsense": True}), actor("human"))
        s = self.state()
        kinds = [n["kind"] for n in s["needs_you"]]
        self.assertEqual(kinds[0], "global_pause")
        self.assertIn("untrusted", kinds)
        self.assertEqual(next(t for t in s["teams"] if t["id"] == pid)["status"], "untrusted")

    def test_timeline_rereads_only_grown_streams(self):
        tl = Timeline(self.bus)
        self.bus.pause("x")
        e1, _ = tl.events()
        cached = tl.cache["control"]
        e2, _ = tl.events()
        self.assertIs(tl.cache["control"], cached)
        self.bus.resume()
        e3, _ = tl.events()
        self.assertEqual(len(e3), len(e1) + 1)
