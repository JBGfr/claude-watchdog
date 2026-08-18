"""Tests fuer die Backoff-Logik (deterministisch ueber injizierten RNG)."""

from __future__ import annotations

import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from claude_watchdog import backoff, config  # noqa: E402
from claude_watchdog.classifier import Classification  # noqa: E402
from claude_watchdog.models import ErrorClass  # noqa: E402


class NoJitter(random.Random):
    """RNG, der immer die Mitte liefert -> Jitter faktisch aus."""

    def uniform(self, a, b):  # noqa: D102
        return 0.0


class MaxJitter(random.Random):
    def uniform(self, a, b):  # noqa: D102
        return b


class TestExponential(unittest.TestCase):
    def test_verdopplung_ohne_jitter(self):
        rng = NoJitter()
        werte = [backoff.exponential(i, base=30, factor=2, cap=1e9, rng=rng)
                 for i in range(5)]
        self.assertEqual(werte, [30.0, 60.0, 120.0, 240.0, 480.0])

    def test_cap_greift(self):
        rng = NoJitter()
        self.assertEqual(backoff.exponential(20, base=30, factor=2, cap=1800, rng=rng),
                         1800.0)

    def test_jitter_bleibt_im_band(self):
        for attempt in range(6):
            wert = backoff.exponential(attempt, base=30, factor=2, cap=1800,
                                       jitter=0.2, rng=MaxJitter())
            basis = min(30 * 2 ** attempt, 1800)
            self.assertLessEqual(wert, basis * 1.2 + 0.01)
            self.assertGreaterEqual(wert, 0.0)

    def test_negativer_versuch_wird_geklemmt(self):
        self.assertEqual(backoff.exponential(-5, base=30, factor=2, rng=NoJitter()),
                         30.0)

    def test_kein_overflow_bei_grossem_versuch(self):
        wert = backoff.exponential(10_000, base=30, factor=2, cap=1800, rng=NoJitter())
        self.assertEqual(wert, 1800.0)


class TestDelayFor(unittest.TestCase):
    def test_usage_limit_wartet_bis_reset(self):
        now = 1_000_000.0
        reset = now + 4200
        cls = Classification(ErrorClass.USAGE_LIMIT, reset_at=reset)
        delay, retry_at = backoff.delay_for(cls, attempt=0, now=now, rng=NoJitter())
        self.assertAlmostEqual(retry_at, reset + config.USAGE_LIMIT_RESET_PADDING, delta=1)
        self.assertAlmostEqual(delay, retry_at - now, delta=1)

    def test_usage_limit_ohne_reset_nutzt_fallback(self):
        now = 1_000_000.0
        cls = Classification(ErrorClass.USAGE_LIMIT, reset_at=None)
        delay, retry_at = backoff.delay_for(cls, attempt=0, now=now, rng=NoJitter())
        self.assertEqual(delay, float(config.USAGE_LIMIT_FALLBACK_WAIT))
        self.assertAlmostEqual(retry_at, now + config.USAGE_LIMIT_FALLBACK_WAIT, delta=1)

    def test_usage_limit_mit_abgelaufenem_reset_nutzt_fallback(self):
        now = 1_000_000.0
        cls = Classification(ErrorClass.USAGE_LIMIT, reset_at=now - 10)
        delay, _ = backoff.delay_for(cls, attempt=0, now=now, rng=NoJitter())
        self.assertEqual(delay, float(config.USAGE_LIMIT_FALLBACK_WAIT))

    def test_rate_limit_respektiert_retry_after(self):
        now = 1_000_000.0
        cls = Classification(ErrorClass.RATE_LIMIT, retry_after=90)
        delay, retry_at = backoff.delay_for(cls, attempt=3, now=now, rng=NoJitter())
        self.assertAlmostEqual(delay, 90 + config.USAGE_LIMIT_RESET_PADDING, delta=0.1)
        self.assertAlmostEqual(retry_at, now + delay, delta=0.1)

    def test_uebrige_klassen_nutzen_exponential(self):
        now = 1_000_000.0
        cls = Classification(ErrorClass.NETWORK)
        delay, _ = backoff.delay_for(cls, attempt=2, now=now, rng=NoJitter())
        self.assertAlmostEqual(delay, config.BACKOFF_BASE * config.BACKOFF_FACTOR ** 2,
                               delta=0.1)


class TestZaehlregeln(unittest.TestCase):
    def test_usage_und_rate_limit_zaehlen_nicht_als_versuch(self):
        for klasse in (ErrorClass.USAGE_LIMIT, ErrorClass.RATE_LIMIT):
            with self.subTest(klasse=klasse):
                self.assertFalse(backoff.counts_as_attempt(Classification(klasse)))

    def test_echte_fehler_zaehlen_als_versuch(self):
        for klasse in (ErrorClass.CRASH, ErrorClass.NETWORK, ErrorClass.API_ERROR,
                       ErrorClass.CONTEXT, ErrorClass.STALLED, ErrorClass.UNKNOWN):
            with self.subTest(klasse=klasse):
                self.assertTrue(backoff.counts_as_attempt(Classification(klasse)))

    def test_awaiting_input_ist_nicht_wiederholbar(self):
        self.assertFalse(backoff.is_retryable(Classification(ErrorClass.AWAITING_INPUT)))

    def test_fehlerklassen_sind_wiederholbar(self):
        for klasse in (ErrorClass.USAGE_LIMIT, ErrorClass.RATE_LIMIT,
                       ErrorClass.API_ERROR, ErrorClass.NETWORK, ErrorClass.CONTEXT,
                       ErrorClass.CRASH, ErrorClass.STALLED, ErrorClass.UNKNOWN):
            with self.subTest(klasse=klasse):
                self.assertTrue(backoff.is_retryable(Classification(klasse)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
