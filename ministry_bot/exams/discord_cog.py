"""Expose exam commands and deliver exam publication and room alerts."""

import asyncio
from datetime import UTC, datetime, timedelta
import logging
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks

from .exam_monitor import ExamCalendarClient, ExamStateStore, ExamMonitor
from ..notifications.subscriptions import add_subscription_prompt, register_subscription_message

logger = logging.getLogger(__name__)


class Exams(commands.Cog):
    exams = app_commands.Group(name="exams", description="Exam timetable commands.")

    def __init__(self, bot):
        self.bot = bot
        self.settings = bot.settings
        self.exam_settings = self.settings.exams
        self.policy = self.exam_settings.policy
        self.state_store = ExamStateStore(self.settings.database_path)
        self.exam_monitor = ExamMonitor(ExamCalendarClient(bot.http_session, self.exam_settings.page_url), self.state_store)
        self.delivery_lock = asyncio.Lock()
        self.failures = 0

    async def cog_load(self):
        if self.exam_settings.enabled:
            self.poll.start()

    async def cog_unload(self):
        self.poll.cancel()
        task = self.poll.get_task()
        if task is not None:
            try:
                await task
            except asyncio.CancelledError:
                pass

    def build_exam_notification_embed(self, notification):
        snapshot = notification.snapshot
        published = notification.kind == "new_period"
        title = "New exam timetable published" if published else "Exam rooms changed"
        embed = discord.Embed(
            title=title,
            description=(
                f"Exam period: **{discord.utils.escape_markdown(snapshot.period.label)}**\n"
                + (f"A timetable with {len(snapshot.events)} examinations is available."
                   if published else f"Room assignments changed for {len(notification.changes)} examinations.")
            ),
            url=snapshot.period.program_url,
            colour=discord.Colour.blue() if published else discord.Colour.orange(),
            timestamp=datetime.fromisoformat(snapshot.checked_at),
        )
        for change in notification.changes[:8]:
            event = change.after
            start = datetime.fromisoformat(event.starts_at).astimezone(ZoneInfo("Europe/Athens"))
            end = datetime.fromisoformat(event.ends_at).astimezone(ZoneInfo("Europe/Athens"))
            old_rooms = ", ".join(change.before.rooms) or "Not assigned"
            new_rooms = ", ".join(event.rooms) or "Not assigned"
            value = (
                f"{start:%d/%m/%Y %H:%M} - {end:%H:%M} (Europe/Athens)\n"
                f"Previous rooms: {discord.utils.escape_markdown(old_rooms)}\n"
                f"New rooms: {discord.utils.escape_markdown(new_rooms)}"
            )
            embed.add_field(name=event.title[:180], value=value[:500], inline=False)
        if len(notification.changes) > 8:
            embed.set_footer(text=f"Showing 8 of {len(notification.changes)} room changes. Open the full timetable for all assignments.")
        embed.add_field(name="Program", value=f"[Open exam timetable]({snapshot.period.program_url})", inline=False)
        return embed

    async def send_pending_notifications(self):
        outbox = self.state_store
        async with self.delivery_lock:
            for notification in outbox.get_pending_notifications():
                try:
                    channel = self.bot.get_channel(self.exam_settings.channel_id)
                    if channel is None:
                        channel = await self.bot.fetch_channel(self.exam_settings.channel_id)
                    if not isinstance(channel, (discord.TextChannel, discord.Thread)):
                        raise ValueError("The exam alert destination must be a text channel or thread.")
                    if channel.guild.id != self.settings.guild_id:
                        raise ValueError("The exam alert channel belongs to a different server.")
                    mentions, roles, users = [], [], []
                    if self.exam_settings.role_id:
                        mentions.append(f"<@&{self.exam_settings.role_id}>")
                        roles.append(discord.Object(id=self.exam_settings.role_id))
                    if self.exam_settings.user_id:
                        mentions.append(f"<@{self.exam_settings.user_id}>")
                        users.append(discord.Object(id=self.exam_settings.user_id))
                    message = ("A new exam timetable is available." if notification.kind == "new_period"
                               else "Exam room assignments have changed.")
                    embed = self.build_exam_notification_embed(notification)
                    add_subscription_prompt(self.bot, embed, channel.id, self.exam_settings.role_id)
                    posted = await channel.send(
                        " ".join(mentions + [message]),
                        embed=embed,
                        allowed_mentions=discord.AllowedMentions(
                            everyone=False, roles=roles, users=users, replied_user=False,
                        ),
                    )
                    outbox.mark_notification_sent(notification.id)
                    await register_subscription_message(self.bot, posted, self.exam_settings.role_id)
                except Exception:
                    logger.exception("Exam notification %s remains pending.", notification.id)
                    break

    @tasks.loop(seconds=60)
    async def poll(self):
        if not self.exam_settings.enabled:
            return
        now = datetime.now(UTC)
        if self.policy.interval_at(now) is None:
            target = self.policy.next_run(now)
        else:
            await self.send_pending_notifications()
            try:
                result = await self.exam_monitor.check_for_exam_changes()
                logger.info("Exam check: %s (period %s).", result.kind, result.snapshot.period.label)
                self.failures = 0
                await self.send_pending_notifications()
                target = self.policy.next_run(datetime.now(UTC), after_check=True)
            except Exception:
                self.failures += 1
                logger.exception("The exam check failed. Previous state has been retained.")
                retry = datetime.now(UTC) + timedelta(seconds=min(300, 30 * 2 ** min(self.failures - 1, 4)))
                target = self.policy.next_run(retry)
        self.poll.change_interval(seconds=self.policy.delay_until(target, datetime.now(UTC)))

    @poll.before_loop
    async def before_poll(self):
        await self.bot.wait_until_ready()

    @exams.command(name="status", description="Show the current exam period and monitoring status.")
    @app_commands.guild_only()
    async def status(self, interaction: discord.Interaction):
        snapshot = self.state_store.get_current_snapshot()
        parts = []
        if snapshot:
            checked = datetime.fromisoformat(snapshot.checked_at).astimezone(self.policy.timezone)
            parts.extend([
                f"**Exam period: {discord.utils.escape_markdown(snapshot.period.label)}**",
                f"Period ID: {snapshot.period.id}. Examinations: {len(snapshot.events)}.",
                f"<{snapshot.period.program_url}>",
                f"Last successful check: {checked:%d/%m/%Y %H:%M:%S %Z}",
            ])
            if not snapshot.events:
                parts.append("Waiting for the current period's timetable to be published.")
        else:
            parts.append("The first exam check has not completed yet.")
        parts.append("Alerts: publication of a new exam period and room changes.")
        if not self.exam_settings.enabled:
            parts.append("Automatic exam checks: OFF (EXAMS_ENABLED=false).")
        elif not self.poll.is_running():
            parts.append("Automatic exam monitoring has stopped. Check the logs.")
        elif self.poll.next_iteration:
            next_time = self.poll.next_iteration.astimezone(self.policy.timezone)
            parts.append(f"Next automatic check: {next_time:%d/%m/%Y %H:%M:%S %Z}")
        parts.append(f"Pending notifications: {len(self.state_store.get_pending_notifications())}")
        await interaction.response.send_message("\n".join(parts), ephemeral=True)

    @exams.command(name="check", description="Check exam publication and rooms immediately.")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def check_for_exam_changes(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            result = await self.exam_monitor.check_for_exam_changes()
        except Exception:
            logger.exception("Manual exam check failed.")
            await interaction.followup.send("The exam check failed. Stored state has been retained.", ephemeral=True)
            return
        await self.send_pending_notifications()
        messages = {
            "baseline": "The current exam timetable was saved as the baseline without a notification.",
            "waiting": "The exam period is visible, but its timetable has no examinations yet. Waiting for publication.",
            "new_period": "A newly published exam timetable was detected.",
            "rooms": f"Room changes were detected for {len(result.changes)} examinations.",
            "unchanged": "No new exam period or room changes were detected.",
        }
        pending = len(self.state_store.get_pending_notifications())
        suffix = f" Pending notifications: {pending}." if pending else ""
        await interaction.followup.send(messages[result.kind] + suffix, ephemeral=True)


async def setup(bot):
    await bot.add_cog(Exams(bot))
