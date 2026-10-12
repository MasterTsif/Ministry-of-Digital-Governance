"""Exercise exam changes using a local server and temporary database."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
import tempfile

import aiohttp
from aiohttp import web
from icalendar import Calendar, Event

from ministry_bot.exams.exam_monitor import ExamCalendarClient, ExamStateStore, ExamMonitor, ExamMonitorError


def build_test_exam_calendar(period: int, room: str) -> bytes:
    calendar = Calendar()
    calendar.add("PRODID", "Ministry of Digital Governance test")
    calendar.add("VERSION", "2.0")
    event = Event()
    start = datetime(2027, 1, 20, 7, tzinfo=UTC)
    event.add("UID", f"test-exam-{period}")
    event.add("SUMMARY", "1001.T.Test course (v.1.00)")
    event.add("DTSTART", start)
    event.add("DTEND", start + timedelta(hours=2))
    event.add("DTSTAMP", start)
    event.add("LOCATION", room)
    calendar.add_component(event)
    return calendar.to_ical()


@asynccontextmanager
async def simulate_exam_update(scenario: str):
    """Exercise one change using local HTTP fixtures and a temporary database."""
    if scenario not in {"new_period", "rooms"}:
        raise ValueError("Use new_period or rooms for the exam simulation.")
    period = 1
    room = "Room 101"

    async def page(request):
        return web.Response(text=(
            f'<div class="panel-heading">Current exam period: TEST period {period}</div>'
            f'<a href="program.php?full=1&sem_id={period}">Full program</a>'
            f'<a href="ical.php?full=1&sem_id={period}">Calendar</a>'
        ), content_type="text/html")

    async def calendar(request):
        return web.Response(body=build_test_exam_calendar(period, room), content_type="text/calendar")

    app = web.Application()
    app.router.add_get("/", page)
    app.router.add_get("/ical.php", calendar)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    try:
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        port = runner.addresses[0][1]
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10), trust_env=False) as session:
            with tempfile.TemporaryDirectory(prefix="ministry-bot-exam-test-") as directory:
                store = ExamStateStore(Path(directory) / "test.sqlite3")
                watcher = ExamMonitor(ExamCalendarClient(session, f"http://127.0.0.1:{port}/"), store)
                baseline = await watcher.check_for_exam_changes()
                if scenario == "new_period":
                    period = 2
                else:
                    room = "Room 202"
                changed = await watcher.check_for_exam_changes()
                unchanged = await watcher.check_for_exam_changes()
                if (baseline.kind, changed.kind, unchanged.kind) != ("baseline", scenario, "unchanged"):
                    raise ExamMonitorError("The exam simulation produced an unexpected result.")
                if len(store.get_pending_notifications()) != 1:
                    raise ExamMonitorError("The exam simulation did not queue exactly one notification.")
                yield store
    finally:
        await runner.cleanup()
