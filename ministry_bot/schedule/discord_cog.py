"""Expose schedule commands and deliver PDF change alerts in Discord."""

import asyncio
from datetime import UTC, datetime, timedelta
import logging

import discord
from discord import app_commands
from discord.ext import commands, tasks

from ..shared.check_policy import CheckPolicy
from ..shared.discord_ui import styled_embed
from ..notifications.subscriptions import (
    add_subscription_prompt, get_subscription_view, register_subscription_message,
)
from .pdf_storage import PdfStateStore
from .pdf_monitor import SchedulePdfClient, SchedulePdfMonitor

logger = logging.getLogger(__name__)
SCHEDULE_ALERT_TEXT = "📢 Υπάρχει νέα έκδοση του προγράμματος μαθημάτων!"


class Schedule(commands.Cog):
    schedule = app_commands.Group(
        name="schedule", description="Πρόγραμμα μαθημάτων και ειδοποιήσεις αλλαγών."
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

    def build_schedule_notification_embed(self, snapshot, channel_id: int) -> discord.Embed:
        """Use one template for real notifications and private previews."""
        embed = styled_embed(
            title="📚 Ενημερώθηκε το πρόγραμμα μαθημάτων",
            description="Εντοπίστηκε αλλαγή στο ωρολόγιο πρόγραμμα.\nΔες το νέο αρχείο στον σύνδεσμο παρακάτω.",
            url=snapshot.url,
            colour=discord.Colour.blue(),
            timestamp=datetime.fromisoformat(snapshot.checked_at),
        )
        embed.add_field(
            name="📄 Νέο πρόγραμμα", value=f"[Άνοιγμα προγράμματος σε PDF]({snapshot.url})", inline=False,
        )
        add_subscription_prompt(self.bot, embed, channel_id, self.settings.role_id)
        return embed

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
                    embed = self.build_schedule_notification_embed(notification.snapshot, channel.id)
                    view = get_subscription_view(self.bot, channel.id, self.settings.role_id)
                    posted = await channel.send(
                        " ".join(mentions + [SCHEDULE_ALERT_TEXT]),
                        embed=embed,
                        view=view,
                        allowed_mentions=discord.AllowedMentions(
                            everyone=False, roles=roles, users=users, replied_user=False
                        ),
                    )
                    outbox.mark_notification_sent(notification.id)
                    await register_subscription_message(
                        self.bot, posted, self.settings.role_id, buttons_attached=view is not None,
                    )
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

    @schedule.command(name="status", description="Δες το τρέχον PDF και την κατάσταση παρακολούθησης.")
    @app_commands.guild_only()
    async def status(self, interaction: discord.Interaction):
        snapshot = self.state_store.get_current_snapshot()
        parts = []
        if snapshot:
            checked = datetime.fromisoformat(snapshot.checked_at).astimezone(
                self.settings.timezone
            )
            parts.extend([
                f"📄 [Άνοιγμα τρέχοντος προγράμματος]({snapshot.url})",
                f"**Τελευταίος επιτυχής έλεγχος:** {checked:%d/%m/%Y %H:%M:%S %Z}",
            ])
        else:
            parts.append("⏳ Ο πρώτος έλεγχος δεν έχει ολοκληρωθεί ακόμη.")
        if not self.settings.schedule_enabled:
            parts.append("**Αυτόματοι έλεγχοι:** ⚪ Ανενεργοί (`SCHEDULE_ENABLED=false`)")
        elif not self.poll.is_running():
            parts.append("⚠️ Η αυτόματη παρακολούθηση έχει σταματήσει. Έλεγξε τις καταγραφές του bot.")
        elif self.poll.next_iteration:
            next_time = self.poll.next_iteration.astimezone(self.settings.timezone)
            parts.append(f"**Επόμενος αυτόματος έλεγχος:** {next_time:%d/%m/%Y %H:%M:%S %Z}")
        parts.append(f"**Ειδοποιήσεις σε αναμονή:** {len(self.state_store.get_pending_notifications())}")
        embed = styled_embed("📚 Παρακολούθηση προγράμματος", "\n\n".join(parts))
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @schedule.command(name="preview", description="Δες δοκιμαστικά την ανακοίνωση νέου προγράμματος, μόνο για εσένα.")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def preview(self, interaction: discord.Interaction):
        if interaction.guild_id != self.settings.guild_id:
            await interaction.response.send_message(
                "⛔ Αυτή η εντολή δεν είναι διαθέσιμη σε αυτόν τον διακομιστή.", ephemeral=True,
            )
            return
        if interaction.channel_id != self.settings.channel_id:
            await interaction.response.send_message(
                f"📍 Χρησιμοποίησε την εντολή στο κανάλι ειδοποιήσεων <#{self.settings.channel_id}>.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)
        try:
            snapshot = self.state_store.get_current_snapshot()
            if snapshot is None:
                snapshot = await self.pdf_monitor.pdf_client.fetch_pdf_snapshot(None)
            embed = self.build_schedule_notification_embed(snapshot, self.settings.channel_id)
            view = get_subscription_view(self.bot, self.settings.channel_id, self.settings.role_id)
        except Exception:
            logger.exception("Could not build the timetable notification preview.")
            await interaction.followup.send(
                "❌ Δεν μπόρεσα να δημιουργήσω την προεπισκόπηση. Έλεγξε τις καταγραφές του bot.",
                ephemeral=True,
            )
            return
        mentions = []
        if self.settings.role_id:
            mentions.append(f"<@&{self.settings.role_id}>")
        if self.settings.user_id:
            mentions.append(f"<@{self.settings.user_id}>")
        # Preserve the interaction's guild context in the returned message.
        posted = await interaction.edit_original_response(
            content=(
                "🧪 **Δοκιμαστική προεπισκόπηση — ορατή μόνο σε εσένα.**\n"
                + " ".join(mentions + [SCHEDULE_ALERT_TEXT])
            ),
            embed=embed, view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        if view is not None:
            await register_subscription_message(
                self.bot, posted, self.settings.role_id, buttons_attached=True,
            )

    @schedule.command(name="check", description="Έλεγξε τώρα το πρόγραμμα, ακόμη και εκτός ωραρίου παρακολούθησης.")
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
                "❌ Ο έλεγχος δεν ολοκληρώθηκε. Η προηγούμενη αποθηκευμένη έκδοση διατηρήθηκε.", ephemeral=True
            )
            return
        await self.send_pending_notifications()
        messages = {
            "baseline": "📌 Αποθηκεύτηκε η αρχική έκδοση του προγράμματος, χωρίς αποστολή ειδοποίησης.",
            "changed": "📢 Εντοπίστηκε νέα έκδοση του προγράμματος μαθημάτων.",
            "unchanged": "✅ Το πρόγραμμα μαθημάτων δεν έχει αλλάξει.",
        }
        pending = len(self.state_store.get_pending_notifications())
        suffix = f"\n⏳ Ειδοποιήσεις σε αναμονή: **{pending}**." if pending else ""
        await interaction.followup.send(messages[result.kind] + suffix, ephemeral=True)


async def setup(bot):
    await bot.add_cog(Schedule(bot))
