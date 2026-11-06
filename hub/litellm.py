"""LiteLLM: totals the requests a gateway served from your own hardware, from its spend logs.

Only counts are kept. /spend/logs/v2 leaves out prompts and responses, and the hub reads no other endpoint."""

import json
import ssl
import threading
import urllib.parse
import urllib.request
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from .db import int_setting, set_setting, setting
from .live import status_json
from .usage import set_gpu_usage


litellm_wake = threading.Event()
PAGE_SIZE = 1000
MAX_PAGES = 500
BACKFILL_DAYS = 30
RECENT_DAYS = 2


def litellm_page(start, end, page):
    query = urllib.parse.urlencode({"start_date": start, "end_date": end, "page": page, "page_size": PAGE_SIZE,
                                    "sort_by": "startTime", "sort_order": "asc"})
    req = urllib.request.Request(f"{setting('litellm_url')}/spend/logs/v2?{query}",
                                 headers={"Authorization": f"Bearer {setting('litellm_key')}", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30, context=ssl.create_default_context()) as r:
        return json.loads(r.read()).get("data") or []


def litellm_logs(start, end):
    """Every log in the window. Oldest first, so requests arriving during the read can't shift rows between pages."""
    seen = set()
    for page in range(1, MAX_PAGES + 1):
        rows = litellm_page(start, end, page)
        for r in rows:
            rid = r.get("request_id")
            if rid is None or rid not in seen:
                seen.add(rid)
                yield r
        if len(rows) < PAGE_SIZE:
            return
    raise RuntimeError(f"more than {MAX_PAGES * PAGE_SIZE} requests in the window")


def split_list(text):
    return [x.strip() for x in text.replace(",", "\n").splitlines() if x.strip()]


def log_meta(row):
    meta = row.get("metadata")
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except ValueError:
            meta = None
    return meta if isinstance(meta, dict) else {}


def served_locally(row, rules):
    """No rules counts everything. `model:name` matches a model exactly; anything else matches part of the backend
    address or provider."""
    if not rules:
        return True
    models = {str(row.get("model_group") or "").lower(), str(row.get("model") or "").lower()}
    backend = f"{row.get('api_base') or ''} {row.get('custom_llm_provider') or ''}".lower()
    for rule in rules:
        rule = rule.lower()
        if rule[6:] in models if rule.startswith("model:") else rule in backend:
            return True
    return False


def log_day(row, tz):
    try:
        at = datetime.fromisoformat(str(row.get("startTime")).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (at if at.tzinfo else at.replace(tzinfo=timezone.utc)).astimezone(tz).date()


def summarize(logs, tz, first_day, rules, excluded):
    totals = {}
    for row in logs:
        meta = log_meta(row)
        client = meta.get("user_api_key_alias") or "?"
        day = log_day(row, tz)
        if "failure" in (row.get("status"), meta.get("status")) or client in excluded or not day or day < first_day or not served_locally(row, rules):
            continue
        t = totals.setdefault((day.isoformat(), client, row.get("model_group") or row.get("model") or "?"), [0, 0, 0])
        t[0] += 1
        t[1] += int(row.get("prompt_tokens") or 0)
        t[2] += int(row.get("completion_tokens") or 0)
    return [{"day": k[0], "client": k[1], "model": k[2], "requests": v[0], "input": v[1], "output": v[2]} for k, v in totals.items()]


def litellm_sync_once() -> dict:
    tz = ZoneInfo(setting("usage_tz") or "UTC")
    url = setting("litellm_url")
    days = RECENT_DAYS if setting("litellm_backfilled") == url else BACKFILL_DAYS
    first_day = datetime.now(tz).date() - timedelta(days=days - 1)
    # A day early and dropped again: a partly read day must never replace a complete total.
    start = datetime.combine(first_day - timedelta(days=1), time.min, tz).astimezone(timezone.utc)
    end = datetime.now(timezone.utc) + timedelta(days=1)
    fmt = "%Y-%m-%d %H:%M:%S"
    rows = summarize(litellm_logs(start.strftime(fmt), end.strftime(fmt)), tz, first_day,
                     split_list(setting("litellm_local")), set(split_list(setting("litellm_exclude"))))
    res = set_gpu_usage({"rows": rows})
    set_setting("litellm_backfilled", url)
    return {"days": days, "requests": sum(r["requests"] for r in rows), "changed": res["changed"]}


def litellm_loop():
    while True:
        try:
            if setting("litellm_url") and setting("litellm_key"):
                status_json("litellm_status", ok=True, **litellm_sync_once())
        except Exception as e:
            status_json("litellm_status", ok=False, error=str(e)[:300])
        litellm_wake.wait(max(1, int_setting("litellm_interval", 5)) * 60)
        litellm_wake.clear()
