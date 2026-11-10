#!/usr/bin/env python3
"""Cursor hook -> VB AI Hub.

As a hook: reads Cursor's hook JSON on stdin, maps it to the hub's session events and POSTs it. Never blocks
or fails a Cursor session.

  python3 cursor.py --reconcile    remove hub cards for sessions this machine no longer has
  python3 cursor.py --backfill     nothing to import: Cursor has no documented local transcript folder

Config (env or ~/.config/vb-ai-hub/config.json): HUB_URL (default http://127.0.0.1:8787), HUB_TOKEN
"""
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

CONFIG = Path.home() / ".config" / "vb-ai-hub" / "config.json"
CACHE = Path.home() / ".cache" / "vb-ai-hub"
JOURNAL = CACHE / "cursor-sessions.json"
# Cursor's hooks.json names, mapped to the events the hub understands.
EVENTS = {"sessionStart": "SessionStart", "beforeSubmitPrompt": "UserPromptSubmit", "stop": "Stop", "sessionEnd": "SessionEnd"}
USAGE_KEYS = {
    "input": "input", "input_tokens": "input",
    "output": "output", "output_tokens": "output",
    "cache_write": "cache_write", "cache_creation_input_tokens": "cache_write",
    "cache_read": "cache_read", "cache_read_input_tokens": "cache_read",
    "messages": "messages", "message_count": "messages",
}


def config():
    cfg = {}
    if CONFIG.exists():
        try:
            cfg = json.loads(CONFIG.read_text())
        except ValueError:
            pass
    url = os.environ.get("HUB_URL") or cfg.get("url") or "http://127.0.0.1:8787"
    token = os.environ.get("HUB_TOKEN") or cfg.get("token") or ""
    return url.rstrip("/"), token


def request(method, path, body=None, timeout=2.5):
    url, token = config()
    data = None if body is None else json.dumps(body).encode()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url + path, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def project_for(cwd: str) -> str:
    if not cwd:
        return ""
    try:
        root = subprocess.run(["git", "--no-optional-locks", "-C", cwd, "rev-parse", "--show-toplevel"],
                              capture_output=True, text=True, timeout=1).stdout.strip()
        if root:
            return Path(root).name
    except (OSError, subprocess.SubprocessError):
        pass
    return Path(cwd).name


def usage_for(ev: dict, model: str) -> dict:
    """Token counts when a payload carries them (Cursor's hooks don't today); ignore anything unexpected."""
    raw = ev.get("usage")
    if not isinstance(raw, dict) or not raw:
        return {}
    per_model = all(isinstance(v, dict) for v in raw.values())
    rows = raw.items() if per_model else [(model or "unknown", raw)]
    out = {}
    for name, counts in rows:
        if not isinstance(counts, dict):
            continue
        row = {USAGE_KEYS[k]: int(v) for k, v in counts.items()
               if k in USAGE_KEYS and isinstance(v, (int, float)) and not isinstance(v, bool)}
        if row:
            out[str(name or "unknown")[:100]] = row
    return out


def normalize(ev: dict) -> dict:
    raw = str(ev.get("hook_event_name") or ev.get("event") or "")
    name = EVENTS.get(raw, raw or "update")
    sid = str(ev.get("session_id") or ev.get("conversation_id") or "")[:100]
    cwd = ev.get("cwd")
    if not isinstance(cwd, str) or not cwd:
        roots = ev.get("workspace_roots")
        cwd = next((r for r in roots if isinstance(r, str) and r), "") if isinstance(roots, list) else ""
    cwd = cwd or os.environ.get("CURSOR_PROJECT_DIR", "")
    prompt = ev.get("prompt") if isinstance(ev.get("prompt"), str) else ""
    model = str(ev.get("model") or ev.get("model_id") or "")[:100]
    out = {
        "hook_event_name": name, "session_id": sid, "cwd": cwd, "project": project_for(cwd),
        "title": str(ev.get("title") or "")[:300] or None,
        "prompt": prompt, "model": model or None, "reason": str(ev.get("reason") or "")[:100] or None,
        "host": socket.gethostname(),
    }
    for key in ("created_at", "last_activity", "msg_count"):
        if ev.get(key) is not None:
            out[key] = ev[key]
    usage = usage_for(ev, model)
    if usage:
        out["usage"] = usage
    return out


def remember(sid: str):
    """Keep the session ids this machine has seen, so --reconcile has a list without guessing at Cursor's storage."""
    if not sid:
        return
    try:
        seen = json.loads(JOURNAL.read_text())
    except (OSError, ValueError):
        seen = {}
    if not isinstance(seen, dict):
        seen = {}
    seen = {str(k): v for k, v in seen.items() if isinstance(v, (int, float))}
    seen[sid] = time.time()
    CACHE.mkdir(parents=True, exist_ok=True)
    JOURNAL.write_text(json.dumps(dict(sorted(seen.items(), key=lambda kv: kv[1])[-500:])))


def cleanup_days() -> int:
    try:
        return max(1, int(os.environ.get("HUB_CURSOR_CLEANUP_DAYS") or 30))
    except ValueError:
        return 30


def reconcile(force=False):
    """Tell the hub which sessions this machine still has. Only sessions seen within the cleanup window count."""
    stamp = CACHE / "last-cursor-reconcile"
    if not force and stamp.exists() and time.time() - stamp.stat().st_mtime < 3600:
        return None
    try:
        seen = json.loads(JOURNAL.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(seen, dict):
        return None
    days = cleanup_days()
    cutoff = time.time() - days * 86400
    sessions = sorted(s for s, at in seen.items() if isinstance(at, (int, float)) and at >= cutoff)
    if not sessions:
        return None
    CACHE.mkdir(parents=True, exist_ok=True)
    stamp.touch()
    return request("POST", "/api/reconcile/cursor", {"host": socket.gethostname(), "sessions": sessions,
                                                     "cleanup_days": days}, timeout=20)


def spawn(arg):
    subprocess.Popen([sys.executable, os.path.abspath(__file__), arg], stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def main():
    args = sys.argv[1:]
    if args[:1] == ["--reconcile"]:
        try:
            print(reconcile(force=True))
        except Exception as e:
            if os.environ.get("HUB_DEBUG"):
                print(f"vb-ai-hub cursor: {e}", file=sys.stderr)
        return 0
    if args[:1] == ["--reconcile-bg"]:
        try:
            reconcile()
        except Exception:
            pass
        return 0
    if args[:1] == ["--backfill"]:
        print("Nothing to import: Cursor has no documented local transcript folder. Live sessions appear as you work.")
        return 0
    try:
        ev = normalize(json.load(sys.stdin))
        remember(ev.get("session_id", ""))
        request("POST", "/api/cursor", ev)
        if ev.get("hook_event_name") == "SessionStart":
            spawn("--reconcile-bg")
    except Exception as e:  # a hub outage must never break a Cursor session
        if os.environ.get("HUB_DEBUG"):
            print(f"vb-ai-hub cursor: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
