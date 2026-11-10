"""The HTTP handler: responses, authentication and dispatch."""

import hashlib
import ipaddress
import json
import queue
import re
import secrets
import shutil
import socket
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse, urlsplit

from . import db
from .config import (ADMIN_ONLY, ALLOWED_HOSTS, HERE, HOST, MAX_BODY, MAX_CONNECTIONS, MAX_JSON, MAX_STREAMS, MAX_STREAMS_PER_CLIENT,
                     MEMBER_WRITES, PROXY_SECRET, SESSION_COOKIE, TOKEN, TRUSTED_PROXIES, env)
from .db import db_lock, setting
from .live import sub_lock, subscribers
from .maintenance import health
from .accounts import SESSION_DAYS, create_user, login, session_row, user_count
from .routes import ApiRoutes


WEB_DIR = HERE / "web"


def web_version(folder=WEB_DIR) -> str:
    """A fingerprint of the page's files, so an open tab can tell it's out of date."""
    h = hashlib.sha256()
    for f in sorted(p for p in folder.rglob("*") if p.is_file()):
        h.update(f.relative_to(folder).as_posix().encode())
        h.update(f.read_bytes())
    return h.hexdigest()[:12]


WEB_VERSION = web_version()
ASSET_TYPES = {".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8"}
# The page is all same-origin files with no inline script. Inline style attributes are used in templates.
PAGE_CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'; object-src 'none'")
MEMBER_RULES = [(m, re.compile(p)) for m, p in MEMBER_WRITES]
BIG_BODIES = re.compile(r"/api/import|/api/(cards|owners)/[^/]+/attachments")
stream_clients: dict[str, int] = {}


def member_may(method, path) -> bool:
    return method == "GET" or any(m == method and rx.fullmatch(path) for m, rx in MEMBER_RULES)


def same_text(a, b) -> bool:
    """Compare a header with a secret in constant time, whatever characters the header holds."""
    return secrets.compare_digest(str(a).encode("utf-8", "surrogateescape"), str(b).encode("utf-8", "surrogateescape"))


def split_host(value):
    """(host name, port or None) from a Host header or an origin, or ("", None) when it can't be read."""
    try:
        u = urlsplit(value if "://" in value else "//" + value)
        return (u.hostname or "").rstrip(".").lower(), u.port
    except ValueError:
        return "", None


def default_port(scheme):
    return {"http": 80, "https": 443}.get(scheme)


def is_ip(host) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def known_host(host) -> bool:
    """A name this hub answers to. Addresses always qualify; a page can only be served from an address the hub
    really has. Other names need to be the hub's own, the one in Settings, or listed in HUB_ALLOWED_HOSTS."""
    if not host or host == "localhost" or host.endswith(".localhost") or is_ip(host):
        return True
    own = socket.gethostname().lower()
    names = {own, own + ".local", HOST.lower(), *ALLOWED_HOSTS}
    public = split_host(setting("public_url"))[0]
    return host in names or (bool(public) and host == public)


def trusted_proxy(addr) -> bool:
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    return any(ip in net for net in TRUSTED_PROXIES)


class HubServer(ThreadingHTTPServer):
    """One thread per connection, up to MAX_CONNECTIONS at once. Past that, new connections are closed straight away,
    so a client that opens thousands of idle connections can't use up the machine's threads."""
    daemon_threads = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.slots = threading.BoundedSemaphore(MAX_CONNECTIONS)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


class Handler(ApiRoutes, BaseHTTPRequestHandler):
    server_version = "VBAIHub"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    timeout = 120  # a silent or very slow connection gives its thread back

    def log_message(self, fmt, *args):
        if env("VERBOSE"):
            super().log_message(fmt, *args)

    def send_bytes(self, body: bytes, ctype: str, status=200, extra=None):
        headers = {"Content-Type": ctype, "Content-Length": str(len(body)), "Cache-Control": "no-store",
                   "X-Content-Type-Options": "nosniff", **(extra or {})}
        for k, v in headers.items():  # check everything before the status line goes out
            if any(c in str(v) for c in "\r\n\0"):
                raise RuntimeError(f"bad value for the {k} header")
            str(v).encode("latin-1")
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, f, ctype: str, extra=None):
        """Send an open file in chunks, so a big export never has to fit in memory."""
        size = f.seek(0, 2)
        f.seek(0)
        self.send_bytes(b"", ctype, extra={**(extra or {}), "Content-Length": str(size)})
        shutil.copyfileobj(f, self.wfile, 1024 * 1024)

    def send_asset(self, path):
        """A file from web/; the page has to load before sign-in, so nothing private may live there."""
        file = (WEB_DIR / path.lstrip("/")).resolve()
        if file.suffix not in ASSET_TYPES or not file.is_relative_to(WEB_DIR) or not file.is_file():
            return self.send_json({"error": "not found"}, 404)
        return self.send_bytes(file.read_bytes(), ASSET_TYPES[file.suffix])

    def send_json(self, obj, status=200):
        self.send_bytes(json.dumps(obj).encode(), "application/json", status)

    def body(self) -> bytes:
        if getattr(self, "_raw", None) is None:
            raw = (self.headers.get("Content-Length") or "0").strip()
            limit = MAX_BODY if BIG_BODIES.fullmatch(self.route) else MAX_JSON
            if not raw.isdigit() or int(raw) > limit:
                self.close_connection = True
                raise ValueError("payload too large" if raw.isdigit() else "bad Content-Length")
            n = int(raw)
            self._raw = self.rfile.read(n) if n else b""
        return self._raw

    def read_json(self):
        return json.loads(self.body() or b"{}")

    def sso_user(self) -> str:
        """The Authentik user the proxy vouches for, or ''."""
        if not PROXY_SECRET:
            return ""
        if not same_text(self.headers.get("X-Hub-Proxy-Secret", ""), PROXY_SECRET):
            return ""
        return self.headers.get("X-Authentik-Username", "").strip()

    def supplied_token(self):
        """The token the caller sent, or None when it sent none. The address form is for the live stream only,
        because EventSource can't set headers. Other Authorization schemes, such as Basic auth added by a
        proxy, aren't a token attempt, so they don't stop a session cookie from working."""
        h = self.headers.get("Authorization", "")
        if h[:7].lower() == "bearer ":
            return h[7:].strip()
        qs = parse_qs(urlparse(self.path).query)
        if "token" in qs:
            return qs["token"][0] if self.route == "/api/stream" else ""
        return None

    def token_ok(self) -> bool:
        supplied = self.supplied_token()
        return bool(TOKEN) and supplied is not None and same_text(supplied, TOKEN)

    def bad_token(self) -> bool:
        """A request that announced itself as a token call, with the wrong token. It never falls back to a cookie,
        so a token request that skipped single sign-on at the proxy can't borrow a local session."""
        return self.supplied_token() is not None and not self.token_ok()

    def client_addr(self) -> str:
        """The address the request came from. Behind a proxy this is the address the proxy saw: the last entry of
        X-Forwarded-For that isn't a trusted proxy, because a client can put anything at the front of that header.
        A proxy is trusted when it sends HUB_PROXY_SECRET or its address is in HUB_TRUSTED_PROXIES."""
        peer = self.client_address[0] if self.client_address else ""
        vouched = PROXY_SECRET and same_text(self.headers.get("X-Hub-Proxy-Secret", ""), PROXY_SECRET)
        if not (vouched or trusted_proxy(peer)):
            return peer
        hops = [h.strip() for h in self.headers.get("X-Forwarded-For", "").split(",") if h.strip()]
        while hops and trusted_proxy(hops[-1]) and len(hops) > 1:
            hops.pop()
        return hops[-1][:64] if hops else peer

    def host_ok(self) -> bool:
        """With no token and no accounts the hub trusts every request, so it only answers to its own names. That stops
        a web page from re-pointing its own domain at the hub (DNS rebinding) and then reading and changing it."""
        if TOKEN or user_count():
            return True
        return known_host(split_host(self.headers.get("Host", ""))[0])

    def same_origin(self) -> bool:
        """For writes: refuse requests a browser marks as coming from another page. Tools and scripts send neither
        header, so they're unaffected. Origins are compared by name and port, because sibling sites on another port
        or subdomain would otherwise count as the same site."""
        site = self.headers.get("Sec-Fetch-Site", "")
        if site and site not in ("same-origin", "none"):
            return False
        origin = self.headers.get("Origin")
        if origin is None:
            return True
        try:
            o = urlsplit(origin.strip())
            o_host, o_port = (o.hostname or "").rstrip(".").lower(), o.port or default_port(o.scheme)
        except ValueError:
            return False
        if not o_host:
            return False  # "null": a sandboxed frame or a local file
        h_host, h_port = split_host(self.headers.get("Host", ""))
        if o_host == h_host and (h_port is None or h_port == o_port):
            return True
        try:
            p = urlsplit(setting("public_url"))
            return bool(p.hostname) and o_host == p.hostname.lower() and o_port == (p.port or default_port(p.scheme))
        except ValueError:
            return False

    def json_write_ok(self, method, path) -> bool:
        """A browser can send a plain-text POST to another site without asking first; a JSON one it can't. So a POST
        that relies on a cookie or on open mode must say it's JSON. Calls with the access token are exempt: no
        page can add that header without the hub's permission, and scripts often post with curl's default type."""
        if method != "POST" or self.token_ok() or path == "/api/auth/logout":
            return True
        ctype = self.headers.get("Content-Type", "").split(";")[0].strip().lower()
        return ctype == "application/json"

    def session_token(self):
        for part in self.headers.get("Cookie", "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == SESSION_COOKIE:
                return v
        return ""

    def who(self):
        """The caller: {name, role, via, id?}, or None. Single sign-on and the access token act as admin."""
        sso = self.sso_user()
        if sso:
            return {"name": sso, "role": "admin", "via": "sso"}
        if self.token_ok():
            return {"name": "access token", "role": "admin", "via": "token"}
        if self.bad_token():
            return None
        row = session_row(self.session_token())
        if row:
            return {"name": row["username"], "role": row["role"], "via": "session", "id": row["id"]}
        if not TOKEN and user_count() == 0:
            return {"name": "local", "role": "admin", "via": "open"}  # no token, no accounts: trusted local install
        return None

    def session_cookie(self, value, max_age):
        secure = "; Secure" if self.headers.get("X-Forwarded-Proto", "") == "https" else ""
        return f"{SESSION_COOKIE}={value}; HttpOnly; SameSite=Strict; Path=/; Max-Age={max_age}{secure}"

    def auth_api(self, method, path):
        """Sign-in endpoints; the only API that works before authentication."""
        if path == "/api/auth/status" and method == "GET":
            me = self.who()
            return self.send_json({"user": me and {k: me[k] for k in ("name", "role", "via")}, "setup_needed": user_count() == 0,
                                   "setup_token_required": bool(TOKEN)})
        if path == "/api/auth/setup" and method == "POST":
            b = self.read_json()
            if user_count():
                raise PermissionError("setup is already done")
            if TOKEN and not secrets.compare_digest(str(b.get("token") or "").encode(), TOKEN.encode()):
                raise PermissionError("wrong access token")
            create_user(b.get("username"), b.get("password"), "admin")
            user, token = login(b.get("username"), b.get("password"))
            return self.send_bytes(json.dumps(user).encode(), "application/json", 201,
                                   {"Set-Cookie": self.session_cookie(token, SESSION_DAYS * 86400)})
        if path == "/api/auth/login" and method == "POST":
            b = self.read_json()
            user, token = login(b.get("username"), b.get("password"), self.client_addr())
            return self.send_bytes(json.dumps(user).encode(), "application/json", 200,
                                   {"Set-Cookie": self.session_cookie(token, SESSION_DAYS * 86400)})
        if path == "/api/auth/logout" and method == "POST":
            raw = self.session_token()
            if raw:
                with db_lock:
                    db.conn.execute("DELETE FROM sessions WHERE hash=?", (hashlib.sha256(raw.encode()).hexdigest(),))
                    db.conn.commit()
            return self.send_bytes(b"{}", "application/json", 200, {"Set-Cookie": self.session_cookie("", 0)})
        raise LookupError("not found")

    def authed(self) -> bool:
        return self.who() is not None

    @property
    def route(self):
        return unquote(urlparse(self.path).path).rstrip("/") or "/"

    @property
    def query(self):
        return {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}

    def is_admin(self) -> bool:
        me = self.who()
        return bool(me) and me["role"] == "admin"

    def dispatch(self, method):
        try:
            path = self.route
            if not self.host_ok():
                self.close_connection = True
                return self.send_json({"error": "this hub doesn't answer to that host name; add it to HUB_ALLOWED_HOSTS "
                                                "or set the hub address in Settings"}, 421)
            if method == "GET" and path in ("/", "/index.html"):
                return self.send_bytes((WEB_DIR / "index.html").read_bytes().replace(b"{{version}}", WEB_VERSION.encode()),
                                       "text/html; charset=utf-8", extra={"Content-Security-Policy": PAGE_CSP, "X-Frame-Options": "DENY",
                                                                          "Referrer-Policy": "same-origin"})
            if method == "GET" and path.startswith(("/css/", "/js/")):
                return self.send_asset(path)
        except ValueError:
            self.close_connection = True
            return self.send_json({"error": "not found"}, 404)
        try:
            if path == "/healthz":
                deep = self.query.get("deep") == "1"
                body, code = health(deep and self.authed())
                return self.send_json(body, code)
            self._raw = None
            public = path in ("/api/auth/status", "/api/auth/setup", "/api/auth/login", "/api/auth/logout")
            if (not public and not self.authed()) or (public and self.bad_token()):
                self.close_connection = True  # don't leave an unread body on a keep-alive connection
                return self.send_json({"error": "unauthorized"}, 401)
            if method in ("POST", "PUT", "PATCH", "DELETE"):
                # Writes from another page are refused, including pages on another port or subdomain of this host.
                if not self.same_origin():
                    self.close_connection = True
                    return self.send_json({"error": "requests from other sites are refused"}, 403)
                if not self.json_write_ok(method, path):
                    self.close_connection = True
                    return self.send_json({"error": "send JSON with Content-Type: application/json"}, 415)
                self.body()  # always consume the body so keep-alive connections stay in sync
            if public:
                return self.auth_api(method, path)
            admin_path = path in ADMIN_ONLY or path.startswith("/api/admin/") or path.startswith("/api/users")
            if not self.is_admin() and (admin_path or not member_may(method, path)):
                return self.send_json({"error": "admins only"}, 403)
            return self.api(method, path)
        except PermissionError as e:
            return self.send_json({"error": str(e)}, 403)
        except LookupError as e:
            return self.send_json({"error": str(e)}, 404)
        except (ValueError, json.JSONDecodeError) as e:
            return self.send_json({"error": str(e)}, 400)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            ref = secrets.token_hex(4)
            print(f"error {ref} on {method} {path}: {e!r}", flush=True)
            traceback.print_exc()
            self.close_connection = True
            try:
                return self.send_json({"error": f"internal error (reference {ref}, details are in the hub's log)"}, 500)
            except OSError:
                pass

    def do_GET(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")

    def do_PUT(self):
        self.dispatch("PUT")

    def do_PATCH(self):
        self.dispatch("PATCH")

    def do_DELETE(self):
        self.dispatch("DELETE")

    def stream(self):
        q: queue.Queue = queue.Queue(maxsize=500)
        addr = self.client_addr()
        with sub_lock:
            if len(subscribers) >= MAX_STREAMS or stream_clients.get(addr, 0) >= MAX_STREAMS_PER_CLIENT:
                self.close_connection = True
                return self.send_json({"error": "too many live connections"}, 429)
            subscribers.append(q)
            stream_clients[addr] = stream_clients.get(addr, 0) + 1
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            hello = json.dumps({"kind": "hello", "data": {"version": WEB_VERSION}})
            self.wfile.write(f"retry: 3000\n\ndata: {hello}\n\n".encode())
            self.wfile.flush()
            while True:
                try:
                    self.wfile.write(f"data: {q.get(timeout=20)}\n\n".encode())
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                self.wfile.flush()
        except OSError:
            pass
        finally:
            with sub_lock:
                if q in subscribers:
                    subscribers.remove(q)
                stream_clients[addr] = stream_clients.get(addr, 1) - 1
                if stream_clients[addr] <= 0:
                    stream_clients.pop(addr, None)
            self.close_connection = True
