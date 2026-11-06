#!/usr/bin/env python3
"""Claude Code hook -> VB AI Hub.

As a hook: reads the hook JSON on stdin, POSTs the live event, and on Stop/SessionEnd starts a detached
archiver (handoff docs, files-created log, turn summaries, compressed transcript). Never blocks or fails a session.

  python3 hook.py --backfill     send and archive every existing session under ~/.claude/projects
  python3 hook.py --archive ID   archive one session now (full transcript)
  python3 hook.py --reindex      recompute token usage and commit/MR links for all sessions
  python3 hook.py --reconcile    remove hub cards for sessions deleted on this machine

Config (env or ~/.config/vb-ai-hub/config.json): HUB_URL (default http://127.0.0.1:8787), HUB_TOKEN
"""
import fcntl
import gzip
import hashlib
import json
import os
import re
import subprocess
import sys
import socket
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

CONFIG = Path.home() / ".config" / "vb-ai-hub" / "config.json"
CACHE = Path.home() / ".cache" / "vb-ai-hub"
PROJECTS = Path.home() / ".claude" / "projects"
DOC_RE = re.compile(r"\.(md|markdown|txt|rst|adoc)$", re.I)
HANDOFF_RE = re.compile(r"(handoff|hand-off|addendum|^claude\.md$|runbook)", re.I)
SCAN_RE = re.compile(r"(handoff|hand-off|addendum|claude\.md$|readme|design|runbook|plan|notes|smoke|changelog|release)", re.I)
SKIP_RE = re.compile(r"/\.claude/(projects|plans|todos)/")
MAX_DOC = 2 * 1024 * 1024
TRANSCRIPT_GROWTH = 1024 * 1024
TRANSCRIPT_EVERY = 20 * 60
FILE_TOOLS = {"Write": "write", "Edit": "edit", "MultiEdit": "edit", "NotebookEdit": "edit"}


def config():
    cfg = {}
    for p in (CONFIG, Path.home() / ".config" / "claude-kanban" / "config.json"):
        if p.exists():
            try:
                cfg = json.loads(p.read_text())
                break
            except ValueError:
                pass
    url = os.environ.get("HUB_URL") or os.environ.get("KANBAN_URL") or cfg.get("url") or "http://127.0.0.1:8787"
    token = os.environ.get("HUB_TOKEN") or os.environ.get("KANBAN_TOKEN") or cfg.get("token") or ""
    return url.rstrip("/"), token


def request(method, path, body=None, timeout=2.5, ctype="application/json"):
    url, token = config()
    data = body if isinstance(body, bytes) or body is None else json.dumps(body).encode()
    headers = {"Content-Type": ctype}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url + path, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def project_for(cwd: str) -> str:
    if not cwd:
        return ""
    try:
        root = subprocess.run(["git", "-C", cwd, "rev-parse", "--show-toplevel"], capture_output=True, text=True, timeout=1).stdout.strip()
        if root:
            return Path(root).name
    except (OSError, subprocess.SubprocessError):
        pass
    return Path(cwd).name


def ts(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return None


def records(f, start=0):
    """Yield (end_offset, raw_line, record) for complete JSON lines from a byte offset."""
    f.seek(start)
    pos = start
    for raw in f:
        if not raw.endswith(b"\n"):
            break
        pos += len(raw)
        try:
            yield pos, raw, json.loads(raw)
        except ValueError:
            continue


def summarize(transcript: str) -> dict:
    out = {"title": "", "first_prompt": "", "model": "", "msg_count": 0, "created_at": None, "last_activity": None, "cwd": ""}
    if not transcript or not os.path.exists(transcript):
        return out
    keys = (b'"custom-title"', b'"type":"user"', b'"type": "user"', b'"type":"assistant"', b'"type": "assistant"')
    try:
        with open(transcript, "rb") as f:
            for line in f:
                if not any(k in line for k in keys):
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                t = d.get("type")
                if t == "custom-title" and d.get("customTitle"):
                    out["title"] = d["customTitle"]
                    continue
                if d.get("isSidechain"):
                    continue
                stamp = ts(d.get("timestamp"))
                if stamp:
                    out["created_at"] = out["created_at"] or stamp
                    out["last_activity"] = stamp
                if t == "user":
                    content = (d.get("message") or {}).get("content")
                    if isinstance(content, str) and not content.startswith("<"):
                        out["msg_count"] += 1
                        out["first_prompt"] = out["first_prompt"] or content.strip()
                        out["cwd"] = d.get("cwd") or out["cwd"]
                elif t == "assistant":
                    out["model"] = (d.get("message") or {}).get("model") or out["model"]
    except OSError:
        pass
    return out


def build(ev: dict) -> dict:
    s = summarize(ev.get("transcript_path", ""))
    cwd = ev.get("cwd") or s["cwd"]
    prompt = ev.get("prompt") or ""
    title = s["title"]
    if not title:
        lines = (s["first_prompt"] or prompt).strip().splitlines()
        title = lines[0][:90] if lines else ""
    name = ev.get("hook_event_name", "update")
    return {
        "hook_event_name": name, "session_id": ev.get("session_id"), "cwd": cwd, "project": project_for(cwd),
        "title": title, "prompt": prompt, "model": s["model"],
        "msg_count": s["msg_count"] + (1 if name == "UserPromptSubmit" else 0),
        "created_at": s["created_at"], "last_activity": s["last_activity"] if name == "backfill" else None,
        "source": ev.get("source", ""), "reason": ev.get("reason", ""), "host": socket.gethostname(),
    }


# ---------------------------------------------------------------- archiver

MR_RE = re.compile(rb"https?://[^\s\"'\\)<>]+?/-/merge_requests/\d+|https://github\.com/[\w.-]+/[\w.-]+/pull/\d+")


def git(repo, *args):
    try:
        return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def web_url(remote: str) -> str:
    """git@host:group/repo.git | ssh://git@host:22/group/repo.git | https://user@host/group/repo.git -> https://host/group/repo"""
    r = remote.strip()
    m = re.match(r"^(?:ssh://)?[^@/]+@([^:/]+)(?::\d+)?[:/](.+?)(?:\.git)?/?$", r)
    if m:
        return f"https://{m.group(1)}/{m.group(2)}"
    m = re.match(r"^https?://(?:[^@/]+@)?([^/]+)/(.+?)(?:\.git)?/?$", r)
    return f"https://{m.group(1)}/{m.group(2)}" if m else ""


def repos_for(paths):
    roots = {}
    for p in paths:
        d = p if os.path.isdir(p) else os.path.dirname(p)
        if d and os.path.isdir(d) and d not in roots:
            roots[d] = git(d, "rev-parse", "--show-toplevel")
    return {r for r in roots.values() if r}


def commit_candidates(repos, since):
    """Commits made in these repos during the session window, keyed by their short hash."""
    out = {}
    for repo in repos:
        log = git(repo, "log", "--all", f"--since=@{int(since) - 120}", "--format=%H%x1f%h%x1f%s%x1f%ct", "-n", "300")
        base = web_url(git(repo, "remote", "get-url", "origin"))
        for line in log.splitlines():
            parts = line.split("\x1f")
            if len(parts) != 4:
                continue
            full, short, subject, ct = parts
            url = (f"{base}/commit/{full}" if "github.com" in base else f"{base}/-/commit/{full}") if base else ""
            out[short.encode()] = {"kind": "commit", "key": full, "title": subject, "url": url, "repo": os.path.basename(repo), "at": float(ct)}
    return out


def tally(msgs):
    """Sum per-message usage into per-model totals and per-day, per-model totals (local dates)."""
    totals, days = {}, {}
    for model, u, at in msgs.values():
        if model == "<synthetic>":
            continue
        day = datetime.fromtimestamp(at).strftime("%Y-%m-%d") if at else datetime.now().strftime("%Y-%m-%d")
        for acc in (totals.setdefault(model, {}), days.setdefault(day, {}).setdefault(model, {})):
            for key, val in (("input", u.get("input_tokens")), ("output", u.get("output_tokens")), ("messages", 1),
                             ("cache_write", u.get("cache_creation_input_tokens")), ("cache_read", u.get("cache_read_input_tokens"))):
                acc[key] = acc.get(key, 0) + (val or 0)
    return totals, days


def merge_usage(dst_totals, dst_days, totals, days):
    def add(dst, src):
        for model, u in src.items():
            acc = dst.setdefault(model, {})
            for k, v in u.items():
                acc[k] = acc.get(k, 0) + v
    add(dst_totals, totals)
    for day, per_model in days.items():
        add(dst_days.setdefault(day, {}), per_model)


def subagent_usage(transcript, offsets):
    """Usage from the session's subagent and workflow transcripts, resuming from per-file offsets (updated in place)."""
    root = Path(transcript[:-6]) / "subagents" if transcript.endswith(".jsonl") else None
    msgs = {}
    if root and root.is_dir():
        for p in root.rglob("*.jsonl"):
            key = str(p.relative_to(root))
            start = offsets.get(key, 0)
            try:
                if p.stat().st_size <= start:
                    continue
                with open(p, "rb") as f:
                    for offsets[key], _, d in records(f, start):
                        m = d.get("message") or {}
                        if d.get("type") == "assistant" and m.get("id") and m.get("usage"):
                            msgs[m["id"]] = (m.get("model") or "unknown", m["usage"], ts(d.get("timestamp")))
            except OSError:
                continue
    return tally(msgs)


def scan_transcript(f, start, scan_names=None, commits=None):
    """One pass over complete transcript lines from a byte offset."""
    r = {"files": [], "logs": [], "usage": {}, "days": {}, "mentioned": set(), "links": {}, "offset": start, "since": None, "last_at": None}
    msgs = {}
    for r["offset"], raw, d in records(f, start):
        for name, full in (scan_names or {}).items():
            if name in raw:
                r["mentioned"].add(full)
        for short, c in (commits or {}).items():
            if short in raw:
                r["links"][c["key"]] = c
        if b"merge_requests/" in raw or b"/pull/" in raw:
            for u in MR_RE.findall(raw):
                u = u.decode("utf-8", "replace")
                r["links"][u] = {"kind": "mr" if "merge_requests" in u else "pr", "key": u, "url": u, "title": "", "at": ts(d.get("timestamp"))}
        if d.get("type") != "assistant":
            continue
        m = d.get("message") or {}
        if m.get("id") and m.get("usage"):
            msgs[m["id"]] = (m.get("model") or "unknown", m["usage"], ts(d.get("timestamp")))  # split records repeat usage; keep the last
        if d.get("isSidechain"):
            continue
        at = ts(d.get("timestamp"))
        if at:
            r["since"] = r["since"] or at
            r["last_at"] = max(r["last_at"] or 0, at)
        content = m.get("content") or []
        for block in content:
            if block.get("type") == "tool_use" and block.get("name") in FILE_TOOLS:
                inp = block.get("input") or {}
                p = inp.get("file_path") or inp.get("notebook_path")
                if p:
                    r["files"].append({"path": p, "action": FILE_TOOLS[block["name"]], "at": at})
        if m.get("stop_reason") in ("end_turn", "stop_sequence"):
            text = "\n".join(b.get("text", "") for b in content if b.get("type") == "text").strip()
            if text:
                r["logs"].append({"ref": d.get("uuid"), "at": at, "text": text})
    r["usage"], r["days"] = tally(msgs)
    return r


def archive(sid: str, transcript: str, cwd: str, force_transcript=False):
    CACHE.mkdir(parents=True, exist_ok=True)
    cache_file = CACHE / f"{sid}.json"
    lock = open(CACHE / f"{sid}.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return  # another archiver is running for this session; the next Stop catches up
    st = {"offset": 0, "docs": {}, "touched": [], "t_size": 0, "t_at": 0, "since": None, "last_at": None}
    if cache_file.exists():
        try:
            st.update(json.loads(cache_file.read_text()))
        except ValueError:
            pass
    card = f"/api/cards/{urllib.parse.quote('code:' + sid, safe='')}"
    touched = set(st["touched"])
    mentioned = set(st.get("mentioned", []))
    # Handoff-style docs sitting in the working dir; credited only if this session's transcript names them,
    # so parallel sessions in the same folder don't pick up each other's docs. Commits work the same way:
    # only commits whose hash appears in this session's transcript are linked.
    scan = {}
    if cwd and os.path.isdir(cwd):
        scan = {p.name.encode(): str(p) for p in Path(cwd).glob("*.md") if SCAN_RE.search(p.name)}
    since = st.get("since") or (os.path.getmtime(transcript) - 30 * 86400)
    commits = {k: v for k, v in commit_candidates(repos_for([cwd, *touched] if cwd else touched), since).items()
               if v["key"] not in set(st.get("commits", []))}
    with open(transcript, "rb") as f:
        size = os.fstat(f.fileno()).st_size
        if size < st["offset"]:
            st["offset"] = 0
        r = scan_transcript(f, st["offset"], scan, commits)
    st["since"] = st.get("since") or r["since"]
    st["last_at"] = max(st.get("last_at") or 0, r["last_at"] or 0) or None
    mentioned |= r["mentioned"]
    touched |= {x["path"] for x in r["files"]}
    if r["files"]:
        request("POST", card + "/files", r["files"], timeout=15)
    if r["logs"]:
        request("POST", card + "/worklog", r["logs"], timeout=15)
    if st["offset"] < st.get("usage_offset", 0):  # a reindex already counted up to usage_offset
        with open(transcript, "rb") as f:
            rest = scan_transcript(f, st["usage_offset"])
        r["usage"], r["days"] = rest["usage"], rest["days"]
    merge_usage(r["usage"], r["days"], *subagent_usage(transcript, st.setdefault("sub", {})))
    if r["usage"]:
        request("POST", card + "/usage", {"models": r["usage"], "days": r["days"]}, timeout=15)
    if r["links"]:
        request("POST", card + "/links", list(r["links"].values()), timeout=15)
        st["commits"] = sorted(set(st.get("commits", [])) | {k for k, v in r["links"].items() if v["kind"] == "commit"})
    st["offset"] = r["offset"]

    # Docs: anything the session wrote that looks like documentation, plus handoff-style docs in the working
    # dir that changed while the session was active (catches docs written through Bash).
    candidates = {p for p in touched if DOC_RE.search(p)}
    if st.get("since"):
        lo, hi = st["since"] - 60, (st.get("last_at") or time.time()) + 600
        for p in mentioned:
            try:
                if lo <= os.stat(p).st_mtime <= hi:
                    candidates.add(p)
            except OSError:
                pass
    candidates = {p for p in candidates if not SKIP_RE.search(p)}
    for p in sorted(candidates):
        try:
            fp = Path(p)
            if not fp.is_file() or fp.stat().st_size > MAX_DOC:
                continue
            data = fp.read_bytes()
        except OSError:
            continue
        sha = hashlib.sha256(data).hexdigest()
        if st["docs"].get(p) == sha:
            continue
        kind = "handoff" if HANDOFF_RE.search(fp.name) else "doc"
        q = urllib.parse.urlencode({"kind": kind, "name": fp.name, "path": p, "at": fp.stat().st_mtime})
        request("PUT", f"{card}/attachments?{q}", data, timeout=30, ctype="text/markdown")
        st["docs"][p] = sha

    # Transcript: compressed, newest copy only; re-sent when it has grown meaningfully.
    due = force_transcript or size - st["t_size"] >= TRANSCRIPT_GROWTH or (size != st["t_size"] and time.time() - st["t_at"] >= TRANSCRIPT_EVERY)
    if due and size:
        with open(transcript, "rb") as f:
            gz = gzip.compress(f.read(), compresslevel=6)
        q = urllib.parse.urlencode({"kind": "transcript", "name": f"{sid}.jsonl.gz", "path": "claude-code-transcript", "replace": "1"})
        request("PUT", f"{card}/attachments?{q}", gz, timeout=300, ctype="application/gzip")
        st["t_size"], st["t_at"] = size, time.time()

    st["touched"] = sorted(touched)
    st["mentioned"] = sorted(mentioned)
    cache_file.write_text(json.dumps(st))


def spawn_archiver(ev: dict):
    payload = json.dumps({"sid": ev.get("session_id"), "transcript": ev.get("transcript_path"), "cwd": ev.get("cwd"),
                          "force": ev.get("hook_event_name") == "SessionEnd"})
    p = subprocess.Popen([sys.executable, os.path.abspath(__file__), "--archive-stdin"], stdin=subprocess.PIPE,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    p.stdin.write(payload.encode())
    p.stdin.close()


def reindex():
    """Recompute token usage and commit/MR links for every session (safe to repeat)."""
    n = 0
    for f in sorted(PROJECTS.glob("*/*.jsonl")):
        sid = f.stem
        card = f"/api/cards/{urllib.parse.quote('code:' + sid, safe='')}"
        cwd = summarize(str(f))["cwd"]
        with open(f, "rb") as fh:
            first = scan_transcript(fh, 0)
            touched = {x["path"] for x in first["files"]}
            commits = commit_candidates(repos_for([cwd, *touched] if cwd else touched), first["since"] or f.stat().st_mtime - 30 * 86400)
            r = scan_transcript(fh, 0, None, commits) if commits else first
        try:
            sub = {}
            merge_usage(r["usage"], r["days"], *subagent_usage(str(f), sub))
            request("POST", card + "/usage", {"replace": True, "models": r["usage"], "days": r["days"]}, timeout=15)
            cache = CACHE / f"{sid}.json"  # later runs count only what comes after this
            st = json.loads(cache.read_text()) if cache.exists() else {}
            st.update(usage_offset=r["offset"], sub=sub)
            CACHE.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(st))
            if r["links"]:
                request("POST", card + "/links", list(r["links"].values()), timeout=15)
            n += 1
            tot = sum(u["output"] for u in r["usage"].values())
            print(f"  {sid[:8]}  {len(r['links']):3} links  {tot:>9,} output tokens", flush=True)
        except OSError as e:
            if "404" not in str(e):
                print(f"  {sid[:8]} failed: {e}", file=sys.stderr)
    print(f"Reindexed {n} sessions.")


def cleanup_days() -> int:
    """Claude Code deletes transcripts older than cleanupPeriodDays (default 30); those aren't user deletions."""
    try:
        return int(json.loads((Path.home() / ".claude" / "settings.json").read_text()).get("cleanupPeriodDays") or 30)
    except (OSError, ValueError, TypeError):
        return 30


def reconcile(force=False):
    """Tell the hub which sessions still exist here, so sessions deleted in Claude Code leave the board."""
    stamp = CACHE / "last-reconcile"
    if not force and stamp.exists() and time.time() - stamp.stat().st_mtime < 3600:
        return None
    sessions = sorted(p.stem for p in PROJECTS.glob("*/*.jsonl"))
    if not sessions:
        return None
    CACHE.mkdir(parents=True, exist_ok=True)
    stamp.touch()
    return request("POST", "/api/reconcile/code", {"host": socket.gethostname(), "sessions": sessions,
                                                    "cleanup_days": cleanup_days()}, timeout=20)


def spawn(arg):
    subprocess.Popen([sys.executable, os.path.abspath(__file__), arg], stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def print_context(ev):
    """On a fresh session, hand Claude Code the project's context. Stdout is the hook's reply, so it's JSON or nothing."""
    if ev.get("hook_event_name") != "SessionStart" or ev.get("source") not in ("startup", "clear"):
        return
    card = urllib.parse.quote("code:" + str(ev.get("session_id")), safe="")
    text = request("GET", f"/api/context/{card}").get("text")
    if text:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}}))


def transcript_for(sid: str):
    hits = list(PROJECTS.glob(f"*/{sid}.jsonl"))
    return str(hits[0]) if hits else None


def backfill():
    files = sorted(PROJECTS.glob("*/*.jsonl"), key=lambda p: p.stat().st_mtime)
    cutoff = time.time() - 3 * 86400
    sent = 0
    for f in files:
        ev = build({"hook_event_name": "backfill", "session_id": f.stem, "transcript_path": str(f)})
        if not ev["msg_count"]:
            continue
        ev["last_activity"] = ev["last_activity"] or f.stat().st_mtime
        ev["column"] = "needs_me" if ev["last_activity"] > cutoff else "done"
        try:
            request("POST", "/api/events", ev, timeout=10)
            archive(f.stem, str(f), ev["cwd"], force_transcript=True)
            sent += 1
            print(f"  {ev['project'] or '-':28} {ev['title'][:70]}", flush=True)
        except OSError as e:
            print(f"failed {f.name}: {e}", file=sys.stderr)
    print(f"Backfilled and archived {sent} sessions.")


def main():
    args = sys.argv[1:]
    if args[:1] == ["--backfill"]:
        return backfill()
    if args[:1] == ["--reindex"]:
        return reindex()
    if args[:1] == ["--reconcile"]:
        print(reconcile(force=True))
        return 0
    if args[:1] == ["--reconcile-bg"]:
        try:
            reconcile()
        except Exception:
            pass
        return 0
    if args[:1] == ["--archive"] and len(args) > 1:
        t = transcript_for(args[1])
        if not t:
            sys.exit(f"no transcript for {args[1]}")
        return archive(args[1], t, summarize(t)["cwd"], force_transcript=True)
    if args[:1] == ["--archive-stdin"]:
        try:
            a = json.load(sys.stdin)
            if a.get("sid") and a.get("transcript") and os.path.exists(a["transcript"]):
                archive(a["sid"], a["transcript"], a.get("cwd") or "", a.get("force", False))
        except Exception as e:
            if os.environ.get("HUB_DEBUG"):
                print(f"vb-ai-hub archiver: {e}", file=sys.stderr)
        return 0
    try:
        ev = json.load(sys.stdin)
        request("POST", "/api/events", build(ev))
        if ev.get("hook_event_name") in ("Stop", "SessionEnd") and ev.get("transcript_path"):
            spawn_archiver(ev)
        if ev.get("hook_event_name") == "SessionStart":
            spawn("--reconcile-bg")
        print_context(ev)
    except Exception as e:  # a hub outage must never break a Claude session
        if os.environ.get("HUB_DEBUG"):
            print(f"vb-ai-hub hook: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
