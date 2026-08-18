"""Test: ein ueberwundener Fehler wird aus dem Task geloescht."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from claude_watchdog.models import (  # noqa: E402
    Action,
    Decision,
    ErrorClass,
    Mode,
    Observation,
    Status,
    Task,
)
from claude_watchdog.recovery import RecoveryEngine  # noqa: E402


class Schweiger:
    """Platzhalter fuer Notifier und Registry: nimmt alles entgegen, tut nichts.

    Die Tests pruefen ausschliesslich, was `execute()` am Task aendert —
    Meldungen und Persistenz gehoeren nicht dazu.
    """

    def __getattr__(self, _name):
        return lambda *a, **kw: None


def engine() -> RecoveryEngine:
    e = RecoveryEngine.__new__(RecoveryEngine)
    e.dry_run = False
    e.children = {}
    e._logged = {}
    e.registry = Schweiger()
    e.notify = Schweiger()
    return e


def make(**kw) -> Task:
    defaults = dict(id="t1", title="Test", cwd="/tmp", mode=Mode.OBSERVED,
                    status=Status.RUNNING, original_prompt="tu was",
                    session_id="11111111-2222-3333-4444-555555555555",
                    last_error_class=ErrorClass.RATE_LIMIT.value,
                    last_error_text="Limit erreicht",
                    next_retry_at=1000.0)
    defaults.update(kw)
    return Task(**defaults)


class ErrorResetTest(unittest.TestCase):
    def setUp(self) -> None:
        self.e = engine()

    def lauf(self, task: Task, obs: Observation, decision: Decision) -> Task:
        return self.e.execute(task, decision, obs, now=2000.0)

    def test_laufende_session_ohne_fehler_raeumt_auf(self) -> None:
        """Der beobachtete Fall: RATE_LIMIT haengt an einer Session, die laeuft."""
        task = self.lauf(
            make(),
            Observation(alive=True, progressed=True),
            Decision(action=Action.NONE, reason="laeuft (Fortschritt)",
                     error_class=ErrorClass.NONE))
        self.assertIsNone(task.last_error_class)
        self.assertIsNone(task.next_retry_at)

    def test_fehlertext_bleibt_als_gedaechtnis(self) -> None:
        task = self.lauf(
            make(),
            Observation(alive=True),
            Decision(action=Action.NONE, reason="laeuft",
                     error_class=ErrorClass.NONE))
        self.assertEqual(task.last_error_text, "Limit erreicht")

    def test_toter_prozess_raeumt_nicht_auf(self) -> None:
        """Ohne lebenden Prozess fehlt der Beleg, dass es weitergeht."""
        task = self.lauf(
            make(),
            Observation(alive=False),
            Decision(action=Action.NONE, reason="untaetig",
                     error_class=ErrorClass.NONE))
        self.assertEqual(task.last_error_class, ErrorClass.RATE_LIMIT.value)
        self.assertEqual(task.next_retry_at, 1000.0)

    def test_frischer_fehler_ueberschreibt_weiterhin(self) -> None:
        task = self.lauf(
            make(),
            Observation(alive=True),
            Decision(action=Action.NOTIFY, reason="API kaputt",
                     error_class=ErrorClass.API_ERROR))
        self.assertEqual(task.last_error_class, ErrorClass.API_ERROR.value)
        self.assertEqual(task.last_error_text, "API kaputt")

    def test_andere_aktion_raeumt_nicht_auf(self) -> None:
        """Nur 'nichts tun' ist ein Beleg — ein Eingriff ist es nicht."""
        task = self.lauf(
            make(),
            Observation(alive=True),
            Decision(action=Action.COMPLETE, reason="result:success",
                     error_class=ErrorClass.NONE))
        self.assertEqual(task.last_error_class, ErrorClass.RATE_LIMIT.value)

    def test_ohne_vorherigen_fehler_passiert_nichts(self) -> None:
        task = self.lauf(
            make(last_error_class=None, last_error_text=None,
                 next_retry_at=None),
            Observation(alive=True, progressed=True),
            Decision(action=Action.NONE, reason="laeuft",
                     error_class=ErrorClass.NONE))
        self.assertIsNone(task.last_error_class)

    def test_wiederanlauf_regel_bleibt_unberuehrt(self) -> None:
        """Die Regel verlangt 'not obs.alive' — dort raeumen wir nie auf."""
        task = make(status=Status.WAITING_FOR_LIMIT, mode=Mode.MANAGED)
        out = self.lauf(
            task, Observation(alive=False),
            Decision(action=Action.NONE, reason="wartet",
                     error_class=ErrorClass.NONE))
        self.assertEqual(out.next_retry_at, 1000.0)


if __name__ == "__main__":
    unittest.main()
