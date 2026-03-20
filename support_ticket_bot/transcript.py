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


def _message_block(message: discord.Message) -> str:
    created = message.created_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    author = html_escape(str(message.author))
    content = html_escape(message.content or "")
    avatar = message.author.display_avatar.url

    attachment_html = ""
    if message.attachments:
        items = []
        for att in message.attachments:
            safe_name = html_escape(att.filename)
            safe_url = html_escape(att.url)
            items.append(f'<li><a href="{safe_url}" target="_blank">{safe_name}</a></li>')
        attachment_html = "<div class='attachments'><strong>Attachments</strong><ul>" + "".join(items) + "</ul></div>"

    embed_note = "<div class='embed-note'>Embed attached</div>" if message.embeds else ""

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
            {embed_note}
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
        content = message.content or ""
        if message.attachments:
            for att in message.attachments:
                content += f" [Attachment: {att.url}]"
        if message.embeds:
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
