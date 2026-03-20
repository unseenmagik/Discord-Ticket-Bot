from __future__ import annotations

import asyncio

import discord
from discord.ext import commands

from support_ticket_bot.config import BotSettings, load_settings
from support_ticket_bot.db import TicketDatabase
from support_ticket_bot.logging_setup import setup_logging


class SupportTicketBot(commands.Bot):
    def __init__(self, settings: BotSettings):
        intents = discord.Intents.default()
        intents.guilds = True
        intents.messages = True
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
        print(f"Logged in as {self.user} ({self.user.id})")


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
    asyncio.run(_run_bot(settings))
