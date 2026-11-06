"""Outgoing notifications."""

import json
import ssl
import urllib.request
from datetime import datetime
from urllib.parse import quote
from zoneinfo import ZoneInfo

from . import db
from .util import clip, now
from .db import db_lock, int_setting, set_setting, setting
from .live import status_json
from .usage import budget_state


def notify(title, message, link):
    url, fmt = setting("notify_url"), setting("notify_format", "ha")
    if not url:
        return False
    if fmt == "ntfy":
        headers = {"Title": title.encode("ascii", "replace").decode(), "Tags": "robot"}
        if link:
            headers["Click"] = link
        req = urllib.request.Request(url, data=message.encode(), headers=headers, method="POST")
    else:
        req = urllib.request.Request(url, data=json.dumps({"title": title, "message": message, "url": link}).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=10, context=ssl.create_default_context()) as r:
        return 200 <= r.status < 300


def card_link(cid):
    base = setting("public_url").rstrip("/")
    return f"{base}/#card={quote(cid, safe='')}" if base else ""


def check_waiting():
    mins = int_setting("notify_after", 10)
    if not setting("notify_url") or mins <= 0:
        return
    rows = db.conn.execute("SELECT id, title, project, summary, last_activity FROM cards WHERE col='needs_me' AND status='waiting'"
                           " AND archived_at IS NULL AND deleted_at IS NULL AND last_activity < ? AND last_activity > ? AND (notified_at IS NULL OR notified_at < last_activity)",
                           (now() - mins * 60, now() - 86400)).fetchall()  # nothing stale: only waits that began in the last day
    for r in rows:
        try:
            notify(f"Claude is waiting: {r['title'][:80]}", (f"[{r['project']}] " if r["project"] else "") + clip(r["summary"] or "Waiting on your reply.", 240), card_link(r["id"]))
        except Exception as e:
            status_json("notify_status", ok=False, error=str(e)[:200])
            return
        with db_lock:
            db.conn.execute("UPDATE cards SET notified_at=? WHERE id=?", (now(), r["id"]))
            db.conn.commit()
        status_json("notify_status", ok=True, last=r["title"][:80])


def check_budget():
    """Warn once when spending passes 80% and 100% of the month's budget, and once a day when it passes the daily limit."""
    if not setting("notify_url"):
        return
    today = datetime.now(ZoneInfo(setting("usage_tz") or "UTC")).strftime("%Y-%m-%d")
    b = budget_state(today)
    month, level = today[:7], 100 if b["monthly"] and b["month"] >= b["monthly"] else 80 if b["monthly"] and b["month"] >= b["monthly"] * .8 else 0
    told = setting("budget_alerted")
    if level and f"{month}:{level}" != told and not (told == f"{month}:100"):
        notify(f"Token spending at {level}% of budget", f"${b['month']:,.2f} of ${b['monthly']:,.2f} this month, estimated at API prices. On course for ${b['projected']:,.2f}.", setting("public_url"))
        set_setting("budget_alerted", f"{month}:{level}")
    if b["daily"] and b["day"] >= b["daily"] and setting("budget_alerted_day") != today:
        notify("Daily token spending limit passed", f"${b['day']:,.2f} today against a limit of ${b['daily']:,.2f}, estimated at API prices.", setting("public_url"))
        set_setting("budget_alerted_day", today)


def task_reminders():
    today = datetime.now().strftime("%Y-%m-%d")
    rows = db.conn.execute("SELECT t.title, t.due, t.priority, p.name FROM tasks t JOIN projects p ON p.id=t.project_id"
                           " WHERE t.status!='done' AND t.due!='' AND t.due<=? ORDER BY t.due", (today,)).fetchall()
    if rows:
        over = [r for r in rows if r["due"] < today]
        msg = "; ".join(f"{r['title']} ({r['name']}{', overdue since ' + r['due'] if r['due'] < today else ''})" for r in rows[:8])
        notify(f"{len(rows)} task{'s' if len(rows) > 1 else ''} due{f', {len(over)} overdue' if over else ''}", msg, setting("public_url"))
