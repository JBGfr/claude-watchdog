"""SQLite-Persistenz fuer Tasks, Session-Locks und das Restart-Budget.

Der State muss einen Neustart des Watchdogs ueberleben, deshalb liegt alles
in einer Datei und nichts nur im Speicher.
"""

from __future__ import annotations

import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from typing import Iterator, Optional

from . import config
from .models import Mode, Status, Task

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id                TEXT PRIMARY KEY,
    title             TEXT NOT NULL,
    original_prompt   TEXT NOT NULL DEFAULT '',
    cwd               TEXT NOT NULL,
    session_id        TEXT,
    mode              TEXT NOT NULL,
    status            TEXT NOT NULL,
    pid               INTEGER,
    transcript_path   TEXT,
    attempts          INTEGER NOT NULL DEFAULT 0,
    max_attempts      INTEGER NOT NULL DEFAULT 5,
    no_auto_resume    INTEGER NOT NULL DEFAULT 0,
    model             TEXT,
    permission_mode   TEXT,
    max_budget_usd    REAL,
    last_error_class  TEXT,
    last_error_text   TEXT,
    created_at        REAL NOT NULL,
    updated_at        REAL NOT NULL,
    last_progress_at  REAL,
    next_retry_at     REAL,
    mute_until        REAL,
    cost_usd_spent    REAL NOT NULL DEFAULT 0,
    transcript_size   INTEGER NOT NULL DEFAULT 0,
    last_resume_marker TEXT,
    same_marker_count INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_session ON tasks(session_id)
    WHERE session_id IS NOT NULL;

-- Lock pro session_id: verhindert, dass ein Task doppelt gestartet wird.
CREATE TABLE IF NOT EXISTS locks (
    session_id  TEXT PRIMARY KEY,
    task_id     TEXT NOT NULL,
    pid         INTEGER NOT NULL,
    acquired_at REAL NOT NULL
);

-- Globales Restart-Budget (ein Eintrag pro Neustart).
CREATE TABLE IF NOT EXISTS restarts (
    ts      REAL NOT NULL,
    task_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_restarts_ts ON restarts(ts);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

_TASK_COLUMNS = [
    "id", "title", "original_prompt", "cwd", "session_id", "mode", "status",
    "pid", "transcript_path", "attempts", "max_attempts", "no_auto_resume",
    "model", "permission_mode", "max_budget_usd", "last_error_class",
    "last_error_text", "created_at", "updated_at", "last_progress_at",
    "next_retry_at", "mute_until", "cost_usd_spent", "transcript_size",
    "last_resume_marker", "same_marker_count",
]

#: Spalten, die erst nach dem urspruenglichen Schema dazugekommen sind.
#: `CREATE TABLE IF NOT EXISTS` laesst eine vorhandene Tabelle unangetastet —
#: ohne dieses Nachruesten wuerde jede bestehende Datenbank beim naechsten
#: Zugriff an der fehlenden Spalte scheitern.
_NACHGEREICHT: tuple[tuple[str, str], ...] = (
    ("mute_until", "REAL"),
)


def _pid_alive(pid: Optional[int]) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class Registry:
    def __init__(self, db_path=None):
        config.ensure_dirs()
        self.path = str(db_path or config.STATE_DB)
        self._conn = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)
        self._nachruesten()
        self._conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )

    def _nachruesten(self) -> None:
        """Fehlende Spalten einer aelteren Datenbank ergaenzen."""
        vorhanden = {r["name"] for r in
                     self._conn.execute("PRAGMA table_info(tasks)")}
        for name, typ in _NACHGEREICHT:
            if name not in vorhanden:
                self._conn.execute(
                    "ALTER TABLE tasks ADD COLUMN %s %s" % (name, typ))

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield self._conn
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        else:
            self._conn.execute("COMMIT")

    # ---------------------------------------------------------------- tasks

    def new_id(self) -> str:
        return uuid.uuid4().hex[:8]

    def add(self, task: Task) -> Task:
        row = task.to_row()
        cols = ", ".join(_TASK_COLUMNS)
        placeholders = ", ".join(f":{c}" for c in _TASK_COLUMNS)
        with self._tx() as conn:
            conn.execute(f"INSERT INTO tasks ({cols}) VALUES ({placeholders})", row)
        return task

    def update(self, task: Task) -> None:
        task.updated_at = time.time()
        row = task.to_row()
        assignments = ", ".join(f"{c} = :{c}" for c in _TASK_COLUMNS if c != "id")
        with self._tx() as conn:
            conn.execute(f"UPDATE tasks SET {assignments} WHERE id = :id", row)

    def get(self, task_id: str) -> Optional[Task]:
        cur = self._conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
        row = cur.fetchone()
        return Task.from_row(row) if row else None

    def get_by_session(self, session_id: str) -> Optional[Task]:
        cur = self._conn.execute("SELECT * FROM tasks WHERE session_id = ?", (session_id,))
        row = cur.fetchone()
        return Task.from_row(row) if row else None

    def find(self, ref: str) -> Optional[Task]:
        """Sucht per Task-ID, Session-ID oder eindeutigem Praefix."""
        task = self.get(ref) or self.get_by_session(ref)
        if task:
            return task
        cur = self._conn.execute(
            "SELECT * FROM tasks WHERE id LIKE ? OR session_id LIKE ?",
            (f"{ref}%", f"{ref}%"),
        )
        rows = cur.fetchall()
        return Task.from_row(rows[0]) if len(rows) == 1 else None

    def list(self, include_terminal: bool = True) -> list[Task]:
        sql = "SELECT * FROM tasks"
        params: tuple = ()
        if not include_terminal:
            sql += " WHERE status NOT IN (?, ?)"
            params = (Status.DONE.value, Status.FAILED.value)
        sql += " ORDER BY created_at ASC"
        return [Task.from_row(r) for r in self._conn.execute(sql, params)]

    def active(self) -> list[Task]:
        """Tasks, um die sich der Daemon kuemmern muss."""
        cur = self._conn.execute(
            "SELECT * FROM tasks WHERE status NOT IN (?, ?, ?) ORDER BY created_at ASC",
            (Status.DONE.value, Status.FAILED.value, Status.PAUSED.value),
        )
        return [Task.from_row(r) for r in cur]

    def terminal_before(self, cutoff: float) -> list[Task]:
        """Abgeschlossene/gescheiterte Tasks, die seit `cutoff` unberuehrt sind.

        Laufende, wartende und pausierte Tasks bleiben unabhaengig vom Alter
        unangetastet - ein Task, der auf ein Usage-Limit wartet, kann aelter
        sein als die Schonfrist.
        """
        cur = self._conn.execute(
            "SELECT * FROM tasks WHERE status IN (?, ?) AND updated_at < ? "
            "ORDER BY updated_at ASC",
            (Status.DONE.value, Status.FAILED.value, cutoff),
        )
        return [Task.from_row(r) for r in cur]

    def delete(self, task_id: str) -> bool:
        with self._tx() as conn:
            cur = conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
            conn.execute("DELETE FROM locks WHERE task_id = ?", (task_id,))
        return cur.rowcount > 0

    # ---------------------------------------------------------------- locks

    def acquire_lock(self, session_id: str, task_id: str) -> bool:
        """Session-Lock holen. False, wenn ein anderer Halter noch lebt."""
        now = time.time()
        with self._tx() as conn:
            cur = conn.execute("SELECT * FROM locks WHERE session_id = ?", (session_id,))
            row = cur.fetchone()
            if row is not None:
                if row["task_id"] == task_id and _pid_alive(row["pid"]):
                    return True
                if _pid_alive(row["pid"]):
                    return False
                # verwaister Lock eines toten Prozesses -> aufraeumen
                conn.execute("DELETE FROM locks WHERE session_id = ?", (session_id,))
            conn.execute(
                "INSERT INTO locks(session_id, task_id, pid, acquired_at) VALUES (?,?,?,?)",
                (session_id, task_id, os.getpid(), now),
            )
        return True

    def retarget_lock(self, session_id: str, task_id: str, pid: int) -> bool:
        """Den Lock auf einen anderen Prozess umschreiben.

        Ein Lock gilt genau so lange, wie die eingetragene PID lebt — das ist
        die Absicht, damit ein Absturz keine Dauersperre hinterlaesst.

        Fuer den Daemon passt `os.getpid()`: er laeuft weiter, solange der
        gestartete Lauf laeuft. Fuer `claude-watchdog reply` passt es nicht.
        Dort ist es die PID des CLI-Aufrufs, und der endet unmittelbar nach
        dem Start des Antwort-Laufs. Der Lock gilt damit sofort als verwaist,
        und ein zweiter reply-Aufruf bekommt ihn anstandslos — zwei
        `claude -r` auf derselben Session zugleich.

        Nach dem Start wird der Lock deshalb auf das Kind umgeschrieben, das
        die Arbeit tatsaechlich macht. False, wenn dieser Task ihn gar nicht
        (mehr) haelt.
        """
        with self._tx() as conn:
            cur = conn.execute(
                "UPDATE locks SET pid = ? WHERE session_id = ? AND task_id = ?",
                (pid, session_id, task_id),
            )
        return cur.rowcount > 0

    def release_lock(self, session_id: str) -> None:
        with self._tx() as conn:
            conn.execute("DELETE FROM locks WHERE session_id = ?", (session_id,))

    def reap_stale_locks(self) -> int:
        removed = 0
        with self._tx() as conn:
            for row in conn.execute("SELECT session_id, pid FROM locks").fetchall():
                if not _pid_alive(row["pid"]):
                    conn.execute("DELETE FROM locks WHERE session_id = ?",
                                 (row["session_id"],))
                    removed += 1
        return removed

    # ------------------------------------------------------- restart budget

    def restarts_last_hour(self) -> int:
        cutoff = time.time() - 3600
        cur = self._conn.execute("SELECT COUNT(*) FROM restarts WHERE ts >= ?", (cutoff,))
        return int(cur.fetchone()[0])

    def record_restart(self, task_id: str) -> None:
        with self._tx() as conn:
            conn.execute("INSERT INTO restarts(ts, task_id) VALUES (?, ?)",
                         (time.time(), task_id))
            conn.execute("DELETE FROM restarts WHERE ts < ?", (time.time() - 86400,))

    def restart_budget_available(self) -> bool:
        return self.restarts_last_hour() < config.MAX_RESTARTS_PER_HOUR


def make_task(
    *,
    registry: Registry,
    title: str,
    cwd: str,
    mode: Mode,
    prompt: str = "",
    session_id: Optional[str] = None,
    model: Optional[str] = None,
    permission_mode: Optional[str] = None,
    max_attempts: Optional[int] = None,
    max_budget_usd: Optional[float] = None,
    no_auto_resume: bool = False,
    status: Status = Status.PENDING,
) -> Task:
    return Task(
        id=registry.new_id(),
        title=title,
        cwd=str(cwd),
        mode=mode,
        status=status,
        original_prompt=prompt,
        session_id=session_id,
        model=model,
        permission_mode=permission_mode,
        max_attempts=max_attempts if max_attempts is not None else config.DEFAULT_MAX_ATTEMPTS,
        max_budget_usd=max_budget_usd,
        no_auto_resume=no_auto_resume,
        transcript_path=str(config.transcript_path(cwd, session_id)) if session_id else None,
    )
