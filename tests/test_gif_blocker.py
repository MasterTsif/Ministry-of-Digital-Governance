"""Exercise GIF detection through the Discord message listener."""

from dataclasses import replace
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from ministry_bot.gif_blocker.discord_cog import GifModeration
from tests.helpers import isolated_settings


class GifBlockerTests(unittest.IsolatedAsyncioTestCase):
    async def test_message_listener_blocks_gifs_and_honors_exemptions(self):
        cases = (
            ("GIF link", "https://tenor.com/view/test-123", False, False, True),
            ("regular text", "Hello everyone", False, False, False),
            ("exempt author", "https://example.test/animation.gif", True, False, False),
            ("bot author", "https://example.test/animation.gif", False, True, False),
        )
        with tempfile.TemporaryDirectory() as directory:
            with isolated_settings(Path(directory) / "bot.sqlite3") as settings:
                for label, content, exempt, bot_author, should_delete in cases:
                    with self.subTest(case=label):
                        effective_settings = replace(settings, gif_exempt_ids=frozenset({321}) if exempt else frozenset())
                        cog = GifModeration(SimpleNamespace(settings=effective_settings))
                        message = SimpleNamespace(
                            id=1, content=content, attachments=[], embeds=[], message_snapshots=[],
                            guild=SimpleNamespace(id=settings.guild_id),
                            channel=SimpleNamespace(id=settings.channel_id, parent_id=None, category_id=None),
                            author=SimpleNamespace(id=321, bot=bot_author, roles=[]),
                            webhook_id=None, delete=AsyncMock(),
                        )
                        await cog.on_message(message)
                        self.assertEqual(message.delete.await_count, int(should_delete))
                        self.assertEqual(cog.removed_count, int(should_delete))

    async def test_forwarded_gif_attachment_is_blocked(self):
        with tempfile.TemporaryDirectory() as directory:
            with isolated_settings(Path(directory) / "bot.sqlite3") as settings:
                cog = GifModeration(SimpleNamespace(settings=settings))
                forwarded = SimpleNamespace(
                    content="", embeds=[],
                    attachments=[SimpleNamespace(content_type="image/gif", filename="animation.gif", url="https://example.test/file")],
                )
                message = SimpleNamespace(
                    id=2, content="", attachments=[], embeds=[], message_snapshots=[forwarded],
                    guild=SimpleNamespace(id=settings.guild_id),
                    channel=SimpleNamespace(id=settings.channel_id, parent_id=None, category_id=None),
                    author=SimpleNamespace(id=321, bot=False, roles=[]), webhook_id=None, delete=AsyncMock(),
                )
                await cog.on_message(message)
                message.delete.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
