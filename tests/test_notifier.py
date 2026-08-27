"""Test: eine Meldung darf den Supervisor nie aufhalten - und nicht fluten."""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from claude_watchdog.notifier import NotifySendNotifier, URGENCY_NORMAL  # noqa: E402


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


class FakeUhr:
    """Zeitquelle fuer die Stundengrenze - stellbar, damit kein Test schlaeft."""

    def __init__(self, start: float = 1000.0):
        self.jetzt = start

    def __call__(self) -> float:
        return self.jetzt

    def vor(self, sekunden: float) -> None:
        self.jetzt += sekunden


class ZaehlenderNotifier(NotifySendNotifier):
    """Wie der echte, nur ohne Subprozess: merkt sich, was auf den Desktop ginge."""

    def __init__(self, max_per_hour: int, clock):
        super().__init__(binary="/bin/true", max_per_hour=max_per_hour, clock=clock)
        self.zugestellt: list[tuple[str, str, str]] = []

    def _zustellen(self, title, body, urgency, now):
        self.zugestellt.append((title, body, urgency))


def _titel(n: ZaehlenderNotifier) -> list[str]:
    return [t for t, _, _ in n.zugestellt]


def _metas(n: ZaehlenderNotifier) -> list[str]:
    return [b for _, b, _ in n.zugestellt if "nur im Log" in b]


class TestStundengrenze(unittest.TestCase):
    def test_zweite_meldung_geht_nur_ins_log(self):
        """Grenze 2 heisst zwei Blasen INSGESAMT - eine echte, eine Drossel.

        Der letzte Platz der Stunde gehoert der Drosselmeldung; sonst waere
        die Grenze keine Obergrenze, sondern N plus Hinweis.
        """
        uhr = FakeUhr()
        n = ZaehlenderNotifier(max_per_hour=2, clock=uhr)
        n.send("eins", "a")
        uhr.vor(10)
        n.send("zwei", "b")
        uhr.vor(10)
        n.send("drei", "c")
        self.assertEqual(_titel(n)[:1], ["eins"])
        self.assertNotIn("zwei", _titel(n))
        self.assertNotIn("drei", _titel(n))
        # Die Meta-Meldung ist die letzte, die noch durchgeht.
        self.assertEqual(len(_metas(n)), 1)
        self.assertEqual(n.zugestellt[-1][1], _metas(n)[0])
        self.assertRegex(_metas(n)[0], r"bis \d\d:\d\d nur im Log")

    def test_meta_meldung_genau_einmal(self):
        uhr = FakeUhr()
        n = ZaehlenderNotifier(max_per_hour=2, clock=uhr)
        for i in range(12):
            uhr.vor(5)
            n.send("m%d" % i, "text")
        # Insgesamt zwei Blasen: eine echte Meldung, eine Drosselmeldung.
        self.assertEqual(len(n.zugestellt), 2)
        self.assertEqual(len(_metas(n)), 1)

    def test_nach_stundenablauf_geht_es_wieder(self):
        uhr = FakeUhr()
        n = ZaehlenderNotifier(max_per_hour=2, clock=uhr)
        n.send("eins", "a")
        n.send("zwei", "b")           # gedrosselt (+ Meta)
        self.assertNotIn("zwei", _titel(n))
        uhr.vor(3601)                 # das Fenster ist durchgelaufen
        n.send("vier", "d")
        self.assertIn("vier", _titel(n))
        # und die naechste Drosselperiode meldet sich wieder einmal.
        n.send("fuenf", "e")
        self.assertNotIn("fuenf", _titel(n))
        self.assertEqual(len(_metas(n)), 2)

    def test_grenze_null_ist_kein_deckel(self):
        uhr = FakeUhr()
        n = ZaehlenderNotifier(max_per_hour=0, clock=uhr)
        for i in range(50):
            n.send("m%d" % i, "text")
        self.assertEqual(len(n.zugestellt), 50)
        self.assertEqual(_metas(n), [])

    def test_log_erhaelt_immer_alles(self):
        """Die Grenze gilt nur fuer notify-send, nie fuer das Log."""
        uhr = FakeUhr()
        n = ZaehlenderNotifier(max_per_hour=1, clock=uhr)
        with self.assertLogs("cw.notifier", level="INFO") as protokoll:
            n.send("eins", "a")
            n.send("zwei", "b")
            n.send("drei", "c")
        titel = [getattr(r, "title", None) for r in protokoll.records]
        self.assertEqual(_titel(n)[:1], ["eins"])   # nur eine ging raus
        for erwartet in ("eins", "zwei", "drei"):
            self.assertIn(erwartet, titel)

    def test_negative_grenze_wirkt_wie_null(self):
        uhr = FakeUhr()
        n = ZaehlenderNotifier(max_per_hour=-5, clock=uhr)
        for i in range(5):
            n.send("m%d" % i, "text")
        self.assertEqual(len(n.zugestellt), 5)
        self.assertEqual(_metas(n), [])



class TestGrenzeIstEineGrenze(unittest.TestCase):
    """Die Obergrenze muss in JEDEM Stundenfenster halten, nicht nur im ersten.

    Der Fall, den die erste Fassung nicht rot machte: das Fenster laeuft
    teilweise ab, waehrend die Flut weiterlaeuft. Jeder frei werdende Platz
    oeffnete eine neue Drosselphase mit einer neuen Meta-Blase, und die zaehlte
    nicht mit - aus 4 pro Stunde wurden gemessene 7,2. Danach zaehlte sie mit,
    kam aber zusaetzlich zu den N: 5 statt 4.
    """

    def _flut(self, grenze: int, stunden: int = 5, takt: int = 30):
        uhr = FakeUhr()
        zeiten: list[float] = []

        class Merker(NotifySendNotifier):
            def _zustellen(self, *args, **kwargs) -> None:
                zeiten.append(uhr())

        n = Merker(max_per_hour=grenze, clock=uhr)
        for i in range((stunden * 3600) // takt):
            n.send("Titel %d" % i, "Text", URGENCY_NORMAL)
            uhr.vor(takt)
        return zeiten

    def test_in_keinem_stundenfenster_mehr_als_erlaubt(self) -> None:
        grenze = 4
        zeiten = self._flut(grenze)
        # Ohne diese Zusicherung wuerde der Test auch dann gruen, wenn gar
        # nichts zugestellt wird - und pruefte damit nichts.
        self.assertGreater(len(zeiten), 3, "es wurde praktisch nichts zugestellt")
        for t in zeiten:
            im_fenster = [z for z in zeiten if t - 3600 < z <= t]
            self.assertLessEqual(
                len(im_fenster), grenze,
                "%d Blasen im Stundenfenster bis %.0f s (erlaubt: %d)"
                % (len(im_fenster), t, grenze))

    def test_ohne_grenze_geht_alles_durch(self) -> None:
        zeiten = self._flut(0, stunden=1)
        self.assertEqual(120, len(zeiten))


if __name__ == "__main__":
    unittest.main()
