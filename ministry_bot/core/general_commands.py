"""General Discord commands, including the connection latency check."""

import discord
from discord import app_commands
from discord.ext import commands


class General(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="ping", description="Check the bot connection.")
    @app_commands.guild_only()
    async def ping(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            f"Pong! {self.bot.latency * 1000:.0f} ms", ephemeral=True
        )


async def setup(bot):
    await bot.add_cog(General(bot))
