"""`relay serve`: foreground, loopback-only dashboard (M1: read-only observer).

Security posture:
* binds 127.0.0.1 only, on a random port unless one is given;
* every request needs the one-time token printed at startup (query `t=` for the
  page and static files, `X-Relay-Token` header for the API);
* the Host header must name this loopback address (DNS-rebinding guard);
* GET/HEAD only in M1 — nothing served here can change relay state;
* no CORS headers, `Cache-Control: no-store`, strict CSP, no third-party assets.
It runs only while the command runs in the foreground; Ctrl-C stops it.
"""

from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .bus import Bus
from .host import Host
from .observer import Discovery, Timeline, build_state
from .storage import RelayError, check_name

MAX_BODY = 64 * 1024
DRAIN_LIMIT = 1024 * 1024  # an oversized body up to this size is read and dropped before the 413

WEB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
STATIC = {"/": ("index.html", "text/html; charset=utf-8"),
          "/app.js": ("app.js", "text/javascript; charset=utf-8"),
          "/app.css": ("app.css", "text/css; charset=utf-8")}
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
       "img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


class ObserverServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, bus: Bus, discovery: Discovery | None, port: int = 0,
                 token: str | None = None, host: Host | None = None):
        super().__init__(("127.0.0.1", port), Handler)
        self.bus, self.discovery = bus, discovery
        self.host = host or Host(bus, discovery, read_only=True)
        self.timeline = Timeline(bus)
        self.token = token or secrets.token_urlsafe(24)
        self.allowed_hosts = {f"127.0.0.1:{self.server_port}", f"localhost:{self.server_port}"}
        self.allowed_origins = {f"http://{h}" for h in self.allowed_hosts}

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_port}/?t={self.token}"


class Handler(BaseHTTPRequestHandler):
    server: ObserverServer
    server_version = "agent-relay"
    sys_version = ""

    def log_message(self, fmt, *args):  # keep the terminal quiet; no tokens in logs
        pass

    # -------------------------------------------------------------- guards
    def _authorized(self, query: dict) -> bool:
        if self.headers.get("Host") not in self.server.allowed_hosts:
            return False
        supplied = self.headers.get("X-Relay-Token") or (query.get("t") or [""])[0]
        return hmac.compare_digest(supplied.encode(), self.server.token.encode())

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _discard(self, n: int) -> None:
        self.connection.settimeout(2)
        try:
            while n > 0:
                chunk = self.rfile.read(min(n, 65536))
                if not chunk:
                    break
                n -= len(chunk)
        except OSError:
            pass

    def _json(self, code: int, obj) -> None:
        self._send(code, json.dumps(obj, default=str).encode(), "application/json")

    # ------------------------------------------------------------- methods
    def do_GET(self):
        url = urlparse(self.path)
        query = parse_qs(url.query)
        if not self._authorized(query):
            return self._json(HTTPStatus.FORBIDDEN, {"error": "forbidden"})
        path = url.path
        try:
            if path in STATIC:
                name, ctype = STATIC[path]
                with open(os.path.join(WEB, name), "rb") as f:
                    body = f.read()
                if name == "index.html":  # asset links must carry the token too
                    body = body.replace(b"__TOKEN__", self.server.token.encode())
                return self._send(200, body, ctype)
            if path == "/api/state":
                return self._json(200, build_state(self.server.bus, self.server.discovery,
                                                   self.server.timeline))
            if path == "/api/sessions":
                return self._json(200, self.server.host.catalog())
            if path == "/api/notify":
                return self._json(200, self.server.host.notify_settings())
            if path.startswith("/api/decision/"):
                return self._json(200, self.server.host.resolve_decision_ref(path.rsplit("/", 1)[1]))
            if path.startswith("/api/console/"):
                pid = check_name(path.rsplit("/", 1)[1], "team id")
                return self._json(200, self.server.host.console(pid))
            if path == "/api/events":
                events, errors = self.server.timeline.events(limit=400)
                return self._json(200, {"events": events, "errors": errors})
            if path.startswith("/api/thread/"):
                tid = check_name(path.rsplit("/", 1)[1], "thread id")
                return self._json(200, self.server.bus.thread(tid))
            if path.startswith("/api/message/"):
                mid = check_name(path.rsplit("/", 1)[1], "message id")
                return self._json(200, self.server.bus.get_message(mid))
        except RelayError as e:
            return self._json(400 if e.code in (2, 6) else 404 if e.code == 3 else 500,
                              {"error": str(e), "type": type(e).__name__})
        return self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    do_HEAD = do_GET

    ACTIONS = re.compile(r"^/api/(manage|context|notify|team/[A-Za-z0-9._-]+/(intervene|say|resume|release|resolve|return|handover|cancel-handoff|resolve-handoff|abandon-unknown))$")

    def do_POST(self):
        url = urlparse(self.path)
        if not self.ACTIONS.match(url.path):  # reading endpoints never accept writes
            return self._json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "method not allowed"})
        # Actions need the token in a header (never the URL), this page's own origin and JSON.
        if self.headers.get("Host") not in self.server.allowed_hosts or not hmac.compare_digest(
                (self.headers.get("X-Relay-Token") or "").encode(), self.server.token.encode()):
            return self._json(HTTPStatus.FORBIDDEN, {"error": "forbidden"})
        if self.headers.get("Origin") not in self.server.allowed_origins:
            return self._json(HTTPStatus.FORBIDDEN, {"error": "cross-origin request refused"})
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            return self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "send JSON"})
        if self.server.host.read_only:
            return self._json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "this console is read-only"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = -1
        if n < 0 or n > MAX_BODY:
            # Drain a bounded amount first: closing on an unread body resets the connection and the
            # client never sees the 413.
            self.close_connection = True
            self._discard(min(max(n, 0), DRAIN_LIMIT))
            return self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "request too large"})
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(body, dict):
                raise ValueError
        except ValueError:
            return self._json(400, {"error": "malformed JSON"})
        h = self.server.host
        try:
            if url.path == "/api/context":
                return self._json(200, h.set_context(str(body.get("key", "")), title=body.get("title"),
                                                     summary=body.get("summary")))
            if url.path == "/api/notify":
                return self._json(200, h.notify_action(body))
            if url.path == "/api/manage":
                return self._json(200, h.manage(str(body.get("key", "")), body.get("manager") or {},
                                                str(body.get("goal", "")), review=body.get("review")))
            parts = url.path.strip("/").split("/")
            if len(parts) == 4 and parts[:2] == ["api", "team"]:
                pid, action = check_name(parts[2], "team id"), parts[3]
                if action == "intervene":
                    return self._json(200, h.intervene(pid))
                if action == "say":
                    to = body.get("to")
                    if to not in (None, "leader", "follower"):
                        return self._json(400, {"error": "to must be leader or follower"})
                    return self._json(200, h.say(pid, str(body.get("text", "")), to))
                if action == "resume":
                    return self._json(200, h.resume(pid))
                if action == "release":
                    return self._json(200, h.release(pid))
                if action == "resolve":
                    return self._json(200, h.resolve(pid, str(body.get("outcome", ""))))
                if action == "return":  # Back to terminal (hand-off), pair-wide
                    roles = body.get("roles")
                    if not isinstance(roles, list) or not roles or \
                            any(r not in ("leader", "follower") for r in roles):
                        return self._json(400, {"error": "roles must list leader and/or follower"})
                    return self._json(200, h.back_to_terminal(pid, roles))
                if action == "handover":
                    return self._json(200, h.hand_over_again(pid))
                if action == "cancel-handoff":
                    return self._json(200, h.cancel_handoff(pid))
                if action == "abandon-unknown":
                    if body.get("confirm") is not True:
                        return self._json(400, {"error": "confirm the statement to record it"})
                    return self._json(200, h.abandon_unknown(pid))
                if action == "resolve-handoff":
                    return self._json(200, h.resolve_handoff(pid, check_name(str(body.get("endpoint", "")),
                                                                             "endpoint id")))
        except RelayError as e:
            code = {2: 400, 3: 404, 4: 409, 5: 409, 6: 400, 8: 409, 9: 409}.get(e.code, 500)
            return self._json(code, {"error": str(e), "type": type(e).__name__})
        return self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def _no(self):
        self._json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "method not allowed"})

    do_PUT = do_PATCH = do_DELETE = _no


def serve(bus: Bus, *, port: int = 0, discovery: Discovery | None = None,
          host: Host | None = None, out=sys.stderr) -> None:
    host = host or Host(bus, discovery, read_only=True)
    srv = ObserverServer(bus, discovery, port, host=host)
    if discovery:
        discovery.start()
    print(f"relay console{' (read-only)' if host.read_only else ''} for {bus.root.path}", file=out)
    print(f"open: {srv.url}", file=out)
    print("loopback only; the link contains a one-time token — don't share it. Ctrl-C stops the "
          "console: AI management it runs pauses, sessions it started close, yours keep running.",
          file=out, flush=True)
    try:
        srv.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("observer stopped", file=out)
    finally:
        host.shutdown()
        if discovery:
            discovery.stop()
        srv.server_close()
