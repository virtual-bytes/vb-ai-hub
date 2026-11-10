"""Projects, routing rules, and what an attachment owner refers to."""

import json
import re
import secrets

from . import db
from .config import PRIORITIES, RULE_FIELDS
from .util import now
from .db import db_lock
from .live import broadcast
from .records import index_card
from .redaction import redact


def ensure_project(name: str):
    if name:
        db.conn.execute("INSERT OR IGNORE INTO projects(id, name, created_at, updated_at) VALUES (?,?,?,?)",
                        (secrets.token_hex(5), name, now(), now()))


def project_by_id(pid):
    r = db.conn.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
    return dict(r) if r else None


def project_by_name(name):
    r = db.conn.execute("SELECT * FROM projects WHERE name=?", (name,)).fetchone()
    return dict(r) if r else None


def load_rules():
    rows = db.conn.execute("SELECT r.*, p.name AS project FROM rules r JOIN projects p ON p.id=r.project_id").fetchall()
    order = {f: i for i, f in enumerate(RULE_FIELDS)}
    return sorted((dict(r) for r in rows), key=lambda r: (order.get(r["field"], 9), -len(r["pattern"])))


MAX_REGEX = 100
# A quantified group followed by another quantifier, like (a+)+ or (a|b)*{2}: the shapes that make
# Python's regex engine take exponential time. Python has no regex timeout, so these are refused outright.
NESTED = re.compile(r"\)[*+?]|\)\{\d")
BACKREF = re.compile(r"\\[1-9]|\(\?P=")
_compiled: dict[str, re.Pattern | None] = {}


def is_regex(p) -> bool:
    return len(p) > 2 and p.startswith("/") and p.endswith("/")


def check_rule_pattern(field, p):
    """Raise ValueError for a regular expression rule that could stall the hub."""
    if field != "title" or not is_regex(p):
        return
    body = p[1:-1]
    if len(body) > MAX_REGEX:
        raise ValueError(f"a regular expression rule can be at most {MAX_REGEX} characters")
    if NESTED.search(body) or BACKREF.search(body):
        raise ValueError("this regular expression repeats a group; use a simpler pattern or a plain word")
    if len(re.findall(r"(?<!\\)[*+]|\{\d*,\}", body)) > 3:
        raise ValueError("a regular expression rule can repeat at most three times (*, + or {n,})")
    try:
        re.compile(body)
    except re.error as e:
        raise ValueError(f"not a valid regular expression: {e}")


def rule_regex(p):
    if p not in _compiled:
        try:
            check_rule_pattern("title", p)
            _compiled[p] = re.compile(p[1:-1], re.I)
        except ValueError:
            _compiled[p] = None  # an older rule that's no longer allowed matches nothing
    return _compiled[p]


def rule_matches(rule, title, cwd, source_project) -> bool:
    p = rule["pattern"].strip()
    if not p:
        return False
    if rule["field"] == "title":
        if is_regex(p):
            rx = rule_regex(p)
            return bool(rx and rx.search((title or "")[:200]))
        return p.lower() in (title or "").lower()
    if rule["field"] == "path":
        return bool(cwd) and (cwd == p.rstrip("/") or cwd.startswith(p.rstrip("/") + "/"))
    if rule["field"] == "source_project":
        return (source_project or "").lower() == p.lower()
    return False


def resolve_project(title, cwd, source_project, rules=None) -> str:
    for r in rules if rules is not None else load_rules():
        if rule_matches(r, title, cwd, source_project):
            return r["project"]
    return source_project or ""


def reapply_rules():
    """Recompute the project of every card whose project a person hasn't set by hand."""
    rules, changed = load_rules(), []
    with db_lock:
        for r in db.conn.execute("SELECT id, title, cwd, source_project, project, user_fields FROM cards").fetchall():
            if "project" in json.loads(r["user_fields"]):
                continue
            p = resolve_project(r["title"], r["cwd"], r["source_project"], rules)
            if p != r["project"]:
                ensure_project(p)
                db.conn.execute("UPDATE cards SET project=?, task_id=NULL WHERE id=?", (p, r["id"]))
                index_card(r["id"])
                changed.append(r["id"])
        db.conn.commit()
    if changed:
        broadcast("reload", {"reason": "rules", "count": len(changed)})
    return len(changed)


def rename_or_merge_project(pid, new_name):
    """Rename a project; if another project already has the name, merge into it."""
    new_name = new_name.strip()
    if not new_name:
        raise ValueError("name required")
    with db_lock:
        src = project_by_id(pid)
        if not src:
            raise LookupError("project not found")
        dst = project_by_name(new_name)
        if dst and dst["id"] != pid:
            merge_projects(src, dst)
            return dst["id"]
        db.conn.execute("UPDATE projects SET name=?, updated_at=? WHERE id=?", (new_name, now(), pid))
        db.conn.execute("UPDATE cards SET project=? WHERE project=?", (new_name, src["name"]))
        # Keep future events from the old source name landing in the renamed project.
        if new_name.lower() != src["name"].lower():
            db.conn.execute("INSERT INTO rules(project_id, field, pattern, created_at) VALUES (?, 'source_project', ?, ?)", (pid, src["name"], now()))
        db.conn.commit()
    broadcast("reload", {"reason": "project"})
    return pid


def merge_projects(src, dst):
    with db_lock:
        db.conn.execute("UPDATE cards SET project=? WHERE project=?", (dst["name"], src["name"]))
        db.conn.execute("UPDATE rules SET project_id=? WHERE project_id=?", (dst["id"], src["id"]))
        db.conn.execute("UPDATE tasks SET project_id=? WHERE project_id=?", (dst["id"], src["id"]))
        db.conn.execute("UPDATE attachments SET card_id=? WHERE card_id=?", (f"project:{dst['id']}", f"project:{src['id']}"))
        db.conn.execute("UPDATE search SET card_id=? WHERE card_id=?", (f"project:{dst['id']}", f"project:{src['id']}"))
        db.conn.execute("INSERT INTO rules(project_id, field, pattern, created_at) VALUES (?, 'source_project', ?, ?)", (dst["id"], src["name"], now()))
        if src["description"] and not dst["description"]:
            db.conn.execute("UPDATE projects SET description=? WHERE id=?", (src["description"], dst["id"]))
        links = json.loads(dst["links"]) + [l for l in json.loads(src["links"]) if l not in json.loads(dst["links"])]
        db.conn.execute("UPDATE projects SET links=?, updated_at=? WHERE id=?", (json.dumps(links), now(), dst["id"]))
        db.conn.execute("DELETE FROM handoff_drafts WHERE project_id=?", (src["id"],))
        db.conn.execute("DELETE FROM projects WHERE id=?", (src["id"],))
        db.conn.commit()
    broadcast("reload", {"reason": "merge"})


def index_project(pid):
    p = project_by_id(pid)
    db.conn.execute("DELETE FROM search WHERE card_id=? AND kind='project'", (f"project:{pid}",))
    if p:
        db.conn.execute("INSERT INTO search(card_id, kind, ref, title, body) VALUES (?, 'project', ?, ?, ?)",
                        (f"project:{pid}", pid, p["name"], f"{p['description']}\n{p['goals']}"))


def index_task(tid):
    t = db.conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
    db.conn.execute("DELETE FROM search WHERE card_id=? AND kind='task'", (f"task:{tid}",))
    if t:
        db.conn.execute("INSERT INTO search(card_id, kind, ref, title, body) VALUES (?, 'task', ?, ?, ?)",
                        (f"task:{tid}", tid, t["title"], t["notes"]))


def is_card(owner: str) -> bool:
    return bool(db.conn.execute("SELECT 1 FROM cards WHERE id=?", (owner,)).fetchone())


def library_owner(owner: str) -> bool:
    """True for a project's or task's own key. Cards for claude.ai projects also start with project:, so a card wins."""
    return owner.startswith(("project:", "task:")) and not is_card(owner)


def library_key_taken(owner: str) -> bool:
    """True when a library project or task already uses this key, so a card must not take it."""
    if owner.startswith("project:"):
        return bool(project_by_id(owner[8:]))
    if owner.startswith("task:"):
        return bool(db.conn.execute("SELECT 1 FROM tasks WHERE id=?", (owner[5:],)).fetchone())
    return False


def owner_exists(owner: str) -> bool:
    if is_card(owner):
        return True
    if owner.startswith("project:"):
        return bool(project_by_id(owner[8:]))
    if owner.startswith("task:"):
        return bool(db.conn.execute("SELECT 1 FROM tasks WHERE id=?", (owner[5:],)).fetchone())
    return bool(db.conn.execute("SELECT 1 FROM cards WHERE id=?", (owner,)).fetchone())


def owner_meta(owner: str) -> dict:
    """Title/project/source for anything that can own library content."""
    if not library_owner(owner):
        c = db.conn.execute("SELECT title, project, source, last_activity FROM cards WHERE id=? AND deleted_at IS NULL", (owner,)).fetchone()
        return dict(c) if c else {"title": "", "project": "", "source": "", "last_activity": None}
    if owner.startswith("project:"):
        p = project_by_id(owner[8:]) or {}
        return {"title": p.get("name", ""), "project": p.get("name", ""), "source": "project-doc", "last_activity": p.get("updated_at")}
    if owner.startswith("task:"):
        t = db.conn.execute("SELECT t.*, p.name AS pname FROM tasks t LEFT JOIN projects p ON p.id=t.project_id WHERE t.id=?", (owner[5:],)).fetchone()
        return {"title": t["title"] if t else "", "project": t["pname"] if t else "", "source": "task", "last_activity": t["updated_at"] if t else None}
    c = db.conn.execute("SELECT title, project, source, last_activity FROM cards WHERE id=? AND deleted_at IS NULL", (owner,)).fetchone()
    return dict(c) if c else {"title": "", "project": "", "source": "", "last_activity": None}


def create_task(b) -> dict:
    p = project_by_id(str(b.get("project_id") or "")) or project_by_name(str(b.get("project") or ""))
    if not p or not str(b.get("title") or "").strip():
        raise ValueError("project and title required")
    tid = secrets.token_hex(5)
    with db_lock:
        db.conn.execute("INSERT INTO tasks(id, project_id, title, notes, priority, due, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                        (tid, p["id"], redact(str(b["title"]).strip())[:200], redact(str(b.get("notes") or ""))[:20000], b.get("priority") if b.get("priority") in PRIORITIES else "normal",
                         b.get("due") if re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(b.get("due") or "")) else "", now(), now()))
        index_task(tid)
        db.conn.commit()
    broadcast("library", {"owner": f"task:{tid}"})
    return dict(db.conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())


def update_task(tid, b) -> dict:
    if not db.conn.execute("SELECT 1 FROM tasks WHERE id=?", (tid,)).fetchone():
        raise LookupError("task not found")
    upd = {k: redact(str(b[k]))[:200 if k == "title" else 20000] for k in ("title", "notes") if k in b}
    if b.get("status") in ("open", "done"):
        upd["status"] = b["status"]
    if b.get("priority") in PRIORITIES:
        upd["priority"] = b["priority"]
    if "due" in b:
        if b["due"] and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(b["due"])):
            raise ValueError("due must be YYYY-MM-DD")
        upd["due"] = b["due"] or ""
    if "project_id" in b:
        if not project_by_id(b["project_id"]):
            raise ValueError("unknown project")
        upd["project_id"] = b["project_id"]
    if upd:
        upd["updated_at"] = now()
        with db_lock:
            db.conn.execute(f"UPDATE tasks SET {','.join(f'{k}=?' for k in upd)} WHERE id=?", [*upd.values(), tid])
            index_task(tid)
            db.conn.commit()
    broadcast("library", {"owner": f"task:{tid}"})
    return dict(db.conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())
