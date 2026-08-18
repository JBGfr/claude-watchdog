"""Test: der gemerkte Transkriptstand gehoert zu genau einer Datei.

Hintergrund: nachdem die Zuordnung korrigiert war, zeigten Tasks auf ihr
eigenes Transkript — trugen aber weiter die Groesse der zuvor faelschlich
ueberwachten fremden Datei. Da `progressed` ein reiner Groessenvergleich
ist, konnte so ein Task nie wieder Fortschritt melden. Beobachtet am
2026-07-31 an 593913bf: eigenes Transkript 3 913 708 Bytes, gemerkt waren
15 183 742 — erst jenseits von 15 MB waere wieder etwas sichtbar geworden.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from claude_watchdog import config, detector  # noqa: E402
from claude_watchdog.models import Mode, Status, Task  # noqa: E402

EIGEN = "11111111-2222-3333-4444-555555555555"
FREMD = "66666666-7777-8888-9999-000000000000"


class KeineAgenten:
    usable = True

    def get(self, _sid):
        return None


def make(**kw) -> Task:
    defaults = dict(id="t1", title="Test", cwd="/home/user/Desktop",
                    mode=Mode.OBSERVED, status=Status.RUNNING,
                    session_id=EIGEN, original_prompt="")
    defaults.update(kw)
    return Task(**defaults)


class GroessenstandTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = unittest.mock.patch.object(config, "CLAUDE_PROJECTS_DIR",
                                       Path(self.tmp.name))
        p.start()
        self.addCleanup(p.stop)
        self.projekt = config.project_dir("/home/user/Desktop")
        self.projekt.mkdir(parents=True)

    def datei(self, session_id: str, bytes_: int) -> Path:
        pfad = self.projekt / ("%s.jsonl" % session_id)
        pfad.write_text("x" * bytes_)
        return pfad

    def test_unmoeglich_grosser_stand_wird_uebernommen(self) -> None:
        """Der beobachtete Fall: Stand stammt von der fremden Datei."""
        self.datei(EIGEN, 100)
        task = make(transcript_size=15_183_742)
        obs = detector.observe(task, KeineAgenten(), now=1000.0)
        self.assertEqual(task.transcript_size, 100)
        self.assertFalse(obs.progressed)

    def test_danach_zaehlt_echtes_wachstum_wieder(self) -> None:
        """Die Heilung muss halten: der naechste Zuwachs ist Fortschritt."""
        pfad = self.datei(EIGEN, 100)
        task = make(transcript_size=15_183_742)
        detector.observe(task, KeineAgenten(), now=1000.0)
        pfad.write_text("x" * 250)
        obs = detector.observe(task, KeineAgenten(), now=1001.0)
        self.assertTrue(obs.progressed)
        self.assertEqual(obs.transcript_size, 250)

    def test_dateiwechsel_meldet_keinen_scheinfortschritt(self) -> None:
        """Wechsel auf eine groessere Datei darf nicht als Zuwachs gelten."""
        fremd = self.datei(FREMD, 50)
        self.datei(EIGEN, 900)
        task = make(transcript_path=str(fremd), transcript_size=50)
        obs = detector.observe(task, KeineAgenten(), now=1000.0)
        self.assertTrue(task.transcript_path.endswith("%s.jsonl" % EIGEN))
        self.assertFalse(obs.progressed)
        self.assertEqual(task.transcript_size, 900)

    def test_normales_wachstum_bleibt_fortschritt(self) -> None:
        """Die Regel darf den Normalfall nicht anfassen."""
        self.datei(EIGEN, 500)
        task = make(transcript_size=200)
        obs = detector.observe(task, KeineAgenten(), now=1000.0)
        self.assertTrue(obs.progressed)
        self.assertEqual(task.transcript_size, 200)


class GemerkteFelderTest(unittest.TestCase):
    """Ohne Speichern haette der Task die Uebernahme jeden Takt neu gemacht."""

    def test_groesse_gehoert_zu_den_gemerkten_feldern(self) -> None:
        from claude_watchdog.daemon import _gemerkte_felder
        self.assertNotEqual(_gemerkte_felder(make(transcript_size=1)),
                            _gemerkte_felder(make(transcript_size=2)))


if __name__ == "__main__":
    unittest.main()
