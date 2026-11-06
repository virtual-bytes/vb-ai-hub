"""Backups, restore, and rebuilding derived data."""

import gzip
import json
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import db
from .config import BACKUP_DIR, BLOB_DIR, DB_PATH, FTS_DOC_LIMIT
from .util import is_text, now
from .db import db_lock, int_setting
from .redaction import redact, redact_bytes
from .live import status_json
from .records import index_card
from .projects import index_project, index_task
from .archive import blob_path, gc_blobs, put_blob


def backup_now() -> dict:
    if not BACKUP_DIR:
        raise RuntimeError("HUB_BACKUP_DIR is not set")
    (BACKUP_DIR / "db").mkdir(parents=True, exist_ok=True)
    (BACKUP_DIR / "blobs").mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    tmp = BACKUP_DIR / "db" / f".hub-{stamp}.db"
    dst = sqlite3.connect(tmp)
    with db_lock:
        db.conn.backup(dst)
    dst.close()
    final = BACKUP_DIR / "db" / f"hub-{stamp}.db.gz"
    with open(tmp, "rb") as fi, gzip.open(final.with_suffix(".tmp"), "wb", compresslevel=6) as fo:
        shutil.copyfileobj(fi, fo)
    final.with_suffix(".tmp").replace(final)
    tmp.unlink()
    copied = total = 0
    for p in BLOB_DIR.glob("*/*"):
        if p.suffix == ".tmp":
            continue
        total += 1
        q = BACKUP_DIR / "blobs" / p.parent.name / p.name
        if not q.exists():
            q.parent.mkdir(exist_ok=True)
            shutil.copy2(p, q.with_suffix(".part"))
            q.with_suffix(".part").replace(q)
            copied += 1
    keep = now() - max(1, int_setting("backup_keep_days", 30)) * 86400
    snaps = sorted((BACKUP_DIR / "db").glob("hub-*.db.gz"))
    for s in snaps[:-3]:  # always keep the newest three
        if s.stat().st_mtime < keep:
            s.unlink()
    info = {"snapshot": final.name, "db_size": final.stat().st_size, "blobs_copied": copied, "blobs_total": total,
            "snapshots": len(list((BACKUP_DIR / "db").glob("hub-*.db.gz")))}
    (BACKUP_DIR / "LATEST.json").write_text(json.dumps({"at": now(), **info}, indent=2))
    return info


def restore(snapshot=None):
    if not BACKUP_DIR:
        sys.exit("HUB_BACKUP_DIR is not set")
    snaps = sorted((BACKUP_DIR / "db").glob("hub-*.db.gz"))
    src = (BACKUP_DIR / "db" / snapshot) if snapshot else (snaps[-1] if snaps else None)
    if not src or not src.exists():
        sys.exit(f"no snapshot found in {BACKUP_DIR / 'db'}")
    for suffix in ("", "-wal", "-shm"):
        p = Path(DB_PATH + suffix)
        if p.exists():
            p.rename(p.with_name(p.name + f".before-restore-{int(now())}"))
    with gzip.open(src, "rb") as fi, open(DB_PATH, "wb") as fo:
        shutil.copyfileobj(fi, fo)
    n = 0
    for p in (BACKUP_DIR / "blobs").glob("*/*"):
        q = BLOB_DIR / p.parent.name / p.name
        if not q.exists():
            q.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, q)
            n += 1
    print(f"Restored {src.name}; copied {n} files into {BLOB_DIR}. The previous database was kept as *.before-restore-*.")


def rebuild_search():
    with db_lock:
        db.conn.execute("DELETE FROM search")
        for r in db.conn.execute("SELECT id FROM cards").fetchall():
            index_card(r["id"])
        for r in db.conn.execute("SELECT id, card_id, text FROM worklog").fetchall():
            db.conn.execute("INSERT INTO search(card_id, kind, ref, title, body) VALUES (?, 'log', ?, '', ?)", (r["card_id"], str(r["id"]), r["text"]))
        for r in db.conn.execute("""SELECT card_id, path, name, sha, mime, size FROM attachments a WHERE kind!='transcript'
                                 AND created_at=(SELECT MAX(created_at) FROM attachments b WHERE b.card_id=a.card_id AND b.path=a.path)""").fetchall():
            if is_text(r["mime"], r["name"]) and r["size"] <= FTS_DOC_LIMIT:
                try:
                    body = blob_path(r["sha"]).read_text("utf-8", "replace")
                except OSError:
                    continue
                db.conn.execute("INSERT INTO search(card_id, kind, ref, title, body) VALUES (?, 'doc', ?, ?, ?)", (r["card_id"], r["path"], r["name"], body))
        for r in db.conn.execute("SELECT id FROM projects").fetchall():
            index_project(r["id"])
        for r in db.conn.execute("SELECT id FROM tasks").fetchall():
            index_task(r["id"])
        db.conn.commit()


def redact_existing() -> dict:
    """One-time scrub of everything already archived, then a fresh backup set without the old copies."""
    counts = {"worklog": 0, "cards": 0, "files": 0}
    with db_lock:
        for r in db.conn.execute("SELECT id, text FROM worklog").fetchall():
            t = redact(r["text"])
            if t != r["text"]:
                db.conn.execute("UPDATE worklog SET text=? WHERE id=?", (t, r["id"]))
                counts["worklog"] += 1
        for r in db.conn.execute("SELECT id, title, last_prompt, summary FROM cards").fetchall():
            new = {k: redact(r[k]) for k in ("title", "last_prompt", "summary")}
            if any(new[k] != r[k] for k in new):
                db.conn.execute("UPDATE cards SET title=?, last_prompt=?, summary=? WHERE id=?", (new["title"], new["last_prompt"], new["summary"], r["id"]))
                counts["cards"] += 1
        for r in db.conn.execute("SELECT id, detail FROM events WHERE detail != ''").fetchall():
            d = redact(r["detail"])
            if d != r["detail"]:
                db.conn.execute("UPDATE events SET detail=? WHERE id=?", (d, r["id"]))
                counts["events"] = counts.get("events", 0) + 1
        for r in db.conn.execute("SELECT id, notes FROM cards WHERE notes != ''").fetchall():
            n = redact(r["notes"])
            if n != r["notes"]:
                db.conn.execute("UPDATE cards SET notes=? WHERE id=?", (n, r["id"]))
                counts["cards"] += 1
        db.conn.execute("DELETE FROM chunks")  # rebuilt from the clean text by the indexer
        db.conn.commit()
    old = []
    for r in db.conn.execute("SELECT id, sha, name, mime FROM attachments").fetchall():
        try:
            data = blob_path(r["sha"]).read_bytes()
        except OSError:
            continue
        clean = redact_bytes(data, r["mime"], r["name"])
        if clean != data:
            sha = put_blob(clean)
            with db_lock:
                dup = db.conn.execute("SELECT id FROM attachments WHERE card_id=(SELECT card_id FROM attachments WHERE id=?) AND path=(SELECT path FROM attachments WHERE id=?) AND sha=?", (r["id"], r["id"], sha)).fetchone()
                if dup:
                    db.conn.execute("DELETE FROM attachments WHERE id=?", (r["id"],))
                else:
                    db.conn.execute("UPDATE attachments SET sha=?, size=? WHERE id=?", (sha, len(clean), r["id"]))
                db.conn.commit()
            old.append(r["sha"])
            counts["files"] += 1
    gc_blobs(old)
    rebuild_search()
    with db_lock:  # rewrite the file so replaced text doesn't linger in free pages or the WAL
        db.conn.commit()
        db.conn.execute("VACUUM")
        db.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    if BACKUP_DIR and (BACKUP_DIR / "db").exists():
        shutil.rmtree(BACKUP_DIR / "db", ignore_errors=True)
        shutil.rmtree(BACKUP_DIR / "blobs", ignore_errors=True)
        info = backup_now()
        status_json("backup_status", ok=True, **info)
        counts["backup"] = info["snapshot"]
    return counts
