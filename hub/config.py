"""Settings from the environment and fixed constants."""

import os
from pathlib import Path


def env(name, default=""):
    return os.environ.get(f"HUB_{name}") or os.environ.get(f"KANBAN_{name}") or default


HERE = Path(__file__).resolve().parent.parent
HOST = env("HOST", "127.0.0.1")
PORT = int(env("PORT", "8787"))
DB_PATH = env("DB", str(HERE / "hub.db"))
TOKEN = env("TOKEN")
# Single sign-on through a reverse proxy (see examples/reverse-proxy):
# the proxy sends the signed-in user in X-Authentik-Username together with this
# shared secret. Requests without both still need the token (hooks, tools).
PROXY_SECRET = env("PROXY_SECRET")
BLOB_DIR = Path(DB_PATH).resolve().parent / "blobs"
BACKUP_DIR = Path(env("BACKUP_DIR", "")) if env("BACKUP_DIR") else None
MAX_BODY = 200 * 1024 * 1024
FTS_DOC_LIMIT = 512 * 1024
COLUMNS = ["ideas", "active", "needs_me", "done"]
EDITABLE = {"title", "project", "notes", "url", "column"}
SECRET_SETTINGS = {"owui_key", "notify_url", "litellm_key"}
SESSION_COOKIE = "hub_session"
ADMIN_ONLY = {"/api/settings", "/api/backup", "/api/index", "/api/integrations/owui/apply-removals"}
RULE_FIELDS = ("title", "path", "source_project")
PRIORITIES = ("low", "normal", "high", "urgent")
DEFAULT_PRICES = """claude-fable-5-1 = 10, 50, 0.25, 12.5
claude-fable-5 = 10, 50, 1, 12.5
claude-opus-5-5 = 4, 20, 0.2, 5
claude-opus-5 = 5, 25, 0.5, 6.25
claude-opus-4 = 5, 25, 0.5, 6.25
claude-sonnet-5 = 2, 10, 0.2, 2.5
claude-sonnet-4 = 3, 15, 0.3, 3.75
claude-haiku-5-5 = 0.1, 0.5, 0.01, 0.125
claude-haiku-4 = 1, 5, 0.1, 1.25"""
DEFAULT_SETTINGS = {
    "auto_flow": "true", "owui_interval": "120",
    "gen_model": "chat", "gen_max_chars": "40000",
    "digest_enabled": "true", "digest_day": "0", "digest_hour": "7",
    "notify_format": "ha", "notify_after": "10",
    "auto_archive_days": "30",
    "backup_hour": "2", "backup_keep_days": "30",
    "context_enabled": "true", "source_delete_action": "trash", "trash_days": "7", "embed_model": "embed", "tasks_notify_hour": "8",
    "stale_handoff_days": "7", "model_prices": DEFAULT_PRICES, "budget_monthly": "", "budget_daily": "", "litellm_interval": "5", "litellm_local": "", "litellm_exclude": "", "usage_tz": "UTC", "local_label": "Local models", "client_names": "",
}
