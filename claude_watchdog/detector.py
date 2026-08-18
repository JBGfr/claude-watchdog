"""Gesundheitspruefung: arbeitet noch / haengt / ist tot.

Es wird bewusst nicht auf ein einzelnes Signal vertraut. Kombiniert werden:
  a) lebt der Prozess (pid bzw. pgrep)
  b) waechst das Transkript (mtime + Groesse)
  c) `claude agents --json` (kennt Session + liefert busy/idle)
  d) bei managed Tasks zusaetzlich Run-Log und Exit-Code
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Optional

from . import config, transcript
from .logging_setup import get
from .models import Mode, Observation, Status, Task

log = get("detector")


# --------------------------------------------------------------------------
# Prozesse
# --------------------------------------------------------------------------

def pid_alive(pid: Optional[int]) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # Zombie zaehlt nicht als lebendig.
    try:
        with open(f"/proc/{pid}/stat", "r", encoding="utf-8") as fh:
            fields = fh.read().rsplit(")", 1)[-1].split()
        if fields and fields[0] == "Z":
            return False
    except OSError:
        pass
    return True


def pid_is_claude(pid: Optional[int]) -> bool:
    """Prueft, ob hinter der pid wirklich ein claude-Prozess steckt.

    Schuetzt davor, eine wiederverwendete pid faelschlich als 'lebt noch' zu
    werten.
    """
    if not pid:
        return False
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            cmdline = fh.read().replace(b"\0", b" ").decode("utf-8", "replace")
    except OSError:
        return False
    return "claude" in cmdline.lower()


def supervising_unit(pid: Optional[int]) -> Optional[str]:
    """Name der systemd-Unit, unter der die pid laeuft - sonst None.

    Die letzte Wegkomponente der cgroup-Zeile ist der Unit-Name, etwa
    'claude-session@dauertest.service'. Steht dort keine .service-Einheit
    (freier Prozess, Scope einer Login-Shell), gibt es keine Aufsicht.
    """
    if not pid:
        return None
    try:
        with open(f"/proc/{pid}/cgroup", "r", encoding="utf-8") as fh:
            raw = fh.read()
    except OSError:
        return None
    for line in raw.splitlines():
        path = line.rsplit(":", 1)[-1]
        name = path.rsplit("/", 1)[-1]
        if name.endswith(".service"):
            return name
    return None


def externally_supervised(pid: Optional[int]) -> Optional[str]:
    """Unit-Name, falls ein fremder Dienst die Session schon neu startet.

    Sessions unter einer eigenen systemd-Unit (Vorgabe: 'claude-session@...')
    haben bereits eine Aufsicht, die den Prozess bei Bedarf neu startet. Der
    Watchdog wuerde sie sonst bei jedem dieser Neustarts erneut aufnehmen und
    beim naechsten Durchlauf wieder als beendet abraeumen - Karteileichen und
    Meldungen ueber etwas, das sich selbst repariert hat.
    """
    if not config.SKIP_SUPERVISED:
        return None
    unit = supervising_unit(pid)
    if unit and unit.startswith(config.SUPERVISED_UNIT_PREFIX):
        return unit
    return None


# --------------------------------------------------------------------------
# `claude agents --json`
# --------------------------------------------------------------------------

class AgentsSnapshot:
    """Gecachter Blick auf die vom CLI gemeldeten aktiven Sessions."""

    def __init__(self, ttl: Optional[int] = None):
        self.ttl = ttl if ttl is not None else config.AGENTS_CACHE_TTL
        self._at: float = 0.0
        self._by_session: dict[str, dict[str, Any]] = {}
        self._ok = False

    def refresh(self, force: bool = False) -> dict[str, dict[str, Any]]:
        now = time.time()
        if not force and self._at and (now - self._at) < self.ttl:
            return self._by_session
        self._at = now
        try:
            proc = subprocess.run(
                [config.CLAUDE_BIN, "agents", "--json", "--all"],
                capture_output=True, text=True, timeout=config.AGENTS_TIMEOUT,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            log.warning("claude agents --json fehlgeschlagen", extra={"error": str(exc)})
            self._ok = False
            return self._by_session
        if proc.returncode != 0:
            log.warning("claude agents --json exit != 0",
                        extra={"rc": proc.returncode, "stderr": proc.stderr[:300]})
            self._ok = False
            return self._by_session
        try:
            entries = json.loads(proc.stdout or "[]")
        except ValueError:
            log.warning("claude agents --json lieferte kein JSON")
            self._ok = False
            return self._by_session
        self._by_session = {
            str(e.get("sessionId")): e
            for e in entries
            if isinstance(e, dict) and e.get("sessionId")
        }
        self._ok = True
        return self._by_session

    @property
    def usable(self) -> bool:
        return self._ok

    def get(self, session_id: Optional[str]) -> Optional[dict[str, Any]]:
        if not session_id:
            return None
        return self.refresh().get(session_id)

    def all(self) -> dict[str, dict[str, Any]]:
        return self.refresh()


# --------------------------------------------------------------------------
# Beobachtung
# --------------------------------------------------------------------------

def _gehoert_zur_session(pfad: str, session_id: str) -> bool:
    """Traegt die Datei die UUID dieser Session im Namen?"""
    return Path(pfad).stem == session_id


def resolve_transcript(task: Task, allow_fallback: bool = True) -> Optional[Path]:
    """Ermittelt den Transkriptpfad — niemals den einer fremden Session.

    Ist die session_id bekannt, ist der Pfad aus cwd und UUID eindeutig
    bestimmt; dann darf der Notbehelf nicht greifen. Er ist ausdruecklich
    nur fuer den Fall gedacht, dass gar keine session_id vorliegt (siehe
    `transcript.newest_transcript`).

    Ohne diese Einschraenkung nahm ein Task das Transkript einer fremden
    Session an und behielt es dauerhaft: `auto_attach` legt den Task an,
    sobald das CLI die Session meldet, und das kann sein, bevor die Datei
    ueberhaupt existiert. Beobachtet am 2026-07-31 an Task 70db0af7 —
    angelegt 14:21:25, eigenes Transkript ab 14:22:06, also 41 s zu frueh.
    In dieser Luecke griff "neueste .jsonl im Projektverzeichnis" und der
    Task ueberwachte fortan die 15-MB-Datei einer voellig anderen, sehr
    aktiven Sitzung. Der gemeldete "Fortschritt" waren fremde Schreib-
    vorgaenge: die laengst beendete Session galt dauerhaft als lebendig und
    wurde nie abgeschlossen.

    `allow_fallback` bleibt zusaetzlich fuer noch nie gestartete Tasks
    False — dort ist die session_id ja noch unbekannt.
    """
    if task.session_id:
        candidate = config.transcript_path(task.cwd, task.session_id)
        if candidate.exists():
            return candidate
        # Ein frueher gespeicherter Pfad zaehlt nur, wenn er wirklich zu
        # dieser Session gehoert — so heilt ein falsch gemerkter Pfad von
        # selbst aus, statt sich weiter fortzuschreiben.
        if (task.transcript_path
                and _gehoert_zur_session(task.transcript_path, task.session_id)
                and Path(task.transcript_path).exists()):
            return Path(task.transcript_path)
        # Lieber gar kein Transkript als das einer fremden Sitzung.
        return None
    if task.transcript_path and Path(task.transcript_path).exists():
        return Path(task.transcript_path)
    if not allow_fallback:
        return None
    # Notbehelf: neueste .jsonl im Projektverzeichnis; der Dateiname ist die UUID.
    return transcript.newest_transcript(task.cwd)


def observe(task: Task, agents: AgentsSnapshot,
            now: Optional[float] = None,
            exit_code: Optional[int] = None) -> Observation:
    now = now if now is not None else time.time()
    obs = Observation()
    obs.exit_code = exit_code

    # --- c) CLI-Sicht ------------------------------------------------------
    entry = agents.get(task.session_id)
    # Der Daemon frischt den Snapshot pro Tick auf; usable sagt, ob die letzte
    # Abfrage geklappt hat. Ohne das waere "CLI kennt die Session nicht" nicht
    # von "CLI liess sich nicht fragen" zu unterscheiden.
    obs.cli_usable = agents.usable
    if entry:
        obs.known_to_cli = True
        obs.agent_status = str(entry.get("status") or "") or None
        # Eine tote pid durch die der Registry ersetzen. Bisher wurde nur
        # gefuellt, wenn task.pid leer war — nach einem Resume laeuft die
        # Sitzung aber unter neuer pid, und die alte blieb stehen. Beobachtet
        # am 2026-07-31: task.pid 33781 laengst tot, waehrend die Registry
        # fuer dieselbe session_id 115196 als lebend fuehrte. `list` zeigte
        # damit eine PID, hinter der nichts mehr steckt.
        #
        # Bewusst nur bei toter pid: laeuft der gespeicherte Prozess noch,
        # ist er die verlaesslichere Auskunft als ein Snapshot, der bis zu
        # AGENTS_CACHE_TTL alt sein kann.
        neue_pid = entry.get("pid")
        if neue_pid and neue_pid != task.pid and not pid_alive(task.pid):
            task.pid = neue_pid

    # --- a) Prozess --------------------------------------------------------
    proc_alive = pid_alive(task.pid) and pid_is_claude(task.pid)
    # Ein voellig leerer Registry-Eintrag ist kein Lebenszeichen.
    #
    # Die Registry behaelt beendete Sitzungen. Gemessen am 2026-07-31: alle
    # sieben lebenden Eintraege hatten pid UND status, alle vier verwaisten
    # hatten weder noch. Ohne diese Unterscheidung blieb obs.alive dauerhaft
    # True und _observed_session_gone() konnte nie greifen — ein Task stand
    # 35 Stunden auf 'laeuft', obwohl sein Prozess laengst weg war. Solche
    # Karteileichen sammelten sich in `list` und in der Uebersicht.
    #
    # Bewusst 'pid ODER status': ein Eintrag aus dem Cache, dessen Prozess
    # gerade verschwunden ist, traegt weiterhin einen Status und gilt bis zum
    # naechsten Snapshot als lebend. Nur wo beides fehlt, weiss die Registry
    # selbst nichts mehr.
    cli_lebt = bool(entry) and (entry.get("pid") is not None
                                or entry.get("status") is not None)
    obs.alive = proc_alive or cli_lebt

    # Ein eingesammelter Exit-Code ist verbindlich: der Prozess IST beendet.
    # Der CLI-Snapshot ist bis zu AGENTS_CACHE_TTL Sekunden alt und listet die
    # Session dann noch als aktiv - ohne diese Regel gilt ein gerade
    # abgestuerzter Lauf als "lebt" und der Exit-Code verfaellt ungenutzt.
    if exit_code is not None:
        obs.alive = False

    # --- b) Transkript -----------------------------------------------------
    # Ein Task, der noch nie lief, darf sich kein fremdes Transkript aneignen.
    never_started = task.status is Status.PENDING and not task.session_id
    tpath = resolve_transcript(task, allow_fallback=not never_started)
    # Nur ein echter Wechsel zaehlt: war noch gar kein Pfad gemerkt, gibt es
    # nichts, wovon der Stand stammen koennte — dann entscheidet allein der
    # Groessenvergleich weiter unten.
    gewechselt = bool(tpath) and bool(task.transcript_path) and \
        str(tpath) != task.transcript_path
    if tpath:
        task.transcript_path = str(tpath)
        if not task.session_id:
            task.session_id = transcript.session_id_from_path(tpath)
        obs.transcript_size = transcript.file_size(tpath)
        obs.events = transcript.tail_events(tpath)
        obs.tail_text = transcript.extract_text(obs.events)
        mtime = transcript.file_mtime(tpath)
    else:
        mtime = 0.0

    # --- d) managed: Run-Log + Exit-Code -----------------------------------
    if task.mode is Mode.MANAGED:
        run_log, run_err = config.latest_run_files(task.id)
        if run_log:
            run_size = transcript.file_size(run_log)
            obs.transcript_size = max(obs.transcript_size, 0) + run_size
            obs.events = obs.events + transcript.tail_events(run_log)
            obs.tail_text = "\n".join(filter(None, [
                obs.tail_text,
                transcript.extract_text(transcript.tail_events(run_log)),
            ]))
            mtime = max(mtime, transcript.file_mtime(run_log))
        if run_err:
            obs.tail_text = "\n".join(filter(None, [
                obs.tail_text, transcript.tail_bytes(run_err, 8192),
            ]))
            mtime = max(mtime, transcript.file_mtime(run_err))

    # --- Fortschritt -------------------------------------------------------
    # Der gemerkte Stand gehoert immer zu genau einer Datei. Zeigt der Task
    # jetzt auf eine andere, oder ist der gemerkte Stand groesser als das,
    # was ueberhaupt da ist — bei append-only unmoeglich —, dann stammt er
    # von einer fremden Datei und wird uebernommen statt verglichen.
    #
    # Ohne das blieb ein Task nach der Umstellung auf sein richtiges
    # Transkript dauerhaft ohne Fortschritt: beobachtet am 2026-07-31 an
    # 593913bf, das eigene Transkript hatte 3 913 708 Bytes, gemerkt waren
    # 15 183 742 aus der fremden Datei. Erst wenn die eigene Sitzung diese
    # 15 MB ueberschritten haette, waere wieder Fortschritt sichtbar
    # geworden — bis dahin haette der Watchdog sie fuer stehengeblieben
    # gehalten.
    if gewechselt or obs.transcript_size < task.transcript_size:
        task.transcript_size = obs.transcript_size
    obs.progressed = obs.transcript_size > task.transcript_size
    reference = task.last_progress_at or 0.0
    if mtime:
        reference = max(reference, mtime)
    obs.idle_seconds = max(0.0, now - reference) if reference else 0.0

    # busy laut CLI zaehlt als Lebenszeichen, auch wenn gerade nichts
    # geschrieben wurde (z.B. laufender Modellaufruf).
    if obs.agent_status == "busy" and obs.idle_seconds > config.STALL_SECONDS:
        log.debug("busy laut CLI trotz Stillstand", extra={"task": task.id})

    return obs


def is_stalled(task: Task, obs: Observation) -> bool:
    """Stall-Kriterium: lebt, aber seit STALL_SECONDS kein Fortschritt."""
    if not obs.alive:
        return False
    if obs.progressed:
        return False
    return obs.idle_seconds >= config.STALL_SECONDS
