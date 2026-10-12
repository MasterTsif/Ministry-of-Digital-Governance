"""Persist schedule PDF snapshots and pending alerts in SQLite."""

from contextlib import contextmanager
from dataclasses import asdict, dataclass
import json
from pathlib import Path
import sqlite3


@dataclass(frozen=True)
class PdfSnapshot:
    url: str
    title: str
    sha256: str
    etag: str | None
    last_modified: str | None
    checked_at: str
    page_modified: str | None


@dataclass(frozen=True)
class PdfNotification:
    id: int
    snapshot: PdfSnapshot


class PdfStateStore:
    """Persist the current file and an outbox in one SQLite transaction."""

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS schedule_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS notifications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    payload TEXT NOT NULL
                );
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

    def get_current_snapshot(self) -> PdfSnapshot | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT payload FROM schedule_state WHERE id = 1"
            ).fetchone()
        return PdfSnapshot(**json.loads(row[0])) if row else None

    def save_snapshot(self, snapshot: PdfSnapshot, *, notify: bool) -> None:
        """Save state and, when requested, queue its alert in one transaction."""
        payload = json.dumps(asdict(snapshot), ensure_ascii=False)
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO schedule_state (id, payload) VALUES (1, ?) "
                "ON CONFLICT(id) DO UPDATE SET payload = excluded.payload",
                (payload,),
            )
            if notify:
                connection.execute(
                    "INSERT INTO notifications (payload) VALUES (?)", (payload,)
                )

    def get_pending_notifications(self) -> list[PdfNotification]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT id, payload FROM notifications ORDER BY id"
            ).fetchall()
        return [PdfNotification(row[0], PdfSnapshot(**json.loads(row[1]))) for row in rows]

    def mark_notification_sent(self, notification_id: int) -> None:
        with self.connect() as connection:
            connection.execute(
                "DELETE FROM notifications WHERE id = ?", (notification_id,)
            )
