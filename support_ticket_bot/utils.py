from __future__ import annotations

import hashlib
import html
import re
from datetime import datetime, timezone

DEFAULT_MESSAGE_TEMPLATES = {
    "panel_title": "Support Tickets",
    "panel_description": "Press **Create Ticket** below, then choose which server the ticket is for.",
    "thread_embed_title": "Ticket Created",
    "thread_embed_description": (
        "**Server:** {server_label}\n"
        "**Opened by:** {user_mention}\n"
        "**Ticket ID:** `{thread_id}`\n\n"
        "Use the button below to close this ticket when it is resolved."
    ),
    "thread_tags_title": "Quick Tags",
    "thread_tags_description": "Click a tag button to apply or remove it from this ticket.",
}


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


class _SafeFormatDict(dict[str, str]):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def render_template(template: str, **values: object) -> str:
    safe_values = _SafeFormatDict({key: str(value) for key, value in values.items()})
    try:
        return template.format_map(safe_values)
    except ValueError:
        return template
