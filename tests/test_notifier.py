"""Test: eine Meldung darf den Supervisor nie aufhalten."""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from claude_watchdog.notifier import NotifySendNotifier  # noqa: E402


class TestMeldungBlockiertNicht(unittest.TestCase):
    def test_haengendes_notify_send_haelt_nicht_auf(self):
        """Der eigentliche Fall: notify-send blockiert auf D-Bus.

        Nachgestellt mit 'sleep 30'. Frueher lief das ueber
        subprocess.run(timeout=10) und kostete zehn Sekunden je Meldung —
        bei einem Poll-Takt von 15 s.
        """
        n = NotifySendNotifier(binary="/bin/sleep")
        t0 = time.monotonic()
        n.send("30", "egal")          # argv: sleep -a ... -u ... 30 egal
        dauer = time.monotonic() - t0
        self.addCleanup(self._abraeumen, n)
        self.assertLess(dauer, 1.0, "send() hat gewartet: %.2f s" % dauer)
        self.assertEqual(len(n._offen), 1)

    def test_fertige_aufrufe_werden_eingesammelt(self):
        n = NotifySendNotifier(binary="/bin/true")
        n.send("a", "b")
        self.assertEqual(len(n._offen), 1)
        n._offen[0][1].wait()
        n.send("c", "d")              # sammelt den ersten mit ein
        self.assertEqual(len(n._offen), 1)

    def test_haenger_wird_nach_der_frist_abgeraeumt(self):
        n = NotifySendNotifier(binary="/bin/sleep")
        n.send("30", "egal")
        self.addCleanup(self._abraeumen, n)
        p = n._offen[0][1]
        # Startzeit zurueckdatieren, statt eine halbe Minute zu warten.
        n._offen = [(time.time() - n.HAENGT_AB - 1, p)]
        n.send("30", "egal")
        self.assertIsNotNone(p.poll(), "haengender Aufruf wurde nicht beendet")
        self.assertEqual(len(n._offen), 1)   # nur der neue

    def test_fehlendes_programm_wird_gemeldet_nicht_geworfen(self):
        n = NotifySendNotifier(binary="/gibt/es/nicht")
        n.send("a", "b")              # darf nicht werfen
        self.assertEqual(n._offen, [])

    def _abraeumen(self, n):
        for _, p in n._offen:
            if p.poll() is None:
                p.kill()
            p.wait()
        n._offen = []


if __name__ == "__main__":
    unittest.main()
