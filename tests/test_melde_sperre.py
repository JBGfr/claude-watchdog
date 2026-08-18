"""Test: eine Meldung drosselt sich selbst, aber nicht die Beobachtung.

Hintergrund: `_act_notify` setzte die Wiederholsperre auf `next_retry_at`.
Den liest `daemon._process` als "diesen Task bis dahin gar nicht ansehen" —
bei STALL_SECONDS = 900 s war der Watchdog nach einer einzigen Meldung eine
Viertelstunde blind (gemessen am 2026-07-31 an zwei laufenden Sitzungen,
beide hatten in der Sperre rund 100 kB geschrieben).
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from claude_watchdog import config  # noqa: E402
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
from claude_watchdog.registry import Registry  # noqa: E402


class Sammler:
    """Notifier-Ersatz, der die Meldungen mitschreibt."""

    def __init__(self) -> None:
        self.gesendet: list[tuple] = []

    def send(self, titel, text, urgency=None):
        self.gesendet.append((titel, text, urgency))


class Schweiger:
    def __getattr__(self, _name):
        return lambda *a, **kw: None


def engine(notify=None) -> RecoveryEngine:
    e = RecoveryEngine.__new__(RecoveryEngine)
    e.dry_run = False
    e.children = {}
    e._logged = {}
    e.registry = Schweiger()
    e.notify = notify or Sammler()
    return e


def make(**kw) -> Task:
    defaults = dict(id="t1", title="Test", cwd="/tmp", mode=Mode.OBSERVED,
                    status=Status.RUNNING, original_prompt="tu was",
                    session_id="11111111-2222-3333-4444-555555555555")
    defaults.update(kw)
    return Task(**defaults)


def meldung(**kw) -> Decision:
    d = dict(action=Action.NOTIFY, reason="wartet auf Benutzereingabe",
             error_class=ErrorClass.AWAITING_INPUT, notify="Bitte antworten")
    d.update(kw)
    return Decision(**d)


class MeldeSperreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.notify = Sammler()
        self.e = engine(self.notify)

    def test_meldung_setzt_keinen_eingriffstermin(self) -> None:
        """Der Kern: nach einer Meldung sieht der Daemon weiter hin.

        `daemon._process` ueberspringt jeden Task mit gesetztem
        next_retry_at — deshalb darf eine blosse Meldung ihn nicht setzen.
        """
        task = self.e.execute(make(), meldung(), Observation(alive=True),
                              now=1000.0)
        self.assertIsNone(task.next_retry_at)
        self.assertEqual(task.mute_until, 1000.0 + config.STALL_SECONDS)

    def test_zweite_meldung_in_der_sperre_wird_geschluckt(self) -> None:
        task = self.e.execute(make(), meldung(), Observation(alive=True),
                              now=1000.0)
        task = self.e.execute(task, meldung(), Observation(alive=True),
                              now=1000.0 + config.STALL_SECONDS - 1)
        self.assertEqual(len(self.notify.gesendet), 1)

    def test_nach_der_sperre_wird_wieder_gemeldet(self) -> None:
        task = self.e.execute(make(), meldung(), Observation(alive=True),
                              now=1000.0)
        spaeter = 1000.0 + config.STALL_SECONDS
        task = self.e.execute(task, meldung(), Observation(alive=True),
                              now=spaeter)
        self.assertEqual(len(self.notify.gesendet), 2)
        self.assertEqual(task.mute_until, spaeter + config.STALL_SECONDS)

    def test_status_wird_auch_in_der_sperre_gefuehrt(self) -> None:
        """Geschluckt wird die Meldung, nicht die Zustandsfuehrung."""
        task = self.e.execute(make(), meldung(), Observation(alive=True),
                              now=1000.0)
        task.status = Status.RUNNING
        task = self.e.execute(task, meldung(), Observation(alive=True),
                              now=1001.0)
        self.assertIs(task.status, Status.BLOCKED)

    def test_ueberwundene_stoerung_hebt_die_sperre_auf(self) -> None:
        """Sonst bliebe die naechste Stoerung bis zu 900 s unbemerkt."""
        task = self.e.execute(make(), meldung(), Observation(alive=True),
                              now=1000.0)
        self.assertIsNotNone(task.mute_until)
        task = self.e.execute(
            task, Decision(action=Action.NONE, reason="laeuft (Fortschritt)",
                           error_class=ErrorClass.NONE),
            Observation(alive=True, progressed=True), now=1010.0)
        self.assertIsNone(task.mute_until)
        # Und die naechste Meldung geht sofort raus.
        self.e.execute(task, meldung(), Observation(alive=True), now=1011.0)
        self.assertEqual(len(self.notify.gesendet), 2)


class SpeicherungTest(unittest.TestCase):
    """Die Sperre muss einen Neustart des Daemons ueberleben."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "state.db"

    def test_wird_gespeichert_und_gelesen(self) -> None:
        reg = Registry(self.db)
        task = reg.add(make(mute_until=1234.5))
        reg.close()
        wieder = Registry(self.db).get(task.id)
        self.assertEqual(wieder.mute_until, 1234.5)

    def test_alte_datenbank_bekommt_die_spalte(self) -> None:
        """`CREATE TABLE IF NOT EXISTS` ruehrt eine vorhandene Tabelle nicht an.

        Ohne Nachruesten scheitert jede bestehende Installation beim ersten
        Zugriff an der fehlenden Spalte.
        """
        con = sqlite3.connect(str(self.db))
        con.executescript("""
            CREATE TABLE tasks (
                id TEXT PRIMARY KEY, title TEXT, original_prompt TEXT,
                cwd TEXT, session_id TEXT, mode TEXT, status TEXT,
                pid INTEGER, transcript_path TEXT, attempts INTEGER,
                max_attempts INTEGER, no_auto_resume INTEGER, model TEXT,
                permission_mode TEXT, max_budget_usd REAL,
                last_error_class TEXT, last_error_text TEXT,
                created_at REAL, updated_at REAL, last_progress_at REAL,
                next_retry_at REAL, cost_usd_spent REAL,
                transcript_size INTEGER, last_resume_marker TEXT,
                same_marker_count INTEGER);
        """)
        con.close()

        reg = Registry(self.db)
        spalten = {r["name"] for r in
                   reg._conn.execute("PRAGMA table_info(tasks)")}
        self.assertIn("mute_until", spalten)
        # Und die Datenbank ist danach wirklich benutzbar.
        task = reg.add(make(mute_until=99.0))
        self.assertEqual(reg.get(task.id).mute_until, 99.0)


class DaemonSichtTest(unittest.TestCase):
    """Der Daemon muss die Sperre mitspeichern, sonst faengt sie bei 0 an."""

    def test_sperre_gehoert_zu_den_gemerkten_feldern(self) -> None:
        from claude_watchdog.daemon import _gemerkte_felder
        vorher = _gemerkte_felder(make())
        nachher = _gemerkte_felder(make(mute_until=500.0))
        self.assertNotEqual(vorher, nachher)


if __name__ == "__main__":
    unittest.main()
