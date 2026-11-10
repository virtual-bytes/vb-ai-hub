"""Creating, changing, trashing and restoring cards."""

import json

from . import db
from .config import COLUMNS, EDITABLE
from .util import as_int, now, parse_ts, plain_status, safe_url
from .db import db_lock, int_setting, setting
from .redaction import redact, redact_clip
from .live import broadcast
from .records import get_card, index_card, log_event, push_card, top_position
from .projects import ensure_project, library_key_taken, resolve_project
from .archive import gc_blobs


def upsert(card_id: str, source: str, fields: dict, initial_col: str, event: str = "", detail: str = ""):
    """Create or update a card from an automated source, respecting fields a person has edited.
    fields['project'] is the source's own project name; matching rules decide the effective project."""
    if "project" in fields:
        fields["source_project"] = fields.pop("project")
    clean_fields(fields)  # scrubbing can take a moment on long text, so it happens before the lock
    with db_lock:
        existing = get_card(card_id)
        t = now()
        if existing is None:
            col = initial_col if initial_col in COLUMNS else "active"
            data = {
                "id": card_id, "source": source, "col": col, "position": top_position(col),
                "created_at": fields.pop("created_at", None) or t, "updated_at": t,
                "last_activity": fields.pop("last_activity", None) or t,
            }
            fields.pop("column", None)
            data.update({k: v for k, v in fields.items() if v is not None})
            data["project"] = resolve_project(data.get("title", ""), data.get("cwd", ""), data.get("source_project", ""))
            ensure_project(data["project"])
            db.conn.execute(f"INSERT INTO cards({','.join(data)}) VALUES ({','.join('?' * len(data))})", list(data.values()))
            log_event(card_id, "created", event)
        else:
            locked = set(existing["user_fields"])
            if existing["source"] == "code" and existing.get("source_project"):
                fields.pop("source_project", None)  # a session keeps its first project when it changes folders
            updates = {k: v for k, v in fields.items() if v not in (None, "") and k not in locked}
            updates.pop("created_at", None)
            if existing.get("deleted_at") and event not in ("", "backfill", "imported"):
                updates["deleted_at"] = None
                log_event(card_id, "restored", "new activity")
            if "column" in updates:
                updates["col"] = updates.pop("column")
                if updates["col"] != existing["column"]:
                    updates["position"] = top_position(updates["col"])
            if existing.get("archived_at") and event not in ("", "backfill", "imported"):
                updates["archived_at"] = None
                log_event(card_id, "unarchived", "new activity")
            if "project" not in locked:
                p = resolve_project(updates.get("title", existing["title"]), updates.get("cwd", existing["cwd"]),
                                    updates.get("source_project", existing["source_project"]))
                if p != existing["project"]:
                    updates["project"], updates["task_id"] = p, None
                    ensure_project(p)
            updates.setdefault("last_activity", t)
            updates["updated_at"] = t
            db.conn.execute(f"UPDATE cards SET {','.join(f'{k}=?' for k in updates)} WHERE id=?", [*updates.values(), card_id])
        if event:
            log_event(card_id, event, detail)
        index_card(card_id)
        db.conn.commit()
    return push_card(card_id)


FIELD_LIMITS = {"title": 300, "last_prompt": 400, "summary": 2000, "notes": 20000, "cwd": 1000, "model": 100,
                "host": 100, "source_project": 300, "session_id": 100}


def clean_fields(fields):
    """Every value from a source is cut to its size, scrubbed and typed before it's stored."""
    for k, n in FIELD_LIMITS.items():
        if fields.get(k) is not None:
            fields[k] = redact_clip(fields[k], n)
    if fields.get("url") is not None:
        fields["url"] = redact_clip(safe_url(fields["url"]), 2000) or None
    if fields.get("status") is not None:
        fields["status"] = plain_status(fields["status"])
    for k in ("created_at", "last_activity"):
        if k in fields:
            fields[k] = parse_ts(fields[k])
    if "msg_count" in fields:
        n = as_int(fields["msg_count"])
        fields["msg_count"] = n if n is not None and n >= 0 else None
    return fields


def patch_card(cid, body):
    given = {}
    for k in EDITABLE & body.keys():  # scrubbed before the lock, as in upsert
        v = body[k]
        if k in ("title", "notes", "project"):
            v = redact_clip(v, 20000 if k == "notes" else 300)
        elif k == "url":
            v = redact_clip(safe_url(v), 2000)
        given[k] = v
    with db_lock:
        card = get_card(cid)
        if not card:
            raise LookupError("not found")
        updates, locked = {}, set(card["user_fields"])
        for k, v in given.items():
            if k == "column" and v not in COLUMNS:
                continue
            updates["col" if k == "column" else k] = v
            if k != "column":
                locked.add(k)
        if "project" in updates:
            if updates["project"]:
                ensure_project(updates["project"])
            else:  # clearing the project hands it back to the rules
                locked.discard("project")
                updates["project"] = resolve_project(card["title"], card["cwd"], card["source_project"])
            if updates["project"] != card["project"]:
                updates["task_id"] = None
        if "task_id" in body:
            tid = body["task_id"] or None
            if tid:
                t = db.conn.execute("SELECT t.id, p.name FROM tasks t JOIN projects p ON p.id=t.project_id WHERE t.id=?", (tid,)).fetchone()
                if not t:
                    raise ValueError("unknown task")
                if t["name"] != updates.get("project", card["project"]):
                    updates["project"] = t["name"]
                    locked.add("project")
            updates["task_id"] = tid
        if "position" in body:
            updates["position"] = float(body["position"])
        if "archived" in body:
            updates["archived_at"] = now() if body["archived"] else None
            log_event(cid, "archived" if body["archived"] else "unarchived", "to library" if body["archived"] else "back to board")
        if updates:
            updates["user_fields"] = json.dumps(sorted(locked))
            updates["updated_at"] = now()
            db.conn.execute(f"UPDATE cards SET {','.join(f'{k}=?' for k in updates)} WHERE id=?", [*updates.values(), cid])
            if "col" in updates and updates["col"] != card["column"]:
                log_event(cid, "moved", f"{card['column']} → {updates['col']}")
            index_card(cid)
            db.conn.commit()
    return push_card(cid)


def delete_card(card_id):
    """Remove a card and its library for good. Only ever a real card: a project's or task's documents are kept even
    if a card made by an older version shares their key."""
    with db_lock:
        if not db.conn.execute("SELECT 1 FROM cards WHERE id=?", (card_id,)).fetchone():
            return
        shared = library_key_taken(card_id)
        shas = [] if shared else [r["sha"] for r in db.conn.execute("SELECT sha FROM attachments WHERE card_id=?", (card_id,))]
        tables = ["cards WHERE id=?", "files_log WHERE card_id=?", "worklog WHERE card_id=?", "usage WHERE card_id=?", "links WHERE card_id=?"]
        tables += ["search WHERE card_id=? AND kind IN ('card', 'log')"] if shared else ["events WHERE card_id=?", "attachments WHERE card_id=?", "search WHERE card_id=?"]
        for t in tables:
            db.conn.execute(f"DELETE FROM {t}", (card_id,))
        db.conn.commit()
        gc_blobs(shas)
    broadcast("delete", {"id": card_id})


def trash_card(cid, reason):
    """Source deletions and the Delete button move cards to the trash; purge_trash() removes them later."""
    with db_lock:
        cur = db.conn.execute("UPDATE cards SET deleted_at=? WHERE id=? AND deleted_at IS NULL", (now(), cid))
        if cur.rowcount:
            log_event(cid, "trashed", reason)
        db.conn.commit()
    if cur.rowcount:
        broadcast("delete", {"id": cid})
        if int_setting("trash_days", 7) <= 0:
            delete_card(cid)
    return cur.rowcount


def source_deleted(cid, reason):
    """Apply the configured action when a chat/session disappears at its source."""
    if setting("source_delete_action", "trash") == "archive":
        with db_lock:
            cur = db.conn.execute("UPDATE cards SET archived_at=COALESCE(archived_at, ?), status='deleted at source' WHERE id=?", (now(), cid))
            log_event(cid, "source-deleted", reason)
            db.conn.commit()
        push_card(cid)
        return cur.rowcount
    return trash_card(cid, reason)


def restore_card(cid):
    with db_lock:
        db.conn.execute("UPDATE cards SET deleted_at=NULL WHERE id=?", (cid,))
        log_event(cid, "restored", "from trash")
        db.conn.commit()
    return push_card(cid)


def purge_trash():
    days = int_setting("trash_days", 7)
    ids = [r["id"] for r in db.conn.execute("SELECT id FROM cards WHERE deleted_at IS NOT NULL AND deleted_at < ?", (now() - max(0, days) * 86400,))]
    for cid in ids:
        delete_card(cid)
    return len(ids)


def prune_projects():
    """Drop projects that were created automatically and now hold nothing."""
    with db_lock:
        rows = db.conn.execute(
            """SELECT p.id FROM projects p WHERE p.manual=0 AND p.description='' AND p.goals='' AND p.links='[]'
               AND NOT EXISTS (SELECT 1 FROM cards c WHERE c.project=p.name)
               AND NOT EXISTS (SELECT 1 FROM tasks t WHERE t.project_id=p.id)
               AND NOT EXISTS (SELECT 1 FROM rules r WHERE r.project_id=p.id)
               AND NOT EXISTS (SELECT 1 FROM attachments a WHERE a.card_id='project:' || p.id)""").fetchall()
        for r in rows:
            db.conn.execute("DELETE FROM handoff_drafts WHERE project_id=?", (r["id"],))
            db.conn.execute("DELETE FROM projects WHERE id=?", (r["id"],))
            db.conn.execute("DELETE FROM search WHERE card_id=?", (f"project:{r['id']}",))
        db.conn.commit()
    if rows:
        broadcast("reload", {"reason": "prune"})
    return len(rows)


def reconcile_code(body):
    """A workstation reports the Claude Code sessions it still has; cards for sessions it deleted go to the trash.
    Sessions older than Claude Code's own cleanup window are kept: their transcripts expire, the library should not."""
    host = str(body.get("host") or "")[:100]
    sessions = set(body.get("sessions") or [])
    if not host or len(sessions) < 1:
        raise ValueError("host and a non-empty session list required")
    keep_after = now() - max(1, int(body.get("cleanup_days") or 30) - 1) * 86400
    # Sessions recorded before hosts were tracked belong to whichever machine still has them. A machine only
    # ever removes what it owns, so a second workstation can't trash another's sessions.
    with db_lock:
        for r in db.conn.execute("SELECT id, session_id FROM cards WHERE source='code' AND host=''").fetchall():
            if r["session_id"] in sessions:
                db.conn.execute("UPDATE cards SET host=? WHERE id=?", (host, r["id"]))
        db.conn.commit()
    rows = db.conn.execute("SELECT id, session_id, last_activity FROM cards WHERE source='code' AND deleted_at IS NULL"
                           " AND host=?", (host,)).fetchall()
    gone = [r for r in rows if r["session_id"] not in sessions and r["last_activity"] >= keep_after]
    if len(gone) > max(5, len(rows) // 2):
        raise ValueError(f"refusing to remove {len(gone)} of {len(rows)} sessions at once")
    for r in gone:
        source_deleted(r["id"], f"session deleted on {host}")
    return {"removed": len(gone), "checked": len(rows)}


def bulk(body):
    ids = [str(i) for i in body.get("ids") or []][:1000]
    if body.get("delete"):
        for cid in ids:
            trash_card(cid, "deleted by hand")
        return {"deleted": len(ids)}
    if body.get("purge"):
        for cid in ids:
            delete_card(cid)
        return {"purged": len(ids)}
    if body.get("restore"):
        for cid in ids:
            restore_card(cid)
        return {"restored": len(ids)}
    patch = {k: v for k, v in (body.get("set") or {}).items() if k in ("column", "archived", "project", "task_id")}
    n = 0
    for cid in ids:
        try:
            patch_card(cid, dict(patch))
            n += 1
        except LookupError:
            pass
    return {"updated": n}
