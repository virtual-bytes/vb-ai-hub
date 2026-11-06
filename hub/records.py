"""Reading cards and the basics every write needs: events, search index, live push."""

import json

from . import db
from .util import now
from .redaction import redact
from .live import broadcast


def card_dict(row) -> dict:
    d = dict(row)
    d["column"] = d.pop("col")
    d["user_fields"] = json.loads(d["user_fields"])
    for k in ("source_updated", "notified_at"):
        d.pop(k, None)
    return d


def get_card(card_id):
    row = db.conn.execute("SELECT * FROM cards WHERE id=?", (card_id,)).fetchone()
    return card_dict(row) if row else None


def usage_totals(where: str, args) -> dict:
    r = db.conn.execute(f"SELECT COALESCE(SUM(input),0) i, COALESCE(SUM(output),0) o, COALESCE(SUM(cache_write),0) w,"
                        f" COALESCE(SUM(cache_read),0) r, COALESCE(SUM(messages),0) m FROM usage WHERE {where}", args).fetchone()
    return {"input": r["i"], "output": r["o"], "cache_write": r["w"], "cache_read": r["r"], "messages": r["m"]}


def card_with_counts(card):
    if card is None:
        return None
    r = db.conn.execute(
        "SELECT (SELECT COUNT(DISTINCT path) FROM attachments WHERE card_id=:id AND kind!='transcript') AS docs,"
        " (SELECT COUNT(*) FROM files_log WHERE card_id=:id) AS files,"
        " (SELECT COUNT(*) FROM worklog WHERE card_id=:id) AS logs,"
        " (SELECT COUNT(*) FROM links WHERE card_id=:id) AS links,"
        " (SELECT COUNT(*) FROM attachments WHERE card_id=:id AND kind='transcript') AS transcripts",
        {"id": card["id"]},
    ).fetchone()
    card["archive"] = dict(r)
    card["usage"] = usage_totals("card_id=?", (card["id"],))
    return card


def push_card(card_id):
    card = card_with_counts(get_card(card_id))
    if card:
        broadcast("card", card)
    return card


def top_position(col: str) -> float:
    row = db.conn.execute("SELECT MIN(position) AS p FROM cards WHERE col=?", (col,)).fetchone()
    return (row["p"] if row and row["p"] is not None else 0) - 1


def log_event(card_id, kind, detail=""):
    db.conn.execute("INSERT INTO events(card_id, kind, at, detail) VALUES (?,?,?,?)", (card_id, kind, now(), redact((detail or "")[:500])))


def index_card(card_id):
    c = db.conn.execute("SELECT title, project, notes, last_prompt, summary, cwd FROM cards WHERE id=?", (card_id,)).fetchone()
    db.conn.execute("DELETE FROM search WHERE card_id=? AND kind='card'", (card_id,))
    if c:
        body = "\n".join(x for x in (c["project"], c["notes"], c["last_prompt"], c["summary"], c["cwd"]) if x)
        db.conn.execute("INSERT INTO search(card_id, kind, ref, title, body) VALUES (?, 'card', ?, ?, ?)", (card_id, card_id, c["title"], body))
