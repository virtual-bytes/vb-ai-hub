"""Removes secrets from anything before it is stored."""

import gzip
import re

from . import db
from .config import TOKEN
from .util import is_text
from .db import setting


SECRET_PATTERNS = [
    # A real key block: header followed by base64 lines (END line optional). A header merely mentioned in prose stays.
    ("private-key", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----(?:[ \t]*\r?\n[A-Za-z0-9+/=]{16,})+(?:[ \t]*\r?\n-----END [A-Z0-9 ]*PRIVATE KEY-----)?")),
    ("anthropic-key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}")),
    ("api-key", re.compile(r"\bsk-(?:proj-|live-|test-)?[A-Za-z0-9_-]{20,}")),
    ("github-token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})")),
    ("gitlab-token", re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}")),
    ("slack-token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("aws-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("google-key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
    ("bearer", re.compile(r"(?i)(\bbearer[ \t]+)(?!\[REDACTED)([A-Za-z0-9._~+/=-]{16,})")),
    ("url-credentials", re.compile(r"(\b[a-z][a-z0-9+.-]*://[^/\s:@\"']+:)(?!\[REDACTED)([^@\s/\"']{3,})(@)")),
    # key=value / key: value on the same line. Skips placeholders ($VAR, <x>, `code`, {x}), values on the next
    # line, and plain lowercase words ("hashed", "command", "certbot").
    ("assignment", re.compile(
        r"(?i)((?:password|passwd|pwd|secret|api[_-]?key|access[_-]?key|auth[_-]?token|token|client[_-]?secret)"
        r"[\"']?[ \t]*[:=][ \t]*[\"']?)(?![`$<{(%\[]|REDACTED)(?-i:(?![a-z]+(?:[\s\"',;.)`]|$)))([^\s\"',;}{`]{6,})")),
]


def known_secrets():
    vals = [TOKEN] + [setting(k) for k in ("owui_key", "notify_url", "litellm_key")] if db.conn is not None else [TOKEN]
    return [v for v in vals if v and len(v) >= 12]


def redact(text: str) -> str:
    """Scrub credentials before anything is stored. Keeps the surrounding text so logs stay readable."""
    if not text:
        return text
    for v in known_secrets():
        text = text.replace(v, "[REDACTED:hub-secret]")
    for name, rx in SECRET_PATTERNS:
        if name in ("bearer", "url-credentials", "assignment"):
            text = rx.sub(lambda m: m.group(1) + f"[REDACTED:{name}]" + (m.group(3) if name == "url-credentials" else ""), text)
        else:
            text = rx.sub(f"[REDACTED:{name}]", text)
    return text


def redact_bytes(data: bytes, mime: str, name: str) -> bytes:
    """Redact text documents and gzip-compressed transcripts; other binaries pass through."""
    if name.endswith(".gz") or mime == "application/gzip":
        try:
            raw = gzip.decompress(data)
        except OSError:
            return data
        clean = redact(raw.decode("utf-8", "surrogateescape")).encode("utf-8", "surrogateescape")
        return data if clean == raw else gzip.compress(clean, compresslevel=6)
    if is_text(mime, name):
        txt = data.decode("utf-8", "surrogateescape")
        clean = redact(txt)
        return data if clean == txt else clean.encode("utf-8", "surrogateescape")
    return data
