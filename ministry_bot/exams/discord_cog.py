"""Expose exam commands and deliver exam publication and room alerts."""

import asyncio
from datetime import UTC, datetime, timedelta
import logging
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks

from .exam_monitor import ExamCalendarClient, ExamStateStore, ExamMonitor
from ..notifications.subscriptions import (
    add_subscription_prompt, get_subscription_view, register_subscription_message,
)
from ..shared.discord_ui import FOOTER_TEXT, styled_embed

logger = logging.getLogger(__name__)


class Exams(commands.Cog):
    exams = app_commands.Group(name="exams", description="Πρόγραμμα εξεταστικής και αλλαγές αιθουσών.")

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
        title = "🎓 Δημοσιεύτηκε νέο πρόγραμμα εξεταστικής" if published else "📍 Αλλαγές αιθουσών εξετάσεων"
        embed = styled_embed(
            title=title,
            description=(
                f"**Εξεταστική περίοδος:** {discord.utils.escape_markdown(snapshot.period.label)}\n\n"
                + (f"Το πρόγραμμα περιλαμβάνει **{len(snapshot.events)} εξετάσεις**."
                   if published else f"Εντοπίστηκαν αλλαγές αιθουσών σε **{len(notification.changes)} εξετάσεις**.")
            ),
            url=snapshot.period.program_url,
            colour=discord.Colour.purple() if published else discord.Colour.orange(),
            timestamp=datetime.fromisoformat(snapshot.checked_at),
        )
        for change in notification.changes[:8]:
            event = change.after
            start = datetime.fromisoformat(event.starts_at).astimezone(ZoneInfo("Europe/Athens"))
            end = datetime.fromisoformat(event.ends_at).astimezone(ZoneInfo("Europe/Athens"))
            old_rooms = ", ".join(change.before.rooms) or "Δεν έχει οριστεί"
            new_rooms = ", ".join(event.rooms) or "Δεν έχει οριστεί"
            value = (
                f"🗓️ **{start:%d/%m/%Y}** · {start:%H:%M}–{end:%H:%M} (ώρα Ελλάδας)\n"
                f"**Πριν:** {discord.utils.escape_markdown(old_rooms)}\n"
                f"**Τώρα:** {discord.utils.escape_markdown(new_rooms)}"
            )
            embed.add_field(name=event.title[:180], value=value[:500], inline=False)
        if len(notification.changes) > 8:
            embed.set_footer(text=f"Εμφανίζονται 8 από {len(notification.changes)} αλλαγές. Δες το πλήρες πρόγραμμα. • {FOOTER_TEXT}")
        embed.add_field(name="📄 Πλήρες πρόγραμμα", value=f"[Άνοιγμα προγράμματος εξεταστικής]({snapshot.period.program_url})", inline=False)
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
                    message = ("📢 Είναι διαθέσιμο νέο πρόγραμμα εξεταστικής!" if notification.kind == "new_period"
                               else "📢 Άλλαξαν οι αίθουσες σε εξετάσεις. Δες τις λεπτομέρειες παρακάτω.")
                    embed = self.build_exam_notification_embed(notification)
                    add_subscription_prompt(self.bot, embed, channel.id, self.exam_settings.role_id)
                    view = get_subscription_view(self.bot, channel.id, self.exam_settings.role_id)
                    posted = await channel.send(
                        " ".join(mentions + [message]),
                        embed=embed,
                        view=view,
                        allowed_mentions=discord.AllowedMentions(
                            everyone=False, roles=roles, users=users, replied_user=False,
                        ),
                    )
                    outbox.mark_notification_sent(notification.id)
                    await register_subscription_message(
                        self.bot, posted, self.exam_settings.role_id, buttons_attached=view is not None,
                    )
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

    @exams.command(name="status", description="Δες την τρέχουσα εξεταστική και την κατάσταση παρακολούθησης.")
    @app_commands.guild_only()
    async def status(self, interaction: discord.Interaction):
        snapshot = self.state_store.get_current_snapshot()
        parts = []
        if snapshot:
            checked = datetime.fromisoformat(snapshot.checked_at).astimezone(self.policy.timezone)
            parts.extend([
                f"**Εξεταστική περίοδος:** {discord.utils.escape_markdown(snapshot.period.label)}",
                f"**Κωδικός περιόδου:** `{snapshot.period.id}` · **Εξετάσεις:** {len(snapshot.events)}",
                f"📄 [Άνοιγμα προγράμματος εξεταστικής]({snapshot.period.program_url})",
                f"**Τελευταίος επιτυχής έλεγχος:** {checked:%d/%m/%Y %H:%M:%S %Z}",
            ])
            if not snapshot.events:
                parts.append("⏳ Αναμένεται η δημοσίευση των εξετάσεων της τρέχουσας περιόδου.")
        else:
            parts.append("⏳ Ο πρώτος έλεγχος εξεταστικής δεν έχει ολοκληρωθεί ακόμη.")
        parts.append("**Ειδοποιήσεις για:** νέα εξεταστική περίοδο και αλλαγές αιθουσών.")
        if not self.exam_settings.enabled:
            parts.append("**Αυτόματοι έλεγχοι:** ⚪ Ανενεργοί (`EXAMS_ENABLED=false`)")
        elif not self.poll.is_running():
            parts.append("⚠️ Η αυτόματη παρακολούθηση εξεταστικής έχει σταματήσει. Έλεγξε τις καταγραφές του bot.")
        elif self.poll.next_iteration:
            next_time = self.poll.next_iteration.astimezone(self.policy.timezone)
            parts.append(f"**Επόμενος αυτόματος έλεγχος:** {next_time:%d/%m/%Y %H:%M:%S %Z}")
        parts.append(f"**Ειδοποιήσεις σε αναμονή:** {len(self.state_store.get_pending_notifications())}")
        embed = styled_embed("🎓 Παρακολούθηση εξεταστικής", "\n\n".join(parts), colour=discord.Colour.purple())
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @exams.command(name="check", description="Έλεγξε τώρα για νέα εξεταστική περίοδο και αλλαγές αιθουσών.")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def check_for_exam_changes(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            result = await self.exam_monitor.check_for_exam_changes()
        except Exception:
            logger.exception("Manual exam check failed.")
            await interaction.followup.send("❌ Ο έλεγχος εξεταστικής δεν ολοκληρώθηκε. Η προηγούμενη αποθηκευμένη κατάσταση διατηρήθηκε.", ephemeral=True)
            return
        await self.send_pending_notifications()
        messages = {
            "baseline": "📌 Αποθηκεύτηκε η αρχική έκδοση της εξεταστικής, χωρίς αποστολή ειδοποίησης.",
            "waiting": "⏳ Η εξεταστική περίοδος εμφανίζεται, αλλά οι εξετάσεις δεν έχουν δημοσιευτεί ακόμη.",
            "new_period": "🎓 Εντοπίστηκε νέο πρόγραμμα εξεταστικής.",
            "rooms": f"📍 Εντοπίστηκαν αλλαγές αιθουσών σε **{len(result.changes)} εξετάσεις**.",
            "unchanged": "✅ Δεν εντοπίστηκε νέα εξεταστική περίοδος ή αλλαγή αιθουσών.",
        }
        pending = len(self.state_store.get_pending_notifications())
        suffix = f"\n⏳ Ειδοποιήσεις σε αναμονή: **{pending}**." if pending else ""
        await interaction.followup.send(messages[result.kind] + suffix, ephemeral=True)


async def setup(bot):
    await bot.add_cog(Exams(bot))
