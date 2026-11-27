#!/usr/bin/env python3
"""Shared atomic file-writing, SQLite lifecycle, and credential-redaction utilities."""

from __future__ import annotations

import contextlib
import os
import re
import sqlite3
import threading
from pathlib import Path
from typing import Iterator

# Matches URI credentials anywhere in text (rtsp://user:pass@host, http://u:p@host, etc.)
# Excludes query-string '@' by forbidding '/', '?', '#', whitespace before '@'.
_URI_USERINFO_ANY_RE = re.compile(
    r"(\b[a-zA-Z][a-zA-Z0-9+.-]*://)([^/@\s?#\"'<>]+)@"
)
_TG_BOT_TOKEN_RE = re.compile(
    r"(https?://api\.telegram\.org/bot)([^/\s\"'<>]+)"
)
_JSON_SECRET_KV_RE = re.compile(
    r'("(?:password|currentPassword|access_secret|telegram_bot_token)"\s*:\s*")([^"]*)(")',
    re.IGNORECASE,
)
_XML_PASSWORD_RE = re.compile(
    r"(<(?:\w+:)?Password\b[^>]*>)(.*?)(</(?:\w+:)?Password>)",
    re.IGNORECASE | re.DOTALL,
)


def write_private(path: Path, text: str) -> None:
    """Write a file atomically with owner-only permissions (0o600 on POSIX)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(str(tmp), 0o600)
        os.replace(str(tmp), str(path))
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def redact_url(url: str) -> str:
    """rtsp://user:pass@host/... -> rtsp://***@host/..."""
    return _URI_USERINFO_ANY_RE.sub(r"\1***@", url or "")


def redact_sensitive_text(text: str) -> str:
    """Scrub embedded stream credentials, Telegram bot tokens, and secret fields from text."""
    if not text:
        return ""
    out = _URI_USERINFO_ANY_RE.sub(r"\1***@", str(text))
    out = _TG_BOT_TOKEN_RE.sub(r"\1***", out)
    out = _JSON_SECRET_KV_RE.sub(r"\1***\3", out)
    out = _XML_PASSWORD_RE.sub(r"\1***\3", out)
    return out


@contextlib.contextmanager
def closing_sqlite(path: Path, *, timeout: float = 10.0) -> Iterator[sqlite3.Connection]:
    """Open SQLite connection with Row factory, commit/rollback, and always close."""
    conn = sqlite3.connect(str(path), timeout=timeout)
    conn.row_factory = sqlite3.Row
    try:
        with conn:
            yield conn
    finally:
        conn.close()
