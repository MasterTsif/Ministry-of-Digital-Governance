"""Integration tests for exam publication, room changes and retained state."""

from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock

from ministry_bot.exams.exam_monitor import (
    ExamCalendarClient, ExamMonitor, ExamMonitorError, ExamStateStore,
)
from tests.helpers import exam_snapshot
from tests.simulations.exam_update import simulate_exam_update


class ExamMonitorTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_period_queues_one_publication_notification(self):
        async with simulate_exam_update("new_period") as store:
            reopened = ExamStateStore(store.path)
            pending = reopened.get_pending_notifications()
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0].kind, "new_period")
            self.assertEqual(reopened.get_current_snapshot().period.id, 2)
            reopened.mark_notification_sent(pending[0].id)
            self.assertEqual(ExamStateStore(store.path).get_pending_notifications(), [])

    async def test_room_update_records_previous_and_new_rooms(self):
        async with simulate_exam_update("rooms") as store:
            notification, = store.get_pending_notifications()
            self.assertEqual(notification.kind, "rooms")
            change, = notification.changes
            self.assertEqual(change.before.rooms, ("Room 101",))
            self.assertEqual(change.after.rooms, ("Room 202",))
            self.assertEqual(store.get_current_snapshot().period.id, 1)

    async def test_invalid_source_update_preserves_saved_exam_state(self):
        baseline = exam_snapshot(period_id=2)
        with tempfile.TemporaryDirectory() as directory:
            store = ExamStateStore(Path(directory) / "bot.sqlite3")
            store.save_snapshot(baseline)
            client = AsyncMock(spec=ExamCalendarClient)
            monitor = ExamMonitor(client, store)
            for invalid in (exam_snapshot(period_id=1), replace(baseline, events=())):
                with self.subTest(period=invalid.period.id, events=len(invalid.events)):
                    client.fetch_exam_snapshot.return_value = invalid
                    with self.assertRaises(ExamMonitorError):
                        await monitor.check_for_exam_changes()
                    self.assertEqual(store.get_current_snapshot(), baseline)
                    self.assertEqual(store.get_pending_notifications(), [])

    async def test_waiting_period_notifies_once_when_exams_are_published(self):
        published = exam_snapshot()
        waiting = replace(published, events=())
        client = AsyncMock(spec=ExamCalendarClient)
        client.fetch_exam_snapshot.side_effect = [waiting, published, published]
        with tempfile.TemporaryDirectory() as directory:
            store = ExamStateStore(Path(directory) / "bot.sqlite3")
            monitor = ExamMonitor(client, store)
            results = [await monitor.check_for_exam_changes() for _ in range(3)]
            self.assertEqual([result.kind for result in results], ["waiting", "new_period", "unchanged"])
            notification, = store.get_pending_notifications()
            self.assertEqual(notification.kind, "new_period")


if __name__ == "__main__":
    unittest.main()
