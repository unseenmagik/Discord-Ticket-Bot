from __future__ import annotations

import asyncio
import logging

import discord
from discord.ext import commands

from support_ticket_bot.config import BotSettings, load_settings
from support_ticket_bot.db import TicketDatabase
from support_ticket_bot.logging_setup import setup_logging

log = logging.getLogger(__name__)


class SupportTicketBot(commands.Bot):
    def __init__(self, settings: BotSettings):
        intents = discord.Intents.default()
        intents.guilds = True
        intents.messages = True
        intents.message_content = settings.message_content_intent
        super().__init__(command_prefix=commands.when_mentioned, intents=intents)
        self.settings = settings
        self.db = TicketDatabase(settings)

    async def setup_hook(self) -> None:
        await self.db.connect()
        await self.load_extension("support_ticket_bot.cogs.tickets")
        guild = discord.Object(id=self.settings.guild_id)
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)

    async def close(self) -> None:
        await self.db.close()
        await super().close()

    async def on_ready(self) -> None:
        log.info("Logged in as %s (%s)", self.user, self.user.id if self.user else "unknown")


async def _run_bot(settings: BotSettings) -> None:
    bot = SupportTicketBot(settings)
    try:
        await bot.start(settings.token)
    finally:
        if not bot.is_closed():
            await bot.close()


def main() -> None:
    setup_logging()
    settings = load_settings()
    if (settings.save_txt_transcript or settings.save_html_transcript) and not settings.message_content_intent:
        log.warning(
            "Message content intent is disabled. Transcript exports will omit message text until "
            "you enable it in config.ini and in the Discord Developer Portal."
        )
    asyncio.run(_run_bot(settings))
