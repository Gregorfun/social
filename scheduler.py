from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from typing import Callable

import schedule

log = logging.getLogger(__name__)


class DailySlotScheduler:
    def __init__(self, posting_slots: list[str], callback: Callable[[str], None], poll_interval_seconds: int = 20):
        self.posting_slots = posting_slots
        self.callback = callback
        self.poll_interval_seconds = poll_interval_seconds

    def start(self):
        schedule.clear("daily-posting-slots")
        for slot in self.posting_slots:
            schedule.every().day.at(slot).do(self._run_slot, slot).tag("daily-posting-slots")
            log.info("Slot geplant: %s", slot)

        next_slot = self.get_next_scheduled_time()
        if next_slot is not None:
            log.info("Naechster geplanter Slot: %s", next_slot.strftime("%d.%m.%Y %H:%M:%S"))

        while True:
            schedule.run_pending()
            time.sleep(self.poll_interval_seconds)

    def _run_slot(self, slot: str):
        log.info("Geplanter Slot erreicht: %s", slot)
        self.callback(slot)

    def get_next_scheduled_time(self) -> datetime | None:
        now = datetime.now()
        candidates: list[datetime] = []
        for slot in self.posting_slots:
            hour_text, minute_text = slot.split(":", maxsplit=1)
            candidate = now.replace(hour=int(hour_text), minute=int(minute_text), second=0, microsecond=0)
            if candidate <= now:
                candidate += timedelta(days=1)
            candidates.append(candidate)
        return min(candidates) if candidates else None