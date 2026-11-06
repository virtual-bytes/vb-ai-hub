"""Server-sent events to open browsers, and background job status."""

import json
import queue
import threading

from .util import now
from .db import set_setting, setting


subscribers: list[queue.Queue] = []
sub_lock = threading.Lock()


def broadcast(kind: str, payload):
    msg = json.dumps({"kind": kind, "data": payload})
    with sub_lock:
        for q in list(subscribers):
            try:
                q.put_nowait(msg)
            except queue.Full:
                pass


def status_json(key, **data):
    set_setting(key, json.dumps({"at": now(), **data}))
    broadcast("settings", {key: setting(key)})
