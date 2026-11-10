"""Removes secrets from anything before it is stored."""

import gzip
import io
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
    ("stripe-key", re.compile(r"\b(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{16,}|\bwhsec_[A-Za-z0-9]{24,}")),
    ("slack-webhook", re.compile(r"https://hooks\.slack\.com/(?:services|workflows|triggers)/[A-Za-z0-9/_-]{20,}")),
    ("discord-webhook", re.compile(r"https://(?:\w+\.)?discord(?:app)?\.com/api/webhooks/\d+/[A-Za-z0-9_-]{20,}")),
    ("telegram-token", re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{33,40}\b")),
    ("sendgrid-key", re.compile(r"\bSG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}")),
    ("twilio-key", re.compile(r"\bSK[0-9a-f]{32}\b")),
    ("npm-token", re.compile(r"\bnpm_[A-Za-z0-9]{36}\b")),
    ("pypi-token", re.compile(r"\bpypi-AgE[A-Za-z0-9_-]{50,}")),
    ("huggingface-token", re.compile(r"\bhf_[A-Za-z0-9]{30,}\b")),
    ("digitalocean-token", re.compile(r"\bdo[opr]_v1_[a-f0-9]{64}\b")),
    ("google-oauth", re.compile(r"\bya29\.[A-Za-z0-9_-]{20,}")),
    ("azure-key", re.compile(r"(?i)(\b(?:AccountKey|SharedAccessKey)=)([A-Za-z0-9+/=]{20,})")),
    ("basic-auth", re.compile(r"(?i)(\bbasic[ \t]+)(?!\[REDACTED)([A-Za-z0-9+/]{12,}={0,2})(?![A-Za-z0-9+/=])")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
    ("bearer", re.compile(r"(?i)(\bbearer[ \t]+)(?!\[REDACTED)([A-Za-z0-9._~+/=-]{16,})")),
    ("url-credentials", re.compile(r"(\b[a-z][a-z0-9+.-]{0,30}://[^/\s:@\"']+:)(?!\[REDACTED)([^@\s\"'#?]{3,}?)(@)(?=[^@\s]*(?:[/\s\"']|$))")),
    # key=value / key: value on the same line. Skips placeholders ($VAR, <x>, `code`, {x}), values on the next
    # line, and plain lowercase words ("hashed", "command", "certbot").
    ("assignment", re.compile(
        r"(?i)((?:password|passwd|pwd|secret|api[_-]?key|access[_-]?key|auth[_-]?token|token|client[_-]?secret)"
        r"[\"']?[ \t]*[:=][ \t]*[\"']?)(?![`$<{(%\[]|REDACTED)(?-i:(?![a-z]+(?:[\s\"',;.)`]|$)))([^\s\"',;}{`]{6,})")),
]


def known_secrets():
    vals = [TOKEN] + [setting(k) for k in ("owui_key", "notify_url", "litellm_key")] if db.conn is not None else [TOKEN]
    return [v for v in vals if v and len(v) >= 12]


CHUNK = 1024 * 1024  # long text is scrubbed in pieces, so other requests keep being answered meanwhile
MAX_EXPANDED = 256 * 1024 * 1024  # a compressed upload may expand to this much text, no more


def pieces(text, size=None):
    """Long text in pieces of about `size` (CHUNK by default), cut at line ends and never inside a private key block."""
    size, start, n = size or CHUNK, 0, len(text)
    while start < n:
        end = text.find("\n", start + size)
        end = n if end < 0 else end + 1
        begin = text.rfind("-----BEGIN ", start, end)
        if begin >= 0 and "PRIVATE KEY" in text[begin:begin + 80] and text.find("-----END ", begin, end) < 0:
            close = text.find("-----END ", end, end + 65536)
            stop = text.find("\n", close if close >= 0 else end + 65536)
            end = n if stop < 0 else stop + 1
        yield text[start:end]
        start = end


def redact(text: str) -> str:
    """Scrub credentials before anything is stored. Keeps the surrounding text so logs stay readable."""
    if not text:
        return text
    if len(text) > CHUNK:
        return "".join(redact_piece(p) for p in pieces(text))
    return redact_piece(text)


def redact_clip(text, n: int) -> str:
    """Scrub a field that's stored at most n characters long, without scrubbing what would be cut anyway."""
    return redact(str(text or "")[:n + 200])[:n]


def redact_piece(text: str) -> str:
    for v in known_secrets():
        text = text.replace(v, "[REDACTED:hub-secret]")
    for name, rx in SECRET_PATTERNS:
        if name in ("bearer", "url-credentials", "assignment", "azure-key", "basic-auth"):
            text = rx.sub(lambda m: m.group(1) + f"[REDACTED:{name}]" + (m.group(3) if name == "url-credentials" else ""), text)
        else:
            text = rx.sub(f"[REDACTED:{name}]", text)
    return text


def redact_bytes(data: bytes, mime: str, name: str) -> bytes:
    """Redact text documents and gzip-compressed transcripts; other binaries pass through."""
    if name.endswith(".gz") or mime == "application/gzip":
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(data)) as g:
                raw = g.read(MAX_EXPANDED + 1)
        except (OSError, EOFError):
            return data
        if len(raw) > MAX_EXPANDED:
            raise ValueError(f"{name} expands to more than {MAX_EXPANDED // 1024 // 1024} MB, so the hub can't check it for secrets")
        clean = redact(raw.decode("utf-8", "surrogateescape")).encode("utf-8", "surrogateescape")
        return data if clean == raw else gzip.compress(clean, compresslevel=6)
    if is_text(mime, name):
        txt = data.decode("utf-8", "surrogateescape")
        clean = redact(txt)
        return data if clean == txt else clean.encode("utf-8", "surrogateescape")
    return data
