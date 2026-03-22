from __future__ import annotations

from typing import TYPE_CHECKING

import discord

if TYPE_CHECKING:
    from support_ticket_bot.bot_core import SupportTicketBot


def _delete_after(bot: "SupportTicketBot") -> float:
    return bot.settings.interaction_delete_after_seconds


def _visible_server_options(bot: "SupportTicketBot", interaction: discord.Interaction) -> list[discord.SelectOption]:
    if interaction.guild is None:
        return []

    member = interaction.user if isinstance(interaction.user, discord.Member) else None
    if member is None:
        return []

    options: list[discord.SelectOption] = []
    for label, channel_id in bot.settings.server_targets.items():
        channel = interaction.guild.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            continue
        permissions = channel.permissions_for(member)
        if not permissions.view_channel:
            continue
        options.append(discord.SelectOption(label=label, value=label))
    return options


class TicketPanelView(discord.ui.View):
    def __init__(self, bot: "SupportTicketBot"):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(label="Create Ticket", style=discord.ButtonStyle.primary, emoji="🎫", custom_id="ticket:create")
    async def create_ticket_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.guild is None or interaction.guild.id != self.bot.settings.guild_id:
            await interaction.response.send_message(
                "This ticket panel is not configured for this server.",
                ephemeral=True,
                delete_after=_delete_after(self.bot),
            )
            return
        options = _visible_server_options(self.bot, interaction)
        if not options:
            await interaction.response.send_message(
                "There are no ticket queues available to your account.",
                ephemeral=True,
                delete_after=_delete_after(self.bot),
            )
            return
        await interaction.response.send_message(
            "Choose which server this ticket is for:",
            view=ServerSelectView(self.bot, options),
            ephemeral=True,
            delete_after=_delete_after(self.bot),
        )


class ServerSelect(discord.ui.Select):
    def __init__(self, bot: "SupportTicketBot", options: list[discord.SelectOption]):
        self.bot = bot
        super().__init__(
            placeholder="Choose which server this ticket is for...",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="ticket:server_select",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = self.bot.get_cog("TicketsCog")
        if cog is None:
            await interaction.response.send_message(
                "Ticket system is not ready yet.",
                ephemeral=True,
                delete_after=_delete_after(self.bot),
            )
            return
        await cog.handle_ticket_creation(interaction, self.values[0])


class ServerSelectView(discord.ui.View):
    def __init__(self, bot: "SupportTicketBot", options: list[discord.SelectOption]):
        super().__init__(timeout=300)
        self.add_item(ServerSelect(bot, options))


class ThreadCloseView(discord.ui.View):
    def __init__(self, bot: "SupportTicketBot", thread_id: int):
        super().__init__(timeout=None)
        self.bot = bot
        self.thread_id = thread_id
        close_button = discord.ui.Button(
            label="Close Ticket",
            style=discord.ButtonStyle.danger,
            emoji="🔒",
            custom_id=f"ticket:close:{thread_id}",
        )
        close_button.callback = self._close_callback
        self.add_item(close_button)

    async def _close_callback(self, interaction: discord.Interaction) -> None:
        cog = self.bot.get_cog("TicketsCog")
        if cog is None:
            await interaction.response.send_message(
                "Ticket system is unavailable.",
                ephemeral=True,
                delete_after=_delete_after(self.bot),
            )
            return
        await cog.handle_close_from_thread(interaction, self.thread_id)


class ThreadReopenView(discord.ui.View):
    def __init__(self, bot: "SupportTicketBot", thread_id: int):
        super().__init__(timeout=None)
        self.bot = bot
        self.thread_id = thread_id
        reopen_button = discord.ui.Button(
            label="Reopen Ticket",
            style=discord.ButtonStyle.success,
            emoji="🔓",
            custom_id=f"ticket:thread_reopen:{thread_id}",
        )
        delete_button = discord.ui.Button(
            label="Delete Ticket",
            style=discord.ButtonStyle.danger,
            emoji="🗑️",
            custom_id=f"ticket:thread_delete:{thread_id}",
        )
        reopen_button.callback = self._reopen_callback
        delete_button.callback = self._delete_callback
        self.add_item(reopen_button)
        self.add_item(delete_button)

    async def _reopen_callback(self, interaction: discord.Interaction) -> None:
        cog = self.bot.get_cog("TicketsCog")
        if cog is None:
            await interaction.response.send_message(
                "Ticket system is unavailable.",
                ephemeral=True,
                delete_after=_delete_after(self.bot),
            )
            return
        await cog.handle_reopen_from_log(interaction, self.thread_id)

    async def _delete_callback(self, interaction: discord.Interaction) -> None:
        cog = self.bot.get_cog("TicketsCog")
        if cog is None:
            await interaction.response.send_message(
                "Ticket system is unavailable.",
                ephemeral=True,
                delete_after=_delete_after(self.bot),
            )
            return
        await cog.handle_delete_from_log(interaction, self.thread_id)


class TicketLogControlsView(discord.ui.View):
    def __init__(self, bot: "SupportTicketBot", thread_id: int):
        super().__init__(timeout=None)
        self.bot = bot
        self.thread_id = thread_id

        reopen_button = discord.ui.Button(
            label="Reopen Ticket",
            style=discord.ButtonStyle.success,
            emoji="🔓",
            custom_id=f"ticket:reopen:{thread_id}",
        )
        delete_button = discord.ui.Button(
            label="Delete Now",
            style=discord.ButtonStyle.danger,
            emoji="🗑️",
            custom_id=f"ticket:delete:{thread_id}",
        )
        reopen_button.callback = self._reopen_callback
        delete_button.callback = self._delete_callback
        self.add_item(reopen_button)
        self.add_item(delete_button)

    async def _reopen_callback(self, interaction: discord.Interaction) -> None:
        cog = self.bot.get_cog("TicketsCog")
        if cog is None:
            await interaction.response.send_message(
                "Ticket system is unavailable.",
                ephemeral=True,
                delete_after=_delete_after(self.bot),
            )
            return
        await cog.handle_reopen_from_log(interaction, self.thread_id)

    async def _delete_callback(self, interaction: discord.Interaction) -> None:
        cog = self.bot.get_cog("TicketsCog")
        if cog is None:
            await interaction.response.send_message(
                "Ticket system is unavailable.",
                ephemeral=True,
                delete_after=_delete_after(self.bot),
            )
            return
        await cog.handle_delete_from_log(interaction, self.thread_id)
