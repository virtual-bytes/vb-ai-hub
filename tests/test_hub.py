#!/usr/bin/env python3
"""End-to-end tests: starts a real server on a temp database and drives it over HTTP. Standard library only.

  python3 -m unittest discover -s tests -v
"""
import collections
import contextlib
import gzip
import hashlib
import http.client
import http.server
import io
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOKEN = "test-token-for-the-suite-0001"
TODAY = date.today().isoformat()


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Hub(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.port = free_port()
        cls.base = f"http://127.0.0.1:{cls.port}"
        env = {**os.environ, "HUB_PORT": str(cls.port), "HUB_TOKEN": TOKEN, "HUB_DB": f"{cls.tmp.name}/data/hub.db",
               "HUB_BACKUP_DIR": f"{cls.tmp.name}/backup", "HUB_PROXY_SECRET": "proxy-secret"}
        os.makedirs(f"{cls.tmp.name}/data")
        cls.proc = subprocess.Popen([sys.executable, str(ROOT / "server.py")], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            try:
                urllib.request.urlopen(cls.base + "/healthz", timeout=1)
                return
            except OSError:
                time.sleep(0.05)
        raise RuntimeError("server did not start")

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait(5)
        cls.tmp.cleanup()

    def call(self, method, path, body=None, token=TOKEN, raw=False):
        data = body if isinstance(body, bytes) else None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, method=method)
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                out = r.read()
                return r.status, out if raw else json.loads(out or b"{}")
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read() or b"{}")

    def ok(self, method, path, body=None, raw=False):
        status, out = self.call(method, path, body, raw=raw)
        self.assertLess(status, 300, f"{method} {path}: {out}")
        return out

    def hook(self, sid, event="UserPromptSubmit", **kw):
        return self.ok("POST", "/api/events", {"hook_event_name": event, "session_id": sid, **kw})

    @staticmethod
    def q(card_id):
        return urllib.parse.quote(card_id, safe="")

    def stats(self):
        return self.ok("GET", f"/api/usage/stats?today={TODAY}")

    def project(self, name):
        return next((p for p in self.ok("GET", "/api/cards")["projects"] if p["name"] == name), None)


class Auth(Hub):
    def test_token_required(self):
        self.assertEqual(self.call("GET", "/api/cards", token=None)[0], 401)
        self.assertEqual(self.call("GET", "/api/cards", token="wrong")[0], 401)

    def test_health_is_public(self):
        self.assertEqual(self.call("GET", "/healthz", token=None)[0], 200)

    def test_settings_never_return_secrets(self):
        self.ok("POST", "/api/settings", {"notify_url": "http://127.0.0.1:9/secret-hook-id"})
        self.assertEqual(self.ok("GET", "/api/cards")["settings"]["notify_url"], "set")
        self.ok("POST", "/api/settings", {"notify_url": ""})

    def test_keep_alive_survives_ignored_body(self):
        """An endpoint that ignores its body must not corrupt the next request on the connection."""
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        h = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
        for _ in range(3):
            c.request("POST", "/api/integrations/owui/sync", body="{}", headers=h)
            r = c.getresponse()
            r.read()
            self.assertEqual(r.status, 200)
        c.close()


class AccountHelpers:
    PW = "correct horse battery"

    def raw(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        c.request(method, path, body=None if body is None else json.dumps(body), headers={"Content-Type": "application/json", **(headers or {})})
        r = c.getresponse()
        out = (r.status, json.loads(r.read() or b"{}"), r.getheader("Set-Cookie") or "")
        c.close()
        return out

    def sign_in(self, user="boss", pw=None):
        status, _, cookie = self.raw("POST", "/api/auth/login", {"username": user, "password": pw or self.PW})
        self.assertEqual(status, 200)
        return {"Cookie": cookie.split(";")[0]}

    def ensure_admin(self):
        if self.raw("GET", "/api/auth/status")[1]["setup_needed"]:
            status, _, _ = self.raw("POST", "/api/auth/setup", {"username": "boss", "password": self.PW, "token": TOKEN})
            self.assertEqual(status, 201)


class Accounts(AccountHelpers, Hub):
    def test_a_setup_needs_the_access_token(self):
        self.assertEqual(self.raw("POST", "/api/auth/setup", {"username": "boss", "password": self.PW, "token": "nope"})[0], 403)
        self.assertTrue(self.raw("GET", "/api/auth/status")[1]["setup_needed"])
        self.ensure_admin()
        self.assertEqual(self.raw("POST", "/api/auth/setup", {"username": "evil", "password": self.PW, "token": TOKEN})[0], 403)

    def test_cookie_is_locked_down_and_grants_access(self):
        self.ensure_admin()
        _, _, cookie = self.raw("POST", "/api/auth/login", {"username": "boss", "password": self.PW})
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        _, _, proxied = self.raw("POST", "/api/auth/login", {"username": "boss", "password": self.PW}, {"X-Forwarded-Proto": "https"})
        self.assertIn("Secure", proxied)
        self.assertEqual(self.raw("GET", "/api/cards", headers={"Cookie": cookie.split(";")[0]})[0], 200)

    def test_logout_ends_the_session(self):
        self.ensure_admin()
        h = self.sign_in()
        self.raw("POST", "/api/auth/logout", {}, h)
        self.assertEqual(self.raw("GET", "/api/cards", headers=h)[0], 401)

    def test_wrong_password_and_lockout(self):
        self.ensure_admin()
        self.ok("POST", "/api/users", {"username": "locky", "password": self.PW})
        for _ in range(5):
            self.assertEqual(self.raw("POST", "/api/auth/login", {"username": "locky", "password": "wrong wrong wrong"})[0], 403)
        status, body, _ = self.raw("POST", "/api/auth/login", {"username": "locky", "password": self.PW})
        self.assertEqual(status, 403)
        self.assertIn("too many", body["error"])

    def test_member_cannot_do_admin_things(self):
        self.ensure_admin()
        self.ok("POST", "/api/users", {"username": "member1", "password": self.PW})
        h = self.sign_in("member1")
        self.assertEqual(self.raw("GET", "/api/cards", headers=h)[0], 200)
        self.assertEqual(self.raw("POST", "/api/settings", {"auto_flow": False}, h)[0], 403)
        self.assertEqual(self.raw("GET", "/api/users", headers=h)[0], 403)
        self.assertEqual(self.raw("POST", "/api/admin/redact", {}, h)[0], 403)

    def test_password_rules_and_hashes(self):
        self.ensure_admin()
        self.assertEqual(self.call("POST", "/api/users", {"username": "shorty", "password": "short"})[0], 400)
        self.assertNotIn("pw", self.ok("GET", "/api/users")["users"][0])

    def test_change_password_signs_out_other_sessions(self):
        self.ensure_admin()
        self.ok("POST", "/api/users", {"username": "changer", "password": self.PW})
        a, b = self.sign_in("changer"), self.sign_in("changer")
        new = "a brand new passphrase"
        self.assertEqual(self.raw("POST", "/api/auth/password", {"current": "bad", "password": new}, a)[0], 403)
        self.assertEqual(self.raw("POST", "/api/auth/password", {"current": self.PW, "password": new}, a)[0], 200)
        self.assertEqual(self.raw("GET", "/api/cards", headers=a)[0], 200)
        self.assertEqual(self.raw("GET", "/api/cards", headers=b)[0], 401)
        self.sign_in("changer", new)

    def test_removing_a_user_ends_their_sessions(self):
        self.ensure_admin()
        u = self.ok("POST", "/api/users", {"username": "leaver", "password": self.PW})
        h = self.sign_in("leaver")
        self.ok("DELETE", f"/api/users/{u['id']}")
        self.assertEqual(self.raw("GET", "/api/cards", headers=h)[0], 401)

    def test_last_admin_and_self_are_protected(self):
        self.ensure_admin()
        boss = next(u for u in self.ok("GET", "/api/users")["users"] if u["username"] == "boss")
        self.assertEqual(self.call("DELETE", f"/api/users/{boss['id']}")[0], 400)

    def test_existing_routes_still_work(self):
        self.ensure_admin()
        self.assertEqual(self.call("GET", "/api/cards")[0], 200)  # bearer token
        sso = {"X-Authentik-Username": "alice", "X-Hub-Proxy-Secret": "proxy-secret"}
        self.assertEqual(self.raw("GET", "/api/cards", headers=sso)[0], 200)
        self.assertEqual(self.raw("GET", "/api/cards", headers={"X-Authentik-Username": "alice"})[0], 401)  # no forged SSO
        self.assertEqual(self.raw("GET", "/api/cards", headers={"Authorization": "Bearer t\u00f6ken".encode().decode("latin-1")})[0], 401)


class Page(Hub):
    def test_page_and_its_assets_load_before_sign_in(self):
        status, html = self.call("GET", "/", token=None, raw=True)
        self.assertEqual(status, 200)
        assets = re.findall(r'(?:src|href)="(/(?:js|css)/[^"]+)"', html.decode())
        self.assertGreater(len(assets), 5)
        for path in assets:
            with urllib.request.urlopen(self.base + path, timeout=5) as r:
                self.assertEqual(r.status, 200, path)
                self.assertTrue(r.read(), path)
                self.assertEqual(r.headers["Content-Type"].split(";")[0], "text/css" if path.endswith(".css") else "text/javascript")

    def test_open_tabs_learn_the_page_version_from_the_stream(self):
        html = self.ok("GET", "/", raw=True).decode()
        version = re.search(r'name="hub-version" content="([0-9a-f]{12})"', html).group(1)
        req = urllib.request.Request(f"{self.base}/api/stream", headers={"Authorization": f"Bearer {TOKEN}"})
        with urllib.request.urlopen(req, timeout=5) as r:
            line = next(l for l in r if l.startswith(b"data: "))
        self.assertEqual(json.loads(line[6:]), {"kind": "hello", "data": {"version": version}})

    def test_the_version_follows_the_page_files(self):
        code = ("import sys, tempfile, pathlib; sys.path.insert(0, sys.argv[1]); from hub.web import web_version\n"
                "d = pathlib.Path(tempfile.mkdtemp()); (d / 'a.js').write_text('1'); first = web_version(d)\n"
                "assert first == web_version(d); (d / 'a.js').write_text('2'); assert first != web_version(d)\n")
        run = subprocess.run([sys.executable, "-c", code, str(ROOT)], capture_output=True, text=True, env={**os.environ, "HUB_DB": os.devnull})
        self.assertEqual(run.returncode, 0, run.stderr)

    def test_every_script_on_disk_is_loaded(self):
        html = (ROOT / "web" / "index.html").read_text()
        for f in (ROOT / "web" / "js").glob("*.js"):
            self.assertIn(f'src="/js/{f.name}"', html)

    def test_both_themes_meet_the_contrast_minimum(self):
        css = (ROOT / "web" / "css" / "hub.css").read_text()
        block = lambda sel: dict(re.findall(r"--([\w-]+):\s*(#[0-9a-fA-F]{6})", re.search(re.escape(sel) + r"\s*\{(.*?)\}", css, re.S).group(1)))

        def lum(c):
            v = [int(c[i:i + 2], 16) / 255 for i in (1, 3, 5)]
            r, g, b = [x / 12.92 if x <= .03928 else ((x + .055) / 1.055) ** 2.4 for x in v]
            return .2126 * r + .7152 * g + .0722 * b

        def ratio(a, b):
            hi, lo = sorted((lum(a), lum(b)), reverse=True)
            return (hi + .05) / (lo + .05)

        surfaces = ("bg", "panel", "col")
        pairs = [(t, s) for t in ("text", "muted", "faint", "accent", "run", "wait", "err") for s in surfaces]
        pairs += [("text", "accent-soft"), ("muted", "accent-soft"), ("faint", "accent-soft"), ("accent", "accent-soft"), ("code", "code-soft"),
                  ("chat", "chat-soft"), ("owui", "owui-soft"), ("manual", "manual-soft"), ("onaccent", "accent"), ("onerr", "err")]
        media = re.search(r"@media \(prefers-color-scheme: dark\) \{\s*:root:not\(\[data-theme=\"light\"\]\) \{(.*?)\}", css, re.S).group(1)
        dark = block(':root[data-theme="dark"]')
        self.assertEqual(dict(re.findall(r"--([\w-]+):\s*(#[0-9a-fA-F]{6})", media)), dark, "the two dark blocks must match")
        for name, theme in (("light", block(":root")), ("dark", dark)):
            for fg, bg in pairs:
                self.assertGreaterEqual(ratio(theme[fg], theme[bg]), 4.5, f"{name}: {fg} on {bg}")

    def test_a_machine_connects_with_one_command(self):
        self.assertEqual(self.call("GET", "/connect/install.sh", token=None, raw=True)[0], 401)
        with tempfile.TemporaryDirectory() as home:
            script = self.ok("GET", "/connect/install.sh", raw=True)
            env = {**os.environ, "HOME": home, "HUB_URL": self.base + "/", "HUB_TOKEN": TOKEN}
            (Path(home) / "hook.py").write_text("# a different file that happens to be in this folder")
            run = subprocess.run(["bash", "-s", "--", "--no-backfill"], input=script, capture_output=True, env=env, cwd=home)
            self.assertEqual(run.returncode, 0, run.stderr.decode())
            config = json.loads((Path(home) / ".config/vb-ai-hub/config.json").read_text())
            self.assertEqual(config, {"url": self.base, "token": TOKEN})
            hook = Path(home) / ".local/share/vb-ai-hub/hook.py"
            self.assertEqual(hook.read_bytes(), (ROOT / "hook.py").read_bytes())
            for _ in range(2):  # running it again replaces the entries rather than doubling them
                subprocess.run(["bash", "-s", "--", "--no-backfill"], input=script, capture_output=True, env=env, cwd=home, check=True)
            hooks = json.loads((Path(home) / ".claude/settings.json").read_text())["hooks"]
            self.assertEqual(set(hooks), {"SessionStart", "UserPromptSubmit", "Stop", "SessionEnd"})
            for groups in hooks.values():
                self.assertEqual(len(groups), 1)
                self.assertIn(str(hook), groups[0]["hooks"][0]["command"])

    @unittest.skipUnless(shutil.which("node"), "needs node")
    def test_scripts_only_call_functions_that_exist(self):
        js = "\n".join(f.read_text() for f in (ROOT / "web" / "js").glob("*.js"))
        defined = set(re.findall(r"function\s+(\w+)", js)) | set(re.findall(r"(?:const|let)\s+(\w+)\s*=", js))
        called = set(re.findall(r"(?<![\w.$`])render[A-Z]\w*(?=\()|(?<![\w.$`])load[A-Z]\w*(?=\()", js))
        self.assertEqual(called - defined, set())

    def test_published_text_has_no_long_dashes_or_hidden_characters(self):
        banned = re.compile("[\u2012-\u2015\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufeff\u00ad]")
        files = [f for f in ROOT.rglob("*") if f.is_file() and not {"private", "backup", "__pycache__", ".git", ".claude"} & set(f.parts) and f.suffix in (".md", ".py", ".js", ".css", ".html", ".yml", ".sh", ".conf")]
        self.assertEqual([f.name for f in files if banned.search(f.read_text(errors="replace"))], [])

    def test_nothing_private_in_the_published_files(self):
        files = [f for f in ROOT.rglob("*") if f.is_file() and not {"private", "backup", "__pycache__", ".git", ".claude"} & set(f.parts) and f.suffix != ".pyc"]
        private = re.compile(r"172\.31\.|virtualbytes\.io|gnetwork|ai-stack-infra|/home/tomek")
        hits = [f.name for f in files if f != Path(__file__).resolve() and private.search(f.read_text(errors="replace"))]
        self.assertEqual(hits, [])

    def test_scripts_only_look_up_elements_that_exist(self):
        js = "\n".join(f.read_text() for f in (ROOT / "web" / "js").glob("*.js"))
        known = set(re.findall(r'\bid="([\w-]+)"', (ROOT / "web" / "index.html").read_text() + js))
        wanted = set(re.findall(r'\$\("#([\w-]+)"\)', js)) | {x[1:] for x in re.findall(r'"(#[\w-]+)"', js)}
        self.assertEqual(wanted - known - {"uiF0"}, set())  # uiF0 is built by dialog() at run time

    @unittest.skipUnless(shutil.which("node"), "needs node")
    def test_every_script_parses(self):
        for f in (ROOT / "web" / "js").glob("*.js"):
            run = subprocess.run(["node", "--check", str(f)], capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, f"{f.name}: {run.stderr}")

    def test_assets_stay_inside_the_web_folder(self):
        outside = ROOT / "outside-web-test.js"
        outside.write_text("secret")
        try:
            for path in ("/js/../../outside-web-test.js", "/js/%2e%2e/%2e%2e/outside-web-test.js", "/css/..%2f..%2foutside-web-test.js",
                         "/js/../../server.py", "/js/../index.html", "/js/missing.js", "/js"):
                conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
                conn.request("GET", path)  # sent as written; urllib would tidy it
                r = conn.getresponse()
                self.assertNotIn(b"secret", r.read(), path)
                self.assertIn(r.status, (401, 404), path)
                conn.close()
        finally:
            outside.unlink()


class Backups(Hub):
    def test_restore_brings_the_database_back(self):
        self.hook("restore-me", cwd="/work/restored", title="Restore me")
        self.ok("POST", "/api/backup", {})
        target = Path(self.tmp.name) / "restored" / "hub.db"
        target.parent.mkdir()
        env = {**os.environ, "HUB_DB": str(target), "HUB_BACKUP_DIR": f"{self.tmp.name}/backup"}
        run = subprocess.run([sys.executable, str(ROOT / "server.py"), "--restore"], env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(run.returncode, 0, run.stderr)
        with contextlib.closing(sqlite3.connect(target)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM cards WHERE id LIKE '%restore-me%'").fetchone()[0], 1)


class FakeLiteLLM(http.server.BaseHTTPRequestHandler):
    """Just enough of the proxy: bearer auth and paged /spend/logs/v2."""
    logs, key, pages = [], "gateway-key-for-the-suite", []

    def log_message(self, *args):
        pass

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
        ok = self.headers.get("Authorization") == f"Bearer {self.key}"
        status = 401 if not ok else 200 if url.path == "/spend/logs/v2" else 404
        page, size = int(q.get("page", 1)), int(q.get("page_size", 50))
        rows = sorted(self.logs, key=lambda r: r["startTime"], reverse=q.get("sort_order") != "asc")
        FakeLiteLLM.pages = [*self.pages, page]
        body = json.dumps({"data": rows[(page - 1) * size:page * size], "total": len(rows), "page": page}).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class LiteLLM(Hub):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.gateway = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeLiteLLM)
        threading.Thread(target=cls.gateway.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.gateway.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.gateway.shutdown()
        super().tearDownClass()

    def status(self):
        return json.loads(self.ok("GET", "/api/cards")["settings"].get("litellm_status") or "null")

    def sync(self, logs, **settings):
        FakeLiteLLM.logs, FakeLiteLLM.pages = logs, []
        started = time.time()
        body = {"litellm_url": self.url, "litellm_key": FakeLiteLLM.key, "litellm_local": "", "litellm_exclude": "", "usage_tz": "UTC", **settings}
        self.ok("POST", "/api/settings", body)
        for _ in range(200):
            s = self.status()
            if s and s["at"] >= started:
                return s
            time.sleep(0.025)
        self.fail("no sync happened")

    def model(self, name):
        return next((m for m in self.stats()["gpu"]["by_model"] if m["name"] == name), None)

    @staticmethod
    def log(model, day=TODAY, base="http://gpu-box:8000/v1", inp=10, out=5, **kw):
        kw.setdefault("startTime", f"{day}T00:30:00Z")
        return {"model_group": model, "api_base": base, "prompt_tokens": inp, "completion_tokens": out, "status": "success",
                "metadata": {"user_api_key_alias": "app"}, **kw}

    def test_counts_only_successful_local_work(self):
        s = self.sync([
            self.log("rules"), self.log("rules", metadata='{"user_api_key_alias": "app"}'),
            self.log("rules", base="https://api.paid.example/v1"),
            self.log("rules", status="failure"),
            self.log("rules", metadata={"user_api_key_alias": "health-check"}),
            self.log("rules-embed", base=""), self.log("rules-other", base=""),
        ], litellm_local="gpu-box, model:rules-embed", litellm_exclude="health-check")
        self.assertTrue(s["ok"], s)
        m = self.model("rules")
        self.assertEqual((m["requests"], m["input"], m["output"]), (2, 20, 10))
        self.assertEqual(self.model("rules-embed")["requests"], 1)
        self.assertIsNone(self.model("rules-other"))

    def test_no_rules_counts_everything(self):
        self.sync([self.log("everything", base="https://api.paid.example/v1")])
        self.assertEqual(self.model("everything")["requests"], 1)

    def test_syncing_twice_does_not_double(self):
        logs = [self.log("twice")]
        self.sync(logs)
        self.sync(logs)
        self.assertEqual(self.model("twice")["requests"], 1)

    def test_reads_every_page(self):
        s = self.sync([self.log("paged", request_id=str(i)) for i in range(1500)])
        self.assertEqual(self.model("paged")["requests"], 1500)
        self.assertEqual(FakeLiteLLM.pages, [1, 2], s)

    def test_a_partly_read_day_keeps_its_total(self):
        old = (date.today() - timedelta(days=2)).isoformat()
        url = self.url.replace("127.0.0.1", "localhost")  # a new address gets the full 30-day read
        self.sync([self.log("partial", day=old, request_id=str(i)) for i in range(5)], litellm_url=url)
        self.assertEqual(self.model("partial")["requests"], 5)
        self.sync([self.log("partial", day=old)], litellm_url=url)  # later reads cover 2 days; this one sits just outside
        self.assertEqual(self.model("partial")["requests"], 5)

    def test_days_follow_the_time_zone(self):
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        self.sync([self.log("zoned", startTime=f"{TODAY}T00:30:00Z"), self.log("zoned", startTime=f"{yesterday}T23:30:00Z")], usage_tz="America/Denver")
        rows = self.ok("GET", f"/api/usage/stats?today={yesterday}")["gpu"]["today"]
        self.assertGreaterEqual(rows["requests"], 2)  # two UTC days, but both are yesterday afternoon in Denver

    def test_wrong_key_is_reported_and_keys_stay_hidden(self):
        s = self.sync([], litellm_key="not-the-gateway-key")
        self.assertFalse(s["ok"])
        self.assertIn("401", s["error"])
        settings = self.ok("GET", "/api/cards")["settings"]
        self.assertEqual(settings["litellm_key"], "set")

    def test_unknown_time_zone_is_refused(self):
        self.assertEqual(self.call("POST", "/api/settings", {"usage_tz": "Mars/Base"})[0], 400)
        self.assertEqual(self.call("POST", "/api/settings", {"litellm_url": "gateway:4000"})[0], 400)


class FakeOpenWebUI(http.server.BaseHTTPRequestHandler):
    """Just enough for generation: the chat endpoint, which records what it was asked and returns a set reply."""
    prompts, reply = [], "# Handoff: test\n\n## Next steps\n1. Ship it"

    def log_message(self, *args):
        pass

    def answer(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.answer(404, {})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path != "/api/chat/completions":
            return self.answer(404, {})
        FakeOpenWebUI.prompts = [*self.prompts, body["messages"][-1]["content"]]
        self.answer(200, {"choices": [{"message": {"content": self.reply}}]})


class HandoffReview(Hub):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.owui = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeOpenWebUI)
        threading.Thread(target=cls.owui.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.owui.shutdown()
        super().tearDownClass()

    def setUp(self):
        self.ok("POST", "/api/settings", {"owui_url": f"http://127.0.0.1:{self.owui.server_address[1]}", "owui_key": "k"})
        FakeOpenWebUI.prompts, FakeOpenWebUI.reply = [], "# Handoff: test\n\n## Next steps\n1. Ship it"
        self.name = f"Review {self.id().rsplit('.', 1)[-1]}"
        self.pid = self.ok("POST", "/api/projects", {"name": self.name})["id"]
        self.cards = []
        for word in ("alpha", "bravo"):
            sid = f"{self.pid}-{word}"
            self.hook(sid, title=word)
            cid = f"code:{sid}"
            self.ok("PATCH", f"/api/cards/{self.q(cid)}", {"project": self.name})
            self.ok("POST", f"/api/cards/{self.q(cid)}/worklog", [{"ref": "a", "text": f"worked on {word}"}])
            self.cards.append(cid)

    def run_job(self, **body):
        job = self.ok("POST", "/api/generate", {"kind": "handoff", "scope": "project", "id": self.pid, **body})
        for _ in range(400):
            j = self.ok("GET", f"/api/jobs/{job['id']}")
            if j["status"] in ("done", "error"):
                self.assertEqual(j["status"], "done", j)
                return j
            time.sleep(0.02)
        self.fail("job did not finish")

    def draft(self):
        return self.ok("GET", f"/api/projects/{self.pid}/draft")

    def docs(self):
        return [d for d in self.ok("GET", f"/api/projects/{self.pid}")["docs"] if d["kind"] == "handoff"]

    def test_a_draft_stays_out_of_the_library_until_saved(self):
        self.run_job(review=True)
        s = self.draft()
        self.assertIn("Ship it", s["draft"]["text"])
        self.assertEqual(s["versions"], [])
        self.assertEqual(self.docs(), [])
        self.assertEqual(self.ok("GET", "/api/search?q=Ship")["results"], [])
        self.assertTrue(self.ok("GET", f"/api/projects/{self.pid}")["draft"])
        self.ok("POST", f"/api/projects/{self.pid}/draft/save")
        s = self.draft()
        self.assertIsNone(s["draft"])
        self.assertEqual(len(s["versions"]), 1)
        self.assertEqual(len(self.docs()), 1)
        self.assertIsNone(self.ok("GET", f"/api/projects/{self.pid}")["draft"])

    def test_left_out_sources_never_reach_the_model(self):
        self.run_job(review=True, exclude=[self.cards[0]])
        self.assertNotIn("worked on alpha", FakeOpenWebUI.prompts[-1])
        self.assertIn("worked on bravo", FakeOpenWebUI.prompts[-1])
        d = self.draft()["draft"]
        self.assertEqual((d["used"], d["excluded"]), ([self.cards[1]], [self.cards[0]]))

    def test_leaving_out_everything_is_refused(self):
        status, _ = self.call("POST", "/api/generate", {"kind": "handoff", "scope": "project", "id": self.pid, "review": True, "exclude": self.cards})
        self.assertEqual(status, 400)

    def test_the_previous_version_is_a_source_you_can_leave_out(self):
        FakeOpenWebUI.reply = "# Handoff: first\n\nUniquePreviousWording"
        self.run_job(review=True)
        self.assertNotIn("Previous handoff", FakeOpenWebUI.prompts[-1])
        self.ok("POST", f"/api/projects/{self.pid}/draft/save")
        self.assertEqual(self.draft()["sources"][0]["id"], "previous")
        self.run_job(review=True)
        self.assertIn("UniquePreviousWording", FakeOpenWebUI.prompts[-1])
        self.run_job(review=True, exclude=["previous"])
        self.assertNotIn("UniquePreviousWording", FakeOpenWebUI.prompts[-1])

    def test_each_save_is_a_new_version(self):
        for n in (1, 2):
            FakeOpenWebUI.reply = f"# Handoff\n\nVersion text {n}"
            self.run_job(review=True)
            self.ok("POST", f"/api/projects/{self.pid}/draft/save")
            time.sleep(0.01)
        self.assertEqual(len(self.draft()["versions"]), 2)
        self.assertEqual(self.docs()[0]["versions"], 2)

    def test_regenerating_replaces_the_draft(self):
        FakeOpenWebUI.reply = "first try"
        self.run_job(review=True)
        FakeOpenWebUI.reply = "second try"
        self.run_job(review=True)
        self.assertIn("second try", self.draft()["draft"]["text"])
        self.assertNotIn("first try", self.draft()["draft"]["text"])

    def test_discarding_saves_nothing(self):
        self.run_job(review=True)
        self.ok("DELETE", f"/api/projects/{self.pid}/draft")
        self.assertIsNone(self.draft()["draft"])
        self.assertEqual(self.docs(), [])
        self.assertEqual(self.call("POST", f"/api/projects/{self.pid}/draft/save")[0], 404)

    def test_secrets_are_redacted_in_the_draft(self):
        FakeOpenWebUI.reply = "key sk-ant-api03-abcdefghijklmnopqrstuvwxyz here"
        self.run_job(review=True)
        text = self.draft()["draft"]["text"]
        self.assertIn("[REDACTED:", text)
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz", text)

    def test_only_project_handoffs_have_a_draft(self):
        body = {"kind": "digest", "scope": "project", "id": self.pid, "review": True}
        self.assertEqual(self.call("POST", "/api/generate", body)[0], 400)
        self.assertEqual(self.call("POST", "/api/generate", {"kind": "handoff", "scope": "card", "id": self.cards[0], "review": True})[0], 400)

    def test_without_review_a_handoff_saves_straight_away(self):
        self.run_job()
        self.assertIsNone(self.draft()["draft"])
        self.assertEqual(len(self.docs()), 1)

    def test_merging_a_project_drops_its_draft(self):
        self.run_job(review=True)
        into = self.ok("POST", "/api/projects", {"name": self.name + " target"})["id"]
        self.ok("POST", f"/api/projects/{self.pid}/merge", {"into": into})
        self.assertEqual(self.call("GET", f"/api/projects/{self.pid}/draft")[0], 404)
        self.assertIsNone(self.ok("GET", f"/api/projects/{into}")["draft"])


class Attention(Hub):
    DOC = """# Handoff

Next steps in prose, not a section.

## Summary
- Not a step

## Next steps (numbered, actionable)
1. **Rotate** the `gateway` key.
   - A detail, not a step
2. [ ] Measure the workflow
3) Write the runbook

## Reference
- Not a step either
"""

    def make(self, name, doc=None):
        pid = self.ok("POST", "/api/projects", {"name": name})["id"]
        if doc is not None:
            self.ok("PUT", f"/api/owners/{self.q('project:' + pid)}/attachments?name=HANDOFF.md&kind=handoff&path=h", doc.encode())
        return pid

    def overview(self, pid):
        return next(p for p in self.ok("GET", f"/api/library/overview?today={TODAY}")["projects"] if p["id"] == pid)

    def test_next_steps_come_from_their_section_only(self):
        pid = self.make("Steps", self.DOC)
        r = self.ok("GET", f"/api/projects/{pid}/next-steps")
        self.assertEqual([s["title"] for s in r["steps"]], ["Rotate the gateway key", "Measure the workflow", "Write the runbook"])
        self.assertEqual(r["handoff"]["name"], "HANDOFF.md")

    def test_steps_already_on_the_task_list_are_marked(self):
        pid = self.make("Steps 2", self.DOC)
        self.ok("POST", "/api/tasks", {"project_id": pid, "title": "measure THE workflow"})
        steps = self.ok("GET", f"/api/projects/{pid}/next-steps")["steps"]
        self.assertEqual([s["exists"] for s in steps], [False, True, False])

    def test_a_bold_label_counts_as_a_section(self):
        pid = self.make("Steps 3", "Intro\n\n**Next steps**\n\n- First\n- Second\n\n# Other\n- No\n")
        self.assertEqual([s["title"] for s in self.ok("GET", f"/api/projects/{pid}/next-steps")["steps"]], ["First", "Second"])

    def test_other_section_labels_are_understood(self):
        for n, head in enumerate(("## Next up (nothing is started)", "### Still open (carried + new)", "# 9. First things to do in Claude Code", "## TODO", "**Follow-ups:**")):
            pid = self.make(f"Label {n}", f"# Doc\n\n## Done\n- Finished\n\n{head}\n\n- Only this\n")
            self.assertEqual([s["title"] for s in self.ok("GET", f"/api/projects/{pid}/next-steps")["steps"]], ["Only this"], head)

    def test_a_chat_only_project_is_not_flagged(self):
        pid = self.make("Chats")
        self.ok("POST", "/api/cards", {"title": "a chat", "project": "Chats"})
        self.assertFalse(self.overview(pid)["unrecorded"])

    def test_a_handoff_without_steps_or_no_handoff_gives_none(self):
        self.assertEqual(self.ok("GET", f"/api/projects/{self.make('Steps 4', '# Handoff')}/next-steps")["steps"], [])
        self.assertEqual(self.ok("GET", f"/api/projects/{self.make('Steps 5')}/next-steps"), {"handoff": None, "steps": []})

    def test_overdue_tasks_need_you(self):
        pid = self.make("Late", self.DOC)
        yesterday = (date.fromisoformat(TODAY) - timedelta(days=1)).isoformat()
        self.ok("POST", "/api/tasks", {"project_id": pid, "title": "Late task", "due": yesterday})
        self.ok("POST", "/api/tasks", {"project_id": pid, "title": "Later task", "due": TODAY})
        p = self.overview(pid)
        self.assertEqual((p["group"], p["overdue"], p["open_tasks"], p["next_step"]), ("needs", 1, 2, "Rotate the gateway key"))

    def test_work_with_no_handoff_is_flagged_after_the_limit(self):
        pid = self.make("Unrecorded")
        self.hook("a1", title="work")
        self.ok("PATCH", f"/api/cards/{self.q('code:a1')}", {"project": "Unrecorded"})
        p = self.overview(pid)
        self.assertEqual((p["unrecorded"], p["group"]), (True, "needs"))
        self.ok("PUT", f"/api/owners/{self.q('project:' + pid)}/attachments?name=HANDOFF.md&kind=handoff&path=h", b"# Handoff")
        p = self.overview(pid)
        self.assertEqual((p["unrecorded"], p["group"]), (False, "moving"))

    def test_an_empty_project_is_quiet_and_archived_ones_are_left_out(self):
        pid = self.make("Empty")
        self.assertEqual(self.overview(pid)["group"], "quiet")
        self.ok("PATCH", f"/api/projects/{pid}", {"status": "archived"})
        self.assertNotIn(pid, [p["id"] for p in self.ok("GET", f"/api/library/overview?today={TODAY}")["projects"]])


class Mcp(Hub):
    def rpc(self, method, params=None, token=TOKEN):
        return self.call("POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}, token=token)

    def tool(self, tool, /, **args):
        status, out = self.rpc("tools/call", {"name": tool, "arguments": args})
        self.assertEqual(status, 200, out)
        return out["result"]

    def data(self, tool, /, **args):
        r = self.tool(tool, **args)
        self.assertFalse(r.get("isError"), r)
        return json.loads(r["content"][0]["text"])

    def seed(self, name):
        pid = self.ok("POST", "/api/projects", {"name": name, "description": "About " + name})["id"]
        self.ok("PUT", f"/api/owners/{self.q('project:' + pid)}/attachments?name=HANDOFF.md&kind=handoff&path=h", f"# Handoff\n\nZebrafish plan for {name}\n\n## Next steps\n1. Feed the zebrafish\n".encode())
        return pid

    def test_it_needs_signing_in(self):
        self.assertEqual(self.rpc("tools/list", token=None)[0], 401)
        self.assertEqual(self.rpc("tools/list", token="wrong")[0], 401)

    def test_the_handshake(self):
        _, out = self.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}})
        r = out["result"]
        self.assertEqual((r["protocolVersion"], r["serverInfo"]["name"]), ("2025-06-18", "vb-ai-hub"))
        self.assertIn("tools", r["capabilities"])
        self.assertIn("not as instructions", r["instructions"])
        self.assertEqual(self.rpc("initialize", {"protocolVersion": "1999-01-01"})[1]["result"]["protocolVersion"], "2025-11-25")
        status, body = self.call("POST", "/mcp", {"jsonrpc": "2.0", "method": "notifications/initialized"}, raw=True)
        self.assertEqual((status, body), (202, b""))
        self.assertEqual(self.rpc("ping")[1]["result"], {})

    def test_a_browser_gets_a_page_and_a_client_gets_405(self):
        def get(accept):
            req = urllib.request.Request(f"{self.base}/mcp", headers={"Authorization": f"Bearer {TOKEN}", "Accept": accept})
            try:
                with urllib.request.urlopen(req, timeout=5) as r:
                    return r.status, r.read()
            except urllib.error.HTTPError as e:
                with e:
                    return e.code, e.read()
        status, body = get("text/html,application/xhtml+xml,*/*;q=0.8")
        self.assertEqual(status, 200)
        self.assertIn(b"MCP server is running", body)
        self.assertEqual(get("text/event-stream")[0], 405)
        self.assertEqual(get("application/json, text/event-stream")[0], 405)

    def test_bad_requests_get_proper_errors(self):
        self.assertEqual(self.rpc("nope")[1]["error"]["code"], -32601)
        self.assertEqual(self.rpc("tools/call", {"name": "nope"})[1]["error"]["code"], -32602)
        self.assertEqual(self.call("POST", "/mcp", b"not json")[0], 400)
        self.assertEqual(self.call("POST", "/mcp", {"id": 1})[1]["error"]["code"], -32600)
        self.assertEqual(self.call("GET", "/mcp", raw=True)[0], 405)
        self.assertTrue(self.tool("get_project", name="Missing")["isError"])
        self.assertTrue(self.tool("add_task", project="Missing", title="x")["isError"])
        self.assertTrue(self.tool("read_document")["isError"])

    def test_every_tool_is_listed_with_a_schema_and_none_deletes(self):
        tools = self.rpc("tools/list")[1]["result"]["tools"]
        self.assertEqual({t["name"] for t in tools}, {"list_projects", "get_project", "search_library", "read_document", "list_tasks", "add_task", "update_task"})
        for t in tools:
            self.assertEqual(t["inputSchema"]["type"], "object")
            self.assertFalse(t["annotations"]["destructiveHint"])

    def test_reading_the_library(self):
        self.seed("Fishery")
        self.assertIn("Fishery", [p["name"] for p in self.data("list_projects")])
        p = self.data("get_project", name="Fishery")
        self.assertEqual((p["description"], p["next_steps"]), ("About Fishery", ["Feed the zebrafish"]))
        self.assertIn("Zebrafish plan", self.data("read_document", project="Fishery")["text"])
        self.assertIn("Zebrafish plan", self.data("read_document", project="Fishery", name="HANDOFF.md")["text"])
        self.assertIn("documents", self.data("read_document", project="Fishery", name="missing.md"))
        hits = self.data("search_library", query="zebrafish")
        self.assertEqual(hits[0]["project"], "Fishery")
        self.assertNotIn("[[", hits[0]["snippet"])

    def test_tasks_can_be_added_and_finished(self):
        pid = self.seed("Pond")
        t = self.data("add_task", project="Pond", title="Clean the pond", priority="high", due="2030-01-02", notes="Before spring")
        self.assertEqual((t["project_id"], t["priority"], t["due"]), (pid, "high", "2030-01-02"))
        self.assertIn(t["id"], [x["id"] for x in self.data("list_tasks", project="Pond")])
        self.data("update_task", task_id=t["id"], status="done")
        self.assertNotIn(t["id"], [x["id"] for x in self.data("list_tasks", project="Pond")])
        self.assertIn(t["id"], [x["id"] for x in self.data("list_tasks", project="Pond", include_done=True)])
        self.assertTrue(self.tool("update_task", task_id="nope", status="done")["isError"])
        self.assertTrue(self.tool("update_task", task_id=t["id"], due="soon")["isError"])


class Costs(Hub):
    def spend(self, sid, project, model, days):
        self.hook(sid, title=sid)
        self.ok("PATCH", f"/api/cards/{self.q('code:' + sid)}", {"project": project})
        self.ok("POST", f"/api/cards/{self.q('code:' + sid)}/usage", {"models": {model: {}}, "days": {d: {model: u} for d, u in days.items()}})

    def report(self):
        return self.ok("GET", f"/api/usage/report?today={TODAY}")

    def test_every_token_type_has_its_own_price(self):
        self.ok("POST", "/api/projects", {"name": "Priced"})
        self.ok("POST", "/api/settings", {"model_prices": "price-a = 10, 20, 1, 12.5"})
        self.spend("c1", "Priced", "price-a", {TODAY: {"input": 1_000_000, "output": 1_000_000, "cache_read": 2_000_000, "cache_write": 2_000_000}})
        r = self.report()["ranges"]["7"]
        self.assertAlmostEqual(next(m for m in r["by_model"] if m["name"] == "price-a")["cost"], 10 + 20 + 2 + 25)
        self.assertAlmostEqual(next(p for p in r["by_project"] if p["name"] == "Priced")["cost"], 57)
        self.assertAlmostEqual(self.report()["series"][-1]["cost"], r["cost"])

    def test_cache_prices_default_and_family_names_match(self):
        self.ok("POST", "/api/settings", {"model_prices": "fam = 10, 20\nfam-pro = 100, 200"})
        self.spend("c2", "", "fam-mini-9", {TODAY: {"cache_read": 1_000_000, "cache_write": 1_000_000}})
        self.spend("c3", "", "fam-pro-9", {TODAY: {"input": 1_000_000}})
        r = self.report()
        by = {m["name"]: m["cost"] for m in r["ranges"]["7"]["by_model"]}
        self.assertAlmostEqual(by["fam-mini-9"], 1 + 12.5)
        self.assertAlmostEqual(by["fam-pro-9"], 100)

    def test_a_model_without_a_price_is_reported_and_costs_nothing(self):
        self.ok("POST", "/api/settings", {"model_prices": ""})
        self.spend("c4", "", "mystery", {TODAY: {"output": 5_000_000}})
        r = self.report()
        self.assertIn("mystery", r["unpriced"])
        self.assertEqual(next(m for m in r["ranges"]["7"]["by_model"] if m["name"] == "mystery")["cost"], 0)

    def test_a_range_only_counts_its_days_and_parts_add_up(self):
        self.ok("POST", "/api/settings", {"model_prices": "ranged = 1, 1"})
        old = (date.fromisoformat(TODAY) - timedelta(days=12)).isoformat()
        self.spend("c5", "", "ranged", {TODAY: {"input": 1_000_000}, old: {"input": 3_000_000}})
        r = self.report()
        cost = lambda key: next(m for m in r["ranges"][key]["by_model"] if m["name"] == "ranged")["cost"]
        self.assertEqual((cost("7"), cost("30")), (1, 4))
        self.assertGreaterEqual(r["ranges"]["7"]["cost_before"], 3)
        for key in ("7", "30", "all"):
            g = r["ranges"][key]
            for part in ("by_model", "by_project", "by_source"):
                self.assertAlmostEqual(sum(x["cost"] for x in g[part]), g["cost"], places=6)

    def test_budget_figures(self):
        self.ok("POST", "/api/settings", {"model_prices": "budgeted = 1, 1", "budget_monthly": "100", "budget_daily": "2"})
        before = self.report()["budget"]
        self.spend("c6", "", "budgeted", {TODAY: {"input": 3_000_000}})
        b = self.report()["budget"]
        self.assertAlmostEqual(b["month"] - before["month"], 3)
        self.assertAlmostEqual(b["day"] - before["day"], 3)
        self.assertEqual((b["monthly"], b["daily"]), (100, 2))
        self.assertGreaterEqual(b["projected"], b["month"])

    def test_bad_settings_are_refused(self):
        self.assertEqual(self.call("POST", "/api/settings", {"model_prices": "no equals sign"})[0], 400)
        self.assertEqual(self.call("POST", "/api/settings", {"model_prices": "x = 1"})[0], 400)
        self.assertEqual(self.call("POST", "/api/settings", {"model_prices": "x = 1, -2"})[0], 400)
        self.assertEqual(self.call("POST", "/api/settings", {"budget_monthly": "lots"})[0], 400)
        self.assertEqual(self.call("POST", "/api/settings", {"budget_daily": "-1"})[0], 400)

    def test_budget_alerts_fire_once_per_level(self):
        code = """
import sys, tempfile, json
sys.path.insert(0, sys.argv[1])
from hub import db, notifications, usage
db.conn = db.connect()
sent = []
notifications.notify = lambda title, message, link: sent.append(title)
state = {"notify_url": "http://x", "usage_tz": "UTC", "public_url": "", "budget_alerted": "", "budget_alerted_day": ""}
notifications.setting = lambda k, d="": state.get(k, d)
notifications.set_setting = lambda k, v: state.__setitem__(k, v)
b = {"month": 85, "day": 1, "monthly": 100, "daily": 5, "projected": 90}
notifications.budget_state = lambda day: b
notifications.check_budget(); notifications.check_budget()
assert sent == ["Token spending at 80% of budget"], sent
b["month"] = 120
notifications.check_budget(); notifications.check_budget()
assert sent[1:] == ["Token spending at 100% of budget"], sent
b["month"] = 85
notifications.check_budget()
assert len(sent) == 2, sent
b["day"] = 9
notifications.check_budget(); notifications.check_budget()
assert sent[2:] == ["Daily token spending limit passed"], sent
state["notify_url"] = ""; sent.clear(); state["budget_alerted"] = ""
notifications.check_budget()
assert sent == [], sent
"""
        with tempfile.TemporaryDirectory() as d:
            run = subprocess.run([sys.executable, "-c", code, str(ROOT)], capture_output=True, text=True, env={**os.environ, "HUB_DB": f"{d}/hub.db"})
        self.assertEqual(run.returncode, 0, run.stderr)


class Sources(Hub):
    def add(self, url):
        return self.ok("POST", "/api/cards", {"title": "A chat", "url": url})

    def test_chat_links_get_their_source(self):
        for url, source in (("https://chatgpt.com/c/abc-1", "chatgpt"), ("https://chat.openai.com/c/abc-2", "chatgpt"), ("https://www.chatgpt.com/c/abc-3", "chatgpt"),
                            ("https://grok.com/chat/abc-4", "grok"), ("https://example.com/c/abc-5", "manual"),
                            ("https://notchatgpt.com/c/abc-6", "manual"), ("https://grok.com.example.net/chat/abc-7", "manual")):
            self.assertEqual(self.add(url)["source"], source, url)

    def test_adding_the_same_link_twice_updates_one_card(self):
        first = self.add("https://grok.com/chat/same?rid=1")
        self.assertEqual(self.add("https://grok.com/chat/same?rid=1/")["id"], first["id"])
        self.assertNotEqual(self.add("https://grok.com/chat/same?rid=2")["id"], first["id"])

    def test_scripts_can_post_as_any_named_source(self):
        for source in ("cursor", "codex", "chatgpt", "grok"):
            card = self.ok("POST", "/api/ingest", {"source": source, "id": "run-1", "title": "t", "worklog": "did it"})
            self.assertEqual((card["id"], card["source"]), (f"{source}:run-1", source))

    def test_the_page_knows_every_named_source(self):
        js = (ROOT / "web" / "js" / "core.js").read_text() + (ROOT / "web" / "js" / "board.js").read_text()
        extra = re.search(r"EXTRA_SOURCES = \[(.*?)\]", js).group(1)
        for source in re.findall(r'"(\w+)"', extra):
            self.assertRegex(js, rf'\b{source}: "')


class Concurrency(Hub):
    def test_parallel_requests_never_see_each_others_queries(self):
        """Threads share one SQLite connection; identical queries in parallel used to fail about 1 time in 70."""
        statuses = []

        def worker():
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
            for _ in range(150):
                conn.request("GET", "/api/auth/status")
                r = conn.getresponse()
                r.read()
                statuses.append(r.status)
            conn.close()

        threads = [threading.Thread(target=worker) for _ in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(set(statuses), {200}, collections.Counter(statuses))


class Sessions(Hub):
    def test_lifecycle_moves_columns(self):
        c = self.hook("life", "SessionStart", cwd="/tmp/p", project="p", title="Life")
        self.assertEqual((c["column"], c["status"]), ("active", "running"))
        c = self.hook("life", "Stop")
        self.assertEqual((c["column"], c["status"]), ("needs_me", "waiting"))
        c = self.hook("life", "UserPromptSubmit", prompt="more")
        self.assertEqual((c["column"], c["status"]), ("active", "running"))

    def test_session_keeps_first_project(self):
        self.hook("sticky", cwd="/tmp/a", project="alpha", title="Sticky")
        self.assertEqual(self.hook("sticky", "Stop", cwd="/tmp/b", project="beta")["project"], "alpha")

    def test_hand_edits_survive_hook_updates(self):
        self.hook("edit", cwd="/tmp/a", project="alpha", title="Auto title")
        self.ok("PATCH", f"/api/cards/{self.q('code:edit')}", {"title": "My title"})
        self.assertEqual(self.hook("edit", "Stop", title="Auto again")["title"], "My title")


class Projects(Hub):
    def test_rule_files_existing_and_new_items(self):
        p = self.ok("POST", "/api/projects", {"name": "Airlock"})
        self.hook("r1", cwd="/tmp/d", project="Desk", title="airlock patching")
        moved = self.ok("POST", "/api/rules", {"project_id": p["id"], "field": "title", "pattern": "airlock"})["moved"]
        self.assertGreaterEqual(moved, 1)
        self.assertEqual(self.hook("r2", cwd="/tmp/d", project="Desk", title="Airlock release")["project"], "Airlock")

    def test_hand_set_project_beats_rules(self):
        p = self.ok("POST", "/api/projects", {"name": "Rules1"})
        self.ok("POST", "/api/rules", {"project_id": p["id"], "field": "title", "pattern": "handset"})
        self.hook("h1", title="handset item")
        self.ok("PATCH", f"/api/cards/{self.q('code:h1')}", {"project": "Mine"})
        self.assertEqual(self.hook("h1", "Stop", title="handset item")["project"], "Mine")

    def test_rename_keeps_new_events_in_renamed_project(self):
        self.hook("n1", cwd="/tmp/x", project="OldName", title="one")
        self.ok("PATCH", f"/api/projects/{self.project('OldName')['id']}", {"name": "NewName"})
        self.assertEqual(self.hook("n2", cwd="/tmp/x", project="OldName", title="two")["project"], "NewName")

    def test_merge_moves_everything(self):
        a = self.ok("POST", "/api/projects", {"name": "MergeA"})
        b = self.ok("POST", "/api/projects", {"name": "MergeB"})
        self.hook("m1", title="m")
        self.ok("PATCH", f"/api/cards/{self.q('code:m1')}", {"project": "MergeA"})
        t = self.ok("POST", "/api/tasks", {"project_id": a["id"], "title": "T"})
        self.ok("PUT", f"/api/owners/{self.q('project:' + a['id'])}/attachments?name=a.md", b"# doc")
        out = self.ok("POST", f"/api/projects/{a['id']}/merge", {"into": b["id"]})
        self.assertEqual([c["id"] for c in out["cards"]], ["code:m1"])
        self.assertIn(t["id"], [x["id"] for x in out["tasks"]])
        self.assertIn("a.md", [d["name"] for d in out["docs"]])
        self.assertIsNone(self.project("MergeA"))

    def test_tasks_validate_due_and_priority(self):
        p = self.ok("POST", "/api/projects", {"name": "Plan"})
        t = self.ok("POST", "/api/tasks", {"project_id": p["id"], "title": "x", "priority": "urgent", "due": "2030-01-02"})
        self.assertEqual((t["priority"], t["due"]), ("urgent", "2030-01-02"))
        self.assertEqual(self.call("PATCH", f"/api/tasks/{t['id']}", {"due": "soon"})[0], 400)

    def test_export_contains_item_folders(self):
        self.hook("ex1", title="Exported item")
        self.ok("PATCH", f"/api/cards/{self.q('code:ex1')}", {"project": "ExportMe"})
        self.ok("PUT", f"/api/cards/{self.q('code:ex1')}/attachments?name=notes.md&kind=doc", b"# notes")
        z = zipfile.ZipFile(io.BytesIO(self.ok("GET", f"/api/projects/{self.project('ExportMe')['id']}/bundle", raw=True)))
        names = z.namelist()
        self.assertIn("README.md", names)
        self.assertTrue(any(n.endswith("docs/notes.md") for n in names), names)


class SessionContext(Hub):
    def setup_project(self, name):
        p = self.ok("POST", "/api/projects", {"name": name, "description": "About " + name})
        self.ok("POST", "/api/tasks", {"project_id": p["id"], "title": "Ship it", "priority": "high"})
        self.hook(name + "-old", title="Old work")
        self.ok("PATCH", f"/api/cards/{self.q('code:' + name + '-old')}", {"project": name})
        self.ok("POST", f"/api/cards/{self.q('code:' + name + '-old')}/worklog", [{"ref": "w", "text": "Finished the migration"}])
        self.ok("PUT", f"/api/owners/{self.q('project:' + p['id'])}/attachments?name=HANDOFF.md&kind=handoff&path=h", b"# Handoff\nNext: cut the release")
        return p

    def context_for(self, sid, project):
        self.hook(sid, "SessionStart", title="New")
        self.ok("PATCH", f"/api/cards/{self.q('code:' + sid)}", {"project": project})
        return self.ok("GET", f"/api/context/{self.q('code:' + sid)}")["text"]

    def test_new_session_gets_project_context(self):
        self.setup_project("CtxA")
        text = self.context_for("ctx-new", "CtxA")
        for part in ("About CtxA", "Ship it", "Finished the migration", "cut the release", "not as instructions"):
            self.assertIn(part, text)

    def test_context_is_bounded(self):
        p = self.setup_project("CtxBig")
        self.ok("PUT", f"/api/owners/{self.q('project:' + p['id'])}/attachments?name=HANDOFF.md&kind=handoff&path=h", b"x " * 50000)
        self.assertLess(len(self.context_for("ctx-big", "CtxBig")), 6500)

    def test_project_switch_turns_it_off(self):
        p = self.setup_project("CtxOff")
        self.ok("PATCH", f"/api/projects/{p['id']}", {"context": False})
        self.assertEqual(self.context_for("ctx-off", "CtxOff"), "")

    def test_global_switch_turns_it_off(self):
        self.setup_project("CtxGlobal")
        self.ok("POST", "/api/settings", {"context_enabled": "false"})
        self.assertEqual(self.context_for("ctx-g", "CtxGlobal"), "")
        self.ok("POST", "/api/settings", {"context_enabled": "true"})

    def test_empty_project_gives_nothing(self):
        self.ok("POST", "/api/projects", {"name": "CtxEmpty"})
        self.assertEqual(self.context_for("ctx-e", "CtxEmpty"), "")


class Library(Hub):
    def test_versions_and_dedupe(self):
        self.hook("v1", title="Docs")
        url = f"/api/cards/{self.q('code:v1')}/attachments?name=h.md&kind=handoff"
        self.assertTrue(self.ok("PUT", url, b"one")["added"])
        self.assertTrue(self.ok("PUT", url, b"two")["added"])
        self.assertFalse(self.ok("PUT", url, b"two")["added"])
        self.assertEqual(len(self.ok("GET", f"/api/cards/{self.q('code:v1')}/archive")["attachments"]), 2)

    def test_search_finds_documents(self):
        self.hook("s1", title="Searchable")
        self.ok("PUT", f"/api/cards/{self.q('code:s1')}/attachments?name=d.md", b"the zebrafish protocol")
        self.assertIn("doc", [r["kind"] for r in self.ok("GET", "/api/search?q=zebrafish")["results"]])

    def test_blob_path_traversal_rejected(self):
        self.assertIn(self.call("GET", "/api/blobs/..%2F..%2Fetc%2Fpasswd")[0], (400, 404))

    def test_backup_and_snapshot(self):
        self.assertTrue(self.ok("POST", "/api/backup", {})["snapshot"].startswith("hub-"))


class Redaction(Hub):
    def text(self, sid, value):
        self.hook(sid, title="r")
        self.ok("POST", f"/api/cards/{self.q('code:' + sid)}/worklog", [{"ref": "a", "text": value}])
        return self.ok("GET", f"/api/cards/{self.q('code:' + sid)}/archive")["worklog"][0]["text"]

    def test_real_secrets_removed(self):
        for i, s in enumerate(("sk-ant-api03-abcdefghijklmnopqrstuvwxyz", "glpat-AbCdEfGhIjKlMnOpQrSt12", "password = Sup3rS3cret!",
                               "https://u:hunter2pass@host.example/x", TOKEN)):
            out = self.text(f"sec{i}", f"before {s} after")
            self.assertIn("[REDACTED:", out)
            self.assertNotIn(s.split("= ")[-1], out)

    def test_prose_and_placeholders_kept(self):
        for i, s in enumerate(("the password:\n```bash", "GRUB password: hashed (x)", "token=${HUB_TOKEN}",
                               "must start with `-----BEGIN OPENSSH PRIVATE KEY-----` and end", "commit 1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b")):
            self.assertEqual(self.text(f"pro{i}", s), s)

    def test_prompts_in_activity_log_are_redacted(self):
        self.hook("evt", title="e", prompt="use sk-ant-api03-abcdefghijklmnopqrstuvwxyz now")
        details = " ".join(e["detail"] for e in self.ok("GET", f"/api/cards/{self.q('code:evt')}/events")["events"])
        self.assertNotIn("sk-ant-api03", details)

    def test_inside_gzip_transcripts(self):
        self.hook("gz", title="g")
        body = gzip.compress(b'{"text":"password=Sup3rS3cret9"}\n')
        sha = self.ok("PUT", f"/api/cards/{self.q('code:gz')}/attachments?kind=transcript&name=t.jsonl.gz&path=t&replace=1", body)["sha"]
        self.assertNotIn(b"Sup3rS3cret9", gzip.decompress(self.ok("GET", f"/api/blobs/{sha}", raw=True)))


class Deletion(Hub):
    def test_delete_goes_to_trash_and_restores(self):
        cid = self.q("code:t1")
        self.hook("t1", title="Trash me")
        self.ok("DELETE", f"/api/cards/{cid}")
        self.assertNotIn("code:t1", [c["id"] for c in self.ok("GET", "/api/cards")["cards"]])
        self.assertIn("code:t1", [c["id"] for c in self.ok("GET", "/api/cards?trash=1")["cards"]])
        self.ok("POST", f"/api/cards/{cid}/restore", {})
        self.assertIn("code:t1", [c["id"] for c in self.ok("GET", "/api/cards")["cards"]])

    def test_reconcile_guard_refuses_mass_removal(self):
        for i in range(8):
            self.hook(f"g{i}", title="g", host="guard-host")
        self.assertEqual(self.call("POST", "/api/reconcile/code", {"host": "guard-host", "sessions": ["none"], "cleanup_days": 30})[0], 400)

    def test_reconcile_removes_only_this_hosts_missing_sessions(self):
        self.hook("keep", title="k", host="h1")
        self.hook("gone", title="g", host="h1")
        self.hook("other", title="o", host="h2")
        self.assertEqual(self.ok("POST", "/api/reconcile/code", {"host": "h1", "sessions": ["keep"], "cleanup_days": 30})["removed"], 1)
        ids = [c["id"] for c in self.ok("GET", "/api/cards")["cards"]]
        self.assertIn("code:keep", ids)
        self.assertIn("code:other", ids)
        self.assertNotIn("code:gone", ids)

    def test_reconcile_claims_unowned_sessions_but_never_removes_them(self):
        self.hook("legacy-here", title="l")
        self.hook("legacy-elsewhere", title="l")
        self.ok("POST", "/api/reconcile/code", {"host": "h3", "sessions": ["legacy-here"], "cleanup_days": 30})
        ids = [c["id"] for c in self.ok("GET", "/api/cards")["cards"]]
        self.assertIn("code:legacy-elsewhere", ids)
        self.assertEqual(self.ok("GET", f"/api/cards/{self.q('code:legacy-here')}")["host"], "h3")

    def test_claude_import_prune(self):
        chat = lambda u: {"uuid": u, "name": u, "created_at": "2026-10-01T00:00:00Z", "updated_at": "2026-10-02T00:00:00Z", "chat_messages": []}
        self.ok("POST", "/api/import", [chat("k1"), chat("k2")])
        self.assertEqual(self.ok("POST", "/api/import?prune=1", [chat("k1")])["removed"], 1)


class Usage(Hub):
    def test_days_and_totals(self):
        before = self.stats()
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        self.hook("u1", title="u")
        self.ok("POST", f"/api/cards/{self.q('code:u1')}/usage", {
            "models": {"m": {"input": 3, "output": 30, "messages": 2}},
            "days": {TODAY: {"m": {"input": 1, "output": 10, "messages": 1}}, yesterday: {"m": {"input": 2, "output": 20, "messages": 1}}}})
        after = self.stats()
        self.assertEqual(after["today"]["output"] - before["today"]["output"], 10)
        self.assertEqual(after["week"]["output"] - before["week"]["output"], 30)
        self.assertEqual(after["all"]["total"] - before["all"]["total"], 33)

    def test_deleting_a_card_keeps_spent_tokens(self):
        self.hook("u2", title="u")
        self.ok("POST", f"/api/cards/{self.q('code:u2')}/usage", {"models": {"m": {"output": 500}}})
        before = self.stats()["all"]["total"]
        self.ok("DELETE", f"/api/cards/{self.q('code:u2')}?purge=1")
        self.assertEqual(self.stats()["all"]["total"], before)

    def test_replace_without_days_adds_only_the_change(self):
        before = self.stats()["today"]["output"]
        for n in (100, 150):
            self.ok("POST", f"/api/cards/{self.q('owui:u3')}/usage", {"replace": True, "models": {"m": {"output": n}}})
        self.assertEqual(self.stats()["today"]["output"] - before, 150)

    def test_reindex_replaces_instead_of_doubling(self):
        self.hook("u4", title="u")
        body = {"replace": True, "models": {"m": {"output": 7}}, "days": {TODAY: {"m": {"output": 7}}}}
        before = self.stats()["all"]["output"]
        self.ok("POST", f"/api/cards/{self.q('code:u4')}/usage", body)
        self.ok("POST", f"/api/cards/{self.q('code:u4')}/usage", body)
        self.assertEqual(self.stats()["all"]["output"] - before, 7)

    def test_project_usage_counts_only_its_own_cards(self):
        pid = self.ok("POST", "/api/projects", {"name": "Burn"})["id"]
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        old = (date.today() - timedelta(days=40)).isoformat()
        self.hook("u5", title="mine")
        self.hook("u6", title="other")
        self.ok("PATCH", f"/api/cards/{self.q('code:u5')}", {"project": "Burn"})
        self.ok("POST", f"/api/cards/{self.q('code:u5')}/usage", {
            "models": {"m": {"input": 7, "output": 70}},
            "days": {TODAY: {"m": {"input": 1, "output": 10}}, yesterday: {"m": {"input": 2, "output": 20}}, old: {"m": {"input": 4, "output": 40}}}})
        self.ok("POST", f"/api/cards/{self.q('code:u6')}/usage", {"models": {"m": {"output": 900}}})
        u = self.ok("GET", f"/api/projects/{pid}/usage?today={TODAY}")
        self.assertEqual((u["today"]["total"], u["week"]["total"], u["month"]["total"], u["all"]["total"]), (11, 33, 33, 77))
        self.assertEqual([d["total"] for d in u["series"][-2:]], [22, 11])
        self.assertEqual(u["series"][-1]["day"], TODAY)
        self.assertEqual([(m["name"], m["output"]) for m in u["by_model"]], [("m", 70)])
        self.assertEqual([(c["title"], c["total"]) for c in u["by_card"]], [("mine", 77)])
        self.assertEqual(self.call("GET", "/api/projects/nope/usage")[0], 404)

    def test_usage_report_splits_by_range(self):
        self.ok("POST", "/api/projects", {"name": "Report"})
        day = lambda n: (date.fromisoformat(TODAY) - timedelta(days=n)).isoformat()
        before = self.ok("GET", f"/api/usage/report?today={TODAY}")
        self.hook("u7", title="reported")
        self.ok("PATCH", f"/api/cards/{self.q('code:u7')}", {"project": "Report"})
        self.ok("POST", f"/api/cards/{self.q('code:u7')}/usage", {
            "models": {"report-model": {"input": 7, "output": 700}},
            "days": {TODAY: {"report-model": {"input": 1, "output": 100}}, day(10): {"report-model": {"input": 2, "output": 200}}, day(40): {"report-model": {"input": 4, "output": 400}}}})
        self.ok("POST", "/api/usage/local", {"rows": [{"day": TODAY, "client": "report-app", "model": "m", "requests": 3, "input": 30, "output": 3}]})
        r = self.ok("GET", f"/api/usage/report?today={TODAY}")
        added = lambda key: r["ranges"][key]["claude"]["total"] - before["ranges"][key]["claude"]["total"]
        self.assertEqual((added("7"), added("30"), added("all")), (101, 303, 707))
        project = lambda key: next(p for p in r["ranges"][key]["by_project"] if p["name"] == "Report")["total"]
        self.assertEqual((project("7"), project("30"), project("all")), (101, 303, 707))
        model = lambda key: next(m for m in r["ranges"][key]["by_model"] if m["name"] == "report-model")["output"]
        self.assertEqual((model("7"), model("all")), (100, 700))
        client = next(c for c in r["ranges"]["7"]["local"]["by_client"] if c["name"] == "report-app")
        self.assertEqual((client["requests"], client["total"]), (3, 33))
        self.assertEqual(r["series"][-1]["day"], TODAY)
        days = [d["day"] for d in r["series"]]
        self.assertEqual(days, sorted(set(days)))
        self.assertEqual(len(days), (date.fromisoformat(TODAY) - date.fromisoformat(days[0])).days + 1)
        self.assertIn(day(40), days)
        self.assertEqual(next(d for d in r["series"] if d["day"] == day(40))["output"] - next((d["output"] for d in before["series"] if d["day"] == day(40)), 0), 400)

    def test_ticker_totals_keep_their_shape(self):
        s = self.stats()
        self.assertNotIn("by_card", s)
        self.assertEqual(s["series"][-1]["day"], TODAY)
        self.assertEqual(set(s["series"][-1]), {"day", "output", "total", "gpu"})

    def test_gpu_feed_is_idempotent(self):
        rows = {"rows": [{"day": TODAY, "client": "c", "model": "m", "requests": 2, "input": 40, "output": 2}]}
        self.assertEqual(self.ok("POST", "/api/usage/gpu", rows)["changed"], 1)
        self.assertEqual(self.ok("POST", "/api/usage/gpu", rows)["changed"], 0)
        g = self.stats()["gpu"]
        self.assertEqual((g["today"]["total"], g["today"]["requests"]), (42, 2))


class HookParsing(unittest.TestCase):
    """The transcript scanner, without a server."""

    @classmethod
    def setUpClass(cls):
        import importlib.util
        spec = importlib.util.spec_from_file_location("hook", ROOT / "hook.py")
        cls.h = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.h)

    def rec(self, mid, out, ts="2026-11-01T12:00:00Z", **kw):
        m = {"id": mid, "model": "claude-x", "usage": {"input_tokens": 1, "output_tokens": out, "cache_read_input_tokens": 5}, "content": kw.pop("content", [])}
        m.update(kw)
        return json.dumps({"type": "assistant", "timestamp": ts, "uuid": mid + str(out), "message": m}).encode() + b"\n"

    def scan(self, data):
        return self.h.scan_transcript(io.BytesIO(data), 0)

    def test_split_records_count_once(self):
        r = self.scan(self.rec("a", 10) + self.rec("a", 10) + self.rec("b", 4))
        self.assertEqual(r["usage"]["claude-x"]["output"], 14)
        self.assertEqual(r["usage"]["claude-x"]["messages"], 2)

    def test_partial_last_line_is_left_for_next_run(self):
        full = self.rec("a", 10)
        r = self.scan(full + b'{"type":"assist')
        self.assertEqual(r["offset"], len(full))

    def test_final_reply_and_file_edits(self):
        tool = [{"type": "tool_use", "name": "Write", "input": {"file_path": "/x/a.md"}}]
        text = [{"type": "text", "text": "All done."}]
        r = self.scan(self.rec("a", 1, content=tool) + self.rec("b", 1, content=text, stop_reason="end_turn"))
        self.assertEqual(r["files"][0]["path"], "/x/a.md")
        self.assertEqual(r["logs"][0]["text"], "All done.")

    def test_context_only_on_fresh_sessions(self):
        calls = []
        real, self.h.request = self.h.request, lambda *a, **k: calls.append(a) or {"text": "ctx"}
        out = io.StringIO()
        try:
            import contextlib
            with contextlib.redirect_stdout(out):
                self.h.print_context({"hook_event_name": "SessionStart", "source": "resume", "session_id": "s"})
                self.h.print_context({"hook_event_name": "Stop", "session_id": "s"})
                self.assertEqual(calls, [])
                self.h.print_context({"hook_event_name": "SessionStart", "source": "startup", "session_id": "s"})
        finally:
            self.h.request = real
        self.assertEqual(json.loads(out.getvalue())["hookSpecificOutput"]["additionalContext"], "ctx")

    def test_web_url(self):
        self.assertEqual(self.h.web_url("git@code.example:a/b.git"), "https://code.example/a/b")
        self.assertEqual(self.h.web_url("https://oauth2:tok@code.example/a/b.git"), "https://code.example/a/b")

    def test_subagents_counted_and_resumed(self):
        with tempfile.TemporaryDirectory() as d:
            sub = Path(d, "s1", "subagents")
            sub.mkdir(parents=True)
            (sub / "agent-1.jsonl").write_bytes(self.rec("a", 9))
            offsets = {}
            totals, _ = self.h.subagent_usage(str(Path(d, "s1.jsonl")), offsets)
            self.assertEqual(totals["claude-x"]["output"], 9)
            self.assertEqual(self.h.subagent_usage(str(Path(d, "s1.jsonl")), offsets)[0], {})


class Security(AccountHelpers, Hub):
    """One or more tests per finding in the November 2026 security review."""

    def conn(self):
        return http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)

    def get(self, path, headers=None):
        c = self.conn()
        c.request("GET", path, headers=headers or {})
        r = c.getresponse()
        body = r.read()
        c.close()
        return r, body

    def member(self, name="member2"):
        self.ensure_admin()
        status, _ = self.call("POST", "/api/users", {"username": name, "password": self.PW})
        self.assertIn(status, (201, 400))  # 400 once the name exists
        return self.sign_in(name)

    # 1. Upload types can't add response headers
    def test_mime_cannot_inject_headers(self):
        cid = self.hook("mime-inject", title="m")["id"]
        q = urllib.parse.urlencode({"name": "a.txt", "kind": "upload", "mime": "text/plain\r\nX-Injected: yes\r\nContent-Disposition: inline"})
        res = self.ok("PUT", f"/api/cards/{self.q(cid)}/attachments?{q}", b"hello")
        r, body = self.get(f"/api/blobs/{res['sha']}", {"Authorization": f"Bearer {TOKEN}"})
        self.assertEqual((r.status, body), (200, b"hello"))
        self.assertIsNone(r.getheader("X-Injected"))
        self.assertEqual(len(r.msg.get_all("Content-Disposition")), 1)
        self.assertEqual(r.getheader("Content-Type"), "text/plain; charset=utf-8")
        mimes = [a["mime"] for a in self.ok("GET", f"/api/cards/{self.q(cid)}/archive")["attachments"]]
        self.assertEqual(mimes, ["text/plain"])

    def test_svg_and_html_uploads_download_as_text(self):
        cid = self.hook("svg-up", title="s")["id"]
        for name, mime in (("x.svg", "image/svg+xml"), ("x.html", "text/html")):
            q = urllib.parse.urlencode({"name": name, "kind": "upload", "mime": mime})
            sha = self.ok("PUT", f"/api/cards/{self.q(cid)}/attachments?{q}", b"<svg onload=alert(1)>")["sha"]
            r, _ = self.get(f"/api/blobs/{sha}", {"Authorization": f"Bearer {TOKEN}"})
            self.assertNotIn("svg", r.getheader("Content-Type"))
            self.assertNotIn("html", r.getheader("Content-Type"))
            self.assertIn("sandbox", r.getheader("Content-Security-Policy"))
            self.assertTrue(r.getheader("Content-Disposition").startswith("attachment"))

    # 2. The address token is for the live stream only, and a wrong token never falls back to a cookie
    def test_query_token_only_for_the_stream(self):
        self.assertEqual(self.call("GET", f"/api/cards?token={TOKEN}", token=None)[0], 401)
        self.assertEqual(self.call("GET", f"/api/users?token={TOKEN}", token=None)[0], 401)
        c = self.conn()
        c.request("GET", f"/api/stream?token={TOKEN}")
        r = c.getresponse()
        self.assertEqual(r.status, 200)
        c.close()

    def test_wrong_token_is_not_rescued_by_a_cookie(self):
        h = self.member("cookie-user")
        self.assertEqual(self.raw("GET", "/api/cards", headers=h)[0], 200)
        self.assertEqual(self.raw("GET", "/api/cards?token=wrong", headers=h)[0], 401)
        self.assertEqual(self.raw("GET", "/api/stream?token=wrong", headers=h)[0], 401)
        self.assertEqual(self.raw("GET", "/api/cards", headers={**h, "Authorization": "Bearer wrong"})[0], 401)
        # Signing in with a made-up token, as a request skipping single sign-on at the proxy would, is refused.
        self.assertEqual(self.raw("POST", "/api/auth/login?token=x", {"username": "cookie-user", "password": self.PW})[0], 401)
        self.assertEqual(self.raw("POST", "/api/auth/login", {"username": "cookie-user", "password": self.PW}, {"Authorization": "Bearer x"})[0], 401)

    def test_proxy_example_has_no_address_token_bypass(self):
        conf = (ROOT / "examples/reverse-proxy/nginx-authentik.conf").read_text()
        self.assertNotIn("token=", conf)
        self.assertIn('"^/healthz(\\?|$)"', conf)

    # 3. Members use the board and library; destructive and admin actions are for admins
    def test_member_cannot_destroy_or_reshape(self):
        h = self.member()
        cid = self.hook("member-victim", title="v")["id"]
        p1 = self.ok("POST", "/api/projects", {"name": "m-one"})
        p2 = self.ok("POST", "/api/projects", {"name": "m-two"})
        t = self.ok("POST", "/api/tasks", {"project_id": p1["id"], "title": "keep me"})
        refused = [
            ("DELETE", f"/api/cards/{self.q(cid)}?purge=1", None),
            ("POST", "/api/cards/bulk", {"ids": [cid], "purge": True}),
            ("POST", f"/api/projects/{p1['id']}/merge", {"into": p2["id"]}),
            ("PATCH", f"/api/projects/{p1['id']}", {"name": "m-two"}),
            ("DELETE", f"/api/tasks/{t['id']}", None),
            ("POST", "/api/import?prune=1", [{"uuid": "x", "chat_messages": []}]),
            ("POST", "/api/reconcile/code", {"host": "h", "sessions": ["a"]}),
            ("POST", "/api/notify/test", {}),
            ("POST", "/api/integrations/owui/sync", {}),
            ("POST", "/api/generate", {"kind": "handoff", "scope": "project", "id": p1["id"]}),
            ("POST", "/api/rules", {"project_id": p1["id"], "field": "title", "pattern": "x"}),
            ("POST", "/api/usage/local", {"rows": []}),
            ("POST", "/api/events", {"hook_event_name": "Stop", "session_id": "m"}),
            ("POST", "/api/tools/file-chat", {"chat_id": "a", "project": "p"}),
        ]
        for method, path, body in refused:
            self.assertEqual(self.raw(method, path, body, h)[0], 403, f"{method} {path}")
        self.assertEqual(self.call("GET", f"/api/cards/{self.q(cid)}")[0], 200)
        self.assertEqual(self.call("GET", f"/api/tasks/{t['id']}")[0], 200)

    def test_member_can_still_work(self):
        h = self.member("worker")
        pid = self.ok("POST", "/api/projects", {"name": "m-work"})["id"]
        status, card, _ = self.raw("POST", "/api/cards", {"title": "an idea", "project": "m-work"}, h)
        self.assertEqual(status, 201)
        cid = self.q(card["id"])
        for method, path, body, want in [
            ("PATCH", f"/api/cards/{cid}", {"column": "active", "notes": "n"}, 200),
            ("DELETE", f"/api/cards/{cid}", None, 200),
            ("POST", f"/api/cards/{cid}/restore", {}, 200),
            ("POST", "/api/tasks", {"project_id": pid, "title": "do it"}, 201),
            ("PATCH", f"/api/projects/{pid}", {"description": "about"}, 200),
            ("POST", "/api/cards/bulk", {"ids": [card["id"]], "set": {"column": "done"}}, 200),
        ]:
            self.assertEqual(self.raw(method, path, body, h)[0], want, f"{method} {path}")

    # 4. Only web addresses become links
    def test_links_must_be_web_urls(self):
        for url in ("javascript:alert(1)", "data:text/html,hi", "vbscript:x", " javascript:alert(1)", "JaVaScRiPt:alert(1)"):
            status, card = self.call("POST", "/api/cards", {"title": "t", "url": url})
            self.assertEqual(status, 201)
            self.assertFalse(card.get("url"), url)
            self.assertFalse(self.ok("PATCH", f"/api/cards/{self.q(card['id'])}", {"url": url}).get("url"), url)
        p = self.ok("POST", "/api/projects", {"name": "linky"})
        out = self.ok("PATCH", f"/api/projects/{p['id']}", {"links": [{"label": "x", "url": "javascript:alert(1)"}, {"label": "ok", "url": "https://example.com"}]})
        self.assertEqual([l["url"] for l in out["project"]["links"]], ["https://example.com"])
        cid = self.hook("linky-sess", title="l")["id"]
        self.ok("POST", f"/api/cards/{self.q(cid)}/links", [{"kind": "pr", "key": "k", "url": "javascript:alert(1)"}])
        self.assertEqual([l["url"] for l in self.ok("GET", f"/api/cards/{self.q(cid)}/archive")["links"]], [""])
        self.assertFalse(self.ok("POST", "/api/ingest", {"source": "tool", "id": "u1", "url": "javascript:alert(1)"}).get("url"))

    def test_the_page_only_links_web_addresses(self):
        js = "\n".join(f.read_text() for f in (ROOT / "web" / "js").glob("*.js"))
        self.assertEqual(re.findall(r'href="\$\{esc\([^)]*url\)\}"', js), [])

    # 5. A status is a short plain label
    def test_ingest_status_is_plain_text(self):
        card = self.ok("POST", "/api/ingest", {"source": "chatgpt", "id": "s1", "title": "t", "status": "<span><img src=x onerror=alert(1)>"})
        self.assertNotIn("<", card["status"])
        self.assertLessEqual(len(card["status"]), 40)
        self.assertNotIn('startsWith("<span")', (ROOT / "web/js/drawer.js").read_text())

    # 6. A chat id can't change the Open WebUI path
    def test_chat_id_cannot_change_the_path(self):
        seen = []

        class Fake(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                seen.append(self.path)
                b = b"{}"
                self.send_response(200)
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Fake)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            self.ok("POST", "/api/settings", {"owui_url": f"http://127.0.0.1:{srv.server_port}", "owui_key": "k" * 20})
            for chat_id in ("../../api/v1/users/", "a/b", "a%2Fb", "x" * 65):
                self.assertEqual(self.call("POST", "/api/tools/file-chat", {"chat_id": chat_id, "project": "p"})[0], 400, chat_id)
            self.assertFalse([p for p in seen if "/chats/" in p and ("." in p or "%2F" in p)], seen)
        finally:
            self.ok("POST", "/api/settings", {"owui_url": "", "owui_key": ""})
            srv.shutdown()

    # 7. Every free-text field is scrubbed, and more key formats are caught
    def test_every_free_text_field_is_scrubbed(self):
        card = self.ok("POST", "/api/cards", {"title": "n", "notes": "password = SuperSecret123!", "url": "https://user:Hunter2pass@git.example.com/repo"})
        self.assertNotIn("SuperSecret123", card["notes"])
        self.assertNotIn("Hunter2pass", card["url"])
        card = self.ok("PATCH", f"/api/cards/{self.q(card['id'])}", {"notes": "token=ghp_" + "A" * 36, "title": "api_key=sk-live-" + "d" * 24})
        self.assertNotIn("ghp_AAAA", card["notes"])
        self.assertNotIn("sk-live-dddd", card["title"])
        p = self.ok("POST", "/api/projects", {"name": "scrub", "description": "secret=abcdef123456789"})
        self.assertNotIn("abcdef123456789", p["description"])
        out = self.ok("PATCH", f"/api/projects/{p['id']}", {"goals": "AKIAABCDEFGHIJKLMNOP", "links": [{"label": "repo", "url": "https://u:Hunter2pass@x.example/r"}]})
        self.assertNotIn("AKIAABCDEFGHIJKLMNOP", out["project"]["goals"])
        self.assertNotIn("Hunter2pass", out["project"]["links"][0]["url"])
        t = self.ok("POST", "/api/tasks", {"project_id": p["id"], "title": "x", "notes": "api_key=sk-live-" + "b" * 24})
        self.assertNotIn("sk-live-bbbb", t["notes"])
        t = self.ok("PATCH", f"/api/tasks/{t['id']}", {"notes": "password: Hunter2pass!"})
        self.assertNotIn("Hunter2pass", t["notes"])
        cid = self.hook("scrub-sess", title="s", cwd="/srv/app")["id"]
        q = urllib.parse.urlencode({"name": "token=ghp_" + "B" * 36 + ".md", "kind": "doc", "path": "/x/password=Hunter2pass.md"})
        self.ok("PUT", f"/api/cards/{self.q(cid)}/attachments?{q}", b"text")
        self.ok("POST", f"/api/cards/{self.q(cid)}/files", [{"path": "/x/secret=Hunter2pass/a.py"}])
        a = self.ok("GET", f"/api/cards/{self.q(cid)}/archive")
        self.assertNotIn("Hunter2pass", json.dumps(a))
        self.assertNotIn("ghp_BBBB", json.dumps(a))

    def test_more_key_formats(self):
        for s in ("https://hooks.slack.com/services/T000/B000/XXXXXXXXXXXXXXXXXXXXXXXX", "123456789:AAE_abcdefghijklmnopqrstuvwxyz0123456",
                  "sk_live_" + "c" * 24, "whsec_" + "e" * 32, "SG.abcdefghijklmnopqr.abcdefghijklmnopqrstu", "npm_" + "a" * 36,
                  "hf_" + "a" * 34, "ya29." + "a" * 30, "Authorization: Basic dXNlcjpwYXNzd29yZA==",
                  "AccountKey=abcdefghijklmnopqrstuvwxyz0123==", "https://discord.com/api/webhooks/123/" + "a" * 40,
                  "postgres://app:pa/ss@db.local:5432/x"):
            notes = self.ok("POST", "/api/cards", {"title": "r", "notes": f"before {s} after"})["notes"]
            self.assertIn("REDACTED", notes, s)
            self.assertTrue(notes.startswith("before ") and notes.endswith(" after"), notes)

    # 8. Non-Latin names download and export
    def test_unicode_names_download_and_export(self):
        card = self.ok("POST", "/api/cards", {"title": "Zażółć gęślą jaźń"})
        q = urllib.parse.urlencode({"name": "notatki_żółć.txt", "kind": "upload"})
        res = self.ok("PUT", f"/api/owners/{self.q(card['id'])}/attachments?{q}", b"tekst")
        r, body = self.get(f"/api/blobs/{res['sha']}", {"Authorization": f"Bearer {TOKEN}"})
        self.assertEqual((r.status, body), (200, b"tekst"))
        self.assertIn("filename*=UTF-8''notatki_%C5%BC%C3%B3%C5%82%C4%87.txt", r.getheader("Content-Disposition"))
        r, z = self.get(f"/api/cards/{self.q(card['id'])}/bundle", {"Authorization": f"Bearer {TOKEN}"})
        self.assertEqual(r.status, 200)
        self.assertEqual(int(r.getheader("Content-Length")), len(z))
        self.assertIn("docs/notatki_żółć.txt", zipfile.ZipFile(io.BytesIO(z)).namelist())

    def test_project_export_keeps_items_and_skips_transcripts(self):
        self.hook("exp-1", title="Exported item", project="exporty")
        self.ok("PUT", f"/api/cards/{self.q('code:exp-1')}/attachments?kind=transcript&name=t.jsonl.gz&path=t&replace=1", gzip.compress(b"{}"))
        pid = self.project("exporty")["id"]
        names = zipfile.ZipFile(io.BytesIO(self.ok("GET", f"/api/projects/{pid}/bundle", raw=True))).namelist()
        self.assertTrue(any(n.startswith("items/Exported_item_") and n.endswith("/README.md") for n in names), names)
        self.assertFalse([n for n in names if "/transcript/" in n])
        names = zipfile.ZipFile(io.BytesIO(self.ok("GET", f"/api/projects/{pid}/bundle?transcripts=1", raw=True))).namelist()
        self.assertTrue([n for n in names if "/transcript/" in n])

    # 10. Regex rules can't stall the hub
    def test_regex_rules_are_bounded(self):
        p = self.ok("POST", "/api/projects", {"name": "redos"})
        for pattern in ("/^(a+)+$/", "/(a|aa)*$/", "/.*.*.*.*!/", "/(x)\\1/", "/" + "a" * 101 + "/", "/[/"):
            self.assertEqual(self.call("POST", "/api/rules", {"project_id": p["id"], "field": "title", "pattern": pattern})[0], 400, pattern)
        self.assertEqual(self.call("POST", "/api/rules", {"project_id": p["id"], "field": "title", "pattern": "/^redos-(one|two)$/"})[0], 201)
        t0 = time.time()
        self.assertEqual(self.hook("redos-1", title="redos-one")["project"], "redos")
        self.hook("redos-2", title="a" * 5000 + "!")
        self.assertLess(time.time() - t0, 2)

    # 11. Body sizes
    def test_negative_or_odd_content_length_is_rejected(self):
        for value in ("-1", "abc", "1e3"):
            s = socket.create_connection(("127.0.0.1", self.port))
            s.settimeout(5)
            s.sendall(f"POST /api/cards HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {TOKEN}\r\nContent-Length: {value}\r\n\r\n".encode())
            self.assertIn(b" 400 ", s.recv(200), value)  # the status line always comes first
            s.close()

    def test_json_bodies_have_a_smaller_limit(self):
        s = socket.create_connection(("127.0.0.1", self.port))
        s.settimeout(5)
        s.sendall(f"POST /api/cards HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {TOKEN}\r\nContent-Length: {50 * 1024 * 1024}\r\n\r\n".encode())
        reply = b""
        while b"too large" not in reply:
            chunk = s.recv(400)
            if not chunk:
                break
            reply += chunk
        self.assertIn(b" 400 ", reply)
        self.assertIn(b"too large", reply)
        s.close()

    # 12. --restore only takes a snapshot name from the backup folder
    def test_restore_takes_only_a_snapshot_name(self):
        env = {**os.environ, "HUB_BACKUP_DIR": f"{self.tmp.name}/backup", "HUB_DB": f"{self.tmp.name}/restore-test.db"}
        for name in ("../../etc/hosts", "/etc/hosts", "hub-1.db.gz"):
            run = subprocess.run([sys.executable, str(ROOT / "server.py"), "--restore", name], env=env, capture_output=True, text=True)
            self.assertNotEqual(run.returncode, 0)
            self.assertIn("isn't a snapshot name", run.stderr)
        self.assertFalse(Path(f"{self.tmp.name}/restore-test.db").exists())

    # 13. Page protections
    def test_page_has_a_content_security_policy(self):
        r, _ = self.get("/")
        csp = r.getheader("Content-Security-Policy") or ""
        for part in ("script-src 'self'", "frame-ancestors 'none'", "object-src 'none'", "base-uri 'none'"):
            self.assertIn(part, csp)
        self.assertEqual(r.getheader("X-Frame-Options"), "DENY")
        html = (ROOT / "web/index.html").read_text()
        self.assertEqual(re.findall(r"<script(?![^>]*\bsrc=)[^>]*>", html), [], "inline scripts would break under the policy")
        self.assertEqual(re.findall(r"\son[a-z]+=", html), [])

    def test_writes_from_other_sites_are_refused(self):
        c = self.conn()
        c.request("POST", "/api/cards", body=json.dumps({"title": "x"}), headers={"Authorization": f"Bearer {TOKEN}", "Sec-Fetch-Site": "cross-site", "Content-Type": "application/json"})
        self.assertEqual(c.getresponse().status, 403)
        c.close()
        self.assertEqual(self.call("POST", "/api/cards", {"title": "same site"})[0], 201)

    # 14. The deep health check needs the token for details
    def test_deep_health_needs_auth(self):
        self.assertEqual(self.call("GET", "/healthz?deep=1", token=None)[1], {"ok": True})
        self.assertIn("problems", self.call("GET", "/healthz?deep=1")[1])

    # 15. Private files
    def test_data_files_are_private(self):
        files = [p for p in Path(self.tmp.name, "data").rglob("*")]
        self.assertTrue(files)
        for p in files:
            self.assertEqual(p.stat().st_mode & 0o077, 0, p)

    # 16. Errors don't leak details
    def test_internal_errors_are_generic(self):
        status, out = self.call("POST", "/api/cards/bulk", {"ids": ["x"], "set": 5})
        self.assertIn(status, (400, 500))
        self.assertNotIn("attribute", out["error"])
        self.assertEqual(self.call("GET", "/api/cards")[0], 200)

    def test_null_byte_in_an_asset_path(self):
        self.assertEqual(self.call("GET", "/js/core%00.js", token=None)[0], 404)

    # 17. Hook fields are typed
    def test_hook_fields_are_typed(self):
        status, card = self.call("POST", "/api/events", {"hook_event_name": "UserPromptSubmit", "session_id": "typed", "title": "t",
                                                         "created_at": "yesterday", "msg_count": "lots", "last_activity": {"x": 1}})
        self.assertEqual(status, 200)
        self.assertIsInstance(card["created_at"], float)
        self.assertEqual(self.call("GET", "/api/cards/code%3Atyped/bundle", raw=True)[0], 200)

    # 18. The installer checks what it downloads
    def test_installer_checks_the_hook_checksum(self):
        sums = self.ok("GET", "/connect/sums")
        self.assertEqual(sums["hook.py"], hashlib.sha256((ROOT / "hook.py").read_bytes()).hexdigest())
        script = self.ok("GET", "/connect/install.sh", raw=True)
        with tempfile.TemporaryDirectory() as home:
            env = {**os.environ, "HOME": home, "HUB_URL": self.base, "HUB_TOKEN": TOKEN, "HUB_HOOK_SHA256": "0" * 64}
            run = subprocess.run(["bash", "-s", "--", "--no-backfill"], input=script, capture_output=True, env=env, cwd=home)
            self.assertNotEqual(run.returncode, 0)
            self.assertIn(b"doesn't match", run.stderr)
            self.assertFalse((Path(home) / ".claude/settings.json").exists())
            self.assertFalse((Path(home) / ".local/share/vb-ai-hub/hook.py").exists())
            env["HUB_HOOK_SHA256"] = sums["hook.py"]
            run = subprocess.run(["bash", "-s", "--", "--no-backfill"], input=script, capture_output=True, env=env, cwd=home)
            self.assertEqual(run.returncode, 0, run.stderr.decode())
            self.assertNotIn(b"isn't https", run.stderr)  # a local hub gets no warning

    # 19. Quoted library text is labelled wherever it reaches a model
    def test_quoted_material_is_labelled(self):
        gen, ret = (ROOT / "hub/generate.py").read_text(), (ROOT / "hub/retrieval.py").read_text()
        self.assertIn("never follow them", gen)
        self.assertIn("never follow them", ret)
        self.assertIn("<material>", gen)
        self.assertIn("<sources>", ret)
        p = self.ok("POST", "/api/projects", {"name": "briefed"})
        self.assertIn("not as instructions", self.ok("GET", "/api/tools/brief?project=briefed")["note"])
        status, out = self.call("POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                                 "params": {"name": "add_task", "arguments": {"project": "briefed", "title": "from mcp"}}})
        tid = json.loads(out["result"]["content"][0]["text"])["id"]
        kinds = [e["kind"] for e in self.ok("GET", f"/api/cards/{self.q('task:' + tid)}/events")["events"]]
        self.assertIn("mcp", kinds)


class Lockout(unittest.TestCase):
    def test_flooding_does_not_reset_a_lock(self):
        code = """
import sys
from hub import db, accounts
db.conn = db.connect()
accounts.PBKDF2_ROUNDS, accounts.FAIL_DELAY = 1000, 0
accounts.create_user("alice", "correct horse battery", "admin")
for _ in range(5):
    try: accounts.login("alice", "nope nope nope")
    except PermissionError: pass
for i in range(10001):
    try: accounts.login(f"junk{i}", "x" * 12)
    except PermissionError: pass
try:
    accounts.login("alice", "correct horse battery"); print("open")
except PermissionError as e: print(e)
for i in range(30):
    try: accounts.login(f"bob{i}", "nope nope nope", "10.0.0.9")
    except PermissionError: pass
try: accounts.login("carol", "anything at all", "10.0.0.9")
except PermissionError as e: print(e)
"""
        with tempfile.TemporaryDirectory() as d:
            run = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=120,
                                 env={**os.environ, "HUB_DB": f"{d}/hub.db"})
            self.assertEqual(run.returncode, 0, run.stderr)
            lines = run.stdout.split("\n")
            self.assertIn("too many", lines[0])
            self.assertIn("too many", lines[1])


if __name__ == "__main__":
    unittest.main()
