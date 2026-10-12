"""Create the Discord bot and load each feature extension."""

import logging

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from .. import BOT_NAME, USER_AGENT
from .config import Settings

logger = logging.getLogger(__name__)


class MinistryBot(commands.Bot):
    """Core application for Ministry of Digital Governance."""

    def __init__(self, settings: Settings):
        intents = discord.Intents.none()
        intents.guilds = True
        intents.guild_messages = settings.gif_block_enabled
        intents.message_content = settings.gif_block_enabled
        intents.guild_reactions = settings.notifications_enabled
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        self.settings = settings
        self.http_session: aiohttp.ClientSession | None = None
        self.tree.on_error = self.command_error

    async def setup_hook(self):
        self.http_session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self.settings.request_timeout),
            headers={
                "User-Agent": USER_AGENT,
                "Accept-Language": "en-US,en;q=0.9",
            },
            trust_env=True,
        )
        await self.load_extension("ministry_bot.core.general_commands")
        if self.settings.guild_id and self.settings.notifications_enabled:
            await self.load_extension("ministry_bot.notifications.discord_cog")
        if self.settings.guild_id and self.settings.channel_id:
            await self.load_extension("ministry_bot.schedule.discord_cog")
        if self.settings.guild_id and self.settings.exams.channel_id:
            await self.load_extension("ministry_bot.exams.discord_cog")
        if self.settings.guild_id:
            await self.load_extension("ministry_bot.gif_blocker.discord_cog")
        if self.settings.guild_id:
            guild = discord.Object(id=self.settings.guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
        else:
            synced = await self.tree.sync()
        logger.info(
            "Registered %s application commands: %s.",
            len(synced),
            ", ".join(f"/{command.name}" for command in synced),
        )

    async def on_ready(self):
        logger.info("%s connected as %s (ID %s).", BOT_NAME, self.user, self.user.id)

    async def command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ):
        if isinstance(error, app_commands.CheckFailure):
            message = "The Manage Server permission is required for this command."
        else:
            logger.error("Application command failed.", exc_info=error)
            message = "The command failed. Check the logs."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)

    async def close(self):
        try:
            await super().close()
        finally:
            if self.http_session is not None:
                await self.http_session.close()
