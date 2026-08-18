"""Test der Wiederholungsunterdrueckung im Entscheidungs-Log."""

from __future__ import annotations

import logging
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from claude_watchdog import config, recovery  # noqa: E402
from claude_watchdog.models import (  # noqa: E402
    Action,
    Decision,
    ErrorClass,
    Mode,
    Status,
    Task,
)


def make(**kw) -> Task:
    defaults = dict(id="t1", title="Test", cwd="/tmp", mode=Mode.OBSERVED,
                    status=Status.RUNNING, original_prompt="tu was",
                    session_id="11111111-2222-3333-4444-555555555555")
    defaults.update(kw)
    return Task(**defaults)


class CapturingHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


class TestLeisesLog(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = recovery.RecoveryEngine.__new__(recovery.RecoveryEngine)
        self.engine.dry_run = False
        self.engine._logged = {}

        self.handler = CapturingHandler()
        self.log = logging.getLogger("cw.recovery")
        self._alte_stufe = self.log.level
        self.log.addHandler(self.handler)
        self.log.setLevel(logging.DEBUG)
        self.addCleanup(self.log.removeHandler, self.handler)
        self.addCleanup(self.log.setLevel, self._alte_stufe)

        self._alter_abstand = config.LOG_REPEAT_INTERVAL
        self.addCleanup(setattr, config, "LOG_REPEAT_INTERVAL",
                        self._alter_abstand)
        config.LOG_REPEAT_INTERVAL = 1800

    def stufen(self) -> list[str]:
        return [r.levelname for r in self.handler.records]

    def ruhig(self, reason: str = "laeuft") -> Decision:
        return Decision(action=Action.NONE, reason=reason)

    def test_erste_entscheidung_ist_laut(self) -> None:
        self.engine._log_decision(make(), self.ruhig(), 1000.0)
        self.assertEqual(self.stufen(), ["INFO"])

    def test_wiederholung_wird_leise(self) -> None:
        task = make()
        for i in range(5):
            self.engine._log_decision(task, self.ruhig(), 1000.0 + i * 15)
        self.assertEqual(self.stufen(), ["INFO", "DEBUG", "DEBUG",
                                         "DEBUG", "DEBUG"])

    def test_eintrag_bleibt_vollstaendig(self) -> None:
        """Leise heisst nur andere Stufe — die Felder muessen alle da sein."""
        self.engine._log_decision(make(), self.ruhig(), 1000.0)
        self.engine._log_decision(make(), self.ruhig(), 1015.0)
        leise = self.handler.records[-1]
        self.assertEqual(leise.levelno, logging.DEBUG)
        for feld in ("task", "title", "mode", "action", "class", "reason",
                     "delay", "attempts", "dry_run"):
            self.assertTrue(hasattr(leise, feld), feld)

    def test_geaenderter_grund_ist_wieder_laut(self) -> None:
        task = make()
        self.engine._log_decision(task, self.ruhig(), 1000.0)
        self.engine._log_decision(task, self.ruhig(), 1015.0)
        self.engine._log_decision(task, self.ruhig("untaetig"), 1030.0)
        self.assertEqual(self.stufen(), ["INFO", "DEBUG", "INFO"])

    def test_eingriff_ist_immer_laut(self) -> None:
        """Alles ausser NONE muss durch — sonst fehlt der Eingriff im Log."""
        task = make()
        for action in Action:
            self.handler.records.clear()
            self.engine._logged = {}
            entscheidung = Decision(action=action, reason="gleich")
            self.engine._log_decision(task, entscheidung, 1000.0)
            self.engine._log_decision(task, entscheidung, 1015.0)
            erwartet = ["INFO", "DEBUG"] if action is Action.NONE \
                else ["INFO", "INFO"]
            with self.subTest(action=action):
                self.assertEqual(self.stufen(), erwartet)

    def test_nach_dem_abstand_wieder_laut(self) -> None:
        task = make()
        self.engine._log_decision(task, self.ruhig(), 1000.0)
        self.engine._log_decision(task, self.ruhig(), 1000.0 + 1799)
        self.engine._log_decision(task, self.ruhig(), 1000.0 + 1800)
        self.assertEqual(self.stufen(), ["INFO", "DEBUG", "INFO"])

    def test_abstand_wird_ab_der_letzten_info_gemessen(self) -> None:
        """Sonst schoebe jede leise Zeile den Herzschlag vor sich her."""
        task = make()
        for i in range(200):          # 200 * 15 s = 3000 s am Stueck still
            self.engine._log_decision(task, self.ruhig(), 1000.0 + i * 15)
        self.assertEqual(self.stufen().count("INFO"), 2)

    def test_null_schaltet_unterdrueckung_ab(self) -> None:
        config.LOG_REPEAT_INTERVAL = 0
        task = make()
        for i in range(3):
            self.engine._log_decision(task, self.ruhig(), 1000.0 + i * 15)
        self.assertEqual(self.stufen(), ["INFO", "INFO", "INFO"])

    def test_tasks_stoeren_sich_nicht_gegenseitig(self) -> None:
        a, b = make(id="a"), make(id="b")
        self.engine._log_decision(a, self.ruhig(), 1000.0)
        self.engine._log_decision(b, self.ruhig(), 1005.0)
        self.engine._log_decision(a, self.ruhig(), 1015.0)
        self.engine._log_decision(b, self.ruhig(), 1020.0)
        self.assertEqual(self.stufen(), ["INFO", "INFO", "DEBUG", "DEBUG"])

    def test_merker_waechst_nicht_unbegrenzt(self) -> None:
        grenze = recovery.RecoveryEngine._LOG_MEMO_MAX
        for i in range(grenze + 50):
            self.engine._log_decision(make(id="t%d" % i), self.ruhig(),
                                      1000.0 + i)
        self.assertLessEqual(len(self.engine._logged), grenze)
        # Der juengste Eintrag muss erhalten bleiben.
        self.assertIn("t%d" % (grenze + 49), self.engine._logged)

    def test_wechselnder_grund_bei_nichtstun_bleibt_leise(self) -> None:
        """Der eigentliche Fall aus dem Betrieb.

        Eine arbeitende Session wechselt von Takt zu Takt zwischen 'laeuft'
        und 'laeuft (Fortschritt)'. Beides heisst 'nichts zu tun' — das darf
        die Unterdrueckung nicht aushebeln.
        """
        task = make()
        gruende = ["laeuft", "laeuft (Fortschritt)"] * 6
        for i, grund in enumerate(gruende):
            self.engine._log_decision(task, self.ruhig(grund), 1000.0 + i * 15)
        self.assertEqual(self.stufen().count("INFO"), 1)

    def test_bei_einem_eingriff_zaehlt_der_grund_weiter(self) -> None:
        """Nur beim Nichtstun wird der Grund ignoriert, sonst nicht."""
        task = make()
        for i, grund in enumerate(("API kaputt", "Netz weg", "API kaputt")):
            self.engine._log_decision(
                task,
                Decision(action=Action.NOTIFY, reason=grund,
                         error_class=ErrorClass.API_ERROR),
                1000.0 + i * 15)
        self.assertEqual(self.stufen(), ["INFO", "INFO", "INFO"])


if __name__ == "__main__":
    unittest.main()
