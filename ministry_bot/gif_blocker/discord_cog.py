"""Block GIF messages and log removals while honoring exemptions."""

from collections import OrderedDict
from datetime import UTC, datetime
import logging

import discord
from discord import app_commands
from discord.ext import commands

from .gif_detector import get_gif_block_reason
from ..shared.discord_ui import GIF_REASON_LABELS, permission_status, styled_embed

logger = logging.getLogger(__name__)
RECENT_REMOVAL_LIMIT = 1000


class GifModeration(commands.Cog):
    gifs = app_commands.Group(name="gifs", description="Φίλτρο GIF και κατάσταση δικαιωμάτων.")

    def __init__(self, bot):
        self.bot = bot
        self.settings = bot.settings
        self.exempt_channel_ids = self.settings.gif_exempt_channel_ids
        self.removed_count = 0
        self.log_failures = 0
        self._in_flight: set[int] = set()
        self._removed: OrderedDict[int, None] = OrderedDict()

    def _remember_removed_message(self, message_id: int):
        self._removed[message_id] = None
        if len(self._removed) > RECENT_REMOVAL_LIMIT:
            self._removed.popitem(last=False)

    def _is_enabled_in_guild(self, guild_id: int | None) -> bool:
        return bool(
            self.settings.gif_block_enabled
            and self.settings.guild_id
            and guild_id == self.settings.guild_id
        )

    async def _is_channel_exempt(self, channel) -> bool | None:
        # The same list accepts channel, thread and category IDs.
        ids = self.exempt_channel_ids
        if not ids:
            return False
        parent_id = getattr(channel, "parent_id", None)
        if getattr(channel, "id", None) in ids or parent_id in ids:
            return True
        if parent_id is None:
            return getattr(channel, "category_id", None) in ids
        # Thread.category_id can raise when its parent is not cached.
        parent = getattr(channel, "parent", None)
        if parent is None:
            parent = self.bot.get_channel(parent_id)
        if parent is None:
            try:
                parent = await self.bot.fetch_channel(parent_id)
            except discord.HTTPException:
                logger.warning(
                    "Could not check category exemptions for thread %s (parent %s); "
                    "GIF moderation was skipped.",
                    getattr(channel, "id", None), parent_id,
                )
                return None
        return getattr(parent, "category_id", None) in ids

    async def _is_author_exempt(self, message: discord.Message) -> bool:
        ids = self.settings.gif_exempt_ids
        if not ids:
            return False
        if message.author.id in ids:
            return True
        roles = getattr(message.author, "roles", None)
        if roles is None:
            try:
                # REST message reads may return a User without guild roles.
                member = await message.guild.fetch_member(message.author.id)
            except discord.HTTPException:
                logger.warning(
                    "Could not check exemption roles for user %s in server %s; "
                    "GIF moderation was skipped for message %s.",
                    message.author.id, message.guild.id, message.id,
                )
                return True
            roles = member.roles
        return any(role.id in ids for role in roles)

    @staticmethod
    def _format_log_preview(text: str, limit: int = 900) -> str:
        if not text:
            return "Χωρίς περιεχόμενο κειμένου."
        text = discord.utils.escape_mentions(text).replace("`", "'")
        if len(text) > limit:
            suffix = "\n[περικοπή]"
            text = text[:limit - len(suffix)] + suffix
        return f"```\n{text}\n```"

    async def _send_deletion_log(self, message: discord.Message, reason: str):
        channel_id = self.settings.gif_log_channel_id
        if channel_id is None:
            return
        try:
            channel = self.bot.get_channel(channel_id)
            if channel is None:
                channel = await self.bot.fetch_channel(channel_id)
            if (
                not isinstance(channel, (discord.TextChannel, discord.Thread))
                or channel.guild.id != self.settings.guild_id
            ):
                raise ValueError(
                    "GIF_LOG_CHANNEL_ID must be a text channel or thread in DISCORD_GUILD_ID."
                )
            embed = styled_embed(
                title="🧹 Αφαιρέθηκε μήνυμα με GIF",
                colour=discord.Colour.orange(),
                timestamp=datetime.now(UTC),
            )
            name = getattr(message.author, "display_name", str(message.author))[:100]
            embed.add_field(
                name="👤 Χρήστης", value=self._format_log_preview(f"{name}\nID χρήστη: {message.author.id}", 200),
            )
            embed.add_field(
                name="📍 Κανάλι", value=f"<#{message.channel.id}>\nID καναλιού: {message.channel.id}",
            )
            embed.add_field(name="🔎 ID μηνύματος", value=str(message.id))
            embed.add_field(name="🚫 Αιτία", value=GIF_REASON_LABELS.get(reason, "Εντοπίστηκε GIF"), inline=False)
            embed.add_field(
                name="💬 Περιεχόμενο μηνύματος", value=self._format_log_preview(message.content), inline=False,
            )
            snapshots = getattr(message, "message_snapshots", [])
            files = [file.filename for item in [message, *snapshots] for file in item.attachments]
            if files:
                embed.add_field(
                    name="📎 Συνημμένα", value=self._format_log_preview("\n".join(files)), inline=False,
                )
            forwarded_text = "\n\n".join(snapshot.content for snapshot in snapshots if snapshot.content)
            if forwarded_text:
                embed.add_field(
                    name="↪️ Προωθημένο περιεχόμενο", value=self._format_log_preview(forwarded_text), inline=False,
                )
            await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except (discord.HTTPException, ValueError):
            self.log_failures += 1
            logger.exception(
                "Message %s was removed, but its GIF deletion log could not be sent "
                "to channel %s. Check the channel ID, server, View Channel, Send Messages "
                "and Embed Links permissions.",
                message.id, channel_id,
            )

    async def block_gif_message(self, message: discord.Message):
        guild_id = message.guild.id if message.guild is not None else None
        if not self._is_enabled_in_guild(guild_id) or message.author.bot or message.webhook_id:
            return
        if await self._is_channel_exempt(message.channel) is not False:
            return
        if message.id in self._in_flight or message.id in self._removed:
            return
        reason = get_gif_block_reason(message)
        if reason is None:
            return
        self._in_flight.add(message.id)
        try:
            if await self._is_author_exempt(message):
                return
            await message.delete()
        except discord.NotFound:
            self._remember_removed_message(message.id)
        except discord.Forbidden:
            logger.warning(
                "Cannot remove GIF message %s in channel %s. "
                "Grant View Channel and Manage Messages and check channel overrides.",
                message.id, message.channel.id,
            )
        except discord.HTTPException:
            logger.exception("GIF message %s could not be removed.", message.id)
        else:
            self._remember_removed_message(message.id)
            self.removed_count += 1
            logger.info(
                "Removed message %s from channel %s in server %s: %s.",
                message.id, message.channel.id, guild_id, reason,
            )
            await self._send_deletion_log(message, reason)
        finally:
            self._in_flight.discard(message.id)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """Moderate new messages only; never recheck edits or embed updates."""
        await self.block_gif_message(message)

    @gifs.command(name="status", description="Δες την κατάσταση του φίλτρου GIF και τα δικαιώματα καναλιών.")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def status(self, interaction: discord.Interaction):
        enabled = self._is_enabled_in_guild(interaction.guild_id)
        parts = [
            "**Κατάσταση:** " + ("🟢 Ενεργό" if enabled else "⚪ Ανενεργό (`GIF_BLOCK_ENABLED=false`)"),
            "Το φίλτρο καλύπτει τα προσβάσιμα κανάλια του διακομιστή, εκτός από τις εξαιρέσεις και τα νήματά τους.",
            "Λειτουργεί όσο το bot είναι ενεργό, ανεξάρτητα από το ωράριο ελέγχου προγραμμάτων.",
            "Ελέγχει μόνο νέα μηνύματα. Δεν επανελέγχει παλιότερα μηνύματα ή επεξεργασίες.",
            f"**Διαγραφές από την εκκίνηση:** {self.removed_count}",
            f"**Εξαιρέσεις χρηστών/ρόλων:** {len(self.settings.gif_exempt_ids)}",
            f"**Εξαιρέσεις καναλιών/κατηγοριών:** {len(self.exempt_channel_ids)}",
        ]
        if interaction.channel is not None:
            exemption = await self._is_channel_exempt(interaction.channel)
            descriptions = {
                True: "εξαιρείται (`GIF_EXEMPT_CHANNEL_IDS`)",
                False: "δεν εξαιρείται από τις ρυθμίσεις καναλιών/κατηγοριών",
                None: "άγνωστη κατάσταση — ο έλεγχος του γονικού καναλιού απέτυχε και το φίλτρο παρακάμπτεται",
            }
            parts.append(
                "**Τρέχον κανάλι:** " + descriptions[exemption]
            )
        if self.settings.gif_log_channel_id:
            parts.append(f"**Κανάλι καταγραφής διαγραφών:** <#{self.settings.gif_log_channel_id}>")
            parts.append(f"**Αποτυχίες αποστολής καταγραφών:** {self.log_failures}")
            log_channel = self.bot.get_channel(self.settings.gif_log_channel_id)
            if log_channel is not None:
                if (
                    not isinstance(log_channel, (discord.TextChannel, discord.Thread))
                    or log_channel.guild.id != self.settings.guild_id
                ):
                    parts.append("⚠️ Το κανάλι καταγραφής δεν είναι έγκυρο. Επίλεξε κανάλι κειμένου στον επιλεγμένο διακομιστή.")
                elif interaction.guild.me is not None:
                    permissions = log_channel.permissions_for(interaction.guild.me)
                    parts.append("\n**Δικαιώματα στο κανάλι καταγραφής:**")
                    for name, granted in (
                        ("view_channel", permissions.view_channel),
                        ("send_messages", permissions.send_messages_in_threads if isinstance(log_channel, discord.Thread) else permissions.send_messages),
                        ("embed_links", permissions.embed_links),
                    ):
                        parts.append(permission_status(name, granted))
            else:
                parts.append("⚠️ Το κανάλι καταγραφής δεν έχει φορτωθεί. Έλεγξε το ID και την πρόσβαση του bot.")
        else:
            parts.append("**Καταγραφή διαγραφών:** ⚪ Ανενεργή (`GIF_LOG_CHANNEL_ID` κενό)")
        member = interaction.guild.me
        if member is not None and interaction.channel is not None:
            permissions = interaction.channel.permissions_for(member)
            parts.append("\n**Δικαιώματα στο τρέχον κανάλι:**")
            for name, granted in (
                ("view_channel", permissions.view_channel),
                ("manage_messages", permissions.manage_messages),
            ):
                parts.append(permission_status(name, granted))
        embed = styled_embed("🧹 Κατάσταση φίλτρου GIF", "\n".join(parts), colour=discord.Colour.orange())
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot):
    await bot.add_cog(GifModeration(bot))
