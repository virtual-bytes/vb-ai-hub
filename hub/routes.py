"""The JSON API routes."""

import hashlib
import json
import re
import secrets
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from . import db
from .config import COLUMNS, DEFAULT_SETTINGS, HERE, RULE_FIELDS
from .util import is_text, now, safe_name
from .db import db_lock, public_settings, set_setting, setting
from .live import broadcast, status_json
from .records import card_dict, card_with_counts, get_card, push_card
from .projects import (
    create_task,
    update_task,
    index_project,
    load_rules,
    merge_projects,
    project_by_id,
    project_by_name,
    reapply_rules,
    rename_or_merge_project,
)
from .archive import add_attachment, add_files, add_links, add_worklog, blob_path, bundle, card_archive, gc_blobs
from .cards import bulk, delete_card, patch_card, reconcile_code, restore_card, trash_card, upsert
from .usage import add_usage, parse_prices, project_usage, set_gpu_usage, usage_report, usage_stats
from .sources import handle_hook, handle_ingest, import_export
from .litellm import litellm_wake
from .owui import owui_wake
from .notifications import notify
from .generate import discard_draft, draft_state, gather, handoff_sources, jobs, run_job, save_draft, start_job
from .retrieval import ask, index_chunks
from .backup import backup_now, redact_existing
from .mcp import handle as mcp_handle
from .library import (
    file_chat,
    library_overview,
    library_project,
    library_projects,
    project_brief,
    project_bundle,
    search,
    project_next_steps,
    session_context,
)
from .accounts import check_password, create_user, delete_user, public_user, set_password


LINK_HOSTS = {"chatgpt.com": "chatgpt", "chat.openai.com": "chatgpt", "grok.com": "grok"}
MCP_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark"><title>VB AI Hub MCP server</title></head>
<body style="max-width:560px;margin:15vh auto;padding:0 20px;font:16px/1.5 ui-sans-serif,system-ui,sans-serif">
<h1 style="font-size:22px;font-weight:600">The MCP server is running</h1>
<p>This address is for Claude Code and other MCP clients, so there's nothing to see in a browser. Your client talks to it directly.</p>
<p>To connect a client, see <a href="/">Settings, Connect tools</a> in the hub.</p></body></html>"""


class ApiRoutes:
    """API routes, mixed into the HTTP handler."""

    def api(self, method, path):
        q = self.query
        seg = path.split("/")[1:]  # ['api', 'cards', '<id>', 'sub']
        if path == "/mcp":
            return self.mcp_endpoint(method)
        if path in ("/connect/hook.py", "/connect/install.sh") and method == "GET":
            return self.send_bytes((HERE / path.rsplit("/", 1)[-1]).read_bytes(), "text/plain; charset=utf-8")
        if path == "/api/stream" and method == "GET":
            return self.stream()
        if path == "/api/cards" and method == "GET":
            archived = q.get("archived") == "1"
            if q.get("trash") == "1":
                where = "deleted_at IS NOT NULL"
            else:
                where = f"deleted_at IS NULL AND archived_at IS {'NOT ' if archived else ''}NULL"
            rows = db.conn.execute(f"SELECT * FROM cards WHERE {where} ORDER BY col, position, last_activity DESC").fetchall()
            return self.send_json({"cards": [card_with_counts(card_dict(r)) for r in rows], "columns": COLUMNS, "settings": public_settings(),
                                   "projects": [dict(r) for r in db.conn.execute("SELECT id, name, status FROM projects ORDER BY name")],
                                   "tasks": [dict(r) for r in db.conn.execute("SELECT id, project_id, title, status, priority, due, notes, updated_at FROM tasks ORDER BY title")]})
        if path == "/api/cards" and method == "POST":
            return self.send_json(self.create_manual(self.read_json()), 201)
        if path == "/api/cards/bulk" and method == "POST":
            return self.send_json(bulk(self.read_json()))
        if path == "/api/events" and method == "POST":
            return self.send_json(handle_hook(self.read_json()))
        if path == "/api/ingest" and method == "POST":
            return self.send_json(handle_ingest(self.read_json()))
        if path == "/api/import" and method == "POST":
            return self.send_json(import_export(self.read_json(), prune=q.get("prune") == "1"))
        if path == "/api/reconcile/code" and method == "POST":
            return self.send_json(reconcile_code(self.read_json()))
        if path == "/api/settings" and method == "POST":
            return self.save_settings(self.read_json())
        if path == "/api/integrations/owui/sync" and method == "POST":
            owui_wake.set()
            return self.send_json({"ok": True})
        if path == "/api/integrations/litellm/sync" and method == "POST":
            litellm_wake.set()
            return self.send_json({"ok": True})
        if path == "/api/integrations/owui/apply-removals" and method == "POST":
            set_setting("owui_apply_removals", "true")  # one-shot approval, consumed by the next sync
            owui_wake.set()
            return self.send_json({"ok": True})
        if path == "/api/notify/test" and method == "POST":
            ok = notify("VB AI Hub test", "Notifications are working.", setting("public_url"))
            return self.send_json({"ok": ok})
        if path == "/api/backup" and method == "POST":
            try:
                info = backup_now()
                status_json("backup_status", ok=True, **info)
                return self.send_json(info)
            except Exception as e:
                status_json("backup_status", ok=False, error=str(e)[:300])
                raise ValueError(str(e))
        if path == "/api/generate" and method == "POST":
            b = self.read_json()
            if b.get("kind") not in ("handoff", "digest"):
                raise ValueError("kind must be handoff or digest")
            gather(b.get("scope"), b.get("id"))  # validates scope and target before queueing
            review, exclude = bool(b.get("review")), [str(x) for x in b.get("exclude") or []]
            if review and (b["kind"], b["scope"]) != ("handoff", "project"):
                raise ValueError("only project handoffs have a review step")
            if exclude and not [s for s in handoff_sources(b["id"]) if s["id"] not in exclude]:
                raise ValueError("pick at least one source")
            return self.send_json(start_job(b["kind"], b["scope"], b["id"], review, exclude), 202)
        if seg[:2] == ["api", "jobs"] and len(seg) == 3 and method == "GET":
            if seg[2] not in jobs:
                raise LookupError("job not found")
            return self.send_json({k: v for k, v in jobs[seg[2]].items() if k != "event"})
        if path == "/api/ask" and method == "POST":
            # Answer inline when it's quick; otherwise hand back a job so a proxy timeout can't cut it off.
            b = self.read_json()
            if not (b.get("question") or "").strip():
                raise ValueError("question required")
            job = run_job("ask", lambda: ask(b.get("question"), b.get("project_id") or None))
            job["event"].wait(40)
            if job["status"] == "done":
                return self.send_json(job["result"])
            if job["status"] == "error":
                raise ValueError(job["error"])
            return self.send_json({k: v for k, v in job.items() if k != "event"}, 202)
        if path == "/api/index" and method == "POST":
            limit = int(q.get("limit", "2000"))
            def go():
                res = index_chunks(limit=limit)
                status_json("index_status", ok=not res["error"], **res)
                return res
            job = run_job("index", go)
            return self.send_json({k: v for k, v in job.items() if k != "event"}, 202)
        if path == "/api/admin/redact" and method == "POST":
            return self.send_json(redact_existing())
        if path == "/api/tools/file-chat" and method == "POST":
            return self.send_json(file_chat(self.read_json()))
        if path == "/api/tools/brief" and method == "GET":
            return self.send_json(project_brief(q.get("project", "")))
        if seg[:2] == ["api", "context"] and len(seg) == 3 and method == "GET":
            return self.send_json({"text": session_context(seg[2])})
        if path in ("/api/usage/local", "/api/usage/gpu") and method == "POST":
            return self.send_json(set_gpu_usage(self.read_json()))
        if path == "/api/auth/password" and method == "POST":
            me, b = self.who(), self.read_json()
            if me["via"] != "session":
                raise ValueError("only accounts have passwords")
            row = db.conn.execute("SELECT pw FROM users WHERE id=?", (me["id"],)).fetchone()
            if not check_password(str(b.get("current") or ""), row["pw"]):
                raise PermissionError("current password is wrong")
            set_password(me["id"], b.get("password"), keep_token=self.session_token())
            return self.send_json({"ok": True})
        if path == "/api/users" and method == "GET":
            return self.send_json({"users": [public_user(r) for r in db.conn.execute("SELECT * FROM users ORDER BY username")]})
        if path == "/api/users" and method == "POST":
            b = self.read_json()
            return self.send_json(create_user(b.get("username"), b.get("password"), b.get("role") or "member"), 201)
        if seg[:2] == ["api", "users"] and len(seg) == 3 and method == "DELETE":
            delete_user(seg[2], self.who().get("id"))
            return self.send_json({"ok": True})
        if path == "/api/usage/report" and method == "GET":
            return self.send_json(usage_report(q.get("today", "")))
        if path == "/api/usage/stats" and method == "GET":
            return self.send_json(usage_stats(q.get("today", "")))
        if path == "/api/library/overview" and method == "GET":
            return self.send_json({"projects": library_overview(q.get("today", ""))})
        if path == "/api/library" and method == "GET":
            return self.send_json({"projects": library_projects()})
        if path == "/api/search" and method == "GET":
            return self.send_json({"results": search(q.get("q", ""))})
        if seg[:2] == ["api", "projects"]:
            return self.projects_api(method, seg[2:])
        if seg[:2] == ["api", "tasks"]:
            return self.tasks_api(method, seg[2:])
        if seg[:2] == ["api", "rules"]:
            return self.rules_api(method, seg[2:])
        if seg[:2] == ["api", "owners"] and len(seg) >= 4 and seg[3] == "archive" and method == "GET":
            return self.send_json(card_archive(seg[2]))
        if seg[:2] == ["api", "owners"] and len(seg) >= 4 and seg[3] == "attachments" and method == "PUT":
            return self.put_attachment(seg[2], q)
        if path.startswith("/api/blobs/") and method == "GET":
            return self.get_blob(path.rsplit("/", 1)[-1])
        if path.startswith("/api/attachments/") and method == "DELETE":
            return self.delete_attachment(int(path.rsplit("/", 1)[-1]))
        if path.startswith("/api/cards/"):
            cid, _, sub = path[len("/api/cards/"):].partition("/")
            return self.card_api(method, cid, sub)
        raise LookupError("not found")

    def save_settings(self, body):
        allowed = set(DEFAULT_SETTINGS) | {"owui_url", "owui_key", "notify_url", "public_url", "litellm_url", "litellm_key"}
        if "embed_model" in body:
            set_setting("embeddings_off", "")
        for k, v in body.items():
            if k not in allowed:
                continue
            v = str(v).strip()
            if k in ("owui_url", "notify_url", "public_url", "litellm_url"):
                v = v.rstrip("/") if k != "notify_url" else v
                if v and not re.match(r"^https?://", v):
                    raise ValueError(f"{k} must start with http:// or https://")
            if k == "model_prices":
                parse_prices(v)
            if k in ("budget_monthly", "budget_daily") and v:
                try:
                    if float(v) < 0:
                        raise ValueError
                except ValueError:
                    raise ValueError("a budget must be a dollar amount")
            if k == "usage_tz":
                try:
                    ZoneInfo(v or "UTC")
                except (KeyError, ValueError, OSError):
                    raise ValueError(f"unknown time zone: {v}")
            set_setting(k, v)
        if {"owui_url", "owui_key"} & body.keys():
            owui_wake.set()
        if {"litellm_url", "litellm_key", "litellm_local", "litellm_exclude", "usage_tz"} & body.keys():
            litellm_wake.set()
        broadcast("settings", {k: v for k, v in public_settings().items() if k in body})
        return self.send_json(public_settings())

    def put_attachment(self, owner, q):
        name = q.get("name") or "file"
        res = add_attachment(owner, q.get("kind") or "upload", name, q.get("path") or name, self.body(),
                             q.get("mime") or "", replace=q.get("replace") == "1", at=float(q["at"]) if q.get("at") else None)
        return self.send_json(res, 201 if res["added"] else 200)

    def get_blob(self, sha):
        if not re.fullmatch(r"[0-9a-f]{64}", sha):
            raise ValueError("bad id")
        row = db.conn.execute("SELECT name, mime FROM attachments WHERE sha=? LIMIT 1", (sha,)).fetchone()
        if not row:
            raise LookupError("not found")
        data = blob_path(sha).read_bytes()
        mime = row["mime"] if is_text(row["mime"], row["name"]) else "application/octet-stream"
        if mime.startswith("text/html"):
            mime = "text/plain"
        return self.send_bytes(data, mime + ("; charset=utf-8" if mime.startswith("text/") else ""), extra={
            "Content-Disposition": f"attachment; filename=\"{safe_name(row['name'])}\"", "Content-Security-Policy": "default-src 'none'"})

    def delete_attachment(self, aid):
        with db_lock:
            row = db.conn.execute("SELECT card_id, sha, path FROM attachments WHERE id=?", (aid,)).fetchone()
            if not row:
                raise LookupError("not found")
            db.conn.execute("DELETE FROM attachments WHERE id=?", (aid,))
            if not db.conn.execute("SELECT 1 FROM attachments WHERE card_id=? AND path=?", (row["card_id"], row["path"])).fetchone():
                db.conn.execute("DELETE FROM search WHERE card_id=? AND kind='doc' AND ref=?", (row["card_id"], row["path"]))
            db.conn.commit()
            gc_blobs([row["sha"]])
        if row["card_id"].startswith(("task:", "project:")):
            broadcast("library", {"owner": row["card_id"]})
        else:
            push_card(row["card_id"])
        return self.send_json({"ok": True})

    def projects_api(self, method, rest):
        if not rest and method == "GET":
            return self.send_json({"projects": library_projects()})
        if not rest and method == "POST":
            b = self.read_json()
            name = (b.get("name") or "").strip()
            if not name:
                raise ValueError("name required")
            if project_by_name(name):
                raise ValueError("a project with that name exists")
            pid = secrets.token_hex(5)
            with db_lock:
                db.conn.execute("INSERT INTO projects(id, name, description, manual, created_at, updated_at) VALUES (?,?,?,1,?,?)",
                                (pid, name, b.get("description") or "", now(), now()))
                index_project(pid)
                db.conn.commit()
            broadcast("reload", {"reason": "project"})
            return self.send_json(project_by_id(pid), 201)
        pid = rest[0]
        if len(rest) == 1 and method == "GET":
            return self.send_json(library_project(pid))
        if len(rest) == 2 and rest[1] == "next-steps" and method == "GET":
            return self.send_json(project_next_steps(pid))
        if len(rest) == 2 and rest[1] == "draft" and method == "GET":
            return self.send_json(draft_state(pid))
        if len(rest) == 2 and rest[1] == "draft" and method == "DELETE":
            discard_draft(pid)
            return self.send_json({"ok": True})
        if len(rest) == 3 and rest[1:] == ["draft", "save"] and method == "POST":
            return self.send_json(save_draft(pid))
        if len(rest) == 2 and rest[1] == "usage" and method == "GET":
            return self.send_json(project_usage(pid, self.query.get("today", "")))
        if len(rest) == 2 and rest[1] == "bundle" and method == "GET":
            p = project_by_id(pid) or {}
            return self.send_bytes(project_bundle(pid, self.query.get("transcripts") == "1"), "application/zip", extra={
                "Content-Disposition": f"attachment; filename=\"{safe_name(p.get('name', 'project'))}.zip\""})
        if len(rest) == 1 and method == "PATCH":
            b = self.read_json()
            p = project_by_id(pid)
            if not p:
                raise LookupError("project not found")
            if b.get("name") and b["name"].strip() != p["name"]:
                pid = rename_or_merge_project(pid, b["name"])
            upd = {}
            if "status" in b and b["status"] in ("active", "paused", "done", "archived"):
                upd["status"] = b["status"]
            for k in ("description", "goals"):
                if k in b:
                    upd[k] = str(b[k])[:20000]
            if "context" in b:
                upd["context"] = 1 if b["context"] else 0
            if "links" in b:
                upd["links"] = json.dumps([{"label": str(l.get("label", ""))[:80], "url": str(l.get("url", ""))[:500]}
                                           for l in b["links"] if l.get("url")][:50])
            if upd:
                upd["updated_at"], upd["manual"] = now(), 1
                with db_lock:
                    db.conn.execute(f"UPDATE projects SET {','.join(f'{k}=?' for k in upd)} WHERE id=?", [*upd.values(), pid])
                    index_project(pid)
                    db.conn.commit()
                broadcast("library", {"owner": f"project:{pid}"})
            return self.send_json(library_project(pid))
        if len(rest) == 2 and rest[1] == "merge" and method == "POST":
            into = project_by_id(self.read_json().get("into", ""))
            src = project_by_id(pid)
            if not into or not src or into["id"] == src["id"]:
                raise ValueError("pick a different existing project")
            merge_projects(src, into)
            return self.send_json(library_project(into["id"]))
        raise LookupError("not found")

    def mcp_endpoint(self, method):
        if method == "GET" and "text/html" in self.headers.get("Accept", ""):
            return self.send_bytes(MCP_PAGE.encode(), "text/html; charset=utf-8")
        if method != "POST":
            return self.send_bytes(b"", "text/plain", 405, {"Allow": "POST"})
        try:
            message = self.read_json()
        except json.JSONDecodeError:
            return self.send_json({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}, 400)
        reply = mcp_handle(message)
        return self.send_json(reply) if reply else self.send_bytes(b"", "text/plain", 202)

    def tasks_api(self, method, rest):
        if not rest and method == "POST":
            b = self.read_json()
            task = create_task(b)
            for cid in b.get("card_ids") or []:
                patch_card(cid, {"task_id": task["id"]})
            return self.send_json(task, 201)
        tid = rest[0] if rest else ""
        if not db.conn.execute("SELECT 1 FROM tasks WHERE id=?", (tid,)).fetchone():
            raise LookupError("task not found")
        if method == "PATCH":
            return self.send_json(update_task(tid, self.read_json()))
        if method == "DELETE":
            with db_lock:
                db.conn.execute("UPDATE cards SET task_id=NULL WHERE task_id=?", (tid,))
                db.conn.execute("DELETE FROM tasks WHERE id=?", (tid,))
                db.conn.execute("DELETE FROM search WHERE card_id=?", (f"task:{tid}",))
                db.conn.commit()
            broadcast("reload", {"reason": "task"})
            return self.send_json({"ok": True})
        if method == "GET":
            return self.send_json(card_archive(f"task:{tid}"))
        raise LookupError("not found")

    def rules_api(self, method, rest):
        if not rest and method == "POST":
            b = self.read_json()
            if b.get("field") not in RULE_FIELDS or not (b.get("pattern") or "").strip() or not project_by_id(b.get("project_id", "")):
                raise ValueError("project_id, field (title|path|source_project) and pattern required")
            with db_lock:
                db.conn.execute("INSERT INTO rules(project_id, field, pattern, created_at) VALUES (?,?,?,?)",
                                (b["project_id"], b["field"], b["pattern"].strip()[:300], now()))
                db.conn.commit()
            return self.send_json({"moved": reapply_rules()}, 201)
        if rest and method == "DELETE":
            with db_lock:
                db.conn.execute("DELETE FROM rules WHERE id=?", (int(rest[0]),))
                db.conn.commit()
            return self.send_json({"moved": reapply_rules()})
        if not rest and method == "GET":
            return self.send_json({"rules": load_rules()})
        raise LookupError("not found")

    def card_api(self, method, cid, sub):
        if not sub:
            if method == "PATCH":
                return self.send_json(patch_card(cid, self.read_json()))
            if method == "DELETE":
                if self.query.get("purge") == "1":
                    delete_card(cid)
                else:
                    trash_card(cid, "deleted by hand")
                return self.send_json({"ok": True})
            if method == "GET":
                card = card_with_counts(get_card(cid))
                if not card:
                    raise LookupError("not found")
                return self.send_json(card)
        if sub == "restore" and method == "POST":
            return self.send_json(restore_card(cid))
        if sub == "events" and method == "GET":
            rows = db.conn.execute("SELECT kind, at, detail FROM events WHERE card_id=? ORDER BY at DESC LIMIT 150", (cid,)).fetchall()
            return self.send_json({"events": [dict(r) for r in rows]})
        if sub == "archive" and method == "GET":
            return self.send_json(card_archive(cid))
        if sub == "attachments" and method == "PUT":
            return self.put_attachment(cid, self.query)
        if sub == "files" and method == "POST":
            add_files(cid, self.read_json())
            return self.send_json({"ok": True})
        if sub == "worklog" and method == "POST":
            add_worklog(cid, self.read_json())
            return self.send_json({"ok": True})
        if sub == "usage" and method == "POST":
            add_usage(cid, self.read_json())
            return self.send_json({"ok": True})
        if sub == "links" and method == "POST":
            add_links(cid, self.read_json())
            return self.send_json({"ok": True})
        if sub == "bundle" and method == "GET":
            card = get_card(cid) or {}
            return self.send_bytes(bundle(cid), "application/zip", extra={
                "Content-Disposition": f"attachment; filename=\"{safe_name(card.get('title', 'card'))}.zip\""})
        raise LookupError("not found")

    def create_manual(self, body):
        url = (body.get("url") or "").strip()
        owui = setting("owui_url")
        if "claude.ai/chat/" in url:
            source = "chat"
        elif "claude.ai/project/" in url:
            source = "project"
        elif owui and url.startswith(owui + "/c/"):
            source = "owui"
        else:
            source = LINK_HOSTS.get((urlparse(url).hostname or "").removeprefix("www."), "manual")
        if source in LINK_HOSTS.values():  # the same link again updates its card
            cid = f"{source}:{hashlib.sha256(url.rstrip('/').encode()).hexdigest()[:16]}"
        else:
            cid = f"{source}:{url.rstrip('/').rsplit('/', 1)[-1]}" if source != "manual" else f"manual:{secrets.token_hex(6)}"
        col = body.get("column") if body.get("column") in COLUMNS else "ideas"
        card = upsert(cid, source, {"title": (body.get("title") or "").strip() or "Untitled", "url": url or None,
                                    "notes": body.get("notes") or None}, col, "created")
        if (body.get("project") or "").strip():
            card = patch_card(cid, {"project": body["project"].strip()})
        return card
