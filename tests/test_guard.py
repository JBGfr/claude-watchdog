"""Test der Sicherheitsinvariante: observed-Tasks werden nie angefasst."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from claude_watchdog.models import (  # noqa: E402
    Action,
    Decision,
    ErrorClass,
    INTRUSIVE_ACTIONS,
    Mode,
    Observation,
    Status,
    Task,
)
from claude_watchdog.recovery import (  # noqa: E402
    decide,
    enforce_mode_guard,
    launch_postponed_until,
)


def make(mode: Mode, **kw) -> Task:
    defaults = dict(id="t1", title="Test", cwd="/tmp", mode=mode,
                    status=Status.RUNNING, original_prompt="tu was",
                    session_id="11111111-2222-3333-4444-555555555555")
    defaults.update(kw)
    return Task(**defaults)


class TestModeGuard(unittest.TestCase):
    def test_alle_eingreifenden_aktionen_werden_fuer_observed_entschaerft(self):
        for action in INTRUSIVE_ACTIONS:
            with self.subTest(action=action):
                task = make(Mode.OBSERVED)
                out = enforce_mode_guard(task, Decision(action=action, reason="x"))
                self.assertIs(out.action, Action.NOTIFY)
                self.assertFalse(out.counts_as_attempt)

    def test_managed_bleibt_unveraendert(self):
        for action in INTRUSIVE_ACTIONS:
            with self.subTest(action=action):
                task = make(Mode.MANAGED)
                original = Decision(action=action, reason="x")
                self.assertIs(enforce_mode_guard(task, original).action, action)

    def test_nicht_eingreifende_aktionen_bleiben(self):
        for action in (Action.NONE, Action.NOTIFY, Action.SCHEDULE,
                       Action.FAIL, Action.COMPLETE):
            with self.subTest(action=action):
                task = make(Mode.OBSERVED)
                out = enforce_mode_guard(task, Decision(action=action, reason="x"))
                self.assertIs(out.action, action)


class TestDecide(unittest.TestCase):
    def test_observed_crash_fuehrt_nur_zu_notify(self):
        task = make(Mode.OBSERVED)
        obs = Observation(alive=False, tail_text="Segmentation fault")
        decision = decide(task, obs, stalled=False, now=1_000_000.0)
        self.assertIs(decision.action, Action.NOTIFY)

    def test_managed_crash_fuehrt_zu_resume(self):
        task = make(Mode.MANAGED)
        obs = Observation(alive=False, tail_text="Segmentation fault")
        decision = decide(task, obs, stalled=False, now=1_000_000.0)
        self.assertIs(decision.action, Action.RESUME)
        self.assertIs(decision.error_class, ErrorClass.CRASH)

    def test_awaiting_input_wird_nie_beantwortet(self):
        for mode in (Mode.MANAGED, Mode.OBSERVED):
            with self.subTest(mode=mode):
                task = make(mode)
                obs = Observation(alive=True, tail_text="Do you want to proceed? [y/n]")
                decision = decide(task, obs, now=1_000_000.0)
                self.assertIs(decision.action, Action.NOTIFY)
                self.assertIs(decision.error_class, ErrorClass.AWAITING_INPUT)

    def test_no_auto_resume_verhindert_neustart(self):
        task = make(Mode.MANAGED, no_auto_resume=True)
        obs = Observation(alive=False, tail_text="fetch failed: ECONNRESET")
        decision = decide(task, obs, now=1_000_000.0)
        self.assertIs(decision.action, Action.NOTIFY)

    def test_max_attempts_fuehrt_zu_fail(self):
        task = make(Mode.MANAGED, attempts=5, max_attempts=5)
        obs = Observation(alive=False, tail_text="fetch failed: ECONNRESET")
        decision = decide(task, obs, now=1_000_000.0)
        self.assertIs(decision.action, Action.FAIL)

    def test_wiederholtes_scheitern_an_gleicher_stelle_fuehrt_zu_fail(self):
        task = make(Mode.MANAGED, same_marker_count=3)
        obs = Observation(alive=False, tail_text="fetch failed: ECONNRESET")
        decision = decide(task, obs, now=1_000_000.0)
        self.assertIs(decision.action, Action.FAIL)

    def test_kontextfehler_startet_frisch_statt_zu_resumen(self):
        task = make(Mode.MANAGED)
        obs = Observation(alive=False, tail_text="prompt is too long: 210000 tokens")
        decision = decide(task, obs, now=1_000_000.0)
        self.assertIs(decision.action, Action.RESTART_FRESH)

    def test_usage_limit_plant_statt_zu_starten(self):
        task = make(Mode.MANAGED)
        obs = Observation(alive=False, events=[{
            "type": "rate_limit_event",
            "rate_limit_info": {"status": "rejected", "resetsAt": 1_003_600},
        }])
        decision = decide(task, obs, now=1_000_000.0)
        self.assertIs(decision.action, Action.SCHEDULE)
        self.assertIs(decision.error_class, ErrorClass.USAGE_LIMIT)
        self.assertFalse(decision.counts_as_attempt)
        self.assertGreater(decision.retry_at, 1_003_000)

    def test_fortschritt_schlaegt_alte_fehlerevidenz(self):
        task = make(Mode.MANAGED)
        obs = Observation(alive=True, progressed=True, tail_text="Segmentation fault")
        decision = decide(task, obs, now=1_000_000.0)
        self.assertIs(decision.action, Action.NONE)

    def test_erschoepftes_budget_startet_nicht(self):
        task = make(Mode.MANAGED, status=Status.PENDING, session_id=None)
        obs = Observation(alive=False)
        decision = decide(task, obs, now=1_000_000.0, budget_available=False)
        self.assertIs(decision.action, Action.SCHEDULE)


class TestBackoffWirdEingehalten(unittest.TestCase):
    """Ein berechnetes Backoff muss den Start auch wirklich verzoegern."""

    def test_termin_in_der_zukunft_verschiebt_den_start(self):
        d = Decision(action=Action.RESUME, reason="x", delay=66.0)
        self.assertAlmostEqual(launch_postponed_until(d, 1_000_000.0),
                               1_000_066.0, delta=0.1)

    def test_ohne_wartezeit_wird_sofort_gestartet(self):
        d = Decision(action=Action.START, reason="Erststart")
        self.assertIsNone(launch_postponed_until(d, 1_000_000.0))

    def test_abgelaufener_termin_haelt_nicht_auf(self):
        d = Decision(action=Action.RESUME, reason="x", retry_at=999_999.0)
        self.assertIsNone(launch_postponed_until(d, 1_000_000.0))

    def test_absturz_bekommt_eine_wartezeit(self):
        # Ohne Backoff wuerde ein Absturz jeden Durchlauf neu versucht.
        task = make(Mode.MANAGED, attempts=1)
        obs = Observation(alive=False, exit_code=-9, tail_text="Segmentation fault")
        decision = decide(task, obs, now=1_000_000.0)
        self.assertIs(decision.action, Action.RESUME)
        self.assertGreater(decision.delay, 0.0)
        self.assertIsNotNone(launch_postponed_until(decision, 1_000_000.0))

    def test_faelliger_wiederanlauf_startet_ohne_weitere_wartezeit(self):
        task = make(Mode.MANAGED, status=Status.WAITING_FOR_LIMIT,
                    next_retry_at=999_999.0)
        obs = Observation(alive=False)
        decision = decide(task, obs, now=1_000_000.0)
        self.assertIs(decision.action, Action.RESUME)
        self.assertIsNone(launch_postponed_until(decision, 1_000_000.0))

    def test_gespeicherte_klasse_ueberlebt_die_wartezeit(self):
        # Nach dem Backoff ist der Exit-Code laengst verbraucht - der Grund
        # soll trotzdem CRASH heissen und nicht auf UNKNOWN verwaschen.
        task = make(Mode.MANAGED, status=Status.WAITING_FOR_LIMIT,
                    next_retry_at=999_999.0, last_error_class="CRASH")
        decision = decide(task, Observation(alive=False), now=1_000_000.0)
        self.assertIs(decision.action, Action.RESUME)
        self.assertIs(decision.error_class, ErrorClass.CRASH)
        self.assertIn("CRASH", decision.reason)

    def test_gemerkter_kontextfehler_setzt_ebenfalls_frisch_an(self):
        task = make(Mode.MANAGED, status=Status.WAITING_FOR_LIMIT,
                    next_retry_at=999_999.0, last_error_class="CONTEXT")
        decision = decide(task, Observation(alive=False), now=1_000_000.0)
        self.assertIs(decision.action, Action.RESTART_FRESH)

    def test_kaputte_gespeicherte_klasse_wird_ignoriert(self):
        task = make(Mode.MANAGED, status=Status.WAITING_FOR_LIMIT,
                    next_retry_at=999_999.0, last_error_class="quatsch")
        decision = decide(task, Observation(alive=False), now=1_000_000.0)
        self.assertIs(decision.action, Action.RESUME)

    def test_kontextfehler_setzt_auch_nach_der_wartezeit_frisch_an(self):
        # Sonst wuerde der verzoegerte Wiederanlauf stumpf resumen und sofort
        # wieder ins volle Kontextfenster laufen.
        task = make(Mode.MANAGED, status=Status.WAITING_FOR_LIMIT,
                    next_retry_at=999_999.0)
        obs = Observation(alive=False, tail_text="prompt is too long: 210000 tokens")
        decision = decide(task, obs, now=1_000_000.0)
        self.assertIs(decision.action, Action.RESTART_FRESH)


class TestObservedUntaetig(unittest.TestCase):
    """Eine offene, aber untaetige Session ist kein Fehler und keine Meldung."""

    def test_untaetige_observed_session_meldet_nicht(self):
        # Vor der Regel wurde daraus 'resume' -> Mode-Guard -> notify, also
        # alle STALL_SECONDS ein Popup fuer jede offene Session.
        obs = Observation(alive=True, progressed=False, idle_seconds=1200.0)
        decision = decide(make(Mode.OBSERVED), obs, stalled=True, now=1_000_000.0)
        self.assertIs(decision.action, Action.NONE)
        self.assertIs(decision.error_class, ErrorClass.NONE)

    def test_managed_haengt_weiterhin_und_wird_fortgesetzt(self):
        obs = Observation(alive=True, progressed=False, idle_seconds=1200.0)
        decision = decide(make(Mode.MANAGED), obs, stalled=True, now=1_000_000.0)
        self.assertIs(decision.action, Action.RESUME)
        self.assertIs(decision.error_class, ErrorClass.STALLED)

    def test_rueckfrage_wird_weiterhin_gemeldet(self):
        # Die Ausnahme von der Regel: das betrifft den Benutzer wirklich.
        obs = Observation(alive=True, progressed=False, idle_seconds=1200.0,
                          tail_text="Do you want to proceed? [y/n]")
        decision = decide(make(Mode.OBSERVED), obs, stalled=True, now=1_000_000.0)
        self.assertIs(decision.action, Action.NOTIFY)
        self.assertIs(decision.error_class, ErrorClass.AWAITING_INPUT)

    def test_echter_fehler_wird_weiterhin_gemeldet(self):
        obs = Observation(alive=True, progressed=False, idle_seconds=1200.0,
                          tail_text="Claude usage limit reached")
        decision = decide(make(Mode.OBSERVED), obs, stalled=True, now=1_000_000.0)
        self.assertIsNot(decision.action, Action.NONE)


class TestObservedSessionEnde(unittest.TestCase):
    """Eine geschlossene observed-Session wird abgeschlossen, nicht ewig gemeldet."""

    @staticmethod
    def gone_obs(**kw) -> Observation:
        defaults = dict(alive=False, progressed=False, cli_usable=True,
                        idle_seconds=3600.0)
        defaults.update(kw)
        return Observation(**defaults)

    def test_verschwundene_session_wird_abgeschlossen(self):
        decision = decide(make(Mode.OBSERVED), self.gone_obs(), now=1_000_000.0)
        self.assertIs(decision.action, Action.COMPLETE)
        self.assertIn("Session beendet", decision.reason)

    def test_letzter_fehler_steht_in_der_meldung(self):
        obs = self.gone_obs(tail_text="Claude usage limit reached")
        decision = decide(make(Mode.OBSERVED), obs, now=1_000_000.0)
        self.assertIs(decision.action, Action.COMPLETE)
        self.assertIn("USAGE_LIMIT", decision.reason)

    def test_offene_rueckfrage_haelt_tote_session_nicht_auf(self):
        # Ohne die Regel wuerde hier ewig "wartet auf deine Eingabe" gemeldet.
        obs = self.gone_obs(tail_text="Do you want to proceed? [y/n]")
        decision = decide(make(Mode.OBSERVED), obs, now=1_000_000.0)
        self.assertIs(decision.action, Action.COMPLETE)

    def test_ohne_cli_auskunft_keine_annahme(self):
        # `claude agents --json` nicht erreichbar -> "kennt die Session nicht"
        # ist wertlos, also beim alten Verhalten bleiben.
        obs = self.gone_obs(cli_usable=False, tail_text="Segmentation fault")
        decision = decide(make(Mode.OBSERVED), obs, now=1_000_000.0)
        self.assertIsNot(decision.action, Action.COMPLETE)

    def test_kurzer_aussetzer_reicht_nicht(self):
        obs = self.gone_obs(idle_seconds=5.0, tail_text="Segmentation fault")
        decision = decide(make(Mode.OBSERVED), obs, now=1_000_000.0)
        self.assertIsNot(decision.action, Action.COMPLETE)

    def test_laufende_session_bleibt_unberuehrt(self):
        obs = self.gone_obs(alive=True)
        decision = decide(make(Mode.OBSERVED), obs, now=1_000_000.0)
        self.assertIsNot(decision.action, Action.COMPLETE)

    def test_neuer_eintrag_im_transkript_zaehlt_als_lebenszeichen(self):
        obs = self.gone_obs(progressed=True)
        decision = decide(make(Mode.OBSERVED), obs, now=1_000_000.0)
        self.assertIsNot(decision.action, Action.COMPLETE)

    def test_managed_wird_weiterhin_fortgesetzt(self):
        # Die Regel gilt nur fuer observed - managed soll gerade wiederanlaufen.
        obs = self.gone_obs(tail_text="fetch failed: ECONNRESET")
        decision = decide(make(Mode.MANAGED), obs, now=1_000_000.0)
        self.assertIs(decision.action, Action.RESUME)


if __name__ == "__main__":
    unittest.main(verbosity=2)
