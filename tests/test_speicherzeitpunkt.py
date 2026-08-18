"""Test: was `observe()` am Task korrigiert, muss auch gespeichert werden.

Hintergrund: `_process` nahm den Vergleichsstand erst **nach** `observe()`.
Die Beobachtung aendert aber selbst pid, session_id und transcript_path —
diese Aenderungen steckten damit schon in `before` und wurden nie
zurueckgeschrieben. Beobachtet am 2026-07-31: Task 70db0af7 zeigte auch
nach einem Neustart weiter auf das Transkript einer fremden Session,
obwohl `resolve_transcript` laengst den richtigen Pfad lieferte.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from claude_watchdog import config, daemon  # noqa: E402
from claude_watchdog.daemon import Watchdog  # noqa: E402
from claude_watchdog.models import (  # noqa: E402
    Action,
    Decision,
    ErrorClass,
    Mode,
    Status,
    Task,
)
from claude_watchdog.registry import Registry  # noqa: E402

EIGEN = "11111111-2222-3333-4444-555555555555"
FREMD = "66666666-7777-8888-9999-000000000000"


class FakeAgents:
    """Das CLI kennt die Session nicht (mehr)."""

    usable = True

    def get(self, _session_id):
        return None

    def all(self):
        return {}

    def refresh(self, force: bool = False):
        return {}


class SpeicherzeitpunktTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        wurzel = Path(self.tmp.name)

        p = unittest.mock.patch.object(config, "CLAUDE_PROJECTS_DIR",
                                       wurzel / "projects")
        p.start()
        self.addCleanup(p.stop)
        self.projekt = config.project_dir("/home/user/Desktop")
        self.projekt.mkdir(parents=True)

        self.registry = Registry(wurzel / "state.db")
        self.addCleanup(self.registry.close)
        self.wd = Watchdog(registry=self.registry, dry_run=True)
        self.wd.agents = FakeAgents()

        # Eine Entscheidung, die den Task selbst nicht anfasst. Damit bleibt
        # als einzige Aenderungsquelle `observe()` uebrig — sonst wuerde ein
        # nebenher gesetzter Status oder Termin das Zurueckschreiben ohnehin
        # ausloesen und der Test ginge auch mit dem Fehler durch.
        p2 = unittest.mock.patch.object(
            daemon, "decide",
            lambda *a, **kw: Decision(action=Action.NONE, reason="ruhig",
                                      error_class=ErrorClass.NONE))
        p2.start()
        self.addCleanup(p2.stop)

    def datei(self, session_id: str, zeilen: int = 1) -> Path:
        pfad = self.projekt / ("%s.jsonl" % session_id)
        pfad.write_text("{}\n" * zeilen)
        return pfad

    def test_korrigierter_transkriptpfad_wird_gespeichert(self) -> None:
        """Der beobachtete Fall — ein einmal falsch gemerkter Pfad.

        Entscheidend ist, dass das eigene Transkript **kleiner** ist als der
        gemerkte Stand: dann meldet `observe` keinen Fortschritt, und das
        Zurueckschreiben haengt allein am Vorher-Nachher-Vergleich. Genau so
        lag der Fall in der Produktion (eigene Datei 2 472 Bytes, gemerkt
        waren 15 183 742 aus dem fremden Transkript).
        """
        fremd = self.datei(FREMD, 500)
        eigen = self.datei(EIGEN)
        task = self.registry.add(Task(
            id="t1", title="Test", cwd="/home/user/Desktop",
            mode=Mode.OBSERVED, status=Status.RUNNING, session_id=EIGEN,
            transcript_path=str(fremd), transcript_size=5000,
            original_prompt=""))

        self.wd._process(task, None, True, 1000.0)

        gespeichert = self.registry.get("t1")
        self.assertEqual(gespeichert.transcript_path, str(eigen))

    def test_gefundene_session_id_wird_gespeichert(self) -> None:
        """`observe` traegt die session_id nach — auch das muss ankommen."""
        eigen = self.datei(EIGEN)
        task = self.registry.add(Task(
            id="t2", title="Test", cwd="/home/user/Desktop",
            mode=Mode.OBSERVED, status=Status.RUNNING, session_id=None,
            transcript_path=str(eigen), transcript_size=5000,
            original_prompt=""))

        self.wd._process(task, None, True, 1000.0)

        self.assertEqual(self.registry.get("t2").session_id, EIGEN)

    def test_ohne_aenderung_wird_nicht_geschrieben(self) -> None:
        """Die Sparsamkeit bleibt: unveraenderte Tasks fassen die DB nicht an."""
        eigen = self.datei(EIGEN)
        task = self.registry.add(Task(
            id="t3", title="Test", cwd="/home/user/Desktop",
            mode=Mode.OBSERVED, status=Status.RUNNING, session_id=EIGEN,
            transcript_path=str(eigen), original_prompt=""))
        self.wd._process(task, None, True, 1000.0)
        vorher = self.registry.get("t3").updated_at

        schreibt: list[str] = []
        echt = self.registry.update

        def merken(t):
            schreibt.append(t.id)
            return echt(t)

        with unittest.mock.patch.object(self.registry, "update", merken):
            self.wd._process(self.registry.get("t3"), None, True, 1001.0)
        self.assertEqual(schreibt, [])
        self.assertEqual(self.registry.get("t3").updated_at, vorher)


if __name__ == "__main__":
    unittest.main()
