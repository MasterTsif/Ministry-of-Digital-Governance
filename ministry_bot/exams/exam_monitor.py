"""Read exam calendars, detect new periods and room changes, and persist alerts."""

import asyncio
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import sqlite3
from urllib.parse import parse_qs, urljoin, urlsplit
from zoneinfo import ZoneInfo

import aiohttp
from icalendar import Calendar


class ExamMonitorError(RuntimeError):
    pass


@dataclass(frozen=True)
class ExamPeriod:
    id: int
    label: str
    program_url: str
    calendar_url: str


@dataclass(frozen=True)
class ExamEvent:
    uid: str
    course_key: str
    title: str
    starts_at: str
    ends_at: str
    rooms: tuple[str, ...]


@dataclass(frozen=True)
class ExamSnapshot:
    period: ExamPeriod
    events: tuple[ExamEvent, ...]
    checked_at: str


@dataclass(frozen=True)
class RoomChange:
    before: ExamEvent
    after: ExamEvent


@dataclass(frozen=True)
class ExamNotification:
    id: int
    kind: str
    snapshot: ExamSnapshot
    changes: tuple[RoomChange, ...]


@dataclass(frozen=True)
class ExamCheckResult:
    kind: str
    snapshot: ExamSnapshot
    changes: tuple[RoomChange, ...] = ()


def clean_text(value: str) -> str:
    return " ".join(value.split())


class ExamPeriodParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.div_depth = 0
        self.heading_depth = None
        self.heading_seen = False
        self.anchor_depth = 0
        self.heading = []
        self.links = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "div":
            self.div_depth += 1
            if not self.heading_seen and "panel-heading" in attrs.get("class", "").split():
                self.heading_seen = True
                self.heading_depth = self.div_depth
        if tag == "a":
            if self.heading_depth is not None:
                self.anchor_depth += 1
            if attrs.get("href"):
                self.links.append(attrs["href"])

    def handle_endtag(self, tag):
        if tag == "a" and self.heading_depth is not None:
            self.anchor_depth = max(0, self.anchor_depth - 1)
        if tag == "div":
            if self.div_depth == self.heading_depth:
                self.heading_depth = None
            self.div_depth -= 1

    def handle_data(self, data):
        if self.heading_depth is not None and not self.anchor_depth:
            self.heading.append(data)


def extract_exam_period(content: str, page_url: str) -> ExamPeriod:
    parser = ExamPeriodParser()
    parser.feed(content)
    heading = clean_text(" ".join(parser.heading))
    label = heading.split(":", 1)[-1].strip()
    if not label or not parser.heading_seen:
        raise ExamMonitorError("The current exam-period heading is missing.")
    base = urlsplit(page_url)
    links = {"program.php": set(), "ical.php": set()}
    for href in parser.links:
        url = urljoin(page_url, href)
        parsed = urlsplit(url)
        name = parsed.path.rsplit("/", 1)[-1]
        if name not in links:
            continue
        if (parsed.scheme, parsed.netloc) != (base.scheme, base.netloc):
            raise ExamMonitorError("The exam source contains an unexpected calendar host.")
        query = parse_qs(parsed.query)
        values = query.get("sem_id", [])
        if query.get("full") == ["1"] and len(values) == 1 and values[0].isdecimal():
            links[name].add((int(values[0]), url))
    if any(len(values) != 1 for values in links.values()):
        raise ExamMonitorError("The current period must have one full program and calendar link.")
    program_id, program_url = next(iter(links["program.php"]))
    calendar_id, calendar_url = next(iter(links["ical.php"]))
    if program_id <= 0 or program_id != calendar_id:
        raise ExamMonitorError("The program and calendar refer to different exam periods.")
    return ExamPeriod(program_id, label, program_url, calendar_url)


def parse_exam_calendar(body: bytes) -> tuple[ExamEvent, ...]:
    stripped = body.strip()
    if not stripped.startswith(b"BEGIN:VCALENDAR") or not stripped.endswith(b"END:VCALENDAR"):
        raise ExamMonitorError("The exam calendar is missing or incomplete.")
    try:
        calendar = Calendar.from_ical(body)
        if calendar.name != "VCALENDAR" or str(calendar.get("VERSION")) != "2.0":
            raise ValueError("Unexpected calendar format")
        events = []
        seen = set()
        for component in calendar.walk("VEVENT"):
            if component.errors:
                raise ValueError("Invalid calendar event")
            uid = clean_text(str(component.get("UID", "")))
            title = clean_text(str(component.get("SUMMARY", "")))
            title = re.sub(r"\s*\(v\.?\s*[\d.]+\)\s*$", "", title, flags=re.I).strip()
            start = component.decoded("DTSTART")
            end = component.decoded("DTEND")
            if not uid or uid in seen or not title:
                raise ValueError("Missing or repeated exam identifier")
            if not isinstance(start, datetime) or not isinstance(end, datetime):
                raise ValueError("Exams must include start and end times")
            if start.tzinfo is None:
                start = start.replace(tzinfo=ZoneInfo("Europe/Athens"))
            if end.tzinfo is None:
                end = end.replace(tzinfo=ZoneInfo("Europe/Athens"))
            start, end = start.astimezone(UTC), end.astimezone(UTC)
            if end <= start:
                raise ValueError("Invalid examination duration")
            if component.get("RRULE") or component.get("RECURRENCE-ID"):
                raise ValueError("Recurring examinations are unsupported")
            if "LOCATION" not in component:
                raise ValueError("The exam room field is missing")
            rooms = tuple(sorted({
                clean_text(value) for value in str(component["LOCATION"]).split(",")
                if clean_text(value)
            }))
            code = re.match(r"^(\d+)\.([^.]+)\.", title)
            course_key = ":".join(code.groups()) if code else title.casefold()
            events.append(ExamEvent(uid, course_key, title, start.isoformat(), end.isoformat(), rooms))
            seen.add(uid)
        return tuple(sorted(events, key=lambda event: event.uid))
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        raise ExamMonitorError("The exam calendar could not be validated.") from error


def detect_room_changes(before: ExamSnapshot, after: ExamSnapshot) -> tuple[RoomChange, ...]:
    old_by_uid = {event.uid: event for event in before.events}
    matched = set()
    unmatched = []
    pairs = []
    for event in after.events:
        old = old_by_uid.get(event.uid)
        if old and old.course_key == event.course_key:
            pairs.append((old, event))
            matched.add(old.uid)
        else:
            unmatched.append(event)
    old_groups = defaultdict(list)
    new_groups = defaultdict(list)
    for event in before.events:
        if event.uid not in matched:
            old_groups[event.course_key].append(event)
    for event in unmatched:
        new_groups[event.course_key].append(event)
    for key, new_events in new_groups.items():
        old_events = old_groups[key]
        if len(old_events) == len(new_events) == 1:
            pairs.append((old_events[0], new_events[0]))
        else:
            # Match multiple examination waves only when their time slots agree.
            for event in new_events:
                candidates = [old for old in old_events if
                              (old.starts_at, old.ends_at) == (event.starts_at, event.ends_at)]
                if len(candidates) == 1:
                    pairs.append((candidates[0], event))
                    old_events.remove(candidates[0])
    changes = [RoomChange(old, new) for old, new in pairs if old.rooms != new.rooms]
    return tuple(sorted(changes, key=lambda change: (change.after.starts_at, change.after.title)))


def deserialize_exam_event(payload) -> ExamEvent:
    return ExamEvent(**{**payload, "rooms": tuple(payload["rooms"])})


def deserialize_exam_snapshot(payload) -> ExamSnapshot:
    return ExamSnapshot(
        ExamPeriod(**payload["period"]),
        tuple(deserialize_exam_event(event) for event in payload["events"]),
        payload["checked_at"],
    )


class ExamStateStore:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(
                "CREATE TABLE IF NOT EXISTS exam_state "
                "(id INTEGER PRIMARY KEY CHECK (id = 1), payload TEXT NOT NULL);"
                "CREATE TABLE IF NOT EXISTS exam_notifications "
                "(id INTEGER PRIMARY KEY AUTOINCREMENT, payload TEXT NOT NULL);"
            )

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def get_current_snapshot(self) -> ExamSnapshot | None:
        with self.connect() as connection:
            row = connection.execute("SELECT payload FROM exam_state WHERE id = 1").fetchone()
        return deserialize_exam_snapshot(json.loads(row[0])) if row else None

    def save_snapshot(self, snapshot: ExamSnapshot, *, kind: str | None = None, changes=()) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO exam_state (id, payload) VALUES (1, ?) "
                "ON CONFLICT(id) DO UPDATE SET payload = excluded.payload",
                (json.dumps(asdict(snapshot), ensure_ascii=False),),
            )
            if kind:
                payload = {"kind": kind, "snapshot": asdict(snapshot),
                           "changes": [asdict(change) for change in changes]}
                connection.execute(
                    "INSERT INTO exam_notifications (payload) VALUES (?)",
                    (json.dumps(payload, ensure_ascii=False),),
                )

    def get_pending_notifications(self) -> list[ExamNotification]:
        with self.connect() as connection:
            rows = connection.execute("SELECT id, payload FROM exam_notifications ORDER BY id").fetchall()
        notifications = []
        for identifier, encoded in rows:
            payload = json.loads(encoded)
            changes = tuple(RoomChange(deserialize_exam_event(item["before"]), deserialize_exam_event(item["after"]))
                            for item in payload["changes"])
            notifications.append(ExamNotification(identifier, payload["kind"],
                                                 deserialize_exam_snapshot(payload["snapshot"]), changes))
        return notifications

    def mark_notification_sent(self, notification_id: int) -> None:
        with self.connect() as connection:
            connection.execute("DELETE FROM exam_notifications WHERE id = ?", (notification_id,))


class ExamCalendarClient:
    def __init__(self, session: aiohttp.ClientSession, page_url: str):
        self.session = session
        self.page_url = page_url

    async def fetch_exam_snapshot(self) -> ExamSnapshot:
        headers = {"Accept-Language": "el-GR,el;q=0.9"}
        async with self.session.get(self.page_url, headers=headers) as response:
            response.raise_for_status()
            period = extract_exam_period(await response.text(), str(response.url))
        async with self.session.get(period.calendar_url, headers=headers) as response:
            response.raise_for_status()
            events = parse_exam_calendar(await response.read())
        return ExamSnapshot(period, events, datetime.now(UTC).isoformat())


class ExamMonitor:
    def __init__(self, client: ExamCalendarClient, store: ExamStateStore):
        self.calendar_client = client
        self.state_store = store
        self.lock = asyncio.Lock()

    async def check_for_exam_changes(self) -> ExamCheckResult:
        async with self.lock:
            previous = self.state_store.get_current_snapshot()
            snapshot = await self.calendar_client.fetch_exam_snapshot()
            if previous and snapshot.period.id < previous.period.id:
                raise ExamMonitorError("The source returned an older exam period; stored state was retained.")
            if not snapshot.events:
                if previous and previous.period.id == snapshot.period.id and previous.events:
                    raise ExamMonitorError("The current exam calendar unexpectedly became empty.")
                self.state_store.save_snapshot(snapshot)
                return ExamCheckResult("waiting", snapshot)
            if previous is None:
                self.state_store.save_snapshot(snapshot)
                return ExamCheckResult("baseline", snapshot)
            if snapshot.period.id != previous.period.id or not previous.events:
                self.state_store.save_snapshot(snapshot, kind="new_period")
                return ExamCheckResult("new_period", snapshot)
            changes = detect_room_changes(previous, snapshot)
            kind = "rooms" if changes else "unchanged"
            self.state_store.save_snapshot(snapshot, kind="rooms" if changes else None, changes=changes)
            return ExamCheckResult(kind, snapshot, changes)
