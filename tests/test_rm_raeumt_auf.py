"""Test: `rm` laesst keine Run-Logs als Waisen zurueck.

`cleanup()` raeumt die Protokolle eines abgelaufenen Tasks ausdruecklich
mit weg ("samt der Run-Logs, die sonst als Waisen liegen bleiben").
`cli.cmd_rm` tat das nicht — und danach kommt niemand mehr an sie heran,
weil cleanup() nur ueber Tasks laeuft, die es noch gibt.

Beobachtet am 2026-08-01 nach zwei Testlaeufen: beide Tasks aus der
Registry entfernt, die Verzeichnisse mit 12 und 26 KB blieben liegen.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from claude_watchdog import cli, config, daemon  # noqa: E402
from claude_watchdog.models import Mode, Status, Task  # noqa: E402
from claude_watchdog.registry import Registry  # noqa: E402


class RmRaeumtAufTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        wurzel = Path(self.tmp.name)
        p = unittest.mock.patch.object(config, "RUNS_DIR", wurzel / "runs")
        p.start()
        self.addCleanup(p.stop)
        config.RUNS_DIR.mkdir(parents=True)
        self.registry = Registry(wurzel / "state.db")
        self.addCleanup(self.registry.close)

    def task_mit_protokoll(self, tid: str = "t1") -> Task:
        task = self.registry.add(Task(
            id=tid, title="Test", cwd="/tmp", mode=Mode.MANAGED,
            status=Status.DONE, original_prompt="tu was"))
        d = config.run_dir(tid)
        d.mkdir(parents=True)
        (d / "attempt-001.jsonl").write_text("{}\n")
        (d / "attempt-001.err").write_text("")
        return task

    def rm(self, tid: str, force: bool = False) -> int:
        args = argparse.Namespace(task=tid, force=force)
        return cli.cmd_rm(args, self.registry)

    def test_protokolle_gehen_mit(self) -> None:
        self.task_mit_protokoll()
        self.assertTrue(config.run_dir("t1").is_dir())
        self.assertEqual(self.rm("t1"), 0)
        self.assertIsNone(self.registry.get("t1"))
        self.assertFalse(config.run_dir("t1").exists(),
                         "Run-Logs duerfen nicht als Waisen zurueckbleiben")

    def test_ohne_protokolle_kein_fehler(self) -> None:
        self.registry.add(Task(id="t2", title="Test", cwd="/tmp",
                               mode=Mode.OBSERVED, status=Status.DONE,
                               original_prompt=""))
        self.assertEqual(self.rm("t2"), 0)

    def test_laufender_task_wird_nicht_angefasst(self) -> None:
        """Ohne --force bleibt alles stehen — auch die Protokolle."""
        self.registry.add(Task(id="t3", title="Test", cwd="/tmp",
                               mode=Mode.MANAGED, status=Status.RUNNING,
                               original_prompt=""))
        d = config.run_dir("t3")
        d.mkdir(parents=True)
        (d / "attempt-001.jsonl").write_text("{}\n")
        self.assertEqual(self.rm("t3"), 3)
        self.assertIsNotNone(self.registry.get("t3"))
        self.assertTrue(d.is_dir())

    def test_fremdes_verzeichnis_bleibt_unangetastet(self) -> None:
        """Das Sicherheitsnetz: nur direkte Unterordner von RUNS_DIR."""
        fremd = Path(self.tmp.name) / "nicht-runs"
        fremd.mkdir()
        (fremd / "wichtig.txt").write_text("bleibt")
        daemon.drop_run_dir("../nicht-runs")
        self.assertTrue((fremd / "wichtig.txt").exists())


if __name__ == "__main__":
    unittest.main()
