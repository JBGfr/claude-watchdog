"""Benachrichtigungskanal.

Bewusst hinter einer schmalen Schnittstelle, damit spaeter ein zweiter Kanal
(z.B. Mail) danebengestellt werden kann, ohne den Rest anzufassen.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from typing import Protocol

from . import config
from .logging_setup import get

log = get("notifier")

URGENCY_LOW = "low"
URGENCY_NORMAL = "normal"
URGENCY_CRITICAL = "critical"


class Notifier(Protocol):
    def send(self, title: str, body: str, urgency: str = URGENCY_NORMAL) -> None:
        ...


class LogOnlyNotifier:
    """Fallback: schreibt nur ins Log (dry-run, Server ohne Desktop)."""

    def __init__(self, reason: str = ""):
        self.reason = reason

    def send(self, title: str, body: str, urgency: str = URGENCY_NORMAL) -> None:
        log.info("notification (log-only)", extra={
            "title": title, "body": body, "urgency": urgency, "reason": self.reason,
        })


class NotifySendNotifier:
    """Desktop-Meldung ueber notify-send — bewusst ohne zu warten.

    Frueher lief das ueber `subprocess.run(..., timeout=10)`. Steht der
    Benachrichtigungsdienst nicht bereit, blockiert `notify-send` auf D-Bus,
    und der Supervisor stand bis zu zehn Sekunden je Meldung still — bei
    einem Poll-Takt von 15 s. Am 2026-07-31 nach einem Neustart genau so
    passiert: drei Meldungen zwischen 14:15 und 14:17, zusammen rund 30
    Sekunden Stillstand, weil xfce4-notifyd noch nicht lief.

    Eine Meldung ist Nebensache. Die Ueberwachung darf darauf nie warten,
    deshalb wird nur gestartet und beim naechsten Mal eingesammelt.
    """

    #: Laenger darf ein notify-send nicht haengen, dann wird es abgeraeumt.
    HAENGT_AB = 30

    def __init__(self, binary: str | None = None):
        self.binary = binary or config.NOTIFY_BIN
        #: Gestartete, noch nicht eingesammelte Aufrufe: [(Startzeit, Popen)].
        self._offen: list[tuple[float, subprocess.Popen]] = []

    def _einsammeln(self, now: float) -> None:
        """Fertige Aufrufe abholen, haengende abraeumen.

        Ohne das blieben Zombies stehen; und ein dauerhaft haengendes
        notify-send wuerde sich sonst bei jeder Meldung neu ansammeln.
        """
        offen = []
        for start, p in self._offen:
            rc = p.poll()
            if rc is not None:
                if rc != 0:
                    log.warning("notify-send endete mit Fehler",
                                extra={"rc": rc})
                continue
            if now - start > self.HAENGT_AB:
                p.kill()
                # wait() statt poll(): nach dem Signal ist der Prozess noch
                # nicht abgeholt, und ein nicht abgeholtes Kind bleibt als
                # Zombie stehen — in einem Dauerlaeufer sammeln die sich an.
                try:
                    p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    log.warning("notify-send liess sich nicht beenden",
                                extra={"pid": p.pid})
                log.warning("notify-send haengt, abgeraeumt",
                            extra={"nach_s": round(now - start)})
                continue
            offen.append((start, p))
        self._offen = offen

    def send(self, title: str, body: str, urgency: str = URGENCY_NORMAL) -> None:
        log.info("notification", extra={"title": title, "body": body, "urgency": urgency})
        now = time.time()
        self._einsammeln(now)
        try:
            p = subprocess.Popen(
                [self.binary, "-a", "Claude Watchdog", "-u", urgency, title, body],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
            )
        except OSError as exc:
            log.warning("notify-send fehlgeschlagen", extra={"error": str(exc)})
            return
        self._offen.append((now, p))


class MultiNotifier:
    def __init__(self, *channels: Notifier):
        self.channels = [c for c in channels if c is not None]

    def send(self, title: str, body: str, urgency: str = URGENCY_NORMAL) -> None:
        for channel in self.channels:
            channel.send(title, body, urgency)


def build(dry_run: bool = False) -> Notifier:
    if dry_run:
        return LogOnlyNotifier("dry-run")
    if not config.NOTIFY_ENABLED:
        return LogOnlyNotifier("CW_NOTIFY=0")
    if shutil.which(config.NOTIFY_BIN) is None:
        return LogOnlyNotifier(f"{config.NOTIFY_BIN} nicht gefunden")
    return NotifySendNotifier()
