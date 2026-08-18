"""Tests fuer das reply-Kommando (Antwort an blockierte managed-Tasks).

Kein echter Subprozess: der Spawn wird gemockt, alles andere laeuft gegen
eine eigene Datenbank und ein eigenes runs/-Verzeichnis.
"""

from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from claude_watchdog import cli, config, recovery  # noqa: E402
from claude_watchdog.models import Mode, Status  # noqa: E402
from claude_watchdog.registry import Registry, make_task  # noqa: E402

SID = "11111111-2222-3333-4444-555555555555"


class Basis(unittest.TestCase):
    """Eigene DB + eigenes runs/ pro Test, wie in test_cleanup."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.reg = Registry(db_path=base / "state.db")
        self._runs = config.RUNS_DIR
        config.RUNS_DIR = base / "runs"
        config.RUNS_DIR.mkdir()

    def tearDown(self):
        self.reg.close()
        config.RUNS_DIR = self._runs
        self.tmp.cleanup()

    def blocked_task(self, **kw):
        kw.setdefault("mode", Mode.MANAGED)
        kw.setdefault("status", Status.BLOCKED)
        kw.setdefault("session_id", SID)
        task = make_task(registry=self.reg, title="t", cwd="/tmp", **kw)
        self.reg.add(task)
        return task

    def args(self, *argv):
        return cli.build_parser().parse_args(["reply", *argv])

    def reply(self, args):
        """cmd_reply mit stummgeschalteten Ausgaben (Konvention der Suite)."""
        with contextlib.redirect_stdout(io.StringIO()), \
             contextlib.redirect_stderr(io.StringIO()):
            return cli.cmd_reply(args, self.reg)


class TestBuildReplyCommand(unittest.TestCase):
    def test_basis(self):
        task = mock.Mock(session_id=SID, model=None, permission_mode=None,
                         max_budget_usd=None)
        cmd = recovery.build_reply_command(task, "Variante A")
        self.assertEqual(cmd[:5], [config.CLAUDE_BIN, "-p", "Variante A", "-r", SID])
        self.assertIn("--output-format", cmd)
        self.assertIn("--verbose", cmd)
        self.assertNotIn("--model", cmd)

    def test_optionale_flags(self):
        task = mock.Mock(session_id=SID, model="sonnet",
                         permission_mode="acceptEdits", max_budget_usd=5.0)
        cmd = recovery.build_reply_command(task, "x")
        self.assertIn("sonnet", cmd)
        self.assertIn("acceptEdits", cmd)
        self.assertIn("5.0", cmd)


class TestNextAttemptNo(Basis):
    def test_leer_beginnt_bei_eins(self):
        self.assertEqual(recovery.next_attempt_no("kein-task"), 1)

    def test_zaehlt_nach_hoechster_datei(self):
        d = config.run_dir("abc")
        d.mkdir(parents=True)
        (d / "attempt-001.jsonl").touch()
        (d / "attempt-007.jsonl").touch()
        (d / "attempt-quatsch.jsonl").touch()  # wird ignoriert
        self.assertEqual(recovery.next_attempt_no("abc"), 8)


class TestReplyVerweigert(Basis):
    def test_unbekannter_task(self):
        self.assertEqual(self.reply(self.args("gibtsnicht", "x")), 2)

    def test_observed_wird_nie_angefasst(self):
        task = self.blocked_task(mode=Mode.OBSERVED)
        self.assertEqual(self.reply(self.args(task.id, "x")), 3)

    def test_nicht_blocked_ohne_force(self):
        task = self.blocked_task(status=Status.RUNNING)
        self.assertEqual(self.reply(self.args(task.id, "x")), 3)

    def test_ohne_session_id(self):
        task = self.blocked_task(session_id=None)
        self.assertEqual(self.reply(self.args(task.id, "x")), 3)

    def test_fremder_lebendiger_lock(self):
        task = self.blocked_task()
        # Lock eines anderen Tasks, gehalten vom eigenen (lebendigen) Prozess.
        self.assertTrue(self.reg.acquire_lock(SID, "anderer-task"))
        self.assertEqual(self.reply(self.args(task.id, "x")), 3)


class TestReplySendet(Basis):
    def send(self, task, *extra):
        proc = mock.Mock(pid=4242)
        with mock.patch.object(cli.subprocess, "Popen",
                               return_value=proc) as popen:
            rc = self.reply(self.args(task.id, "Variante A", *extra))
        return rc, popen

    def test_erfolg_setzt_zustand_und_schonfrist(self):
        task = self.blocked_task()
        vorher = time.time()
        rc, popen = self.send(task)
        self.assertEqual(rc, 0)
        neu = self.reg.find(task.id)
        self.assertIs(neu.status, Status.RUNNING)
        self.assertEqual(neu.pid, 4242)
        self.assertGreaterEqual(neu.next_retry_at, vorher + config.REPLY_GRACE - 1)
        # Eingriff zaehlt gegen das globale Neustart-Budget.
        self.assertEqual(self.reg.restarts_last_hour(), 1)
        # Umgebung ist von Session-Variablen bereinigt.
        env = popen.call_args.kwargs["env"]
        self.assertNotIn("CLAUDECODE", env)

    def test_attempt_nummer_aus_verzeichnis(self):
        task = self.blocked_task()
        d = config.run_dir(task.id)
        d.mkdir(parents=True)
        (d / "attempt-002.jsonl").touch()
        rc, popen = self.send(task)
        self.assertEqual(rc, 0)
        self.assertTrue((d / "attempt-003.jsonl").exists())
        self.assertFalse((d / "attempt-002.err").exists())

    def test_force_erlaubt_nicht_blocked(self):
        task = self.blocked_task(status=Status.RUNNING)
        rc, _ = self.send(task, "--force")
        self.assertEqual(rc, 0)

    def test_done_ist_ohne_force_erlaubt(self):
        # Headless-Worker beenden ihren Turn regulaer, auch wenn sie inhaltlich
        # blockiert sind (CEO-BLOCKIERT im Ergebnisblock) — reply setzt fort.
        task = self.blocked_task(status=Status.DONE)
        rc, _ = self.send(task)
        self.assertEqual(rc, 0)
        self.assertIs(self.reg.find(task.id).status, Status.RUNNING)


if __name__ == "__main__":
    unittest.main()


class TestLockUeberlebtDenCliAufruf(Basis):
    """Der Lock muss den Antwort-Lauf schuetzen, nicht den CLI-Aufruf.

    Ein Lock gilt so lange, wie die eingetragene PID lebt. Traegt reply die
    PID des CLI-Prozesses ein, ist der Lock verwaist, sobald das Kommando
    zurueckkehrt — und ein zweiter reply setzt einen weiteren 'claude -r' auf
    dieselbe Session.
    """

    def test_retarget_schreibt_die_pid_um(self):
        reg = self.reg
        self.assertTrue(reg.acquire_lock("sess", "t1"))
        self.assertTrue(reg.retarget_lock("sess", "t1", 4242))
        with reg._tx() as conn:
            row = conn.execute(
                "SELECT pid FROM locks WHERE session_id = ?", ("sess",)).fetchone()
        self.assertEqual(row["pid"], 4242)

    def test_retarget_nur_fuer_den_haltenden_task(self):
        reg = self.reg
        reg.acquire_lock("sess", "t1")
        self.assertFalse(reg.retarget_lock("sess", "fremd", 4242))

    def test_lock_haelt_solange_das_kind_lebt(self):
        """Kernaussage: nach dem Umschreiben blockiert ein zweiter Aufruf."""
        import subprocess
        reg = self.reg
        kind = subprocess.Popen(["sleep", "30"])
        # kill allein hinterlaesst einen Zombie und eine ResourceWarning —
        # der Prozess muss auch abgeholt werden.
        self.addCleanup(kind.wait)
        self.addCleanup(kind.kill)
        reg.acquire_lock("sess", "t1")
        reg.retarget_lock("sess", "t1", kind.pid)
        self.assertFalse(reg.acquire_lock("sess", "t2"))

    def test_nach_dem_ende_des_kindes_wieder_frei(self):
        """Ein beendeter Lauf darf keine Dauersperre hinterlassen."""
        import subprocess
        reg = self.reg
        kind = subprocess.Popen(["true"])
        kind.wait()
        reg.acquire_lock("sess", "t1")
        reg.retarget_lock("sess", "t1", kind.pid)
        self.assertTrue(reg.acquire_lock("sess", "t2"))


class TestProtokollnummernKollidieren(Basis):
    """Antwort und Daemon-Start duerfen nicht dieselbe Protokolldatei nehmen.

    Der Daemon rechnete den Dateinamen aus task.attempts + 1, ein reply aus
    den vorhandenen Dateien — und ein reply erhoeht den Zaehler bewusst nicht.
    Nach einer Antwort zeigten beide auf dieselbe Nummer.
    """

    def test_nummer_waechst_ueber_ein_reply_hinweg(self):
        tid = "t1"
        d = config.run_dir(tid)
        d.mkdir(parents=True, exist_ok=True)
        for n in (1, 2):
            config.run_log(tid, n).write_text("{}\n")

        # So schreibt der reply-Pfad.
        r = recovery.next_attempt_no(tid)
        self.assertEqual(r, 3)
        config.run_log(tid, r).write_text('{"antwort": 1}\n')

        # Der naechste Daemon-Start darf jetzt NICHT wieder 3 nehmen.
        self.assertEqual(recovery.next_attempt_no(tid), 4)

    def test_antwort_bleibt_erhalten(self):
        tid = "t2"
        d = config.run_dir(tid)
        d.mkdir(parents=True, exist_ok=True)
        config.run_log(tid, 1).write_text("{}\n")
        r = recovery.next_attempt_no(tid)
        config.run_log(tid, r).write_text('{"antwort": "wichtig"}\n')
        naechste = recovery.next_attempt_no(tid)
        config.run_log(tid, naechste).write_text('{"lauf": "danach"}\n')
        self.assertNotEqual(r, naechste)
        self.assertIn("wichtig", config.run_log(tid, r).read_text())

    def test_fuehrende_nullen_werden_gelesen(self):
        """Die Dateien heissen attempt-007.jsonl — int('007') muss klappen."""
        tid = "t3"
        d = config.run_dir(tid)
        d.mkdir(parents=True, exist_ok=True)
        config.run_log(tid, 7).write_text("{}\n")
        self.assertEqual(recovery.next_attempt_no(tid), 8)
