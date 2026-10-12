"""Recognize GIF attachments, links, embeds and forwarded messages."""

import re
from urllib.parse import unquote, urlsplit

import discord

URL_PATTERN = re.compile(r"(?:https?://|www\.)[^\s<>\"`|]+", re.IGNORECASE)
MEDIA_SUFFIXES = (".gif", ".gifv", ".mp4", ".webm", ".webp")


def is_gif_url(value: str | None) -> bool:
    if not value:
        return False
    value = value.strip().rstrip(".,;!?:)]}")
    if value.lower().startswith("www."):
        value = "https://" + value
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    if parsed.scheme.lower() not in {"http", "https"} or not host:
        return False
    path = unquote(parsed.path).lower().rstrip("/")
    if path.endswith((".gif", ".gifv")):
        return True
    if host in {"tenor.com", "www.tenor.com"}:
        return path.startswith(("/view/", "/embed/"))
    if host in {"giphy.com", "www.giphy.com"}:
        return path.startswith(("/gifs/", "/stickers/", "/embed/", "/media/"))
    if host == "gph.is":
        return bool(path)
    media_host = (
        host in {"media.tenor.com", "c.tenor.com", "i.giphy.com"}
        or re.fullmatch(r"media\d*\.giphy\.com", host) is not None
    )
    return media_host and path.endswith(MEDIA_SUFFIXES)


def get_gif_block_reason(message: discord.Message) -> str | None:
    """Recognize GIF metadata without downloading user-provided links."""
    items = [message, *getattr(message, "message_snapshots", [])]
    for item in items:
        for attachment in item.attachments:
            media_type = (attachment.content_type or "").split(";", 1)[0].strip().lower()
            if (
                media_type == "image/gif"
                or attachment.filename.lower().endswith(".gif")
                or is_gif_url(attachment.url)
            ):
                return "GIF attachment"
        if any(is_gif_url(match.group()) for match in URL_PATTERN.finditer(item.content)):
            return "GIF link"
        for embed in item.embeds:
            if embed.type == "gifv":
                return "GIF embed"
            urls = (embed.url, embed.image.url, embed.thumbnail.url, embed.video.url)
            if any(is_gif_url(url) for url in urls):
                return "GIF embed"
    return None
