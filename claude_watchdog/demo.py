"""Erfundene Tasks fuer Screenshots - echte Auftraege bleiben draussen."""

from __future__ import annotations

import os
import time

from .models import Mode, Status, Task

#: Fester Bezugspunkt fuer die Zeitangaben, relativ zum Aufruf. Damit stimmen
#: "in 4m" und Konsorten, ohne dass zwei Laeufe verschiedene Bilder ergeben.
_ABSTAENDE = (240.0, 900.0)


def aktiv() -> bool:
    """Ist der Demo-Modus eingeschaltet?

    Die Ausgabe von `status` und `list` zeigt Auftragstexte und Titel echter
    Sitzungen. Ein Bild davon veroeffentlicht genau die - deshalb entsteht
    jedes Bild fuer die Doku aus diesem Modul.
    """
    return os.environ.get("CW_DEMO", "").strip() not in ("", "0", "false", "no")


def tasks() -> list[Task]:
    """Ein Satz erfundener Tasks, der die interessanten Zustaende zeigt."""
    jetzt = time.time()

    def task(id_: str, titel: str, mode: Mode, status: Status, **rest) -> Task:
        t = Task(id=id_, title=titel, cwd="/home/user/code/shop-api",
                 mode=mode, status=status)
        for name, wert in rest.items():
            setattr(t, name, wert)
        return t

    return [
        task("11111111", "Refactor the payment webhook",
             Mode.OBSERVED, Status.RUNNING),
        task("22222222", "Port the CLI to argparse",
             Mode.MANAGED, Status.WAITING_FOR_LIMIT,
             attempts=1, next_retry_at=jetzt + _ABSTAENDE[0],
             last_error_class="USAGE_LIMIT", cost_usd_spent=0.42),
        task("33333333", "Nightly build babysitter",
             Mode.MANAGED, Status.BLOCKED,
             attempts=2, last_error_class="AWAITING_INPUT",
             cost_usd_spent=1.87),
        task("44444444", "Write the migration guide",
             Mode.MANAGED, Status.PENDING),
        task("55555555", "Flaky test in the queue worker",
             Mode.OBSERVED, Status.DONE, cost_usd_spent=0.09),
    ]
