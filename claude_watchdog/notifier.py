"""Benachrichtigungskanal.

Bewusst hinter einer schmalen Schnittstelle, damit spaeter ein zweiter Kanal
(z.B. Mail) danebengestellt werden kann, ohne den Rest anzufassen.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from typing import Callable, Protocol

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

    Dazu eine gleitende Obergrenze (CW_NOTIFY_MAX_PER_HOUR): hoechstens N
    Meldungen je Stunde landen auf dem Desktop, alles weitere nur noch im Log.
    Beim Zudrehen geht genau eine Meta-Meldung raus ("weitere Meldungen bis
    HH:MM nur im Log"), denn eine schweigende Drossel sieht von aussen aus wie
    ein kaputter Melder.
    """

    #: Laenger darf ein notify-send nicht haengen, dann wird es abgeraeumt.
    HAENGT_AB = 30

    #: Laenge des gleitenden Fensters der Stundengrenze in Sekunden.
    FENSTER = 3600

    def __init__(self, binary: str | None = None,
                 max_per_hour: int | None = None,
                 clock: Callable[[], float] = time.monotonic):
        self.binary = binary or config.NOTIFY_BIN
        #: Hoechstens so viele Desktop-Meldungen je gleitender Stunde,
        #: <= 0 bedeutet unbegrenzt.
        self.max_per_hour = (
            config.NOTIFY_MAX_PER_HOUR if max_per_hour is None else max_per_hour
        )
        #: Zeitquelle der Grenze, injizierbar fuer Tests ohne sleep. Monoton,
        #: damit ein NTP-Sprung oder die Zeitumstellung die Drossel weder
        #: aufreisst noch fuer eine Stunde zudreht.
        self.clock = clock
        #: Gestartete, noch nicht eingesammelte Aufrufe: [(Startzeit, Popen)].
        self._offen: list[tuple[float, subprocess.Popen]] = []
        #: Zeitpunkte (clock) der zugestellten Meldungen im laufenden Fenster.
        self._fenster: list[float] = []
        #: Ende der laufenden Drosselperiode, solange sie laeuft. Dient
        #: zugleich als Merker, dass die Meta-Meldung schon raus ist.
        self._gedrosselt_bis: float | None = None

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

    def _darf_auf_den_desktop(self, now: float) -> bool:
        """Stundengrenze pruefen; beim Zudrehen einmalig die Meta-Meldung.

        Die Grenze ist eine harte Obergrenze fuer ALLES, was auf dem Desktop
        landet - die Meta-Meldung eingeschlossen.

        `now` ist Wanduhrzeit und wird nur fuer den lesbaren Zeitpunkt im
        Text gebraucht; gerechnet wird mit self.clock().
        """
        if self.max_per_hour <= 0:
            return True
        jetzt = self.clock()
        self._fenster = [t for t in self._fenster if jetzt - t < self.FENSTER]
        # Der letzte Platz der Stunde gehoert der Drosselmeldung. Sonst waere
        # die Grenze keine: die Meldung kaeme ZUSAETZLICH zu den N Blasen, und
        # bei Dauerflut wurden so 5 statt 4 Blasen pro Stunde gemessen. Bei
        # max_per_hour == 1 gaebe es dann gar keine echte Meldung mehr - dort
        # gilt der eine Platz der echten Meldung, ohne Drosselhinweis.
        fuer_echte = self.max_per_hour - 1 if self.max_per_hour > 1 else 1
        if len(self._fenster) < fuer_echte:
            self._fenster.append(jetzt)
            self._gedrosselt_bis = None
            return True
        if self._gedrosselt_bis is None and len(self._fenster) < self.max_per_hour:
            # Die Meta-Meldung belegt diesen Platz selbst - nur angehaengt,
            # nie gegen eine aeltere getauscht: ein Tausch haette eine noch
            # gueltige Meldung vergessen und damit Kapazitaet freigegeben.
            self._fenster.append(jetzt)
            self._gedrosselt_bis = self._fenster[0] + self.FENSTER
            rest = max(0.0, self._gedrosselt_bis - jetzt)
            bis = time.strftime("%H:%M", time.localtime(now + rest))
            titel = "Claude Watchdog: Meldungen gedrosselt"
            text = ("Grenze von %d Meldungen pro Stunde erreicht. "
                    "Weitere Meldungen bis %s nur im Log." % (self.max_per_hour, bis))
            log.info("notification (drossel)", extra={
                "title": titel, "body": text, "urgency": URGENCY_LOW,
                "max_per_hour": self.max_per_hour, "bis": bis,
            })
            self._zustellen(titel, text, URGENCY_LOW, now)
        return False

    def _zustellen(self, title: str, body: str, urgency: str, now: float) -> None:
        """Startet notify-send, ohne auf das Ende zu warten."""
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

    def send(self, title: str, body: str, urgency: str = URGENCY_NORMAL) -> None:
        # Das Log bekommt immer alles - die Grenze gilt nur fuer notify-send,
        # sonst wuerde die Drossel die Nachvollziehbarkeit mitnehmen.
        log.info("notification", extra={"title": title, "body": body, "urgency": urgency})
        now = time.time()
        self._einsammeln(now)
        if not self._darf_auf_den_desktop(now):
            return
        self._zustellen(title, body, urgency, now)


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
