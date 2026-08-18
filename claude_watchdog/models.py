"""Datenmodelle: Task, Enums, Observation, Decision."""

from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Optional


class Mode(str, Enum):
    """Betriebsart eines Tasks.

    MANAGED  - vom Watchdog selbst headless gestartet, volle Kontrolle.
    OBSERVED - vom Benutzer interaktiv gestartet, nur beobachten + melden.
    """

    MANAGED = "managed"
    OBSERVED = "observed"


class Status(str, Enum):
    PENDING = "pending"                    # angelegt, noch nicht gestartet
    RUNNING = "running"                    # arbeitet
    STALLED = "stalled"                    # lebt, aber kein Fortschritt
    BLOCKED = "blocked"                    # wartet auf Benutzereingabe
    WAITING_FOR_LIMIT = "waiting_for_limit"  # wartet auf Reset / Backoff
    DONE = "done"
    FAILED = "failed"
    PAUSED = "paused"                      # manuell pausiert

    @property
    def is_terminal(self) -> bool:
        return self in (Status.DONE, Status.FAILED)


class ErrorClass(str, Enum):
    NONE = "NONE"
    USAGE_LIMIT = "USAGE_LIMIT"
    RATE_LIMIT = "RATE_LIMIT"
    API_ERROR = "API_ERROR"
    NETWORK = "NETWORK"
    CONTEXT = "CONTEXT"
    CRASH = "CRASH"
    STALLED = "STALLED"
    AWAITING_INPUT = "AWAITING_INPUT"
    UNKNOWN = "UNKNOWN"


class Action(str, Enum):
    """Was der Watchdog als naechstes tut."""

    NONE = "none"                    # alles in Ordnung, nichts tun
    NOTIFY = "notify"                # nur melden
    START = "start"                  # managed-Task erstmalig starten
    RESUME = "resume"                # claude -r <session_id>
    RESTART_FRESH = "restart_fresh"  # neue Session mit verdichtetem Kontext
    SCHEDULE = "schedule"            # Wartezeit setzen, spaeter erneut pruefen
    FAIL = "fail"                    # aufgeben
    COMPLETE = "complete"            # erfolgreich beendet


#: Aktionen, die tatsaechlich in eine Session eingreifen. Fuer observed-Tasks
#: sind ausschliesslich diese hier verboten - der Guard in recovery.py haengt
#: an dieser Menge.
INTRUSIVE_ACTIONS = frozenset({Action.START, Action.RESUME, Action.RESTART_FRESH})


@dataclass
class Task:
    id: str
    title: str
    cwd: str
    mode: Mode
    status: Status
    original_prompt: str = ""
    session_id: Optional[str] = None
    pid: Optional[int] = None
    transcript_path: Optional[str] = None
    attempts: int = 0
    max_attempts: int = 5
    no_auto_resume: bool = False
    model: Optional[str] = None
    permission_mode: Optional[str] = None
    max_budget_usd: Optional[float] = None
    last_error_class: Optional[str] = None
    last_error_text: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    last_progress_at: Optional[float] = None
    #: Wann der Watchdog fruehestens wieder **eingreifen** darf.
    next_retry_at: Optional[float] = None
    #: Wann er fruehestens wieder ueber dasselbe **melden** darf. Bewusst
    #: getrennt von next_retry_at: eine Meldung darf die Beobachtung nicht
    #: aussetzen (siehe recovery._act_notify).
    mute_until: Optional[float] = None
    cost_usd_spent: float = 0.0
    transcript_size: int = 0
    last_resume_marker: Optional[str] = None
    same_marker_count: int = 0

    def to_row(self) -> dict[str, Any]:
        d = asdict(self)
        d["mode"] = self.mode.value
        d["status"] = self.status.value
        d["no_auto_resume"] = 1 if self.no_auto_resume else 0
        return d

    @classmethod
    def from_row(cls, row: Any) -> "Task":
        d = dict(row)
        d["mode"] = Mode(d["mode"])
        d["status"] = Status(d["status"])
        d["no_auto_resume"] = bool(d["no_auto_resume"])
        return cls(**d)


@dataclass
class Observation:
    """Momentaufnahme der Gesundheit eines Tasks."""

    alive: bool = False
    #: Transkript ist seit der letzten Pruefung gewachsen.
    progressed: bool = False
    transcript_size: int = 0
    #: Sekunden seit dem letzten beobachteten Fortschritt.
    idle_seconds: float = 0.0
    #: Status aus `claude agents --json` (busy/idle/None).
    agent_status: Optional[str] = None
    #: True, wenn die Session in `claude agents --json` auftaucht.
    known_to_cli: bool = False
    #: True, wenn `claude agents --json` ueberhaupt erreichbar war. Nur dann
    #: ist ein "kennt die Session nicht" eine belastbare Aussage.
    cli_usable: bool = False
    #: Exit-Code eines beendeten managed-Laufs.
    exit_code: Optional[int] = None
    #: Strukturierte Events aus Transkript/Run-Log (letzte N).
    events: list[dict[str, Any]] = field(default_factory=list)
    #: Rohtext fuer die Regex-Fallback-Klassifikation.
    tail_text: str = ""

    @property
    def stalled(self) -> bool:
        return self.alive and not self.progressed


@dataclass
class Decision:
    action: Action
    reason: str
    error_class: ErrorClass = ErrorClass.NONE
    #: Sekunden, die bis zum naechsten Versuch gewartet wird.
    delay: float = 0.0
    #: Absoluter Zeitpunkt fuer den naechsten Versuch (hat Vorrang vor delay).
    retry_at: Optional[float] = None
    #: Text fuer die Benachrichtigung, falls eine faellig ist.
    notify: Optional[str] = None
    #: Wird der Versuchszaehler erhoeht?
    counts_as_attempt: bool = False

    def effective_retry_at(self, now: Optional[float] = None) -> Optional[float]:
        if self.retry_at is not None:
            return self.retry_at
        if self.delay:
            return (now if now is not None else time.time()) + self.delay
        return None
