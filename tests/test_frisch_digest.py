"""Tests fuer den Schalter CW_FRESH_DIGEST beim Neuanfang nach Kontextlimit."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from claude_watchdog import config, recovery  # noqa: E402
from claude_watchdog.models import Mode, Observation, Status, Task  # noqa: E402

#: Steht stellvertretend fuer alles, was zufaellig im Transkript-Tail landet.
VERLAUF = (
    "Ich habe /home/user/Projekte/kunde/.env gelesen und "
    "das Token GEHEIM-ABC123 gegen die API geprueft."
)

AUFTRAG = "Baue den Importer fuer die Kundendatei fertig."


def make_task() -> Task:
    return Task(id="t1", title="Importer", cwd="/tmp", mode=Mode.MANAGED,
                status=Status.RUNNING, original_prompt=AUFTRAG,
                session_id="11111111-2222-3333-4444-555555555555")


class Basis(unittest.TestCase):
    """Engine ohne Registry, Schalter wird pro Test gesetzt und zurueckgeholt."""

    def setUp(self) -> None:
        self.engine = recovery.RecoveryEngine.__new__(recovery.RecoveryEngine)
        self.engine.dry_run = True
        self.task = make_task()
        self.obs = Observation(alive=False, tail_text=VERLAUF)
        self._alter_schalter = config.FRESH_DIGEST

    def tearDown(self) -> None:
        config.FRESH_DIGEST = self._alter_schalter

    def prompt(self) -> str:
        return self.engine.fresh_prompt(self.task, self.obs)


class TestSchalterAn(Basis):
    def test_auszug_steht_im_prompt(self) -> None:
        config.FRESH_DIGEST = True
        self.assertIn(VERLAUF, self.prompt())

    def test_kein_platzhalter_wenn_an(self) -> None:
        config.FRESH_DIGEST = True
        self.assertNotIn(recovery.DIGEST_PLATZHALTER, self.prompt())

    def test_leerer_verlauf_meldet_das(self) -> None:
        config.FRESH_DIGEST = True
        self.obs = Observation(alive=False, tail_text="")
        self.assertIn("kein verwertbarer Verlauf gefunden", self.prompt())

    def test_langer_verlauf_wird_gekuerzt(self) -> None:
        config.FRESH_DIGEST = True
        self.obs = Observation(alive=False, tail_text="A" * 500 + VERLAUF)
        auszug = self.engine._digest(self.task, self.obs, limit=len(VERLAUF))
        self.assertEqual(auszug, VERLAUF)


class TestSchalterAus(Basis):
    def test_auszug_fehlt_im_prompt(self) -> None:
        config.FRESH_DIGEST = False
        self.assertNotIn(VERLAUF, self.prompt())

    def test_kein_bruchstueck_des_verlaufs(self) -> None:
        config.FRESH_DIGEST = False
        self.assertNotIn("GEHEIM-ABC123", self.prompt())

    def test_platzhalter_steht_drin(self) -> None:
        config.FRESH_DIGEST = False
        self.assertIn(recovery.DIGEST_PLATZHALTER, self.prompt())

    def test_platzhalter_auch_ohne_verlauf(self) -> None:
        config.FRESH_DIGEST = False
        self.obs = Observation(alive=False, tail_text="")
        self.assertIn(recovery.DIGEST_PLATZHALTER, self.prompt())

    def test_digest_liefert_nur_den_platzhalter(self) -> None:
        config.FRESH_DIGEST = False
        self.assertEqual(self.engine._digest(self.task, self.obs),
                         recovery.DIGEST_PLATZHALTER)


class TestAuftragBleibt(Basis):
    """Der urspruengliche Auftrag darf in keinem der beiden Faelle wegfallen."""

    def test_auftrag_bei_eingeschaltetem_auszug(self) -> None:
        config.FRESH_DIGEST = True
        self.assertIn(AUFTRAG, self.prompt())

    def test_auftrag_bei_ausgeschaltetem_auszug(self) -> None:
        config.FRESH_DIGEST = False
        self.assertIn(AUFTRAG, self.prompt())

    def test_neustart_hinweis_bleibt_in_beiden_faellen(self) -> None:
        for schalter in (True, False):
            with self.subTest(schalter=schalter):
                config.FRESH_DIGEST = schalter
                self.assertIn("Kontextlimit", self.prompt())

    def test_ohne_schalter_haengt_nichts_am_verlauf(self) -> None:
        """Ausgeschaltet darf die Prompt-Laenge nicht mehr vom Tail abhaengen."""
        config.FRESH_DIGEST = False
        leer = self.prompt()
        self.obs = Observation(alive=False, tail_text="B" * 100000)
        self.assertEqual(self.prompt(), leer)


if __name__ == "__main__":
    unittest.main()
