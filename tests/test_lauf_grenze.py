"""Test: gestartete Laeufe bekommen eine Speichergrenze.

Hintergrund: managed Laeufe hingen ohne jede Grenze in der cgroup des
Daemons. Am 2026-07-31 wuchsen zwei interaktive Claude-Fenster auf 27,8
bzw. 28,6 GB und rissen per globalem OOM den ollama-Dienst mit — hier
laeuft dasselbe Programm, und ein durchgehender Lauf haette ausgerechnet
den Supervisor mitgerissen, der ihn wieder herstellen soll.

Der Watchdog verlaesst sich auf zwei Eigenschaften des Starts, die die
Einpackung nicht kaputtmachen darf:
  * `proc.poll()` liefert den Exit-Code des Programms,
  * `proc.pid` IST das Programm (pid_is_claude prueft /proc/<pid>/cmdline).
Beides wird hier gegen das echte systemd-run geprueft, nicht nachgebaut.
"""

from __future__ import annotations

import functools
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from claude_watchdog import recovery  # noqa: E402

#: Kleines Programm im Scope: meldet die eigene cgroup und deren Speichergrenze.
PROBELAUF = (
    'p=$(sed -n "s/^0:://p" /proc/self/cgroup) || exit 1\n'
    'test -n "$p" || exit 1\n'
    'printf "%s\\n" "$p"\n'
    'cat "/sys/fs/cgroup$p/memory.max"\n'
)


@functools.lru_cache(maxsize=None)
def systemd_run_taugt() -> bool:
    """Einmaliger Probelauf: laesst sich hier wirklich ein Scope starten?

    `shutil.which` allein reicht nicht. In Containern, etwa in GitHub
    Actions, liegt `systemd-run` zwar im Pfad, es fehlt aber der User-Bus
    oder die cgroup-Delegation. Der Aufruf scheitert dann erst beim Start,
    und die Tests unten faerbten sich rot, obwohl nichts am Code kaputt ist.

    Deshalb wird genau der Startweg der Tests einmal ausgefuehrt und sein
    Erfolg geprueft: Exit-Code 0 (systemd-run reicht ihn durch), eine eigene
    `.scope`-cgroup und eine dort tatsaechlich gesetzte Speichergrenze. Die
    erwartete Groesse wird bewusst NICHT geprueft; das bleibt Sache von
    `EinpackenTest` und `test_lauf_bekommt_eine_eigene_cgroup`, sonst
    verstummte eine echte Regression zu einem stillen Uebersprung.
    """
    if shutil.which("systemd-run") is None:
        return False
    try:
        fertig = subprocess.run(
            recovery.mit_speichergrenze(["/bin/sh", "-c", PROBELAUF],
                                        "probelauf"),
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    if fertig.returncode != 0:
        return False
    zeilen = fertig.stdout.decode("utf-8", "replace").split()
    if len(zeilen) != 2:
        return False
    pfad, grenze = zeilen
    return pfad.endswith(".scope") and grenze.isdigit()


class EinpackenTest(unittest.TestCase):
    def test_grenzen_sind_dabei(self) -> None:
        argv = recovery.mit_speichergrenze(["claude", "--resume", "x"], "t1")
        for grenze in ("MemoryHigh=8G", "MemoryMax=12G", "MemorySwapMax=2G"):
            self.assertIn("--property=" + grenze, argv)

    def test_befehl_bleibt_unveraendert(self) -> None:
        """Alles hinter '--' muss exakt der urspruengliche Befehl sein."""
        cmd = ["claude", "--resume", "abc", "-p", "tu was"]
        argv = recovery.mit_speichergrenze(cmd, "t1")
        self.assertEqual(argv[argv.index("--") + 1:], cmd)

    def test_grenzen_stehen_vor_dem_trenner(self) -> None:
        argv = recovery.mit_speichergrenze(["claude"], "t1")
        trenner = argv.index("--")
        for i, teil in enumerate(argv):
            if teil.startswith("--property=Memory"):
                self.assertLess(i, trenner, teil)

    def test_scope_statt_eigener_unit(self) -> None:
        """Nur `--scope` exec't den Befehl und erhaelt damit pid und Exit-Code."""
        argv = recovery.mit_speichergrenze(["claude"], "t1")
        self.assertIn("--scope", argv)

    def test_ohne_systemd_run_bleibt_der_befehl_nackt(self) -> None:
        """Lieber ohne Grenze starten als gar nicht."""
        cmd = ["claude", "--resume", "x"]
        with unittest.mock.patch.object(recovery.shutil, "which",
                                        lambda _n: None):
            self.assertEqual(recovery.mit_speichergrenze(cmd, "t1"), cmd)


class EchterStartTest(unittest.TestCase):
    """Gegen das echte systemd-run — hier haengt der Recovery-Pfad dran."""

    @classmethod
    def setUpClass(cls) -> None:
        if not systemd_run_taugt():
            raise unittest.SkipTest(
                "systemd-run hier nicht benutzbar (kein Bus oder keine "
                "cgroup-Delegation)")

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def programm(self, rumpf: str) -> str:
        p = Path(self.tmp.name) / "unecht-claude"
        p.write_text("#!/bin/sh\n" + rumpf + "\n")
        p.chmod(p.stat().st_mode | stat.S_IEXEC)
        return str(p)

    def test_exit_code_kommt_an(self) -> None:
        """proc.poll() muss weiterhin den Code des Programms liefern."""
        prog = self.programm("exit 42")
        proc = subprocess.Popen(
            recovery.mit_speichergrenze([prog], "t1"),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.assertEqual(proc.wait(timeout=30), 42)

    def test_pid_ist_das_programm_selbst(self) -> None:
        """pid_is_claude liest /proc/<pid>/cmdline — dort muss der Befehl stehen."""
        prog = self.programm("sleep 5")
        proc = subprocess.Popen(
            recovery.mit_speichergrenze([prog, "--resume", "abc"], "t1"),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            cmdline = ""
            for _ in range(50):          # systemd-run braucht einen Moment
                try:
                    with open("/proc/%d/cmdline" % proc.pid, "rb") as fh:
                        cmdline = fh.read().replace(b"\0", b" ").decode()
                except OSError:
                    break
                if "--resume" in cmdline:
                    break
                time.sleep(0.1)
            self.assertIn("--resume abc", cmdline)
        finally:
            proc.kill()
            proc.wait(timeout=10)

    def test_lauf_bekommt_eine_eigene_cgroup(self) -> None:
        """Ohne eigene cgroup gaebe es nichts zu begrenzen."""
        prog = self.programm("grep ^0:: /proc/self/cgroup; sleep 3")
        proc = subprocess.Popen(
            recovery.mit_speichergrenze([prog], "t1"),
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        try:
            zeile = proc.stdout.readline().decode().strip()
            pfad = zeile.split("::", 1)[-1]
            self.assertTrue(pfad.endswith(".scope"), pfad)
            grenze = Path("/sys/fs/cgroup" + pfad + "/memory.max")
            self.assertEqual(grenze.read_text().strip(), str(12 * 1024**3))
        finally:
            proc.kill()
            proc.wait(timeout=10)


if __name__ == "__main__":
    unittest.main()
