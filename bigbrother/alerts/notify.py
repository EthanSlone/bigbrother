"""Notification delivery.

Pushover: native apps on iPhone and Android, shows the snapshot. API rules we
follow (pushover.net/api): one attachment up to 5 MB per message (we resize
before sending); 4xx means the request itself is wrong, so it is never
retried (repeated 4xx can get the IP blocked); 5xx or no reply may be retried,
no sooner than 5 seconds later; requests are sent one at a time.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

log = logging.getLogger(__name__)

PUSHOVER_URL = "https://api.pushover.net/1/messages.json"
MAX_ATTACHMENT = 5_242_880


@dataclass
class Message:
    title: str
    body: str
    image_jpeg: bytes | None = None
    url: str = ""
    url_title: str = ""
    timestamp: int | None = None


@dataclass
class SendResult:
    ok: bool
    status: str        # sent | logged | failed
    detail: str = ""


class LogNotifier:
    """Used when no notification service is configured: records instead of sending."""

    name = "log"

    def send(self, msg: Message) -> SendResult:
        log.info("ALERT (not sent, no notifier configured): %s | %s", msg.title, msg.body)
        return SendResult(True, "logged", "no notifier configured: add PUSHOVER_USER and PUSHOVER_TOKEN to .env")


def encode_multipart(fields: dict[str, str], files: dict[str, tuple[str, bytes, str]]) -> tuple[bytes, str]:
    boundary = "bb-" + secrets.token_hex(12)
    out = bytearray()
    for k, v in fields.items():
        out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n").encode()
    for k, (filename, data, ctype) in files.items():
        out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"; filename=\"{filename}\"\r\n"
                f"Content-Type: {ctype}\r\n\r\n").encode()
        out += data + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return bytes(out), f"multipart/form-data; boundary={boundary}"


class PushoverNotifier:
    name = "pushover"

    def __init__(self, user: str, token: str, priority: int = 0, sound: str = "",
                 url: str = PUSHOVER_URL, attempts: int = 3, retry_wait: float = 5.0):
        self.user, self.token = user, token
        self.priority, self.sound = priority, sound
        self.url, self.attempts, self.retry_wait = url, attempts, retry_wait
        self.remaining: int | None = None  # monthly messages left, from response headers

    def build(self, msg: Message) -> tuple[bytes, str]:
        fields = {"token": self.token, "user": self.user, "title": msg.title[:250], "message": msg.body[:1024],
                  "priority": str(self.priority)}
        if self.sound:
            fields["sound"] = self.sound
        if msg.url:
            fields["url"] = msg.url[:512]
            fields["url_title"] = (msg.url_title or "Open")[:100]
        if msg.timestamp:
            fields["timestamp"] = str(int(msg.timestamp))
        files = {}
        if msg.image_jpeg and len(msg.image_jpeg) <= MAX_ATTACHMENT:
            files["attachment"] = ("snapshot.jpg", msg.image_jpeg, "image/jpeg")
        return encode_multipart(fields, files)

    def send(self, msg: Message) -> SendResult:
        body, ctype = self.build(msg)
        last = ""
        for attempt in range(1, self.attempts + 1):
            req = urllib.request.Request(self.url, data=body, headers={"Content-Type": ctype}, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    rem = resp.headers.get("X-Limit-App-Remaining")
                    self.remaining = int(rem) if rem and rem.isdigit() else self.remaining
                    data = json.loads(resp.read() or b"{}")
                    if data.get("status") == 1:
                        return SendResult(True, "sent", f"request {data.get('request', '?')}")
                    return SendResult(False, "failed", f"Pushover said: {data}")
            except urllib.error.HTTPError as exc:
                detail = _http_error_detail(exc)
                if 400 <= exc.code < 500:
                    # The request itself is wrong (bad key, limit reached...): retrying can't help.
                    hint = " (monthly message limit reached)" if exc.code == 429 else ""
                    return SendResult(False, "failed", f"HTTP {exc.code}{hint}: {detail}")
                last = f"HTTP {exc.code}: {detail}"
            except Exception as exc:  # noqa: BLE001 - network trouble: temporary, retry
                last = f"{type(exc).__name__}: {exc}"
            if attempt < self.attempts:
                log.warning("Pushover attempt %d failed (%s); retrying in %.0fs", attempt, last, self.retry_wait)
                time.sleep(self.retry_wait)
        return SendResult(False, "failed", last)


def _http_error_detail(exc: urllib.error.HTTPError) -> str:
    try:
        data = json.loads(exc.read() or b"{}")
        return "; ".join(data.get("errors") or []) or str(data)
    except Exception:  # noqa: BLE001
        return exc.reason or ""


def validate_user(token: str, user: str, url: str = PUSHOVER_URL) -> tuple[bool, str]:
    """Ask Pushover whether a user or group key is real (pushover.net/api#verification)."""
    body = urllib.parse.urlencode({"token": token, "user": user}).encode()
    req = urllib.request.Request(url.replace("messages.json", "users/validate.json"), data=body, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        if 400 <= exc.code < 500:
            detail = _http_error_detail(exc)
            if "token" in detail and "user" not in detail:
                return False, "Pushover rejected the app token. An admin can fix it under Settings, Alerts."
            return False, "Pushover doesn't recognise that user key. Copy it from the top of pushover.net after signing in."
        return True, f"couldn't check (Pushover HTTP {exc.code}); saved anyway"
    except Exception as exc:  # noqa: BLE001 - offline: don't block saving
        return True, f"couldn't reach Pushover to check ({type(exc).__name__}); saved anyway"
    if data.get("status") == 1:
        devices = data.get("devices") or []
        return True, f"devices: {', '.join(devices)}" if devices else "key is valid"
    return False, "Pushover doesn't recognise that user key."


class RoutedNotifier:
    """Sends to the people who asked for this camera's alerts, in one Pushover request.

    Pushover takes up to 50 comma-separated user keys per message, so a visit costs one
    request whoever is listening. Without a token or anyone to send to, alerts are logged.
    """

    def __init__(self, directory, priority: int = 0, sound: str = "", url: str = PUSHOVER_URL,
                 factory=PushoverNotifier):
        self.directory, self.priority, self.sound, self.url = directory, priority, sound, url
        self.factory = factory
        self.remaining: int | None = None

    @property
    def name(self) -> str:
        token, people = self.directory.load()
        return "pushover" if token and people else "log"

    def targets(self, camera: str | None = None, health: bool = False) -> list[str]:
        _, people = self.directory.load()
        return sorted({r.key for r in people if r.wants(camera, health)})

    def send(self, msg: Message, camera: str | None = None, health: bool = False,
             keys: list[str] | None = None) -> SendResult:
        token, people = self.directory.load()
        if keys is None:
            keys = self.targets(camera, health)
        if not token or not keys:
            why = ("no Pushover app token: an admin can add one under Settings, Alerts" if not token else
                   "nobody has phone alerts set up for this" if people else
                   "nobody has added a Pushover key yet (Settings, Your account)")
            log.info("ALERT (not sent, %s): %s | %s", why, msg.title, msg.body)
            return SendResult(True, "logged", why)
        sender = self.factory(",".join(keys[:50]), token, self.priority, self.sound, url=self.url)
        res = sender.send(msg)
        self.remaining = getattr(sender, "remaining", None) or self.remaining
        return res


def make_notifier(kind: str, priority: int = 0, sound: str = "", web_db=None):
    if kind == "pushover":
        from .recipients import Directory
        # BB_PUSHOVER_URL exists only so tests can point at a local stand-in.
        return RoutedNotifier(Directory(web_db), priority, sound, url=os.environ.get("BB_PUSHOVER_URL", PUSHOVER_URL))
    return LogNotifier()
