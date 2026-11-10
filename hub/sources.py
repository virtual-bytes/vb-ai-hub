"""Incoming events: Claude Code and Cursor hooks, the ingest API and claude.ai exports."""

import gzip
import json
import re

from . import db
from .config import COLUMNS
from .util import UUID, guess_mime, now, parse_ts
from .db import db_lock, setting
from .records import get_card, push_card
from .archive import add_attachment, add_worklog
from .cards import source_deleted, upsert
from .usage import add_usage


def handle_hook(ev: dict) -> dict:
    return handle_session_event(ev, "code")


def handle_cursor(ev: dict) -> dict:
    return handle_session_event(ev, "cursor")


def handle_session_event(ev: dict, source: str) -> dict:
    sid = str(ev.get("session_id") or "")[:100]
    if not sid:
        raise ValueError("session_id required")
    name = ev.get("hook_event_name") or ev.get("event") or "update"
    card_id = f"{source}:{sid}"
    existing = get_card(card_id)
    auto = setting("auto_flow") == "true"
    fields = {
        "title": ev.get("title") or None, "project": ev.get("project") or None, "cwd": ev.get("cwd") or None,
        "session_id": sid, "model": ev.get("model") or None, "msg_count": ev.get("msg_count") or None,
        "last_activity": ev.get("last_activity") or None, "host": ev.get("host") or None,
    }
    if ev.get("created_at"):
        fields["created_at"] = ev["created_at"]
    if ev.get("prompt"):
        fields["last_prompt"] = str(ev["prompt"])[:400]
    col = existing["column"] if existing else None
    if name in ("SessionStart", "UserPromptSubmit"):
        fields["status"] = "running"
        if auto and col in ("needs_me", "done", "ideas"):
            fields["column"] = "active"
    elif name == "Stop":
        fields["status"] = "waiting"
        if auto and col == "active":
            fields["column"] = "needs_me"
    elif name == "SessionEnd":
        fields["status"] = "ended"
    elif name == "backfill" and not existing:
        fields["status"] = ""
    initial = ev.get("column") if ev.get("column") in COLUMNS else None
    initial = initial or ("needs_me" if name == "Stop" else "active")
    if not fields.get("title") and not existing:
        fields["title"] = str(ev.get("prompt") or "")[:80] or f"Session {sid[:8]}"
    card = upsert(card_id, source, fields, initial, str(name)[:40], str(ev.get("prompt") or ev.get("source") or ev.get("reason") or ""))
    if ev.get("usage"):
        add_usage(card_id, {"models": ev["usage"], "days": ev.get("days")})
    return card


def handle_ingest(ev: dict) -> dict:
    """Generic ingest: {source, id, title, project, url, status, column, event, detail, summary, notes, worklog, archived}."""
    source = re.sub(r"[^a-z0-9_-]", "", str(ev.get("source", "")).lower())[:32]
    ext = str(ev.get("id") or "").strip()[:200]
    if not source or not ext:
        raise ValueError("source and id required")
    if source in ("project", "task"):
        raise ValueError(f"{source} is a reserved source name; use another")
    card_id = f"{source}:{ext}"
    existing = get_card(card_id)
    fields = {k: ev.get(k) or None for k in ("title", "project", "url", "status", "model", "notes", "last_prompt", "summary",
                                             "cwd", "host", "session_id", "msg_count")}
    fields["last_activity"] = ev.get("at") or None
    if ev.get("created_at"):
        fields["created_at"] = ev["created_at"]
    if ev.get("column") in COLUMNS and existing:
        fields["column"] = ev["column"]
    if not existing and not fields["title"]:
        fields["title"] = f"{source} {ext[:12]}"
    upsert(card_id, source, fields, ev.get("column") or "active", str(ev.get("event") or "update")[:40], str(ev.get("detail") or "")[:2000])
    if ev.get("worklog"):
        add_worklog(card_id, [{"text": ev["worklog"], "at": ev.get("at"), "ref": ev.get("ref")}])
    if ev.get("usage"):
        add_usage(card_id, {"models": ev["usage"], "days": ev.get("days")})
    if ev.get("archived"):
        with db_lock:
            db.conn.execute("UPDATE cards SET archived_at=COALESCE(archived_at, ?) WHERE id=?", (now(), card_id))
            db.conn.commit()
    return push_card(card_id)


def import_export(payload, prune=False) -> dict:
    """claude.ai data export: conversations.json or projects.json. prune=True removes chats/projects missing from it."""
    items = payload if isinstance(payload, list) else payload.get("items", [])
    counts = {"chats": 0, "projects": 0, "removed": 0}
    cutoff = now() - 14 * 86400
    for it in items:
        if not isinstance(it, dict) or not it.get("uuid"):
            continue
        if "chat_messages" not in it and not UUID.fullmatch(str(it["uuid"])):
            continue  # a claude.ai project's card is project:<uuid>; any other id could clash with a library project's key
        updated = parse_ts(it.get("updated_at")) or parse_ts(it.get("created_at")) or now()
        created = parse_ts(it.get("created_at")) or updated
        col = "done" if updated < cutoff else "active"
        if "chat_messages" in it:
            msgs = it.get("chat_messages") or []
            proj = it.get("project") or {}
            cid = f"chat:{it['uuid']}"
            upsert(cid, "chat", {
                "title": it.get("name") or "Untitled chat",
                "project": proj.get("name") if isinstance(proj, dict) else None,
                "url": f"https://claude.ai/chat/{it['uuid']}", "msg_count": len(msgs) or None,
                "created_at": created, "last_activity": updated,
            }, col, "imported")
            if msgs:
                add_attachment(cid, "transcript", f"{it['uuid']}.json.gz", "claude.ai-export",
                               gzip.compress(json.dumps(it).encode()), "application/gzip", replace=True)
                add_worklog(cid, [{"ref": m.get("uuid"), "at": parse_ts(m.get("created_at")), "text": m.get("text", "")}
                                  for m in msgs if m.get("sender") == "assistant" and m.get("text")][-20:])
            counts["chats"] += 1
        else:
            cid = f"project:{it['uuid']}"
            upsert(cid, "project", {
                "title": it.get("name") or "Untitled project", "project": it.get("name") or None,
                "notes": (it.get("description") or "")[:2000] or None,
                "url": f"https://claude.ai/project/{it['uuid']}", "created_at": created, "last_activity": updated,
            }, col, "imported")
            for d in it.get("docs") or []:
                if d.get("filename") and d.get("content"):
                    add_attachment(cid, "doc", d["filename"], d["filename"], d["content"].encode(), guess_mime(d["filename"]))
            if it.get("prompt_template"):
                add_attachment(cid, "doc", "project-instructions.md", "project-instructions.md", it["prompt_template"].encode(), "text/markdown")
            counts["projects"] += 1
    if prune and items:
        kind = "chat" if any("chat_messages" in it for it in items if isinstance(it, dict)) else "project"
        present = {f"{kind}:{it['uuid']}" for it in items if isinstance(it, dict) and it.get("uuid")}
        hub = [r["id"] for r in db.conn.execute("SELECT id FROM cards WHERE source=? AND deleted_at IS NULL", (kind,))]
        for cid in hub:
            if cid not in present:
                counts["removed"] += source_deleted(cid, "missing from the claude.ai export")
    return counts
