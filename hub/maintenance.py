"""The maintenance loop and health checks."""

import json
import threading
import time
from datetime import datetime

from . import db
from .config import BACKUP_DIR
from .util import now
from .db import db_lock, int_setting, set_setting, setting
from .live import broadcast, status_json
from .records import log_event
from .cards import prune_projects, purge_trash
from .notifications import check_budget, check_waiting, notify, task_reminders
from .generate import weekly_digests
from .backup import backup_now


def auto_archive():
    days = int_setting("auto_archive_days", 30)
    if days <= 0:
        return 0
    with db_lock:
        ids = [r["id"] for r in db.conn.execute("SELECT id FROM cards WHERE col='done' AND archived_at IS NULL AND deleted_at IS NULL AND last_activity < ?", (now() - days * 86400,))]
        for cid in ids:
            db.conn.execute("UPDATE cards SET archived_at=? WHERE id=?", (now(), cid))
            log_event(cid, "archived", f"auto: done for {days}+ days")
        db.conn.commit()
    if ids:
        broadcast("reload", {"reason": "auto-archive", "count": len(ids)})
    return len(ids)


def maintenance_loop():
    while True:
        time.sleep(60)
        try:
            check_waiting()
            check_budget()
            local = datetime.now()
            day = local.strftime("%Y-%m-%d")
            if setting("last_archive_day") != day:
                set_setting("last_archive_day", day)
                auto_archive()
            if BACKUP_DIR and local.hour >= int_setting("backup_hour", 2) and setting("last_backup_day") != day:
                set_setting("last_backup_day", day)
                try:
                    status_json("backup_status", ok=True, **backup_now())
                except Exception as e:
                    status_json("backup_status", ok=False, error=str(e)[:300])
                    notify("VB AI Hub backup failed", str(e)[:300], "")
            purge_trash()
            prune_projects()
            if setting("notify_url") and local.hour >= int_setting("tasks_notify_hour", 8) and setting("last_tasks_day") != day:
                set_setting("last_tasks_day", day)
                task_reminders()
            week = local.strftime("%G-W%V")
            if (setting("digest_enabled") == "true" and setting("owui_key") and local.weekday() == int_setting("digest_day", 0)
                    and local.hour >= int_setting("digest_hour", 7) and setting("last_digest_week") != week):
                set_setting("last_digest_week", week)
                threading.Thread(target=lambda: status_json("digest_status", ok=True, projects=weekly_digests()), daemon=True).start()
        except Exception as e:
            print(f"maintenance: {e!r}", flush=True)


def health(deep=False):
    out = {"ok": True}
    if not deep:
        return out, 200
    problems = []
    if BACKUP_DIR:
        try:
            b = json.loads(setting("backup_status") or "null")
        except ValueError:
            b = None
        if not b or not b.get("ok") or now() - b.get("at", 0) > 36 * 3600:
            problems.append("backup older than 36h or failing")
    if setting("owui_key"):
        try:
            o = json.loads(setting("owui_status") or "null")
        except ValueError:
            o = None
        if o and not o.get("ok"):
            problems.append("open webui sync failing")
    out.update(ok=not problems, problems=problems)
    return out, 200 if not problems else 503
