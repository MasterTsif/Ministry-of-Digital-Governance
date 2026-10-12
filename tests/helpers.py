"""Shared test settings, exam snapshots and mocked Discord channels."""

from contextlib import contextmanager
from datetime import UTC, datetime
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord

from ministry_bot.core.config import Settings
from ministry_bot.exams.exam_monitor import ExamPeriod, ExamSnapshot, parse_exam_calendar
from tests.simulations.exam_update import build_test_exam_calendar


@contextmanager
def isolated_settings(database_path, **overrides):
    """Load settings without reading the user's .env or requiring real tokens."""
    environment = {
        "DISCORD_TOKEN": "offline-test-token",
        "DISCORD_GUILD_ID": "100",
        "ALERT_CHANNEL_ID": "200",
        "ALERT_ROLE_ID": "300",
        "SCHEDULE_ENABLED": "false",
        "EXAMS_ENABLED": "false",
        "GIF_BLOCK_ENABLED": "true",
        "NOTIFICATIONS_ENABLED": "false",
        "DATABASE_PATH": str(database_path),
    }
    environment.update(overrides)
    with patch.dict(os.environ, environment, clear=True):
        with patch("ministry_bot.core.config.load_dotenv", return_value=False):
            yield Settings.load()


def mock_alert_channel(settings):
    channel = Mock(spec=discord.TextChannel)
    channel.id = settings.channel_id
    channel.guild = SimpleNamespace(id=settings.guild_id)
    posted = SimpleNamespace(id=500, channel=channel)
    channel.send = AsyncMock(return_value=posted)
    return channel


def exam_snapshot(period_id=1, room="Room 101"):
    return ExamSnapshot(
        period=ExamPeriod(
            period_id, f"Exam period {period_id}",
            f"https://example.test/program.php?full=1&sem_id={period_id}",
            f"https://example.test/ical.php?full=1&sem_id={period_id}",
        ),
        events=parse_exam_calendar(build_test_exam_calendar(period_id, room)),
        checked_at=datetime.now(UTC).isoformat(),
    )
