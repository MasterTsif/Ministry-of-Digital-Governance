"""Read and validate environment settings for all bot features."""

from dataclasses import dataclass, field
from datetime import time
from pathlib import Path
import os
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from ..shared.check_policy import CheckPolicy

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def read_optional_env_id(name: str) -> int | None:
    value = os.getenv(name, "").strip()
    if not value:
        return None
    if not value.isdecimal() or int(value) <= 0:
        raise ValueError(f"{name}: a positive numeric Discord ID is required.")
    return int(value)


def read_positive_env_int(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name}: a positive integer is required.") from exc
    if value <= 0:
        raise ValueError(f"{name}: a positive integer is required.")
    return value


def read_env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name, str(default)).strip().lower()
    if value not in {"true", "false"}:
        raise ValueError(f"{name}: use true or false.")
    return value == "true"


def read_env_id_list(name: str) -> frozenset[int]:
    value = os.getenv(name, "").strip()
    if not value:
        return frozenset()
    parts = [part.strip() for part in value.split(",")]
    if any(not part.isdecimal() or int(part) <= 0 for part in parts):
        raise ValueError(f"{name}: use positive numeric IDs separated by commas.")
    return frozenset(int(part) for part in parts)


def default_exam_policy() -> CheckPolicy:
    return CheckPolicy(
        ZoneInfo("Europe/Athens"), time(8), time(14), time(18),
        300, 900, frozenset(range(5)),
    )


@dataclass(frozen=True)
class ExamSettings:
    enabled: bool = False
    page_url: str = "https://www.iee.ihu.gr/exams-program/"
    channel_id: int | None = None
    role_id: int | None = None
    user_id: int | None = None
    policy: CheckPolicy = field(default_factory=default_exam_policy)


@dataclass(frozen=True)
class Settings:
    token: str = field(repr=False)
    guild_id: int | None
    schedule_enabled: bool
    channel_id: int | None
    role_id: int | None
    user_id: int | None
    database_path: Path
    api_url: str
    timezone: ZoneInfo
    morning_start: time
    evening_start: time
    night_start: time
    day_interval: int
    evening_interval: int
    active_weekdays: frozenset[int]
    request_timeout: int
    gif_block_enabled: bool = False
    gif_exempt_ids: frozenset[int] = field(default_factory=frozenset)
    gif_log_channel_id: int | None = None
    exams: ExamSettings = field(default_factory=ExamSettings)
    notifications_enabled: bool = False

    @classmethod
    def load(cls, *, require_discord: bool = True) -> "Settings":
        load_dotenv(PROJECT_ROOT / ".env", override=False)
        token = os.getenv("DISCORD_TOKEN", "").strip()
        if require_discord and not token:
            raise ValueError("Set DISCORD_TOKEN in your local .env file.")
        enabled = read_env_bool("SCHEDULE_ENABLED")
        gif_block_enabled = read_env_bool("GIF_BLOCK_ENABLED")
        channel = read_optional_env_id("ALERT_CHANNEL_ID")
        role = read_optional_env_id("ALERT_ROLE_ID")
        user = read_optional_env_id("ALERT_USER_ID")
        guild_id = read_optional_env_id("DISCORD_GUILD_ID")
        notifications_enabled = read_env_bool("NOTIFICATIONS_ENABLED")
        if require_discord and notifications_enabled and not (guild_id and channel and role):
            raise ValueError(
                "DISCORD_GUILD_ID, ALERT_CHANNEL_ID and ALERT_ROLE_ID are required "
                "when NOTIFICATIONS_ENABLED=true."
            )
        if require_discord and enabled and channel is None:
            raise ValueError("ALERT_CHANNEL_ID is required when SCHEDULE_ENABLED=true.")
        if require_discord and enabled and guild_id is None:
            raise ValueError("DISCORD_GUILD_ID is required when SCHEDULE_ENABLED=true.")
        if require_discord and gif_block_enabled and guild_id is None:
            raise ValueError("DISCORD_GUILD_ID is required when GIF_BLOCK_ENABLED=true.")
        morning = time.fromisoformat(os.getenv("MORNING_START", "08:00"))
        evening = time.fromisoformat(os.getenv("EVENING_START", "14:00"))
        night = time.fromisoformat(os.getenv("NIGHT_START", "18:00"))
        if not morning < evening < night:
            raise ValueError("Times must satisfy MORNING_START < EVENING_START < NIGHT_START.")
        weekdays = frozenset(
            int(value.strip())
            for value in os.getenv("ACTIVE_WEEKDAYS", "0,1,2,3,4").split(",")
        )
        if not weekdays or not weekdays <= set(range(7)):
            raise ValueError("ACTIVE_WEEKDAYS: use numbers 0-6 (0=Monday, 6=Sunday).")
        exam_morning = time.fromisoformat(os.getenv("EXAMS_MORNING_START", "08:00"))
        exam_evening = time.fromisoformat(os.getenv("EXAMS_EVENING_START", "14:00"))
        exam_night = time.fromisoformat(os.getenv("EXAMS_NIGHT_START", "18:00"))
        if not exam_morning < exam_evening < exam_night:
            raise ValueError(
                "Times must satisfy EXAMS_MORNING_START < EXAMS_EVENING_START < EXAMS_NIGHT_START."
            )
        exam_days = frozenset(
            int(value.strip())
            for value in os.getenv("EXAMS_ACTIVE_WEEKDAYS", "0,1,2,3,4").split(",")
        )
        if not exam_days or not exam_days <= set(range(7)):
            raise ValueError("EXAMS_ACTIVE_WEEKDAYS: use numbers 0-6.")
        exam_url = os.getenv("EXAMS_PAGE_URL", ExamSettings.page_url).strip()
        parsed_url = urlsplit(exam_url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            raise ValueError("EXAMS_PAGE_URL must be an HTTP or HTTPS URL.")
        if "sem_id" in parse_qs(parsed_url.query, keep_blank_values=True):
            raise ValueError("EXAMS_PAGE_URL must follow the current period; remove sem_id.")
        exams = ExamSettings(
            enabled=read_env_bool("EXAMS_ENABLED"),
            page_url=exam_url,
            channel_id=read_optional_env_id("EXAMS_ALERT_CHANNEL_ID") or channel,
            role_id=read_optional_env_id("EXAMS_ALERT_ROLE_ID") if "EXAMS_ALERT_ROLE_ID" in os.environ else role,
            user_id=read_optional_env_id("EXAMS_ALERT_USER_ID") if "EXAMS_ALERT_USER_ID" in os.environ else user,
            policy=CheckPolicy(
                ZoneInfo(os.getenv("EXAMS_CHECK_TIMEZONE", "Europe/Athens")),
                exam_morning, exam_evening, exam_night,
                read_positive_env_int("EXAMS_DAY_INTERVAL_MINUTES", 5) * 60,
                read_positive_env_int("EXAMS_EVENING_INTERVAL_MINUTES", 15) * 60,
                exam_days,
            ),
        )
        if require_discord and exams.enabled and exams.channel_id is None:
            raise ValueError("Set EXAMS_ALERT_CHANNEL_ID or ALERT_CHANNEL_ID when EXAMS_ENABLED=true.")
        if require_discord and exams.enabled and guild_id is None:
            raise ValueError("DISCORD_GUILD_ID is required when EXAMS_ENABLED=true.")
        database = Path(os.getenv("DATABASE_PATH", "data/bot.sqlite3"))
        if not database.is_absolute():
            database = PROJECT_ROOT / database
        return cls(
            token=token,
            guild_id=guild_id,
            schedule_enabled=enabled,
            channel_id=channel,
            role_id=role,
            user_id=user,
            database_path=database,
            api_url=os.getenv(
                "SCHEDULE_API_URL",
                "https://www.iee.ihu.gr/wp-json/wp/v2/pages/101",
            ),
            timezone=ZoneInfo(os.getenv("CHECK_TIMEZONE", "Europe/Athens")),
            morning_start=morning,
            evening_start=evening,
            night_start=night,
            day_interval=read_positive_env_int("DAY_INTERVAL_MINUTES", 5) * 60,
            evening_interval=read_positive_env_int("EVENING_INTERVAL_MINUTES", 15) * 60,
            active_weekdays=weekdays,
            request_timeout=read_positive_env_int("REQUEST_TIMEOUT_SECONDS", 20),
            gif_block_enabled=gif_block_enabled,
            gif_exempt_ids=read_env_id_list("GIF_EXEMPT_IDS"),
            gif_log_channel_id=read_optional_env_id("GIF_LOG_CHANNEL_ID"),
            exams=exams,
            notifications_enabled=notifications_enabled,
        )
