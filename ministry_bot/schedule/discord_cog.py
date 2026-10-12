"""Expose schedule commands and deliver PDF change alerts in Discord."""

import asyncio
from datetime import UTC, datetime, timedelta
import logging

import discord
from discord import app_commands
from discord.ext import commands, tasks

from ..shared.check_policy import CheckPolicy
from ..notifications.subscriptions import add_subscription_prompt, register_subscription_message
from .pdf_storage import PdfStateStore
from .pdf_monitor import SchedulePdfClient, SchedulePdfMonitor

logger = logging.getLogger(__name__)


class Schedule(commands.Cog):
    schedule = app_commands.Group(
        name="schedule", description="Class timetable commands."
    )

    def __init__(self, bot):
        self.bot = bot
        self.settings = bot.settings
        self.state_store = PdfStateStore(self.settings.database_path)
        self.pdf_monitor = SchedulePdfMonitor(
            SchedulePdfClient(bot.http_session, self.settings.api_url), self.state_store
        )
        self.policy = CheckPolicy(
            timezone=self.settings.timezone,
            morning_start=self.settings.morning_start,
            evening_start=self.settings.evening_start,
            night_start=self.settings.night_start,
            day_interval=self.settings.day_interval,
            evening_interval=self.settings.evening_interval,
            active_weekdays=self.settings.active_weekdays,
        )
        self.delivery_lock = asyncio.Lock()
        self.failures = 0

    async def cog_load(self):
        if self.settings.schedule_enabled:
            self.poll.start()

    async def cog_unload(self):
        self.poll.cancel()
        task = self.poll.get_task()
        if task is not None:
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def send_pending_notifications(self):
        outbox = self.state_store
        async with self.delivery_lock:
            for notification in outbox.get_pending_notifications():
                try:
                    channel = self.bot.get_channel(self.settings.channel_id)
                    if channel is None:
                        channel = await self.bot.fetch_channel(self.settings.channel_id)
                    if not isinstance(channel, (discord.TextChannel, discord.Thread)):
                        raise ValueError("ALERT_CHANNEL_ID must refer to a text channel or thread.")
                    if self.settings.guild_id and channel.guild.id != self.settings.guild_id:
                        raise ValueError("The alert channel belongs to a different server.")
                    mentions = []
                    roles = []
                    users = []
                    if self.settings.role_id:
                        mentions.append(f"<@&{self.settings.role_id}>")
                        roles.append(discord.Object(id=self.settings.role_id))
                    if self.settings.user_id:
                        mentions.append(f"<@{self.settings.user_id}>")
                        users.append(discord.Object(id=self.settings.user_id))
                    snapshot = notification.snapshot
                    embed = discord.Embed(
                        title="Class timetable updated",
                        description="An updated class timetable PDF is available.",
                        url=snapshot.url,
                        colour=discord.Colour.blue(),
                        timestamp=datetime.fromisoformat(snapshot.checked_at),
                    )
                    embed.add_field(
                        name="PDF", value=f"[Open timetable]({snapshot.url})", inline=False
                    )
                    message = "A change to the timetable PDF was detected."
                    add_subscription_prompt(self.bot, embed, channel.id, self.settings.role_id)
                    posted = await channel.send(
                        " ".join(mentions + [message]),
                        embed=embed,
                        allowed_mentions=discord.AllowedMentions(
                            everyone=False, roles=roles, users=users, replied_user=False
                        ),
                    )
                    outbox.mark_notification_sent(notification.id)
                    await register_subscription_message(self.bot, posted, self.settings.role_id)
                except Exception:
                    logger.exception(
                        "Notification %s remains pending.", notification.id
                    )
                    break

    @tasks.loop(seconds=60)
    async def poll(self):
        if not self.settings.schedule_enabled:
            return
        now = datetime.now(UTC)
        if self.policy.interval_at(now) is None:
            target = self.policy.next_run(now)
        else:
            await self.send_pending_notifications()
            try:
                result = await self.pdf_monitor.check_for_pdf_changes()
                logger.info("Timetable check: %s.", result.kind)
                self.failures = 0
                await self.send_pending_notifications()
                target = self.policy.next_run(datetime.now(UTC), after_check=True)
            except Exception:
                self.failures += 1
                logger.exception("The check failed. Previous state has been retained.")
                retry = datetime.now(UTC) + timedelta(
                    seconds=min(300, 30 * (2 ** min(self.failures - 1, 4)))
                )
                target = self.policy.next_run(retry)
        self.poll.change_interval(
            seconds=self.policy.delay_until(target, datetime.now(UTC))
        )

    @poll.before_loop
    async def before_poll(self):
        await self.bot.wait_until_ready()

    @schedule.command(name="status", description="Show the current PDF and monitoring status.")
    @app_commands.guild_only()
    async def status(self, interaction: discord.Interaction):
        snapshot = self.state_store.get_current_snapshot()
        parts = []
        if snapshot:
            checked = datetime.fromisoformat(snapshot.checked_at).astimezone(
                self.settings.timezone
            )
            parts.extend([
                "**Class timetable PDF**",
                f"<{snapshot.url}>",
                f"Last successful check: {checked:%d/%m/%Y %H:%M:%S %Z}",
            ])
        else:
            parts.append("The first check has not completed yet.")
        if not self.settings.schedule_enabled:
            parts.append("Automatic checks: OFF (SCHEDULE_ENABLED=false).")
        elif not self.poll.is_running():
            parts.append("Automatic monitoring has stopped. Check the logs.")
        elif self.poll.next_iteration:
            next_time = self.poll.next_iteration.astimezone(self.settings.timezone)
            parts.append(f"Next automatic check: {next_time:%d/%m/%Y %H:%M:%S %Z}")
        parts.append(f"Pending notifications: {len(self.state_store.get_pending_notifications())}")
        await interaction.response.send_message("\n".join(parts), ephemeral=True)

    @schedule.command(name="check", description="Check manually, including outside automatic check hours.")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def check_for_pdf_changes(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            result = await self.pdf_monitor.check_for_pdf_changes()
        except Exception:
            logger.exception("Manual timetable check failed.")
            await interaction.followup.send(
                "The check failed. The stored version has been retained.", ephemeral=True
            )
            return
        await self.send_pending_notifications()
        messages = {
            "baseline": "The baseline version was saved without a notification.",
            "changed": "A different PDF was detected.",
            "unchanged": "The PDF is unchanged.",
        }
        pending = len(self.state_store.get_pending_notifications())
        suffix = f" Pending notifications: {pending}." if pending else ""
        await interaction.followup.send(messages[result.kind] + suffix, ephemeral=True)


async def setup(bot):
    await bot.add_cog(Schedule(bot))
