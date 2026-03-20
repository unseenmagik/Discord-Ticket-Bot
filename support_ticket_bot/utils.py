from __future__ import annotations

import hashlib
import html
import re
from datetime import datetime, timezone


def clean_slug(text: str, max_length: int = 80) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9\- ]+", "", text)
    text = re.sub(r"\s+", "-", text)
    return (text[:max_length] or "ticket").strip("-")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def html_escape(value: str) -> str:
    return html.escape(value, quote=True)


def hash_password(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
