"""Startweg 'service': Laeufe entkommen der Netzsperre des Daemons."""

from __future__ import annotations

import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from claude_watchdog import config, recovery  # noqa: E402


class KommandoTest(unittest.TestCase):
    """Die Kommandozeile muss den Manager starten lassen, nicht den Daemon."""

    def setUp(self) -> None:
        self.argv = recovery.dienst_kommando(
            cmd=["/pfad/claude", "-p", "auftrag"],
            task_id="abc123", unit="claude-watchdog-abc123-001",
            cwd="/home/user/Projekt",
            out_pfad=Path("/runs/out.jsonl"), err_pfad=Path("/runs/out.err"),
            rc_pfad=Path("/runs/out.rc"), env={"PATH": "/usr/bin"})

    def test_startet_als_dienst_nicht_als_scope(self) -> None:
        """--scope waere ein Kind des Daemons und erbte dessen Netzsperre."""
        self.assertIn("systemd-run", self.argv[0])
        self.assertIn("--user", self.argv)
        self.assertNotIn("--scope", self.argv)

    def test_unit_und_arbeitsverzeichnis(self) -> None:
        self.assertIn("--unit", self.argv)
        self.assertEqual("claude-watchdog-abc123-001",
                         self.argv[self.argv.index("--unit") + 1])
        self.assertIn("--property=WorkingDirectory=/home/user/Projekt", self.argv)

    def test_speichergrenzen_bleiben(self) -> None:
        for grenze in recovery.SCOPE_LIMITS:
            self.assertIn(grenze, self.argv)

    def test_umgebung_wird_weitergereicht(self) -> None:
        """Ein Dienst erbt die Umgebung des Daemons nicht - sie muss mit."""
        self.assertIn("--setenv=PATH=/usr/bin", self.argv)

    def test_wrapper_bekommt_seine_drei_pfade_und_dann_den_befehl(self) -> None:
        trenner = self.argv.index("--")
        rest = self.argv[trenner + 1:]
        self.assertEqual(["/bin/sh", "-c", recovery.DIENST_WRAPPER, "_",
                          "/runs/out.rc", "/runs/out.jsonl", "/runs/out.err",
                          "/pfad/claude", "-p", "auftrag"],
                         rest)

    def test_wrapper_haelt_den_rueckgabewert_fest(self) -> None:
        self.assertIn('printf %s "$?" > "$rc"', recovery.DIENST_WRAPPER)

    def test_wrapper_enthaelt_kein_doppeltes_dollarzeichen(self) -> None:
        """systemd liest die Kommandozeile selbst und macht aus $$ ein $.

        Am 2026-08-17 stand deshalb woertlich "$" in der PID-Datei. Wer hier
        wieder $$ hineinschreibt, bekommt denselben stillen Fehler.
        """
        self.assertNotIn("$$", recovery.DIENST_WRAPPER)
        self.assertNotIn("$$", " ".join(self.argv))


class UmgebungTest(unittest.TestCase):

    def test_systemd_eigene_variablen_bleiben_draussen(self) -> None:
        argumente = recovery.dienst_umgebung({
            "PATH": "/usr/bin", "INVOCATION_ID": "deadbeef",
            "NOTIFY_SOCKET": "/run/x", "JOURNAL_STREAM": "8:12345"})
        self.assertEqual(["--setenv=PATH=/usr/bin"], argumente)

    def test_zeilenumbruch_wird_uebersprungen(self) -> None:
        """--setenv kann keine mehrzeiligen Werte, das braeche den Start."""
        self.assertEqual([], recovery.dienst_umgebung({"X": "a\nb"}))


class LaufTest(unittest.TestCase):
    """poll() beantwortet aus Dateien, was Popen aus dem Kind beantwortet."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.rc = Path(self.tmp.name) / "attempt-001.rc"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_laeuft_noch(self) -> None:
        lauf = recovery.DienstLauf("u", os.getpid(), self.rc)
        self.assertIsNone(lauf.poll())

    def test_rueckgabewert_null(self) -> None:
        self.rc.write_text("0", encoding="utf-8")
        self.assertEqual(0, recovery.DienstLauf("u", os.getpid(), self.rc).poll())

    def test_signalwert_bleibt_erhalten(self) -> None:
        """137/143 sind genau die Werte, die der Classifier kennt."""
        self.rc.write_text("137", encoding="utf-8")
        self.assertEqual(137, recovery.DienstLauf("u", os.getpid(), self.rc).poll())

    def test_prozess_weg_ohne_rueckgabewert(self) -> None:
        """Abgeraeumte cgroup: der Wrapper kam nicht mehr zum Schreiben."""
        lauf = recovery.DienstLauf("u", 4242, self.rc)
        with mock.patch.object(recovery, "_prozess_lebt", return_value=False):
            self.assertEqual(137, lauf.poll())

    def test_unlesbarer_wert_haelt_den_daemon_nicht_auf(self) -> None:
        self.rc.write_text("kaputt", encoding="utf-8")
        self.assertEqual(0, recovery.DienstLauf("u", os.getpid(), self.rc).poll())

    def test_wert_wird_gemerkt(self) -> None:
        """Einmal beendet, bleibt beendet - auch wenn die Datei verschwindet."""
        self.rc.write_text("3", encoding="utf-8")
        lauf = recovery.DienstLauf("u", os.getpid(), self.rc)
        self.assertEqual(3, lauf.poll())
        self.rc.unlink()
        self.assertEqual(3, lauf.poll())


class PidTest(unittest.TestCase):
    """Die PID kommt vom Manager - der Lauf ist kein Kindprozess mehr."""

    def _systemctl(self, ausgabe: str):
        return mock.patch.object(recovery.subprocess, "run",
                                 return_value=mock.Mock(stdout=ausgabe))

    def test_liest_mainpid(self) -> None:
        with self._systemctl("4711\n"):
            self.assertEqual(4711, recovery.mainpid("u.service"))

    def test_null_heisst_noch_nicht_gestartet(self) -> None:
        with self._systemctl("0\n"):
            self.assertIsNone(recovery.mainpid("u.service"))

    def test_systemctl_kaputt_ist_kein_absturz(self) -> None:
        with mock.patch.object(recovery.subprocess, "run",
                               side_effect=OSError("weg")):
            self.assertIsNone(recovery.mainpid("u.service"))

    def test_warten_gibt_auf(self) -> None:
        with mock.patch.object(recovery, "mainpid", return_value=None):
            self.assertIsNone(recovery.warte_auf_mainpid("u", grenze=0.0))

    def test_wartet_bis_der_manager_gestartet_hat(self) -> None:
        """Direkt nach systemd-run steht MainPID noch auf 0."""
        antworten = [None, None, 777]

        with mock.patch.object(recovery, "mainpid",
                               side_effect=lambda _u: antworten.pop(0)):
            self.assertEqual(777, recovery.warte_auf_mainpid(
                "u", grenze=1.0, schlaf=lambda _d: None))


class SchalterTest(unittest.TestCase):

    def _neu_laden(self, wert: str) -> str:
        with mock.patch.dict(os.environ, {"CW_RUN_LAUNCHER": wert}):
            importlib.reload(config)
            return config.RUN_LAUNCHER

    def tearDown(self) -> None:
        importlib.reload(config)

    def test_vorgabe_ist_scope(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CW_RUN_LAUNCHER", None)
            importlib.reload(config)
            self.assertEqual("scope", config.RUN_LAUNCHER)

    def test_dienst_ist_waehlbar(self) -> None:
        self.assertEqual("service", self._neu_laden("service"))
        self.assertEqual("service", self._neu_laden(" SERVICE "))

    def test_unsinn_faellt_auf_scope_zurueck(self) -> None:
        """Ein Tippfehler darf nicht heimlich einen dritten Startweg erfinden."""
        self.assertEqual("scope", self._neu_laden("dienstt"))


class StartTest(unittest.TestCase):
    """Der Start selbst: richtige Unit, PID vom Manager, saubere Ausnahme."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.alt = config.RUNS_DIR
        config.RUNS_DIR = Path(self.tmp.name)
        config.run_dir("t1").mkdir(parents=True, exist_ok=True)
        self.engine = recovery.RecoveryEngine.__new__(recovery.RecoveryEngine)
        self.task = mock.Mock(id="t1", cwd="/home/user/Projekt")

    def tearDown(self) -> None:
        config.RUNS_DIR = self.alt
        self.tmp.cleanup()

    def _starten(self, pid, gestartet=None):
        return self.engine._starte_als_dienst(
            ["/pfad/claude", "-p", "x"], self.task, 7,
            config.run_log("t1", 7), config.run_err("t1", 7))

    def test_start_liefert_lauf_mit_pid_vom_manager(self) -> None:
        with mock.patch.object(recovery.subprocess, "run",
                               return_value=mock.Mock(returncode=0)), \
             mock.patch.object(recovery, "warte_auf_mainpid", return_value=4711):
            lauf = self._starten(4711)
        self.assertEqual(4711, lauf.pid)
        self.assertEqual("claude-watchdog-t1-007", lauf.unit)

    def test_alte_reste_werden_vorher_geloescht(self) -> None:
        """Ein alter Rueckgabewert liesse den neuen Lauf sofort beendet wirken."""
        config.run_rc("t1", 7).write_text("0", encoding="utf-8")
        gesehen = {}

        def starten(argv, **_kw):
            gesehen["rc_da"] = config.run_rc("t1", 7).exists()
            return mock.Mock(returncode=0)

        with mock.patch.object(recovery.subprocess, "run", side_effect=starten), \
             mock.patch.object(recovery, "warte_auf_mainpid", return_value=4711):
            self._starten(4711)
        self.assertFalse(gesehen["rc_da"])

    def test_ohne_pid_gilt_der_start_als_gescheitert(self) -> None:
        with mock.patch.object(recovery.subprocess, "run",
                               return_value=mock.Mock(returncode=0)), \
             mock.patch.object(recovery, "warte_auf_mainpid", return_value=None):
            with self.assertRaises(OSError):
                self._starten(None)

    def test_sehr_kurzer_lauf_ist_kein_fehler(self) -> None:
        """Der Lauf kann fertig sein, bevor der erste Blick auf MainPID faellt."""
        def starten(argv, **_kw):
            config.run_rc("t1", 7).write_text("0", encoding="utf-8")
            return mock.Mock(returncode=0)

        with mock.patch.object(recovery.subprocess, "run", side_effect=starten), \
             mock.patch.object(recovery, "warte_auf_mainpid", return_value=None):
            lauf = self._starten(None)
        self.assertIsNone(lauf.pid)
        self.assertEqual(0, lauf.poll())


if __name__ == "__main__":
    unittest.main()
