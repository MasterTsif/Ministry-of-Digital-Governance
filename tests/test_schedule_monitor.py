"""Integration tests for PDF monitoring and its persisted notification queue."""

from contextlib import asynccontextmanager
from pathlib import Path
import tempfile
import unittest

import aiohttp
from aiohttp import web

from ministry_bot.schedule.pdf_monitor import (
    PdfMonitorError, SchedulePdfClient, SchedulePdfMonitor,
)
from ministry_bot.schedule.pdf_storage import PdfStateStore
from tests.simulations.pdf_update import build_test_pdf, simulate_pdf_update


@asynccontextmanager
async def serve_pdf_source(state):
    """Serve a mutable PDF so failures exercise the actual HTTP client."""
    async def page(request):
        return web.json_response({
            "content": {"rendered": '<a href="/schedule.pdf">Class timetable</a>'},
        })

    async def pdf(request):
        return web.Response(body=state["body"], status=state.get("status", 200))

    app = web.Application()
    app.router.add_get("/api", page)
    app.router.add_get("/schedule.pdf", pdf)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    try:
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        port = runner.addresses[0][1]
        yield f"http://127.0.0.1:{port}/api"
    finally:
        await runner.cleanup()


class ScheduleMonitorTests(unittest.IsolatedAsyncioTestCase):
    async def test_pdf_update_queues_one_persistent_notification(self):
        async with simulate_pdf_update() as store:
            reopened = PdfStateStore(store.path)
            notifications = reopened.get_pending_notifications()
            self.assertEqual(len(notifications), 1)
            current = reopened.get_current_snapshot()
            self.assertEqual(notifications[0].snapshot.sha256, current.sha256)
            self.assertEqual(notifications[0].snapshot.url, current.url)
            reopened.mark_notification_sent(notifications[0].id)
            self.assertEqual(PdfStateStore(store.path).get_pending_notifications(), [])

    async def test_invalid_download_retains_previous_snapshot(self):
        state = {"body": build_test_pdf(1)}
        async with serve_pdf_source(state) as api_url:
            async with aiohttp.ClientSession(trust_env=False) as session:
                with tempfile.TemporaryDirectory() as directory:
                    store = PdfStateStore(Path(directory) / "bot.sqlite3")
                    monitor = SchedulePdfMonitor(SchedulePdfClient(session, api_url), store)
                    baseline = await monitor.check_for_pdf_changes()
                    self.assertEqual(baseline.kind, "baseline")
                    for invalid_body in (b"<html>Error page</html>", b"%PDF-1.4\ntruncated"):
                        with self.subTest(body=invalid_body):
                            state["body"] = invalid_body
                            with self.assertRaises(PdfMonitorError):
                                await monitor.check_for_pdf_changes()
                            self.assertEqual(store.get_current_snapshot(), baseline.snapshot)
                            self.assertEqual(store.get_pending_notifications(), [])

    async def test_unchanged_pdf_without_cache_headers_does_not_notify(self):
        state = {"body": build_test_pdf(1)}
        async with serve_pdf_source(state) as api_url:
            async with aiohttp.ClientSession(trust_env=False) as session:
                with tempfile.TemporaryDirectory() as directory:
                    store = PdfStateStore(Path(directory) / "bot.sqlite3")
                    monitor = SchedulePdfMonitor(SchedulePdfClient(session, api_url), store)
                    await monitor.check_for_pdf_changes()
                    result = await monitor.check_for_pdf_changes()
                    self.assertEqual(result.kind, "unchanged")
                    self.assertEqual(store.get_pending_notifications(), [])

    async def test_unexpected_304_without_baseline_does_not_save_state(self):
        state = {"body": b"", "status": 304}
        async with serve_pdf_source(state) as api_url:
            async with aiohttp.ClientSession(trust_env=False) as session:
                with tempfile.TemporaryDirectory() as directory:
                    store = PdfStateStore(Path(directory) / "bot.sqlite3")
                    monitor = SchedulePdfMonitor(SchedulePdfClient(session, api_url), store)
                    with self.assertRaises(PdfMonitorError):
                        await monitor.check_for_pdf_changes()
                    self.assertIsNone(store.get_current_snapshot())


if __name__ == "__main__":
    unittest.main()
