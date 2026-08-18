"""Test: ein Task uebernimmt niemals das Transkript einer fremden Session.

Hintergrund: `auto_attach` legt den Task an, sobald das CLI die Session
meldet — das kann sein, bevor deren Transkriptdatei ueberhaupt existiert.
In dieser Luecke griff der Notbehelf "neueste .jsonl im Projektverzeichnis"
und der Task ueberwachte fortan eine voellig andere Sitzung. Beobachtet am
2026-07-31 an Task 70db0af7: angelegt 14:21:25, eigenes Transkript ab
14:22:06 — 41 Sekunden zu frueh.
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


def make(**kw) -> Task:
    defaults = dict(id="t1", title="Test", cwd="/home/user/Desktop",
                    mode=Mode.OBSERVED, status=Status.RUNNING,
                    session_id=EIGEN)
    defaults.update(kw)
    return Task(**defaults)


class TranskriptZuordnungTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        wurzel = Path(self.tmp.name)
        patcher = unittest.mock.patch.object(config, "CLAUDE_PROJECTS_DIR", wurzel)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.projekt = config.project_dir("/home/user/Desktop")
        self.projekt.mkdir(parents=True)

    def datei(self, session_id: str, inhalt: str = "{}\n") -> Path:
        p = self.projekt / ("%s.jsonl" % session_id)
        p.write_text(inhalt)
        return p

    def test_eigenes_transkript_wird_genommen(self) -> None:
        eigen = self.datei(EIGEN)
        self.assertEqual(detector.resolve_transcript(make()), eigen)

    def test_kein_fremdes_transkript_wenn_das_eigene_fehlt(self) -> None:
        """Der beobachtete Fall: eigene Datei noch nicht da, fremde schon.

        Frueher lieferte der Notbehelf hier die fremde Datei — und der Task
        behielt sie, weil der Pfad gespeichert wird. Die laengst beendete
        Session galt damit dauerhaft als lebendig.
        """
        self.datei(FREMD, "{}\n" * 500)
        self.assertIsNone(detector.resolve_transcript(make()))

    def test_falsch_gemerkter_pfad_heilt_aus(self) -> None:
        """Ein einmal gespeicherter Fremdpfad darf sich nicht fortschreiben."""
        fremd = self.datei(FREMD, "{}\n" * 500)
        eigen = self.datei(EIGEN)
        task = make(transcript_path=str(fremd))
        self.assertEqual(detector.resolve_transcript(task), eigen)

    def test_fremdpfad_wird_auch_ohne_ersatz_verworfen(self) -> None:
        fremd = self.datei(FREMD)
        task = make(transcript_path=str(fremd))
        self.assertIsNone(detector.resolve_transcript(task))

    def test_gemerkter_eigener_pfad_bleibt_gueltig(self) -> None:
        """Liegt das Transkript woanders, aber traegt die richtige UUID."""
        anderswo = Path(self.tmp.name) / ("%s.jsonl" % EIGEN)
        anderswo.write_text("{}\n")
        task = make(transcript_path=str(anderswo))
        self.assertEqual(detector.resolve_transcript(task), anderswo)

    def test_ohne_session_id_greift_der_notbehelf_weiter(self) -> None:
        """Genau dafuer ist er da — ein managed Task vor dem ersten Start."""
        fremd = self.datei(FREMD)
        task = make(session_id=None, status=Status.RUNNING)
        self.assertEqual(detector.resolve_transcript(task), fremd)

    def test_nie_gestarteter_task_bekommt_nichts(self) -> None:
        self.datei(FREMD)
        task = make(session_id=None, status=Status.PENDING)
        self.assertIsNone(
            detector.resolve_transcript(task, allow_fallback=False))


class ObserveNimmtNichtsFremdesTest(unittest.TestCase):
    """Die Wirkung in `observe`: kein erfundener Fortschritt."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = unittest.mock.patch.object(
            config, "CLAUDE_PROJECTS_DIR", Path(self.tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.projekt = config.project_dir("/home/user/Desktop")
        self.projekt.mkdir(parents=True)

    def test_fremdes_wachstum_gilt_nicht_als_fortschritt(self) -> None:
        (self.projekt / ("%s.jsonl" % FREMD)).write_text("{}\n" * 500)

        class KeineAgenten:
            usable = True

            def get(self, _sid):
                return None

        obs = detector.observe(make(transcript_size=0), KeineAgenten(),
                               now=1000.0)
        self.assertFalse(obs.progressed)
        self.assertEqual(obs.transcript_size, 0)


if __name__ == "__main__":
    unittest.main()
