from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands, tasks

from support_ticket_bot.transcript import generate_transcripts, store_html_transcript
from support_ticket_bot.utils import clean_slug, render_template, utc_now_iso
from support_ticket_bot.views import TicketLogControlsView, TicketPanelView, ThreadCloseView, ThreadReopenView

log = logging.getLogger(__name__)


class TicketsCog(commands.Cog):
    INFO_EMBED_COLOR = 0x3B82F6
    REOPENED_EMBED_COLOR = 0xF59E0B
    CLOSED_EMBED_COLOR = 0xEF4444

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
            thread = await self._resolve_thread(ticket["thread_id"])
            if thread is not None:
                await self._set_thread_controls(thread, closed=False)
        for ticket in await self.bot.db.list_closed_tickets():
            self.bot.add_view(ThreadReopenView(self.bot, ticket["thread_id"]))
            thread = await self._resolve_thread(ticket["thread_id"])
            if thread is not None:
                await self._set_thread_controls(thread, closed=True)
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

    def _notice_embed(self, title: str, description: str, *, color: int) -> discord.Embed:
        embed = self._embed(title, description)
        embed.color = color
        return embed

    async def _send_thread_notice(
        self,
        thread: discord.Thread,
        *,
        title: str,
        description: str,
        color: int,
    ) -> None:
        try:
            await thread.send(embed=self._notice_embed(title, description, color=color))
        except discord.HTTPException:
            log.warning("Failed to send embed thread notice title=%s thread_id=%s; falling back to text.", title, thread.id)
            try:
                await thread.send(f"**{title}**\n{description}")
            except discord.HTTPException:
                log.exception("Failed to send fallback thread notice title=%s thread_id=%s", title, thread.id)

    async def _reply(self, interaction: discord.Interaction, content: str, *, delete_after: float | None = None) -> None:
        if delete_after is None:
            delete_after = self.bot.settings.interaction_delete_after_seconds
        try:
            if interaction.response.is_done():
                message = await interaction.followup.send(content, ephemeral=True, wait=True)
                if delete_after and message is not None:
                    asyncio.create_task(self._delete_followup_message_later(message, delete_after))
            else:
                await interaction.response.send_message(content, ephemeral=True, delete_after=delete_after)
        except discord.NotFound:
            log.warning("Interaction expired before a reply could be sent.")
        except discord.HTTPException:
            log.exception("Failed to send interaction reply.")

    async def _delete_followup_message_later(self, message: discord.WebhookMessage, delay: float) -> None:
        await asyncio.sleep(delay)
        try:
            await message.delete()
        except (discord.NotFound, discord.HTTPException):
            pass

    async def _delete_message_quietly(self, message: discord.Message, *, context: str) -> None:
        try:
            await message.delete()
        except discord.NotFound:
            return
        except (discord.Forbidden, discord.HTTPException):
            log.warning("Failed to delete message during %s message_id=%s", context, message.id)

    async def _find_thread_control_message(self, thread: discord.Thread, thread_id: int) -> discord.Message | None:
        close_custom_id = f"ticket:close:{thread_id}"
        reopen_custom_id = f"ticket:thread_reopen:{thread_id}"

        async for message in thread.history(limit=50, oldest_first=True):
            if self.bot.user is None or message.author.id != self.bot.user.id:
                continue
            for row in message.components:
                for component in row.children:
                    custom_id = getattr(component, "custom_id", None)
                    if custom_id in {close_custom_id, reopen_custom_id}:
                        return message
        return None

    async def _set_thread_controls(
        self,
        thread: discord.Thread,
        *,
        closed: bool,
        skip_archived: bool = True,
    ) -> None:
        if skip_archived and thread.archived:
            return

        control_message = await self._find_thread_control_message(thread, thread.id)
        if control_message is None:
            return

        view = ThreadReopenView(self.bot, thread.id) if closed else ThreadCloseView(self.bot, thread.id)
        self.bot.add_view(view)
        try:
            await control_message.edit(view=view)
        except discord.HTTPException as exc:
            if getattr(exc, "code", None) == 50083:
                log.debug("Skipped updating thread controls for archived thread_id=%s", thread.id)
                return
            log.exception("Failed to update thread controls for thread_id=%s", thread.id)

    async def _delete_seed_message(self, ticket: dict) -> None:
        channel_id = ticket.get("target_channel_id")
        message_id = ticket.get("seed_message_id")
        if not channel_id or not message_id:
            return

        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except (discord.Forbidden, discord.HTTPException, discord.NotFound):
                return

        if not isinstance(channel, discord.TextChannel):
            return

        try:
            message = await channel.fetch_message(message_id)
        except discord.NotFound:
            return
        except (discord.Forbidden, discord.HTTPException):
            log.exception(
                "Failed to fetch seed message for thread_id=%s seed_message_id=%s",
                ticket.get("thread_id"),
                message_id,
            )
            return

        try:
            await message.delete()
        except discord.NotFound:
            return
        except (discord.Forbidden, discord.HTTPException):
            log.exception(
                "Failed to delete seed message for thread_id=%s seed_message_id=%s",
                ticket.get("thread_id"),
                message_id,
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

    async def _get_message_templates(self) -> dict[str, str]:
        return await self.bot.db.get_message_templates()

    async def _resolve_ticket_user(self, user_id: int) -> discord.User | None:
        user = self.bot.get_user(user_id)
        if user is not None:
            return user
        try:
            return await self.bot.fetch_user(user_id)
        except (discord.Forbidden, discord.HTTPException, discord.NotFound):
            return None

    def _thread_link(self, thread: discord.Thread) -> str:
        jump_url = getattr(thread, "jump_url", None)
        if jump_url:
            return str(jump_url)
        return f"https://discord.com/channels/{thread.guild.id}/{thread.id}"

    async def _send_ticket_created_dm(
        self,
        *,
        opener_id: int,
        thread: discord.Thread,
        server_label: str,
    ) -> None:
        user = await self._resolve_ticket_user(opener_id)
        if user is None:
            log.warning("Could not resolve ticket opener for created DM opener_id=%s", opener_id)
            return

        embed = self._embed(
            "Ticket Created",
            "Your ticket has been created successfully.",
        )
        embed.color = discord.Color.green()
        embed.add_field(name="Ticket Name", value=thread.name, inline=False)
        embed.add_field(name="Queue", value=server_label, inline=True)
        embed.add_field(name="Ticket ID", value=f"`{thread.id}`", inline=True)
        embed.add_field(name="Open Ticket", value=f"[Open in Discord]({self._thread_link(thread)})", inline=False)

        try:
            await user.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException):
            log.warning(
                "Failed to send ticket created DM for thread_id=%s opener_id=%s",
                thread.id,
                opener_id,
            )

    async def _send_transcript_dm(self, ticket: dict, transcript_url: str | None) -> None:
        if not transcript_url:
            return

        opener_id = ticket.get("opener_id")
        if not opener_id:
            return

        user = await self._resolve_ticket_user(opener_id)
        if user is None:
            log.warning("Could not resolve ticket opener for transcript DM opener_id=%s", opener_id)
            return

        embed = self._embed(
            "Ticket Closed",
            "Your ticket has been closed.",
        )
        embed.color = discord.Color.red()
        embed.add_field(name="Ticket ID", value=f"`{ticket.get('thread_id')}`", inline=True)
        embed.add_field(name="Queue", value=str(ticket.get("server_label") or "Unknown"), inline=True)
        embed.add_field(name="Transcript", value=f"[Open Transcript]({transcript_url})", inline=False)
        embed.set_footer(text="If prompted, sign in to the dashboard with Discord to open it.")

        try:
            await user.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException):
            log.warning(
                "Failed to send transcript DM for thread_id=%s opener_id=%s",
                ticket.get("thread_id"),
                opener_id,
            )

    async def _send_ticket_reopened_dm(self, ticket: dict, thread: discord.Thread) -> None:
        opener_id = ticket.get("opener_id")
        if not opener_id:
            return

        user = await self._resolve_ticket_user(opener_id)
        if user is None:
            log.warning("Could not resolve ticket opener for reopened DM opener_id=%s", opener_id)
            return

        embed = self._embed(
            "Ticket Reopened",
            "Your ticket has been reopened.",
        )
        embed.color = discord.Color.orange()
        embed.add_field(name="Ticket Name", value=thread.name, inline=False)
        embed.add_field(name="Queue", value=str(ticket.get("server_label") or "Unknown"), inline=True)
        embed.add_field(name="Ticket ID", value=f"`{thread.id}`", inline=True)
        embed.add_field(name="Open Ticket", value=f"[Open in Discord]({self._thread_link(thread)})", inline=False)

        try:
            await user.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException):
            log.warning(
                "Failed to send ticket reopened DM for thread_id=%s opener_id=%s",
                thread.id,
                opener_id,
            )

    async def _record_audit_event(
        self,
        *,
        event_type: str,
        actor: discord.abc.User,
        ticket_thread_id: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        try:
            await self.bot.db.add_audit_event(
                event_type=event_type,
                actor_discord_user_id=actor.id,
                actor_username=getattr(actor, "name", str(actor)),
                actor_display_name=getattr(actor, "display_name", getattr(actor, "name", str(actor))),
                ticket_thread_id=ticket_thread_id,
                metadata=metadata,
                created_at=utc_now_iso(),
            )
        except Exception:
            log.exception("Failed to record audit event %s for thread_id=%s", event_type, ticket_thread_id)

    async def _assign_ticket_to_member(
        self,
        *,
        thread: discord.Thread,
        ticket: dict,
        assignee: discord.Member,
        actor: discord.abc.User,
    ) -> tuple[bool, str]:
        if ticket["status"] != "open":
            return False, "Only open tickets can be assigned."

        existing_assignee_id = ticket.get("assignee_discord_user_id")
        if existing_assignee_id == assignee.id:
            return False, f"This ticket is already assigned to {assignee.mention}."

        if assignee.bot:
            return False, "Bots cannot be assigned to tickets."

        if assignee.guild.id != thread.guild.id:
            return False, "That user is not a member of this server."

        try:
            await thread.add_user(assignee)
        except discord.Forbidden:
            return False, "I do not have permission to add that user to this thread."
        except discord.HTTPException as exc:
            return False, f"Failed to add assignee to ticket: {exc}"

        await self.bot.db.assign_ticket(
            thread_id=thread.id,
            assignee_discord_user_id=assignee.id,
            assignee_display_name=assignee.display_name,
            assigned_at=utc_now_iso(),
            assigned_by_discord_user_id=actor.id,
            assigned_by_display_name=getattr(actor, "display_name", str(actor)),
        )

        is_reassignment = existing_assignee_id is not None
        action_label = "reassigned" if is_reassignment else "assigned"
        await self._send_thread_notice(
            thread,
            title="Ticket Reassigned" if is_reassignment else "Ticket Assigned",
            description=(
                f"This ticket has been {action_label} to {assignee.mention} "
                f"by {getattr(actor, 'mention', str(actor))}."
            ),
            color=self.INFO_EMBED_COLOR,
        )

        await self._record_audit_event(
            event_type="ticket_assigned",
            actor=actor,
            ticket_thread_id=thread.id,
            metadata={
                "assignee_discord_user_id": assignee.id,
                "assignee_display_name": assignee.display_name,
                "reassigned": is_reassignment,
                "source": "discord_slash_command",
            },
        )
        log.info(
            "Ticket assigned thread_id=%s guild_id=%s assignee_id=%s assigned_by_id=%s reassigned=%s",
            thread.id,
            thread.guild.id,
            assignee.id,
            actor.id,
            is_reassignment,
        )
        return True, f"Assigned {thread.mention} to {assignee.mention}."

    async def _add_tag_to_ticket(
        self,
        *,
        thread: discord.Thread,
        ticket: dict,
        tag: dict[str, Any],
        actor: discord.abc.User,
        source: str,
    ) -> tuple[bool, str]:
        if ticket["status"] != "open":
            return False, "Only open tickets can be updated."

        existing_tags = await self.bot.db.list_ticket_tags(thread.id)
        if any(existing["id"] == tag["id"] for existing in existing_tags):
            return False, f'Tag "{tag["tag_name"]}" is already applied to this ticket.'

        await self.bot.db.add_ticket_tag(
            thread_id=thread.id,
            tag_id=tag["id"],
            assigned_at=utc_now_iso(),
            assigned_by_discord_user_id=actor.id,
            assigned_by_display_name=getattr(actor, "display_name", str(actor)),
        )
        await self._send_thread_notice(
            thread,
            title="Tag Added",
            description=f'Tag `{tag["tag_name"]}` was added to this ticket by {getattr(actor, "mention", str(actor))}.',
            color=self.INFO_EMBED_COLOR,
        )
        await self._record_audit_event(
            event_type="ticket_tag_added",
            actor=actor,
            ticket_thread_id=thread.id,
            metadata={"tag_id": tag["id"], "tag_name": tag["tag_name"], "source": source},
        )
        return True, f'Added tag "{tag["tag_name"]}" to {thread.mention}.'

    async def _remove_tag_from_ticket(
        self,
        *,
        thread: discord.Thread,
        ticket: dict,
        tag: dict[str, Any],
        actor: discord.abc.User,
        source: str,
    ) -> tuple[bool, str]:
        if ticket["status"] != "open":
            return False, "Only open tickets can be updated."

        existing_tags = await self.bot.db.list_ticket_tags(thread.id)
        if not any(existing["id"] == tag["id"] for existing in existing_tags):
            return False, f'Tag "{tag["tag_name"]}" is not applied to this ticket.'

        await self.bot.db.remove_ticket_tag(thread_id=thread.id, tag_id=tag["id"])
        await self._send_thread_notice(
            thread,
            title="Tag Removed",
            description=f'Tag `{tag["tag_name"]}` was removed from this ticket by {getattr(actor, "mention", str(actor))}.',
            color=self.INFO_EMBED_COLOR,
        )
        await self._record_audit_event(
            event_type="ticket_tag_removed",
            actor=actor,
            ticket_thread_id=thread.id,
            metadata={"tag_id": tag["id"], "tag_name": tag["tag_name"], "source": source},
        )
        return True, f'Removed tag "{tag["tag_name"]}" from {thread.mention}.'

    async def _tag_name_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
        *,
        assigned_only: bool = False,
        unassigned_only: bool = False,
    ) -> list[app_commands.Choice[str]]:
        if assigned_only and isinstance(interaction.channel, discord.Thread):
            tags = await self.bot.db.list_ticket_tags(interaction.channel.id)
        else:
            tags = await self.bot.db.list_tag_definitions()

        if unassigned_only and isinstance(interaction.channel, discord.Thread):
            assigned_tag_ids = {tag["id"] for tag in await self.bot.db.list_ticket_tags(interaction.channel.id)}
            tags = [tag for tag in tags if tag["id"] not in assigned_tag_ids]

        needle = current.casefold().strip()
        if needle:
            tags = [tag for tag in tags if needle in str(tag["tag_name"]).casefold()]

        return [
            app_commands.Choice(name=str(tag["tag_name"]), value=str(tag["tag_name"]))
            for tag in tags[:25]
        ]

    def _member_has_staff_ticket_access(
        self,
        member: discord.Member,
        *,
        can_manage_threads: bool | None = None,
    ) -> bool:
        support_match = bool(set(role.id for role in member.roles) & set(self.bot.settings.support_role_ids))
        if can_manage_threads is None:
            can_manage_threads = bool(getattr(member.guild_permissions, "manage_threads", False))
        return member.guild_permissions.administrator or can_manage_threads or support_match

    def _user_can_manage_ticket_without_thread(
        self,
        interaction: discord.Interaction,
        ticket: dict | None,
        *,
        reopening: bool = False,
    ) -> bool:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return False
        member = interaction.user
        is_opener = bool(ticket and ticket.get("opener_id") == member.id)
        has_staff_access = self._member_has_staff_ticket_access(member)

        if reopening:
            if self.bot.settings.allow_thread_owner_reopen and is_opener:
                return True
            return has_staff_access

        if self.bot.settings.close_requires_staff:
            return has_staff_access
        if self.bot.settings.allow_thread_owner_close and is_opener:
            return True
        return has_staff_access

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
        is_opener = bool(ticket and ticket.get("opener_id") == member.id)
        has_staff_access = self._member_has_staff_ticket_access(member, can_manage_threads=parent_perms.manage_threads)

        if reopening:
            if self.bot.settings.allow_thread_owner_reopen and is_opener:
                return True
            return has_staff_access

        if self.bot.settings.close_requires_staff:
            return has_staff_access
        if self.bot.settings.allow_thread_owner_close and is_opener:
            return True
        return has_staff_access

    async def handle_ticket_creation(self, interaction: discord.Interaction, chosen_label: str) -> None:
        if interaction.guild is None:
            await self._reply(interaction, "This can only be used inside a server.")
            return
        settings = self.bot.settings
        channel_id = settings.server_targets[chosen_label]
        target_channel = interaction.guild.get_channel(channel_id)
        if target_channel is None or not isinstance(target_channel, discord.TextChannel):
            await self._reply(interaction, "The configured destination channel is invalid.")
            return

        try:
            await interaction.response.defer(ephemeral=True)
        except discord.HTTPException:
            log.warning("Failed to defer ticket creation interaction for user_id=%s", interaction.user.id)

        seed_message = await target_channel.send(f"New ticket request from {interaction.user.mention} for **{chosen_label}**")
        thread_name_prefix = (
            f"{settings.thread_name_prefix}-{clean_slug(chosen_label, 30)}-"
            f"{clean_slug(interaction.user.name, 30)}"
        )
        thread_name = thread_name_prefix[:100]

        try:
            thread = await seed_message.create_thread(
                name=thread_name,
                auto_archive_duration=settings.auto_archive_duration,
                reason=f"Support ticket opened by {interaction.user} for {chosen_label}",
            )
        except discord.Forbidden:
            await self._delete_message_quietly(seed_message, context="thread creation cleanup")
            await self._reply(interaction, "I do not have permission to create threads there.")
            return
        except discord.HTTPException as exc:
            await self._delete_message_quietly(seed_message, context="thread creation cleanup")
            await self._reply(interaction, f"Failed to create ticket thread: {exc}")
            return

        final_thread_name = f"{thread_name_prefix}-{thread.id}"[:100]
        if thread.name != final_thread_name:
            try:
                await thread.edit(name=final_thread_name, reason="Use ticket ID in thread name")
            except discord.HTTPException:
                log.warning("Failed to rename ticket thread thread_id=%s final_name=%s", thread.id, final_thread_name)

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
        templates = await self._get_message_templates()
        embed = self._embed(
            render_template(
                templates["thread_embed_title"],
                guild_name=interaction.guild.name,
                server_label=chosen_label,
                user_mention=interaction.user.mention,
                user_name=interaction.user.display_name,
                thread_id=thread.id,
            ),
            render_template(
                templates["thread_embed_description"],
                guild_name=interaction.guild.name,
                server_label=chosen_label,
                user_mention=interaction.user.mention,
                user_name=interaction.user.display_name,
                thread_id=thread.id,
            ),
        )

        close_view = ThreadCloseView(self.bot, thread.id)
        self.bot.add_view(close_view)
        await thread.send(content=mentions or None, embed=embed, view=close_view)
        await self._send_ticket_created_dm(
            opener_id=interaction.user.id,
            thread=thread,
            server_label=chosen_label,
        )
        log.info(
            "Ticket opened thread_id=%s guild_id=%s opener_id=%s server_label=%s target_channel_id=%s",
            thread.id,
            interaction.guild.id,
            interaction.user.id,
            chosen_label,
            target_channel.id,
        )
        await self._reply(interaction, f"Your ticket has been created: {thread.mention}")

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

        try:
            bundle = await generate_transcripts(
                thread,
                include_txt=self.bot.settings.save_txt_transcript,
                include_html=self.bot.settings.save_html_transcript,
            )
        except Exception:
            log.exception("Failed to generate transcripts for thread_id=%s", thread.id)
            return None, None

        files = [item for item in (bundle.txt_file, bundle.html_file) if item is not None]
        transcript_url = None
        if bundle.transcript_html is not None:
            try:
                store_html_transcript(thread.id, bundle.transcript_html)
                transcript_url = self.bot.settings.dashboard_base_url.rstrip("/") + f"/tickets/{thread.id}/transcript"
            except OSError:
                log.exception("Failed to store HTML transcript for thread_id=%s", thread.id)

        dashboard_link = self.bot.settings.dashboard_base_url.rstrip("/") + f"/tickets/{thread.id}"
        assignee_line = ""
        if ticket.get("assignee_display_name") or ticket.get("assignee_discord_user_id"):
            assignee_value = ticket.get("assignee_display_name") or ticket.get("assignee_discord_user_id")
            assignee_line = f"**Assignee:** {assignee_value}\n"
        embed = self._embed(
            "Ticket Closed",
            (
                f"**Thread:** {thread.mention}\n"
                f"**Server:** {ticket['server_label']}\n"
                f"**Opened by:** <@{ticket['opener_id']}>\n"
                f"{assignee_line}"
                f"**Closed by:** {closed_by.mention}\n"
                f"**Dashboard:** [Click Here]({dashboard_link})\n"
                f"**Delete after:** {self.bot.settings.delete_closed_threads_after_hours} hour(s)"
            ),
        )
        view = TicketLogControlsView(self.bot, thread.id)
        try:
            log_message = await log_channel.send(embed=embed, files=files, view=view)
        except discord.HTTPException:
            log.exception("Failed to send transcript log for thread_id=%s channel_id=%s", thread.id, channel_id)
            return None, transcript_url

        await self.bot.db.set_log_message_id(thread.id, log_message.id)
        self.bot.add_view(view, message_id=log_message.id)
        if transcript_url is None and log_message.jump_url:
            transcript_url = log_message.jump_url
        return log_message.id, transcript_url

    async def handle_close_from_thread(self, interaction: discord.Interaction, thread_id: int) -> None:
        thread = await self._resolve_thread(thread_id)
        if thread is None:
            await self._reply(interaction, "Could not find that ticket thread.")
            return

        ticket = await self.bot.db.get_ticket(thread.id)
        if ticket is None:
            await self._reply(interaction, "That thread is not tracked as a ticket.")
            return
        if ticket["status"] == "closed":
            await self._reply(interaction, "This ticket is already closed.")
            return
        if not await self._user_can_manage_ticket(interaction, thread, ticket, reopening=False):
            await self._reply(interaction, "You do not have permission to close this ticket.")
            return

        await self._reply(interaction, "Closing ticket...")
        log_message_id, transcript_message_url = await self._send_transcript_log(thread, interaction.user, ticket)
        await self.bot.db.close_ticket(
            thread_id=thread.id,
            closed_at=utc_now_iso(),
            closed_by_id=interaction.user.id,
            closed_by_name=str(interaction.user),
            log_message_id=log_message_id,
            transcript_message_url=transcript_message_url,
        )
        await self._send_transcript_dm(ticket, transcript_message_url)
        await self._send_thread_notice(
            thread,
            title="Ticket Closed",
            description=f"This ticket was closed by {interaction.user.mention}.",
            color=self.CLOSED_EMBED_COLOR,
        )
        await self._set_thread_controls(thread, closed=True)
        log.info(
            "Ticket closed thread_id=%s guild_id=%s closed_by_id=%s log_message_id=%s",
            thread.id,
            thread.guild.id,
            interaction.user.id,
            log_message_id,
        )
        await thread.edit(archived=True, locked=True, reason=f"Ticket closed by {interaction.user}")

    async def handle_reopen_from_log(self, interaction: discord.Interaction, thread_id: int) -> None:
        thread = await self._resolve_thread(thread_id)
        if thread is None:
            await self._reply(interaction, "Could not find that ticket thread.")
            return
        ticket = await self.bot.db.get_ticket(thread.id)
        if ticket is None:
            await self._reply(interaction, "That thread is not tracked as a ticket.")
            return
        if ticket["status"] != "closed":
            await self._reply(interaction, "This ticket is not closed.")
            return
        if not await self._user_can_manage_ticket(interaction, thread, ticket, reopening=True):
            await self._reply(interaction, "You do not have permission to reopen this ticket.")
            return
        await self._reply(interaction, "Reopening ticket...")
        await thread.edit(archived=False, locked=False, reason=f"Ticket reopened by {interaction.user}")
        await self._send_thread_notice(
            thread,
            title="Ticket Reopened",
            description=f"This ticket was reopened by {interaction.user.mention}.",
            color=self.REOPENED_EMBED_COLOR,
        )
        try:
            fetched_thread = await self.bot.fetch_channel(thread.id)
        except (discord.Forbidden, discord.HTTPException, discord.NotFound):
            fetched_thread = None
        refreshed_thread = fetched_thread if isinstance(fetched_thread, discord.Thread) else await self._resolve_thread(thread.id)
        await self._set_thread_controls(refreshed_thread or thread, closed=False, skip_archived=False)
        await self.bot.db.reopen_ticket(
            thread_id=thread.id,
            reopened_at=utc_now_iso(),
            reopened_by_id=interaction.user.id,
            reopened_by_name=str(interaction.user),
        )
        await self._send_ticket_reopened_dm(ticket, thread)
        log.info(
            "Ticket reopened thread_id=%s guild_id=%s reopened_by_id=%s",
            thread.id,
            thread.guild.id,
            interaction.user.id,
        )

    async def handle_delete_from_log(self, interaction: discord.Interaction, thread_id: int) -> None:
        thread = await self._resolve_thread(thread_id)
        ticket = await self.bot.db.get_ticket(thread_id)
        if ticket is None:
            await self._reply(interaction, "That thread is not tracked as a ticket.")
            return
        if thread is not None:
            allowed = await self._user_can_manage_ticket(interaction, thread, ticket, reopening=True)
        else:
            allowed = self._user_can_manage_ticket_without_thread(interaction, ticket, reopening=True)
        if not allowed:
            await self._reply(interaction, "You do not have permission to delete this ticket.")
            return
        await self._reply(interaction, "Deleting ticket thread...")
        if thread is not None:
            try:
                await thread.delete()
            except discord.HTTPException:
                pass
        await self._delete_seed_message(ticket)
        await self.bot.db.mark_deleted(
            thread_id=thread_id,
            deleted_at=utc_now_iso(),
            deleted_by_id=interaction.user.id,
            deleted_by_name=str(interaction.user),
        )
        log.info(
            "Ticket deleted thread_id=%s guild_id=%s deleted_by_id=%s",
            thread_id,
            interaction.guild.id if interaction.guild else "unknown",
            interaction.user.id,
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
            await self._delete_seed_message(ticket)
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
            await self._reply(interaction, "This command must be used in a server.")
            return
        channel = interaction.guild.get_channel(self.bot.settings.panel_channel_id)
        if channel is None or not isinstance(channel, discord.TextChannel):
            await self._reply(interaction, "The configured panel channel is invalid.")
            return
        templates = await self._get_message_templates()
        embed = self._embed(
            render_template(
                templates["panel_title"],
                guild_name=interaction.guild.name,
                panel_channel_mention=channel.mention,
            ),
            render_template(
                templates["panel_description"],
                guild_name=interaction.guild.name,
                panel_channel_mention=channel.mention,
            ),
        )
        message = await channel.send(embed=embed, view=TicketPanelView(self.bot))
        try:
            await message.pin(reason="Ticket panel")
        except discord.HTTPException:
            pass
        await self._reply(interaction, f"Ticket panel posted in {channel.mention}.")

    @app_commands.command(name="ticket_panel", description="Post another ticket panel")
    @app_commands.default_permissions(administrator=True)
    async def ticket_panel(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await self._reply(interaction, "This command must be used in a server.")
            return
        channel = interaction.guild.get_channel(self.bot.settings.panel_channel_id)
        if channel is None or not isinstance(channel, discord.TextChannel):
            await self._reply(interaction, "The configured panel channel is invalid.")
            return
        templates = await self._get_message_templates()
        embed = self._embed(
            render_template(
                templates["panel_title"],
                guild_name=interaction.guild.name,
                panel_channel_mention=channel.mention,
            ),
            render_template(
                templates["panel_description"],
                guild_name=interaction.guild.name,
                panel_channel_mention=channel.mention,
            ),
        )
        message = await channel.send(embed=embed, view=TicketPanelView(self.bot))
        try:
            await message.pin(reason="Ticket panel")
        except discord.HTTPException:
            pass
        await self._reply(interaction, f"Ticket panel posted in {channel.mention}.")

    @app_commands.command(name="close_ticket", description="Close the current ticket thread")
    async def close_ticket(self, interaction: discord.Interaction) -> None:
        if not isinstance(interaction.channel, discord.Thread):
            await self._reply(interaction, "This command can only be used inside a ticket thread.")
            return
        await self.handle_close_from_thread(interaction, interaction.channel.id)

    @app_commands.command(name="reopen_ticket", description="Reopen the current ticket thread")
    async def reopen_ticket(self, interaction: discord.Interaction) -> None:
        if not isinstance(interaction.channel, discord.Thread):
            await self._reply(interaction, "This command can only be used inside a ticket thread.")
            return
        await self.handle_reopen_from_log(interaction, interaction.channel.id)

    @app_commands.command(name="ticket_info", description="Show metadata for the current ticket")
    async def ticket_info(self, interaction: discord.Interaction) -> None:
        if not isinstance(interaction.channel, discord.Thread):
            await self._reply(interaction, "This command can only be used inside a ticket thread.")
            return
        ticket = await self.bot.db.get_ticket(interaction.channel.id)
        if ticket is None:
            await self._reply(interaction, "This thread is not tracked as a ticket.")
            return
        ticket_tags = await self.bot.db.list_ticket_tags(interaction.channel.id)
        tags_text = ", ".join(tag["tag_name"] for tag in ticket_tags) if ticket_tags else "None"
        embed = self._embed(
            "Ticket Info",
            (
                f"**Thread ID:** `{ticket['thread_id']}`\n"
                f"**Status:** {ticket['status']}\n"
                f"**Opened by:** <@{ticket['opener_id']}>\n"
                f"**Server:** {ticket['server_label']}\n"
                f"**Tags:** {tags_text}\n"
                f"**Assignee:** {ticket.get('assignee_display_name') or 'Unassigned'}\n"
                f"**Created:** {ticket['created_at']}\n"
                f"**Closed:** {ticket.get('closed_at') or 'N/A'}"
            ),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="list_tags", description="List all managed ticket tags")
    async def list_tags(self, interaction: discord.Interaction) -> None:
        tags = await self.bot.db.list_tag_definitions()
        if not tags:
            await self._reply(interaction, "No managed tags have been created yet.")
            return
        embed = self._embed(
            "Managed Tags",
            "\n".join(f"- `{tag['tag_name']}`" for tag in tags),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="create_tag", description="Create a managed ticket tag")
    @app_commands.default_permissions(administrator=True)
    async def create_tag(self, interaction: discord.Interaction, name: str) -> None:
        cleaned_name = " ".join(name.strip().split())
        if not cleaned_name:
            await self._reply(interaction, "Tag name cannot be empty.")
            return
        existing = await self.bot.db.get_tag_definition_by_name(cleaned_name)
        if existing is not None:
            await self._reply(interaction, f'Tag "{existing["tag_name"]}" already exists.')
            return

        created = await self.bot.db.create_tag_definition(
            tag_name=cleaned_name,
            created_by_discord_user_id=interaction.user.id,
            created_by_display_name=getattr(interaction.user, "display_name", str(interaction.user)),
            created_at=utc_now_iso(),
        )
        await self._record_audit_event(
            event_type="tag_created",
            actor=interaction.user,
            metadata={"tag_id": created["id"], "tag_name": created["tag_name"], "source": "discord_slash_command"},
        )
        await self._reply(interaction, f'Created tag "{created["tag_name"]}".')

    @app_commands.command(name="delete_tag", description="Delete a managed ticket tag")
    @app_commands.default_permissions(administrator=True)
    async def delete_tag(self, interaction: discord.Interaction, name: str) -> None:
        tag = await self.bot.db.get_tag_definition_by_name(name)
        if tag is None:
            await self._reply(interaction, "Tag not found.")
            return
        await self.bot.db.delete_tag_definition(tag["id"])
        await self._record_audit_event(
            event_type="tag_deleted",
            actor=interaction.user,
            metadata={"tag_id": tag["id"], "tag_name": tag["tag_name"], "source": "discord_slash_command"},
        )
        await self._reply(interaction, f'Deleted tag "{tag["tag_name"]}".')

    @delete_tag.autocomplete("name")
    async def delete_tag_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        return await self._tag_name_autocomplete(interaction, current)

    @app_commands.command(name="assign_ticket", description="Assign the current ticket to yourself or another user")
    async def assign_ticket(self, interaction: discord.Interaction, user: discord.Member | None = None) -> None:
        if not isinstance(interaction.channel, discord.Thread):
            await self._reply(interaction, "This command can only be used inside a ticket thread.")
            return

        thread = interaction.channel
        ticket = await self.bot.db.get_ticket(thread.id)
        if ticket is None:
            await self._reply(interaction, "This thread is not tracked as a ticket.")
            return
        if not await self._user_can_manage_ticket(interaction, thread, ticket, reopening=False):
            await self._reply(interaction, "You do not have permission to assign this ticket.")
            return
        if not isinstance(interaction.user, discord.Member):
            await self._reply(interaction, "This command must be used by a server member.")
            return

        assignee = user or interaction.user
        assigned, message = await self._assign_ticket_to_member(
            thread=thread,
            ticket=ticket,
            assignee=assignee,
            actor=interaction.user,
        )
        await self._reply(interaction, message)

    @app_commands.command(name="add_ticket_tag", description="Add a managed tag to the current ticket")
    async def add_ticket_tag(self, interaction: discord.Interaction, tag_name: str) -> None:
        if not isinstance(interaction.channel, discord.Thread):
            await self._reply(interaction, "This command can only be used inside a ticket thread.")
            return

        thread = interaction.channel
        ticket = await self.bot.db.get_ticket(thread.id)
        if ticket is None:
            await self._reply(interaction, "This thread is not tracked as a ticket.")
            return
        if not await self._user_can_manage_ticket(interaction, thread, ticket, reopening=False):
            await self._reply(interaction, "You do not have permission to update ticket tags.")
            return

        tag = await self.bot.db.get_tag_definition_by_name(tag_name)
        if tag is None:
            await self._reply(interaction, "Tag not found.")
            return

        added, message = await self._add_tag_to_ticket(
            thread=thread,
            ticket=ticket,
            tag=tag,
            actor=interaction.user,
            source="discord_slash_command",
        )
        await self._reply(interaction, message)

    @add_ticket_tag.autocomplete("tag_name")
    async def add_ticket_tag_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        return await self._tag_name_autocomplete(interaction, current, unassigned_only=True)

    @app_commands.command(name="remove_ticket_tag", description="Remove a managed tag from the current ticket")
    async def remove_ticket_tag(self, interaction: discord.Interaction, tag_name: str) -> None:
        if not isinstance(interaction.channel, discord.Thread):
            await self._reply(interaction, "This command can only be used inside a ticket thread.")
            return

        thread = interaction.channel
        ticket = await self.bot.db.get_ticket(thread.id)
        if ticket is None:
            await self._reply(interaction, "This thread is not tracked as a ticket.")
            return
        if not await self._user_can_manage_ticket(interaction, thread, ticket, reopening=False):
            await self._reply(interaction, "You do not have permission to update ticket tags.")
            return

        tag = await self.bot.db.get_tag_definition_by_name(tag_name)
        if tag is None:
            await self._reply(interaction, "Tag not found.")
            return

        removed, message = await self._remove_tag_from_ticket(
            thread=thread,
            ticket=ticket,
            tag=tag,
            actor=interaction.user,
            source="discord_slash_command",
        )
        await self._reply(interaction, message)

    @remove_ticket_tag.autocomplete("tag_name")
    async def remove_ticket_tag_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        return await self._tag_name_autocomplete(interaction, current, assigned_only=True)

    @app_commands.command(name="add_ticket_user", description="Add a user to the current ticket thread")
    async def add_ticket_user(self, interaction: discord.Interaction, user: discord.Member) -> None:
        if not isinstance(interaction.channel, discord.Thread):
            await self._reply(interaction, "This command can only be used inside a ticket thread.")
            return

        thread = interaction.channel
        ticket = await self.bot.db.get_ticket(thread.id)
        if ticket is None:
            await self._reply(interaction, "This thread is not tracked as a ticket.")
            return
        if ticket["status"] != "open":
            await self._reply(interaction, "You can only add users to an open ticket.")
            return
        if not await self._user_can_manage_ticket(interaction, thread, ticket, reopening=False):
            await self._reply(interaction, "You do not have permission to add users to this ticket.")
            return

        try:
            await thread.add_user(user)
        except discord.Forbidden:
            await self._reply(interaction, "I do not have permission to add that user to this thread.")
            return
        except discord.HTTPException as exc:
            await self._reply(interaction, f"Failed to add user to ticket: {exc}")
            return

        await self._send_thread_notice(
            thread,
            title="Member Added",
            description=f"{user.mention} has been added to this ticket by {interaction.user.mention}.",
            color=self.INFO_EMBED_COLOR,
        )

        log.info(
            "Ticket user added thread_id=%s guild_id=%s added_user_id=%s added_by_id=%s",
            thread.id,
            thread.guild.id,
            user.id,
            interaction.user.id,
        )
        await self._reply(interaction, f"Added {user.mention} to {thread.mention}.")


async def setup(bot: "SupportTicketBot") -> None:
    await bot.add_cog(TicketsCog(bot))
