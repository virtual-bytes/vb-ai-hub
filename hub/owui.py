"""Open WebUI: API client and chat sync."""

import gzip
import json
import re
import threading
import urllib.error
import urllib.request
from urllib.parse import quote

from . import db
from .util import fetch, now, parse_ts
from .db import db_lock, int_setting, set_setting, setting
from .live import status_json
from .records import log_event, push_card
from .archive import add_attachment, add_worklog
from .cards import source_deleted, upsert
from .usage import add_usage


owui_wake = threading.Event()


def owui_request(path, body=None, timeout=20):
    base, key = setting("owui_url"), setting("owui_key")
    if not base or not key:
        raise RuntimeError("Open WebUI is not connected (Settings → Open WebUI)")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base.rstrip("/") + path, data=data, method="POST" if data else "GET",
                                 headers={"Authorization": f"Bearer {key}", "Accept": "application/json", "Content-Type": "application/json"})
    with fetch(req, timeout) as r:
        return json.loads(r.read())


def owui_messages(chat: dict) -> list:
    hist = chat.get("history") or {}
    msgs, cur = [], hist.get("currentId")
    by_id = hist.get("messages") or {}
    while cur and cur in by_id and len(msgs) < 5000:
        msgs.append(by_id[cur])
        cur = by_id[cur].get("parentId")
    return list(reversed(msgs)) or chat.get("messages") or []


def owui_usage(msgs) -> dict:
    out = {}
    for m in msgs:
        if m.get("role") != "assistant":
            continue
        u = m.get("usage") or (m.get("info") or {}).get("usage") or m.get("info") or {}
        i = u.get("prompt_tokens") or u.get("input_tokens") or u.get("prompt_eval_count") or 0
        o = u.get("completion_tokens") or u.get("output_tokens") or u.get("eval_count") or 0
        if not (i or o):
            continue
        model = m.get("model") or "unknown"
        acc = out.setdefault(model, {"input": 0, "output": 0, "messages": 0})
        acc["input"] += int(i)
        acc["output"] += int(o)
        acc["messages"] += 1
    return out


CHAT_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")


def owui_folders():
    folders = {}
    try:
        for f in owui_request("/api/v1/folders/") or []:
            folders[f["id"]] = f
    except Exception:
        pass
    return folders


def owui_folder_name(folders, fid):
    parts, seen = [], set()
    while fid and fid in folders and fid not in seen:
        seen.add(fid)
        parts.append(folders[fid].get("name") or "")
        fid = folders[fid].get("parent_id")
    return " / ".join(reversed(parts))


def owui_sync_chat(chat_id, folders=None, archived_at_source=False, force=False):
    """Fetch one chat and update its card. Returns True when the card changed."""
    if not CHAT_ID.fullmatch(str(chat_id)):
        raise ValueError("not an Open WebUI chat id")
    base = setting("owui_url")
    folders = folders if folders is not None else owui_folders()
    cid = f"owui:{chat_id}"
    full = owui_request(f"/api/v1/chats/{quote(chat_id, safe="")}")
    upd = parse_ts(full.get("updated_at")) or now()
    row = db.conn.execute("SELECT source_updated, col, archived_at FROM cards WHERE id=?", (cid,)).fetchone()
    if row and not force and row["source_updated"] and row["source_updated"] >= upd:
        return False
    chat = full.get("chat") or {}
    msgs = owui_messages(chat)
    users = [m for m in msgs if m.get("role") == "user"]
    bots = [m for m in msgs if m.get("role") == "assistant"]
    fields = {
        "title": full.get("title") or "Untitled chat",
        "project": owui_folder_name(folders, full.get("folder_id")) or "Open WebUI",
        "url": f"{base.rstrip('/')}/c/{chat_id}",
        "msg_count": len(users) or None,
        "model": (bots[-1].get("model") if bots else None) or ((chat.get("models") or [None])[0]),
        "last_prompt": (users[-1].get("content") or "")[:400] if users and isinstance(users[-1].get("content"), str) else None,
        "created_at": parse_ts(full.get("created_at")) or upd,
        "last_activity": upd,
    }
    if row and setting("auto_flow") == "true" and row["col"] == "done" and not archived_at_source:
        fields["column"] = "active"
    upsert(cid, "owui", fields, "active" if upd > now() - 3 * 86400 else "done", "sync" if row else "imported")
    with db_lock:
        db.conn.execute("UPDATE cards SET source_updated=? WHERE id=?", (upd, cid))
        if archived_at_source:
            db.conn.execute("UPDATE cards SET archived_at=COALESCE(archived_at, ?) WHERE id=?", (now(), cid))
        db.conn.commit()
    add_attachment(cid, "transcript", f"{chat_id}.json.gz", "open-webui-chat", gzip.compress(json.dumps(full).encode()), "application/gzip", replace=True)
    add_worklog(cid, [{"ref": m.get("id"), "at": parse_ts(m.get("timestamp")), "text": m.get("content")}
                      for m in bots[-20:] if isinstance(m.get("content"), str)])
    u = owui_usage(msgs)
    if u:
        add_usage(cid, {"replace": True, "models": u})
    return True


def owui_list(path):
    items, page = [], 1
    while page <= 200:
        batch = owui_request(f"{path}{'&' if '?' in path else '?'}page={page}")
        if not batch:
            break
        items.extend(batch)
        page += 1
    return items


def owui_sync_once() -> dict:
    if not setting("owui_url") or not setting("owui_key"):
        return {"skipped": "not configured"}
    folders = owui_folders()
    listed = owui_list("/api/v1/chats/")
    try:
        archived = owui_list("/api/v1/chats/archived")
    except Exception:
        archived = None  # unknown: skip deletion detection rather than guess
    changed = 0
    for item, is_arch in [(i, False) for i in listed] + [(i, True) for i in (archived or [])]:
        cid, upd = f"owui:{item['id']}", parse_ts(item.get("updated_at")) or now()
        row = db.conn.execute("SELECT source_updated, archived_at FROM cards WHERE id=?", (cid,)).fetchone()
        if row and row["source_updated"] and row["source_updated"] >= upd:
            if is_arch and not row["archived_at"]:
                with db_lock:
                    db.conn.execute("UPDATE cards SET archived_at=? WHERE id=?", (now(), cid))
                    log_event(cid, "archived", "archived in Open WebUI")
                    db.conn.commit()
                push_card(cid)
            continue
        try:
            changed += owui_sync_chat(item["id"], folders, archived_at_source=is_arch)
        except Exception:
            continue
    removed, held = 0, 0
    if archived is not None:
        present = {i["id"] for i in listed} | {i["id"] for i in archived}
        hub = [r["id"] for r in db.conn.execute("SELECT id FROM cards WHERE source='owui' AND deleted_at IS NULL")]
        missing = [cid for cid in hub if cid[5:] not in present]
        approved = setting("owui_apply_removals") == "true"
        if missing and len(missing) > max(3, len(hub) // 3) and not approved:
            held = len(missing)  # too many at once: wait for a person to confirm in Settings
        else:
            set_setting("owui_apply_removals", "")
            for cid in missing:
                try:
                    owui_request(f"/api/v1/chats/{quote(cid[5:], safe="")}")
                    continue  # still reachable: not deleted
                except urllib.error.HTTPError as e:
                    if e.code not in (401, 403, 404):
                        continue
                except Exception:
                    continue
                removed += source_deleted(cid, "deleted in Open WebUI")
    return {"chats": len(listed), "archived": len(archived or []), "changed": changed, "removed": removed, "held": held}


def owui_loop():
    while True:
        try:
            if setting("owui_url") and setting("owui_key"):
                status_json("owui_status", ok=True, **owui_sync_once())
        except Exception as e:
            status_json("owui_status", ok=False, error=str(e)[:300])
        owui_wake.wait(max(30, int_setting("owui_interval", 120)))
        owui_wake.clear()
