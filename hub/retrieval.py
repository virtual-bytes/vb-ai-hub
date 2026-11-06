"""Ask the library: chunking, embeddings, retrieval and answers."""

import array
import hashlib
import math
import re
import time
import urllib.error

from . import db
from .config import FTS_DOC_LIMIT
from .util import clip, is_text, iso
from .db import db_lock, set_setting, setting
from .live import status_json
from .projects import owner_meta, project_by_id
from .archive import blob_path
from .owui import owui_request
from .generate import gen_lock, llm


CHUNK = 1400
OVERLAP = 150


def split_text(text):
    """Split on headings/paragraphs into ~CHUNK-sized pieces with a little overlap."""
    text = (text or "").strip()
    if len(text) <= CHUNK:
        return [text] if text else []
    parts, buf = [], ""
    for block in re.split(r"\n(?=#{1,4} )|\n\s*\n", text):
        if len(buf) + len(block) + 2 <= CHUNK:
            buf = f"{buf}\n\n{block}" if buf else block
            continue
        if buf:
            parts.append(buf)
        while len(block) > CHUNK:
            parts.append(block[:CHUNK])
            block = block[CHUNK - OVERLAP:]
        buf = (parts[-1][-OVERLAP:] + "\n" if parts else "") + block
    if buf.strip():
        parts.append(buf)
    return parts


def corpus():
    """Everything worth retrieving: (owner, kind, ref, title, text)."""
    for r in db.conn.execute("SELECT w.id, w.card_id, w.text, c.title FROM worklog w JOIN cards c ON c.id=w.card_id WHERE c.deleted_at IS NULL"):
        yield r["card_id"], "log", str(r["id"]), r["title"], r["text"]
    for r in db.conn.execute("SELECT id, title, notes, summary, project FROM cards WHERE deleted_at IS NULL AND (notes!='' OR summary!='')"):
        yield r["id"], "card", r["id"], r["title"], f"{r['title']}\n{r['notes']}"
    for r in db.conn.execute("""SELECT a.card_id, a.path, a.name, a.sha, a.size, a.mime FROM attachments a
                             WHERE a.kind!='transcript' AND a.size<=? AND a.created_at=(SELECT MAX(created_at) FROM attachments b
                             WHERE b.card_id=a.card_id AND b.path=a.path)""", (FTS_DOC_LIMIT,)):
        if is_text(r["mime"], r["name"]):
            try:
                yield r["card_id"], "doc", r["path"], r["name"], blob_path(r["sha"]).read_text("utf-8", "replace")
            except OSError:
                pass
    for r in db.conn.execute("SELECT id, name, description, goals FROM projects WHERE description!='' OR goals!=''"):
        yield f"project:{r['id']}", "project", r["id"], r["name"], f"{r['description']}\n{r['goals']}"
    for r in db.conn.execute("SELECT id, title, notes FROM tasks WHERE notes!=''"):
        yield f"task:{r['id']}", "task", r["id"], r["title"], r["notes"]


def owui_embed(texts):
    model = setting("embed_model", "embed") or "embed"
    res = owui_request("/api/embeddings", {"model": model, "input": texts}, timeout=300)
    data = res.get("data") if isinstance(res, dict) else None
    if not data:
        raise RuntimeError("no embeddings in response")
    out = []
    for d in sorted(data, key=lambda x: x.get("index", 0)):
        v = d["embedding"]
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        out.append(array.array("f", (x / n for x in v)).tobytes())
    return out, model


def index_chunks(limit=400) -> dict:
    """Bring the chunk table in line with the corpus; embed new chunks when embeddings are available."""
    seen, added = set(), 0
    for owner, kind, ref, title, text in corpus():
        h = hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()
        seen.add((owner, kind, ref))
        row = db.conn.execute("SELECT hash FROM chunks WHERE owner=? AND kind=? AND ref=? AND idx=0", (owner, kind, ref)).fetchone()
        if row and row["hash"] == h:
            continue
        with db_lock:
            db.conn.execute("DELETE FROM chunks WHERE owner=? AND kind=? AND ref=?", (owner, kind, ref))
            for i, piece in enumerate(split_text(text)):
                db.conn.execute("INSERT INTO chunks(owner, kind, ref, idx, hash, text) VALUES (?,?,?,?,?,?)",
                                (owner, kind, ref, i, h, f"{title}\n{piece}" if i == 0 else piece))
            db.conn.commit()
        added += 1
    with db_lock:  # drop chunks whose source is gone
        for r in db.conn.execute("SELECT DISTINCT owner, kind, ref FROM chunks").fetchall():
            if (r["owner"], r["kind"], r["ref"]) not in seen:
                db.conn.execute("DELETE FROM chunks WHERE owner=? AND kind=? AND ref=?", (r["owner"], r["kind"], r["ref"]))
        db.conn.commit()
    embedded, err = 0, None
    if setting("owui_key") and setting("embeddings_off") != "true":
        model = setting("embed_model", "embed")
        todo = db.conn.execute("SELECT id, text FROM chunks WHERE vec IS NULL OR model!=? LIMIT ?", (model, limit)).fetchall()
        for i in range(0, len(todo), 32):
            batch = todo[i:i + 32]
            try:
                vecs, used = owui_embed([r["text"][:6000] for r in batch])
            except urllib.error.HTTPError as e:
                err = f"embeddings unavailable ({e.code})"
                if e.code in (404, 405):
                    set_setting("embeddings_off", "true")
                break
            except Exception as e:
                err = str(e)[:200]
                break
            with db_lock:
                for r, v in zip(batch, vecs):
                    db.conn.execute("UPDATE chunks SET vec=?, model=? WHERE id=?", (v, used, r["id"]))
                db.conn.commit()
            embedded += len(batch)
    total = db.conn.execute("SELECT COUNT(*) n, SUM(vec IS NOT NULL) v FROM chunks").fetchone()
    return {"sources_updated": added, "embedded": embedded, "chunks": total["n"], "with_vectors": total["v"] or 0, "error": err}


def owners_for_project(pid):
    p = project_by_id(pid)
    if not p:
        return None
    owners = {r["id"] for r in db.conn.execute("SELECT id FROM cards WHERE project=? AND deleted_at IS NULL", (p["name"],))}
    owners |= {f"task:{r['id']}" for r in db.conn.execute("SELECT id FROM tasks WHERE project_id=?", (pid,))}
    owners.add(f"project:{pid}")
    return owners


def retrieve(question, pid=None, k=10):
    owners = owners_for_project(pid) if pid else None
    scores = {}
    # keyword side
    terms = re.findall(r"[\w.-]{3,}", question)[:12]
    if terms:
        fts = " OR ".join('"' + t.replace('"', "") + '"*' for t in terms)
        for rank, r in enumerate(db.conn.execute("SELECT card_id, kind, ref FROM search WHERE search MATCH ? ORDER BY rank LIMIT 60", (fts,))):
            if owners is not None and r["card_id"] not in owners:
                continue
            kind = r["kind"]
            for c in db.conn.execute("SELECT id, text FROM chunks WHERE owner=? AND kind=? AND ref=?", (r["card_id"], kind, r["ref"])).fetchall():
                bonus = sum(t.lower() in c["text"].lower() for t in terms) / len(terms)
                scores[c["id"]] = scores.get(c["id"], 0) + 1 / (60 + rank) + bonus * 0.01
    # vector side
    vec_ok = False
    if setting("owui_key") and setting("embeddings_off") != "true":
        try:
            qv = array.array("f")
            qv.frombytes(owui_embed([question])[0][0])
            model = setting("embed_model", "embed")
            sims = []
            for c in db.conn.execute("SELECT id, owner, vec FROM chunks WHERE vec IS NOT NULL AND model=?", (model,)):
                if owners is not None and c["owner"] not in owners:
                    continue
                v = array.array("f")
                v.frombytes(c["vec"])
                if len(v) == len(qv):
                    sims.append((sum(a * b for a, b in zip(qv, v)), c["id"]))
            sims.sort(reverse=True)
            for rank, (_, cid) in enumerate(sims[:60]):
                scores[cid] = scores.get(cid, 0) + 1 / (60 + rank)
            vec_ok = bool(sims)
        except Exception:
            pass
    top = sorted(scores.items(), key=lambda x: -x[1])[: k * 4]
    out, per_owner, seen_text = [], {}, set()
    for cid, _ in top:
        c = db.conn.execute("SELECT owner, kind, ref, text FROM chunks WHERE id=?", (cid,)).fetchone()
        if not c or per_owner.get((c["owner"], c["ref"]), 0) >= 2:
            continue
        fp = hashlib.sha1(re.sub(r"\s+", " ", c["text"].split("\n", 1)[-1])[:400].encode()).hexdigest()
        if fp in seen_text:  # forked sessions repeat the same passages
            continue
        seen_text.add(fp)
        per_owner[(c["owner"], c["ref"])] = per_owner.get((c["owner"], c["ref"]), 0) + 1
        meta = owner_meta(c["owner"])
        if not meta["title"]:
            continue
        at = None
        if c["kind"] == "log":
            w = db.conn.execute("SELECT at FROM worklog WHERE id=?", (c["ref"],)).fetchone()
            at = w["at"] if w else None
        out.append({"owner": c["owner"], "kind": c["kind"], "ref": c["ref"], "text": c["text"], "title": meta["title"],
                    "project": meta["project"], "source": meta["source"], "at": at or meta["last_activity"]})
        if len(out) >= k:
            break
    return out, vec_ok


ASK_PROMPT = """You answer questions about the user's AI work using only the numbered sources from their project library.
Cite sources inline like [1] or [2][3]. If the sources don't contain the answer, say so plainly and suggest where to look.
Be concise and concrete: names, paths, versions, decisions, dates. Markdown is fine."""


def ask(question, pid=None):
    question = (question or "").strip()
    if not question:
        raise ValueError("question required")
    sources, vec_ok = retrieve(question, pid)
    if not sources:
        return {"answer": "Nothing in the library matches that question yet.", "sources": [], "semantic": vec_ok}
    ctx = "\n\n".join(f"[{i}] {s['title']} ({s['kind']}, {s['project'] or 'no project'}, {iso(s['at'])})\n{clip(s['text'], 1800)}"
                      for i, s in enumerate(sources, 1))
    with gen_lock:
        answer = llm(ASK_PROMPT, f"Question: {question}\n\nSources:\n{ctx}")
    for s in sources:
        s["snippet"] = clip(s.pop("text"), 300)
    return {"answer": answer, "sources": sources, "semantic": vec_ok}


def embed_loop():
    while True:
        time.sleep(90)
        try:
            res = index_chunks()
            if res["sources_updated"] or res["embedded"] or res["error"]:
                status_json("index_status", ok=not res["error"], **res)
        except Exception as e:
            status_json("index_status", ok=False, error=str(e)[:200])
