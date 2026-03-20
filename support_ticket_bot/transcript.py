from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import timezone
from pathlib import Path

import discord

from .utils import html_escape

TRANSCRIPTS_DIR = Path(__file__).resolve().parent / "dashboard" / "transcripts"


@dataclass(slots=True)
class TranscriptBundle:
    txt_file: discord.File | None
    html_file: discord.File | None
    transcript_text: str
    transcript_html: str | None


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


def _embed_text(embed: discord.Embed) -> str:
    parts: list[str] = []

    if embed.title:
        parts.append(f"Title: {embed.title}")
    if embed.description:
        parts.append(f"Description: {embed.description}")
    if embed.author and embed.author.name:
        parts.append(f"Author: {embed.author.name}")
    for field in embed.fields:
        parts.append(f"{field.name}: {field.value}")
    if embed.footer and embed.footer.text:
        parts.append(f"Footer: {embed.footer.text}")

    return "\n".join(parts)


def _embed_html(embed: discord.Embed) -> str:
    parts: list[str] = []

    if embed.title:
        parts.append(f"<div class='embed-title'>{html_escape(embed.title)}</div>")
    if embed.description:
        parts.append(f"<div class='embed-description'>{html_escape(embed.description)}</div>")
    if embed.author and embed.author.name:
        parts.append(f"<div class='embed-author'>Author: {html_escape(embed.author.name)}</div>")
    if embed.fields:
        field_items = []
        for field in embed.fields:
            field_items.append(
                "<div class='embed-field'>"
                f"<div class='embed-field-name'>{html_escape(field.name)}</div>"
                f"<div class='embed-field-value'>{html_escape(field.value)}</div>"
                "</div>"
            )
        parts.append("<div class='embed-fields'>" + "".join(field_items) + "</div>")
    if embed.footer and embed.footer.text:
        parts.append(f"<div class='embed-footer'>{html_escape(embed.footer.text)}</div>")

    if not parts:
        parts.append("<div class='embed-note'>Embed attached</div>")

    return "<div class='embed-block'>" + "".join(parts) + "</div>"


def _message_block(message: discord.Message) -> str:
    created = message.created_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    author = html_escape(str(message.author))
    content = html_escape(_message_content(message))
    avatar = message.author.display_avatar.url

    attachment_html = ""
    if message.attachments:
        items = []
        for att in message.attachments:
            safe_name = html_escape(att.filename)
            safe_url = html_escape(att.url)
            items.append(f'<li><a href="{safe_url}" target="_blank">{safe_name}</a></li>')
        attachment_html = "<div class='attachments'><strong>Attachments</strong><ul>" + "".join(items) + "</ul></div>"

    embed_html = "".join(_embed_html(embed) for embed in message.embeds)

    return f"""
    <div class='message'>
        <img class='avatar' src='{html_escape(avatar)}' alt='avatar'>
        <div class='bubble'>
            <div class='meta'>
                <span class='author'>{author}</span>
                <span class='time'>{created}</span>
            </div>
            <pre class='content'>{content or '[no text content]'}</pre>
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
        content = _message_content(message)
        if message.attachments:
            for att in message.attachments:
                content += f" [Attachment: {att.url}]"
        if message.embeds:
            embed_text = "\n".join(filter(None, (_embed_text(embed) for embed in message.embeds)))
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
<title>Transcript - {html_escape(thread.name)}</title>
<style>
body {{ font-family: Arial, sans-serif; background: #1e1f22; color: #f2f3f5; margin: 0; padding: 24px; }}
.wrapper {{ max-width: 1000px; margin: 0 auto; }}
.header {{ background: #2b2d31; border-radius: 12px; padding: 20px; margin-bottom: 20px; }}
.message {{ display: flex; gap: 14px; margin-bottom: 16px; }}
.avatar {{ width: 44px; height: 44px; border-radius: 50%; flex: 0 0 44px; }}
.bubble {{ background: #2b2d31; border-radius: 12px; padding: 12px 14px; width: 100%; }}
.author {{ font-weight: 700; margin-right: 12px; }}
.time {{ color: #949ba4; font-size: 0.9rem; }}
.content {{ white-space: pre-wrap; word-wrap: break-word; margin: 0; font-family: inherit; }}
a {{ color: #7cb7ff; }}
.embed-block {{ margin-top: 10px; border-left: 4px solid #5865f2; background: #23272f; padding: 10px 12px; border-radius: 8px; }}
.embed-title {{ font-weight: 700; margin-bottom: 6px; }}
.embed-description {{ white-space: pre-wrap; margin-bottom: 8px; }}
.embed-author, .embed-footer {{ color: #c9d1d9; font-size: 0.95rem; margin-top: 6px; }}
.embed-fields {{ display: grid; gap: 8px; margin-top: 8px; }}
.embed-field-name {{ font-weight: 700; margin-bottom: 2px; }}
.embed-field-value {{ white-space: pre-wrap; }}
.embed-note {{ margin-top: 8px; color: #f0b232; }}
</style>
</head>
<body>
<div class="wrapper">
    <div class="header">
        <h1>{html_escape(thread.name)}</h1>
        <p>Guild ID: {thread.guild.id}</p>
        <p>Thread ID: {thread.id}</p>
        <p>Archived at export: {'Yes' if thread.archived else 'No'}</p>
    </div>
    {''.join(html_messages) if html_messages else '<p>No messages in thread.</p>'}
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
