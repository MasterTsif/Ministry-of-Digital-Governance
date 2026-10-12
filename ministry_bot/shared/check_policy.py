"""Choose allowed monitoring times and the next check interval."""

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class CheckPolicy:
    timezone: ZoneInfo
    morning_start: time
    evening_start: time
    night_start: time
    day_interval: int
    evening_interval: int
    active_weekdays: frozenset[int]

    def interval_at(self, now: datetime) -> int | None:
        local = now.astimezone(self.timezone)
        if local.weekday() not in self.active_weekdays:
            return None
        clock = local.time()
        if self.morning_start <= clock < self.evening_start:
            return self.day_interval
        if self.evening_start <= clock < self.night_start:
            return self.evening_interval
        return None

    def next_run(self, now: datetime, *, after_check: bool = False) -> datetime:
        """Return the next permitted check; arithmetic uses real elapsed time."""
        local = now.astimezone(self.timezone)
        interval = self.interval_at(local)
        if after_check and interval is not None:
            local = (local.astimezone(UTC) + timedelta(seconds=interval)).astimezone(
                self.timezone
            )
        for offset in range(8):
            day = local.date() + timedelta(days=offset)
            if day.weekday() not in self.active_weekdays:
                continue
            start = datetime.combine(day, self.morning_start, self.timezone)
            end = datetime.combine(day, self.night_start, self.timezone)
            if local < start:
                return start
            if local < end:
                return local
        raise RuntimeError("No active check window was found.")

    def delay_until(self, target: datetime, now: datetime) -> float:
        return max(1.0, (target.astimezone(UTC) - now.astimezone(UTC)).total_seconds())
