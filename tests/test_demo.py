"""Demo-Modus: erfundene Tasks, nur auf Ansage, keine echten Daten."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from claude_watchdog import demo  # noqa: E402


class SchalterTest(unittest.TestCase):

    def test_aus_ohne_variable(self) -> None:
        umgebung = {k: v for k, v in os.environ.items() if k != "CW_DEMO"}
        with mock.patch.dict(os.environ, umgebung, clear=True):
            self.assertFalse(demo.aktiv())

    def test_werte_die_aus_bedeuten(self) -> None:
        for wert in ("", "0", "false", "no", "  "):
            with mock.patch.dict(os.environ, {"CW_DEMO": wert}):
                self.assertFalse(demo.aktiv(), wert)

    def test_an(self) -> None:
        with mock.patch.dict(os.environ, {"CW_DEMO": "1"}):
            self.assertTrue(demo.aktiv())


class DatenTest(unittest.TestCase):

    def setUp(self) -> None:
        self.tasks = demo.tasks()

    def test_keine_echten_pfade(self) -> None:
        """Nichts aus einem echten Home darf in ein Bild geraten."""
        for t in self.tasks:
            self.assertTrue(t.cwd.startswith("/home/user/"), t.cwd)
            self.assertNotIn("kali", t.cwd.lower())

    def test_kennungen_sind_erfunden(self) -> None:
        for t in self.tasks:
            self.assertEqual(1, len(set(t.id)), t.id)

    def test_zeigt_die_interessanten_zustaende(self) -> None:
        """Ein Bild mit lauter 'running' erklaert nichts."""
        zustaende = {t.status.value for t in self.tasks}
        self.assertIn("waiting_for_limit", zustaende)
        self.assertIn("blocked", zustaende)
        self.assertGreaterEqual(len(zustaende), 4)

    def test_beide_betriebsarten_kommen_vor(self) -> None:
        self.assertEqual({"managed", "observed"},
                         {t.mode.value for t in self.tasks})

    def test_zwei_laeufe_sind_gleich(self) -> None:
        self.assertEqual([t.title for t in self.tasks],
                         [t.title for t in demo.tasks()])


if __name__ == "__main__":
    unittest.main()
