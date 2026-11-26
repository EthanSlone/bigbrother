"""Automatic arming: alerts turn on at the start of each window and off at its end.

Only the moments of change act, so turning alerts on or off by hand (the lamp, Home
Assistant) sticks until the next start or end. A new or edited schedule applies at once.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import time
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)
KEY = "arm_schedule_state"


def signature(windows) -> str:
    return json.dumps([[sorted(w.days), w.start.isoformat(), w.end.isoformat()] for w in windows])


class ArmSchedule:
    def __init__(self, windows, store, timezone: str, publish=None, clock=time.time):
        self.windows, self.store, self.publish, self.clock = tuple(windows), store, publish, clock
        self.tz = ZoneInfo(timezone)
        self.sig = signature(self.windows)

    def tick(self) -> bool | None:
        """Returns the state it switched to, or None when nothing changed."""
        if not self.windows:
            return None
        now = dt.datetime.fromtimestamp(self.clock(), self.tz)
        want = any(w.active(now) for w in self.windows)
        try:
            last = json.loads(self.store.setting(KEY) or "null")
        except json.JSONDecodeError:
            last = None
        if last and last.get("sig") == self.sig and last.get("want") == want:
            return None
        self.store.set_setting(KEY, json.dumps({"sig": self.sig, "want": want}))
        if self.store.armed() != want:
            self.store.set_armed(want)
            log.info("alerts %s by the schedule", "armed" if want else "disarmed")
            if self.publish:
                self.publish("alerts/armed", "on" if want else "off", True)
        return want
