"""Development checks for the Greek Discord presentation."""

from dataclasses import replace
from pathlib import Path
import re
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

import discord

from ministry_bot.core.bot import MinistryBot
from ministry_bot.core.general_commands import General
from ministry_bot.exams.discord_cog import Exams
from ministry_bot.gif_blocker.discord_cog import GifModeration
from ministry_bot.notifications.discord_cog import Notifications, SubscriptionError
from ministry_bot.schedule.discord_cog import Schedule
from ministry_bot.shared.discord_ui import FOOTER_TEXT
from tests.helpers import isolated_settings, mock_alert_channel


def interaction(guild_id=100):
    return SimpleNamespace(
        guild_id=guild_id,
        guild=SimpleNamespace(me=None),
        channel=None,
        user=SimpleNamespace(roles=[], guild_permissions=discord.Permissions.none()),
        response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock(), is_done=lambda: False),
        followup=SimpleNamespace(send=AsyncMock()),
    )


class GreekUiTests(unittest.IsolatedAsyncioTestCase):
    def assert_greek_embed(self, embed):
        self.assertRegex(embed.title, r'[\u0370-\u03ff]')
        self.assertEqual(embed.footer.text, FOOTER_TEXT)
        self.assertLessEqual(len(embed), 6000)
        self.assertLessEqual(len(embed.title), 256)
        self.assertLessEqual(len(embed.description or ''), 4096)
        for field in embed.fields:
            self.assertLessEqual(len(field.name), 256)
            self.assertLessEqual(len(field.value), 1024)

    async def test_status_commands_use_greek_embeds_and_keep_responses_private(self):
        with tempfile.TemporaryDirectory() as directory:
            with isolated_settings(Path(directory) / 'bot.sqlite3') as settings:
                bot = SimpleNamespace(settings=settings, http_session=None)
                for cog_type in (Schedule, Exams, GifModeration, Notifications):
                    with self.subTest(cog=cog_type.__name__):
                        cog = cog_type(bot)
                        request = interaction(settings.guild_id)
                        await cog.status.callback(cog, request)
                        payload = request.response.send_message.await_args.kwargs
                        self.assertTrue(payload['ephemeral'])
                        self.assert_greek_embed(payload['embed'])

    async def test_ping_uses_a_greek_latency_card(self):
        cog = General(SimpleNamespace(latency=0.032))
        request = interaction()
        await cog.ping.callback(cog, request)
        embed = request.response.send_message.await_args.kwargs['embed']
        self.assert_greek_embed(embed)
        self.assertIn('32 ms', embed.description)

    async def test_subscription_error_is_greek_for_users_and_english_for_diagnostics(self):
        error = SubscriptionError('The configured server is unavailable.', 'Ο διακομιστής δεν είναι διαθέσιμος.')
        self.assertEqual(str(error), 'The configured server is unavailable.')
        with tempfile.TemporaryDirectory() as directory:
            with isolated_settings(Path(directory) / 'bot.sqlite3') as settings:
                cog = Notifications(SimpleNamespace(settings=settings))
                cog.ensure_initial_subscription_message = AsyncMock(side_effect=error)
                request = interaction(settings.guild_id)
                with self.assertLogs('ministry_bot.notifications.discord_cog', level='ERROR'):
                    await cog.setup_message.callback(cog, request)
                self.assertEqual(cog.last_error, str(error))
                self.assertIn(error.user_message, request.followup.send.await_args.args[0])
                self.assertNotIn(str(error), request.followup.send.await_args.args[0])

    async def test_gif_deletion_card_localizes_the_reason_without_changing_the_reason_code(self):
        with tempfile.TemporaryDirectory() as directory:
            with isolated_settings(Path(directory) / 'bot.sqlite3') as settings:
                settings = replace(settings, gif_log_channel_id=200)
                channel = mock_alert_channel(settings)
                cog = GifModeration(SimpleNamespace(settings=settings, get_channel=lambda _: channel))
                message = SimpleNamespace(
                    id=123, channel=channel, author=SimpleNamespace(id=321, display_name='Test member'),
                    content='', attachments=[], message_snapshots=[],
                )
                await cog._send_deletion_log(message, 'GIF attachment')
                embed = channel.send.await_args.kwargs['embed']
                self.assert_greek_embed(embed)
                fields = {field.name: field.value for field in embed.fields}
                self.assertEqual(fields['🚫 Αιτία'], 'Συνημμένο GIF')
                self.assertIn('Χωρίς περιεχόμενο', fields['💬 Περιεχόμενο μηνύματος'])

    async def test_permission_error_response_is_greek(self):
        with tempfile.TemporaryDirectory() as directory:
            with isolated_settings(Path(directory) / 'bot.sqlite3') as settings:
                async with MinistryBot(settings) as bot:
                    request = interaction(settings.guild_id)
                    await bot.command_error(request, discord.app_commands.MissingPermissions(['manage_guild']))
                    self.assertIn('Διαχείριση διακομιστή', request.response.send_message.await_args.args[0])


if __name__ == '__main__':
    unittest.main()
