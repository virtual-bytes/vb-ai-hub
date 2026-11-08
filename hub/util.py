"""Small helpers with no state: time, names, text and file types."""

import re
import time
from datetime import datetime, timezone
from urllib.parse import quote, urlparse


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
    try:
        return datetime.fromtimestamp(float(ts), timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if ts else ""
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


MIME = re.compile(r"[a-z0-9!#$&^_.+-]{1,64}/[a-z0-9!#$&^_.+-]{1,96}")


def clean_mime(mime, name="") -> str:
    """A bare media type such as text/plain, or a guess from the name. Never parameters or control characters."""
    m = str(mime or "").split(";")[0].strip().lower()
    return m if MIME.fullmatch(m) else guess_mime(name)


def safe_url(s) -> str:
    """The link if it's a plain web address, otherwise "". Keeps javascript:, data: and the like out of links."""
    s = str(s or "").strip()
    if not s or any(ord(ch) < 32 for ch in s):
        return ""
    try:
        u = urlparse(s)
    except ValueError:
        return ""
    return s if u.scheme in ("http", "https") and u.netloc else ""


def plain_status(s) -> str:
    """A card status is a short label, never markup."""
    return re.sub(r"[^\w .-]", "", str(s or ""))[:40].strip()


def attachment_header(name: str) -> str:
    """Content-Disposition for a download, with an ASCII fallback and the real name for browsers that read filename*."""
    fallback = name.encode("ascii", "replace").decode().replace("?", "_").replace('"', "").replace("\\", "_") or "file"
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(name, safe='')}"


def safe_name(s: str) -> str:
    return re.sub(r"[^\w.@+-]+", "_", s).strip("_")[:120] or "file"


def parse_ts(s) -> float | None:
    if not s or isinstance(s, bool):
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


def as_int(v):
    """A whole number from a request, or None."""
    if isinstance(v, bool):
        return None
    try:
        return int(v)
    except (TypeError, ValueError, OverflowError):
        return None
