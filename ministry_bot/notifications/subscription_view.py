"""Persistent notification buttons with private interaction responses."""

import logging
from typing import TYPE_CHECKING

import discord

from .subscriptions import (
    SUBSCRIBE_EMOJI, SUBSCRIBE_LABEL, UNSUBSCRIBE_EMOJI, UNSUBSCRIBE_LABEL,
)

if TYPE_CHECKING:
    from .discord_cog import Notifications

logger = logging.getLogger(__name__)


class SubscriptionView(discord.ui.View):
    """Use stable button IDs so stored messages work after a bot restart."""

    def __init__(self, cog: "Notifications"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(
        label=SUBSCRIBE_LABEL,
        emoji=SUBSCRIBE_EMOJI,
        style=discord.ButtonStyle.success,
        custom_id="ministry:notifications:subscribe",
    )
    async def subscribe(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.change_subscription(interaction, subscribe=True)

    @discord.ui.button(
        label=UNSUBSCRIBE_LABEL,
        emoji=UNSUBSCRIBE_EMOJI,
        style=discord.ButtonStyle.danger,
        custom_id="ministry:notifications:unsubscribe",
    )
    async def unsubscribe(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.change_subscription(interaction, subscribe=False)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item):
        self.cog.last_error = str(error)
        logger.error(
            "Notification button failed for member %s.", interaction.user.id,
            exc_info=(type(error), error, error.__traceback__),
        )
        message = (
            "❌ Δεν μπόρεσα να επιβεβαιώσω την ενέργεια. "
            "Δες την κατάστασή σου με `/notifications status`."
        )
        try:
            if interaction.response.is_done():
                await interaction.followup.send(
                    message, ephemeral=True, allowed_mentions=discord.AllowedMentions.none(),
                )
            else:
                await interaction.response.send_message(
                    message, ephemeral=True, allowed_mentions=discord.AllowedMentions.none(),
                )
        except discord.HTTPException:
            logger.exception("Could not send the private notification button error response.")
