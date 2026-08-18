"""Entscheidung und Ausfuehrung.

Zwei klar getrennte Schritte:
  decide()  - rein funktional, trifft die Entscheidung, aendert nichts
  execute() - fuehrt aus, respektiert dry-run

Der Mode-Guard (observed greift nie ein) sitzt an genau einer Stelle:
`enforce_mode_guard`. Beide Schritte laufen durch ihn.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Optional

from . import backoff, config
from .classifier import Classification, classify
from .logging_setup import get
from .models import (
    Action,
    Decision,
    ErrorClass,
    INTRUSIVE_ACTIONS,
    Mode,
    Observation,
    Status,
    Task,
)
from .notifier import URGENCY_CRITICAL, URGENCY_LOW, URGENCY_NORMAL, Notifier
from .registry import Registry

log = get("recovery")


#: Speichergrenzen fuer gestartete Laeufe — dieselben Werte wie in der
#: Uebersicht (claude_sessions/actions.py), in claude-session-open und in
#: claude-session@.service. Am 2026-07-31 wuchsen zwei interaktive
#: Claude-Fenster auf 27,8 bzw. 28,6 GB und rissen per globalem OOM den
#: ollama-Dienst mit.
#:
#: MemoryHigh stand zuerst auf 4G — begruendet mit den ueberwachten
#: Sitzungen (hoechstens 1,1 GB), die aber meist untaetig warten. An den
#: tatsaechlich benutzten Fenstern nachgemessen am 2026-07-31 um 19:55:
#: 373 MB, 740 MB, 985 MB und 3502 MB. Die letzte lief seit 5,5 Stunden
#: voellig gesund und waere binnen einer Stunde in die Bremse gelaufen.
SCOPE_LIMITS = ("--property=MemoryHigh=8G",
                "--property=MemoryMax=12G",
                "--property=MemorySwapMax=2G")


def mit_speichergrenze(cmd: list[str], task_id: str) -> list[str]:
    """`cmd` in einen systemd-Scope mit Speichergrenze einpacken.

    Gestartete Laeufe hingen bisher ohne jede Grenze in der cgroup des
    Daemons. Ein durchgehender Lauf haette also ausgerechnet den Supervisor
    mitgerissen, der ihn wieder herstellen soll.

    Bewusst `--scope` und nicht `--unit`: systemd-run exec't den Befehl
    dabei, die pid bleibt also die des Programms selbst (nachgemessen:
    cmdline und /proc/<pid>/exe zeigen auf den Befehl, nicht auf
    systemd-run). Damit funktionieren `proc.poll()` zum Einsammeln des
    Exit-Codes, `pid_is_claude` und die gespeicherte pid unveraendert
    weiter. Der Exit-Code wird durchgereicht (nachgemessen: 42 bleibt 42).

    Nebenwirkung, die uns entgegenkommt: der Scope ist eine eigene Unit und
    haengt nicht mehr in der cgroup von claude-watchdog.service. Dass die
    Laeufe einen Daemon-Neustart ueberleben, haengt damit nicht laenger
    allein an `KillMode=main`.

    Fehlt `systemd-run`, bleibt es beim nackten Befehl — lieber ohne Grenze
    starten als gar nicht.
    """
    if not shutil.which("systemd-run"):
        log.debug("systemd-run nicht vorhanden, starte ohne Speichergrenze",
                  extra={"task": task_id})
        return cmd
    return ["systemd-run", "--user", "--scope", "--collect", "--quiet",
            *SCOPE_LIMITS,
            "--description=Claude-Watchdog: %s" % task_id,
            "--", *cmd]


# --------------------------------------------------------------------------
# Startweg "service": der Lauf gehoert dem User-Manager, nicht dem Daemon
# --------------------------------------------------------------------------

#: Wrapper um den eigentlichen Lauf. Er uebernimmt zwei Dinge, die beim
#: Scope-Startweg der Daemon selbst erledigt und hier nicht mehr kann:
#:   1. die Ausgabe in die Protokolldateien lenken,
#:   2. den Rueckgabewert festhalten - `Popen.poll()` gibt es hier nicht.
#: `$?` ist nach einem Signal 128+n, also genau die Werte, die der Classifier
#: in SIGNAL_EXIT_CODES erwartet (137 = SIGKILL, 143 = SIGTERM).
#:
#: Die eigene PID legt der Wrapper bewusst NICHT ab: `$$` waere die
#: naheliegende Form, aber systemd liest die Kommandozeile selbst und macht
#: aus `$$` ein einzelnes `$` (das ist dort die Escape-Form). Nachgestellt am
#: 2026-08-17 - in der PID-Datei stand danach woertlich "$". Die PID kommt
#: deshalb vom Manager selbst, siehe warte_auf_mainpid().
DIENST_WRAPPER = (
    'rc="$1"; out="$2"; err="$3"; shift 3; '
    'exec >>"$out" 2>>"$err"; '
    '"$@"; '
    'printf %s "$?" > "$rc"'
)

#: Umgebungsvariablen, die systemd fuer jede Unit selbst setzt. Wer sie
#: weiterreicht, luegt dem neuen Dienst etwas ueber seine Herkunft vor.
NICHT_WEITERREICHEN = frozenset({
    "INVOCATION_ID", "JOURNAL_STREAM", "LISTEN_FDNAMES", "LISTEN_FDS",
    "LISTEN_PID", "MAINPID", "MANAGERPID", "MEMORY_PRESSURE_WATCH",
    "MEMORY_PRESSURE_WRITE", "NOTIFY_SOCKET", "SYSTEMD_EXEC_PID",
    "WATCHDOG_PID", "WATCHDOG_USEC",
})

#: So lange wird auf die PID-Datei gewartet. Der Manager startet den Dienst
#: asynchron; ohne PID hielte der naechste Durchlauf den Lauf fuer tot.
PID_WARTE_SEKUNDEN = 5.0
PID_TAKT = 0.05


def _prozess_lebt(pid: Optional[int]) -> bool:
    """Wie detector.pid_alive, aber ohne Import - recovery kennt detector nicht."""
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def dienst_umgebung(env: dict) -> list[str]:
    """--setenv-Argumente aus einer Umgebung bauen.

    Der Dienst erbt die Umgebung des Daemons nicht (er ist kein Kind mehr),
    deshalb wird sie ausdruecklich weitergereicht - dieselbe, die der
    Scope-Startweg geerbt haette, ohne die systemd-eigenen Variablen.
    """
    argumente = []
    for schluessel, wert in sorted(env.items()):
        if schluessel in NICHT_WEITERREICHEN or "\n" in wert:
            continue
        argumente.append("--setenv=%s=%s" % (schluessel, wert))
    return argumente


def dienst_kommando(cmd: list[str], task_id: str, unit: str, cwd: str,
                    out_pfad: Path, err_pfad: Path, rc_pfad: Path,
                    env: dict) -> list[str]:
    """Kommandozeile, die `cmd` als transienten Dienst startet."""
    return [
        "systemd-run", "--user", "--collect", "--quiet",
        "--unit", unit,
        "--description=Claude-Watchdog: %s" % task_id,
        "--property=WorkingDirectory=%s" % cwd,
        *SCOPE_LIMITS,
        *dienst_umgebung(env),
        "--", "/bin/sh", "-c", DIENST_WRAPPER, "_",
        str(rc_pfad), str(out_pfad), str(err_pfad),
        *cmd,
    ]


def mainpid(unit: str) -> Optional[int]:
    """MainPID einer Unit, sonst None. Fragt den Manager, nicht das Dateisystem."""
    try:
        fertig = subprocess.run(
            ["systemctl", "--user", "show", unit, "-p", "MainPID", "--value"],
            capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    roh = fertig.stdout.strip()
    if roh.isdigit() and int(roh) > 0:
        return int(roh)
    return None


def warte_auf_mainpid(unit: str, grenze: Optional[float] = None,
                      schlaf=time.sleep) -> Optional[int]:
    """Auf die PID des gestarteten Dienstes warten. None, wenn sie ausbleibt.

    Der Manager startet asynchron: direkt nach `systemd-run` steht MainPID
    noch auf 0. `grenze` wird erst beim Aufruf aus PID_WARTE_SEKUNDEN geholt
    und nicht als Vorgabewert gebunden - sonst liesse sich die Wartezeit im
    Test nicht heruntersetzen und die Testsuite haende hier im Leerlauf.
    """
    if grenze is None:
        grenze = PID_WARTE_SEKUNDEN
    ende = time.monotonic() + grenze
    while True:
        pid = mainpid(unit)
        if pid is not None:
            return pid
        if time.monotonic() >= ende:
            return None
        schlaf(PID_TAKT)


class DienstLauf:
    """Ein Lauf als transienter Dienst - schmale Popen-Schnittstelle.

    Der Daemon braucht von einem laufenden Lauf nur zweierlei: `pid` und
    `poll()`. Beides beantwortet diese Klasse aus Dateien statt aus einem
    Kindprozess-Handle, denn ein Dienst ist kein Kind des Daemons - genau
    darin liegt sein Zweck: er entkommt dessen Netzsperre.
    """

    def __init__(self, unit: str, pid: Optional[int], rc_pfad: Path) -> None:
        self.unit = unit
        self.pid = pid
        self.rc_pfad = rc_pfad
        self._code: Optional[int] = None

    def poll(self) -> Optional[int]:
        """Rueckgabewert, sobald er feststeht - sonst None."""
        if self._code is not None:
            return self._code
        roh = ""
        try:
            roh = self.rc_pfad.read_text(encoding="utf-8").strip()
        except OSError:
            pass
        if roh:
            try:
                self._code = int(roh)
                return self._code
            except ValueError:
                log.warning("unlesbarer Rueckgabewert",
                            extra={"unit": self.unit, "wert": roh[:40]})
                self._code = 0
                return self._code
        if _prozess_lebt(self.pid):
            return None
        # Kein Rueckgabewert, aber der Prozess ist weg: der Wrapper kam nicht
        # mehr zum Schreiben, die cgroup wurde also abgeraeumt (systemctl stop,
        # OOM-Killer, Abmeldung). 137 sagt genau das, und der Classifier kennt
        # den Wert bereits als SIGKILL.
        log.info("Lauf ohne Rueckgabewert beendet",
                 extra={"unit": self.unit, "pid": self.pid})
        self._code = 137
        return self._code


# --------------------------------------------------------------------------
# Guard
# --------------------------------------------------------------------------

def enforce_mode_guard(task: Task, decision: Decision) -> Decision:
    """observed-Tasks werden niemals angefasst - nur gemeldet.

    Das ist die einzige Stelle, an der diese Regel durchgesetzt wird; sie
    laesst sich damit nicht versehentlich woanders umgehen.
    """
    if task.mode is not Mode.OBSERVED:
        return decision
    if decision.action not in INTRUSIVE_ACTIONS:
        return decision
    return Decision(
        action=Action.NOTIFY,
        reason=f"observed-Task: '{decision.action.value}' unterdrueckt ({decision.reason})",
        error_class=decision.error_class,
        notify=decision.notify or (
            f"Session braucht Aufmerksamkeit ({decision.error_class.value}). "
            f"Der Watchdog greift bei observed-Tasks nicht ein."
        ),
        counts_as_attempt=False,
    )


# --------------------------------------------------------------------------
# Entscheidung
# --------------------------------------------------------------------------

def _finished_successfully(classification: Classification, obs: Observation) -> bool:
    if classification.error_class is not ErrorClass.NONE:
        return False
    return classification.detail == "result:success"


def launch_postponed_until(decision: Decision, now: float) -> Optional[float]:
    """Termin, bis zu dem ein Start warten muss - None heisst: sofort.

    `decide()` berechnet fuer RESUME/RESTART_FRESH ein Backoff. Ohne diese
    Auswertung wuerde jeder POLL_INTERVAL erneut gestartet und das Backoff
    waere wirkungslos.
    """
    due = decision.effective_retry_at(now)
    return due if due and due > now else None


#: Zusatz, den `decide()` an 'laeuft' haengt, wenn das Transkript seit dem
#: letzten Takt gewachsen ist.
_FORTSCHRITT = " (Fortschritt)"


def _ohne_fortschritt(grund: str) -> str:
    """Grund fuer die Wiederholungserkennung vereinheitlichen.

    Eine arbeitende Session wechselt von Takt zu Takt zwischen 'laeuft' und
    'laeuft (Fortschritt)' — je nachdem, ob das Transkript gerade gewachsen
    ist. Fuer die Frage "hat sich die Lage geaendert?" ist das derselbe
    Zustand. Ohne diese Vereinheitlichung gilt jede Runde als Aenderung, und
    die Unterdrueckung greift ausgerechnet dort nicht, wo am meisten los ist:
    gemessen 183 Zeilen in einer Stunde, davon 170 dieses Hin und Her.

    Die Logzeile selbst zeigt weiterhin den echten Grund; vereinheitlicht
    wird nur der Fingerabdruck.
    """
    return grund[:-len(_FORTSCHRITT)] if grund.endswith(_FORTSCHRITT) else grund


def _observed_session_gone(task: Task, obs: Observation,
                           now: Optional[float] = None) -> bool:
    """Ist eine beobachtete Session endgueltig weg?

    Eine observed-Session endet, wenn der Benutzer sie schliesst - der
    Watchdog darf sie nicht wiederbeleben (Mode-Guard), soll sie aber auch
    nicht ewig weiterbeobachten und stuendlich melden.

    Drei Bedingungen gemeinsam, damit ein kurzer Aussetzer nicht reicht:
      * kein Prozess mehr und dem CLI unbekannt (obs.alive deckt beides ab)
      * `claude agents --json` war erreichbar - sonst ist die Aussage
        "kennt die Session nicht" wertlos
      * seit OBSERVED_GONE_SECONDS kein neuer Eintrag im Transkript
    """
    if task.mode is not Mode.OBSERVED:
        return False
    if obs.alive or obs.progressed:
        return False
    if not obs.cli_usable:
        return False
    return obs.idle_seconds >= config.OBSERVED_GONE_SECONDS


def decide(task: Task,
           obs: Observation,
           classification: Optional[Classification] = None,
           stalled: bool = False,
           now: Optional[float] = None,
           budget_available: bool = True) -> Decision:
    now = now if now is not None else time.time()
    classification = classification or classify(
        obs.events, obs.tail_text, obs.exit_code, stalled=stalled, now=now
    )
    cls = classification.error_class

    # 1) Erfolgreich beendet?
    if _finished_successfully(classification, obs):
        return enforce_mode_guard(task, Decision(
            action=Action.COMPLETE,
            reason="result:success",
            notify=f"Task '{task.title}' fertig.",
        ))

    # 2) Beobachtete Session ist geschlossen worden -> Beobachtung beenden.
    #    Muss vor allen Fehlerzweigen stehen: eine Session, die es nicht mehr
    #    gibt, wartet auch nicht auf Eingabe und braucht keinen Wiederanlauf.
    if _observed_session_gone(task, obs, now):
        letzter = (f" (zuletzt: {cls.value})"
                   if cls not in (ErrorClass.NONE, ErrorClass.UNKNOWN) else "")
        return Decision(
            action=Action.COMPLETE,
            reason=f"observed: Session beendet{letzter}",
            notify=f"Session '{task.title}' ist beendet - Beobachtung "
                   f"abgeschlossen{letzter}.",
        )

    # 3) Wartet auf Eingabe - kein Fehler, niemals automatisch beantworten.
    if cls is ErrorClass.AWAITING_INPUT:
        return enforce_mode_guard(task, Decision(
            action=Action.NOTIFY,
            reason="wartet auf Benutzereingabe",
            error_class=cls,
            notify=f"Task '{task.title}' wartet auf deine Eingabe "
                   f"(Session {task.session_id or '?'}).",
        ))

    # 4) Untaetigkeit ist bei einer interaktiven Session kein Ereignis: der
    #    Benutzer liest, denkt nach oder ist kurz weg. Die STALLED-Regel
    #    stammt aus der Zeit, als es nur selbst gestartete Tasks gab; seit
    #    dem Auto-Attach traefe sie jede offene Session und wuerde alle
    #    STALL_SECONDS eine Meldung erzeugen. Gemeldet wird bei observed nur,
    #    was den Benutzer wirklich betrifft: Rueckfragen und echte Fehler.
    if task.mode is Mode.OBSERVED and cls is ErrorClass.STALLED:
        return Decision(action=Action.NONE,
                        reason="observed: untaetig, kein Fehler",
                        error_class=ErrorClass.NONE)

    # 5) Sichtbarer Fortschritt schlaegt alte Evidenz im Transkript-Tail.
    #    Ohne diese Regel koennte ein laengst ueberholtes Fehler-Event nach
    #    einem erfolgreichen Resume erneut zuschlagen.
    if obs.alive and obs.progressed:
        return Decision(action=Action.NONE, reason="laeuft (Fortschritt)",
                        error_class=ErrorClass.NONE)

    if obs.alive and not stalled and cls in (ErrorClass.NONE, ErrorClass.UNKNOWN):
        return Decision(action=Action.NONE, reason="laeuft", error_class=ErrorClass.NONE)

    # 6) Geplanter Wiederanlauf ist faellig (z.B. Usage-Limit vorbei).
    #    Hier wird bewusst NICHT neu aus der alten Evidenz abgeleitet - sonst
    #    wuerde dieselbe abgelaufene Reset-Zeit eine weitere Wartezeit setzen.
    retry_due = (task.next_retry_at is not None and now >= task.next_retry_at
                 and task.status is Status.WAITING_FOR_LIMIT and not obs.alive)
    if retry_due and task.mode is Mode.MANAGED:
        if not budget_available:
            return Decision(action=Action.SCHEDULE, reason="Restart-Budget erschoepft",
                            error_class=cls, delay=300.0)
        # Bis der Termin faellig ist, ist die Evidenz von damals oft weg: der
        # Exit-Code wurde einmalig eingesammelt, das Log ist ueberholt. Dann
        # ist die gespeicherte Klasse ehrlicher als ein frisches UNKNOWN - und
        # nur so greift unten die Kontext-Sonderregel noch.
        if cls is ErrorClass.UNKNOWN and task.last_error_class:
            try:
                cls = ErrorClass(task.last_error_class)
            except ValueError:
                pass

        if not task.session_id:
            action = Action.START
        elif cls is ErrorClass.CONTEXT:
            # Der Kontext ist nach der Wartezeit immer noch voll - genau wie
            # unten in 7) nicht stumpf resumen, sondern verdichtet neu ansetzen.
            action = Action.RESTART_FRESH
        else:
            action = Action.RESUME
        return enforce_mode_guard(task, Decision(
            action=action,
            reason=f"geplanter Wiederanlauf nach {cls.value}",
            error_class=cls,
            counts_as_attempt=backoff.counts_as_attempt(classification),
        ))

    # 7) Erstmaliger Start eines managed Tasks.
    if task.status is Status.PENDING and task.mode is Mode.MANAGED:
        if not budget_available:
            return Decision(action=Action.SCHEDULE, reason="Restart-Budget erschoepft",
                            delay=300.0)
        return enforce_mode_guard(task, Decision(
            action=Action.START, reason="Erststart", counts_as_attempt=True,
        ))

    # 8) Ab hier: es gibt eine Unterbrechung.
    if task.no_auto_resume:
        return enforce_mode_guard(task, Decision(
            action=Action.NOTIFY,
            reason="no_auto_resume gesetzt",
            error_class=cls,
            notify=f"Task '{task.title}' unterbrochen ({cls.value}). "
                   f"Auto-Resume ist fuer diesen Task deaktiviert.",
        ))

    if not backoff.is_retryable(classification):
        return enforce_mode_guard(task, Decision(
            action=Action.NOTIFY, reason=f"nicht wiederholbar: {cls.value}",
            error_class=cls,
            notify=f"Task '{task.title}': {cls.value} - {classification.detail}",
        ))

    # Anti-Schleife: dreimal an derselben Stelle gescheitert -> aufgeben.
    if task.same_marker_count >= config.MAX_SAME_MARKER_RETRIES:
        return enforce_mode_guard(task, Decision(
            action=Action.FAIL,
            reason=f"Resume scheitert wiederholt an derselben Stelle "
                   f"({task.same_marker_count}x)",
            error_class=cls,
            notify=f"Task '{task.title}' aufgegeben: Resume kommt an derselben "
                   f"Stelle nicht weiter.",
        ))

    # Harter Retry-Deckel.
    if backoff.counts_as_attempt(classification) and task.attempts >= task.max_attempts:
        return enforce_mode_guard(task, Decision(
            action=Action.FAIL,
            reason=f"max_attempts erreicht ({task.attempts}/{task.max_attempts})",
            error_class=cls,
            notify=f"Task '{task.title}' aufgegeben nach {task.attempts} Versuchen "
                   f"(zuletzt: {cls.value}).",
        ))

    delay, retry_at = backoff.delay_for(classification, task.attempts, now=now)

    # Globales Budget: lieber warten als das Kontingent verheizen.
    if not budget_available:
        return Decision(action=Action.SCHEDULE, reason="Restart-Budget erschoepft",
                        error_class=cls, delay=max(delay, 300.0))

    if cls is ErrorClass.USAGE_LIMIT:
        when = time.strftime("%H:%M", time.localtime(retry_at)) if retry_at else "?"
        return enforce_mode_guard(task, Decision(
            action=Action.SCHEDULE,
            reason=f"Usage-Limit, warte bis {when}",
            error_class=cls, delay=delay, retry_at=retry_at,
            notify=f"Usage-Limit erreicht. Task '{task.title}' wird um {when} "
                   f"automatisch fortgesetzt.",
            counts_as_attempt=False,
        ))

    # Kontext voll: nicht stumpf resumen, sondern verdichtet neu ansetzen.
    action = Action.RESTART_FRESH if cls is ErrorClass.CONTEXT else Action.RESUME
    return enforce_mode_guard(task, Decision(
        action=action,
        reason=f"{cls.value}: {classification.detail}",
        error_class=cls,
        delay=delay, retry_at=retry_at,
        counts_as_attempt=backoff.counts_as_attempt(classification),
    ))


# --------------------------------------------------------------------------
# Ausfuehrung
# --------------------------------------------------------------------------

CONTINUE_TEMPLATE = (
    "Diese Session wurde unterbrochen ({reason}). Setze die Arbeit dort fort, "
    "wo du aufgehoert hast - fange NICHT von vorne an. "
    "Der urspruengliche Auftrag war:\n\n{prompt}\n\n"
    "Pruefe zuerst kurz, was bereits erledigt ist, und mache dann weiter."
)

FRESH_TEMPLATE = (
    "Die vorherige Session lief in ein Kontextlimit und konnte nicht "
    "fortgesetzt werden. Der urspruengliche Auftrag war:\n\n{prompt}\n\n"
    "Aus dem bisherigen Verlauf ist folgender Stand bekannt:\n\n{digest}\n\n"
    "Setze die Arbeit auf dieser Grundlage fort. Verschaffe dir bei Bedarf "
    "einen kurzen Ueberblick ueber den aktuellen Stand im Arbeitsverzeichnis, "
    "bevor du weitermachst."
)

#: Steht anstelle des woertlichen Auszugs im Prompt, wenn CW_FRESH_DIGEST aus
#: ist. Der Satz muss dem Modell sagen, dass der Verlauf absichtlich fehlt -
#: sonst sucht es nach einem Stand, den es nie bekommen hat.
DIGEST_PLATZHALTER = (
    "Kein Auszug: Der bisherige Sitzungsverlauf wurde aus "
    "Datenschutzgruenden absichtlich nicht mitgegeben."
)


#: Aus einer Claude-Session geerbte Variablen schalten in einem Kindprozess
#: die Transkript-Speicherung ab (gleiches Muster wie im claude-session-runner).
#: Der Daemon startet aus sauberer systemd-Umgebung; `reply` laeuft dagegen
#: aus der Shell des Aufrufers — etwa der CEO-Session — und muss sie entfernen.
INHERITED_SESSION_VARS = (
    "CLAUDECODE", "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_PID", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_EFFORT",
)


def scrubbed_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k not in INHERITED_SESSION_VARS}


def next_attempt_no(task_id: str) -> int:
    """Naechste freie attempt-Nummer aus dem Run-Verzeichnis.

    Bewusst aus dem Verzeichnis statt aus task.attempts: ein reply zaehlt
    nicht als Fehlversuch, darf aber kein vorhandenes Log ueberschreiben.
    """
    nums = []
    for p in config.run_dir(task_id).glob("attempt-*.jsonl"):
        try:
            nums.append(int(p.stem.split("-", 1)[1]))
        except (IndexError, ValueError):
            continue
    return max(nums, default=0) + 1


def build_reply_command(task: Task, text: str) -> list[str]:
    """Kommando fuer eine Antwort an einen blockierten managed-Task.

    Haengt einen Turn an das bestehende Transkript an (`-r`). Rein funktional,
    damit der Kommandobau ohne Subprozess testbar bleibt. Der Watchdog selbst
    beantwortet weiterhin nichts — die inhaltliche Antwort kommt vom Aufrufer
    (CEO oder Mensch), hier wird nur der Mechanismus gekapselt.
    """
    cmd = [config.CLAUDE_BIN, "-p", text,
           "-r", task.session_id,
           "--output-format", "stream-json", "--verbose"]
    if task.model:
        cmd += ["--model", task.model]
    if task.permission_mode:
        cmd += ["--permission-mode", task.permission_mode]
    if task.max_budget_usd:
        cmd += ["--max-budget-usd", str(task.max_budget_usd)]
    return cmd


class RecoveryEngine:
    def __init__(self, registry: Registry, notify: Notifier, dry_run: bool = False):
        self.registry = registry
        self.notify = notify
        self.dry_run = dry_run
        #: task_id -> Popen der laufenden managed Prozesse
        self.children: dict[str, subprocess.Popen] = {}
        #: task_id -> (Fingerabdruck der Entscheidung, Zeit der letzten
        #: INFO-Zeile). Grundlage der Wiederholungsunterdrueckung in `_log`.
        self._logged: dict[str, tuple[tuple, float]] = {}

    # ------------------------------------------------------------ Kommandos

    def build_command(self, task: Task, resume: bool,
                      prompt: str, session_id: Optional[str]) -> list[str]:
        cmd = [config.CLAUDE_BIN, "-p", prompt,
               "--output-format", "stream-json",
               # stream-json verlangt --verbose (per Testlauf bestaetigt)
               "--verbose"]
        if resume and task.session_id:
            cmd += ["-r", task.session_id]
        elif session_id:
            cmd += ["--session-id", session_id]
        if task.model:
            cmd += ["--model", task.model]
        if task.permission_mode:
            cmd += ["--permission-mode", task.permission_mode]
        if task.max_budget_usd:
            cmd += ["--max-budget-usd", str(task.max_budget_usd)]
        return cmd

    def _digest(self, task: Task, obs: Observation, limit: int = 2000) -> str:
        """Stand aus dem bisherigen Verlauf fuer den Neuanfang.

        Ist CW_FRESH_DIGEST aus, verlaesst hier kein woertlicher
        Sitzungsinhalt die Maschine - dann steht nur der Platzhalter im
        Prompt.
        """
        if not config.FRESH_DIGEST:
            return DIGEST_PLATZHALTER
        text = (obs.tail_text or "").strip()
        return text[-limit:] if text else "(kein verwertbarer Verlauf gefunden)"

    def fresh_prompt(self, task: Task, obs: Observation) -> str:
        """Prompt fuer den Neuanfang nach einem Kontextlimit.

        Eigene Methode, damit der Prompt ohne Subprozess pruefbar bleibt.
        """
        return FRESH_TEMPLATE.format(prompt=task.original_prompt,
                                     digest=self._digest(task, obs))

    # -------------------------------------------------------- Protokollierung

    #: Mehr Eintraege behaelt `_logged` nicht; danach fliegt der aelteste raus.
    #: Nur eine Obergrenze fuer den Dauerbetrieb, kein fachliches Limit.
    _LOG_MEMO_MAX = 256

    def _log_decision(self, task: Task, decision: Decision, now: float) -> None:
        """Jede Entscheidung protokollieren, Wiederholungen aber leise.

        Ohne Eingriff faellt alle `POLL_INTERVAL` Sekunden dieselbe Zeile an
        ('action: none, reason: laeuft'). Ueber Stunden verdraengt das jedes
        echte Ereignis aus dem Log. Deshalb: ein Eingriff, eine geaenderte
        Entscheidung oder ein abgelaufener Wiederholungsabstand landen als
        INFO, alles andere als DEBUG (sichtbar mit `-v`). Die Entscheidung
        selbst bleibt davon unberuehrt — das hier aendert nur die Stufe.
        """
        fingerprint = (task.mode.value, decision.action.value,
                       decision.error_class.value,
                       _ohne_fortschritt(decision.reason))
        previous = self._logged.get(task.id)
        repeat = config.LOG_REPEAT_INTERVAL
        loud = (
            decision.action is not Action.NONE
            or previous is None
            or previous[0] != fingerprint
            or repeat <= 0
            or now - previous[1] >= repeat
        )
        if loud:
            if len(self._logged) >= self._LOG_MEMO_MAX:
                oldest = min(self._logged, key=lambda k: self._logged[k][1])
                del self._logged[oldest]
            self._logged[task.id] = (fingerprint, now)
        log.log(logging.INFO if loud else logging.DEBUG, "entscheidung", extra={
            "task": task.id, "title": task.title, "mode": task.mode.value,
            "action": decision.action.value, "class": decision.error_class.value,
            "reason": decision.reason, "delay": round(decision.delay, 1),
            "attempts": task.attempts, "dry_run": self.dry_run,
        })

    # ------------------------------------------------------------- Ausfuehren

    @staticmethod
    def _clear_error(task: Task) -> None:
        """Einen ueberwundenen Fehler aus dem Task loeschen.

        `last_error_class` wurde bisher nur gesetzt, nie zurueckgenommen. Ein
        RATE_LIMIT von 00:08 klebte deshalb noch Stunden spaeter an einer
        Session, die laengst wieder arbeitete — `list` zeigte es als aktuellen
        Fehler, die Uebersicht als Warnpille (beobachtet am 2026-07-31).

        Aufgeraeumt wird nur, wenn der Prozess lebt UND die Entscheidung
        keinen Fehler mehr sieht. `next_retry_at` geht mit: ein Termin fuer
        einen Wiederanlauf ist gegenstandslos, solange die Session laeuft.
        Die Wiederanlauf-Regel kann das nicht stoeren, sie verlangt
        ausdruecklich `not obs.alive`.

        `last_error_text` bleibt absichtlich stehen — er ist das Gedaechtnis
        fuer die Nachschau, taucht aber in keiner Statusanzeige auf.
        """
        task.last_error_class = None
        task.next_retry_at = None
        # Die Melde-Sperre geht mit: nach einer ueberstandenen Stoerung soll
        # die naechste sofort gemeldet werden und nicht erst, wenn die alte
        # Sperre ausgelaufen ist.
        task.mute_until = None

    def execute(self, task: Task, decision: Decision, obs: Observation,
                now: Optional[float] = None) -> Task:
        now = now if now is not None else time.time()
        decision = enforce_mode_guard(task, decision)

        self._log_decision(task, decision, now)

        if decision.error_class is not ErrorClass.NONE:
            task.last_error_class = decision.error_class.value
            task.last_error_text = decision.reason[:2000]
        elif decision.action is Action.NONE and obs.alive:
            self._clear_error(task)

        handler = {
            Action.NONE: self._act_none,
            Action.NOTIFY: self._act_notify,
            Action.SCHEDULE: self._act_schedule,
            Action.START: self._act_launch,
            Action.RESUME: self._act_launch,
            Action.RESTART_FRESH: self._act_launch,
            Action.FAIL: self._act_fail,
            Action.COMPLETE: self._act_complete,
        }[decision.action]
        return handler(task, decision, obs, now)

    # ----------------------------------------------------------- Einzelfaelle

    def _act_none(self, task: Task, decision: Decision, obs: Observation,
                  now: float) -> Task:
        if obs.progressed:
            task.last_progress_at = now
            task.transcript_size = obs.transcript_size
            task.same_marker_count = 0
        if task.status not in (Status.RUNNING, Status.PENDING):
            task.status = Status.RUNNING
        return task

    def _act_notify(self, task: Task, decision: Decision, obs: Observation,
                    now: float) -> Task:
        urgency = URGENCY_CRITICAL if decision.error_class in (
            ErrorClass.CRASH, ErrorClass.CONTEXT) else URGENCY_NORMAL
        if decision.error_class is ErrorClass.AWAITING_INPUT:
            task.status = Status.BLOCKED
            urgency = URGENCY_NORMAL
        # Nicht in Dauerschleife melden — aber die Sperre gilt nur fuer die
        # Meldung, nicht fuer das Hinsehen.
        #
        # Frueher stand hier `task.next_retry_at`. Den liest der Daemon aber
        # als "diesen Task bis dahin gar nicht erst ansehen" (daemon._process),
        # und STALL_SECONDS betraegt 900 s: eine einzige Meldung machte den
        # Watchdog fuer eine Viertelstunde blind. Gemessen am 2026-07-31 an
        # zwei laufenden Sitzungen — beide hatten in der Sperre rund 100 kB
        # geschrieben, ohne dass davon etwas ankam. Schlimmer noch, die Sperre
        # hielt sich selbst am Leben: `_clear_error` haette den Termin
        # geloescht, sobald die Session wieder lief, wurde in dieser Zeit aber
        # nie erreicht. Waere die Sitzung stattdessen abgestuerzt, haette es
        # bis zu 900 s gedauert, bis das jemand bemerkt.
        if task.mute_until and now < task.mute_until:
            return task
        self.notify.send(
            f"Claude Watchdog: {task.title}",
            decision.notify or decision.reason,
            urgency,
        )
        task.mute_until = now + max(decision.delay, config.STALL_SECONDS)
        return task

    def _act_schedule(self, task: Task, decision: Decision, obs: Observation,
                      now: float) -> Task:
        retry_at = decision.effective_retry_at(now)
        task.next_retry_at = retry_at
        task.status = Status.WAITING_FOR_LIMIT
        if decision.counts_as_attempt:
            task.attempts += 1
        if decision.notify:
            self.notify.send(f"Claude Watchdog: {task.title}", decision.notify, URGENCY_LOW)
        return task

    def _act_fail(self, task: Task, decision: Decision, obs: Observation,
                  now: float) -> Task:
        task.status = Status.FAILED
        task.next_retry_at = None
        if task.session_id:
            self.registry.release_lock(task.session_id)
        self.notify.send(
            f"Claude Watchdog: {task.title} fehlgeschlagen",
            decision.notify or decision.reason,
            URGENCY_CRITICAL,
        )
        return task

    def _act_complete(self, task: Task, decision: Decision, obs: Observation,
                      now: float) -> Task:
        task.status = Status.DONE
        task.next_retry_at = None
        task.cost_usd_spent = self._read_cost(task) or task.cost_usd_spent
        if task.session_id:
            self.registry.release_lock(task.session_id)
        self.notify.send(
            f"Claude Watchdog: {task.title} fertig",
            decision.notify or f"Abgeschlossen. Kosten: ${task.cost_usd_spent:.4f}",
            URGENCY_LOW,
        )
        return task

    def _act_launch(self, task: Task, decision: Decision, obs: Observation,
                    now: float) -> Task:
        """START / RESUME / RESTART_FRESH - der einzige Weg, der Prozesse startet."""
        if task.mode is not Mode.MANAGED:
            # Kann durch den Guard eigentlich nicht passieren; doppelt genaeht.
            log.error("Launch fuer observed-Task unterbunden", extra={"task": task.id})
            return task

        if config.stop_requested():
            log.warning("STOP-Datei vorhanden - kein Start", extra={"task": task.id})
            task.next_retry_at = now + config.POLL_INTERVAL
            return task

        # Backoff einhalten. Der faellige Start kommt dann ueber den Zweig
        # "geplanter Wiederanlauf" in decide() - der Versuchszaehler wird also
        # erst hochgezaehlt, wenn wirklich gestartet wird.
        due = launch_postponed_until(decision, now)
        if due is not None:
            task.next_retry_at = due
            task.status = Status.WAITING_FOR_LIMIT
            log.info("Wiederanlauf geplant", extra={
                "task": task.id, "in_s": round(due - now, 1),
                "class": decision.error_class.value, "reason": decision.reason,
            })
            return task

        fresh = decision.action is Action.RESTART_FRESH or not task.session_id
        session_id = task.session_id
        if fresh:
            session_id = str(uuid.uuid4())

        # Lock pro Session: kein Task wird doppelt gestartet.
        lock_key = session_id or task.id
        if not self.registry.acquire_lock(lock_key, task.id):
            log.warning("Session-Lock belegt", extra={"task": task.id, "session": lock_key})
            task.next_retry_at = now + config.POLL_INTERVAL
            return task

        if decision.action is Action.START:
            prompt = task.original_prompt
        elif decision.action is Action.RESTART_FRESH:
            prompt = self.fresh_prompt(task, obs)
        else:
            prompt = CONTINUE_TEMPLATE.format(reason=decision.reason,
                                              prompt=task.original_prompt)

        attempt_no = task.attempts + 1
        cmd = self.build_command(task, resume=not fresh, prompt=prompt,
                                 session_id=session_id)

        if self.dry_run:
            log.info("DRY-RUN: wuerde starten", extra={
                "task": task.id, "cmd": cmd[:6] + ["..."], "cwd": task.cwd,
                "session": session_id, "fresh": fresh,
            })
            task.next_retry_at = now + config.POLL_INTERVAL
            self.registry.release_lock(lock_key)
            return task

        run_dir = config.run_dir(task.id)
        run_dir.mkdir(parents=True, exist_ok=True)
        # Dateinummer aus dem Verzeichnis, nicht aus dem Zaehler.
        #
        # `attempt_no` ist task.attempts + 1 und bleibt der Fehlversuchszaehler.
        # Als Dateiname taugt er nicht mehr: ein `reply` schreibt ebenfalls ein
        # Protokoll, erhoeht den Zaehler aber bewusst NICHT (eine Antwort ist
        # kein Fehlversuch). Nach einer Antwort zeigen beide Rechenwege auf
        # dieselbe Nummer, und der naechste Start des Daemons ueberschrieb das
        # Protokoll der Antwort stillschweigend (nachgestellt am 2026-07-31).
        #
        # next_attempt_no() zaehlt vorhandene Dateien und waechst dadurch
        # monoton — beide Pfade koennen sich nicht mehr in die Quere kommen.
        datei_no = next_attempt_no(task.id)
        out_path = config.run_log(task.id, datei_no)
        err_path = config.run_err(task.id, datei_no)

        out_fh = None
        err_fh = None
        try:
            if config.RUN_LAUNCHER == "service":
                proc = self._starte_als_dienst(cmd, task, datei_no,
                                               out_path, err_path)
            else:
                out_fh = out_path.open("wb")
                err_fh = err_path.open("wb")
                proc = subprocess.Popen(
                    mit_speichergrenze(cmd, task.id),
                    cwd=task.cwd, stdout=out_fh, stderr=err_fh,
                    stdin=subprocess.DEVNULL,
                    start_new_session=True,   # ueberlebt einen Daemon-Neustart
                )
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            log.error("Start fehlgeschlagen", extra={"task": task.id, "error": str(exc)})
            self.registry.release_lock(lock_key)
            task.last_error_class = ErrorClass.CRASH.value
            task.last_error_text = f"Start fehlgeschlagen: {exc}"
            task.attempts += 1
            task.next_retry_at = now + backoff.exponential(task.attempts)
            task.status = Status.WAITING_FOR_LIMIT
            return task
        finally:
            for fh in (out_fh, err_fh):
                if fh is None:
                    continue
                try:
                    fh.close()
                except OSError:
                    pass

        self.children[task.id] = proc
        self.registry.record_restart(task.id)

        task.pid = proc.pid
        task.session_id = session_id
        task.transcript_path = str(config.transcript_path(task.cwd, session_id)) \
            if session_id else task.transcript_path
        task.status = Status.RUNNING
        task.attempts = attempt_no if decision.counts_as_attempt else task.attempts
        task.last_progress_at = now
        task.next_retry_at = None
        task.transcript_size = 0

        marker = f"{obs.transcript_size}:{decision.error_class.value}"
        if marker == task.last_resume_marker:
            task.same_marker_count += 1
        else:
            task.last_resume_marker = marker
            task.same_marker_count = 1

        log.info("gestartet", extra={
            "task": task.id, "pid": proc.pid, "session": session_id,
            "attempt": attempt_no, "fresh": fresh, "log": str(out_path),
            "startweg": config.RUN_LAUNCHER,
        })
        return task

    def _starte_als_dienst(self, cmd: list[str], task: Task, datei_no: int,
                           out_pfad: Path, err_pfad: Path) -> DienstLauf:
        """Den Lauf vom User-Manager starten lassen, statt selbst zu forken.

        Ein Scope waere ein Kind des Daemons und erbte dessen
        Netzwerk-Namespace; ein Dienst nicht. Genau darauf beruht die
        Netz-Isolation: der Daemon selbst darf per PrivateNetwork=yes ohne
        Netz laufen, waehrend der Lauf die API weiterhin erreicht.
        Gemessen am 2026-08-17, Nachweis in SECURITY.md.
        """
        unit = "claude-watchdog-%s-%03d" % (task.id, datei_no)
        rc_pfad = config.run_rc(task.id, datei_no)
        # Ein Rest eines frueheren Versuchs mit derselben Nummer wuerde sofort
        # als "schon beendet" gelesen.
        try:
            rc_pfad.unlink()
        except OSError:
            pass
        argv = dienst_kommando(cmd, task.id, unit, task.cwd, out_pfad,
                               err_pfad, rc_pfad, scrubbed_env())
        subprocess.run(argv, check=True, capture_output=True, text=True,
                       timeout=30)
        pid = warte_auf_mainpid(unit)
        if pid is None and not rc_pfad.exists():
            raise OSError("Dienst %s hat binnen %.0f s keine PID gemeldet"
                          % (unit, PID_WARTE_SEKUNDEN))
        # pid None mit vorhandener rc-Datei heisst: der Lauf war schneller als
        # der erste Blick. Kein Fehler - poll() liest den Wert im naechsten
        # Durchlauf aus der Datei.
        return DienstLauf(unit, pid, rc_pfad)

    # ------------------------------------------------------------- Hilfsmittel

    def _read_cost(self, task: Task) -> Optional[float]:
        """Liest total_cost_usd aus dem letzten result-Event."""
        run_log, _ = config.latest_run_files(task.id)
        if not run_log:
            return None
        from . import transcript as tr
        total = task.cost_usd_spent
        for ev in tr.tail_events(run_log, max_events=10):
            if ev.get("type") == "result" and ev.get("total_cost_usd") is not None:
                try:
                    total = task.cost_usd_spent + float(ev["total_cost_usd"])
                except (TypeError, ValueError):
                    pass
        return total

    def reap(self) -> list[tuple[str, int]]:
        """Sammelt beendete Kindprozesse ein und liefert (task_id, exit_code)."""
        finished: list[tuple[str, int]] = []
        for task_id, proc in list(self.children.items()):
            code = proc.poll()
            if code is None:
                continue
            finished.append((task_id, code))
            del self.children[task_id]
            log.info("Lauf beendet", extra={"task": task_id, "exit": code,
                                            "startweg": config.RUN_LAUNCHER})
        return finished

    def terminate_all(self) -> None:
        """Beim Herunterfahren NICHT toeten - laufende Arbeit soll weiterlaufen.

        Die Kinder laufen in einer eigenen Session (start_new_session=True) und
        werden beim naechsten Daemon-Start ueber pid + Run-Log wieder adoptiert.
        """
        for task_id, proc in self.children.items():
            log.info("lasse Lauf weiterlaufen",
                     extra={"task": task_id, "pid": proc.pid})
        self.children.clear()
