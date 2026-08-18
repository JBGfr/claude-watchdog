"""Tests fuers Aufraeumen abgeschlossener Tasks."""

from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from claude_watchdog import config  # noqa: E402
from claude_watchdog.daemon import Watchdog  # noqa: E402
from claude_watchdog.models import Mode, Status, Task  # noqa: E402
from claude_watchdog.registry import Registry  # noqa: E402

TAG = 86400.0


class Basis(unittest.TestCase):
    """Jeder Test bekommt eine eigene Datenbank und ein eigenes runs/."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.reg = Registry(db_path=base / "state.db")
        self._runs = config.RUNS_DIR
        self._tage = config.RETENTION_DAYS
        self._intervall = config.CLEANUP_INTERVAL
        config.RUNS_DIR = base / "runs"
        config.RUNS_DIR.mkdir()
        config.RETENTION_DAYS = 14
        config.CLEANUP_INTERVAL = 3600
        self.now = 1_800_000_000.0

    def tearDown(self):
        self.reg.close()
        config.RUNS_DIR = self._runs
        config.RETENTION_DAYS = self._tage
        config.CLEANUP_INTERVAL = self._intervall
        self.tmp.cleanup()

    def anlegen(self, task_id: str, status: Status, alter_tage: float,
                mode: Mode = Mode.OBSERVED) -> Task:
        task = Task(id=task_id, title=f"t-{task_id}", cwd="/tmp", mode=mode,
                    status=status, session_id=f"s-{task_id}")
        self.reg.add(task)
        # updated_at direkt setzen: reg.update() wuerde es auf jetzt ziehen.
        self.reg._conn.execute("UPDATE tasks SET updated_at = ? WHERE id = ?",
                               (self.now - alter_tage * TAG, task_id))
        return task

    def ids(self) -> set[str]:
        return {t.id for t in self.reg.list(include_terminal=True)}


class TestAuswahl(Basis):
    """terminal_before(): was faellt ueberhaupt in die Auswahl?"""

    def test_nur_alte_terminale_tasks(self):
        self.anlegen("alt_done", Status.DONE, 30)
        self.anlegen("alt_failed", Status.FAILED, 30)
        self.anlegen("neu_done", Status.DONE, 1)
        self.anlegen("alt_laeuft", Status.RUNNING, 30)
        self.anlegen("alt_wartet", Status.WAITING_FOR_LIMIT, 30)
        self.anlegen("alt_pausiert", Status.PAUSED, 30)

        treffer = {t.id for t in self.reg.terminal_before(self.now - 14 * TAG)}
        self.assertEqual(treffer, {"alt_done", "alt_failed"})

    def test_wartender_task_wird_nie_erfasst(self):
        # Ein Task, der auf ein Usage-Limit wartet, darf beliebig alt sein.
        self.anlegen("wartet", Status.WAITING_FOR_LIMIT, 400)
        self.assertEqual(self.reg.terminal_before(self.now), [])


class TestAufraeumen(Basis):
    def wd(self) -> Watchdog:
        w = Watchdog(registry=self.reg, dry_run=True)
        return w

    def test_alte_tasks_verschwinden_neue_bleiben(self):
        self.anlegen("alt", Status.DONE, 30)
        self.anlegen("neu", Status.DONE, 2)
        self.assertEqual(self.wd().cleanup(self.now), 1)
        self.assertEqual(self.ids(), {"neu"})

    def test_run_logs_verschwinden_mit(self):
        self.anlegen("alt", Status.DONE, 30, mode=Mode.MANAGED)
        laufdir = config.run_dir("alt")
        laufdir.mkdir(parents=True)
        (laufdir / "attempt-001.jsonl").write_text("{}", encoding="utf-8")

        self.wd().cleanup(self.now)
        self.assertFalse(laufdir.exists())

    def test_fremde_verzeichnisse_bleiben_unangetastet(self):
        # Run-Logs eines Tasks, der gar nicht entfernt wird.
        self.anlegen("alt", Status.DONE, 30)
        fremd = config.run_dir("jemand_anders")
        fremd.mkdir(parents=True)
        self.wd().cleanup(self.now)
        self.assertTrue(fremd.exists())

    def test_abgeschaltet_passiert_nichts(self):
        config.RETENTION_DAYS = 0
        self.anlegen("uralt", Status.DONE, 999)
        self.assertEqual(self.wd().cleanup(self.now), 0)
        self.assertEqual(self.ids(), {"uralt"})

    def test_laeuft_nicht_bei_jedem_durchlauf(self):
        self.anlegen("alt", Status.DONE, 30)
        w = self.wd()
        self.assertEqual(w.cleanup(self.now), 1)
        self.anlegen("alt2", Status.DONE, 30)
        # Direkt danach: Intervall noch nicht um.
        self.assertEqual(w.cleanup(self.now + 60), 0)
        self.assertIn("alt2", self.ids())
        # Eine Stunde spaeter schon.
        self.assertEqual(w.cleanup(self.now + 3601), 1)

    def test_schonfrist_ist_einstellbar(self):
        config.RETENTION_DAYS = 60
        self.anlegen("dreissig_tage", Status.DONE, 30)
        self.assertEqual(self.wd().cleanup(self.now), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
