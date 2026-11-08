"""The archive: content-addressed blobs, attachments, file and work logs, links, card export."""

import hashlib
import io
import os
import zipfile
from pathlib import Path

from . import db
from .config import BLOB_DIR, FTS_DOC_LIMIT
from .util import clean_mime, is_text, iso, now, safe_name, safe_url
from .db import db_lock
from .redaction import redact, redact_bytes
from .live import broadcast
from .records import get_card, index_card, log_event, push_card
from .projects import owner_exists


def blob_path(sha: str) -> Path:
    return BLOB_DIR / sha[:2] / sha


def put_blob(data: bytes) -> str:
    sha = hashlib.sha256(data).hexdigest()
    p = blob_path(sha)
    if not p.exists():
        p.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(BLOB_DIR, 0o700)
        tmp = p.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(p)
    return sha


def gc_blobs(shas):
    for sha in set(shas):
        if not db.conn.execute("SELECT 1 FROM attachments WHERE sha=? LIMIT 1", (sha,)).fetchone():
            try:
                blob_path(sha).unlink()
            except FileNotFoundError:
                pass


def add_attachment(owner, kind, name, path, data: bytes, mime="", replace=False, at=None):
    """Store a file version for a card, task or project. Identical content for the same path is a no-op."""
    name, path = redact(str(name or "file"))[:300], redact(str(path or name))[:1000]
    mime = clean_mime(mime, name)
    data = redact_bytes(data, mime, name)
    sha = put_blob(data)
    with db_lock:
        if not owner_exists(owner):
            raise LookupError("owner not found")
        old = []
        if replace:
            old = [r["sha"] for r in db.conn.execute("SELECT sha FROM attachments WHERE card_id=? AND path=? AND sha!=?", (owner, path, sha))]
            db.conn.execute("DELETE FROM attachments WHERE card_id=? AND path=? AND sha!=?", (owner, path, sha))
        cur = db.conn.execute(
            "INSERT OR IGNORE INTO attachments(card_id, kind, name, path, sha, size, mime, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (owner, kind, name, path, sha, len(data), mime, at or now()),
        )
        added = cur.rowcount > 0
        if added:
            log_event(owner, "archived", f"{name} ({kind})")
            if kind != "transcript" and is_text(mime, name) and len(data) <= FTS_DOC_LIMIT:
                db.conn.execute("DELETE FROM search WHERE card_id=? AND kind='doc' AND ref=?", (owner, path))
                db.conn.execute("INSERT INTO search(card_id, kind, ref, title, body) VALUES (?, 'doc', ?, ?, ?)",
                                (owner, path, name, data.decode("utf-8", "replace")))
        db.conn.commit()
        gc_blobs(old)
    if added:
        if owner.startswith(("task:", "project:")):
            broadcast("library", {"owner": owner})
        else:
            push_card(owner)
    return {"sha": sha, "added": added, "size": len(data)}


def add_files(card_id, items):
    with db_lock:
        for it in items:
            p, at = redact(str(it.get("path", "")))[:1000], float(it.get("at") or now())
            if not p:
                continue
            db.conn.execute(
                "INSERT INTO files_log(card_id, path, first_at, last_at, edits, last_action) VALUES (?,?,?,?,1,?)"
                " ON CONFLICT(card_id, path) DO UPDATE SET last_at=MAX(last_at, excluded.last_at), edits=edits+1, last_action=excluded.last_action",
                (card_id, p, at, at, str(it.get("action") or "edit")[:20]),
            )
        db.conn.commit()
    push_card(card_id)


def add_worklog(card_id, items):
    latest = None
    with db_lock:
        for it in items:
            text = redact((it.get("text") or "").strip())
            if not text:
                continue
            at = float(it.get("at") or now())
            cur = db.conn.execute("INSERT OR IGNORE INTO worklog(card_id, at, ref, text) VALUES (?,?,?,?)",
                                  (card_id, at, str(it.get("ref") or hashlib.sha1(text.encode()).hexdigest()), text[:20000]))
            if cur.rowcount:
                db.conn.execute("INSERT INTO search(card_id, kind, ref, title, body) VALUES (?, 'log', ?, '', ?)", (card_id, str(cur.lastrowid), text[:20000]))
                if latest is None or at >= latest[0]:
                    latest = (at, text)
        if latest:
            db.conn.execute("UPDATE cards SET summary=? WHERE id=?", (latest[1][:1200], card_id))
            index_card(card_id)
        db.conn.commit()
    push_card(card_id)


def add_links(card_id, items):
    with db_lock:
        for it in items:
            if not it.get("key") or it.get("kind") not in ("commit", "mr", "pr"):
                continue
            db.conn.execute(
                "INSERT INTO links(card_id, kind, key, title, url, repo, at) VALUES (?,?,?,?,?,?,?)"
                " ON CONFLICT(card_id, kind, key) DO UPDATE SET title=COALESCE(NULLIF(excluded.title,''), title),"
                " url=COALESCE(NULLIF(excluded.url,''), url), repo=COALESCE(NULLIF(excluded.repo,''), repo)",
                (card_id, it["kind"], str(it["key"])[:200], redact(str(it.get("title") or ""))[:300], redact(safe_url(it.get("url")))[:500],
                 str(it.get("repo") or "")[:200], it.get("at") if isinstance(it.get("at"), (int, float)) else None))
        db.conn.commit()
    push_card(card_id)


def card_archive(card_id):
    atts = [dict(r) for r in db.conn.execute(
        "SELECT id, kind, name, path, sha, size, mime, created_at FROM attachments WHERE card_id=? ORDER BY path, created_at DESC", (card_id,))]
    files = [dict(r) for r in db.conn.execute("SELECT path, first_at, last_at, edits, last_action FROM files_log WHERE card_id=? ORDER BY last_at DESC", (card_id,))]
    logs = [dict(r) for r in db.conn.execute("SELECT id, at, text FROM worklog WHERE card_id=? ORDER BY at DESC LIMIT 500", (card_id,))]
    links = [dict(r) for r in db.conn.execute("SELECT kind, key, title, url, repo, at FROM links WHERE card_id=? ORDER BY at DESC", (card_id,))]
    usage = [dict(r) for r in db.conn.execute("SELECT * FROM usage WHERE card_id=? ORDER BY output DESC", (card_id,))]
    return {"attachments": atts, "files": files, "worklog": logs, "links": links, "usage": usage}


def bundle(card_id, out=None):
    """A card's export as a zip. Written to `out` (a file) when given, so large exports don't sit in memory."""
    if not get_card(card_id):
        raise LookupError("card not found")
    buf = out if out is not None else io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        write_card(z, card_id)
    return buf if out is not None else buf.getvalue()


def write_card(z, card_id, prefix="", transcripts=True):
    """Add one card's files to an open zip, under `prefix`."""
    card = get_card(card_id)
    a = card_archive(card_id)
    meta = [f"# {card['title']}", "", f"- Source: {card['source']}", f"- Project: {card['project']}",
            f"- Created: {iso(card['created_at'])}", f"- Last activity: {iso(card['last_activity'])}"]
    for k in ("url", "cwd", "session_id", "model"):
        if card.get(k):
            meta.append(f"- {k}: {card[k]}")
    if card["notes"]:
        meta += ["", "## Notes", "", card["notes"]]
    if a["links"]:
        meta += ["", "## Commits and merge requests", ""] + [f"- {l['kind']}: [{l['key'][:12]}]({l['url']}) {l['title']}" for l in a["links"]]
    if a["usage"]:
        meta += ["", "## Usage", "", "| Model | Input | Output | Cache write | Cache read |", "|---|---|---|---|---|"] + [
            f"| {u['model']} | {u['input']} | {u['output']} | {u['cache_write']} | {u['cache_read']} |" for u in a["usage"]]
    z.writestr(prefix + "README.md", "\n".join(meta) + "\n")
    if a["worklog"]:
        z.writestr(prefix + "worklog.md", "\n\n".join(f"## {iso(w['at'])}\n\n{w['text']}" for w in reversed(a["worklog"])) + "\n")
    if a["files"]:
        z.writestr(prefix + "files.md", "| Path | Last action | Edits | First | Last |\n|---|---|---|---|---|\n" + "\n".join(
            f"| `{f['path']}` | {f['last_action']} | {f['edits']} | {iso(f['first_at'])} | {iso(f['last_at'])} |" for f in a["files"]) + "\n")
    seen = {}
    for att in a["attachments"]:
        n = seen.get(att["path"], 0)
        seen[att["path"]] = n + 1
        base = safe_name(att["name"])
        if att["kind"] == "transcript" and not transcripts:
            continue
        folder = prefix + ("transcript" if att["kind"] == "transcript" else "docs")
        name = f"{folder}/{base}" if n == 0 else f"{folder}/versions/{iso(att['created_at']).replace(' ', '_').replace(':', '')}_{base}"
        try:
            z.write(blob_path(att["sha"]), name)
        except FileNotFoundError:
            pass
