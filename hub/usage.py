"""Token usage: per card, per day, and GPU-served totals."""

import re
from datetime import datetime, timedelta

from . import db
from .util import now
from .db import db_lock, setting
from .live import broadcast
from .projects import project_by_id
from .records import push_card


USAGE_COLS = ("input", "output", "cache_write", "cache_read", "messages")


def add_daily(card_id, day, model, vals):
    if not any(vals):
        return
    db.conn.execute(
        "INSERT INTO usage_daily(card_id, day, model, source, input, output, cache_write, cache_read, messages) VALUES (?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(card_id, day, model) DO UPDATE SET input=input+excluded.input, output=output+excluded.output,"
        " cache_write=cache_write+excluded.cache_write, cache_read=cache_read+excluded.cache_read, messages=messages+excluded.messages",
        (card_id, day, str(model)[:100], card_id.split(":", 1)[0], *vals))


def add_usage(card_id, body):
    """Per-card totals plus daily history. body: {models, days?, replace?}. days = {"YYYY-MM-DD": {model: {...}}};
    without it the change since the last report is dated today. Daily rows outlive their card: spent is spent."""
    models, days, replace = body.get("models") or {}, body.get("days"), body.get("replace")
    today = datetime.now().strftime("%Y-%m-%d")
    with db_lock:
        before = {r["model"]: [r[k] for k in USAGE_COLS] for r in db.conn.execute("SELECT * FROM usage WHERE card_id=?", (card_id,))}
        if replace:
            db.conn.execute("DELETE FROM usage WHERE card_id=?", (card_id,))
        for model, u in models.items():
            vals = [int(u.get(k) or 0) for k in USAGE_COLS]
            db.conn.execute(
                "INSERT INTO usage(card_id, model, input, output, cache_write, cache_read, messages) VALUES (?,?,?,?,?,?,?)"
                " ON CONFLICT(card_id, model) DO UPDATE SET input=input+excluded.input, output=output+excluded.output,"
                " cache_write=cache_write+excluded.cache_write, cache_read=cache_read+excluded.cache_read, messages=messages+excluded.messages",
                (card_id, str(model)[:100], *vals))
            if days is None:
                old = before.get(str(model)[:100], [0] * 5) if replace else [0] * 5
                add_daily(card_id, today, model, [max(0, a - b) for a, b in zip(vals, old)])
        if days is not None:
            if replace:
                db.conn.execute("DELETE FROM usage_daily WHERE card_id=?", (card_id,))
            for day, per_model in days.items():
                if re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(day)):
                    for model, u in per_model.items():
                        add_daily(card_id, day, model, [int(u.get(k) or 0) for k in USAGE_COLS])
        db.conn.commit()
    push_card(card_id)
    broadcast("usage", {})


def set_gpu_usage(body):
    """Daily totals of work served by your own hardware. Each post is the full picture per day/client/model, so it's
    an idempotent upsert; days the source has since pruned simply keep their last value."""
    rows, changed = body.get("rows") or [], 0
    with db_lock:
        for r in rows:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(r.get("day"))):
                continue
            key = (r["day"], str(r.get("client") or "?")[:80], str(r.get("model") or "?")[:100])
            vals = (int(r.get("requests") or 0), int(r.get("input") or 0), int(r.get("output") or 0))
            old = db.conn.execute("SELECT requests, input, output FROM gpu_usage WHERE day=? AND client=? AND model=?", key).fetchone()
            if old is None or tuple(old) != vals:
                db.conn.execute("INSERT OR REPLACE INTO gpu_usage(day, client, model, requests, input, output) VALUES (?,?,?,?,?,?)", (*key, *vals))
                changed += 1
        db.conn.execute("INSERT OR REPLACE INTO settings VALUES ('gpu_updated', ?)", (str(now()),))
        db.conn.commit()
    if changed:
        broadcast("usage", {})
    return {"rows": len(rows), "changed": changed}


def gpu_stats(today, since):
    sums = "SUM(requests) requests, SUM(input) input, SUM(output) output"

    def total(where="1=1", args=()):
        r = db.conn.execute(f"SELECT {sums} FROM gpu_usage WHERE {where}", args).fetchone()
        d = {k: r[k] or 0 for k in ("requests", "input", "output")}
        d["total"] = d["input"] + d["output"]
        return d

    def grouped(col):
        rows = [dict(r) | {"total": (r["input"] or 0) + (r["output"] or 0)} for r in db.conn.execute(f"SELECT {col} name, {sums} FROM gpu_usage GROUP BY {col}")]
        return sorted(rows, key=lambda r: -r["total"])

    return {"today": total("day=?", (today,)), "week": total("day>=? AND day<=?", (since(7), today)),
            "month": total("day>=? AND day<=?", (since(30), today)), "all": total(),
            "by_client": grouped("client"), "by_model": grouped("model"), "updated": float(setting("gpu_updated") or 0) or None}


def usage_summary(today, scope="1=1", args=()):
    """Period totals, the last 30 days and breakdowns, for every card or just those in scope. Returns the summary and
    a function giving the date n days back, counting today as the first."""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", today or ""):
        today = datetime.now().strftime("%Y-%m-%d")
    base = datetime.strptime(today, "%Y-%m-%d")
    since = lambda n: (base - timedelta(days=n - 1)).strftime("%Y-%m-%d")
    sums = "SUM(input) input, SUM(cache_write) cache_write, SUM(cache_read) cache_read, SUM(output) output, SUM(messages) messages"

    def total(where="1=1", extra=()):
        r = db.conn.execute(f"SELECT {sums} FROM usage_daily WHERE ({scope}) AND {where}", (*args, *extra)).fetchone()
        d = {k: r[k] or 0 for k in USAGE_COLS}
        d["total"] = d["input"] + d["cache_write"] + d["cache_read"] + d["output"]
        return d

    def grouped(col):
        rows = [dict(r) for r in db.conn.execute(f"SELECT {col} name, {sums} FROM usage_daily WHERE {scope} GROUP BY {col}", args)]
        for r in rows:
            r["total"] = r["input"] + r["cache_write"] + r["cache_read"] + r["output"]
        return sorted(rows, key=lambda r: -r["total"])

    by_day = {r["day"]: r for r in db.conn.execute(
        f"SELECT day, {sums} FROM usage_daily WHERE ({scope}) AND day>=? AND day<=? GROUP BY day", (*args, since(30), today))}
    series = []
    for i in range(29, -1, -1):
        day = (base - timedelta(days=i)).strftime("%Y-%m-%d")
        r = by_day.get(day)
        out = r["output"] if r else 0
        series.append({"day": day, "output": out, "total": (r["input"] + r["cache_write"] + r["cache_read"] + out) if r else 0})
    return {"today": total("day=?", (today,)), "week": total("day>=? AND day<=?", (since(7), today)),
            "month": total("day>=? AND day<=?", (since(30), today)), "all": total(),
            "series": series, "by_model": grouped("model"), "by_source": grouped("source"), "by_card": grouped("card_id")}, since


def usage_stats(today=""):
    """Totals for the ticker and its breakdown. today comes from the viewer so 'today' is their local day."""
    stats, since = usage_summary(today)
    del stats["by_card"]
    days = [d["day"] for d in stats["series"]]
    gpu_day = {r["day"]: r["t"] for r in db.conn.execute("SELECT day, SUM(input + output) t FROM gpu_usage WHERE day>=? AND day<=? GROUP BY day", (days[0], days[-1]))}
    for d in stats["series"]:
        d["gpu"] = gpu_day.get(d["day"], 0)
    return stats | {"gpu": gpu_stats(days[-1], since)}


def parse_prices(text) -> dict:
    """Lines of `model = input, output[, cache read, cache write]` in dollars per million tokens. Cache prices default to the usual multiples of input."""
    out = {}
    for n, line in enumerate(text.splitlines(), 1):
        line = line.split("#")[0].strip()
        if not line:
            continue
        model, sep, nums = line.partition("=")
        try:
            vals = [float(x) for x in nums.split(",")]
        except ValueError:
            vals = []
        if not sep or not model.strip() or len(vals) not in (2, 3, 4) or min(vals) < 0:
            raise ValueError(f"prices, line {n}: expected model = input, output, cache read, cache write")
        out[model.strip()] = (vals[0], vals[1], vals[2] if len(vals) > 2 else vals[0] * .1, vals[3] if len(vals) > 3 else vals[0] * 1.25)
    return out


def price_of(model, prices):
    """An exact match, or the longest name the model starts with, so one line can cover a family."""
    if model in prices:
        return prices[model]
    return prices[max((p for p in prices if model.startswith(p)), key=len, default="")] if any(model.startswith(p) for p in prices) else None


def priced_rows(day_now):
    prices = parse_prices(setting("model_prices"))
    rows, unpriced = [], set()
    for r in db.conn.execute("SELECT card_id, day, model, source, input, output, cache_read, cache_write FROM usage_daily WHERE day<=?", (day_now,)):
        p = price_of(r["model"], prices)
        if p is None:
            unpriced.add(r["model"])
        cost = (r["input"] * p[0] + r["output"] * p[1] + r["cache_read"] * p[2] + r["cache_write"] * p[3]) / 1e6 if p else 0
        rows.append({"card_id": r["card_id"], "day": r["day"], "model": r["model"], "source": r["source"], "cost": cost})
    return rows, sorted(unpriced)


def budget_state(day_now, rows=None) -> dict:
    rows = priced_rows(day_now)[0] if rows is None else rows
    month = sum(r["cost"] for r in rows if r["day"].startswith(day_now[:7]))
    day = sum(r["cost"] for r in rows if r["day"] == day_now)
    d = datetime.strptime(day_now, "%Y-%m-%d")
    length = ((d.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)).day

    def limit(key):
        try:
            return max(float(setting(key) or 0), 0)
        except ValueError:
            return 0
    return {"month": month, "day": day, "monthly": limit("budget_monthly"), "daily": limit("budget_daily"), "projected": month / d.day * length}


def usage_report(today=""):
    """The usage page: totals and breakdowns for each time range, and every day since records began."""
    day_now = usage_summary(today)[0]["series"][-1]["day"]
    base = datetime.strptime(day_now, "%Y-%m-%d")
    back = lambda n: (base - timedelta(days=n)).strftime("%Y-%m-%d")
    projects = {r["id"]: r["project"] or "" for r in db.conn.execute("SELECT id, project FROM cards")}

    def by_project(rows):
        out = {}
        for r in rows:
            name = projects.get(r["name"], "")
            p = out.setdefault(name, {"name": name, **{k: 0 for k in (*USAGE_COLS, "total")}})
            for k in (*USAGE_COLS, "total"):
                p[k] += r[k]
        return sorted(out.values(), key=lambda r: -r["total"])

    def local(where, args):
        sums = "COALESCE(SUM(requests),0) requests, COALESCE(SUM(input),0) input, COALESCE(SUM(output),0) output"
        total = dict(db.conn.execute(f"SELECT {sums} FROM gpu_usage WHERE {where}", args).fetchone())

        def grouped(col):
            rows = [dict(r) for r in db.conn.execute(f"SELECT {col} name, {sums} FROM gpu_usage WHERE {where} GROUP BY {col}", args)]
            return sorted(({**r, "total": r["input"] + r["output"]} for r in rows), key=lambda r: -r["total"])
        return {**total, "total": total["input"] + total["output"], "by_client": grouped("client"), "by_model": grouped("model")}

    costs, unpriced = priced_rows(day_now)

    def spent(rows, field, name=lambda v: v):
        out = {}
        for r in rows:
            out[name(r[field])] = out.get(name(r[field]), 0) + r["cost"]
        return out

    ranges = {}
    for key, days in (("7", 7), ("30", 30), ("all", None)):
        where, args = ("day>=? AND day<=?", (back(days - 1), day_now)) if days else ("1=1", ())
        s = usage_summary(day_now, where, args)[0]
        rows = [c for c in costs if not days or c["day"] >= back(days - 1)]
        ranges[key] = {"claude": s["all"], "by_model": s["by_model"], "by_source": s["by_source"], "by_project": by_project(s["by_card"]), "local": local(where, args),
                       "cost": sum(c["cost"] for c in rows)}
        for group, field, name in (("by_model", "model", lambda v: v), ("by_source", "source", lambda v: v), ("by_project", "card_id", lambda v: projects.get(v, ""))):
            totals = spent(rows, field, name)
            for g in ranges[key][group]:
                g["cost"] = totals.get(g["name"], 0)
        if days:
            before = (back(2 * days - 1), back(days))
            ranges[key]["before"] = usage_summary(day_now, "day>=? AND day<=?", before)[0]["all"]["total"] + local("day>=? AND day<=?", before)["total"]
            ranges[key]["cost_before"] = sum(c["cost"] for c in costs if before[0] <= c["day"] <= before[1])
    sums = "SUM(input + cache_write + cache_read + output) total, SUM(output) output"
    claude = {r["day"]: r for r in db.conn.execute(f"SELECT day, {sums} FROM usage_daily WHERE day<=? GROUP BY day", (day_now,))}
    gpu = {r["day"]: r["t"] for r in db.conn.execute("SELECT day, SUM(input + output) t FROM gpu_usage WHERE day<=? GROUP BY day", (day_now,))}
    first = min([*claude, *gpu, back(29)])
    series, day, by_day = [], datetime.strptime(first, "%Y-%m-%d"), spent(costs, "day")
    while day <= base:
        d = day.strftime("%Y-%m-%d")
        series.append({"day": d, "total": claude[d]["total"] if d in claude else 0, "output": claude[d]["output"] if d in claude else 0, "gpu": gpu.get(d, 0), "cost": by_day.get(d, 0)})
        day += timedelta(days=1)
    return {"ranges": ranges, "series": series, "budget": budget_state(day_now, costs), "unpriced": unpriced}


def project_usage(pid, today=""):
    """The same figures for one project's cards. Local model use isn't recorded per project, so it isn't here."""
    p = project_by_id(pid)
    if not p:
        raise LookupError("project not found")
    stats, _ = usage_summary(today, "card_id IN (SELECT id FROM cards WHERE project=? AND deleted_at IS NULL)", (p["name"],))
    titles = {r["id"]: r["title"] for r in db.conn.execute("SELECT id, title FROM cards WHERE project=? AND deleted_at IS NULL", (p["name"],))}
    stats["by_card"] = [c | {"title": titles.get(c["name"], c["name"])} for c in stats["by_card"][:8]]
    return stats
