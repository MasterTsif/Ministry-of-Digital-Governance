"""Persist subscription messages and attach notification role prompts."""

from contextlib import contextmanager
from dataclasses import dataclass
import logging
from pathlib import Path
import sqlite3

import discord

logger = logging.getLogger(__name__)
SUBSCRIBE_EMOJI = "🔔"
UNSUBSCRIBE_EMOJI = "🔕"
SUBSCRIBE_LABEL = "Ενεργοποίηση ειδοποιήσεων"
UNSUBSCRIBE_LABEL = "Απενεργοποίηση ειδοποιήσεων"
SUBSCRIPTION_PROMPT = (
    f"🔔 **{SUBSCRIBE_LABEL}:** παίρνεις τον ρόλο που γίνεται tag για αλλαγές του προγράμματος μαθημάτων και εξεταστικής.\n"
    f"🔕 **{UNSUBSCRIBE_LABEL}:** αφαιρείται ο ρόλος και σταματούν οι ειδοποιήσεις.\n\n"
    "*Πάτησε το αντίστοιχο κουμπί παρακάτω. Εμφανίζεται μια επιβεβαίωση μόνο σε εσένα.*"
)


@dataclass(frozen=True)
class SubscriptionMessage:
    message_id: int
    guild_id: int
    channel_id: int
    role_id: int
    initial: bool = False
    ready: bool = False


class SubscriptionStore:
    """Remember actionable messages without changing monitoring state."""

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS subscription_messages (
                    message_id INTEGER PRIMARY KEY,
                    guild_id INTEGER NOT NULL,
                    channel_id INTEGER NOT NULL,
                    role_id INTEGER NOT NULL,
                    initial INTEGER NOT NULL DEFAULT 0,
                    ready INTEGER NOT NULL DEFAULT 0
                );
                CREATE UNIQUE INDEX IF NOT EXISTS subscription_initial_message
                    ON subscription_messages(guild_id) WHERE initial = 1;
                """
            )

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def get(self, message_id: int) -> SubscriptionMessage | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM subscription_messages WHERE message_id = ?", (message_id,)
            ).fetchone()
        return SubscriptionMessage(*row) if row else None

    def initial(self, guild_id: int) -> SubscriptionMessage | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM subscription_messages WHERE guild_id = ? AND initial = 1",
                (guild_id,),
            ).fetchone()
        return SubscriptionMessage(*row) if row else None

    def remember(self, message: SubscriptionMessage):
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO subscription_messages VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(message_id) DO UPDATE SET ready = excluded.ready",
                (message.message_id, message.guild_id, message.channel_id,
                 message.role_id, int(message.initial), int(message.ready)),
            )

    def mark_ready(self, message_id: int):
        with self.connect() as connection:
            connection.execute(
                "UPDATE subscription_messages SET ready = 1 WHERE message_id = ?",
                (message_id,),
            )

    def pending(self, guild_id: int) -> list[SubscriptionMessage]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM subscription_messages WHERE guild_id = ? AND ready = 0 "
                "ORDER BY message_id", (guild_id,),
            ).fetchall()
        return [SubscriptionMessage(*row) for row in rows]

    def forget(self, message_id: int):
        with self.connect() as connection:
            connection.execute(
                "DELETE FROM subscription_messages WHERE message_id = ?", (message_id,)
            )


def get_subscription_cog(bot):
    if not bot.settings.notifications_enabled:
        return None
    get_cog = getattr(bot, "get_cog", None)
    return get_cog("Notifications") if get_cog is not None else None


def add_subscription_prompt(bot, embed: discord.Embed, channel_id: int, role_id: int | None):
    cog = get_subscription_cog(bot)
    if cog is not None and cog.accepts_subscription(channel_id, role_id):
        embed.add_field(name="Ειδοποιήσεις", value=SUBSCRIPTION_PROMPT, inline=False)


def get_subscription_view(bot, channel_id: int, role_id: int | None):
    """Create buttons only for alerts using the configured notification role."""
    cog = get_subscription_cog(bot)
    if cog is not None and cog.accepts_subscription(channel_id, role_id):
        return cog.create_subscription_view()
    return None


async def register_subscription_message(
    bot, message, role_id: int | None, *, buttons_attached: bool = False,
):
    cog = None
    try:
        cog = get_subscription_cog(bot)
        if cog is None or not cog.accepts_subscription(message.channel.id, role_id):
            return
        await cog.register_alert_message(message, buttons_attached=buttons_attached)
    except Exception as exc:
        if cog is not None:
            cog.last_error = str(exc)
        logger.exception(
            "Subscription buttons could not be registered for message %s. "
            "The timetable notification was already delivered and will not be repeated.",
            message.id,
        )
