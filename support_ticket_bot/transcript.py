from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import re
from typing import Any

import discord

from .utils import html_escape

TRANSCRIPTS_DIR = Path(__file__).resolve().parent / "dashboard" / "transcripts"


@dataclass(slots=True)
class TranscriptBundle:
    txt_file: discord.File | None
    html_file: discord.File | None
    transcript_text: str
    transcript_html: str | None


MENTION_PATTERN = re.compile(r"<(@[!&]?|#)(\d+)>")
CODE_BLOCK_PATTERN = re.compile(r"```(?:[a-zA-Z0-9_+-]+\n)?([\s\S]*?)```")
INLINE_CODE_PATTERN = re.compile(r"`([^`\n]+)`")


class MentionResolver:
    def __init__(self, guild: discord.Guild | None):
        self.guild = guild
        self.user_cache: dict[int, str] = {}
        self.role_cache: dict[int, str] = {}
        self.channel_cache: dict[int, str] = {}

    async def resolve_label(self, mention_type: str, entity_id: int) -> str:
        if mention_type in {"@", "@!"}:
            if entity_id in self.user_cache:
                return self.user_cache[entity_id]

            member = self.guild.get_member(entity_id) if self.guild else None
            if member is None and self.guild is not None:
                try:
                    member = await self.guild.fetch_member(entity_id)
                except (discord.Forbidden, discord.HTTPException, discord.NotFound):
                    member = None

            label = f"@{member.display_name}" if member is not None else f"@user-{entity_id}"
            self.user_cache[entity_id] = label
            return label

        if mention_type == "@&":
            if entity_id in self.role_cache:
                return self.role_cache[entity_id]

            role = self.guild.get_role(entity_id) if self.guild else None
            label = f"@{role.name}" if role is not None else f"@role-{entity_id}"
            self.role_cache[entity_id] = label
            return label

        if mention_type == "#":
            if entity_id in self.channel_cache:
                return self.channel_cache[entity_id]

            channel = None
            if self.guild is not None:
                channel_getter = getattr(self.guild, "get_channel_or_thread", None)
                channel = channel_getter(entity_id) if callable(channel_getter) else self.guild.get_channel(entity_id)
            label = f"#{channel.name}" if channel is not None else f"#channel-{entity_id}"
            self.channel_cache[entity_id] = label
            return label

        return str(entity_id)


async def _replace_discord_mentions(value: str, resolver: MentionResolver) -> str:
    parts: list[str] = []
    last_index = 0

    for match in MENTION_PATTERN.finditer(value):
        parts.append(value[last_index:match.start()])
        mention_type = match.group(1)
        entity_id = int(match.group(2))
        parts.append(await resolver.resolve_label(mention_type, entity_id))
        last_index = match.end()

    parts.append(value[last_index:])
    return "".join(parts)


async def _strip_discord_markup(value: str, resolver: MentionResolver) -> str:
    text = await _replace_discord_mentions(value, resolver)
    text = CODE_BLOCK_PATTERN.sub(lambda match: match.group(1).strip("\n"), text)
    text = INLINE_CODE_PATTERN.sub(lambda match: match.group(1), text)
    replacements = [
        (re.compile(r"\*\*(.+?)\*\*", re.S), r"\1"),
        (re.compile(r"__(.+?)__", re.S), r"\1"),
        (re.compile(r"\*(.+?)\*", re.S), r"\1"),
        (re.compile(r"_(.+?)_", re.S), r"\1"),
        (re.compile(r"~~(.+?)~~", re.S), r"\1"),
    ]
    for pattern, replacement in replacements:
        text = pattern.sub(replacement, text)
    return text


async def _render_discord_markup(value: str, resolver: MentionResolver) -> str:
    text = await _replace_discord_mentions(value, resolver)
    placeholders: dict[str, str] = {}

    def _store_placeholder(prefix: str, rendered_html: str) -> str:
        token = f"@@{prefix}{len(placeholders)}@@"
        placeholders[token] = rendered_html
        return token

    def _code_block(match: re.Match[str]) -> str:
        code_html = f"<pre class='code-block'>{html_escape(match.group(1).strip())}</pre>"
        return _store_placeholder("CODEBLOCK", code_html)

    def _inline_code(match: re.Match[str]) -> str:
        code_html = f"<code class='inline-code'>{html_escape(match.group(1))}</code>"
        return _store_placeholder("INLINECODE", code_html)

    text = CODE_BLOCK_PATTERN.sub(_code_block, text)
    text = INLINE_CODE_PATTERN.sub(_inline_code, text)
    text = html_escape(text)

    rules = [
        (re.compile(r"\*\*(.+?)\*\*", re.S), r"<strong>\1</strong>"),
        (re.compile(r"__(.+?)__", re.S), r"<span class='underline'>\1</span>"),
        (re.compile(r"~~(.+?)~~", re.S), r"<span class='strikethrough'>\1</span>"),
        (re.compile(r"\*(.+?)\*", re.S), r"<em>\1</em>"),
        (re.compile(r"_(.+?)_", re.S), r"<em>\1</em>"),
    ]
    for pattern, replacement in rules:
        text = pattern.sub(replacement, text)

    text = text.replace("\n", "<br>")
    for token, rendered_html in placeholders.items():
        text = text.replace(token, rendered_html)
    return text


def _message_content(message: discord.Message) -> str:
    content = message.content or getattr(message, "clean_content", "") or ""
    if content:
        return content

    system_content = getattr(message, "system_content", "") or ""
    if system_content:
        return system_content

    reference = getattr(message, "reference", None)
    resolved = getattr(reference, "resolved", None)
    if isinstance(resolved, discord.Message):
        referenced_content = resolved.content or getattr(resolved, "clean_content", "") or ""
        if referenced_content:
            return f"[Thread starter] {referenced_content}"

    return ""


async def _embed_text(embed: discord.Embed, resolver: MentionResolver) -> str:
    parts: list[str] = []

    if embed.title:
        parts.append(f"Title: {await _strip_discord_markup(embed.title, resolver)}")
    if embed.description:
        parts.append(f"Description: {await _strip_discord_markup(embed.description, resolver)}")
    if embed.author and embed.author.name:
        parts.append(f"Author: {await _strip_discord_markup(embed.author.name, resolver)}")
    for field in embed.fields:
        parts.append(
            f"{await _strip_discord_markup(field.name, resolver)}: {await _strip_discord_markup(field.value, resolver)}"
        )
    if embed.footer and embed.footer.text:
        parts.append(f"Footer: {await _strip_discord_markup(embed.footer.text, resolver)}")

    return "\n".join(parts)


async def _embed_html(embed: discord.Embed, resolver: MentionResolver) -> str:
    parts: list[str] = []

    if embed.title:
        parts.append(f"<div class='embed-title'>{await _render_discord_markup(embed.title, resolver)}</div>")
    if embed.description:
        parts.append(f"<div class='embed-description'>{await _render_discord_markup(embed.description, resolver)}</div>")
    if embed.author and embed.author.name:
        parts.append(f"<div class='embed-author'>Author: {await _render_discord_markup(embed.author.name, resolver)}</div>")
    if embed.fields:
        field_items = []
        for field in embed.fields:
            field_items.append(
                "<div class='embed-field'>"
                f"<div class='embed-field-name'>{await _render_discord_markup(field.name, resolver)}</div>"
                f"<div class='embed-field-value'>{await _render_discord_markup(field.value, resolver)}</div>"
                "</div>"
            )
        parts.append("<div class='embed-fields'>" + "".join(field_items) + "</div>")
    if embed.footer and embed.footer.text:
        parts.append(f"<div class='embed-footer'>{await _render_discord_markup(embed.footer.text, resolver)}</div>")

    if not parts:
        parts.append("<div class='embed-note'>Embed attached</div>")

    return "<div class='embed-block'>" + "".join(parts) + "</div>"


def _is_image_attachment(attachment: discord.Attachment) -> bool:
    content_type = (attachment.content_type or "").lower()
    if content_type.startswith("image/"):
        return True
    filename = attachment.filename.lower()
    return filename.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"))


def _attachment_html(attachment: discord.Attachment) -> str:
    safe_name = html_escape(attachment.filename)
    safe_url = html_escape(attachment.url)
    if _is_image_attachment(attachment):
        return (
            "<div class='attachment attachment-image-wrap'>"
            f"<a href='{safe_url}' target='_blank' rel='noopener noreferrer'>"
            f"<img class='attachment-image' src='{safe_url}' alt='{safe_name}'>"
            "</a>"
            f"<div class='attachment-caption'><a href='{safe_url}' target='_blank' rel='noopener noreferrer'>{safe_name}</a></div>"
            "</div>"
        )
    return (
        "<div class='attachment attachment-file'>"
        f"<a href='{safe_url}' target='_blank' rel='noopener noreferrer'>{safe_name}</a>"
        "</div>"
    )


async def _message_block(message: discord.Message, resolver: MentionResolver) -> str:
    created = message.created_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    author = html_escape(str(message.author))
    content = await _render_discord_markup(_message_content(message), resolver)
    avatar = message.author.display_avatar.url

    attachment_html = ""
    if message.attachments:
        items = "".join(_attachment_html(att) for att in message.attachments)
        attachment_html = "<div class='attachments'><strong>Attachments</strong>" + items + "</div>"

    embed_html = "".join([await _embed_html(embed, resolver) for embed in message.embeds])

    return f"""
    <div class='message'>
        <img class='avatar' src='{html_escape(avatar)}' alt='avatar'>
        <div class='bubble'>
            <div class='meta'>
                <span class='author'>{author}</span>
                <span class='time'>{created}</span>
            </div>
            <div class='content'>{content or '[no text content]'}</div>
            {attachment_html}
            {embed_html}
        </div>
    </div>
    """


async def generate_transcripts(
    thread: discord.Thread,
    *,
    include_txt: bool = True,
    include_html: bool = True,
) -> TranscriptBundle:
    lines: list[str] = []
    html_messages: list[str] = []
    resolver = MentionResolver(thread.guild)

    async for message in thread.history(limit=None, oldest_first=True):
        created = message.created_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        content = await _strip_discord_markup(_message_content(message), resolver)
        if message.attachments:
            for att in message.attachments:
                content += f" [Attachment: {att.url}]"
        if message.embeds:
            embed_parts: list[str] = []
            for embed in message.embeds:
                embed_text = await _embed_text(embed, resolver)
                if embed_text:
                    embed_parts.append(embed_text)
            embed_text = "\n".join(embed_parts)
            if embed_text:
                content = f"{content}\n{embed_text}".strip()
            else:
                content += " [Embed]"
        lines.append(f"[{created}] {message.author}: {content}")
        html_messages.append(await _message_block(message, resolver))

    transcript_text = "\n".join(lines) if lines else "No messages in thread."

    txt_file = None
    html_file = None
    transcript_html = None

    if include_txt:
        txt_buffer = io.BytesIO(transcript_text.encode("utf-8"))
        txt_file = discord.File(txt_buffer, filename=f"{thread.name}.txt")

    if include_html:
        exported_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        html_doc = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Transcript - {html_escape(thread.name)}</title>
<style>
* {{ box-sizing: border-box; }}
body {{ font-family: Arial, sans-serif; margin: 0; background: #111827; color: #f3f4f6; }}
a {{ color: #93c5fd; text-decoration: none; }}
.topbar {{ display:flex; justify-content:space-between; align-items:center; padding:16px 24px; background:#1f2937; }}
.topbar-left {{ display:flex; align-items:center; gap:24px; }}
.topnav {{ display:flex; gap:16px; }}
.topnav a {{ color:#dbeafe; }}
.page {{ max-width: 1200px; margin: 0 auto; padding: 24px; }}
.page-header {{ margin-bottom: 16px; }}
.page-header h1 {{ margin: 0; font-size: 3rem; line-height: 1.1; }}
.meta-card {{ background: #1f2937; border-radius: 12px; padding: 20px; margin-bottom: 16px; border: 1px solid #374151; }}
.meta-card p {{ margin: 6px 0; color: #9ca3af; }}
.messages {{ display: grid; gap: 16px; }}
.message {{ display: flex; gap: 14px; align-items: flex-start; }}
.avatar {{ width: 44px; height: 44px; border-radius: 50%; flex: 0 0 44px; }}
.bubble {{ background: #1f2937; border-radius: 12px; padding: 14px 16px; width: 100%; border: 1px solid #374151; }}
.meta {{ margin-bottom: 10px; }}
.author {{ font-weight: 700; margin-right: 12px; }}
.time {{ color: #9ca3af; font-size: 0.9rem; }}
.content {{ margin: 0; color: #f3f4f6; line-height: 1.55; word-break: break-word; }}
.attachments {{ margin-top: 12px; padding-top: 12px; border-top: 1px solid #374151; }}
.attachments strong {{ display: block; margin-bottom: 8px; }}
.attachment + .attachment {{ margin-top: 10px; }}
.attachment-image-wrap {{ display: grid; gap: 8px; }}
.attachment-image {{ display: block; max-width: 100%; max-height: 520px; border-radius: 10px; border: 1px solid #374151; background: #0f172a; object-fit: contain; }}
.attachment-caption {{ color: #9ca3af; font-size: 0.95rem; }}
.attachment-file a {{ display: inline-block; padding: 10px 12px; border-radius: 8px; background: #0f172a; border: 1px solid #374151; }}
.embed-block {{ margin-top: 12px; border: 1px solid #374151; border-left: 4px solid #2563eb; background: #111827; padding: 12px 14px; border-radius: 10px; }}
.embed-title {{ font-weight: 700; margin-bottom: 6px; }}
.embed-description {{ margin-bottom: 8px; line-height: 1.5; }}
.embed-author, .embed-footer {{ color: #9ca3af; font-size: 0.95rem; margin-top: 6px; }}
.embed-fields {{ display: grid; gap: 8px; margin-top: 8px; }}
.embed-field-name {{ font-weight: 700; margin-bottom: 2px; }}
.embed-field-value {{ line-height: 1.5; }}
.embed-note {{ margin-top: 8px; color: #fbbf24; }}
.inline-code {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; background: #0f172a; border: 1px solid #374151; border-radius: 6px; padding: 1px 6px; color: #e5e7eb; }}
.code-block {{ margin: 10px 0 0; padding: 12px; border-radius: 10px; background: #0f172a; border: 1px solid #374151; color: #e5e7eb; overflow-x: auto; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; white-space: pre-wrap; }}
.underline {{ text-decoration: underline; }}
.strikethrough {{ text-decoration: line-through; }}
</style>
</head>
<body>
<div class="topbar">
    <div class="topbar-left">
        <div><strong>Discord Ticket Transcript</strong></div>
        <nav class="topnav">
            <a href="#">Transcript</a>
        </nav>
    </div>
</div>
<div class="page">
    <div class="page-header">
        <h1>{html_escape(thread.name)}</h1>
    </div>
    <div class="meta-card">
        <p>Guild ID: {thread.guild.id}</p>
        <p>Thread ID: {thread.id}</p>
        <p>Archived at export: {'Yes' if thread.archived else 'No'}</p>
        <p>Exported at: {exported_at}</p>
    </div>
    <div class="messages">
        {''.join(html_messages) if html_messages else '<div class="bubble">No messages in thread.</div>'}
    </div>
</div>
</body>
</html>
"""
        transcript_html = html_doc
        html_buffer = io.BytesIO(html_doc.encode("utf-8"))
        html_file = discord.File(html_buffer, filename=f"{thread.name}.html")

    return TranscriptBundle(
        txt_file=txt_file,
        html_file=html_file,
        transcript_text=transcript_text,
        transcript_html=transcript_html,
    )


def store_html_transcript(thread_id: int, html_content: str) -> Path:
    TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
    transcript_path = TRANSCRIPTS_DIR / f"{thread_id}.html"
    transcript_path.write_text(html_content, encoding="utf-8")
    return transcript_path
