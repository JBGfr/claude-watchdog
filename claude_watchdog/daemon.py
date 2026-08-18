"""Hauptschleife des Watchdogs."""

from __future__ import annotations

import fcntl
import os
import shutil
import signal
import time
from typing import Optional

from . import config, detector, logging_setup, notifier, transcript
from .classifier import classify
from .logging_setup import get
from .models import Mode, Status, Task
from .recovery import RecoveryEngine, decide
from .registry import Registry, make_task

log = get("daemon")


class SingleInstance:
    """Verhindert, dass zwei Daemons gleichzeitig laufen."""

    def __init__(self, path=None):
        self.path = str(path or config.DAEMON_LOCK)
        self._fh = None

    def __enter__(self) -> "SingleInstance":
        config.ensure_dirs()
        self._fh = open(self.path, "w", encoding="utf-8")
        try:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self._fh.close()
            raise RuntimeError(
                f"Ein anderer Watchdog laeuft bereits (Lock: {self.path})"
            ) from exc
        self._fh.write(str(os.getpid()))
        self._fh.flush()
        return self

    def __exit__(self, *exc) -> None:
        if self._fh:
            try:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            finally:
                self._fh.close()


def _gemerkte_felder(task: Task) -> tuple:
    """Felder, deren Aenderung in die Datenbank muss.

    Frueher standen hier nur Status, Versuche und Termin. `observe()` setzt
    aber auch pid, session_id und transcript_path — aendert sich nur eines
    davon, wurde nichts gespeichert. Folge: nach einem Resume trug der Task
    in der Datenbank weiter die alte, tote pid, waehrend die Registry laengst
    eine andere fuehrte (beobachtet am 2026-07-31: 33781 tot, 115196 lebend,
    gleiche session_id). `list` und die Uebersicht zeigten damit eine PID,
    hinter der nichts mehr steckt.

    `transcript_size` gehoert ebenfalls dazu: `observe()` uebernimmt den
    Stand, wenn der Task auf eine andere Datei zeigt. Ohne diesen Eintrag
    waere die Uebernahme nie gespeichert worden — jeder Tick haette sie neu
    berechnet und wieder verworfen, und der Task haette dauerhaft keinen
    Fortschritt mehr melden koennen.
    """
    return (task.status, task.attempts, task.next_retry_at, task.mute_until,
            task.pid, task.session_id, task.transcript_path,
            task.transcript_size)


def drop_run_dir(task_id: str) -> None:
    """Run-Logs eines entfernten Tasks wegraeumen.

    Bewusst auf Modulebene und nicht als Methode: `cleanup()` raeumt die
    Protokolle mit, `cli.cmd_rm` tat es nicht — wer einen Task von Hand
    entfernte, liess seine Run-Logs als Waisen zurueck, um die sich danach
    niemand mehr kuemmern kann (cleanup() laeuft nur ueber Tasks, die es
    noch gibt). Beobachtet am 2026-08-01 nach zwei Testlaeufen: Task weg,
    Verzeichnisse mit 12 und 26 KB blieben liegen.
    """
    directory = config.run_dir(task_id)
    # Sicherheitsnetz: ausschliesslich ein direktes Unterverzeichnis von
    # RUNS_DIR wird angefasst, sonst gar nichts.
    if directory.parent != config.RUNS_DIR or not directory.is_dir():
        return
    try:
        shutil.rmtree(directory)
    except OSError as exc:
        log.warning("Run-Logs nicht entfernbar",
                    extra={"task": task_id, "error": str(exc)})


class Watchdog:
    def __init__(self, registry: Optional[Registry] = None, dry_run: bool = False):
        self.registry = registry or Registry()
        self.dry_run = dry_run
        self.notify = notifier.build(dry_run=dry_run)
        self.engine = RecoveryEngine(self.registry, self.notify, dry_run=dry_run)
        self.agents = detector.AgentsSnapshot()
        self._running = True
        self._stop_announced = False
        #: 0.0 = beim ersten Durchlauf wird sofort einmal aufgeraeumt.
        self._last_cleanup = 0.0

    # ------------------------------------------------------------ Lifecycle

    def install_signal_handlers(self) -> None:
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, self._handle_signal)

    def _handle_signal(self, signum, _frame) -> None:
        log.info("Signal empfangen, fahre herunter", extra={"signal": signum})
        self._running = False

    def adopt(self) -> None:
        """Beim Start bestehende Tasks rekonstruieren statt blind neu starten."""
        adopted = alive = 0
        for task in self.registry.active():
            adopted += 1
            still_alive = (detector.pid_alive(task.pid)
                           and detector.pid_is_claude(task.pid))
            entry = self.agents.get(task.session_id)
            if still_alive or entry:
                alive += 1
                task.status = Status.RUNNING
                if entry and not task.pid:
                    task.pid = entry.get("pid")
            elif task.status is Status.RUNNING:
                # Prozess ist waehrend unserer Abwesenheit verschwunden. Nicht
                # sofort neu starten - der naechste Tick klassifiziert erst.
                task.pid = None
            self.registry.update(task)
        removed = self.registry.reap_stale_locks()
        log.info("Adoption abgeschlossen", extra={
            "tasks": adopted, "noch_aktiv": alive, "locks_bereinigt": removed,
        })

    # ---------------------------------------------------------- Auto-Attach

    def _observed_task(self, session_id: str, entry: dict, cwd: str) -> Task:
        """Baut den Task zu einer laufenden fremden Session (nur beobachten)."""
        tpath = config.transcript_path(cwd, session_id)
        task = make_task(
            registry=self.registry,
            title=entry.get("name") or f"session {session_id[:8]}",
            cwd=cwd, mode=Mode.OBSERVED, session_id=session_id,
            status=Status.RUNNING,
        )
        task.pid = entry.get("pid")
        task.transcript_path = str(tpath)
        task.transcript_size = transcript.file_size(tpath)
        task.last_progress_at = transcript.file_mtime(tpath) or time.time()
        return task

    @staticmethod
    def _lebt_wieder(task: Task, entry: dict) -> bool:
        """Abgeschlossener Task, dessen Session nachweislich wieder laeuft.

        Eine session_id ueberlebt das Ende einer Sitzung: `claude --resume`
        nimmt dieselbe wieder auf. Der Task dazu ist aber ein einmaliger
        Eintrag — ist er einmal terminal, sperrte er die Session dauerhaft
        gegen eine erneute Aufnahme.

        Beobachtet am 2026-07-31: Task 50d8d1f2 wurde am 30.07. um 14:15:46
        abgeschlossen, waehrend pid 20728 als `claude --resume 2eb30eac-…`
        weiterlief und vom CLI als `busy` gemeldet wurde. Zweiter Fall:
        510c5877, abgeschlossen 30.07. um 14:15:26, pid 14616 lebendig. Zwei
        laufende Sitzungen standen damit ueber einen Tag ohne jede Aufsicht —
        genau das, wovor der Watchdog schuetzen soll.

        Verlangt wird ein **lebender claude-Prozess**, nicht bloss ein
        Eintrag: eine gerade beendete Sitzung steht noch im Snapshot, hat
        aber keine lebende pid mehr. Damit bleibt die urspruengliche Sorge
        gewahrt, dass eine eben abgeschlossene Session sofort wieder
        aufgenommen und im naechsten Durchlauf erneut abgeschlossen wird.
        """
        if not task.status.is_terminal:
            return False
        pid = entry.get("pid")
        return (bool(pid) and detector.pid_alive(pid)
                and detector.pid_is_claude(pid))

    def _wiederaufnehmen(self, task: Task, entry: dict, cwd: str) -> Task:
        """Einen terminalen Task fuer die wieder laufende Session herrichten.

        Neu anlegen geht nicht: auf session_id liegt ein eindeutiger Index.
        Der vorhandene Eintrag wird deshalb zurueckgesetzt — Zaehler und
        alter Fehler muessen weg, sonst startet die Sitzung mit dem
        Gepaeck ihres letzten Lebens.
        """
        tpath = config.transcript_path(cwd, task.session_id)
        task.status = Status.RUNNING
        task.pid = entry.get("pid")
        task.mode = Mode.OBSERVED
        task.attempts = 0
        task.next_retry_at = None
        task.mute_until = None
        task.last_error_class = None
        task.transcript_path = str(tpath)
        task.transcript_size = transcript.file_size(tpath)
        task.last_progress_at = transcript.file_mtime(tpath) or time.time()
        return task

    def auto_attach(self) -> int:
        """Laufende Sessions von selbst aufnehmen.

        Immer als `observed`: der Mode-Guard verbietet damit jeden Eingriff,
        der Watchdog meldet nur.

        Sessions, zu denen es schon einen Task gibt, werden uebergangen —
        mit einer Ausnahme: laeuft die Sitzung nachweislich wieder, waehrend
        ihr Task terminal ist, wird der Eintrag wiederaufgenommen (siehe
        `_lebt_wieder`).
        """
        if not config.AUTO_ATTACH:
            return 0
        added = 0
        for session_id, entry in self.agents.all().items():
            vorhanden = self.registry.get_by_session(session_id)
            if vorhanden and not self._lebt_wieder(vorhanden, entry):
                continue
            cwd = entry.get("cwd")
            if not config.auto_attach_allows(cwd):
                log.debug("Auto-Attach uebersprungen", extra={
                    "session": session_id, "cwd": cwd,
                    "grund": "Arbeitsverzeichnis nicht freigegeben" if cwd
                             else "kein Arbeitsverzeichnis gemeldet",
                })
                continue
            unit = detector.externally_supervised(entry.get("pid"))
            if unit:
                log.debug("Auto-Attach uebersprungen", extra={
                    "session": session_id, "cwd": cwd, "unit": unit,
                    "grund": "Session laeuft unter fremder systemd-Aufsicht",
                })
                continue
            try:
                if vorhanden:
                    task = self._wiederaufnehmen(vorhanden, entry, cwd)
                    self.registry.update(task)
                else:
                    task = self.registry.add(
                        self._observed_task(session_id, entry, cwd))
            except Exception:
                log.exception("Auto-Attach fehlgeschlagen",
                              extra={"session": session_id})
                continue
            added += 1
            log.info("Session wieder aufgenommen" if vorhanden
                     else "Session automatisch aufgenommen", extra={
                "task": task.id, "session": session_id, "cwd": cwd,
                "title": task.title, "agent_status": entry.get("status"),
            })
        return added

    # ------------------------------------------------------------ Aufraeumen

    def cleanup(self, now: Optional[float] = None) -> int:
        """Abgeschlossene Tasks nach der Schonfrist entfernen.

        Auto-Attach legt pro Session einen Task an; ohne Verfallsdatum waechst
        die Liste unbegrenzt. Entfernt wird nur, was terminal ist und seit
        RETENTION_DAYS nicht mehr angefasst wurde - samt der Run-Logs, die
        sonst als Waisen liegen bleiben. Jede Entfernung steht im Log.
        """
        now = now if now is not None else time.time()
        if config.RETENTION_DAYS <= 0:
            return 0
        if now - self._last_cleanup < config.CLEANUP_INTERVAL:
            return 0
        self._last_cleanup = now

        cutoff = now - config.RETENTION_DAYS * 86400
        removed = 0
        for task in self.registry.terminal_before(cutoff):
            self.registry.delete(task.id)
            drop_run_dir(task.id)
            removed += 1
            log.info("Task aufgeraeumt", extra={
                "task": task.id, "title": task.title,
                "status": task.status.value,
                "alter_tage": round((now - task.updated_at) / 86400, 1),
            })
        if removed:
            log.info("Aufraeumen abgeschlossen", extra={
                "entfernt": removed, "schonfrist_tage": config.RETENTION_DAYS,
            })
        return removed

    # ----------------------------------------------------------------- Tick

    def tick(self, now: Optional[float] = None) -> None:
        now = now if now is not None else time.time()

        if config.stop_requested():
            if not self._stop_announced:
                log.warning("STOP-Datei vorhanden - Watchdog im Leerlauf",
                            extra={"file": str(config.STOP_FILE)})
                self._stop_announced = True
            return
        if self._stop_announced:
            log.info("STOP-Datei entfernt - nehme Arbeit wieder auf")
            self._stop_announced = False

        self.registry.reap_stale_locks()
        exits = {task_id: code for task_id, code in self.engine.reap()}
        self.agents.refresh()
        self.auto_attach()
        self.cleanup(now)
        budget = self.registry.restart_budget_available()
        if not budget:
            log.warning("Restart-Budget erschoepft",
                        extra={"letzte_stunde": self.registry.restarts_last_hour(),
                               "limit": config.MAX_RESTARTS_PER_HOUR})

        for task in self.registry.active():
            try:
                self._process(task, exits.get(task.id), budget, now)
            except Exception:
                log.exception("Fehler bei der Task-Verarbeitung",
                              extra={"task": task.id})

    def _process(self, task: Task, exit_code: Optional[int],
                 budget: bool, now: float) -> None:
        # Wartet dieser Task noch auf seinen Termin?
        if task.next_retry_at and now < task.next_retry_at:
            return

        # Der Vergleichsstand muss **vor** observe() genommen werden: die
        # Beobachtung aendert selbst pid, session_id und transcript_path am
        # Task. Stand der Aufruf danach, waren diese Aenderungen schon in
        # `before` enthalten und wurden nie gespeichert. Beobachtet am
        # 2026-07-31: Task 70db0af7 zeigte auch nach dem Neustart weiter auf
        # das Transkript einer fremden Session, obwohl resolve_transcript
        # laengst den richtigen Pfad lieferte — jeder Tick korrigierte ihn
        # brav und verwarf die Korrektur beim Zurueckschreiben.
        before = _gemerkte_felder(task)

        obs = detector.observe(task, self.agents, now=now, exit_code=exit_code)
        stalled = detector.is_stalled(task, obs)

        classification = classify(obs.events, obs.tail_text, exit_code,
                                  stalled=stalled, now=now)
        decision = decide(task, obs, classification, stalled=stalled,
                          now=now, budget_available=budget)

        log.debug("beobachtung", extra={
            "task": task.id, "alive": obs.alive, "progressed": obs.progressed,
            "idle_s": round(obs.idle_seconds, 1), "agent_status": obs.agent_status,
            "class": classification.error_class.value, "detail": classification.detail,
        })

        task = self.engine.execute(task, decision, obs, now=now)
        if obs.progressed:
            task.transcript_size = obs.transcript_size
            task.last_progress_at = now
        after = _gemerkte_felder(task)
        if before != after or obs.progressed:
            self.registry.update(task)

    # ----------------------------------------------------------------- Loop

    def run(self) -> int:
        self.install_signal_handlers()
        log.info("Watchdog gestartet", extra={
            "pid": os.getpid(), "dry_run": self.dry_run,
            "poll": config.POLL_INTERVAL, "stall": config.STALL_SECONDS,
            "claude": config.CLAUDE_BIN,
        })
        self.adopt()
        while self._running:
            start = time.time()
            try:
                self.tick(start)
            except Exception:
                log.exception("Fehler im Tick")
            elapsed = time.time() - start
            sleep_for = max(1.0, config.POLL_INTERVAL - elapsed)
            deadline = time.time() + sleep_for
            while self._running and time.time() < deadline:
                time.sleep(min(1.0, deadline - time.time()))
        self.engine.terminate_all()
        log.info("Watchdog beendet")
        return 0


def main(dry_run: bool = False, verbose: bool = False,
         to_console: bool = True) -> int:
    logging_setup.setup(verbose=verbose, to_console=to_console)
    try:
        with SingleInstance():
            return Watchdog(dry_run=dry_run).run()
    except RuntimeError as exc:
        log.error(str(exc))
        return 1
