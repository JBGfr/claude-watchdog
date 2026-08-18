"""Tests fuer die Klassifikation: Beispielausgabe rein, erwartete Klasse raus."""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from claude_watchdog.classifier import (  # noqa: E402
    classify,
    classify_structured,
    classify_text,
    parse_epoch,
    parse_reset_at,
    parse_retry_after,
    rate_limit_blocks,
)
from claude_watchdog.models import ErrorClass  # noqa: E402

#: Warnhinweis, wie ihn das CLI real liefert (Auslastung hoch, aber erlaubt).
WARN_EVENT = {
    "type": "rate_limit_event",
    "rate_limit_info": {
        "status": "allowed_warning",
        "resetsAt": 1785294000,
        "rateLimitType": "five_hour",
        "utilization": 0.98,
        "isUsingOverage": False,
        "surpassedThreshold": 0.9,
    },
    "uuid": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
}

SUCCESS_EVENT = {"type": "result", "subtype": "success", "is_error": False,
                 "total_cost_usd": 0.0276749}


class TestStructured(unittest.TestCase):
    """Stufe 1: strukturierte Events schlagen den Regex-Fallback."""

    def test_rate_limit_event_blockiert(self):
        reset = time.time() + 3600
        events = [{
            "type": "rate_limit_event",
            "rate_limit_info": {
                "status": "rejected",
                "resetsAt": int(reset),
                "rateLimitType": "five_hour",
            },
        }]
        result = classify_structured(events)
        self.assertIsNotNone(result)
        self.assertIs(result.error_class, ErrorClass.USAGE_LIMIT)
        self.assertAlmostEqual(result.reset_at, int(reset), delta=1)
        self.assertEqual(result.source, "structured")

    def test_rate_limit_event_allowed_ist_kein_fehler(self):
        events = [{
            "type": "rate_limit_event",
            "rate_limit_info": {"status": "allowed", "resetsAt": 1785294000},
        }]
        self.assertIsNone(classify_structured(events))

    def test_allowed_warning_ist_kein_fehler(self):
        # 98 % Auslastung wird gemeldet, die Anfrage laeuft aber durch.
        self.assertIsNone(classify_structured([WARN_EVENT]))

    def test_blocking_status_erkennung(self):
        self.assertIsNone(rate_limit_blocks({"status": "allowed"}))
        self.assertIsNone(rate_limit_blocks({"status": "allowed_warning"}))
        self.assertIsNone(rate_limit_blocks({"status": "ALLOWED_WARNING"}))
        self.assertIsNone(rate_limit_blocks({}))
        self.assertEqual(rate_limit_blocks({"status": "rejected"}), "rejected")
        self.assertEqual(rate_limit_blocks({"status": "blocked"}), "blocked")

    def test_juengeres_result_schlaegt_aelteren_warnhinweis(self):
        # Reale Reihenfolge aus dem Smoketest: erst der Warnhinweis, danach
        # der erfolgreiche Abschluss. Der Abschluss muss gewinnen.
        result = classify_structured([WARN_EVENT, SUCCESS_EVENT])
        self.assertIs(result.error_class, ErrorClass.NONE)
        self.assertEqual(result.detail, "result:success")

    def test_juengeres_rate_limit_schlaegt_aelteres_result(self):
        blocked = {"type": "rate_limit_event",
                   "rate_limit_info": {"status": "rejected",
                                       "resetsAt": int(time.time() + 3600)}}
        result = classify_structured([SUCCESS_EVENT, blocked])
        self.assertIs(result.error_class, ErrorClass.USAGE_LIMIT)

    def test_result_success(self):
        events = [{"type": "result", "subtype": "success", "is_error": False}]
        result = classify_structured(events)
        self.assertIs(result.error_class, ErrorClass.NONE)

    def test_result_api_error_429_wird_rate_limit(self):
        events = [{"type": "result", "subtype": "error_during_execution",
                   "is_error": True, "api_error_status": "429"}]
        result = classify_structured(events)
        self.assertIs(result.error_class, ErrorClass.RATE_LIMIT)

    def test_result_api_error_529_wird_api_error(self):
        events = [{"type": "result", "subtype": "error_during_execution",
                   "is_error": True, "api_error_status": 529}]
        result = classify_structured(events)
        self.assertIs(result.error_class, ErrorClass.API_ERROR)

    def test_result_max_turns_wird_stalled(self):
        events = [{"type": "result", "subtype": "error_max_turns", "is_error": True}]
        result = classify_structured(events)
        self.assertIs(result.error_class, ErrorClass.STALLED)

    def test_api_error_message_flag(self):
        events = [{"type": "assistant", "isApiErrorMessage": True,
                   "message": {"content": "API Error: Connection reset"}}]
        result = classify_structured(events)
        self.assertIn(result.error_class, (ErrorClass.API_ERROR, ErrorClass.NETWORK))


class TestTextPatterns(unittest.TestCase):
    """Stufe 2: die zentrale Mustertabelle."""

    CASES = [
        ("Claude usage limit reached. Your limit will reset at 3pm.", ErrorClass.USAGE_LIMIT),
        ("5-hour limit reached", ErrorClass.USAGE_LIMIT),
        ("You've reached your weekly usage limit", ErrorClass.USAGE_LIMIT),
        ("Error: rate_limit_error - too many requests", ErrorClass.RATE_LIMIT),
        ("HTTP 429 Too Many Requests", ErrorClass.RATE_LIMIT),
        ("API Error: 529 overloaded_error", ErrorClass.API_ERROR),
        ("Internal server error while contacting the API", ErrorClass.API_ERROR),
        ("fetch failed: ECONNRESET", ErrorClass.NETWORK),
        ("getaddrinfo EAI_AGAIN api.anthropic.com", ErrorClass.NETWORK),
        ("connection refused", ErrorClass.NETWORK),
        ("prompt is too long: 210000 tokens > 200000 maximum", ErrorClass.CONTEXT),
        ("stop_reason: model_context_window_exceeded", ErrorClass.CONTEXT),
        ("Compaction failed, unable to continue", ErrorClass.CONTEXT),
        ("Do you want to proceed with this edit? [y/n]", ErrorClass.AWAITING_INPUT),
        ("Permission required to run Bash", ErrorClass.AWAITING_INPUT),
        ("STATUS: CEO-BLOCKIERT: Variante A oder B?", ErrorClass.AWAITING_INPUT),
        ("FATAL ERROR: JavaScript heap out of memory", ErrorClass.CRASH),
        ("Segmentation fault", ErrorClass.CRASH),
    ]

    def test_alle_muster(self):
        for text, expected in self.CASES:
            with self.subTest(text=text):
                result = classify_text(text)
                self.assertIs(result.error_class, expected,
                              f"{text!r} -> {result.error_class} statt {expected}")

    def test_unauffaelliger_text_ist_unknown(self):
        result = classify_text("Reading file src/main.py ... done")
        self.assertIs(result.error_class, ErrorClass.UNKNOWN)

    def test_usage_limit_hat_vorrang_vor_rate_limit(self):
        # Enthaelt beide Begriffe - die spezifischere Klasse muss gewinnen.
        result = classify_text("Claude usage limit reached (rate limit applies)")
        self.assertIs(result.error_class, ErrorClass.USAGE_LIMIT)

    def test_evidence_wird_mitgeliefert(self):
        result = classify_text("blah blah Segmentation fault blah")
        self.assertIn("Segmentation fault", result.evidence)

    def test_ceo_sentinel_in_deutschem_ergebnisblock(self):
        # Der Flotten-Sentinel muss auch mitten im Berichtsblock greifen -
        # die generischen englischen Muster treffen deutsche Berichte nicht.
        tail = (
            "=== ERGEBNIS ===\n"
            "STATUS: CEO-BLOCKIERT: Soll die Migration auch alte Eintraege umfassen?\n"
            "BRANCH: feature/ceo-migration\nTESTS: noch nicht gelaufen\nPR: keiner\n"
        )
        result = classify_text(tail)
        self.assertIs(result.error_class, ErrorClass.AWAITING_INPUT)

    def test_ceo_sentinel_ignoriert_instruktionstext(self):
        # Das Wort steht in den Auftrags-Instruktionen jedes Workers - ein
        # FERTIG-Bericht mit zitierter Instruktion darf NICHT blockiert wirken.
        tail = (
            "Auftrag: Unklares als CEO-BLOCKIERT melden.\n"
            "STATUS: FERTIG | CEO-BLOCKIERT waere die Alternative gewesen\n"
            "=== ERGEBNIS ===\nSTATUS: FERTIG\nPR: #2\n"
        )
        result = classify_text(tail)
        self.assertIsNot(result.error_class, ErrorClass.AWAITING_INPUT)


class TestZeitParser(unittest.TestCase):
    def test_epoch_sekunden(self):
        self.assertAlmostEqual(parse_epoch(1785294000), 1785294000, delta=0.1)

    def test_epoch_millisekunden(self):
        self.assertAlmostEqual(parse_epoch(1785294000000), 1785294000, delta=0.1)

    def test_epoch_unplausibel(self):
        self.assertIsNone(parse_epoch(42))
        self.assertIsNone(parse_epoch(None))
        self.assertIsNone(parse_epoch("keine zahl"))

    def test_reset_aus_epoch(self):
        now = 1785200000.0
        got = parse_reset_at(f"resets at {int(now) + 7200}", now=now)
        self.assertAlmostEqual(got, now + 7200, delta=1)

    def test_reset_aus_relativer_dauer(self):
        now = 1785200000.0
        got = parse_reset_at("try again in 2h 30m", now=now)
        self.assertAlmostEqual(got, now + 2 * 3600 + 30 * 60, delta=1)

    def test_reset_aus_uhrzeit_in_der_zukunft(self):
        now = 1785200000.0
        got = parse_reset_at("Your limit resets at 11pm", now=now)
        self.assertIsNotNone(got)
        self.assertGreater(got, now)

    def test_reset_ohne_angabe(self):
        self.assertIsNone(parse_reset_at("limit reached", now=1785200000.0))

    def test_reset_ohne_das_wort_at(self):
        """Die Formulierung, die Claude Code wirklich benutzt.

        Woertlich aus dem Transkript von Session 0eeaf952:

            You've hit your session limit · resets 5am (Europe/Berlin)

        Frueher verlangte das Muster ein "at" und fand hier nichts. Ohne
        Reset-Zeit greift USAGE_LIMIT_FALLBACK_WAIT (3600 s): eine Sitzung,
        die um 22 Uhr ans Limit laeuft, waere bis 5 Uhr sieben Mal
        vergeblich neu gestartet worden, statt einmal zu warten.
        """
        import datetime
        now = datetime.datetime(2026, 7, 31, 23, 0).timestamp()
        got = parse_reset_at(
            "You've hit your session limit \u00b7 resets 5am (Europe/Berlin)",
            now=now)
        self.assertIsNotNone(got, "Reset-Zeit muss auch ohne 'at' erkannt werden")
        self.assertEqual(datetime.datetime.fromtimestamp(got).hour, 5)
        self.assertGreater(got, now)

    def test_reset_ohne_at_mit_24h_zeit(self):
        import datetime
        now = datetime.datetime(2026, 7, 31, 9, 0).timestamp()
        got = parse_reset_at("resets 17:30", now=now)
        self.assertIsNotNone(got)
        d = datetime.datetime.fromtimestamp(got)
        self.assertEqual((d.hour, d.minute), (17, 30))

    def test_blosse_zahl_hinter_resets_ist_keine_uhrzeit(self):
        """Sonst wuerde 'resets 3 times' die 3 zu einer Uhrzeit machen.

        Ohne "at" muss deshalb eine Minutenangabe oder am/pm dabei sein.
        """
        now = 1785200000.0
        self.assertIsNone(parse_reset_at("der Zaehler resets 3 mal taeglich", now=now))
        self.assertIsNone(parse_reset_at("resets 3 times", now=now))

    def test_retry_after(self):
        self.assertEqual(parse_retry_after('"retry-after": 42'), 42.0)
        self.assertIsNone(parse_retry_after("nothing here"))


class TestGesamtklassifikation(unittest.TestCase):
    def test_strukturiert_schlaegt_text(self):
        events = [{
            "type": "rate_limit_event",
            "rate_limit_info": {"status": "rejected", "resetsAt": int(time.time() + 60)},
        }]
        result = classify(events, text="connection refused")
        self.assertIs(result.error_class, ErrorClass.USAGE_LIMIT)

    def test_exit_code_als_letzte_instanz(self):
        result = classify([], text="", exit_code=137)
        self.assertIs(result.error_class, ErrorClass.CRASH)
        self.assertIn("137", result.detail)

    def test_stall_wenn_sonst_nichts(self):
        result = classify([], text="", exit_code=None, stalled=True)
        self.assertIs(result.error_class, ErrorClass.STALLED)

    def test_ohne_evidenz_unknown(self):
        result = classify([], text="", exit_code=0)
        self.assertIs(result.error_class, ErrorClass.UNKNOWN)

    def test_erfolgreiches_result_bleibt_none(self):
        events = [{"type": "result", "subtype": "success", "is_error": False}]
        result = classify(events, text="", exit_code=0)
        self.assertIs(result.error_class, ErrorClass.NONE)

    def test_smoketest_sequenz_gilt_als_erfolg(self):
        # Der Fall, an dem der Watchdog real gescheitert ist: Warnhinweis im
        # Log, danach erfolgreicher Abschluss - der Task ist fertig, nicht
        # limitiert.
        result = classify([WARN_EVENT, SUCCESS_EVENT],
                          text="Die Datei hello.txt wurde erstellt.", exit_code=0)
        self.assertIs(result.error_class, ErrorClass.NONE)
        self.assertEqual(result.detail, "result:success")

    def test_erfolg_schlaegt_alten_limit_text_im_tail(self):
        # Alter Limit-Hinweis steht noch im Tail-Text, das Ergebnis ist aber da.
        result = classify([SUCCESS_EVENT],
                          text="Claude usage limit reached. Resets at 5am.",
                          exit_code=0)
        self.assertIs(result.error_class, ErrorClass.NONE)


if __name__ == "__main__":
    unittest.main(verbosity=2)
