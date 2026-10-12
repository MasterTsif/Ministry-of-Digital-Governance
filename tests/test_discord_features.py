"""Load real Cogs and exercise delivery using mocked Discord objects."""

from datetime import UTC, datetime
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from ministry_bot.core.bot import MinistryBot
from ministry_bot.core.config import PROJECT_ROOT, Settings
from ministry_bot.exams.discord_cog import Exams
from ministry_bot.exams.exam_monitor import RoomChange
from ministry_bot.notifications.discord_cog import Notifications
from ministry_bot.notifications.subscriptions import (
    SUBSCRIPTION_PROMPT, add_subscription_prompt, get_subscription_cog,
    register_subscription_message,
)
from ministry_bot.schedule.discord_cog import Schedule
from ministry_bot.schedule.pdf_storage import PdfSnapshot
from tests.helpers import exam_snapshot, isolated_settings, mock_alert_channel


class DiscordFeatureTests(unittest.IsolatedAsyncioTestCase):
    async def test_startup_loads_all_extensions_and_registers_expected_commands(self):
        with tempfile.TemporaryDirectory() as directory:
            with isolated_settings(Path(directory) / "bot.sqlite3", NOTIFICATIONS_ENABLED="true") as settings:
                async with MinistryBot(settings) as bot:
                    with patch.object(bot.tree, "sync", new=AsyncMock(return_value=[])):
                        await bot.setup_hook()
                    self.assertEqual(set(bot.cogs), {"General", "Schedule", "Exams", "GifModeration", "Notifications"})
                    self.assertEqual(len(bot.extensions), 5)
                    commands = {
                        command.qualified_name
                        for command in bot.tree.walk_commands(guild=discord.Object(id=settings.guild_id))
                        if isinstance(command, discord.app_commands.Command)
                    }
                    self.assertEqual(commands, {
                        "ping", "schedule status", "schedule check", "schedule preview", "exams status", "exams check",
                        "gifs status", "notifications status", "notifications setup",
                    })
                    self.assertIs(get_subscription_cog(bot), bot.get_cog("Notifications"))
                    self.assertFalse(bot.intents.guild_reactions)
                    self.assertIn(bot.get_cog("Notifications").persistent_view, bot.persistent_views)

    async def test_schedule_delivery_sends_real_pdf_link_and_clears_queue(self):
        with tempfile.TemporaryDirectory() as directory:
            with isolated_settings(Path(directory) / "bot.sqlite3") as settings:
                channel = mock_alert_channel(settings)
                bot = SimpleNamespace(settings=settings, http_session=None, get_channel=lambda _: channel)
                cog = Schedule(bot)
                snapshot = PdfSnapshot(
                    "https://example.test/schedule.pdf", "Class timetable", "a" * 64,
                    None, None, datetime.now(UTC).isoformat(), None,
                )
                cog.state_store.save_snapshot(snapshot, notify=True)
                await cog.send_pending_notifications()
                channel.send.assert_awaited_once()
                embed = channel.send.await_args.kwargs["embed"]
                self.assertEqual(embed.title, "📚 Ενημερώθηκε το πρόγραμμα μαθημάτων")
                self.assertEqual(embed.url, snapshot.url)
                self.assertIn(snapshot.url, embed.fields[0].value)
                self.assertEqual(cog.state_store.get_pending_notifications(), [])

    async def test_failed_schedule_delivery_keeps_notification_for_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            with isolated_settings(Path(directory) / "bot.sqlite3") as settings:
                channel = mock_alert_channel(settings)
                channel.send.side_effect = RuntimeError("Delivery unavailable")
                cog = Schedule(SimpleNamespace(settings=settings, http_session=None, get_channel=lambda _: channel))
                snapshot = PdfSnapshot(
                    "https://example.test/schedule.pdf", "Class timetable", "a" * 64,
                    None, None, datetime.now(UTC).isoformat(), None,
                )
                cog.state_store.save_snapshot(snapshot, notify=True)
                with self.assertLogs("ministry_bot.schedule.discord_cog", level="ERROR"):
                    await cog.send_pending_notifications()
                self.assertEqual(len(cog.state_store.get_pending_notifications()), 1)

    async def test_exam_delivery_includes_program_and_room_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            with isolated_settings(Path(directory) / "bot.sqlite3") as settings:
                channel = mock_alert_channel(settings)
                cog = Exams(SimpleNamespace(settings=settings, http_session=None, get_channel=lambda _: channel))
                before = exam_snapshot(room="Room 101")
                after = exam_snapshot(room="Room 202")
                changes = (RoomChange(before.events[0], after.events[0]),)
                cog.state_store.save_snapshot(after, kind="rooms", changes=changes)
                await cog.send_pending_notifications()
                channel.send.assert_awaited_once()
                embed = channel.send.await_args.kwargs["embed"]
                self.assertEqual(embed.title, "📍 Αλλαγές αιθουσών εξετάσεων")
                self.assertEqual(embed.url, after.period.program_url)
                self.assertIn("Room 101", embed.fields[0].value)
                self.assertIn("Room 202", embed.fields[0].value)
                self.assertEqual(cog.state_store.get_pending_notifications(), [])

    async def test_subscription_setup_and_alert_registration_share_the_same_cog(self):
        with tempfile.TemporaryDirectory() as directory:
            with isolated_settings(Path(directory) / "bot.sqlite3", NOTIFICATIONS_ENABLED="true") as settings:
                role = MagicMock(spec=discord.Role)
                role.id = settings.role_id
                role.is_default.return_value = False
                role.managed = False
                role.mentionable = True
                role.permissions = discord.Permissions.none()
                role.__ge__.return_value = False
                guild = SimpleNamespace(
                    id=settings.guild_id,
                    get_role=lambda _: role,
                    me=SimpleNamespace(guild_permissions=discord.Permissions(manage_roles=True), top_role=role),
                )
                channel = mock_alert_channel(settings)
                channel.guild = guild
                channel.permissions_for.return_value = discord.Permissions(
                    view_channel=True, send_messages=True, embed_links=True,
                    read_message_history=True, add_reactions=True, manage_messages=True,
                )
                user = SimpleNamespace(id=999)
                initial = SimpleNamespace(id=500, channel=channel, guild=guild, author=user, embeds=[], components=[])
                async def update_embed(**kwargs):
                    if "embed" in kwargs:
                        initial.embeds = [kwargs["embed"]]
                    if "view" in kwargs:
                        initial.components = [discord.ActionRow(row) for row in kwargs["view"].to_components()]
                    return initial
                initial.edit = AsyncMock(side_effect=update_embed)
                channel.send.return_value = initial
                channel.fetch_message = AsyncMock(return_value=initial)
                bot = SimpleNamespace(settings=settings, user=user, get_guild=lambda _: guild, get_channel=lambda _: channel)
                cog = Notifications(bot)
                bot.get_cog = lambda _: cog
                self.assertIs(await cog.ensure_initial_subscription_message(), initial)
                self.assertIs(await cog.ensure_initial_subscription_message(), initial)
                self.assertIs(await cog.ensure_initial_subscription_message(), initial)
                initial.edit.assert_awaited_once()
                self.assertTrue(initial.embeds[0].title)
                channel.send.assert_awaited_once()
                self.assertTrue(cog.subscription_store.initial(guild.id).ready)
                alert = SimpleNamespace(id=501, channel=channel, guild=guild, author=user, edit=AsyncMock())
                embed = discord.Embed()
                add_subscription_prompt(bot, embed, channel.id, role.id)
                self.assertTrue(embed.fields[0].name)
                self.assertEqual(embed.fields[0].value, SUBSCRIPTION_PROMPT)
                await register_subscription_message(bot, alert, role.id)
                self.assertTrue(alert.edit.await_args.kwargs["view"].is_persistent())
                self.assertTrue(cog.subscription_store.get(alert.id).ready)
                self.assertEqual(cog.subscription_store.pending(guild.id), [])
                self.assertIsNone(cog.last_error)

    def test_configuration_resolves_existing_project_paths(self):
        self.assertEqual(PROJECT_ROOT, Path(__file__).resolve().parents[1])
        with patch.dict(os.environ, {}, clear=True):
            with patch("ministry_bot.core.config.load_dotenv", return_value=False):
                settings = Settings.load(require_discord=False)
        self.assertEqual(settings.database_path, PROJECT_ROOT / "data" / "bot.sqlite3")


if __name__ == "__main__":
    unittest.main()
