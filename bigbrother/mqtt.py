"""Small MQTT publisher. MQTT is the bus every service reports on, so Home
Assistant can subscribe later without any changes here. Failures to reach
the broker are logged and never stop the calling service."""

from __future__ import annotations

import json
import logging
from typing import Any, Callable

import paho.mqtt.client as mqtt

log = logging.getLogger(__name__)


class Publisher:
    def __init__(self, host: str, port: int, prefix: str, client_id: str):
        self.prefix = prefix.rstrip("/")
        self.available_topic = f"{self.prefix}/{client_id}/available"
        self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
        self._client.will_set(self.available_topic, "offline", qos=1, retain=True)
        self._client.on_connect = self._on_connect
        self._client.reconnect_delay_set(min_delay=1, max_delay=30)
        self._client.on_message = self._on_message
        self._host, self._port = host, port
        self._subs: dict[str, Callable[[str, bytes], None]] = {}

    def subscribe(self, topic: str, callback: Callable[[str, bytes], None]) -> None:
        """Subscribe to prefix/topic; callback(full_topic, payload). Survives reconnects."""
        full = f"{self.prefix}/{topic}"
        self._subs[full] = callback
        if self._client.is_connected():
            self._client.subscribe(full, qos=1)

    def _on_message(self, client, userdata, msg) -> None:
        for pattern, cb in self._subs.items():
            if mqtt.topic_matches_sub(pattern, msg.topic):
                try:
                    cb(msg.topic, msg.payload)
                except Exception:  # noqa: BLE001 - a bad message must not kill the client
                    log.exception("MQTT handler failed for %s", msg.topic)

    def start(self) -> None:
        self._client.connect_async(self._host, self._port, keepalive=30)
        self._client.loop_start()

    def _on_connect(self, client, userdata, flags, reason_code, properties) -> None:
        if reason_code.is_failure:
            log.warning("MQTT connect failed: %s", reason_code)
            return
        log.info("MQTT connected to %s:%s", self._host, self._port)
        client.publish(self.available_topic, "online", qos=1, retain=True)
        for topic in self._subs:
            client.subscribe(topic, qos=1)

    def publish(self, topic: str, payload: Any, retain: bool = False) -> None:
        if not isinstance(payload, (str, bytes)):
            payload = json.dumps(payload)
        self._client.publish(f"{self.prefix}/{topic}", payload, qos=1, retain=retain)

    def stop(self) -> None:
        try:
            self._client.publish(self.available_topic, "offline", qos=1, retain=True).wait_for_publish(2)
        except Exception:  # noqa: BLE001
            pass
        self._client.loop_stop()
        self._client.disconnect()
