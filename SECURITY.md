# Security and privacy

This document describes what Claude Watchdog reads, what it writes, what it can
execute, and the one path on which data leaves the machine. Every claim points
at the code it follows from (`file:line`, state of 2026-08-17). Line numbers
drift with edits; the names do not.

The watchdog is a local user process. It opens no listening socket, has no
server component, no configuration file that can be written from elsewhere, and
no authentication of its own — it runs with exactly the privileges of the user
whose systemd instance starts it.

## What it reads

- **Claude Code transcripts.** Path built in `config.transcript_path()`
  (`claude_watchdog/config.py:216`) below `CW_PROJECTS_DIR`
  (`claude_watchdog/config.py:24`, default `~/.claude/projects`). Only the last
  `CW_TAIL_BYTES` bytes are read (`claude_watchdog/transcript.py:29`, limit from
  `claude_watchdog/config.py:100`), at
  `claude_watchdog/detector.py:286-287`. `attach` additionally lists the
  directory names under `CW_PROJECTS_DIR` to find a session
  (`claude_watchdog/cli.py:117`).
- **`/proc`, for tracked pids only.** `stat` to tell a live process from a
  zombie (`claude_watchdog/detector.py:41`), `cmdline` to confirm the pid is
  really a `claude` process (`claude_watchdog/detector.py:59`), `cgroup` to see
  whether the session already runs under a systemd unit of its own
  (`claude_watchdog/detector.py:76`).
- **Its own data.** `state.db` through `sqlite3`
  (`claude_watchdog/registry.py:109-110`) and the run logs it wrote itself
  (`claude_watchdog/detector.py:298`, `claude_watchdog/config.py:237`).

Nothing else is opened for reading. In particular the watchdog does not read
your source files, your git history, `~/.claude` beyond the transcript
directory, or the environment of other processes.

## What it writes, and where

Everything the daemon writes lives under `CW_BASE_DIR`
(`claude_watchdog/config.py:17`, default `~/.claude-watchdog`), on the local
filesystem:

| Path | Written at | Content |
|---|---|---|
| `state.db` | `config.py:18`, `registry.py:109-110` | one row per task: id, title, cwd, session id, pid, transcript path, status, counters, the task text you passed to `add`, and the last error text taken from the transcript |
| `watchdog.log` | `config.py:19`, `logging_setup.py:54-59` | one JSON line per decision, rotated at `CW_LOG_MAX_BYTES` |
| `runs/<task-id>/attempt-NNN.jsonl` and `.err` | `config.py:229`, `config.py:233`, `recovery.py:728-733`, `cli.py:277-282` | raw stdout and stderr of the runs the watchdog started |
| `daemon.lock` | `config.py:21`, `daemon.py:26`, `daemon.py:33` | single-instance lock (`flock`) |

Four things touch a path outside that directory, all of them visible:

- The same log messages also go to stderr (`logging_setup.py:63-67`). Under the
  unit that is the systemd journal, so decisions are readable with
  `journalctl --user -u claude-watchdog.service` and are subject to the
  journal's own retention, not to `CW_RETENTION_DAYS`.
- A run started by the watchdog runs with `cwd=task.cwd`
  (`recovery.py:736`) and writes whatever that run decides to write. That is
  the run's doing; the watchdog only starts it.
- `reply` appends a turn to an existing transcript, by handing the text to the
  CLI with `-r <session-id>` (`recovery.py:412-430`). The watchdog does not
  edit transcript files itself.
- The optional backup script writes a dated `tar.gz` of the repository
  directory to `CW_BACKUP_DIR` (`bin/backup-repo:32`, default `~/backups`). It
  runs only if you enable `claude-watchdog-backup.timer`.

Nothing is ever deleted outside `CW_BASE_DIR`. The retention pass
(`daemon.py:260-280`) removes only task rows that are terminal and older than
`CW_RETENTION_DAYS`, together with their run logs; the delete helper refuses
any path that is not a direct child of `runs/` (`daemon.py:86`).

## The code contains no network client

There is no socket, no HTTP client, no TLS, no URL anywhere in
`claude_watchdog/`. This is not a promise, it is a test:
`tests/test_lokal.py` parses every file of the package with `ast` and fails on
any import whose root name is in its blocklist (`tests/test_lokal.py:21`:
`socket`, `ssl`, `urllib`, `http`, `smtplib`, `ftplib`, `poplib`, `imaplib`,
`telnetlib`, `xmlrpc`, `asyncio`, `webbrowser`, `requests`, `httpx`, `aiohttp`,
`urllib3`). The same test file carries deliberately faulty dummy sources, so
that a check which stopped detecting anything would itself fail.

```sh
python3 -m unittest discover -s tests
```

The same test also forbids the shell: `shell=` with anything other than
`False`, `os.system`, `os.popen`, `subprocess.getoutput` and
`subprocess.getstatusoutput` are all reported as findings. Every command is
built as an argument list, never as a string.

## The four programs it can start

The allowlist is `tests/test_lokal.py:30`; the test resolves every
`subprocess.run/Popen/call/check_call/check_output` in the package statically
back to the program name and fails on anything else, including a name it cannot
resolve.

| Program | Started at | Why |
|---|---|---|
| `claude` (`config.CLAUDE_BIN`, `config.py:27`) | `detector.py:124-127`, `recovery.py:734`, `cli.py:278` | session state query, the supervised run, `reply` |
| `notify-send` (`config.NOTIFY_BIN`, `config.py:153`) | `notifier.py:150-154` | desktop notification, disabled with `CW_NOTIFY=0` and rate-limited via `CW_NOTIFY_MAX_PER_HOUR` (`notifier.py:118-145`), which only throttles the notifications - the log still records every one |
| `systemd-run` | `recovery.py:79-86`, `recovery.py:dienst_kommando()` | wrapper only: puts a started run into a `--user --scope`, or into a transient service, with `MemoryHigh=8G`, `MemoryMax=12G`, `MemorySwapMax=2G` (`recovery.py:52-55`). Missing `systemd-run` means the run starts without that limit, not that it fails |
| `systemctl` | `recovery.py:mainpid()` | asks the user manager for the `MainPID` of a run started as a transient service (`CW_RUN_LAUNCHER=service`). Talks to the manager's unix socket, never to a network |

`claude agents --json --all` (`detector.py:125`) is passed no task content, no
prompt and no transcript — only those three arguments. What that subcommand does
internally belongs to the Claude Code CLI and is outside this repository.

## The only path on which data leaves the machine

Exactly one: the run that the watchdog starts for a **managed** task. An
`observed` session never causes such a run (see the invariants below).

The command line is built in `RecoveryEngine.build_command()`
(`recovery.py:445-461`):

```
claude -p <prompt> --output-format stream-json --verbose
      [-r <session-id> | --session-id <uuid>]
      [--model <model>] [--permission-mode <mode>] [--max-budget-usd <n>]
```

`--model`, `--permission-mode` and `--max-budget-usd` appear only if you set
them on `add`. The command is wrapped by `systemd-run` (`recovery.py:83-86`),
started with `cwd=task.cwd` and `start_new_session=True` (`recovery.py:736-738`)
and inherits the daemon's environment; `reply` strips the inherited Claude
session variables first (`recovery.py:388-394`, `cli.py:280`).

`<prompt>` is one of four texts, and this is the whole list:

1. **First start** — the task text you passed to `add`, unchanged
   (`recovery.py:694`).
2. **Resume** — `CONTINUE_TEMPLATE` (`recovery.py:358`): the reason for the
   interruption plus your original task text (`recovery.py:698`). The reason is
   a string the watchdog generates — an error class and the label of the
   pattern that matched (`classifier.py:380`, `recovery.py:347`) — not an
   excerpt of the transcript. Note that `-r` resumes an existing session, so
   its history is already on the API side from the original run; the resume
   adds nothing new from disk.
3. **Restart after a context limit** — `FRESH_TEMPLATE` (`recovery.py:365-372`):
   your original task text **plus a verbatim excerpt of the previous session**.
   The excerpt is the last 2000 characters of the collected tail
   (`recovery.py:463-473`), which is transcript text and, for managed tasks,
   run-log text. It can contain anything that happened to be at the end of the
   session: file paths, command output, error messages, and any secret that was
   printed there.
4. **`reply`** — the text you passed on the command line, appended to the
   existing session (`recovery.py:412-430`).

Case 3 is the one case in which locally collected session content is sent
onward, and it can be switched off — in the daemon's environment, e.g. as a
drop-in for the unit:

```sh
systemctl --user edit claude-watchdog.service
# [Service]
# Environment=CW_FRESH_DIGEST=0
```

With the switch off, `_digest()` returns a fixed placeholder before it ever
touches the tail (`config.py:112`, `recovery.py:470-471`, `recovery.py:377-380`) —
the new run is told that the history was withheld deliberately, and your
original task text still goes with it. `tests/test_frisch_digest.py` covers
both switch positions, including the check that with the switch off the prompt
does not depend on the tail at all.

Everything else the watchdog knows — decisions, error classes, task rows —
stays in `CW_BASE_DIR` and is never sent anywhere.

## Network isolation: the daemon itself, with no network at all

The sections above show that the code contains no network client. That is an
audit, not a guarantee — a compromised process could still open a socket. The
kernel can enforce it, and this repository ships the drop-in for it:
`systemd/claude-watchdog.service.d/netz-isolation.conf`.

```sh
mkdir -p ~/.config/systemd/user/claude-watchdog.service.d
ln -s <repo>/systemd/claude-watchdog.service.d/netz-isolation.conf \
      ~/.config/systemd/user/claude-watchdog.service.d/
systemctl --user daemon-reload && systemctl --user restart claude-watchdog
```

It sets two things that belong together:

```ini
PrivateNetwork=yes
Environment=CW_RUN_LAUNCHER=service
```

**Why `PrivateNetwork` and not `IPAddressDeny`.** Measured on 2026-08-17,
systemd 261, user manager: `IPAddressDeny=any` has **no effect** in user units —
the connection went through in both a transient scope and a transient service.
`PrivateNetwork=yes` does work: the unit gets its own network namespace, in
which only `lo` exists. Do not take this on faith, measure it yourself:

```sh
systemd-run --user --wait --pipe -p PrivateNetwork=yes -- ip -o link
# expect a single line: lo
```

**Why the launcher has to change with it.** A run started with
`systemd-run --user --scope` is a child of the daemon and inherits its network
namespace — measured: from inside an isolated unit, a scope could not reach a
listener on the host. A *transient service* is started by the user manager
instead, so it does not inherit anything from the daemon — measured: it reached
the listener. `CW_RUN_LAUNCHER=service` switches the daemon to that path
(`recovery.py:_starte_als_dienst()`), which is why the drop-in sets both.

Consequences, all measured on the running daemon:

| | with the drop-in |
|---|---|
| interfaces in the daemon's namespace | `lo` only |
| `claude agents --json --all` | works — it needs no network |
| desktop notifications, `systemd-run`, `systemctl` | work — all unix sockets |
| observing sessions | unchanged |
| a managed run | starts as a transient service and reaches the API |

Two settings are deliberately **not** in the drop-in: `RestrictAddressFamilies`
and `SystemCallFilter`. Both are seccomp filters, both are inherited by every
child and cannot be dropped again — they would break `claude agents --json` and
the Node JIT of the run itself.

The `--scope` path remains the default (`CW_RUN_LAUNCHER=scope`) for anyone who
does not want the drop-in. Both paths are covered by
`tests/test_netz_isolation.py`.

## Invariants

**1. An `observed` session is never started, resumed or restarted.**
Enforced in one place: `enforce_mode_guard()` (`recovery.py:93-112`), anchored
on `INTRUSIVE_ACTIONS = {START, RESUME, RESTART_FRESH}` (`models.py:66`). An
intrusive decision for an observed task is rewritten into a notification and
does not count as an attempt. A new intrusive action must be entered in that
set, not routed past the guard. `reply` refuses observed tasks separately
(`cli.py:248-251`).
Evidence: `tests/test_guard.py:37` (every action in `INTRUSIVE_ACTIONS` is
defused), `:45` (managed is untouched), `:52` (non-intrusive actions pass
through), `:62` (an observed crash produces a notification, not a resume).

**2. A question is reported, never answered.**
`AWAITING_INPUT` yields `Action.NOTIFY` in both modes and no automatic answer
is ever generated (`recovery.py:213-221`); `backoff.is_retryable()` refuses the
class as well. The `reply` command exists, but it sends the text its caller
passed and nothing else (`recovery.py:412-430`).
Evidence: `tests/test_guard.py:75`, `tests/test_backoff.py:108`.

## Verify it yourself

```sh
# 1) the invariants, as tests - this is the load-bearing check
python3 -m unittest discover -s tests
python3 -m unittest tests.test_lokal -v
python3 -m unittest tests.test_guard -v
python3 -m unittest tests.test_frisch_digest -v

# 2) grep, as a second pair of eyes - both expect no output
grep -rnE '^[[:space:]]*(import|from)[[:space:]]+(socket|ssl|urllib|http|smtplib|xmlrpc|asyncio|requests|httpx|aiohttp)' claude_watchdog/*.py
grep -rn 'shell=\|os\.system\|os\.popen' claude_watchdog/*.py

# 3) the running daemon holds no network socket
PID=$(systemctl --user show -p MainPID --value claude-watchdog.service)
ss -tunp 2>/dev/null | grep -F "pid=$PID"  # expect: no rows - no TCP, no UDP
ls -l /proc/"$PID"/fd
#   expect: /dev/null, the log file, the lock file and the sqlite files. Two
#   entries do read socket:[...]: file descriptors 1 and 2, stdout and stderr
#   wired to the journal by systemd. They are AF_UNIX and not opened by this
#   code; everything else in the list is a plain file.

# 4) syscalls of the daemon itself, without following children
#    (children are the claude runs; those do talk to the API by design)
strace -e trace=network -p "$PID"          # expect: nothing over several passes

# 5) what it actually did, in its own words
journalctl --user -u claude-watchdog.service -f
tail -n 50 ~/.claude-watchdog/watchdog.log

# 6) what it actually started
ps -o pid,ppid,args -C claude
systemd-cgls --user-unit claude-watchdog.service
```

Point 3 is worth repeating in the other direction: the `claude` processes the
watchdog starts do reach the network, because that is what they are for. The
claim of this document is about the watchdog's own process, and about which
text it hands to those runs.

## Reporting a vulnerability

Please open an issue at
<https://github.com/JBGfr/claude-watchdog/issues>.

There is no private disclosure channel and no security mailing address. Keep
reports free of anything you would not publish: session IDs, transcript
excerpts, absolute home paths, API keys, tokens. A minimal reproduction against
a scratch `CW_BASE_DIR` is more useful than a log from your real installation.

Useful in a report: the commit you tested, the command you ran, what you
expected, what happened, and — if the finding touches one of the invariants
above — the test that ought to have caught it.

## Deutsch

Diese Datei ist bewusst nur auf Englisch gepflegt, damit es zu den
Sicherheitsaussagen genau **eine** maßgebliche Fassung gibt. Die deutsche
Bedienungsanleitung steht in [README.de.md](README.de.md); dort sind
Datenverzeichnis, Not-Aus (`STOP`), `CW_FRESH_DIGEST` und der Testbefehl
ebenfalls beschrieben.

Kurzfassung: Der Watchdog liest Transkripte, `/proc` und seine eigene
Datenbank, schreibt ausschließlich unterhalb von `CW_BASE_DIR`, enthält selbst
keinen Netzwerk-Client (abgesichert durch `tests/test_lokal.py`) und kann genau
drei Programme starten: `claude`, `notify-send`, `systemd-run`. Der einzige
Weg, auf dem eingesammelter Inhalt die Maschine verlässt, ist der gestartete
`claude -p …`-Lauf eines **managed**-Tasks; nach einem Kontextlimit geht dabei
ein wörtlicher Auszug des bisherigen Verlaufs mit, abschaltbar mit
`CW_FRESH_DIGEST=0`. Observed-Sessions werden nie angefasst, Rückfragen nie
automatisch beantwortet.
