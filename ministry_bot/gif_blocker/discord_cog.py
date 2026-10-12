"""Block GIF messages and log removals while honoring exemptions."""

from collections import OrderedDict
from datetime import UTC, datetime
import logging

import discord
from discord import app_commands
from discord.ext import commands

from ..core.config import read_env_id_list
from .gif_detector import get_gif_block_reason

logger = logging.getLogger(__name__)
RECENT_REMOVAL_LIMIT = 1000


class GifModeration(commands.Cog):
    gifs = app_commands.Group(name="gifs", description="GIF filtering commands.")

    def __init__(self, bot):
        self.bot = bot
        self.settings = bot.settings
        self.exempt_channel_ids = read_env_id_list("GIF_EXEMPT_CHANNEL_IDS")
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
        # The same list accepts_subscription channel, thread and category IDs.
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
            return "No text content."
        text = discord.utils.escape_mentions(text).replace("`", "'")
        if len(text) > limit:
            text = text[:limit - 14] + "\n[truncated]"
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
            embed = discord.Embed(
                title="GIF message removed",
                colour=discord.Colour.orange(),
                timestamp=datetime.now(UTC),
            )
            name = getattr(message.author, "display_name", str(message.author))[:100]
            embed.add_field(
                name="User", value=self._format_log_preview(f"{name}\nUser ID: {message.author.id}", 200),
            )
            embed.add_field(
                name="Channel", value=f"<#{message.channel.id}>\nChannel ID: {message.channel.id}",
            )
            embed.add_field(name="Message ID", value=str(message.id))
            embed.add_field(name="Reason", value=reason, inline=False)
            embed.add_field(
                name="Message content", value=self._format_log_preview(message.content), inline=False,
            )
            snapshots = getattr(message, "message_snapshots", [])
            files = [file.filename for item in [message, *snapshots] for file in item.attachments]
            if files:
                embed.add_field(
                    name="Attachments", value=self._format_log_preview("\n".join(files)), inline=False,
                )
            forwarded_text = "\n\n".join(snapshot.content for snapshot in snapshots if snapshot.content)
            if forwarded_text:
                embed.add_field(
                    name="Forwarded content", value=self._format_log_preview(forwarded_text), inline=False,
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
        await self.block_gif_message(message)

    @commands.Cog.listener()
    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent):
        if not self._is_enabled_in_guild(payload.guild_id):
            return
        if payload.channel_id in self.exempt_channel_ids:
            return
        if payload.message_id in self._removed or payload.message_id in self._in_flight:
            return
        if not {"content", "attachments", "embeds", "message_snapshots"}.intersection(payload.data):
            return
        if payload.data.get("author", {}).get("bot") or payload.data.get("webhook_id"):
            return
        try:
            channel = self.bot.get_channel(payload.channel_id)
            if channel is None:
                channel = await self.bot.fetch_channel(payload.channel_id)
            if await self._is_channel_exempt(channel) is not False:
                return
            fetch_message = getattr(channel, "fetch_message", None)
            if fetch_message is None:
                return
            # Fetch the complete current message, including edits outside the cache.
            message = await fetch_message(payload.message_id)
        except discord.NotFound:
            return
        except discord.Forbidden:
            logger.warning(
                "Cannot inspect edited message %s in channel %s. "
                "Grant View Channel and Read Message History and check channel overrides.",
                payload.message_id, payload.channel_id,
            )
            return
        except discord.HTTPException:
            logger.exception("Edited message %s could not be inspected.", payload.message_id)
            return
        await self.block_gif_message(message)

    @gifs.command(name="status", description="Show GIF filtering status and channel permissions.")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def status(self, interaction: discord.Interaction):
        enabled = self._is_enabled_in_guild(interaction.guild_id)
        parts = [
            "GIF filtering: " + ("ON" if enabled else "OFF (GIF_BLOCK_ENABLED=false)."),
            "Scope: accessible channels in the configured server, except exempt channels/categories and their threads.",
            "Filtering runs whenever the bot is online, independently of timetable check hours.",
            f"Messages removed since startup: {self.removed_count}.",
            f"Exemption IDs configured: {len(self.settings.gif_exempt_ids)}.",
            f"Exempt channel/category IDs configured: {len(self.exempt_channel_ids)}.",
        ]
        if interaction.channel is not None:
            exemption = await self._is_channel_exempt(interaction.channel)
            descriptions = {
                True: "EXEMPT (GIF_EXEMPT_CHANNEL_IDS).",
                False: "not exempt by channel or category ID.",
                None: "UNKNOWN (parent channel lookup failed; GIF filtering skipped).",
            }
            parts.append(
                "This channel: " + descriptions[exemption]
            )
        if self.settings.gif_log_channel_id:
            parts.append(f"GIF deletion log channel ID: {self.settings.gif_log_channel_id}.")
            parts.append(f"Log delivery failures since startup: {self.log_failures}.")
            log_channel = self.bot.get_channel(self.settings.gif_log_channel_id)
            if log_channel is not None:
                if (
                    not isinstance(log_channel, (discord.TextChannel, discord.Thread))
                    or log_channel.guild.id != self.settings.guild_id
                ):
                    parts.append("Log channel: INVALID (use a text channel in the configured server).")
                elif interaction.guild.me is not None:
                    permissions = log_channel.permissions_for(interaction.guild.me)
                    parts.append("Bot permissions in the log channel:")
                    for name, granted in (
                        ("View Channel", permissions.view_channel),
                        ("Send Messages", permissions.send_messages_in_threads if isinstance(log_channel, discord.Thread) else permissions.send_messages),
                        ("Embed Links", permissions.embed_links),
                    ):
                        parts.append(f"{name}: {'OK' if granted else 'MISSING'}")
            else:
                parts.append("Log channel is not cached. Verify its ID and the bot's channel access.")
        else:
            parts.append("GIF deletion logs: OFF (GIF_LOG_CHANNEL_ID is blank).")
        member = interaction.guild.me
        if member is not None and interaction.channel is not None:
            permissions = interaction.channel.permissions_for(member)
            parts.append("Bot permissions in this channel:")
            for name, granted in (
                ("View Channel", permissions.view_channel),
                ("Manage Messages", permissions.manage_messages),
                ("Read Message History", permissions.read_message_history),
            ):
                parts.append(f"{name}: {'OK' if granted else 'MISSING'}")
        await interaction.response.send_message("\n".join(parts), ephemeral=True)


async def setup(bot):
    await bot.add_cog(GifModeration(bot))
