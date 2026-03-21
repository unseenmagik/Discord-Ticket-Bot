from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import timezone
from pathlib import Path
import re

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


def _resolve_mention_label(guild: discord.Guild | None, mention_type: str, entity_id: int) -> str:
    if mention_type in {"@", "@!"}:
        member = guild.get_member(entity_id) if guild else None
        if member is not None:
            return f"@{member.display_name}"
        return f"@user-{entity_id}"

    if mention_type == "@&":
        role = guild.get_role(entity_id) if guild else None
        if role is not None:
            return f"@{role.name}"
        return f"@role-{entity_id}"

    if mention_type == "#":
        if guild is not None:
            channel_getter = getattr(guild, "get_channel_or_thread", None)
            channel = channel_getter(entity_id) if callable(channel_getter) else guild.get_channel(entity_id)
            if channel is not None:
                return f"#{channel.name}"
        return f"#channel-{entity_id}"

    return str(entity_id)


def _replace_discord_mentions(value: str, guild: discord.Guild | None) -> str:
    def _replacement(match: re.Match[str]) -> str:
        mention_type = match.group(1)
        entity_id = int(match.group(2))
        return _resolve_mention_label(guild, mention_type, entity_id)

    return MENTION_PATTERN.sub(_replacement, value)


def _strip_discord_markup(value: str, guild: discord.Guild | None) -> str:
    text = _replace_discord_mentions(value, guild)
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


def _render_discord_markup(value: str, guild: discord.Guild | None) -> str:
    text = _replace_discord_mentions(value, guild)
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
    content = getattr(message, "clean_content", "") or message.content or ""
    if content:
        return content

    system_content = getattr(message, "system_content", "") or ""
    if system_content:
        return system_content

    reference = getattr(message, "reference", None)
    resolved = getattr(reference, "resolved", None)
    if isinstance(resolved, discord.Message):
        referenced_content = getattr(resolved, "clean_content", "") or resolved.content or ""
        if referenced_content:
            return f"[Thread starter] {referenced_content}"

    return ""


def _embed_text(embed: discord.Embed, guild: discord.Guild | None) -> str:
    parts: list[str] = []

    if embed.title:
        parts.append(f"Title: {_strip_discord_markup(embed.title, guild)}")
    if embed.description:
        parts.append(f"Description: {_strip_discord_markup(embed.description, guild)}")
    if embed.author and embed.author.name:
        parts.append(f"Author: {_strip_discord_markup(embed.author.name, guild)}")
    for field in embed.fields:
        parts.append(
            f"{_strip_discord_markup(field.name, guild)}: {_strip_discord_markup(field.value, guild)}"
        )
    if embed.footer and embed.footer.text:
        parts.append(f"Footer: {_strip_discord_markup(embed.footer.text, guild)}")

    return "\n".join(parts)


def _embed_html(embed: discord.Embed, guild: discord.Guild | None) -> str:
    parts: list[str] = []

    if embed.title:
        parts.append(f"<div class='embed-title'>{_render_discord_markup(embed.title, guild)}</div>")
    if embed.description:
        parts.append(f"<div class='embed-description'>{_render_discord_markup(embed.description, guild)}</div>")
    if embed.author and embed.author.name:
        parts.append(f"<div class='embed-author'>Author: {_render_discord_markup(embed.author.name, guild)}</div>")
    if embed.fields:
        field_items = []
        for field in embed.fields:
            field_items.append(
                "<div class='embed-field'>"
                f"<div class='embed-field-name'>{_render_discord_markup(field.name, guild)}</div>"
                f"<div class='embed-field-value'>{_render_discord_markup(field.value, guild)}</div>"
                "</div>"
            )
        parts.append("<div class='embed-fields'>" + "".join(field_items) + "</div>")
    if embed.footer and embed.footer.text:
        parts.append(f"<div class='embed-footer'>{_render_discord_markup(embed.footer.text, guild)}</div>")

    if not parts:
        parts.append("<div class='embed-note'>Embed attached</div>")

    return "<div class='embed-block'>" + "".join(parts) + "</div>"


def _message_block(message: discord.Message) -> str:
    created = message.created_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    author = html_escape(str(message.author))
    content = _render_discord_markup(_message_content(message), message.guild)
    avatar = message.author.display_avatar.url

    attachment_html = ""
    if message.attachments:
        items = []
        for att in message.attachments:
            safe_name = html_escape(att.filename)
            safe_url = html_escape(att.url)
            items.append(f'<li><a href="{safe_url}" target="_blank">{safe_name}</a></li>')
        attachment_html = "<div class='attachments'><strong>Attachments</strong><ul>" + "".join(items) + "</ul></div>"

    embed_html = "".join(_embed_html(embed, message.guild) for embed in message.embeds)

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

    async for message in thread.history(limit=None, oldest_first=True):
        created = message.created_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        content = _strip_discord_markup(_message_content(message), message.guild)
        if message.attachments:
            for att in message.attachments:
                content += f" [Attachment: {att.url}]"
        if message.embeds:
            embed_text = "\n".join(filter(None, (_embed_text(embed, message.guild) for embed in message.embeds)))
            if embed_text:
                content = f"{content}\n{embed_text}".strip()
            else:
                content += " [Embed]"
        lines.append(f"[{created}] {message.author}: {content}")
        html_messages.append(_message_block(message))

    transcript_text = "\n".join(lines) if lines else "No messages in thread."

    txt_file = None
    html_file = None
    transcript_html = None

    if include_txt:
        txt_buffer = io.BytesIO(transcript_text.encode("utf-8"))
        txt_file = discord.File(txt_buffer, filename=f"{thread.name}.txt")

    if include_html:
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
.page {{ max-width: 1200px; margin: 0 auto; padding: 24px; }}
.header {{ background: #1f2937; border-radius: 12px; padding: 20px; margin-bottom: 16px; border: 1px solid #374151; }}
.header h1 {{ margin: 0 0 10px; }}
.header p {{ margin: 6px 0; color: #9ca3af; }}
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
.attachments ul {{ margin: 0; padding-left: 20px; }}
.attachments li + li {{ margin-top: 6px; }}
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
<div class="page">
    <div class="header">
        <h1>{html_escape(thread.name)}</h1>
        <p>Guild ID: {thread.guild.id}</p>
        <p>Thread ID: {thread.id}</p>
        <p>Archived at export: {'Yes' if thread.archived else 'No'}</p>
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
