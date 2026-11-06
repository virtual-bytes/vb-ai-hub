#!/usr/bin/env python3
"""VB AI Hub: live board + project library for Claude Code, claude.ai, Open WebUI and any tool that posts to it.

Standard library only. Env: HUB_HOST, HUB_PORT, HUB_DB, HUB_TOKEN, HUB_BACKUP_DIR (KANBAN_* names still work).
Archived files live in a content-addressed store next to the database (<db dir>/blobs).

  python3 server.py                       run
  python3 server.py --restore [SNAPSHOT]  restore hub.db + blobs from HUB_BACKUP_DIR (server must be stopped)
"""

import sys
import threading
from http.server import ThreadingHTTPServer

from hub import db
from hub.backup import restore
from hub.config import BACKUP_DIR, BLOB_DIR, DB_PATH, HOST, PORT, TOKEN
from hub.litellm import litellm_loop
from hub.maintenance import maintenance_loop
from hub.owui import owui_loop
from hub.retrieval import embed_loop
from hub.web import Handler


def main():
    if sys.argv[1:2] == ["--restore"]:
        BLOB_DIR.mkdir(parents=True, exist_ok=True)
        return restore(sys.argv[2] if len(sys.argv) > 2 else None)
    db.conn = db.connect()
    threading.Thread(target=owui_loop, daemon=True, name="owui-sync").start()
    threading.Thread(target=litellm_loop, daemon=True, name="litellm-sync").start()
    threading.Thread(target=maintenance_loop, daemon=True, name="maintenance").start()
    threading.Thread(target=embed_loop, daemon=True, name="embed").start()
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    srv.daemon_threads = True
    auth = "token required" if TOKEN else "no token (set HUB_TOKEN before exposing beyond localhost)"
    print(f"VB AI Hub on http://{HOST}:{PORT}  db={DB_PATH}  backups={BACKUP_DIR or 'off'}  {auth}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
