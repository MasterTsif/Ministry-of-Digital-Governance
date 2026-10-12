"""Manage notification role subscriptions through Discord reactions."""

import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands

from .subscriptions import (
    REACTION_CHOICES, SUBSCRIBE_EMOJI, SUBSCRIPTION_PROMPT,
    SubscriptionMessage, SubscriptionStore,
)

logger = logging.getLogger(__name__)


class SubscriptionError(Exception):
    pass


class Notifications(commands.Cog):
    notifications = app_commands.Group(
        name="notifications", description="Timetable notification subscriptions."
    )

    def __init__(self, bot):
        self.bot = bot
        self.settings = bot.settings
        self.subscription_store = SubscriptionStore(self.settings.database_path)
        self.lock = asyncio.Lock()
        self.last_error: str | None = None

    def accepts_subscription(self, channel_id: int, role_id: int | None) -> bool:
        return (
            self.settings.notifications_enabled
            and channel_id == self.settings.channel_id
            and role_id == self.settings.role_id
        )

    async def get_subscription_resources(self):
        guild = self.bot.get_guild(self.settings.guild_id)
        if guild is None:
            raise SubscriptionError("The configured server is unavailable.")
        role = guild.get_role(self.settings.role_id)
        if role is None:
            raise SubscriptionError("ALERT_ROLE_ID does not identify a role in this server.")
        if role.is_default() or role.managed:
            raise SubscriptionError("Use a regular notification role, not @everyone or a managed role.")
        restricted = (
            "administrator", "manage_guild", "manage_roles", "manage_channels",
            "manage_messages", "manage_webhooks", "kick_members", "ban_members",
            "moderate_members",
        )
        if any(getattr(role.permissions, permission) for permission in restricted):
            raise SubscriptionError("The notification role must not grant moderation or administration permissions.")
        me = guild.me or await guild.fetch_member(self.bot.user.id)
        if not me.guild_permissions.manage_roles:
            raise SubscriptionError("The bot needs Manage Roles in the server.")
        if role >= me.top_role:
            raise SubscriptionError("Move the bot's highest role above the notification role.")
        channel = self.bot.get_channel(self.settings.channel_id)
        if channel is None:
            channel = await self.bot.fetch_channel(self.settings.channel_id)
        if not isinstance(channel, discord.TextChannel) or channel.guild.id != guild.id:
            raise SubscriptionError("ALERT_CHANNEL_ID must be a text channel in this server.")
        permissions = channel.permissions_for(me)
        required = (
            "view_channel", "send_messages", "embed_links", "read_message_history",
            "add_reactions", "manage_messages",
        )
        missing = [name for name in required if not getattr(permissions, name)]
        if missing:
            raise SubscriptionError("Missing channel permissions: " + ", ".join(missing) + ".")
        if not role.mentionable and not permissions.mention_everyone:
            raise SubscriptionError(
                "Allow the bot to Mention @everyone, @here, and All Roles in the alert channel, "
                "or make the notification role mentionable."
            )
        return guild, role, channel

    async def install_subscription_reactions(self, message):
        for emoji in REACTION_CHOICES:
            await message.add_reaction(emoji)
        self.subscription_store.mark_ready(message.id)

    async def ensure_initial_subscription_message(self):
        async with self.lock:
            guild, role, channel = await self.get_subscription_resources()
            saved = self.subscription_store.initial(guild.id)
            if saved and not self.accepts_subscription(saved.channel_id, saved.role_id):
                self.subscription_store.forget(saved.message_id)
                saved = None
            if saved:
                try:
                    message = await channel.fetch_message(saved.message_id)
                except discord.NotFound:
                    self.subscription_store.forget(saved.message_id)
                else:
                    if message.author.id != self.bot.user.id:
                        raise SubscriptionError("The saved subscription message was not sent by this bot.")
                    await self.install_subscription_reactions(message)
                    return message
            embed = discord.Embed(
                title="Timetable notifications", description=SUBSCRIPTION_PROMPT,
                colour=discord.Colour.blue(),
            )
            embed.add_field(
                name="Notifications", value="New class timetables, new exam periods and exam room changes.",
                inline=False,
            )
            message = await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
            self.subscription_store.remember(SubscriptionMessage(message.id, guild.id, channel.id, role.id, initial=True))
            await self.install_subscription_reactions(message)
            return message

    async def register_alert_message(self, message):
        async with self.lock:
            if message.guild.id != self.settings.guild_id or message.author.id != self.bot.user.id:
                raise SubscriptionError("Only this bot's messages in the configured server can accept subscriptions.")
            self.subscription_store.remember(SubscriptionMessage(
                message.id, message.guild.id, message.channel.id, self.settings.role_id,
            ))
            await self.install_subscription_reactions(message)

    @commands.Cog.listener()
    async def on_ready(self):
        if not self.settings.notifications_enabled:
            return
        try:
            await self.ensure_initial_subscription_message()
            async with self.lock:
                _, _, channel = await self.get_subscription_resources()
                for saved in self.subscription_store.pending(self.settings.guild_id):
                    if not self.accepts_subscription(saved.channel_id, saved.role_id):
                        continue
                    try:
                        message = await channel.fetch_message(saved.message_id)
                    except discord.NotFound:
                        self.subscription_store.forget(saved.message_id)
                        continue
                    if message.author.id == self.bot.user.id:
                        await self.install_subscription_reactions(message)
            self.last_error = None
        except Exception as exc:
            self.last_error = str(exc)
            logger.exception("PdfNotification subscription setup failed.")

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        emoji = str(payload.emoji)
        if (
            not self.settings.notifications_enabled
            or payload.guild_id != self.settings.guild_id
            or payload.channel_id != self.settings.channel_id
            or emoji not in REACTION_CHOICES
            or self.bot.user is None
            or payload.user_id == self.bot.user.id
            or (payload.member is not None and payload.member.bot)
        ):
            return
        saved = self.subscription_store.get(payload.message_id)
        if saved is None or not self.accepts_subscription(saved.channel_id, saved.role_id):
            return
        try:
            async with self.lock:
                guild, role, channel = await self.get_subscription_resources()
                member = await guild.fetch_member(payload.user_id)
                if member.bot or member.pending:
                    return
                subscribed = any(existing.id == role.id for existing in member.roles)
                if emoji == SUBSCRIBE_EMOJI and not subscribed:
                    await member.add_roles(role, reason="Timetable notification subscription", atomic=True)
                elif emoji != SUBSCRIBE_EMOJI and subscribed:
                    await member.remove_roles(role, reason="Timetable notification unsubscription", atomic=True)
                try:
                    await channel.get_partial_message(payload.message_id).remove_reaction(
                        payload.emoji, discord.Object(id=member.id),
                    )
                except discord.NotFound:
                    self.subscription_store.forget(payload.message_id)
                self.last_error = None
        except discord.NotFound:
            logger.info("The reacting member or subscription message is no longer available.")
        except Exception as exc:
            self.last_error = str(exc)
            logger.exception("Could not process timetable subscription for member %s.", payload.user_id)

    @notifications.command(name="status", description="Show your timetable notification subscription.")
    @app_commands.guild_only()
    async def status(self, interaction: discord.Interaction):
        if interaction.guild_id != self.settings.guild_id:
            await interaction.response.send_message("This command is unavailable in this server.", ephemeral=True)
            return
        member = interaction.user
        subscribed = any(role.id == self.settings.role_id for role in getattr(member, "roles", ()))
        saved = self.subscription_store.initial(self.settings.guild_id)
        parts = [f"Your timetable notification role: {'ON' if subscribed else 'OFF'}."]
        parts.append("Use 🔔 to subscribe or 🔕 to unsubscribe on a timetable alert or the initial message.")
        if saved:
            parts.append(f"Initial message: <https://discord.com/channels/{saved.guild_id}/{saved.channel_id}/{saved.message_id}>")
        if self.last_error and getattr(member, "guild_permissions", discord.Permissions.none()).manage_guild:
            parts.append("A subscription operation failed. Check the bot permissions and logs.")
        await interaction.response.send_message("\n".join(parts), ephemeral=True)

    @notifications.command(name="setup", description="Create or restore the initial subscription message.")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def setup_message(self, interaction: discord.Interaction):
        if interaction.guild_id != self.settings.guild_id:
            await interaction.response.send_message("This command is unavailable in this server.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            message = await self.ensure_initial_subscription_message()
            self.last_error = None
            result = f"The subscription message is ready: <{message.jump_url}>"
        except Exception as exc:
            self.last_error = str(exc)
            logger.exception("Manual notification subscription setup failed.")
            result = str(exc) if isinstance(exc, SubscriptionError) else "Setup failed. Check the logs."
        await interaction.followup.send(result, ephemeral=True)


async def setup(bot):
    await bot.add_cog(Notifications(bot))
