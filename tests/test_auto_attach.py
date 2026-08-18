"""Tests fuer das automatische Aufnehmen laufender Sessions."""

from __future__ import annotations

import io
import sys
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from claude_watchdog import config, detector  # noqa: E402
from claude_watchdog.daemon import Watchdog  # noqa: E402
from claude_watchdog.models import Mode, Status, Task  # noqa: E402


class FakeRegistry:
    """Registry-Ersatz ohne Datenbank."""

    def __init__(self, bekannt: dict[str, Task] | None = None):
        self.bekannt = bekannt or {}
        self.hinzugefuegt: list[Task] = []
        self.aktualisiert: list[Task] = []
        self._n = 0

    def new_id(self) -> str:
        self._n += 1
        return f"id{self._n:04d}"

    def get_by_session(self, session_id: str):
        return self.bekannt.get(session_id)

    def add(self, task: Task) -> Task:
        self.hinzugefuegt.append(task)
        self.bekannt[task.session_id] = task
        return task

    def update(self, task: Task) -> Task:
        self.aktualisiert.append(task)
        self.bekannt[task.session_id] = task
        return task


class FakeAgents:
    def __init__(self, sessions: dict):
        self._sessions = sessions

    def all(self) -> dict:
        return self._sessions

    def get(self, session_id):
        return self._sessions.get(session_id)

    @property
    def usable(self) -> bool:
        return True


def build(sessions: dict, bekannt: dict | None = None) -> tuple[Watchdog, FakeRegistry]:
    reg = FakeRegistry(bekannt)
    wd = Watchdog(registry=reg, dry_run=True)
    wd.agents = FakeAgents(sessions)
    return wd, reg


SESSION = {"sessionId": "aaaa1111", "cwd": "/home/user/Desktop",
           "status": "busy", "pid": 4242, "name": "desktop-1"}


class TestAutoAttach(unittest.TestCase):
    def setUp(self):
        self._an = config.AUTO_ATTACH
        self._dirs = list(config.AUTO_ATTACH_DIRS)
        config.AUTO_ATTACH = True
        config.AUTO_ATTACH_DIRS = []

    def tearDown(self):
        config.AUTO_ATTACH = self._an
        config.AUTO_ATTACH_DIRS = self._dirs

    def test_unbekannte_session_wird_aufgenommen(self):
        wd, reg = build({"aaaa1111": SESSION})
        self.assertEqual(wd.auto_attach(), 1)
        task = reg.hinzugefuegt[0]
        self.assertIs(task.mode, Mode.OBSERVED)
        self.assertIs(task.status, Status.RUNNING)
        self.assertEqual(task.session_id, "aaaa1111")
        self.assertEqual(task.cwd, "/home/user/Desktop")
        self.assertEqual(task.pid, 4242)
        self.assertEqual(task.title, "desktop-1")

    def test_nie_als_managed(self):
        # Fremde Sessions duerfen niemals angefasst werden - observed ist die
        # einzige zulaessige Betriebsart fuers Auto-Attach.
        wd, reg = build({"aaaa1111": SESSION})
        wd.auto_attach()
        self.assertTrue(all(t.mode is Mode.OBSERVED for t in reg.hinzugefuegt))

    def test_bekannte_session_wird_uebergangen(self):
        vorhanden = Task(id="x", title="da", cwd="/tmp", mode=Mode.OBSERVED,
                         status=Status.RUNNING, session_id="aaaa1111")
        wd, reg = build({"aaaa1111": SESSION}, {"aaaa1111": vorhanden})
        self.assertEqual(wd.auto_attach(), 0)

    def test_abgeschlossene_session_wird_nicht_wieder_aufgenommen(self):
        # Sonst: Auto-Attach legt an -> Regel "Session beendet" schliesst ab ->
        # Auto-Attach legt wieder an. Endlosschleife.
        #
        # Die pid im Fixture ist erfunden; ohne ausdrueckliches Abschalten
        # haenge der Test daran, dass es sie auf dem Rechner zufaellig nicht
        # gibt. Seit der Wiederaufnahme (siehe WiederaufnahmeTest) entscheidet
        # genau diese Pruefung, also wird sie hier festgelegt.
        erledigt = Task(id="x", title="fertig", cwd="/tmp", mode=Mode.OBSERVED,
                        status=Status.DONE, session_id="aaaa1111")
        wd, reg = build({"aaaa1111": SESSION}, {"aaaa1111": erledigt})
        with unittest.mock.patch.object(detector, "pid_alive",
                                        lambda _p: False):
            self.assertEqual(wd.auto_attach(), 0)

    def test_ohne_arbeitsverzeichnis_kein_attach(self):
        ohne = dict(SESSION); ohne.pop("cwd")
        wd, reg = build({"aaaa1111": ohne})
        self.assertEqual(wd.auto_attach(), 0)

    def test_abgeschaltet_passiert_nichts(self):
        config.AUTO_ATTACH = False
        wd, reg = build({"aaaa1111": SESSION})
        self.assertEqual(wd.auto_attach(), 0)

    def test_nur_freigegebene_verzeichnisse(self):
        config.AUTO_ATTACH_DIRS = ["/home/user/Desktop"]
        drinnen = dict(SESSION, cwd="/home/user/Desktop/projekt")
        draussen = dict(SESSION, sessionId="bbbb2222", cwd="/etc")
        wd, reg = build({"aaaa1111": drinnen, "bbbb2222": draussen})
        self.assertEqual(wd.auto_attach(), 1)
        self.assertEqual(reg.hinzugefuegt[0].cwd, "/home/user/Desktop/projekt")

    def test_mehrere_sessions_auf_einmal(self):
        zweite = dict(SESSION, sessionId="bbbb2222", cwd="/tmp", name="tmp-1")
        wd, reg = build({"aaaa1111": SESSION, "bbbb2222": zweite})
        self.assertEqual(wd.auto_attach(), 2)

    def test_zweiter_durchlauf_legt_nicht_doppelt_an(self):
        wd, reg = build({"aaaa1111": SESSION})
        wd.auto_attach()
        self.assertEqual(wd.auto_attach(), 0)
        self.assertEqual(len(reg.hinzugefuegt), 1)


class TestVerzeichnisfilter(unittest.TestCase):
    def setUp(self):
        self._dirs = list(config.AUTO_ATTACH_DIRS)

    def tearDown(self):
        config.AUTO_ATTACH_DIRS = self._dirs

    def test_ohne_konfiguration_ist_alles_erlaubt(self):
        config.AUTO_ATTACH_DIRS = []
        self.assertTrue(config.auto_attach_allows("/beliebig/wo"))

    def test_ohne_cwd_niemals(self):
        config.AUTO_ATTACH_DIRS = []
        self.assertFalse(config.auto_attach_allows(None))
        self.assertFalse(config.auto_attach_allows(""))

    def test_unterverzeichnis_zaehlt_dazu(self):
        config.AUTO_ATTACH_DIRS = ["/home/user/Desktop"]
        self.assertTrue(config.auto_attach_allows("/home/user/Desktop/a/b"))

    def test_fremdes_verzeichnis_wird_abgelehnt(self):
        config.AUTO_ATTACH_DIRS = ["/home/user/Desktop"]
        self.assertFalse(config.auto_attach_allows("/home/user/Dokumente"))

    def test_praefix_allein_reicht_nicht(self):
        # /home/user/Desktop-alt liegt NICHT unter /home/user/Desktop.
        config.AUTO_ATTACH_DIRS = ["/home/user/Desktop"]
        self.assertFalse(config.auto_attach_allows("/home/user/Desktop-alt"))


# Echte cgroup-Zeilen dieser Kiste (cgroup v2, 'cat /proc/<pid>/cgroup'):
CGROUP_DIENST = (
    "0::/user.slice/user-1000.slice/user@1000.service/app.slice/"
    "app-claude-session.slice/claude-session@dauertest.service\n"
)
CGROUP_TERMINAL = (
    "0::/user.slice/user-1000.slice/user@1000.service/app.slice/"
    "app-qterminal-a1b2c3.scope\n"
)
CGROUP_WATCHDOG = (
    "0::/user.slice/user-1000.slice/user@1000.service/app.slice/"
    "claude-watchdog.service\n"
)


class TestFremdeAufsicht(unittest.TestCase):
    """Sessions unter eigener systemd-Unit gehoeren nicht dem Watchdog."""

    def setUp(self):
        self._skip = config.SKIP_SUPERVISED
        self._prefix = config.SUPERVISED_UNIT_PREFIX
        self._an = config.AUTO_ATTACH
        self._dirs = list(config.AUTO_ATTACH_DIRS)
        config.SKIP_SUPERVISED = True
        config.SUPERVISED_UNIT_PREFIX = "claude-session@"
        config.AUTO_ATTACH = True
        config.AUTO_ATTACH_DIRS = []

    def tearDown(self):
        config.SKIP_SUPERVISED = self._skip
        config.SUPERVISED_UNIT_PREFIX = self._prefix
        config.AUTO_ATTACH = self._an
        config.AUTO_ATTACH_DIRS = self._dirs

    @staticmethod
    def _mit_cgroup(inhalt: str):
        """open() so ersetzen, dass /proc/<pid>/cgroup 'inhalt' liefert.

        Alle anderen Pfade gehen unveraendert an das echte open - sonst
        stolpert schon der Import ueber die Attrappe.
        """
        echt = open

        def fake_open(pfad, *args, **kwargs):
            if str(pfad).endswith("/cgroup"):
                return io.StringIO(inhalt)
            return echt(pfad, *args, **kwargs)

        return fake_open

    def test_unit_name_aus_cgroup(self):
        with unittest.mock.patch("builtins.open", self._mit_cgroup(CGROUP_DIENST)):
            self.assertEqual(detector.supervising_unit(4242),
                             "claude-session@dauertest.service")

    def test_terminal_scope_ist_keine_unit(self):
        # Eine von Hand gestartete Session laeuft in einem .scope, nicht in
        # einer .service-Einheit - die darf der Watchdog weiter aufnehmen.
        with unittest.mock.patch("builtins.open", self._mit_cgroup(CGROUP_TERMINAL)):
            self.assertIsNone(detector.supervising_unit(4242))

    def test_fremde_unit_zaehlt_nicht(self):
        # Nur der konfigurierte Praefix gilt als fremde Aufsicht.
        with unittest.mock.patch("builtins.open", self._mit_cgroup(CGROUP_WATCHDOG)):
            self.assertIsNone(detector.externally_supervised(4242))

    def test_dienst_session_wird_uebersprungen(self):
        wd, reg = build({"aaaa1111": SESSION})
        with unittest.mock.patch("builtins.open", self._mit_cgroup(CGROUP_DIENST)):
            self.assertEqual(wd.auto_attach(), 0)
        self.assertEqual(reg.hinzugefuegt, [])

    def test_freie_session_wird_weiter_aufgenommen(self):
        wd, reg = build({"aaaa1111": SESSION})
        with unittest.mock.patch("builtins.open", self._mit_cgroup(CGROUP_TERMINAL)):
            self.assertEqual(wd.auto_attach(), 1)

    def test_abschaltbar(self):
        config.SKIP_SUPERVISED = False
        wd, reg = build({"aaaa1111": SESSION})
        with unittest.mock.patch("builtins.open", self._mit_cgroup(CGROUP_DIENST)):
            self.assertEqual(wd.auto_attach(), 1)

    def test_ohne_pid_keine_aufsicht(self):
        self.assertIsNone(detector.supervising_unit(None))
        self.assertIsNone(detector.externally_supervised(None))

    def test_unlesbare_cgroup_ist_keine_aufsicht(self):
        def kaputt(pfad, *args, **kwargs):
            if str(pfad).endswith("/cgroup"):
                raise OSError("weg")
            return open(pfad, *args, **kwargs)

        with unittest.mock.patch("builtins.open", kaputt):
            self.assertIsNone(detector.supervising_unit(4242))


if __name__ == "__main__":
    unittest.main(verbosity=2)


class WiederaufnahmeTest(unittest.TestCase):
    """Eine wieder laufende Sitzung darf nicht dauerhaft unbeobachtet bleiben.

    Eine session_id ueberlebt das Ende einer Sitzung: `claude --resume` nimmt
    dieselbe wieder auf. Der Task dazu ist aber ein einmaliger Eintrag — war
    er terminal, sperrte er die Session fuer immer gegen eine erneute
    Aufnahme. Beobachtet am 2026-07-31: Task 50d8d1f2 am 30.07. um 14:15:46
    abgeschlossen, waehrend pid 20728 als `claude --resume 66666666-…`
    weiterlief und vom CLI als `busy` gemeldet wurde.
    """

    def setUp(self):
        self._an = config.AUTO_ATTACH
        self._dirs = config.AUTO_ATTACH_DIRS
        self._skip = config.SKIP_SUPERVISED
        config.AUTO_ATTACH = True
        config.AUTO_ATTACH_DIRS = []
        config.SKIP_SUPERVISED = False

    def tearDown(self):
        config.AUTO_ATTACH = self._an
        config.AUTO_ATTACH_DIRS = self._dirs
        config.SKIP_SUPERVISED = self._skip

    def erledigt(self, **kw) -> Task:
        vorgabe = dict(id="alt", title="fertig", cwd="/home/user/Desktop",
                       mode=Mode.OBSERVED, status=Status.DONE,
                       session_id="aaaa1111", attempts=4,
                       last_error_class="UNKNOWN", transcript_size=999_999)
        vorgabe.update(kw)
        return Task(**vorgabe)

    def lauf(self, task, lebt=True, ist_claude=True):
        wd, reg = build({"aaaa1111": SESSION}, {"aaaa1111": task})
        with unittest.mock.patch.object(detector, "pid_alive",
                                        lambda _p: lebt), \
             unittest.mock.patch.object(detector, "pid_is_claude",
                                        lambda _p: ist_claude):
            return wd.auto_attach(), reg

    def test_lebende_session_wird_wieder_aufgenommen(self):
        n, reg = self.lauf(self.erledigt())
        self.assertEqual(n, 1)
        self.assertIs(reg.bekannt["aaaa1111"].status, Status.RUNNING)

    def test_kein_zweiter_eintrag(self):
        """Auf session_id liegt ein eindeutiger Index — nur aktualisieren."""
        n, reg = self.lauf(self.erledigt())
        self.assertEqual(reg.hinzugefuegt, [])
        self.assertEqual(len(reg.aktualisiert), 1)
        self.assertEqual(reg.aktualisiert[0].id, "alt")

    def test_altes_gepaeck_wird_abgelegt(self):
        _, reg = self.lauf(self.erledigt())
        neu = reg.bekannt["aaaa1111"]
        self.assertEqual(neu.attempts, 0)
        self.assertIsNone(neu.last_error_class)
        self.assertIsNone(neu.next_retry_at)
        self.assertIsNone(neu.mute_until)
        self.assertEqual(neu.pid, SESSION["pid"])

    def test_groessenstand_stammt_von_der_eigenen_datei(self):
        """Sonst schleppt die Sitzung den Stand ihres letzten Lebens mit."""
        _, reg = self.lauf(self.erledigt())
        self.assertNotEqual(reg.bekannt["aaaa1111"].transcript_size, 999_999)

    def test_tote_pid_bleibt_abgeschlossen(self):
        """Die urspruengliche Sorge: gerade beendete Session nicht neu anlegen."""
        n, reg = self.lauf(self.erledigt(), lebt=False)
        self.assertEqual(n, 0)
        self.assertIs(reg.bekannt["aaaa1111"].status, Status.DONE)

    def test_fremder_prozess_hinter_der_pid_zaehlt_nicht(self):
        """Schutz gegen eine neu vergebene pid."""
        n, _ = self.lauf(self.erledigt(), ist_claude=False)
        self.assertEqual(n, 0)

    def test_laufender_task_wird_weiter_uebergangen(self):
        """Nur terminale Eintraege werden angefasst."""
        n, reg = self.lauf(self.erledigt(status=Status.RUNNING))
        self.assertEqual(n, 0)
        self.assertEqual(reg.aktualisiert, [])
