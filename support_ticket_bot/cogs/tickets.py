from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

from support_ticket_bot.transcript import generate_transcripts, store_html_transcript
from support_ticket_bot.utils import clean_slug, utc_now_iso
from support_ticket_bot.views import TicketLogControlsView, TicketPanelView, ThreadCloseView

log = logging.getLogger(__name__)


class TicketsCog(commands.Cog):
    def __init__(self, bot: "SupportTicketBot"):
        self.bot = bot
        self.cleanup_closed_threads.start()

    async def cog_load(self) -> None:
        await self.register_persistent_views()

    def cog_unload(self) -> None:
        self.cleanup_closed_threads.cancel()

    async def register_persistent_views(self) -> None:
        self.bot.add_view(TicketPanelView(self.bot))
        for ticket in await self.bot.db.list_open_tickets():
            self.bot.add_view(ThreadCloseView(self.bot, ticket["thread_id"]))
        for ticket in await self.bot.db.list_tickets_with_log_controls():
            log_message_id = ticket.get("log_message_id")
            if log_message_id:
                self.bot.add_view(TicketLogControlsView(self.bot, ticket["thread_id"]), message_id=log_message_id)

    def _embed(self, title: str, description: str) -> discord.Embed:
        return discord.Embed(
            title=title,
            description=description,
            color=self.bot.settings.embed_color,
            timestamp=datetime.now(timezone.utc),
        )

    async def _resolve_thread(self, thread_id: int) -> discord.Thread | None:
        cached = self.bot.get_channel(thread_id)
        if isinstance(cached, discord.Thread):
            return cached

        for guild in self.bot.guilds:
            thread = guild.get_thread(thread_id)
            if thread is not None:
                return thread

        try:
            fetched = await self.bot.fetch_channel(thread_id)
        except (discord.Forbidden, discord.HTTPException, discord.NotFound):
            return None
        return fetched if isinstance(fetched, discord.Thread) else None

    async def _user_can_manage_ticket(
        self,
        interaction: discord.Interaction,
        thread: discord.Thread,
        ticket: dict | None,
        *,
        reopening: bool = False,
    ) -> bool:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return False
        member = interaction.user
        parent = thread.parent
        parent_perms = parent.permissions_for(member) if parent else member.guild_permissions
        is_admin = member.guild_permissions.administrator
        can_manage_threads = parent_perms.manage_threads
        support_match = bool(set(role.id for role in member.roles) & set(self.bot.settings.support_role_ids))
        is_opener = bool(ticket and ticket.get("opener_id") == member.id)

        if reopening:
            if self.bot.settings.allow_thread_owner_reopen and is_opener:
                return True
            return is_admin or can_manage_threads or support_match

        if self.bot.settings.close_requires_staff:
            return is_admin or can_manage_threads or support_match
        if self.bot.settings.allow_thread_owner_close and is_opener:
            return True
        return is_admin or can_manage_threads or support_match

    async def handle_ticket_creation(self, interaction: discord.Interaction, chosen_label: str) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("This can only be used inside a server.", ephemeral=True)
            return
        settings = self.bot.settings
        channel_id = settings.server_targets[chosen_label]
        target_channel = interaction.guild.get_channel(channel_id)
        if target_channel is None or not isinstance(target_channel, discord.TextChannel):
            await interaction.response.send_message("The configured destination channel is invalid.", ephemeral=True)
            return

        if settings.prevent_duplicate_open_tickets:
            existing = await self.bot.db.get_open_ticket_for_user(interaction.user.id, chosen_label)
            if existing:
                thread = interaction.guild.get_thread(existing["thread_id"]) or self.bot.get_channel(existing["thread_id"])
                mention = thread.mention if isinstance(thread, discord.Thread) else f"`{existing['thread_id']}`"
                await interaction.response.send_message(
                    f"You already have an open ticket for **{chosen_label}**: {mention}",
                    ephemeral=True,
                )
                return

        seed_message = await target_channel.send(f"New ticket request from {interaction.user.mention} for **{chosen_label}**")
        thread_name = (
            f"{settings.thread_name_prefix}-{clean_slug(chosen_label, 30)}-"
            f"{clean_slug(interaction.user.name, 30)}-{interaction.user.id}"
        )[:100]

        try:
            thread = await seed_message.create_thread(
                name=thread_name,
                auto_archive_duration=settings.auto_archive_duration,
                reason=f"Support ticket opened by {interaction.user} for {chosen_label}",
            )
        except discord.Forbidden:
            await interaction.response.send_message("I do not have permission to create threads there.", ephemeral=True)
            return
        except discord.HTTPException as exc:
            await interaction.response.send_message(f"Failed to create ticket thread: {exc}", ephemeral=True)
            return

        try:
            await thread.add_user(interaction.user)
        except discord.HTTPException:
            pass

        await self.bot.db.create_ticket(
            thread_id=thread.id,
            guild_id=interaction.guild.id,
            opener_id=interaction.user.id,
            opener_name=str(interaction.user),
            server_label=chosen_label,
            target_channel_id=target_channel.id,
            seed_message_id=seed_message.id,
            created_at=utc_now_iso(),
        )

        mentions = " ".join(f"<@&{role_id}>" for role_id in settings.support_role_ids)
        embed = self._embed(
            "Ticket Created",
            (
                f"**Server:** {chosen_label}\n"
                f"**Opened by:** {interaction.user.mention}\n"
                f"**Ticket ID:** `{thread.id}`\n\n"
                "Use the button below to close this ticket when it is resolved."
            ),
        )

        close_view = ThreadCloseView(self.bot, thread.id)
        self.bot.add_view(close_view)
        await thread.send(content=mentions or None, embed=embed, view=close_view)
        await interaction.response.send_message(f"Your ticket has been created: {thread.mention}", ephemeral=True)

    async def _send_transcript_log(
        self,
        thread: discord.Thread,
        closed_by: discord.abc.User,
        ticket: dict,
    ) -> tuple[int | None, str | None]:
        channel_id = self.bot.settings.transcript_channel_id
        if not channel_id:
            return None, None
        log_channel = thread.guild.get_channel(channel_id)
        if log_channel is None or not isinstance(log_channel, discord.TextChannel):
            return None, None

        bundle = await generate_transcripts(
            thread,
            include_txt=self.bot.settings.save_txt_transcript,
            include_html=self.bot.settings.save_html_transcript,
        )
        files = [item for item in (bundle.txt_file, bundle.html_file) if item is not None]
        dashboard_link = self.bot.settings.dashboard_base_url.rstrip("/") + f"/tickets/{thread.id}"
        embed = self._embed(
            "Ticket Closed",
            (
                f"**Thread:** {thread.mention}\n"
                f"**Server:** {ticket['server_label']}\n"
                f"**Opened by:** <@{ticket['opener_id']}>\n"
                f"**Closed by:** {closed_by.mention}\n"
                f"**Dashboard:** {dashboard_link}\n"
                f"**Delete after:** {self.bot.settings.delete_closed_threads_after_hours} hour(s)"
            ),
        )
        view = TicketLogControlsView(self.bot, thread.id)
        log_message = await log_channel.send(embed=embed, files=files, view=view)
        await self.bot.db.set_log_message_id(thread.id, log_message.id)
        self.bot.add_view(view, message_id=log_message.id)
        transcript_url = None
        if bundle.transcript_html is not None:
            store_html_transcript(thread.id, bundle.transcript_html)
            transcript_url = self.bot.settings.dashboard_base_url.rstrip("/") + f"/tickets/{thread.id}/transcript"
        elif log_message.jump_url:
            transcript_url = log_message.jump_url
        return log_message.id, transcript_url

    async def handle_close_from_thread(self, interaction: discord.Interaction, thread_id: int) -> None:
        thread = await self._resolve_thread(thread_id)
        if thread is None:
            await interaction.response.send_message("Could not find that ticket thread.", ephemeral=True)
            return

        ticket = await self.bot.db.get_ticket(thread.id)
        if ticket is None:
            await interaction.response.send_message("That thread is not tracked as a ticket.", ephemeral=True)
            return
        if ticket["status"] == "closed":
            await interaction.response.send_message("This ticket is already closed.", ephemeral=True)
            return
        if not await self._user_can_manage_ticket(interaction, thread, ticket, reopening=False):
            await interaction.response.send_message("You do not have permission to close this ticket.", ephemeral=True)
            return

        await interaction.response.send_message("Closing ticket...", ephemeral=True)
        log_message_id, transcript_message_url = await self._send_transcript_log(thread, interaction.user, ticket)
        await self.bot.db.close_ticket(
            thread_id=thread.id,
            closed_at=utc_now_iso(),
            closed_by_id=interaction.user.id,
            closed_by_name=str(interaction.user),
            log_message_id=log_message_id,
            transcript_message_url=transcript_message_url,
        )
        try:
            await thread.send(f"Ticket closed by {interaction.user.mention}.")
        except discord.HTTPException:
            pass
        await thread.edit(archived=True, locked=True, reason=f"Ticket closed by {interaction.user}")

    async def handle_reopen_from_log(self, interaction: discord.Interaction, thread_id: int) -> None:
        thread = await self._resolve_thread(thread_id)
        if thread is None:
            await interaction.response.send_message("Could not find that ticket thread.", ephemeral=True)
            return
        ticket = await self.bot.db.get_ticket(thread.id)
        if ticket is None:
            await interaction.response.send_message("That thread is not tracked as a ticket.", ephemeral=True)
            return
        if ticket["status"] != "closed":
            await interaction.response.send_message("This ticket is not closed.", ephemeral=True)
            return
        if not await self._user_can_manage_ticket(interaction, thread, ticket, reopening=True):
            await interaction.response.send_message("You do not have permission to reopen this ticket.", ephemeral=True)
            return
        await interaction.response.send_message("Reopening ticket...", ephemeral=True)
        await thread.edit(archived=False, locked=False, reason=f"Ticket reopened by {interaction.user}")
        try:
            await thread.send(f"Ticket reopened by {interaction.user.mention}.")
        except discord.HTTPException:
            pass
        await self.bot.db.reopen_ticket(
            thread_id=thread.id,
            reopened_at=utc_now_iso(),
            reopened_by_id=interaction.user.id,
            reopened_by_name=str(interaction.user),
        )

    async def handle_delete_from_log(self, interaction: discord.Interaction, thread_id: int) -> None:
        thread = await self._resolve_thread(thread_id)
        ticket = await self.bot.db.get_ticket(thread_id)
        if ticket is None:
            await interaction.response.send_message("That thread is not tracked as a ticket.", ephemeral=True)
            return
        if thread and not await self._user_can_manage_ticket(interaction, thread, ticket, reopening=True):
            await interaction.response.send_message("You do not have permission to delete this ticket.", ephemeral=True)
            return
        await interaction.response.send_message("Deleting ticket thread...", ephemeral=True)
        if thread is not None:
            try:
                await thread.delete()
            except discord.HTTPException:
                pass
        await self.bot.db.mark_deleted(
            thread_id=thread_id,
            deleted_at=utc_now_iso(),
            deleted_by_id=interaction.user.id,
            deleted_by_name=str(interaction.user),
        )

    @tasks.loop(hours=1)
    async def cleanup_closed_threads(self) -> None:
        await self.bot.wait_until_ready()
        cutoff = datetime.now(timezone.utc) - timedelta(hours=self.bot.settings.delete_closed_threads_after_hours)
        for ticket in await self.bot.db.list_closed_tickets():
            closed_at = ticket.get("closed_at")
            if not closed_at:
                continue
            try:
                closed_dt = datetime.fromisoformat(closed_at)
            except ValueError:
                continue
            if closed_dt > cutoff:
                continue
            thread = await self._resolve_thread(ticket["thread_id"])
            if thread is not None:
                try:
                    await thread.delete(reason="Closed ticket expired")
                except discord.HTTPException:
                    log.exception("Failed to delete expired closed thread %s", ticket["thread_id"])
                    continue
            await self.bot.db.mark_deleted(
                thread_id=ticket["thread_id"],
                deleted_at=utc_now_iso(),
                deleted_by_id=None,
                deleted_by_name="auto-cleanup",
            )

    @cleanup_closed_threads.before_loop
    async def before_cleanup(self) -> None:
        await self.bot.wait_until_ready()

    @app_commands.command(name="setup_tickets", description="Post the ticket panel")
    @app_commands.default_permissions(administrator=True)
    async def setup_tickets(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("This command must be used in a server.", ephemeral=True)
            return
        channel = interaction.guild.get_channel(self.bot.settings.panel_channel_id)
        if channel is None or not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message("The configured panel channel is invalid.", ephemeral=True)
            return
        embed = self._embed(
            "Support Tickets",
            "Press **Create Ticket** below, then choose which server the ticket is for.",
        )
        message = await channel.send(embed=embed, view=TicketPanelView(self.bot))
        try:
            await message.pin(reason="Ticket panel")
        except discord.HTTPException:
            pass
        await interaction.response.send_message(f"Ticket panel posted in {channel.mention}.", ephemeral=True)

    @app_commands.command(name="ticket_panel", description="Post another ticket panel")
    @app_commands.default_permissions(administrator=True)
    async def ticket_panel(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("This command must be used in a server.", ephemeral=True)
            return
        channel = interaction.guild.get_channel(self.bot.settings.panel_channel_id)
        if channel is None or not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message("The configured panel channel is invalid.", ephemeral=True)
            return
        embed = self._embed(
            "Support Tickets",
            "Press **Create Ticket** below, then choose which server the ticket is for.",
        )
        message = await channel.send(embed=embed, view=TicketPanelView(self.bot))
        try:
            await message.pin(reason="Ticket panel")
        except discord.HTTPException:
            pass
        await interaction.response.send_message(f"Ticket panel posted in {channel.mention}.", ephemeral=True)

    @app_commands.command(name="close_ticket", description="Close the current ticket thread")
    async def close_ticket(self, interaction: discord.Interaction) -> None:
        if not isinstance(interaction.channel, discord.Thread):
            await interaction.response.send_message("This command can only be used inside a ticket thread.", ephemeral=True)
            return
        await self.handle_close_from_thread(interaction, interaction.channel.id)

    @app_commands.command(name="reopen_ticket", description="Reopen the current ticket thread")
    async def reopen_ticket(self, interaction: discord.Interaction) -> None:
        if not isinstance(interaction.channel, discord.Thread):
            await interaction.response.send_message("This command can only be used inside a ticket thread.", ephemeral=True)
            return
        await self.handle_reopen_from_log(interaction, interaction.channel.id)

    @app_commands.command(name="ticket_info", description="Show metadata for the current ticket")
    async def ticket_info(self, interaction: discord.Interaction) -> None:
        if not isinstance(interaction.channel, discord.Thread):
            await interaction.response.send_message("This command can only be used inside a ticket thread.", ephemeral=True)
            return
        ticket = await self.bot.db.get_ticket(interaction.channel.id)
        if ticket is None:
            await interaction.response.send_message("This thread is not tracked as a ticket.", ephemeral=True)
            return
        embed = self._embed(
            "Ticket Info",
            (
                f"**Thread ID:** `{ticket['thread_id']}`\n"
                f"**Status:** {ticket['status']}\n"
                f"**Opened by:** <@{ticket['opener_id']}>\n"
                f"**Server:** {ticket['server_label']}\n"
                f"**Created:** {ticket['created_at']}\n"
                f"**Closed:** {ticket.get('closed_at') or 'N/A'}"
            ),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: "SupportTicketBot") -> None:
    await bot.add_cog(TicketsCog(bot))
