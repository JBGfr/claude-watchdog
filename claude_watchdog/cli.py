"""Kommandozeilen-Frontend."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from . import config, daemon, detector, logging_setup, recovery, transcript
from .models import Mode, Status, Task
from .registry import Registry, make_task

PROG = "claude-watchdog"


# --------------------------------------------------------------------------
# Darstellung
# --------------------------------------------------------------------------

def _age(ts: Optional[float]) -> str:
    if not ts:
        return "-"
    delta = max(0, int(time.time() - ts))
    if delta < 60:
        return f"{delta}s"
    if delta < 3600:
        return f"{delta // 60}m"
    if delta < 86400:
        return f"{delta // 3600}h"
    return f"{delta // 86400}d"


def _until(ts: Optional[float]) -> str:
    if not ts:
        return "-"
    delta = int(ts - time.time())
    if delta <= 0:
        return "jetzt"
    if delta < 60:
        return f"in {delta}s"
    if delta < 3600:
        return f"in {delta // 60}m"
    return f"in {delta // 3600}h{(delta % 3600) // 60:02d}m"


def _print_table(tasks: list[Task]) -> None:
    if not tasks:
        print("Keine Tasks. Anlegen mit: claude-watchdog add \"<prompt>\"")
        return
    headers = ["ID", "MODE", "STATUS", "TITEL", "VERS.", "NEXT", "FEHLER", "$"]
    rows = []
    for t in tasks:
        rows.append([
            t.id,
            t.mode.value,
            t.status.value,
            (t.title[:32] + "...") if len(t.title) > 35 else t.title,
            f"{t.attempts}/{t.max_attempts}",
            _until(t.next_retry_at),
            (t.last_error_class or "-")[:14],
            f"{t.cost_usd_spent:.3f}" if t.cost_usd_spent else "-",
        ])
    widths = [max(len(str(r[i])) for r in ([headers] + rows)) for i in range(len(headers))]
    line = "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))
    print(line)
    print("-" * len(line))
    for r in rows:
        print("  ".join(str(c).ljust(widths[i]) for i, c in enumerate(r)))


# --------------------------------------------------------------------------
# Kommandos
# --------------------------------------------------------------------------

def cmd_add(args: argparse.Namespace, reg: Registry) -> int:
    cwd = str(Path(args.cwd or os.getcwd()).resolve())
    if not Path(cwd).is_dir():
        print(f"Arbeitsverzeichnis existiert nicht: {cwd}", file=sys.stderr)
        return 2
    title = args.title or (args.prompt[:60] + ("..." if len(args.prompt) > 60 else ""))
    task = make_task(
        registry=reg, title=title, cwd=cwd, mode=Mode.MANAGED,
        prompt=args.prompt, model=args.model,
        permission_mode=args.permission_mode,
        max_attempts=args.max_attempts, max_budget_usd=args.max_budget_usd,
        no_auto_resume=args.no_auto_resume, status=Status.PENDING,
    )
    reg.add(task)
    print(f"Task {task.id} angelegt (managed, {cwd}).")
    if args.no_auto_resume:
        print("  Hinweis: no_auto_resume -> wird nur gemeldet, nie fortgesetzt.")
    print("  Der Daemon startet ihn beim naechsten Durchlauf.")
    return 0


def cmd_attach(args: argparse.Namespace, reg: Registry) -> int:
    session_id = args.session_id
    existing = reg.get_by_session(session_id)
    if existing:
        print(f"Session ist bereits als Task {existing.id} erfasst.")
        return 0

    agents = detector.AgentsSnapshot()
    entry = agents.get(session_id)
    cwd = args.cwd
    pid = None
    if entry:
        cwd = cwd or entry.get("cwd")
        pid = entry.get("pid")
    if not cwd:
        # Transkript in allen bekannten Projektverzeichnissen suchen.
        for directory in sorted(config.CLAUDE_PROJECTS_DIR.glob("*")):
            if (directory / f"{session_id}.jsonl").exists():
                cwd = "/" + directory.name.lstrip("-").replace("-", "/")
                break
    if not cwd:
        print(f"Session {session_id} nicht gefunden. Laufende Sessions:",
              file=sys.stderr)
        for sid, e in agents.all().items():
            print(f"  {sid}  {e.get('cwd')}  {e.get('status')}", file=sys.stderr)
        return 2

    tpath = config.transcript_path(cwd, session_id)
    task = make_task(
        registry=reg, title=args.title or (entry or {}).get("name") or f"session {session_id[:8]}",
        cwd=cwd, mode=Mode.OBSERVED, session_id=session_id,
        status=Status.RUNNING if entry else Status.STALLED,
    )
    task.pid = pid
    task.transcript_path = str(tpath)
    task.transcript_size = transcript.file_size(tpath)
    task.last_progress_at = transcript.file_mtime(tpath) or time.time()
    reg.add(task)
    print(f"Task {task.id} angelegt (observed, {cwd}).")
    print("  observed = der Watchdog beobachtet und meldet, greift aber nicht ein.")
    return 0


def cmd_list(args: argparse.Namespace, reg: Registry) -> int:
    tasks = reg.list(include_terminal=args.all)
    if args.json:
        print(json.dumps([t.to_row() for t in tasks], indent=2, default=str))
        return 0
    _print_table(tasks)
    if not args.all:
        print("\n(abgeschlossene/gescheiterte Tasks mit --all anzeigen)")
    return 0


def cmd_logs(args: argparse.Namespace, reg: Registry) -> int:
    if args.task:
        task = reg.find(args.task)
        if not task:
            print(f"Task '{args.task}' nicht gefunden.", file=sys.stderr)
            return 2
        run_log, run_err = config.latest_run_files(task.id)
        print(f"# Task {task.id} - {task.title}")
        print(f"# status={task.status.value} mode={task.mode.value} "
              f"attempts={task.attempts}/{task.max_attempts}")
        print(f"# session={task.session_id} cwd={task.cwd}")
        print(f"# transkript={task.transcript_path}")
        if task.last_error_text:
            print(f"# letzter Fehler: {task.last_error_class} - {task.last_error_text}")
        if run_log:
            print(f"\n--- {run_log} (letzte {args.lines} Events) ---")
            for ev in transcript.tail_events(run_log, max_events=args.lines):
                subtype = ev.get("subtype")
                print(f"{ev.get('type')}{'/' + str(subtype) if subtype else ''}")
        if run_err:
            text = transcript.tail_bytes(run_err, 4000).strip()
            if text:
                print(f"\n--- {run_err} ---\n{text}")
        # Watchdog-Entscheidungen zu diesem Task
        print(f"\n--- Entscheidungen aus {config.LOG_FILE} ---")
        _print_log_lines(task_id=task.id, lines=args.lines)
        return 0
    _print_log_lines(task_id=None, lines=args.lines)
    return 0


def _print_log_lines(task_id: Optional[str], lines: int) -> None:
    if not config.LOG_FILE.exists():
        print("(noch kein Log vorhanden)")
        return
    text = transcript.tail_bytes(config.LOG_FILE, 400_000)
    out = []
    for raw in text.split("\n"):
        raw = raw.strip()
        if not raw:
            continue
        try:
            rec = json.loads(raw)
        except ValueError:
            continue
        if task_id and rec.get("task") != task_id:
            continue
        extra = " ".join(
            f"{k}={v}" for k, v in rec.items()
            if k not in ("ts", "time", "level", "logger", "msg")
        )
        out.append(f"{rec.get('time')} {rec.get('level'):<7} {rec.get('msg')} {extra}")
    for entry in out[-lines:]:
        print(entry)


def cmd_pause(args: argparse.Namespace, reg: Registry) -> int:
    task = reg.find(args.task)
    if not task:
        print(f"Task '{args.task}' nicht gefunden.", file=sys.stderr)
        return 2
    task.status = Status.PAUSED
    task.next_retry_at = None
    reg.update(task)
    print(f"Task {task.id} pausiert (laufender Prozess wird nicht beendet).")
    return 0


def cmd_resume(args: argparse.Namespace, reg: Registry) -> int:
    task = reg.find(args.task)
    if not task:
        print(f"Task '{args.task}' nicht gefunden.", file=sys.stderr)
        return 2
    task.status = Status.PENDING if not task.session_id else Status.WAITING_FOR_LIMIT
    task.next_retry_at = time.time()
    if args.reset_attempts:
        task.attempts = 0
        task.same_marker_count = 0
    reg.update(task)
    print(f"Task {task.id} freigegeben (Status: {task.status.value}).")
    return 0


def cmd_reply(args: argparse.Namespace, reg: Registry) -> int:
    """Antwort an einen blockierten managed-Task senden.

    Der Watchdog beantwortet weiterhin nichts von sich aus — dieses Kommando
    liefert nur den Mechanismus; die inhaltliche Antwort kommt vom Aufrufer.
    """
    task = reg.find(args.task)
    if not task:
        print(f"Task '{args.task}' nicht gefunden.", file=sys.stderr)
        return 2
    if task.mode is not Mode.MANAGED:
        print("reply gibt es nur fuer managed-Tasks — observed-Sessions "
              "werden nie angefasst.", file=sys.stderr)
        return 3
    # BLOCKED: klassische Rueckfrage einer lebenden Session. DONE: ein
    # headless Lauf beendet seinen Turn auch dann regulaer, wenn er inhaltlich
    # eine Rueckfrage stellt (CEO-BLOCKIERT im Ergebnisblock) — die Antwort
    # setzt schlicht die Konversation fort.
    if task.status not in (Status.BLOCKED, Status.DONE) and not args.force:
        print(f"Task ist '{task.status.value}', nicht 'blocked' oder 'done'. "
              f"Mit --force trotzdem antworten.", file=sys.stderr)
        return 3
    if not task.session_id:
        print("Task hat keine Session-ID — nichts, woran sich anknuepfen "
              "laesst.", file=sys.stderr)
        return 3
    if not reg.acquire_lock(task.session_id, task.id):
        print("Session-Lock belegt — zu dieser Session laeuft schon ein "
              "Prozess.", file=sys.stderr)
        return 3

    attempt_no = recovery.next_attempt_no(task.id)
    run_dir = config.run_dir(task.id)
    run_dir.mkdir(parents=True, exist_ok=True)
    out_path = config.run_log(task.id, attempt_no)
    err_path = config.run_err(task.id, attempt_no)
    cmd = recovery.build_reply_command(task, args.text)

    try:
        with out_path.open("wb") as out_fh, err_path.open("wb") as err_fh:
            proc = subprocess.Popen(
                cmd, cwd=task.cwd, stdout=out_fh, stderr=err_fh,
                stdin=subprocess.DEVNULL, env=recovery.scrubbed_env(),
                start_new_session=True,
            )
    except (OSError, ValueError) as exc:
        reg.release_lock(task.session_id)
        print(f"Start fehlgeschlagen: {exc}", file=sys.stderr)
        return 4

    # Der Lock traegt bis hier die PID dieses CLI-Aufrufs — und der endet
    # gleich. Ohne das Umschreiben waere er sofort verwaist und ein zweiter
    # reply koennte einen weiteren Lauf auf dieselbe Session setzen.
    reg.retarget_lock(task.session_id, task.id, proc.pid)

    now = time.time()
    task.pid = proc.pid
    task.status = Status.RUNNING
    task.last_progress_at = now
    # Schonfrist: der Daemon laesst den Task in Ruhe, bis der Antwort-Lauf
    # erste Ausgaben schreibt — sonst wuerde der alte Tail sofort wieder
    # als AWAITING_INPUT klassifiziert.
    task.next_retry_at = now + config.REPLY_GRACE
    reg.update(task)
    # Ein Eingriff ist ein Eingriff: zaehlt gegen das globale Neustart-Budget.
    reg.record_restart(task.id)
    print(f"Antwort an {task.id} gestartet (PID {proc.pid}, {out_path.name}).")
    return 0


def cmd_rm(args: argparse.Namespace, reg: Registry) -> int:
    task = reg.find(args.task)
    if not task:
        print(f"Task '{args.task}' nicht gefunden.", file=sys.stderr)
        return 2
    if task.status is Status.RUNNING and not args.force:
        print(f"Task {task.id} laeuft noch. Mit --force trotzdem entfernen "
              f"(der Claude-Prozess laeuft weiter).", file=sys.stderr)
        return 3
    reg.delete(task.id)
    # Die Run-Logs muessen mit: nach dem Loeschen aus der Registry kommt
    # niemand mehr an sie heran. `cleanup()` raeumt zwar Protokolle weg,
    # laeuft dafuer aber ueber Tasks, die es noch gibt — ein von Hand
    # entfernter Task hinterliess seine Logs dauerhaft als Waisen.
    daemon.drop_run_dir(task.id)
    print(f"Task {task.id} entfernt.")
    return 0


def cmd_run(args: argparse.Namespace, reg: Registry) -> int:
    reg.close()
    return daemon.main(dry_run=args.dry_run, verbose=args.verbose,
                       to_console=not args.quiet)


def cmd_status(args: argparse.Namespace, reg: Registry) -> int:
    tasks = reg.list(include_terminal=True)
    active = [t for t in tasks if not t.status.is_terminal]
    print(f"Watchdog-Verzeichnis : {config.BASE_DIR}")
    print(f"Kill-Switch          : "
          f"{'AKTIV (' + str(config.STOP_FILE) + ')' if config.stop_requested() else 'inaktiv'}")
    print(f"Neustarts (1h)       : {reg.restarts_last_hour()}/{config.MAX_RESTARTS_PER_HOUR}")
    print(f"Tasks                : {len(active)} aktiv, {len(tasks)} gesamt")
    print()
    _print_table(active or tasks)
    return 0


# --------------------------------------------------------------------------
# Parser
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=PROG,
        description="Supervisor fuer Claude-Code-Sessions: ueberwacht laufende "
                    "Tasks und setzt sie nach einer Unterbrechung fort.",
    )
    p.add_argument("--verbose", action="store_true", help="ausfuehrliches Logging")
    sub = p.add_subparsers(dest="command", required=True)

    a = sub.add_parser("add", help="managed-Task anlegen (wird vom Daemon gestartet)")
    a.add_argument("prompt", help="Auftrag fuer Claude")
    a.add_argument("--cwd", help="Arbeitsverzeichnis (Default: aktuelles)")
    a.add_argument("--title", help="Anzeigename")
    a.add_argument("--model", help="z.B. opus, sonnet, claude-haiku-4-5")
    a.add_argument("--permission-mode", dest="permission_mode",
                   choices=["acceptEdits", "auto", "bypassPermissions", "manual",
                            "dontAsk", "plan"],
                   help="Permission-Modus; der Watchdog setzt von sich aus keinen")
    a.add_argument("--max-attempts", dest="max_attempts", type=int)
    a.add_argument("--max-budget-usd", dest="max_budget_usd", type=float)
    a.add_argument("--no-auto-resume", dest="no_auto_resume", action="store_true",
                   help="nur melden, nie automatisch fortsetzen")
    a.set_defaults(func=cmd_add)

    at = sub.add_parser("attach", help="laufende Session als observed aufnehmen")
    at.add_argument("session_id")
    at.add_argument("--cwd", help="Arbeitsverzeichnis der Session")
    at.add_argument("--title")
    at.set_defaults(func=cmd_attach)

    for name, helptext in (("list", "Tasks auflisten"), ("status", "Uebersicht")):
        s = sub.add_parser(name, help=helptext)
        s.add_argument("--all", action="store_true", help="auch abgeschlossene")
        s.add_argument("--json", action="store_true")
        s.set_defaults(func=cmd_list if name == "list" else cmd_status)

    lg = sub.add_parser("logs", help="Entscheidungen und Run-Logs anzeigen")
    lg.add_argument("task", nargs="?", help="Task-ID oder Session-ID")
    lg.add_argument("-n", "--lines", type=int, default=40)
    lg.set_defaults(func=cmd_logs)

    ps = sub.add_parser("pause", help="Task aus der Ueberwachung nehmen")
    ps.add_argument("task")
    ps.set_defaults(func=cmd_pause)

    rs = sub.add_parser("resume", help="pausierten Task wieder freigeben")
    rs.add_argument("task")
    rs.add_argument("--reset-attempts", action="store_true")
    rs.set_defaults(func=cmd_resume)

    rp = sub.add_parser("reply", help="Antwort an einen blockierten managed-Task senden")
    rp.add_argument("task", help="Task-ID oder Session-ID")
    rp.add_argument("text", help="die Antwort (haengt einen Turn ans Transkript an)")
    rp.add_argument("--force", action="store_true",
                    help="auch senden, wenn der Task nicht 'blocked' ist")
    rp.set_defaults(func=cmd_reply)

    rm = sub.add_parser("rm", help="Task entfernen")
    rm.add_argument("task")
    rm.add_argument("--force", action="store_true")
    rm.set_defaults(func=cmd_rm)

    rn = sub.add_parser("run", help="Daemon im Vordergrund starten (fuer systemd)")
    rn.add_argument("--dry-run", action="store_true",
                    help="alles entscheiden und loggen, aber nichts ausfuehren")
    rn.add_argument("--quiet", action="store_true", help="keine Konsolenausgabe")
    # SUPPRESS: wird der Schalter hier nicht gesetzt, bleibt der Wert der
    # globalen Option erhalten (sonst wuerde der Default ihn ueberschreiben).
    rn.add_argument("--verbose", action="store_true", default=argparse.SUPPRESS,
                    help="ausfuehrliches Logging")
    rn.set_defaults(func=cmd_run)

    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command != "run":
        logging_setup.setup(verbose=args.verbose, to_console=False)
    reg = Registry()
    try:
        return args.func(args, reg)
    finally:
        try:
            reg.close()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
