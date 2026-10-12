"""Shared presentation helpers for Greek Discord messages."""

from datetime import datetime

import discord


FOOTER_TEXT = "Υπουργείο Ψηφιακής Διακυβέρνησης"

PERMISSION_LABELS = {
    "view_channel": "Προβολή καναλιού",
    "send_messages": "Αποστολή μηνυμάτων",
    "send_messages_in_threads": "Αποστολή μηνυμάτων σε νήματα",
    "embed_links": "Ενσωμάτωση συνδέσμων",
    "read_message_history": "Ανάγνωση ιστορικού μηνυμάτων",
    "add_reactions": "Προσθήκη αντιδράσεων",
    "manage_messages": "Διαχείριση μηνυμάτων",
    "manage_roles": "Διαχείριση ρόλων",
    "mention_everyone": "Αναφορά όλων των ρόλων",
}

GIF_REASON_LABELS = {
    "GIF attachment": "Συνημμένο GIF",
    "GIF link": "Σύνδεσμος GIF",
    "GIF embed": "Ενσωματωμένο GIF",
}


def styled_embed(
    title: str,
    description: str | None = None,
    *,
    colour: discord.Colour | None = None,
    url: str | None = None,
    timestamp: datetime | None = None,
) -> discord.Embed:
    """Create a consistent embed without adding time-dependent defaults."""
    embed = discord.Embed(
        title=title,
        description=description,
        colour=colour if colour is not None else discord.Colour.blue(),
        url=url,
        timestamp=timestamp,
    )
    embed.set_footer(text=FOOTER_TEXT)
    return embed


def permission_status(name: str, granted: bool) -> str:
    """Format a permission check for a Discord status message."""
    label = PERMISSION_LABELS.get(name, name)
    return f"{'✅' if granted else '❌'} {label}"
