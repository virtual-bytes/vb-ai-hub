"""Small helpers with no state: time, names, text and file types."""

import re
import time
from datetime import datetime, timezone


TEXT_MIME = re.compile(r"^(text/|application/(json|x-yaml|yaml|xml|toml))")


def now() -> float:
    return time.time()


def is_text(mime: str, name: str) -> bool:
    return bool(TEXT_MIME.match(mime)) or name.lower().endswith((".md", ".markdown", ".txt", ".rst", ".adoc", ".yml", ".yaml", ".json", ".toml", ".ini", ".sh", ".py", ".tf", ".hcl"))


def guess_mime(name: str) -> str:
    n = name.lower()
    for ext, m in ((".md", "text/markdown"), (".markdown", "text/markdown"), (".txt", "text/plain"), (".json", "application/json"),
                   (".yml", "application/yaml"), (".yaml", "application/yaml"), (".gz", "application/gzip"), (".pdf", "application/pdf"),
                   (".png", "image/png"), (".jpg", "image/jpeg"), (".jpeg", "image/jpeg"), (".zip", "application/zip"), (".html", "text/html")):
        if n.endswith(ext):
            return m
    return "text/plain" if is_text("", name) else "application/octet-stream"


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if ts else ""


def safe_name(s: str) -> str:
    return re.sub(r"[^\w.@+-]+", "_", s).strip("_")[:120] or "file"


def parse_ts(s) -> float | None:
    if not s:
        return None
    if isinstance(s, (int, float)):
        return float(s) / (1e9 if s > 1e15 else 1e3 if s > 1e12 else 1)
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def clip(s, n):
    s = s or ""
    return s if len(s) <= n else s[: n - 20] + "\n…[truncated]"
