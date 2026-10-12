"""Manage notification role subscriptions through persistent Discord buttons."""

import asyncio
import logging
from weakref import WeakSet

import discord
from discord import app_commands
from discord.ext import commands

from .subscriptions import (
    SUBSCRIBE_LABEL, SUBSCRIPTION_PROMPT, UNSUBSCRIBE_LABEL, SubscriptionMessage,
    SubscriptionStore,
)
from .subscription_view import SubscriptionView
from ..shared.discord_ui import PERMISSION_LABELS, styled_embed

logger = logging.getLogger(__name__)


class SubscriptionError(Exception):
    """Keep English diagnostics separate from the Greek user response."""

    def __init__(self, message: str, user_message: str):
        super().__init__(message)
        self.user_message = user_message


class Notifications(commands.Cog):
    notifications = app_commands.Group(
        name="notifications", description="Εγγραφή στις ειδοποιήσεις προγράμματος και εξεταστικής."
    )

    def __init__(self, bot):
        self.bot = bot
        self.settings = bot.settings
        self.subscription_store = SubscriptionStore(self.settings.database_path)
        self.lock = asyncio.Lock()
        self.last_error: str | None = None
        self.views: WeakSet[SubscriptionView] = WeakSet()
        self.persistent_view = self.create_subscription_view()

    async def cog_load(self):
        self.bot.add_view(self.persistent_view)

    async def cog_unload(self):
        for view in tuple(self.views):
            view.stop()

    def create_subscription_view(self) -> SubscriptionView:
        view = SubscriptionView(self)
        self.views.add(view)
        return view

    def accepts_subscription(self, channel_id: int, role_id: int | None) -> bool:
        return (
            self.settings.notifications_enabled
            and role_id == self.settings.role_id
            and (channel_id, role_id) in {
                (self.settings.channel_id, self.settings.role_id),
                (self.settings.exams.channel_id, self.settings.exams.role_id),
            }
        )

    async def get_subscription_resources(self, channel_id: int | None = None):
        channel_id = channel_id if channel_id is not None else self.settings.channel_id
        guild = self.bot.get_guild(self.settings.guild_id)
        if guild is None:
            raise SubscriptionError(
                "The configured server is unavailable.",
                "Ο επιλεγμένος διακομιστής δεν είναι διαθέσιμος.",
            )
        role = guild.get_role(self.settings.role_id)
        if role is None:
            raise SubscriptionError(
                "ALERT_ROLE_ID does not identify a role in this server.",
                "Δεν βρέθηκε ο ρόλος ειδοποιήσεων. Έλεγξε τη ρύθμιση `ALERT_ROLE_ID`.",
            )
        if role.is_default() or role.managed:
            raise SubscriptionError(
                "Use a regular notification role, not @everyone or a managed role.",
                "Επίλεξε έναν απλό ρόλο ειδοποιήσεων, αντί για `@everyone` ή ρόλο που διαχειρίζεται κάποια εφαρμογή.",
            )
        restricted = (
            "administrator", "manage_guild", "manage_roles", "manage_channels",
            "manage_messages", "manage_webhooks", "kick_members", "ban_members",
            "moderate_members",
        )
        if any(getattr(role.permissions, permission) for permission in restricted):
            raise SubscriptionError(
                "The notification role must not grant moderation or administration permissions.",
                "Ο ρόλος ειδοποιήσεων πρέπει να έχει μόνο απλά δικαιώματα, χωρίς δυνατότητες διαχείρισης ή συντονισμού.",
            )
        me = guild.me or await guild.fetch_member(self.bot.user.id)
        if not me.guild_permissions.manage_roles:
            raise SubscriptionError(
                "The bot needs Manage Roles in the server.",
                "Το bot χρειάζεται το δικαίωμα **Διαχείριση ρόλων** στον διακομιστή.",
            )
        if role >= me.top_role:
            raise SubscriptionError(
                "Move the bot's highest role above the notification role.",
                "Μετακίνησε τον ανώτερο ρόλο του bot πάνω από τον ρόλο ειδοποιήσεων στη λίστα ρόλων.",
            )
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            channel = await self.bot.fetch_channel(channel_id)
        if not isinstance(channel, discord.TextChannel) or channel.guild.id != guild.id:
            raise SubscriptionError(
                "The notification destination must be a text channel in this server.",
                "Το κανάλι ειδοποιήσεων πρέπει να είναι κανάλι κειμένου αυτού του διακομιστή.",
            )
        permissions = channel.permissions_for(me)
        required = (
            "view_channel", "send_messages", "embed_links", "read_message_history",
        )
        missing = [name for name in required if not getattr(permissions, name)]
        if missing:
            raise SubscriptionError(
                "Missing channel permissions: " + ", ".join(missing) + ".",
                "Λείπουν δικαιώματα στο κανάλι: **"
                + ", ".join(PERMISSION_LABELS[name] for name in missing) + "**.",
            )
        if not role.mentionable and not permissions.mention_everyone:
            raise SubscriptionError(
                "Allow the bot to Mention @everyone, @here, and All Roles in the alert channel, "
                "or make the notification role mentionable.",
                "Επίτρεψε στο bot να αναφέρει όλους τους ρόλους στο κανάλι ειδοποιήσεων "
                "ή ενεργοποίησε τη δυνατότητα αναφοράς του ρόλου ειδοποιήσεων.",
            )
        return guild, role, channel

    async def install_subscription_buttons(self, message):
        await message.edit(view=self.create_subscription_view())
        self.subscription_store.mark_ready(message.id)

    def has_subscription_buttons(self, message) -> bool:
        buttons = [component for row in message.components for component in row.children]
        expected = self.persistent_view.children
        return len(buttons) == len(expected) and all(
            isinstance(button, discord.Button)
            and button.custom_id == item.custom_id
            and button.label == item.label
            and button.style == item.style
            and str(button.emoji) == str(item.emoji)
            and not button.disabled
            for button, item in zip(buttons, expected)
        )

    def build_subscription_embed(self) -> discord.Embed:
        """Build the reusable subscription panel without a changing timestamp."""
        embed = styled_embed(
            "Ειδοποιήσεις προγραμμάτων σχολής",
            SUBSCRIPTION_PROMPT,
            colour=discord.Colour.teal(),
        )
        embed.add_field(
            name="📬 Με ενεργές ειδοποιήσεις, θα γίνεσαι tag όταν:",
            value=(
                "• Δημοσιεύεται ή ενημερώνεται το πρόγραμμα μαθημάτων\n"
                "• Δημοσιεύεται νέο πρόγραμμα εξεταστικής\n"
                "• Αλλάζουν οι αίθουσες στο πρόγραμμα εξεταστικής"
            ),
            inline=False,
        )
        return embed

    async def ensure_initial_subscription_message(self):
        async with self.lock:
            guild, role, channel = await self.get_subscription_resources()
            embed = self.build_subscription_embed()
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
                        raise SubscriptionError(
                            "The saved subscription message was not sent by this bot.",
                            "Το αποθηκευμένο μήνυμα εγγραφής δεν ανήκει σε αυτό το bot.",
                        )
                    current_embeds = message.embeds
                    if (
                        len(current_embeds) != 1
                        or current_embeds[0].to_dict() != embed.to_dict()
                        or not self.has_subscription_buttons(message)
                    ):
                        await message.edit(
                            embed=embed, view=self.create_subscription_view(),
                            allowed_mentions=discord.AllowedMentions.none(),
                        )
                    self.subscription_store.mark_ready(message.id)
                    return message
            message = await channel.send(
                embed=embed, view=self.create_subscription_view(),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            self.subscription_store.remember(SubscriptionMessage(
                message.id, guild.id, channel.id, role.id, initial=True, ready=True,
            ))
            return message

    async def register_alert_message(self, message, *, buttons_attached: bool = False):
        async with self.lock:
            if (
                message.guild.id != self.settings.guild_id
                or message.author.id != self.bot.user.id
                or not self.accepts_subscription(message.channel.id, self.settings.role_id)
            ):
                raise SubscriptionError(
                    "Only this bot's messages in the configured server can accept subscriptions.",
                    "Οι εγγραφές υποστηρίζονται μόνο σε μηνύματα αυτού του bot στον επιλεγμένο διακομιστή.",
                )
            self.subscription_store.remember(SubscriptionMessage(
                message.id, message.guild.id, message.channel.id, self.settings.role_id,
                ready=buttons_attached,
            ))
            if not buttons_attached:
                await self.install_subscription_buttons(message)

    @commands.Cog.listener()
    async def on_ready(self):
        if not self.settings.notifications_enabled:
            return
        try:
            await self.ensure_initial_subscription_message()
            async with self.lock:
                for saved in self.subscription_store.pending(self.settings.guild_id):
                    if not self.accepts_subscription(saved.channel_id, saved.role_id):
                        continue
                    _, _, channel = await self.get_subscription_resources(saved.channel_id)
                    try:
                        message = await channel.fetch_message(saved.message_id)
                    except discord.NotFound:
                        self.subscription_store.forget(saved.message_id)
                        continue
                    if message.author.id == self.bot.user.id:
                        await self.install_subscription_buttons(message)
            self.last_error = None
        except Exception as exc:
            self.last_error = str(exc)
            logger.exception("Notification subscription setup failed.")

    async def change_subscription(self, interaction: discord.Interaction, *, subscribe: bool):
        """Acknowledge promptly and confirm only a completed role operation."""
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            async with self.lock:
                message = interaction.message
                saved = self.subscription_store.get(message.id) if message is not None else None
                if (
                    saved is None
                    or self.bot.user is None
                    or interaction.guild_id != self.settings.guild_id
                    or saved.guild_id != interaction.guild_id
                    or saved.channel_id != interaction.channel_id
                    or message.author.id != self.bot.user.id
                    or not self.accepts_subscription(saved.channel_id, saved.role_id)
                ):
                    raise SubscriptionError(
                        "The interaction does not belong to an active subscription message.",
                        "Αυτό το μήνυμα εγγραφής δεν είναι διαθέσιμο. Δοκίμασε το αρχικό μήνυμα ειδοποιήσεων.",
                    )
                guild, role, _ = await self.get_subscription_resources(saved.channel_id)
                member = await guild.fetch_member(interaction.user.id)
                if member.bot or member.pending:
                    raise SubscriptionError(
                        "Only human members who completed server screening can subscribe.",
                        "Ολοκλήρωσε πρώτα την αποδοχή των κανόνων του διακομιστή και δοκίμασε ξανά.",
                    )
                subscribed = any(existing.id == role.id for existing in member.roles)
                if subscribe and not subscribed:
                    await member.add_roles(role, reason="Εγγραφή στις ειδοποιήσεις προγράμματος", atomic=True)
                    result = f"✅ Οι ειδοποιήσεις ενεργοποιήθηκαν!\nΣου δόθηκε ο ρόλος {role.mention}."
                elif not subscribe and subscribed:
                    await member.remove_roles(role, reason="Διαγραφή από τις ειδοποιήσεις προγράμματος", atomic=True)
                    result = f"🔕 Οι ειδοποιήσεις απενεργοποιήθηκαν.\nΟ ρόλος {role.mention} αφαιρέθηκε."
                elif subscribe:
                    result = f"ℹ️ Οι ειδοποιήσεις σου είναι ήδη ενεργές.\nΈχεις ήδη τον ρόλο {role.mention}."
                else:
                    result = f"ℹ️ Οι ειδοποιήσεις σου είναι ήδη ανενεργές.\nΔεν έχεις τον ρόλο {role.mention}."
                self.last_error = None
        except SubscriptionError as exc:
            self.last_error = str(exc)
            logger.warning("Notification subscription rejected: %s", exc)
            result = f"❌ {exc.user_message}"
        except discord.NotFound:
            self.last_error = "The subscribing member or notification role is no longer available."
            logger.warning(self.last_error)
            result = "❌ Δεν βρέθηκε το μέλος ή ο ρόλος ειδοποιήσεων. Δοκίμασε ξανά ή ενημέρωσε τον διαχειριστή."
        except discord.Forbidden:
            self.last_error = "Discord denied the notification role change."
            logger.exception(self.last_error)
            result = "❌ Δεν μπόρεσα να αλλάξω τον ρόλο σου. Ο διαχειριστής πρέπει να ελέγξει τα δικαιώματα και τη σειρά των ρόλων."
        except Exception as exc:
            self.last_error = str(exc)
            logger.exception("Could not process notification subscription for member %s.", interaction.user.id)
            result = "❌ Η αλλαγή του ρόλου δεν επιβεβαιώθηκε. Δες την κατάστασή σου με `/notifications status` και δοκίμασε ξανά."
        await interaction.followup.send(
            result, ephemeral=True, allowed_mentions=discord.AllowedMentions.none(),
        )

    @notifications.command(name="status", description="Δες αν λαμβάνεις ειδοποιήσεις προγράμματος και εξεταστικής.")
    @app_commands.guild_only()
    async def status(self, interaction: discord.Interaction):
        if interaction.guild_id != self.settings.guild_id:
            await interaction.response.send_message("⛔ Αυτή η εντολή δεν είναι διαθέσιμη σε αυτόν τον διακομιστή.", ephemeral=True)
            return
        member = interaction.user
        subscribed = any(role.id == self.settings.role_id for role in getattr(member, "roles", ()))
        saved = self.subscription_store.initial(self.settings.guild_id)
        parts = ["**Κατάσταση:** " + ("🟢 Λαμβάνεις ειδοποιήσεις" if subscribed else "⚪ Δεν λαμβάνεις ειδοποιήσεις")]
        parts.append(
            f"Πάτησε **«{SUBSCRIBE_LABEL}»** ή **«{UNSUBSCRIBE_LABEL}»** "
            "σε μια ειδοποίηση ή στο αρχικό μήνυμα."
        )
        if saved:
            parts.append(f"📌 [Άνοιγμα μηνύματος εγγραφής](https://discord.com/channels/{saved.guild_id}/{saved.channel_id}/{saved.message_id})")
        if self.last_error and getattr(member, "guild_permissions", discord.Permissions.none()).manage_guild:
            parts.append("⚠️ Μια ενέργεια εγγραφής απέτυχε. Έλεγξε τα δικαιώματα και τις καταγραφές του bot.")
        embed = styled_embed("🔔 Οι ειδοποιήσεις σου", "\n\n".join(parts), colour=discord.Colour.teal())
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @notifications.command(name="setup", description="Δημιούργησε ή ανανέωσε το αρχικό μήνυμα εγγραφής στις ειδοποιήσεις.")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def setup_message(self, interaction: discord.Interaction):
        if interaction.guild_id != self.settings.guild_id:
            await interaction.response.send_message("⛔ Αυτή η εντολή δεν είναι διαθέσιμη σε αυτόν τον διακομιστή.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            message = await self.ensure_initial_subscription_message()
            self.last_error = None
            result = f"✅ Το μήνυμα εγγραφής είναι έτοιμο.\n[Άνοιγμα μηνύματος]({message.jump_url})"
        except Exception as exc:
            self.last_error = str(exc)
            logger.exception("Manual notification subscription setup failed.")
            result = f"❌ {exc.user_message}" if isinstance(exc, SubscriptionError) else "❌ Η ρύθμιση δεν ολοκληρώθηκε. Έλεγξε τις καταγραφές του bot."
        await interaction.followup.send(result, ephemeral=True)


async def setup(bot):
    await bot.add_cog(Notifications(bot))
