# Claude Watchdog — Projektanweisungen

Supervisor-Daemon für Claude-Code-Sessions. Fachliche Beschreibung, Betriebsarten,
Konfigurationsvariablen und Entscheidungstabelle stehen in `README.md` (englisch) und
`README.de.md` (deutsch), Datenfluss und Invarianten in `SECURITY.md` — hier steht nur,
wie in diesem Repo Code geschrieben wird.

## Tech-Stack

| | |
|---|---|
| Sprache | Python, `.venv/bin/python` = **3.13.14** |
| Abhängigkeiten | **keine.** Ausschließlich stdlib (`sqlite3`, `fcntl`, `subprocess`, `argparse`, …) |
| Persistenz | rohes `sqlite3`, kein ORM — `Task.to_row()` / `Task.from_row()` in `models.py` |
| Tests | `unittest` aus der stdlib, **kein pytest** |
| Packaging | keins — kein `pyproject.toml`, kein `setup.py` |
| CI | GitHub Actions, `.github/workflows/tests.yml` — nur `checkout` + `setup-python`, dann die Testsuite |

**Die Null-Abhängigkeits-Regel ist die wichtigste Einschränkung des Projekts.** Kein
`pip install` in dieses venv, keine Fremdpakete in Code oder Tests. Wenn etwas nur mit
einer Bibliothek geht, ist das eine Designfrage und keine Implementierungsentscheidung —
vorher fragen.

## Build & Run

```sh
# Tests (müssen grün sein, Laufzeit < 1 s) - stdlib-only, venv optional
python3 -m unittest discover -s tests -q

# CLI aus dem Repo; der Wrapper setzt PYTHONPATH und nimmt .venv, wenn es da ist
bin/claude-watchdog status

# Als Dienst
systemctl --user status claude-watchdog
journalctl --user -u claude-watchdog -f
```

Das Paket wird **nicht installiert**: `bin/claude-watchdog` ist ein `/bin/sh`-Wrapper, der
`PYTHONPATH` auf das Projektverzeichnis setzt und `python -m claude_watchdog.cli` startet.
Neue Einstiegspunkte genauso anlegen, keine `entry_points` einführen.

## Code-Stil

- `from __future__ import annotations` als erste Anweisung nach dem Modul-Docstring
  (in 12 von 13 Modulen so).
- Modul-Docstring ist ein deutscher Einzeiler: `"""Datenmodelle: Task, Enums, Observation, Decision."""`
- Zustände sind **`str`-Enums** (`class Mode(str, Enum)`), damit sie direkt in SQLite und
  JSON landen. Keine nackten Strings für Zustände.
- Datenträger sind `@dataclass`, Felder werden mit `#:`-Kommentaren direkt über dem Feld
  dokumentiert (Sphinx-Stil), nicht in einem Sammel-Docstring.
- Ausnahmen werden **eng** gefangen: `OSError`, `ValueError`, `ProcessLookupError`,
  `PermissionError`. `except Exception:` nur an Prozessgrenzen, wo ein Fehler die
  Hauptschleife nicht abbrechen darf.
- Logging über ein Modul-`log`; `log.info` für Entscheidungen, `log.exception` für
  Unerwartetes. Jede Entscheidung wird als JSON-Zeile geloggt — dieses Format nicht brechen.

### Doku: zweisprachig

`README.md` ist **englisch** und die Hauptdokumentation für Fremde, `README.de.md` die
deutsche Entsprechung. Was in der einen steht, gehört in die andere — insbesondere jede
neue `CW_*`-Variable. `SECURITY.md` ist englisch mit deutscher Kurzfassung am Ende und
belegt jede Aussage mit `Datei:Zeile`.

### Umlaute: nur in Markdown

Konsequent im ganzen Repo: `.py`, Commit-Betreffs und `.service`-Dateien sind **reines
ASCII** mit Transliteration (`naechstes`, `Laeufe`, `ausschliesslich`) — 0 von 19
Python-Dateien und 0 von 6 Commits enthalten Umlaute. Nur `.md`-Dateien nutzen echte
Umlaute. Sprache ist überall Deutsch.

## Tests

- `tests/test_<modul>.py`, Klassen erben von `unittest.TestCase`, Zusicherungen als
  `self.assertEqual` / `self.assertTrue` — keine bloßen `assert`. Aktuell 425 Vorkommen
  (`grep -rhoE 'self\.assert[A-Za-z]+' tests/*.py | wc -l`).
- Stand **2026-08-18**: **288 Tests, grün.** Gezählt mit
  `grep -rhoE "^\s+def test_" tests/*.py | wc -l`, gefahren mit
  `python3 -m unittest discover -s tests -q`. Beide Zahlen wachsen laufend — sie stehen
  hier mit Datum und Messbefehl, damit sichtbar wird, wenn sie veralten. Vorher standen
  hier 104, 157 und 210; die ersten beiden waren monatelang der Stand vom Juli und
  niemandem fiel es auf.
- Die Tests laufen ohne Netz, ohne Dateisystem-Zufall und in Millisekunden; das soll so
  bleiben. Keine `sleep`, keine echten Subprozesse, keine Netzwerkzugriffe in Tests.
  `tests/test_lokal.py` hält das fest: kein Netz-Import im Paket, keine Shell, nur die
  drei erlaubten Programme (`claude`, `notify-send`, `systemd-run`).
- Neue Fehlerklassen oder Textmuster brauchen einen Testfall mit einer **echten
  Ereignisfolge** aus `runs/`, nicht mit erfundenem Text.

## Sicherheitsinvariante — nicht umgehen

Eine `observed`-Session darf **niemals** gestartet, fortgesetzt oder neu aufgesetzt werden.
Durchgesetzt an genau einer Stelle: `enforce_mode_guard()` in `recovery.py`, verankert an
`INTRUSIVE_ACTIONS` in `models.py`. Wer eine neue eingreifende Aktion einführt, trägt sie
**dort** ein — nicht am Guard vorbei. `tests/test_guard.py` sichert das ab.

Ebenso unverhandelbar: `AWAITING_INPUT` wird nur gemeldet, nie beantwortet. Der Watchdog
trifft keine inhaltlichen Entscheidungen.

## Lokalität — die zweite Invariante

Der Daemon selbst geht nicht ins Netz. Abgesichert durch `tests/test_lokal.py`: gesperrte
Importe (`socket`, `urllib`, `http`, …) und eine **Allowlist der startbaren Programme**
(`claude`, `notify-send`, `systemd-run`, `systemctl`). Wer ein weiteres Programm startet,
macht den Test rot — die Liste wächst nur mit einer Begründung im Kommentar daneben.

Wer das erzwingen will, nimmt den Drop-in
`systemd/claude-watchdog.service.d/netz-isolation.conf` (`install.sh --netz-isolation`):
`PrivateNetwork=yes` plus `CW_RUN_LAUNCHER=service`. Beides gehört zusammen — ein per
`--scope` gestarteter Lauf ist ein Kind des Daemons und erbt dessen Netzsperre, ein
transienter Dienst wird vom User-Manager gestartet und erbt sie nicht. Messungen und
Gegenprobe stehen in `SECURITY.md`; `IPAddressDeny` wirkt in User-Units **nicht**.

Zwei Fallen, die dabei Zeit gekostet haben und nicht wiederkommen sollen:

- **`$$` gehört nicht in eine systemd-Kommandozeile.** systemd liest sie selbst und macht
  daraus ein einzelnes `$` — in der PID-Datei stand danach wörtlich `$`. Die PID kommt
  deshalb über `systemctl show MainPID` vom Manager (`recovery.mainpid()`).
- **Kein `RestrictAddressFamilies`, kein `SystemCallFilter`.** Beides sind seccomp-Filter,
  die jedes Kind erbt und die sich nicht wieder ablegen lassen; sie träfen
  `claude agents --json` und den Node-JIT des Laufs.

## Wo was hingehört

| Ich will… | …dann hierhin |
|---|---|
| ein neues Textmuster für die Klassifikation | `classifier.py`, ausschließlich in die `PATTERNS`-Tabelle |
| eine neue Aktion oder Entscheidungsregel | `recovery.py` — `decide()` entscheidet, `execute()` führt aus |
| Wartezeiten / Retry-Regeln ändern | `backoff.py` |
| ein neues Feld am Task | `models.py` **und** das SQLite-Schema in `registry.py` |
| eine neue Konfigurationsvariable | `config.py` als `CW_*`-Umgebungsvariable mit Default, plus Zeile in **beiden** README-Tabellen (`README.md`, `README.de.md`) |
| ein neues CLI-Kommando | `cli.py` (`argparse`) |
| Beobachtungsquellen erweitern | `detector.py` — Signale kombinieren, keiner Quelle allein vertrauen |
| einen weiteren Startweg für Läufe | `recovery.py` — `CW_RUN_LAUNCHER` in `config.py`, dazu ein Objekt mit `pid`/`poll()` wie `DienstLauf` |

## Git

- Branch `main`, lokal direkt darauf committen; gezielt stagen, nie `git add -A`.
- Commit-Betreff: deutsch, ASCII, beschreibt die Wirkung — z. B. `Untaetige observed-Sessions
  nicht mehr melden`. **Keine** `feat:`/`fix:`-Präfixe.
- Ein `push` auf `main` blockiert `.git/hooks/pre-push`; ins Remote geht es über einen
  eigenen Branch und einen PR, gemergt wird serverseitig. Seit `.github/workflows/tests.yml`
  läuft die Testsuite bei jedem Push und jedem PR.
- Nicht eingecheckt und nie hinzufügen: `state.db`, `watchdog.log`, `runs/`, `daemon.lock`, `.venv/`.

## Veröffentlichung

Der Code wird öffentlich gespiegelt, die **Historie nicht**: veröffentlicht wird ein
**Orphan-Branch mit genau einem sauberen Commit** (Arbeitsstand ohne Vorgeschichte). Das
private Repo behält seine vollständige History und bleibt privat. **Nie die volle History
pushen** — auch nicht „nur die letzten paar Commits": in ihnen stehen Pfade, Session-IDs
und Transkript-Reste aus dem Alltagsbetrieb.

Was **niemals** in einen veröffentlichten Baum darf:

- echte Session-UUIDs (in Tests gehören erfundene hin: `11111111-2222-3333-4444-555555555555`)
- `/home/<benutzer>`-Pfade — im Beispieltext `/home/user`, im Code `Path.home()` oder `%h`
- Transkript-Ausschnitte, Run-Logs, Logzeilen aus echten Läufen
- `state.db` in jeder Form, auch als Sicherung mit angehängtem Datum
- Klarnamen und E-Mail-Adressen, auch in Commit-Adressen (`git log --format=%ae`)

Geprüft wird das **vor jeder Veröffentlichung** mit dem Werkzeug im Repo:

```sh
python3 tools/leak-check.py --selbsttest     # zuerst: schlaegt die Pruefung ueberhaupt an?
python3 tools/leak-check.py <ref>            # dann der zu veroeffentlichende Baum
```

Exit-Code 0 heißt sauber, 2 heißt Befunde. Der Selbsttest zuerst, sonst ist ein grüner
Haken nur die Aussage, dass nichts geprüft wurde.
