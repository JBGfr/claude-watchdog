# Claude Watchdog

*English version: [README.md](README.md) — die englische Fassung ist die
Hauptdokumentation des Repos, dieser Text ist die deutsche Entsprechung.*

Supervisor-Daemon für Claude-Code-Sessions: er beobachtet laufende Arbeiten,
erkennt Unterbrechungen (Usage-Limit, Netzwerk, Absturz, Kontextende) und setzt
sie automatisch fort, sobald es wieder geht.

Kein tmux, keine Tastatur-Simulation, kein Eingriff in fremde Terminals. Der
Watchdog liest die Transkripte, die Claude Code ohnehin schreibt, fragt
`claude agents --json` nach dem Zustand und startet bei Bedarf einen eigenen
headless Lauf.

Was der Watchdog liest, schreibt und startet — und wie man das selbst nachprüft
— steht in [SECURITY.md](SECURITY.md).

![claude-watchdog status im Demo-Modus](assets/screenshot.png)

*Das Bild entsteht im Demo-Modus (`CW_DEMO=1 claude-watchdog status`), der die
Taskliste durch erfundene ersetzt — die echte trägt Sitzungstitel und
Projektpfade.*

## Zwei Betriebsarten

| Modus | Herkunft | Was der Watchdog darf |
|---|---|---|
| `managed` | selbst gestartet über `add` | alles: starten, fortsetzen, neu ansetzen |
| `observed` | deine interaktive Session, automatisch erfasst oder per `attach` | **nur beobachten und melden** |

Laufende Sessions nimmt der Daemon **von selbst** auf: was `claude agents --json`
listet und noch keinen Task hat, wird als `observed` erfasst. Du musst also an
nichts denken, wenn du eine lange Session startest und weggehst. Abschalten mit
`CW_AUTO_ATTACH=0`, einschränken mit `CW_AUTO_ATTACH_DIRS`. Bereits erfasste
Sessions werden übergangen — auch abgeschlossene, sonst würde eine gerade
beendete Session sofort wieder aufgenommen.

**Übersprungen werden außerdem Sessions, die schon unter einer eigenen
systemd-Unit laufen** (Vorgabe: Unit-Präfix `claude-session@`, siehe
`CW_SKIP_SUPERVISED`). Dort startet systemd den Prozess bei Bedarf selbst neu;
ein zweiter Beobachter würde bei jedem dieser planmäßigen Neustarts einen neuen
Task anlegen und ihn kurz darauf wieder als beendet abräumen — Karteileichen und
Meldungen über etwas, das sich selbst repariert hat. Erkannt wird das an der
cgroup des Prozesses, nicht am Namen.

Die Grenze wird an genau einer Stelle durchgesetzt (`enforce_mode_guard` in
`recovery.py`) und ist durch Tests abgesichert: Eine observed-Session wird
niemals gestartet, fortgesetzt oder neu aufgesetzt. Endet sie, wird der Task
abgeschlossen statt in Dauerschleife gemeldet.

## Voraussetzungen

- Linux mit systemd-Nutzerinstanz (`systemctl --user`)
- Python 3.11 oder neuer, ausschließlich Standardbibliothek — kein pip, keine
  Fremdpakete (entwickelt und getestet auf 3.13, die CI läuft mit 3.13)
- Claude-Code-CLI unter `~/.local/bin/claude` oder an beliebiger Stelle, auf die
  `CW_CLAUDE_BIN` zeigt (ein reiner Programmname wird dort über den `PATH` aufgelöst)
- optional: `notify-send` für Desktop-Meldungen, `systemd-run` für die
  Speichergrenze um gestartete Läufe

## Installation

Das Repo darf liegen, wo es will — ein `~/Projekte` wird nirgends
vorausgesetzt.

```sh
git clone https://github.com/JBGfr/claude-watchdog.git
cd claude-watchdog
./install.sh
```

`install.sh` legt ausschließlich Symlinks an und macht ein `daemon-reload`. Es
löscht nichts, überschreibt nichts und startet nichts:

- `bin/claude-watchdog` → `~/.local/bin/claude-watchdog`
- `bin/backup-repo` → `~/.local/bin/claude-watchdog-backup`
- `systemd/*.service`, `systemd/*.timer` → `~/.config/systemd/user/`

`~/.local/bin` muss im `PATH` liegen: die Units rufen die CLI genau über diesen
Pfad auf (`ExecStart=%h/.local/bin/claude-watchdog run`). Liegt dort schon eine
andere Datei, meldet `install.sh` das und lässt sie in Ruhe.

Einschalten bleibt eine eigene Entscheidung:

```sh
systemctl --user enable --now claude-watchdog.service
systemctl --user status claude-watchdog.service
```

Das Paket wird **nicht** installiert: `bin/claude-watchdog` ist ein
`/bin/sh`-Wrapper, der das Repo auf den `PYTHONPATH` legt und
`python -m claude_watchdog.cli` startet. Ein `.venv` wird benutzt, wenn es da
ist, ist aber keine Voraussetzung — sonst ließe sich das Repo nicht ohne
Bauschritt nutzen.

Mit `sudo loginctl enable-linger $USER` läuft der Dienst auch ohne angemeldeten
Benutzer und ab dem Systemstart.

Das zweite Unit-Paar (`claude-watchdog-backup.service` / `.timer`) ist
optional und tut nichts, solange der Timer nicht aktiviert ist. Es legt ein
datiertes `tar.gz` des Repo-Verzeichnisses in `~/backups` ab (`CW_BACKUP_DIR`).

### Datenverzeichnis

Alle Laufzeitdaten liegen unter `CW_BASE_DIR`, Vorgabe `~/.claude-watchdog`:
`state.db`, `watchdog.log`, `runs/<task-id>/`, `daemon.lock` und die
STOP-Datei. Das Verzeichnis wird beim ersten Start angelegt; es muss **nicht**
im Repo liegen und hat mit dem Ort des Quellcodes nichts zu tun.

Auf der Entwicklungsmaschine ist `~/.claude-watchdog` ein **Symlink auf das
Repo-Verzeichnis** — deshalb tauchen Datenbank und Log dort mitten in der
Arbeitskopie auf und deshalb sind sie in `.gitignore` mehrfach abgesichert.
Das ist eine lokale Eigenheit, keine Voraussetzung. Wer neu installiert,
bekommt schlicht ein eigenes Verzeichnis und braucht keinen Symlink.

### Unit anpassen

Die mitgelieferte Unit geht von einer X11-Desktop-Sitzung aus, damit
Benachrichtigungen ankommen:

```
Environment=DISPLAY=:0
Environment=DBUS_SESSION_BUS_ADDRESS=unix:path=%t/bus
```

Auf einem anderen Display, unter Wayland oder auf einer Maschine ohne Desktop
gehören diese Zeilen angepasst oder entfernt (dann `CW_NOTIFY=0` setzen).
Ebenfalls angenommen: `notify-send` liegt in `/usr/bin`, `claude` in
`~/.local/bin` (`Environment=PATH=…`).

Die installierte Unit ist ein Symlink ins Repo — sie zu editieren heißt, das
Repo zu ändern. Besser ein Drop-in:

```sh
systemctl --user edit claude-watchdog.service
```

Zwei Details in der Unit sind Absicht:

- `KillMode=process` — beim Stoppen wird nur der Daemon beendet. Gestartete
  claude-Läufe laufen weiter und werden beim nächsten Start wieder adoptiert.
- `DISPLAY` und `DBUS_SESSION_BUS_ADDRESS` werden gesetzt, sonst kommt keine
  Desktop-Benachrichtigung an.

## Bedienung

```sh
# Auftrag anlegen - der Daemon startet ihn beim naechsten Durchlauf
claude-watchdog add "Baue die Tests gruen" --model sonnet --max-budget-usd 2

# Session von Hand erfassen - nur noetig, wenn Auto-Attach aus ist oder das
# CLI die Session nicht listet
claude-watchdog attach <session-id>

# Ueberblick
claude-watchdog status
claude-watchdog list --all
claude-watchdog logs <task-id> -n 60

# Steuern
claude-watchdog pause <task-id>     # aus der Ueberwachung nehmen
claude-watchdog resume <task-id>    # wieder freigeben
claude-watchdog reply <task-id> "Antwort"  # blockiertem managed-Task antworten
claude-watchdog rm <task-id>        # entfernen
```

Nützliche Schalter:

- `add`: `--cwd`, `--title`, `--model`, `--permission-mode`, `--max-attempts`,
  `--max-budget-usd`, `--no-auto-resume` (nur melden, nie von selbst fortsetzen)
- `attach`: `--cwd`, `--title`
- `list` / `status`: `--all` (auch abgeschlossene), `--json`
- `logs`: `-n/--lines` (Vorgabe 40)
- `resume`: `--reset-attempts`
- `reply` / `rm`: `--force`
- `run`: `--dry-run` (alles entscheiden und protokollieren, nichts ausführen),
  `--quiet`, `--verbose` — `run` ist das, was die systemd-Unit startet

`reply` liefert nur den Mechanismus (Turn ans Transkript anhaengen, Schonfrist
`CW_REPLY_GRACE`, zaehlt gegen das Neustart-Budget) — die inhaltliche Antwort
kommt vom Aufrufer. Für observed-Tasks wird es verweigert. Der Watchdog selbst
beantwortet weiterhin nichts.

Die Session-ID einer laufenden Session steht in `claude agents --json` oder ist
der Dateiname des Transkripts unter `~/.claude/projects/<cwd>/<session-id>.jsonl`.

**Not-Aus:** Solange die Datei `$CW_BASE_DIR/STOP` (Vorgabe
`~/.claude-watchdog/STOP`) existiert, startet der Watchdog nichts mehr.
Laufende Prozesse bleiben unberührt.

```sh
touch ~/.claude-watchdog/STOP   # nichts mehr starten
rm ~/.claude-watchdog/STOP      # wieder freigeben
```

## Wie entschieden wird

Pro Durchlauf (Default alle 15 s) wird jeder aktive Task beobachtet, die Lage
klassifiziert und daraus genau eine Aktion abgeleitet.

**Beobachtung** kombiniert vier Quellen, statt einer einzelnen zu vertrauen:
Prozess lebt (`/proc`), Transkript wächst, `claude agents --json` kennt die
Session, und bei managed Tasks zusätzlich Run-Log und Exit-Code.

**Klassifikation** in zwei Stufen (`classifier.py`): erst die strukturierten
JSON-Felder (`rate_limit_info`, `result`, `isApiErrorMessage`), dann als
Fallback eine zentrale Regex-Tabelle über den Rohtext. Innerhalb der ersten
Stufe gewinnt das **jüngste** Signal — ein alter Warnhinweis darf ein späteres
`result: success` nicht überstimmen.

| Klasse | Aktion |
|---|---|
| `NONE` (result: success) | `complete` |
| `USAGE_LIMIT` | `schedule` bis zum Reset-Zeitpunkt, zählt **nicht** als Fehlversuch |
| `RATE_LIMIT` | `schedule` nach `retry-after`, sonst exponentiell |
| `API_ERROR`, `NETWORK`, `CRASH`, `STALLED` | `resume` mit exponentiellem Backoff |
| `CONTEXT` | `restart_fresh` — neue Session mit verdichtetem Stand statt stumpfem Resume |
| `AWAITING_INPUT` | nur `notify` — eine Rückfrage wird **nie** automatisch beantwortet |
| observed-Session verschwunden | `complete` |

**Grenzen**, damit nichts Amok läuft:

- `max_attempts` pro Task (Default 5)
- höchstens 20 Neustarts pro Stunde über alle Tasks zusammen
- Anti-Schleife: dreimal an derselben Transkript-Position gescheitert → `failed`
- Session-Lock: derselbe Task wird nie doppelt gestartet
- Usage- und Rate-Limits erhöhen den Versuchszähler nicht — ein Kontingentende
  ist kein Fehlversuch des Tasks

Eine observed-Session gilt erst als beendet, wenn **alle drei** Bedingungen
zutreffen: kein Prozess mehr, `claude agents --json` war erreichbar und kennt
sie nicht, und seit `CW_OBSERVED_GONE_SECONDS` kein Transkript-Zuwachs. Ist die
CLI-Abfrage kaputt, wird nichts angenommen.

## Als Dienst

```sh
systemctl --user status claude-watchdog
journalctl --user -u claude-watchdog -f
```

## Konfiguration

Alles über Umgebungsvariablen, kein Codeeingriff nötig (`config.py`).
Schalter sind aus bei `0`, `false` und `no`, sonst an. Für den Dienst gehören
sie in ein Drop-in (`systemctl --user edit claude-watchdog.service`, darin unter
`[Service]` eine Zeile `Environment=CW_...=...`); die CLI nimmt sie aus der
Shell.

| Variable | Default | Bedeutung |
|---|---|---|
| `CW_BASE_DIR` | `~/.claude-watchdog` | Datenverzeichnis: `state.db`, `watchdog.log`, `runs/`, `daemon.lock`, `STOP` |
| `CW_CLAUDE_BIN` | `~/.local/bin/claude` | Pfad zur CLI |
| `CW_PROJECTS_DIR` | `~/.claude/projects` | wo die Transkripte liegen |
| `CW_AUTO_ATTACH` | `1` | laufende Sessions automatisch als observed aufnehmen |
| `CW_AUTO_ATTACH_DIRS` | leer | nur Sessions unterhalb dieser Verzeichnisse (komma-getrennt) |
| `CW_SKIP_SUPERVISED` | `1` | Sessions überspringen, die schon unter einer eigenen systemd-Unit laufen |
| `CW_SUPERVISED_UNIT_PREFIX` | `claude-session@` | Unit-Präfix, das als fremde Aufsicht gilt |
| `CW_POLL_INTERVAL` | `15` | Sekunden zwischen zwei Durchläufen |
| `CW_STALL_SECONDS` | `900` | ohne Fortschritt → gilt als hängend |
| `CW_OBSERVED_GONE_SECONDS` | `120` | ab wann eine observed-Session als beendet gilt |
| `CW_REPLY_GRACE` | `180` | Schonfrist nach einem `reply`, bevor der Daemon den Task wieder anfasst |
| `CW_AGENTS_CACHE_TTL` | `30` | Cache für `claude agents --json` |
| `CW_AGENTS_TIMEOUT` | `20` | Timeout für denselben Aufruf |
| `CW_BACKOFF_BASE` | `30` | erste Wartezeit in Sekunden |
| `CW_BACKOFF_FACTOR` | `2.0` | Wachstumsfaktor je Versuch |
| `CW_BACKOFF_CAP` | `1800` | Obergrenze der Wartezeit |
| `CW_BACKOFF_JITTER` | `0.2` | relative Streuung |
| `CW_USAGE_LIMIT_FALLBACK_WAIT` | `3600` | Wartezeit ohne verwertbare Reset-Zeit |
| `CW_USAGE_LIMIT_RESET_PADDING` | `30` | Puffer nach dem Reset |
| `CW_MAX_ATTEMPTS` | `5` | Wiederholversuche pro Task |
| `CW_MAX_RESTARTS_PER_HOUR` | `20` | globales Neustart-Budget |
| `CW_MAX_SAME_MARKER_RETRIES` | `3` | Anti-Schleifen-Grenze |
| `CW_TAIL_BYTES` | `65536` | wieviel Logende gelesen wird |
| `CW_FRESH_DIGEST` | `1` | darf ein `restart_fresh` den wörtlichen Auszug des bisherigen Verlaufs in den neuen Prompt schreiben? `0` setzt stattdessen einen neutralen Hinweis ein; der ursprüngliche Auftrag bleibt in beiden Fällen erhalten (siehe [SECURITY.md](SECURITY.md)) |
| `CW_RUN_LAUNCHER` | `scope` | wie ein managed-Lauf gestartet wird. `scope` = Kind des Daemons; `service` = der User-Manager startet ihn als transienten Dienst — nur so kann der Daemon selbst ohne Netz laufen (siehe [SECURITY.md](SECURITY.md)) |
| `CW_RETENTION_DAYS` | `14` | Schonfrist für abgeschlossene Tasks, `0` = nie aufräumen |
| `CW_CLEANUP_INTERVAL` | `3600` | Abstand zwischen zwei Aufräum-Läufen |
| `CW_LOG_MAX_BYTES` | `5242880` | Log-Rotation: Größe |
| `CW_LOG_BACKUP_COUNT` | `5` | Log-Rotation: aufbewahrte Dateien |
| `CW_LOG_REPEAT_INTERVAL` | `1800` | Sekunden, die eine unveränderte Entscheidung ohne Eingriff still bleibt (`0` = jede Runde protokollieren) |
| `CW_NOTIFY` | `1` | Desktop-Meldungen |
| `CW_NOTIFY_BIN` | `notify-send` | Meldeprogramm |
| `CW_NOTIFY_MAX_PER_HOUR` | `0` | harte Obergrenze für Desktop-Meldungen je gleitender Stunde, die Drosselmeldung eingerechnet, `0` = keine Grenze. Der letzte Platz der Stunde trägt den Hinweis, dass es ab jetzt nur noch ins Log geht; das Log bekommt immer alles |
| `CW_BACKUP_DIR` | `~/backups` | Zielverzeichnis von `bin/backup-repo` (nur dieses Skript, der Daemon liest es nicht) |

## Aufbau

```
claude_watchdog/
  daemon.py      Hauptschleife, Single-Instance-Lock, Adoption beim Start
  detector.py    Beobachtung: lebt / haengt / ist weg
  classifier.py  Klassifikation; ALLE Textmuster stehen in PATTERNS
  recovery.py    decide() entscheidet, execute() fuehrt aus, Mode-Guard
  backoff.py     Wartezeiten und Retry-Regeln
  registry.py    SQLite-Zustand, Locks, Neustart-Budget
  transcript.py  robustes Lesen mitwachsender JSON-Lines-Dateien
  notifier.py    notify-send
  config.py      Pfade und Defaults
  cli.py         Kommandozeile
```

Laufzeitdaten (nicht im Repo): `state.db`, `watchdog.log`, `runs/<task-id>/`
mit einem `attempt-NNN.jsonl` pro Lauf, `daemon.lock`.

Jede Entscheidung landet als JSON-Zeile in `watchdog.log` — im Nachhinein ist
damit nachvollziehbar, warum der Watchdog etwas getan hat.

## Tests

```sh
python3 -m unittest discover -s tests -q
```

Kein venv nötig, das Projekt ist stdlib-only; wo eines liegt, geht auch
`.venv/bin/python -m unittest discover -s tests -q`.

280 Tests (Stand 2026-08-17, mit dem Befehl darüber nachgezählt), ohne
Fremdpakete. Abgedeckt sind die Klassifikation (inklusive der realen
Ereignisfolgen aus echten Läufen), die Backoff-Regeln, die
Sicherheitsinvariante, dass observed-Sessions nie angefasst werden, und die
Invariante, dass der Code überhaupt keinen Netzwerk-Client enthält
(`tests/test_lokal.py`).

## Verwandtes

[claude-sessions](https://github.com/JBGfr/claude-sessions) ist die
Schwester-App: eine GTK3-Oberfläche, die laufende Claude-Code-Sessions in einem
Fenster zeigt. Eigenes Projekt, für den Watchdog nicht nötig.

## Bekannte Eigenheiten

- Ein `done`-Task behält seine letzte Fehlerklasse in der Übersicht — das ist
  Historie, kein aktueller Zustand.
- `permission_mode` setzt der Watchdog von sich aus nie; ohne Angabe fragt ein
  managed Lauf bei heiklen Aktionen und wird als `AWAITING_INPUT` gemeldet.
- Der Watchdog beantwortet grundsätzlich keine Rückfragen und trifft keine
  inhaltlichen Entscheidungen — er sorgt nur dafür, dass weitergearbeitet wird.

## Lizenz

MIT, siehe [LICENSE](LICENSE).

Nicht mit Anthropic verbunden. Claude und Claude Code sind Produkte von
Anthropic; dies ist ein unabhängiges Werkzeug.
