"""Tests der Gesundheitspruefung - insbesondere: was schlaegt was?"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from claude_watchdog import detector  # noqa: E402
from claude_watchdog.models import Mode, Status, Task  # noqa: E402


class FakeAgents:
    """Ersatz fuer AgentsSnapshot: liefert eine feste Antwort."""

    def __init__(self, entry=None, usable: bool = True):
        self._entry = entry
        self._usable = usable

    def get(self, session_id):
        return self._entry

    @property
    def usable(self) -> bool:
        return self._usable


def make(**kw) -> Task:
    defaults = dict(id="t1", title="Test", cwd="/nicht/vorhanden",
                    mode=Mode.MANAGED, status=Status.RUNNING,
                    session_id="11111111-2222-3333-4444-555555555555")
    defaults.update(kw)
    return Task(**defaults)


class TestExitCode(unittest.TestCase):
    """Ein eingesammelter Exit-Code ist verbindlicher als der CLI-Snapshot."""

    #: So sieht ein Eintrag aus, den `claude agents --json` noch liefert,
    #: obwohl der Prozess schon weg ist (Cache bis AGENTS_CACHE_TTL).
    STALE = {"sessionId": "11111111-2222-3333-4444-555555555555",
             "status": "busy"}

    def test_exit_code_schlaegt_veralteten_cli_snapshot(self):
        obs = detector.observe(make(), FakeAgents(self.STALE),
                               now=1_000_000.0, exit_code=-9)
        self.assertFalse(obs.alive, "beendeter Prozess darf nicht als lebend gelten")
        self.assertEqual(obs.exit_code, -9)

    def test_exit_null_zaehlt_genauso(self):
        # Auch ein sauberes Ende ist ein Ende.
        obs = detector.observe(make(), FakeAgents(self.STALE),
                               now=1_000_000.0, exit_code=0)
        self.assertFalse(obs.alive)

    def test_ohne_exit_code_bleibt_der_cli_snapshot_massgeblich(self):
        obs = detector.observe(make(), FakeAgents(self.STALE), now=1_000_000.0)
        self.assertTrue(obs.alive)
        self.assertTrue(obs.known_to_cli)
        self.assertIsNone(obs.exit_code)

    def test_cli_unerreichbar_wird_vermerkt(self):
        obs = detector.observe(make(), FakeAgents(None, usable=False),
                               now=1_000_000.0)
        self.assertFalse(obs.cli_usable)
        self.assertFalse(obs.known_to_cli)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestVerwaisterRegistryEintrag(unittest.TestCase):
    """Ein Eintrag ohne pid UND ohne status ist kein Lebenszeichen.

    Die Registry behaelt beendete Sitzungen. Zaehlte das als 'lebt', konnte
    _observed_session_gone() nie greifen und der Task stand endlos auf
    'laeuft' — beobachtet mit 35 Stunden Abstand zum letzten Transkript.
    """

    #: So sieht ein verwaister Eintrag wirklich aus (2026-07-31 gemessen).
    VERWAIST = {"sessionId": "11111111-2222-3333-4444-555555555555",
                "name": "Steam-Fehler beheben"}

    def test_leerer_eintrag_gilt_nicht_als_lebend(self):
        obs = detector.observe(make(), FakeAgents(self.VERWAIST), now=1_000_000.0)
        self.assertFalse(obs.alive, "verwaister Eintrag darf nicht 'lebt' bedeuten")
        # Bekannt ist er der CLI trotzdem — das ist eine andere Aussage.
        self.assertTrue(obs.known_to_cli)

    def test_eintrag_mit_pid_gilt_als_lebend(self):
        eintrag = dict(self.VERWAIST, pid=999_999)
        obs = detector.observe(make(), FakeAgents(eintrag), now=1_000_000.0)
        self.assertTrue(obs.alive)

    def test_eintrag_mit_status_gilt_als_lebend(self):
        """Cache-Eintrag kurz nach dem Verschwinden des Prozesses."""
        eintrag = dict(self.VERWAIST, status="busy")
        obs = detector.observe(make(), FakeAgents(eintrag), now=1_000_000.0)
        self.assertTrue(obs.alive)


class TestPidFolgtDerSitzung(unittest.TestCase):
    """Nach einem Resume laeuft die Sitzung unter neuer pid.

    Beobachtet am 2026-07-31: task.pid 33781 laengst tot, waehrend die
    Registry fuer dieselbe session_id 115196 als lebend fuehrte.
    """

    def test_tote_pid_wird_ersetzt(self):
        t = make()
        t.pid = 999_999          # existiert nicht
        obs = detector.observe(t, FakeAgents({"sessionId": t.session_id,
                                              "pid": 4242, "status": "idle"}),
                               now=1_000_000.0)
        self.assertEqual(t.pid, 4242)
        self.assertTrue(obs.alive)

    def test_leere_pid_wird_gefuellt(self):
        t = make()
        t.pid = None
        detector.observe(t, FakeAgents({"sessionId": t.session_id,
                                        "pid": 4242, "status": "idle"}),
                         now=1_000_000.0)
        self.assertEqual(t.pid, 4242)

    def test_lebende_pid_bleibt_stehen(self):
        """Der eigene Prozess lebt — der ist verlaesslicher als der Snapshot."""
        t = make()
        t.pid = os.getpid()
        detector.observe(t, FakeAgents({"sessionId": t.session_id,
                                        "pid": 4242, "status": "idle"}),
                         now=1_000_000.0)
        self.assertEqual(t.pid, os.getpid())

    def test_eintrag_ohne_pid_aendert_nichts(self):
        t = make()
        t.pid = 999_999
        detector.observe(t, FakeAgents({"sessionId": t.session_id}),
                         now=1_000_000.0)
        self.assertEqual(t.pid, 999_999)
