"""Load Discord credentials from .env and bot options from settings.toml."""

from dataclasses import dataclass, field
from datetime import time
from pathlib import Path
import os
import tomllib
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

from ..shared.check_policy import CheckPolicy

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SETTING_NAMES = frozenset({
    "SCHEDULE_ENABLED", "SCHEDULE_API_URL", "ALERT_CHANNEL_ID", "ALERT_ROLE_ID", "ALERT_USER_ID",
    "CHECK_TIMEZONE", "ACTIVE_WEEKDAYS", "MORNING_START", "EVENING_START", "NIGHT_START",
    "DAY_INTERVAL_MINUTES", "EVENING_INTERVAL_MINUTES", "EXAMS_ENABLED", "EXAMS_PAGE_URL",
    "EXAMS_ALERT_CHANNEL_ID", "EXAMS_ALERT_ROLE_ID", "EXAMS_ALERT_USER_ID", "EXAMS_CHECK_TIMEZONE",
    "EXAMS_ACTIVE_WEEKDAYS", "EXAMS_MORNING_START", "EXAMS_EVENING_START", "EXAMS_NIGHT_START",
    "EXAMS_DAY_INTERVAL_MINUTES", "EXAMS_EVENING_INTERVAL_MINUTES", "NOTIFICATIONS_ENABLED",
    "GIF_BLOCK_ENABLED", "GIF_EXEMPT_IDS", "GIF_EXEMPT_CHANNEL_IDS", "GIF_LOG_CHANNEL_ID",
    "DATABASE_PATH", "REQUEST_TIMEOUT_SECONDS",
})


def load_settings_file(path: Path) -> dict:
    try:
        with path.open("rb") as file:
            values = tomllib.load(file)
    except FileNotFoundError as exc:
        raise ValueError(f"Missing {path}. Copy settings.example.toml to settings.toml first.") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"Invalid TOML in {path}: {exc}") from exc
    except OSError as exc:
        raise ValueError(f"Cannot read settings file {path}: {exc.strerror}.") from exc
    unknown = values.keys() - SETTING_NAMES
    if unknown:
        raise ValueError(
            f"{path.name}: unknown settings: {', '.join(sorted(unknown))}. "
            "DISCORD_TOKEN and DISCORD_GUILD_ID belong in .env."
        )
    return values


def optional_discord_id(value, name: str) -> int | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if type(value) is int and value > 0:
        return value
    if isinstance(value, str) and value.strip().isdecimal() and int(value) > 0:
        return int(value)
    raise ValueError(f"{name}: a positive numeric Discord ID is required.")


def read_optional_id(values: dict, name: str) -> int | None:
    return optional_discord_id(values.get(name), name)


def read_positive_int(values: dict, name: str, default: int) -> int:
    value = values.get(name, default)
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name}: a positive integer is required.")
    return value


def read_bool(values: dict, name: str, default: bool = False) -> bool:
    value = values.get(name, default)
    if type(value) is not bool:
        raise ValueError(f"{name}: use true or false without quotes in settings.toml.")
    return value


def read_string(values: dict, name: str, default: str) -> str:
    value = values.get(name, default)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name}: a non-empty string is required.")
    return value.strip()


def read_id_list(values: dict, name: str) -> frozenset[int]:
    value = values.get(name, [])
    if not isinstance(value, list):
        raise ValueError(f"{name}: use an array of positive numeric Discord IDs.")
    ids = [optional_discord_id(item, name) for item in value]
    if None in ids:
        raise ValueError(f"{name}: empty IDs are not allowed in the array.")
    return frozenset(ids)


def read_weekdays(values: dict, name: str) -> frozenset[int]:
    value = values.get(name, [0, 1, 2, 3, 4])
    if not isinstance(value, list) or not value or any(type(day) is not int or not 0 <= day <= 6 for day in value):
        raise ValueError(f"{name}: use an array of numbers 0-6 (0=Monday, 6=Sunday).")
    return frozenset(value)


def read_time(values: dict, name: str, default: str) -> time:
    try:
        value = time.fromisoformat(read_string(values, name, default))
    except ValueError as exc:
        raise ValueError(f"{name}: use a local time such as \"08:00\".") from exc
    if value.tzinfo is not None:
        raise ValueError(f"{name}: use a local time without a UTC offset.")
    return value


def read_timezone(values: dict, name: str) -> ZoneInfo:
    try:
        return ZoneInfo(read_string(values, name, "Europe/Athens"))
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"{name}: use a valid timezone such as \"Europe/Athens\".") from exc


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
    gif_exempt_channel_ids: frozenset[int] = field(default_factory=frozenset)
    exams: ExamSettings = field(default_factory=ExamSettings)
    notifications_enabled: bool = False

    @classmethod
    def load(cls, *, require_discord: bool = True, settings_path: Path | None = None) -> "Settings":
        load_dotenv(PROJECT_ROOT / ".env", override=False)
        token = os.getenv("DISCORD_TOKEN", "").strip()
        if require_discord and not token:
            raise ValueError("Set DISCORD_TOKEN in your local .env file.")
        guild_id = optional_discord_id(os.getenv("DISCORD_GUILD_ID", ""), "DISCORD_GUILD_ID")
        values = load_settings_file(PROJECT_ROOT / "settings.toml" if settings_path is None else Path(settings_path))
        enabled = read_bool(values, "SCHEDULE_ENABLED")
        gif_block_enabled = read_bool(values, "GIF_BLOCK_ENABLED")
        channel = read_optional_id(values, "ALERT_CHANNEL_ID")
        role = read_optional_id(values, "ALERT_ROLE_ID")
        user = read_optional_id(values, "ALERT_USER_ID")
        notifications_enabled = read_bool(values, "NOTIFICATIONS_ENABLED")
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
        morning = read_time(values, "MORNING_START", "08:00")
        evening = read_time(values, "EVENING_START", "14:00")
        night = read_time(values, "NIGHT_START", "18:00")
        if not morning < evening < night:
            raise ValueError("Times must satisfy MORNING_START < EVENING_START < NIGHT_START.")
        weekdays = read_weekdays(values, "ACTIVE_WEEKDAYS")
        exam_morning = read_time(values, "EXAMS_MORNING_START", "08:00")
        exam_evening = read_time(values, "EXAMS_EVENING_START", "14:00")
        exam_night = read_time(values, "EXAMS_NIGHT_START", "18:00")
        if not exam_morning < exam_evening < exam_night:
            raise ValueError(
                "Times must satisfy EXAMS_MORNING_START < EXAMS_EVENING_START < EXAMS_NIGHT_START."
            )
        exam_days = read_weekdays(values, "EXAMS_ACTIVE_WEEKDAYS")
        exam_url = read_string(values, "EXAMS_PAGE_URL", ExamSettings.page_url)
        parsed_url = urlsplit(exam_url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            raise ValueError("EXAMS_PAGE_URL must be an HTTP or HTTPS URL.")
        if "sem_id" in parse_qs(parsed_url.query, keep_blank_values=True):
            raise ValueError("EXAMS_PAGE_URL must follow the current period; remove sem_id.")
        exams = ExamSettings(
            enabled=read_bool(values, "EXAMS_ENABLED"),
            page_url=exam_url,
            channel_id=read_optional_id(values, "EXAMS_ALERT_CHANNEL_ID") or channel,
            role_id=read_optional_id(values, "EXAMS_ALERT_ROLE_ID") if "EXAMS_ALERT_ROLE_ID" in values else role,
            user_id=read_optional_id(values, "EXAMS_ALERT_USER_ID") if "EXAMS_ALERT_USER_ID" in values else user,
            policy=CheckPolicy(
                read_timezone(values, "EXAMS_CHECK_TIMEZONE"),
                exam_morning, exam_evening, exam_night,
                read_positive_int(values, "EXAMS_DAY_INTERVAL_MINUTES", 5) * 60,
                read_positive_int(values, "EXAMS_EVENING_INTERVAL_MINUTES", 15) * 60,
                exam_days,
            ),
        )
        if require_discord and exams.enabled and exams.channel_id is None:
            raise ValueError("Set EXAMS_ALERT_CHANNEL_ID or ALERT_CHANNEL_ID when EXAMS_ENABLED=true.")
        if require_discord and exams.enabled and guild_id is None:
            raise ValueError("DISCORD_GUILD_ID is required when EXAMS_ENABLED=true.")
        database = Path(read_string(values, "DATABASE_PATH", "data/bot.sqlite3"))
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
            api_url=read_string(values, "SCHEDULE_API_URL", "https://www.iee.ihu.gr/wp-json/wp/v2/pages/101"),
            timezone=read_timezone(values, "CHECK_TIMEZONE"),
            morning_start=morning,
            evening_start=evening,
            night_start=night,
            day_interval=read_positive_int(values, "DAY_INTERVAL_MINUTES", 5) * 60,
            evening_interval=read_positive_int(values, "EVENING_INTERVAL_MINUTES", 15) * 60,
            active_weekdays=weekdays,
            request_timeout=read_positive_int(values, "REQUEST_TIMEOUT_SECONDS", 20),
            gif_block_enabled=gif_block_enabled,
            gif_exempt_ids=read_id_list(values, "GIF_EXEMPT_IDS"),
            gif_log_channel_id=read_optional_id(values, "GIF_LOG_CHANNEL_ID"),
            gif_exempt_channel_ids=read_id_list(values, "GIF_EXEMPT_CHANNEL_IDS"),
            exams=exams,
            notifications_enabled=notifications_enabled,
        )
