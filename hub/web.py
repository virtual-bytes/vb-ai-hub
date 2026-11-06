"""The HTTP handler: responses, authentication and dispatch."""

import hashlib
import json
import queue
import secrets
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, unquote, urlparse

from . import db
from .config import ADMIN_ONLY, HERE, MAX_BODY, PROXY_SECRET, SESSION_COOKIE, TOKEN, env
from .db import db_lock
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


class Handler(ApiRoutes, BaseHTTPRequestHandler):
    server_version = "VBAIHub/3.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        if env("VERBOSE"):
            super().log_message(fmt, *args)

    def send_bytes(self, body: bytes, ctype: str, status=200, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

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
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                self.close_connection = True
                raise ValueError("payload too large")
            self._raw = self.rfile.read(n) if n else b""
        return self._raw

    def read_json(self):
        return json.loads(self.body() or b"{}")

    def sso_user(self) -> str:
        """The Authentik user the proxy vouches for, or ''."""
        if not PROXY_SECRET:
            return ""
        if not secrets.compare_digest(self.headers.get("X-Hub-Proxy-Secret", ""), PROXY_SECRET):
            return ""
        return self.headers.get("X-Authentik-Username", "").strip()

    def token_ok(self) -> bool:
        h = self.headers.get("Authorization", "")
        q = parse_qs(urlparse(self.path).query).get("token", [""])[0]
        supplied = h[7:] if h.startswith("Bearer ") else q
        return bool(TOKEN) and secrets.compare_digest(supplied.encode(), TOKEN.encode())

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
        row = session_row(self.session_token())
        if row:
            return {"name": row["username"], "role": row["role"], "via": "session", "id": row["id"]}
        if not TOKEN and user_count() == 0:
            return {"name": "local", "role": "admin", "via": "open"}  # no token, no accounts: trusted local install
        return None

    def session_cookie(self, value, max_age):
        secure = "; Secure" if self.headers.get("X-Forwarded-Proto", "") == "https" else ""
        return f"{SESSION_COOKIE}={value}; HttpOnly; SameSite=Lax; Path=/; Max-Age={max_age}{secure}"

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
            user, token = login(b.get("username"), b.get("password"))
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

    def dispatch(self, method):
        path = self.route
        if method == "GET" and path in ("/", "/index.html"):
            return self.send_bytes((WEB_DIR / "index.html").read_bytes().replace(b"{{version}}", WEB_VERSION.encode()), "text/html; charset=utf-8")
        if method == "GET" and path.startswith(("/css/", "/js/")):
            return self.send_asset(path)
        if path == "/healthz":
            body, code = health(self.query.get("deep") == "1")
            return self.send_json(body, code)
        self._raw = None
        public = path in ("/api/auth/status", "/api/auth/setup", "/api/auth/login", "/api/auth/logout")
        if not public and not self.authed():
            self.close_connection = True  # don't leave an unread body on a keep-alive connection
            return self.send_json({"error": "unauthorized"}, 401)
        try:
            if method in ("POST", "PUT", "PATCH", "DELETE"):
                self.body()  # always consume the body so keep-alive connections stay in sync
            if public:
                return self.auth_api(method, path)
            if path in ADMIN_ONLY or path.startswith("/api/admin/") or path.startswith("/api/users"):
                if self.who()["role"] != "admin":
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
            print(f"error on {method} {path}: {e!r}", flush=True)
            return self.send_json({"error": str(e)[:300] or "internal error"}, 500)

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
        with sub_lock:
            subscribers.append(q)
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
            self.close_connection = True
