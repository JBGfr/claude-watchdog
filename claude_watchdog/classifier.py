"""Klassifikation von Unterbrechungen.

Zwei Stufen, bewusst in dieser Reihenfolge:

1. Strukturierte Signale aus den JSON-Events. Claude Code liefert
   `rate_limit_event.rate_limit_info` mit `status` und `resetsAt` (Unix-Epoch)
   sowie `result`-Events mit `is_error`/`subtype`/`api_error_status`. Das ist
   verlaesslich und wird zuerst ausgewertet.
2. Regex-Fallback ueber den Rohtext - fuer alles, was nur als Meldung
   auftaucht.

Innerhalb von Stufe 1 entscheidet die Aktualitaet: die Events werden von
hinten nach vorne EINMAL durchlaufen, das juengste verwertbare Signal gewinnt.
Ein alter Warnhinweis darf ein spaeteres `result: success` nicht mehr kippen.

Alle Textmuster stehen in EINER Tabelle (PATTERNS) und nirgends sonst.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Optional

from .models import ErrorClass

# --------------------------------------------------------------------------
# Die zentrale Mustertabelle. Reihenfolge = Prioritaet (spezifisch zuerst).
# Erweiterung: hier eine Zeile ergaenzen, sonst nirgends.
# --------------------------------------------------------------------------

PATTERNS: list[tuple[ErrorClass, str, str]] = [
    # --- Usage-Limit (Kontingent erschoepft) -------------------------------
    (ErrorClass.USAGE_LIMIT, r"claude\s+usage\s+limit\s+reached", "usage_limit_reached"),
    (ErrorClass.USAGE_LIMIT, r"\b(?:usage|session|weekly|monthly)\s+limit\s+reached", "limit_reached"),
    (ErrorClass.USAGE_LIMIT, r"\b(?:five|5)[-\s]?hour\s+limit", "five_hour_limit"),
    (ErrorClass.USAGE_LIMIT, r"you(?:'ve| have)\s+(?:reached|hit)\s+your\s+.{0,40}limit", "limit_reached_you"),
    (ErrorClass.USAGE_LIMIT, r"limit\s+(?:will\s+)?resets?\s+at", "limit_resets_at"),
    (ErrorClass.USAGE_LIMIT, r"out\s+of\s+(?:credits|usage)", "out_of_credits"),

    # --- Rate-Limit (kurzfristige Drosselung) ------------------------------
    (ErrorClass.RATE_LIMIT, r"\brate[\s_-]?limit(?:ed|_error)?\b", "rate_limit"),
    (ErrorClass.RATE_LIMIT, r"\btoo\s+many\s+requests\b", "too_many_requests"),
    (ErrorClass.RATE_LIMIT, r"\bhttp\s*429\b|\bstatus[\s:]*429\b|\b429\s+", "http_429"),

    # --- Kontext voll ------------------------------------------------------
    (ErrorClass.CONTEXT, r"model_context_window_exceeded", "ctx_window_exceeded"),
    (ErrorClass.CONTEXT, r"context\s+(?:window|length)\s+(?:exceeded|too\s+long|full)", "ctx_window"),
    (ErrorClass.CONTEXT, r"prompt\s+is\s+too\s+long", "prompt_too_long"),
    (ErrorClass.CONTEXT, r"exceeds?\s+(?:the\s+)?(?:maximum\s+)?context", "exceeds_context"),
    (ErrorClass.CONTEXT, r"compact(?:ion|ing)?\s+(?:failed|error)", "compaction_failed"),
    (ErrorClass.CONTEXT, r"conversation\s+(?:is\s+)?too\s+long", "conversation_too_long"),

    # --- API-Fehler --------------------------------------------------------
    (ErrorClass.API_ERROR, r"\boverloaded(?:_error)?\b", "overloaded"),
    (ErrorClass.API_ERROR, r"\bapi[\s_-]?error\b", "api_error"),
    (ErrorClass.API_ERROR, r"\binternal\s+server\s+error\b", "internal_server_error"),
    (ErrorClass.API_ERROR, r"\bservice\s+unavailable\b", "service_unavailable"),
    (ErrorClass.API_ERROR, r"\b(?:http\s*|status[\s:]*)(?:500|502|503|504|529)\b", "http_5xx"),

    # --- Netzwerk ----------------------------------------------------------
    (ErrorClass.NETWORK, r"\b(?:ECONNRESET|ECONNREFUSED|ENOTFOUND|ETIMEDOUT|EAI_AGAIN|EPIPE)\b", "errno"),
    (ErrorClass.NETWORK, r"\bfetch\s+failed\b", "fetch_failed"),
    (ErrorClass.NETWORK, r"\bgetaddrinfo\b", "getaddrinfo"),
    (ErrorClass.NETWORK, r"\bnetwork\s+(?:error|request\s+failed|unreachable)\b", "network_error"),
    (ErrorClass.NETWORK, r"\bconnection\s+(?:refused|reset|timed?\s*out|closed)\b", "connection"),
    (ErrorClass.NETWORK, r"\bsocket\s+hang\s+up\b", "socket_hangup"),

    # --- Wartet auf Eingabe (KEIN Fehler) ----------------------------------
    # Sentinel der CEO-Flotte: Worker melden Rueckfragen als eigene Zeile
    # "STATUS: CEO-BLOCKIERT: <Frage>". Der Zeilenanfangs-Anker ist Pflicht:
    # das Wort allein steht auch in den Auftrags-Instruktionen jedes Workers
    # und wuerde sonst fertige Tasks als blockiert einstufen (im Probelauf
    # passiert; der CEO musste den Task von Hand pausieren).
    (ErrorClass.AWAITING_INPUT, r"(?m)^\s*STATUS:\s*CEO-BLOCKIERT:", "ceo_blocked"),
    (ErrorClass.AWAITING_INPUT, r"do\s+you\s+want\s+to\s+(?:proceed|continue|allow|make)", "confirm_prompt"),
    (ErrorClass.AWAITING_INPUT, r"permission\s+(?:required|request(?:ed)?|needed)", "permission_required"),
    (ErrorClass.AWAITING_INPUT, r"waiting\s+for\s+(?:your\s+)?(?:input|approval|confirmation|response)", "waiting_input"),
    (ErrorClass.AWAITING_INPUT, r"requires\s+(?:your\s+)?approval", "requires_approval"),
    (ErrorClass.AWAITING_INPUT, r"\[y/n\]|\(y/n\)", "yn_prompt"),

    # --- Absturz -----------------------------------------------------------
    (ErrorClass.CRASH, r"javascript\s+heap\s+out\s+of\s+memory", "heap_oom"),
    (ErrorClass.CRASH, r"\bout\s+of\s+memory\b|\boom[\s-]?kill", "oom"),
    (ErrorClass.CRASH, r"traceback\s+\(most\s+recent\s+call\s+last\)", "python_traceback"),
    (ErrorClass.CRASH, r"segmentation\s+fault|\bsigsegv\b", "segfault"),
    (ErrorClass.CRASH, r"\bfatal\s+error\b", "fatal_error"),
    (ErrorClass.CRASH, r"\buncaught\s+exception\b", "uncaught_exception"),
]

_COMPILED: list[tuple[ErrorClass, re.Pattern[str], str]] = [
    (cls, re.compile(pat, re.IGNORECASE), name) for cls, pat, name in PATTERNS
]

#: Werte von `rate_limit_info.status`, bei denen weitergearbeitet werden darf.
#: Wichtig: "allowed_warning" meldet nur hohe Auslastung (utilization ~0.98,
#: surpassedThreshold 0.9) - die Anfrage geht trotzdem durch. Alles, was mit
#: "allowed" beginnt, wird deshalb generell als erlaubt behandelt; nur
#: eindeutige Ablehnungen ("rejected", "blocked", ...) gelten als Sperre.
RATE_LIMIT_OK_STATUSES = {"", "allowed", "allowed_warning", "ok"}

#: Exit-Codes, die eindeutig auf ein Signal (Absturz/Kill) hindeuten.
SIGNAL_EXIT_CODES = {
    137: "SIGKILL (evtl. OOM-Killer)",
    139: "SIGSEGV",
    143: "SIGTERM",
    124: "Timeout",
}


@dataclass
class Classification:
    error_class: ErrorClass
    detail: str = ""
    #: Absoluter Zeitpunkt, ab dem es wieder losgehen darf (Usage-Limit).
    reset_at: Optional[float] = None
    #: Vom Server vorgegebene Wartezeit in Sekunden (Rate-Limit).
    retry_after: Optional[float] = None
    #: "structured" oder "regex" oder "exit_code" - fuers Log.
    source: str = ""
    evidence: str = ""

    @property
    def is_error(self) -> bool:
        return self.error_class not in (ErrorClass.NONE,)


# --------------------------------------------------------------------------
# Zeit-Parser
# --------------------------------------------------------------------------

_ISO_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:?\d{2})?)\b")
_EPOCH_RE = re.compile(r"\b(1[6-9]\d{8})\b")          # Sekunden, ~2020-2033
_EPOCH_MS_RE = re.compile(r"\b(1[6-9]\d{11})\b")      # Millisekunden
_IN_DURATION_RE = re.compile(
    r"\bin\s+(?:(\d+)\s*h(?:ours?)?)?\s*(?:(\d+)\s*m(?:in(?:utes?)?)?)?\s*(?:(\d+)\s*s(?:ec(?:onds?)?)?)?",
    re.IGNORECASE,
)
#: Uhrzeit hinter "resets". Das "at" ist optional, weil Claude Code es
#: weglaesst — im Transkript von Session 0eeaf952 steht woertlich:
#:
#:     You've hit your session limit · resets 5am (Europe/Berlin)
#:
#: Mit der frueheren Fassung (`resets\s+at\s+…`) blieb die Zeit unerkannt,
#: und backoff.py griff auf USAGE_LIMIT_FALLBACK_WAIT (3600 s) zurueck. Eine
#: Sitzung, die um 22 Uhr ans Limit laeuft und erst um 5 Uhr zurueckgesetzt
#: wird, waere damit sieben Mal stuendlich vergeblich neu gestartet worden —
#: statt einmal bis zum Reset zu warten, was der Code ausdruecklich will
#: ("wird bis zum Reset-Zeitpunkt gewartet und NICHT vorher gepollt").
#:
#: Ohne "at" muss aber eine Minutenangabe oder am/pm dabei sein. Sonst
#: verwandelte "der Zaehler resets 3 mal taeglich" die 3 in eine Uhrzeit.
_AT_CLOCK_RE = re.compile(
    r"\bresets?\s+(?P<at>at\s+)?(?P<hour>\d{1,2})"
    r"(?::(?P<minute>\d{2}))?\s*(?P<meridiem>am|pm)?\b",
    re.IGNORECASE,
)
_RETRY_AFTER_RE = re.compile(r"retry[-\s]?after[\":\s]+(\d+(?:\.\d+)?)", re.IGNORECASE)


def parse_epoch(value: Any) -> Optional[float]:
    """Nimmt Sekunden oder Millisekunden und liefert Sekunden."""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if num <= 0:
        return None
    if num > 1e11:      # Millisekunden
        num /= 1000.0
    if num < 1e9:       # unplausibel klein
        return None
    return num


def parse_reset_at(text: str, now: Optional[float] = None) -> Optional[float]:
    """Extrahiert einen Reset-Zeitpunkt aus freiem Text.

    Unterstuetzt Unix-Epoch, ISO-8601, "in 4h 12m" und "resets at 3pm".
    Gibt einen absoluten Zeitstempel (Sekunden) zurueck oder None.
    """
    if not text:
        return None
    now = now if now is not None else time.time()

    for regex in (_EPOCH_MS_RE, _EPOCH_RE):
        m = regex.search(text)
        if m:
            ts = parse_epoch(m.group(1))
            if ts and ts > now:
                return ts

    m = _ISO_RE.search(text)
    if m:
        raw = m.group(1).replace(" ", "T").replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(raw)
        except ValueError:
            dt = None
        if dt is not None:
            ts = dt.timestamp() if dt.tzinfo else dt.astimezone().timestamp()
            if ts > now:
                return ts

    m = _IN_DURATION_RE.search(text)
    if m and any(m.groups()):
        hours = int(m.group(1) or 0)
        minutes = int(m.group(2) or 0)
        seconds = int(m.group(3) or 0)
        total = hours * 3600 + minutes * 60 + seconds
        if total > 0:
            return now + total

    m = _AT_CLOCK_RE.search(text)
    if m and (m.group("at") or m.group("minute") or m.group("meridiem")):
        hour = int(m.group("hour"))
        minute = int(m.group("minute") or 0)
        meridiem = (m.group("meridiem") or "").lower()
        if meridiem == "pm" and hour < 12:
            hour += 12
        elif meridiem == "am" and hour == 12:
            hour = 0
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            base = datetime.fromtimestamp(now)
            target = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if target.timestamp() <= now:
                target += timedelta(days=1)
            return target.timestamp()

    return None


def parse_retry_after(text: str) -> Optional[float]:
    m = _RETRY_AFTER_RE.search(text or "")
    if not m:
        return None
    try:
        value = float(m.group(1))
    except ValueError:
        return None
    return value if 0 < value <= 86400 else None


# --------------------------------------------------------------------------
# Stufe 1: strukturierte Events
# --------------------------------------------------------------------------

def _walk(obj: Any, key: str) -> list[Any]:
    """Sammelt rekursiv alle Werte zu einem Schluessel."""
    found: list[Any] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == key:
                found.append(v)
            else:
                found.extend(_walk(v, key))
    elif isinstance(obj, list):
        for item in obj:
            found.extend(_walk(item, key))
    return found


def rate_limit_blocks(info: dict[str, Any]) -> Optional[str]:
    """Liefert den Status, wenn er die Arbeit sperrt - sonst None.

    Nur eine echte Ablehnung ist eine Sperre. Warnstatus wie
    "allowed_warning" bedeutet: Kontingent fast aufgebraucht, Anfrage laeuft
    aber durch.
    """
    status = str(info.get("status", "")).strip().lower()
    if status in RATE_LIMIT_OK_STATUSES or status.startswith("allowed"):
        return None
    return status


def _from_rate_limit(ev: dict[str, Any], now: float) -> Optional[Classification]:
    for info in _walk(ev, "rate_limit_info"):
        if not isinstance(info, dict):
            continue
        status = rate_limit_blocks(info)
        if status is None:
            continue
        reset_at = parse_epoch(info.get("resetsAt") or info.get("resets_at"))
        kind = str(info.get("rateLimitType") or info.get("rate_limit_type") or "")
        return Classification(
            error_class=ErrorClass.USAGE_LIMIT,
            detail=f"rate_limit_info.status={status} type={kind or 'unknown'}",
            reset_at=reset_at,
            source="structured",
            evidence=str(info)[:400],
        )
    return None


def _from_result(ev: dict[str, Any], now: float) -> Optional[Classification]:
    """result-Event eines headless Laufs."""
    if ev.get("type") != "result":
        return None
    if not ev.get("is_error") and ev.get("subtype") in (None, "success"):
        return Classification(ErrorClass.NONE, detail="result:success", source="structured")
    subtype = str(ev.get("subtype") or "")
    api_status = ev.get("api_error_status")
    blob = " ".join(str(x) for x in (subtype, api_status, ev.get("result")) if x)
    if api_status:
        code = str(api_status)
        if code.startswith("429"):
            return Classification(ErrorClass.RATE_LIMIT, f"api_error_status={code}",
                                  retry_after=parse_retry_after(blob),
                                  source="structured", evidence=blob[:400])
        return Classification(ErrorClass.API_ERROR, f"api_error_status={code}",
                              source="structured", evidence=blob[:400])
    if "max_turns" in subtype:
        return Classification(ErrorClass.STALLED, f"result.subtype={subtype}",
                              source="structured", evidence=blob[:400])
    if subtype:
        # Rohtext des Fehler-Results noch durch die Mustertabelle schicken.
        refined = classify_text(blob, now=now)
        if refined.error_class is not ErrorClass.UNKNOWN:
            refined.detail = f"result.subtype={subtype}; {refined.detail}"
            return refined
        return Classification(ErrorClass.UNKNOWN, f"result.subtype={subtype}",
                              source="structured", evidence=blob[:400])
    return None


def _from_api_error_flag(ev: dict[str, Any], now: float) -> Optional[Classification]:
    """Von Claude Code markierte API-Fehlermeldung im Transkript."""
    if not ev.get("isApiErrorMessage"):
        return None
    text = str(ev)[:2000]
    refined = classify_text(text, now=now)
    if refined.error_class is not ErrorClass.UNKNOWN:
        refined.source = "structured+regex"
        return refined
    return Classification(ErrorClass.API_ERROR, "isApiErrorMessage",
                          source="structured", evidence=text[:400])


#: Reihenfolge der Auswertung INNERHALB eines Events (spezifisch zuerst).
_EXTRACTORS = (_from_rate_limit, _from_result, _from_api_error_flag)


def classify_structured(events: list[dict[str, Any]],
                        now: Optional[float] = None) -> Optional[Classification]:
    """Wertet die strukturierten Felder aus. None = kein klares Signal.

    Ein einziger Durchlauf von hinten nach vorne: das juengste Event mit einem
    verwertbaren Signal entscheidet. Frueher lief pro Signaltyp ein eigener
    Durchlauf, wodurch ein altes rate_limit_event ein neueres
    `result: success` ueberstimmen konnte.
    """
    now = now if now is not None else time.time()
    for ev in reversed(events):
        for extract in _EXTRACTORS:
            verdict = extract(ev, now)
            if verdict is not None:
                return verdict
    return None


# --------------------------------------------------------------------------
# Stufe 2: Regex-Fallback
# --------------------------------------------------------------------------

def classify_text(text: str, now: Optional[float] = None) -> Classification:
    """Wendet die Mustertabelle auf freien Text an."""
    if not text:
        return Classification(ErrorClass.UNKNOWN, "no text", source="regex")
    now = now if now is not None else time.time()
    for error_class, regex, name in _COMPILED:
        m = regex.search(text)
        if not m:
            continue
        start = max(0, m.start() - 120)
        evidence = text[start:m.end() + 200].strip()
        result = Classification(
            error_class=error_class,
            detail=name,
            source="regex",
            evidence=evidence[:400],
        )
        if error_class is ErrorClass.USAGE_LIMIT:
            result.reset_at = parse_reset_at(evidence, now=now) or parse_reset_at(text, now=now)
        elif error_class is ErrorClass.RATE_LIMIT:
            result.retry_after = parse_retry_after(text)
        return result
    return Classification(ErrorClass.UNKNOWN, "no pattern matched", source="regex")


def classify_exit_code(exit_code: Optional[int]) -> Optional[Classification]:
    if exit_code is None or exit_code == 0:
        return None
    label = SIGNAL_EXIT_CODES.get(exit_code)
    if label:
        return Classification(ErrorClass.CRASH, f"exit={exit_code} ({label})",
                              source="exit_code")
    return Classification(ErrorClass.CRASH, f"exit={exit_code}", source="exit_code")


# --------------------------------------------------------------------------
# Einstiegspunkt
# --------------------------------------------------------------------------

def classify(events: Optional[list[dict[str, Any]]] = None,
             text: str = "",
             exit_code: Optional[int] = None,
             stalled: bool = False,
             now: Optional[float] = None) -> Classification:
    """Gesamtklassifikation: strukturiert vor Regex vor Exit-Code."""
    events = events or []
    now = now if now is not None else time.time()

    structured = classify_structured(events, now=now)
    if structured is not None and structured.error_class is not ErrorClass.UNKNOWN:
        # Auch ein NONE ist eine Aussage: das juengste strukturierte Signal war
        # ein erfolgreiches result. Ein Regex-Treffer aus aelterem Tail-Text
        # (z.B. ein laengst ueberholter Limit-Hinweis) darf das nicht kippen.
        return structured

    by_text = classify_text(text, now=now)
    if by_text.error_class not in (ErrorClass.UNKNOWN,):
        return by_text

    by_exit = classify_exit_code(exit_code)
    if by_exit is not None:
        return by_exit

    if structured is not None:
        # UNKNOWN, aber mit Detail (z.B. result.subtype=...) - besser als nichts.
        return structured

    if stalled:
        return Classification(ErrorClass.STALLED, "kein Fortschritt", source="timing")

    return Classification(ErrorClass.UNKNOWN, "keine Evidenz", source="none")
