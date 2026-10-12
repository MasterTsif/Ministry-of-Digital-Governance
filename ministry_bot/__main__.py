"""Start the bot or inspect its real schedule and exam sources once."""

import argparse
import asyncio
from dataclasses import asdict
import json
import logging
from logging.handlers import RotatingFileHandler

import aiohttp

from . import BOT_NAME, USER_AGENT, __version__
from .core.bot import MinistryBot
from .core.config import PROJECT_ROOT, Settings
from .schedule.pdf_monitor import SchedulePdfClient


async def inspect_schedule(settings: Settings):
    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=settings.request_timeout),
        headers={
            "User-Agent": USER_AGENT,
            "Accept-Language": "en-US,en;q=0.9",
        },
        trust_env=True,
    ) as session:
        snapshot = await SchedulePdfClient(session, settings.api_url).fetch_pdf_snapshot(None)
        print(json.dumps(asdict(snapshot), ensure_ascii=False, indent=2))


async def inspect_exams(settings: Settings):
    from .exams.exam_monitor import ExamCalendarClient

    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=settings.request_timeout),
        headers={"User-Agent": USER_AGENT}, trust_env=True,
    ) as session:
        snapshot = await ExamCalendarClient(session, settings.exams.page_url).fetch_exam_snapshot()
        print(json.dumps(asdict(snapshot), ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description=f"{BOT_NAME}: an extensible Discord bot.")
    parser.add_argument("--version", action="version", version=f"{BOT_NAME} {__version__}")
    inspection = parser.add_mutually_exclusive_group()
    inspection.add_argument(
        "--inspect-schedule",
        action="store_true",
        help="Read the current timetable PDF once without connecting to Discord or saving state.",
    )
    inspection.add_argument(
        "--inspect-exams", action="store_true",
        help="Read the current exam period and calendar without connecting to Discord or saving state.",
    )
    args = parser.parse_args()
    try:
        settings = Settings.load(require_discord=not (args.inspect_schedule or args.inspect_exams))
    except (ValueError, KeyError) as error:
        parser.error(str(error))
    if args.inspect_schedule:
        asyncio.run(inspect_schedule(settings))
        return
    if args.inspect_exams:
        asyncio.run(inspect_exams(settings))
        return
    log_path = PROJECT_ROOT / "logs"
    log_path.mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            RotatingFileHandler(
                log_path / "bot.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8"
            ),
        ],
    )
    logging.getLogger(__name__).info(
        "Starting %s version %s from %s.", BOT_NAME, __version__, PROJECT_ROOT
    )
    bot = MinistryBot(settings)
    bot.run(settings.token, log_handler=None)


if __name__ == "__main__":
    main()
