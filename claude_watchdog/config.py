"""Pfade, Defaults und Laufzeit-Konfiguration.

Alle Werte lassen sich per Umgebungsvariable (Prefix CW_) ueberschreiben,
damit sich der Daemon ohne Code-Aenderung tunen laesst.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

# --------------------------------------------------------------------------
# Pfade
# --------------------------------------------------------------------------

BASE_DIR = Path(os.environ.get("CW_BASE_DIR", Path.home() / ".claude-watchdog"))
STATE_DB = BASE_DIR / "state.db"
LOG_FILE = BASE_DIR / "watchdog.log"
STOP_FILE = BASE_DIR / "STOP"
DAEMON_LOCK = BASE_DIR / "daemon.lock"
RUNS_DIR = BASE_DIR / "runs"

CLAUDE_PROJECTS_DIR = Path(
    os.environ.get("CW_PROJECTS_DIR", Path.home() / ".claude" / "projects")
)
CLAUDE_BIN = os.environ.get("CW_CLAUDE_BIN", str(Path.home() / ".local" / "bin" / "claude"))

#: Wie ein managed-Lauf gestartet wird.
#:
#: "scope"   - `systemd-run --user --scope`, also ein Kindprozess des Daemons.
#:             Vorgabe, weil es seit dem ersten Tag so laeuft.
#: "service" - transienter Dienst; gestartet wird er dann vom User-Manager und
#:             nicht vom Daemon. Das ist der einzige Weg, auf dem ein Lauf einer
#:             Netzsperre des Daemons (PrivateNetwork=yes) entkommt: ein Scope
#:             erbt die Netzwerk-Namespace des Aufrufers, ein Dienst nicht.
#:             Gemessen am 2026-08-17, siehe SECURITY.md.
RUN_LAUNCHER = os.environ.get("CW_RUN_LAUNCHER", "scope").strip().lower()
if RUN_LAUNCHER not in ("scope", "service"):
    RUN_LAUNCHER = "scope"


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


# --------------------------------------------------------------------------
# Timing
# --------------------------------------------------------------------------

#: Abstand zwischen zwei Durchlaeufen der Hauptschleife.
POLL_INTERVAL = _env_int("CW_POLL_INTERVAL", 15)

#: Kein Fortschritt laenger als das -> Task gilt als haengend (STALLED).
STALL_SECONDS = _env_int("CW_STALL_SECONDS", 900)

#: Wie lange eine observed-Session verschwunden sein muss (Prozess weg, dem
#: CLI unbekannt, kein Fortschritt), bevor die Beobachtung als beendet gilt.
OBSERVED_GONE_SECONDS = _env_int("CW_OBSERVED_GONE_SECONDS", 120)

#: TTL fuer den Cache von `claude agents --json` (der Aufruf startet einen
#: Node-Prozess, deshalb nicht bei jedem Tick).
AGENTS_CACHE_TTL = _env_int("CW_AGENTS_CACHE_TTL", 30)

#: Schonfrist nach einem `reply`: solange fasst der Daemon den Task nicht an,
#: damit der alte Transkript-Tail nicht erneut als AWAITING_INPUT klassifiziert
#: wird, bevor der Antwort-Lauf erste Ausgaben schreibt.
REPLY_GRACE = _env_int("CW_REPLY_GRACE", 180)

#: Timeout fuer den `claude agents --json` Aufruf.
AGENTS_TIMEOUT = _env_int("CW_AGENTS_TIMEOUT", 20)

# --------------------------------------------------------------------------
# Backoff
# --------------------------------------------------------------------------

BACKOFF_BASE = _env_float("CW_BACKOFF_BASE", 30.0)
BACKOFF_FACTOR = _env_float("CW_BACKOFF_FACTOR", 2.0)
BACKOFF_CAP = _env_float("CW_BACKOFF_CAP", 1800.0)
BACKOFF_JITTER = _env_float("CW_BACKOFF_JITTER", 0.2)

#: Fallback-Wartezeit, wenn ein Usage-Limit ohne verwertbare Reset-Zeit kommt.
USAGE_LIMIT_FALLBACK_WAIT = _env_int("CW_USAGE_LIMIT_FALLBACK_WAIT", 3600)

#: Sicherheitspuffer nach dem Reset-Zeitpunkt, damit nicht auf die Sekunde
#: genau (und damit womoeglich zu frueh) angeklopft wird.
USAGE_LIMIT_RESET_PADDING = _env_int("CW_USAGE_LIMIT_RESET_PADDING", 30)

# --------------------------------------------------------------------------
# Sicherheitsgrenzen
# --------------------------------------------------------------------------

DEFAULT_MAX_ATTEMPTS = _env_int("CW_MAX_ATTEMPTS", 5)

#: Globales Budget: maximale Anzahl Neustarts ueber alle Tasks pro Stunde.
MAX_RESTARTS_PER_HOUR = _env_int("CW_MAX_RESTARTS_PER_HOUR", 20)

#: Wie oft darf ein Resume an derselben Stelle scheitern, bevor der Task
#: als failed gilt (Anti-Schleifen-Regel).
MAX_SAME_MARKER_RETRIES = _env_int("CW_MAX_SAME_MARKER_RETRIES", 3)

#: Wieviel Text vom Ende einer Logdatei fuer die Klassifikation gelesen wird.
TAIL_BYTES = _env_int("CW_TAIL_BYTES", 65536)

# --------------------------------------------------------------------------
# Datenschutz
# --------------------------------------------------------------------------

#: Darf der Neuanfang nach einem Kontextlimit (RESTART_FRESH) den woertlichen
#: Auszug aus Transkript und Run-Log in den neuen Prompt schreiben? Das ist die
#: einzige Stelle, an der lokal eingesammelter Sitzungsinhalt an die API geht -
#: samt allem, was zufaellig im Tail steht (Pfade, Ausgaben, Schluessel).
#: 0 setzt dort einen neutralen Hinweis ein; der urspruengliche Auftrag bleibt
#: in beiden Faellen erhalten.
FRESH_DIGEST = os.environ.get("CW_FRESH_DIGEST", "1") not in ("0", "false", "no")

# --------------------------------------------------------------------------
# Aufraeumen
# --------------------------------------------------------------------------

#: Schonfrist fuer abgeschlossene und gescheiterte Tasks in Tagen. Durch
#: Auto-Attach kommt pro Session ein Eintrag dazu - ohne Verfallsdatum waechst
#: die Liste unbegrenzt. 0 schaltet das Aufraeumen ab.
RETENTION_DAYS = _env_int("CW_RETENTION_DAYS", 14)

#: Abstand zwischen zwei Aufraeum-Laeufen (der erste laeuft beim Start).
CLEANUP_INTERVAL = _env_int("CW_CLEANUP_INTERVAL", 3600)

# --------------------------------------------------------------------------
# Logging / Notification
# --------------------------------------------------------------------------

LOG_MAX_BYTES = _env_int("CW_LOG_MAX_BYTES", 5 * 1024 * 1024)
LOG_BACKUP_COUNT = _env_int("CW_LOG_BACKUP_COUNT", 5)

#: Wie lange eine unveraenderte Entscheidung ohne Eingriff still bleibt, bevor
#: sie erneut als INFO auftaucht. Bei POLL_INTERVAL=15 schreibt sonst jeder
#: Task rund 240 gleichlautende Zeilen pro Stunde und verdraengt damit die
#: Ereignisse, die wirklich zaehlen. 0 schaltet die Unterdrueckung ab.
LOG_REPEAT_INTERVAL = _env_int("CW_LOG_REPEAT_INTERVAL", 1800)

NOTIFY_ENABLED = os.environ.get("CW_NOTIFY", "1") not in ("0", "false", "no")
NOTIFY_BIN = os.environ.get("CW_NOTIFY_BIN", "notify-send")


# --------------------------------------------------------------------------
# Auto-Attach
# --------------------------------------------------------------------------

#: Laufende Sessions von selbst als observed aufnehmen. Beobachtet wird nur -
#: der Mode-Guard verbietet jeden Eingriff in fremde Sessions.
AUTO_ATTACH = os.environ.get("CW_AUTO_ATTACH", "1") not in ("0", "false", "no")

#: Sessions ueberspringen, die schon unter einer eigenen systemd-Unit laufen
#: (siehe SUPERVISED_UNIT_PREFIX). Dort sorgt bereits systemd fuer den
#: Neustart; ein zweiter Beobachter erzeugt nur Karteileichen und Meldungen
#: ueber Neustarts, die planmaessig waren.
SKIP_SUPERVISED = os.environ.get("CW_SKIP_SUPERVISED", "1") not in ("0", "false", "no")

#: Unit-Namen mit diesem Praefix gelten als fremdbeaufsichtigt.
SUPERVISED_UNIT_PREFIX = os.environ.get("CW_SUPERVISED_UNIT_PREFIX", "claude-session@")

#: Optionale Einschraenkung: nur Sessions unterhalb dieser Verzeichnisse
#: aufnehmen (Komma-getrennt). Leer bedeutet: ueberall.
AUTO_ATTACH_DIRS = [
    entry.strip()
    for entry in os.environ.get("CW_AUTO_ATTACH_DIRS", "").split(",")
    if entry.strip()
]


def auto_attach_allows(cwd: Optional[str]) -> bool:
    """Darf eine Session in diesem Arbeitsverzeichnis aufgenommen werden?

    Ohne konfigurierte Verzeichnisse ist alles erlaubt; ein unbekanntes
    Arbeitsverzeichnis nie (ohne cwd laesst sich das Transkript nicht finden).
    """
    if not cwd:
        return False
    if not AUTO_ATTACH_DIRS:
        return True
    try:
        target = Path(cwd).expanduser().resolve()
    except OSError:
        return False
    for base in AUTO_ATTACH_DIRS:
        try:
            target.relative_to(Path(base).expanduser().resolve())
            return True
        except (ValueError, OSError):
            continue
    return False


def ensure_dirs() -> None:
    """Legt die Verzeichnisstruktur an (idempotent)."""
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    RUNS_DIR.mkdir(parents=True, exist_ok=True)


def stop_requested() -> bool:
    """Kill-Switch: Existiert die STOP-Datei, wird nichts mehr gestartet."""
    return STOP_FILE.exists()


def escaped_cwd(cwd: str) -> str:
    """Wandelt einen Pfad in die Projektverzeichnis-Schreibweise von Claude Code.

    /home/user/Desktop -> -home-user-Desktop
    """
    return str(Path(cwd).resolve()).replace("/", "-")


def project_dir(cwd: str) -> Path:
    """Verzeichnis, in dem die Transkripte fuer dieses cwd liegen."""
    return CLAUDE_PROJECTS_DIR / escaped_cwd(cwd)


def transcript_path(cwd: str, session_id: str) -> Path:
    """Pfad zum Transkript einer Session."""
    return project_dir(cwd) / f"{session_id}.jsonl"


# --------------------------------------------------------------------------
# Run-Logs eines managed Laufs
# --------------------------------------------------------------------------

def run_dir(task_id: str) -> Path:
    return RUNS_DIR / task_id


def run_log(task_id: str, attempt: int) -> Path:
    return run_dir(task_id) / f"attempt-{attempt:03d}.jsonl"


def run_err(task_id: str, attempt: int) -> Path:
    return run_dir(task_id) / f"attempt-{attempt:03d}.err"


def run_rc(task_id: str, attempt: int) -> Path:
    """Datei mit dem Rueckgabewert eines Laufs.

    Nur der Dienst-Startweg braucht sie: dort ist der Lauf kein Kindprozess,
    `Popen.poll()` gibt es also nicht. Der Wrapper schreibt `$?` hier hinein.
    """
    return run_dir(task_id) / f"attempt-{attempt:03d}.rc"


def latest_run_files(task_id: str) -> tuple[Path | None, Path | None]:
    """Neuestes Run-Log-Paar (jsonl, err) eines Tasks."""
    directory = run_dir(task_id)
    if not directory.is_dir():
        return None, None
    logs = sorted(directory.glob("attempt-*.jsonl"))
    if not logs:
        return None, None
    newest = logs[-1]
    err = newest.with_suffix(".err")
    return newest, (err if err.exists() else None)
