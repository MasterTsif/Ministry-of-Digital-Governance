"""General Discord commands, including the connection latency check."""

import discord
from discord import app_commands
from discord.ext import commands

from ..shared.discord_ui import styled_embed


class General(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="ping", description="Έλεγχος σύνδεσης και χρόνου απόκρισης του bot.")
    @app_commands.guild_only()
    async def ping(self, interaction: discord.Interaction):
        embed = styled_embed(
            "🏓 Το bot είναι ενεργό",
            f"**Χρόνος απόκρισης:** `{self.bot.latency * 1000:.0f} ms`",
            colour=discord.Colour.green(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot):
    await bot.add_cog(General(bot))
