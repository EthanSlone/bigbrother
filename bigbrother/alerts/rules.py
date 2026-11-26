"""Alert rules: should this event notify you, and if not, why not?

Rules run on every event start/update until the event alerts or ends, because
some conditions change while an object is in view: it walks into the porch
zone, or a "parked" car drives off.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from ..config import AlertSettings, Camera


VEHICLES = {"car", "truck", "bus", "motorcycle"}


@dataclass(frozen=True)
class Decision:
    alert: bool
    reason: str = ""      # why not, in plain words
    final: bool = False   # True: nothing about this event can change the answer


def evaluate(event: dict, cam: Camera, settings: AlertSettings, armed: bool,
             last_alert_ts: float | None, now_ts: float, now_local: dt.datetime) -> Decision:
    a = cam.alert
    label = event["label"]
    if not settings.enabled:
        return Decision(False, "alerts are turned off", final=True)
    if not a.enabled:
        return Decision(False, f"alerts are off for {cam.name}", final=True)
    if label not in a.labels:
        return Decision(False, f"{label} doesn't alert on this camera (alerts for: {', '.join(a.labels)})")
    if not armed:
        return Decision(False, "disarmed")
    if a.schedule and not any(w.active(now_local) for w in a.schedule):
        return Decision(False, "outside the alert schedule")
    if event.get("stationary"):
        return Decision(False, "stationary (e.g. a parked car)")
    # A vehicle has to actually go somewhere before it alerts: a parked car that the detector
    # loses and finds again (someone walks past, night, a restart) is otherwise a "new" car.
    if settings.vehicles_must_move and label in VEHICLES and not event.get("moved", True):
        return Decision(False, "hasn't moved (looks parked)")
    if a.zones and not set(event.get("zones") or []) & set(a.zones):
        return Decision(False, f"hasn't entered {' / '.join(a.zones)}")
    if last_alert_ts is not None and now_ts - last_alert_ts < a.cooldown_seconds:
        ago = int(now_ts - last_alert_ts)
        return Decision(False, f"cooldown: a {label} alert was sent {ago}s ago")
    return Decision(True)
