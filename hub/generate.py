"""Generated handoffs and digests, and background jobs."""

import json
import re
import secrets
import threading
from datetime import datetime, timedelta

from . import db
from .util import clip, is_text, iso, now, safe_name
from .db import db_lock, int_setting, setting
from .live import broadcast
from .redaction import redact_bytes
from .records import get_card, push_card
from .projects import project_by_id
from .archive import add_attachment, blob_path, card_archive
from .owui import owui_request
from .notifications import notify


gen_lock = threading.Lock()
jobs: dict[str, dict] = {}


def card_context(cid, budget, since=None) -> str:
    c = get_card(cid)
    if not c:
        return ""
    a = card_archive(cid)
    parts = [f"### {c['title']}  ({c['source']}, {c['column']}{', archived' if c.get('archived_at') else ''})",
             f"Created {iso(c['created_at'])}; last activity {iso(c['last_activity'])}."]
    if c["cwd"]:
        parts.append(f"Folder: {c['cwd']}")
    if c["url"]:
        parts.append(f"Link: {c['url']}")
    if c["notes"]:
        parts.append("Notes:\n" + clip(c["notes"], 3000))
    links = a["links"][:30]
    if links:
        parts.append("Commits / merge requests:\n" + "\n".join(f"- {l['kind']} {l['key'][:10]} {l['title']} {l['url']}" for l in links))
    files = a["files"][:80]
    if files:
        parts.append("Files written or edited:\n" + "\n".join(f"- {f['path']} ({f['last_action']}, {f['edits']}×)" for f in files))
    docs = [x for x in a["attachments"] if x["kind"] in ("handoff", "doc", "upload") and is_text(x["mime"], x["name"])]
    seen = set()
    latest_docs = [d for d in docs if not (d["path"] in seen or seen.add(d["path"]))]
    if latest_docs:
        parts.append("Documents: " + ", ".join(d["name"] for d in latest_docs[:40]))
        hand = next((d for d in latest_docs if d["kind"] == "handoff"), None)
        if hand:
            try:
                parts.append(f"Latest handoff doc ({hand['name']}):\n" + clip(blob_path(hand["sha"]).read_text("utf-8", "replace"), budget // 4))
            except OSError:
                pass
    logs = [w for w in a["worklog"] if not since or w["at"] >= since]
    head = "\n\n".join(parts)
    room = max(1000, budget - len(head))
    picked, used = [], 0
    for w in logs:  # newest first
        t = clip(w["text"], 2500)
        if used + len(t) > room:
            break
        picked.append(f"[{iso(w['at'])}]\n{t}")
        used += len(t)
    if picked:
        head += "\n\nWork log (newest first):\n" + "\n\n".join(picked)
    return head


def gather(scope, oid, since=None, exclude=()):
    budget = max(8000, int_setting("gen_max_chars", 40000))
    if scope == "card":
        c = get_card(oid)
        if not c:
            raise LookupError("card not found")
        return c["title"], c["project"], card_context(oid, budget, since), oid
    if scope == "task":
        t = db.conn.execute("SELECT t.*, p.name AS pname FROM tasks t JOIN projects p ON p.id=t.project_id WHERE t.id=?", (oid,)).fetchone()
        if not t:
            raise LookupError("task not found")
        ids = [r["id"] for r in db.conn.execute("SELECT id FROM cards WHERE task_id=? AND deleted_at IS NULL ORDER BY last_activity DESC", (oid,))]
        head = f"Task: {t['title']}\nProject: {t['pname']}\nStatus: {t['status']}\n" + (f"Notes:\n{t['notes']}\n" if t["notes"] else "")
        per = max(3000, (budget - len(head)) // max(1, len(ids)))
        return t["title"], t["pname"], head + "\n\n".join(card_context(i, per, since) for i in ids), f"task:{oid}"
    if scope == "project":
        p = project_by_id(oid)
        if not p:
            raise LookupError("project not found")
        q = "SELECT id FROM cards WHERE project=? AND deleted_at IS NULL" + (" AND last_activity>=?" if since else "") + " ORDER BY last_activity DESC LIMIT 25"
        ids = [r["id"] for r in db.conn.execute(q, (p["name"], since) if since else (p["name"],)) if r["id"] not in exclude]
        tasks = db.conn.execute("SELECT title, status FROM tasks WHERE project_id=?", (oid,)).fetchall()
        head = (f"Project: {p['name']} (status: {p['status']})\n" + (f"Description:\n{p['description']}\n" if p["description"] else "")
                + (f"Goals:\n{p['goals']}\n" if p["goals"] else "")
                + ("Links:\n" + "\n".join(f"- {l.get('label')}: {l.get('url')}" for l in json.loads(p["links"])) + "\n" if json.loads(p["links"]) else "")
                + ("Tasks:\n" + "\n".join(f"- {t['title']} ({t['status']})" for t in tasks) + "\n" if tasks else ""))
        per = max(2500, (budget - len(head)) // max(1, len(ids)))
        return p["name"], p["name"], head + "\n\n".join(card_context(i, per, since) for i in ids), f"project:{oid}"
    raise ValueError("scope must be card, task or project")


HANDOFF_PROMPT = """You write engineering handoff documents. Use only facts in the material.
If something is unknown, say so; never invent paths, versions, hosts or results. Be concise and concrete.
Output GitHub-flavoured Markdown with these sections:
# Handoff: <title>
_One-line status and date._
## Summary
## Current state
## What was built or changed (files, commits, docs)
## Decisions and why
## Open issues and risks
## Next steps (numbered, actionable)
## Reference (paths, links, sessions)"""
DRAFT_PATH, PREVIOUS = "generated:handoff", "previous"
DIGEST_PROMPT = """You write a weekly digest for one project. Use only facts in the material; never invent.
Output GitHub-flavoured Markdown:
# Weekly digest: <project> (<date range>)
## Highlights
## Progress by task or item
## Blocked or needs attention
## Next week
Keep it under 400 words."""


def llm(system, user) -> str:
    model = setting("gen_model", "chat") or "chat"
    res = owui_request("/api/chat/completions", {"model": model, "stream": False,
                       "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}, timeout=900)
    text = ((res.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
    if not text:
        raise RuntimeError("model returned no text")
    return text


def compose(kind, scope, oid, exclude=(), extra="") -> tuple:
    """Write the document with the model. Returns its owner, name, path and bytes; nothing is stored."""
    since = now() - 7 * 86400 if kind == "digest" else None
    title, project, context, owner = gather(scope, oid, since, exclude)
    if not context.strip():
        raise ValueError("nothing to summarize yet")
    today = datetime.now().strftime("%Y-%m-%d")
    if kind == "digest":
        rng = f"{(datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')} to {today}"
        text = llm(DIGEST_PROMPT, f"Project: {project}\nDate range: {rng}\n\nMaterial:\n{context}")
        name, path = f"DIGEST-{safe_name(project)}.md", "generated:digest"
    else:
        text = llm(HANDOFF_PROMPT, f"Title: {title}\nProject: {project}\nDate: {today}\n\nMaterial:\n{context}{extra}")
        name, path = f"HANDOFF-{safe_name(title)[:60]}.md", DRAFT_PATH
    footer = f"\n\n---\n_Generated {iso(now())} by VB AI Hub with model `{setting('gen_model', 'chat')}` from the {scope}'s library._\n"
    return owner, name, path, (text + footer).encode()


def generate(kind, scope, oid) -> dict:
    owner, name, path, data = compose(kind, scope, oid)
    return add_attachment(owner, kind if kind == "digest" else "handoff", name, path, data, "text/markdown")


def handoff_versions(pid) -> list:
    """The project's saved handoffs, newest first."""
    return [dict(r) for r in db.conn.execute(
        "SELECT id, name, sha, size, created_at FROM attachments WHERE card_id=? AND path=? ORDER BY created_at DESC", (f"project:{pid}", DRAFT_PATH))]


def handoff_sources(pid) -> list:
    """What a project handoff can be built from: the previous version and the cards gather() would read."""
    p = project_by_id(pid)
    if not p:
        raise LookupError("project not found")
    versions = handoff_versions(pid)
    previous = [{"id": PREVIOUS, "title": f"Handoff version {len(versions)}", "source": "handoff", "at": versions[0]["created_at"]}] if versions else []
    return previous + [{"id": r["id"], "title": r["title"], "source": r["source"], "at": r["last_activity"]} for r in db.conn.execute(
        "SELECT id, title, source, last_activity FROM cards WHERE project=? AND deleted_at IS NULL ORDER BY last_activity DESC LIMIT 25", (p["name"],))]


def draft_handoff(pid, exclude=()) -> dict:
    """Write a handoff for review. It replaces any earlier draft and stays out of the library until it's saved."""
    exclude = set(exclude)
    sources, versions, extra = handoff_sources(pid), handoff_versions(pid), ""
    if versions and PREVIOUS not in exclude:
        budget = max(8000, int_setting("gen_max_chars", 40000))
        try:
            extra = "\n\nPrevious handoff. Keep what is still true and keep its wording where nothing changed; update only what the material shows has changed:\n" + clip(blob_path(versions[0]["sha"]).read_text("utf-8", "replace"), budget // 4)
        except OSError:
            pass
    _, name, _, data = compose("handoff", "project", pid, exclude, extra)
    text = redact_bytes(data, "text/markdown", name).decode()
    row = (pid, name, text, json.dumps([s["id"] for s in sources if s["id"] not in exclude]), json.dumps(sorted(exclude)), now())
    with db_lock:
        db.conn.execute("INSERT OR REPLACE INTO handoff_drafts(project_id, name, text, used, excluded, created_at) VALUES (?,?,?,?,?,?)", row)
        db.conn.commit()
    broadcast("library", {"owner": f"project:{pid}"})
    return {"project_id": pid, "created_at": row[-1]}


def draft_state(pid) -> dict:
    sources = handoff_sources(pid)
    r = db.conn.execute("SELECT * FROM handoff_drafts WHERE project_id=?", (pid,)).fetchone()
    draft = r and dict(r) | {"used": json.loads(r["used"]), "excluded": json.loads(r["excluded"])}
    return {"draft": draft, "sources": sources, "versions": handoff_versions(pid)}


def save_draft(pid) -> dict:
    r = db.conn.execute("SELECT * FROM handoff_drafts WHERE project_id=?", (pid,)).fetchone()
    if not r:
        raise LookupError("no draft to save")
    saved = add_attachment(f"project:{pid}", "handoff", r["name"], DRAFT_PATH, r["text"].encode(), "text/markdown")
    discard_draft(pid)
    return saved


def discard_draft(pid):
    with db_lock:
        db.conn.execute("DELETE FROM handoff_drafts WHERE project_id=?", (pid,))
        db.conn.commit()
    broadcast("library", {"owner": f"project:{pid}"})


def run_job(kind, fn):
    """Run fn in the background; the job record holds its result for polling via /api/jobs/<id>."""
    jid = secrets.token_hex(6)
    job = {"id": jid, "kind": kind, "status": "running", "started": now(), "event": threading.Event()}
    jobs[jid] = job

    def go():
        try:
            job.update(status="done", result=fn())
        except Exception as e:
            job.update(status="error", error=str(e)[:300])
        job["finished"] = now()
        job["event"].set()
        for old in [k for k, v in jobs.items() if v.get("finished") and now() - v["finished"] > 3600]:
            jobs.pop(old, None)
    threading.Thread(target=go, daemon=True).start()
    return job


def start_job(kind, scope, oid, review=False, exclude=()):
    jid = secrets.token_hex(6)
    jobs[jid] = {"id": jid, "kind": kind, "scope": scope, "target": oid, "review": review, "status": "queued", "started": now()}
    broadcast("job", jobs[jid])

    def run():
        with gen_lock:  # one generation at a time; the GPU is shared
            jobs[jid]["status"] = "running"
            broadcast("job", jobs[jid])
            try:
                jobs[jid].update(status="done", result=draft_handoff(oid, exclude) if review else generate(kind, scope, oid))
            except Exception as e:
                jobs[jid].update(status="error", error=str(e)[:300])
            jobs[jid]["finished"] = now()
            broadcast("job", jobs[jid])
            if scope == "card":
                push_card(oid)
    threading.Thread(target=run, daemon=True).start()
    return jobs[jid]


def weekly_digests():
    since = now() - 7 * 86400
    rows = db.conn.execute("SELECT DISTINCT p.id, p.name FROM projects p JOIN cards c ON c.project=p.name WHERE c.last_activity>=? AND c.deleted_at IS NULL AND p.status!='archived'", (since,)).fetchall()
    done = []
    for r in rows:
        try:
            with gen_lock:
                generate("digest", "project", r["id"])
            done.append(r["name"])
        except Exception as e:
            print(f"digest {r['name']}: {e!r}", flush=True)
    if done:
        notify("Weekly digests ready", f"{len(done)} projects: " + ", ".join(done[:8]), "")
    return done
