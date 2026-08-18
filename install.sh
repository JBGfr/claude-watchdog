#!/bin/sh
# Richtet Claude Watchdog fuer den aktuellen Nutzer ein.
#
# Angelegt werden nur Symlinks: die beiden Skripte nach ~/.local/bin und die
# drei Units nach ~/.config/systemd/user. Danach ein daemon-reload.
#
# Was dieses Skript bewusst NICHT tut: loeschen, vorhandene Dateien
# ueberschreiben, Dienste enable-n oder starten. Das Einschalten bleibt eine
# Entscheidung des Nutzers; am Ende steht, welche Befehle dafuer noetig sind.
#
# Ein ~/Projekte wird nicht vorausgesetzt - das Repo darf liegen, wo es will.
#
# Mit --netz-isolation wird zusaetzlich der Drop-in verlinkt, der dem Daemon
# jeden Netzzugang entzieht (PrivateNetwork) und die Laeufe dafuer ueber den
# User-Manager startet. Begruendung und Messung: SECURITY.md.
set -eu

NETZ_ISOLATION=0
for arg in "$@"; do
    case $arg in
        --netz-isolation) NETZ_ISOLATION=1 ;;
        -h|--help)
            echo "Aufruf: install.sh [--netz-isolation]"
            exit 0 ;;
        *)
            echo "unbekannte Option: $arg" >&2
            exit 2 ;;
    esac
done

# --- Repo-Verzeichnis aus dem eigenen Pfad ermitteln --------------------------
# (readlink -f waere kuerzer, ist aber nicht POSIX)
self=$0
case $self in
    */*) ;;
    *) self=$(command -v -- "$self") ;;
esac
hops=0
while [ -L "$self" ]; do
    hops=$((hops + 1))
    [ "$hops" -le 40 ] || { echo "Symlink-Schleife bei $0" >&2; exit 1; }
    link=$(readlink "$self")
    case $link in
        /*) self=$link ;;
        *)  self=$(dirname "$self")/$link ;;
    esac
done
REPO=$(cd -P -- "$(dirname -- "$self")" && pwd)

# ~/.local/bin ist fest verdrahtet, weil die Units genau darauf zeigen (%h/.local/bin).
BIN_DIR="$HOME/.local/bin"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

SKRIPTE="bin/claude-watchdog bin/backup-repo"
UNITS="claude-watchdog.service claude-watchdog-backup.service claude-watchdog-backup.timer"

# Vorher pruefen, ob das wirklich das Repo ist - sonst legt man Symlinks ins Leere.
for f in $SKRIPTE; do
    [ -f "$REPO/$f" ] || { echo "fehlt: $REPO/$f - liegt install.sh im Repo?" >&2; exit 1; }
done
for u in $UNITS; do
    [ -f "$REPO/systemd/$u" ] || { echo "fehlt: $REPO/systemd/$u" >&2; exit 1; }
done

echo "Repo: $REPO"
echo

HANDARBEIT=""
merke() { HANDARBEIT="$HANDARBEIT  $1
"; }

# Legt einen Symlink an, wenn dort noch nichts liegt. Zeigt schon etwas anderes
# dorthin, wird es gemeldet und in Ruhe gelassen.
verlinke() {
    ziel=$1
    link=$2
    if [ -L "$link" ]; then
        ist=$(readlink "$link")
        case $ist in
            /*) ;;
            *)  ist=$(dirname -- "$link")/$ist ;;
        esac
        istdir=$(cd -P -- "$(dirname -- "$ist")" 2>/dev/null && pwd) || istdir=""
        [ -n "$istdir" ] && ist="$istdir/$(basename -- "$ist")"
        if [ "$ist" = "$ziel" ]; then
            echo "  ok       $link"
        else
            echo "  ANDERS   $link -> $ist"
            merke "$link zeigt woandershin - pruefen, dann: ln -sfn '$ziel' '$link'"
        fi
    elif [ -e "$link" ]; then
        echo "  BELEGT   $link (kein Symlink)"
        merke "$link ist eine echte Datei - selbst wegraeumen, dann install.sh erneut"
    else
        ln -s "$ziel" "$link"
        echo "  neu      $link"
    fi
}

mkdir -p "$BIN_DIR" "$UNIT_DIR"

echo "Skripte in $BIN_DIR:"
verlinke "$REPO/bin/claude-watchdog" "$BIN_DIR/claude-watchdog"
verlinke "$REPO/bin/backup-repo" "$BIN_DIR/claude-watchdog-backup"

echo
echo "Units in $UNIT_DIR:"
for u in $UNITS; do
    verlinke "$REPO/systemd/$u" "$UNIT_DIR/$u"
done

DROPIN="claude-watchdog.service.d/netz-isolation.conf"
if [ "$NETZ_ISOLATION" = 1 ]; then
    echo
    echo "Netz-Isolation in $UNIT_DIR:"
    mkdir -p "$UNIT_DIR/claude-watchdog.service.d"
    verlinke "$REPO/systemd/$DROPIN" "$UNIT_DIR/$DROPIN"
fi

# Das Ausfuehrbar-Bit geht z. B. beim Auspacken eines ZIP verloren.
for f in $SKRIPTE; do
    [ -x "$REPO/$f" ] || merke "chmod +x '$REPO/$f'   (Skript ist nicht ausfuehrbar)"
done

case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) merke "$BIN_DIR liegt nicht im PATH - in ~/.profile oder ~/.zshrc ergaenzen" ;;
esac

echo
if command -v systemctl >/dev/null 2>&1; then
    if systemctl --user daemon-reload 2>/dev/null; then
        echo "systemctl --user daemon-reload: erledigt"
    else
        echo "systemctl --user daemon-reload fehlgeschlagen (keine User-Instanz?)"
        merke "systemctl --user daemon-reload  in einer Sitzung mit laufendem User-Manager"
    fi
else
    echo "kein systemctl gefunden - die Units liegen bereit, geladen wurde nichts."
fi

echo
echo "Noch selbst zu tun - das Skript startet und aktiviert bewusst nichts:"
echo "  systemctl --user enable --now claude-watchdog.service"
echo "  systemctl --user enable --now claude-watchdog-backup.timer"
echo "  systemctl --user status claude-watchdog.service"
if [ "$NETZ_ISOLATION" = 0 ]; then
    echo
    echo "Optional: der Daemon kann ohne jeden Netzzugang laufen (die Laeufe"
    echo "behalten ihren). Dafuer install.sh --netz-isolation aufrufen; was das"
    echo "genau bewirkt, steht in SECURITY.md."
fi
if [ -n "$HANDARBEIT" ]; then
    echo
    echo "Ausserdem offen:"
    printf '%s' "$HANDARBEIT"
fi
