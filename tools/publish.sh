#!/bin/sh
# Baut den oeffentlichen Stand als Commit auf dem Zweig "public" und prueft ihn
# auf private Reste. Pusht NICHTS - der letzte Schritt bleibt beim Menschen.
#
# Warum ein eigener Zweig statt der echten History: in der History dieses Repos
# stehen Session-IDs, Transkriptpfade und private Absenderadressen. Die laesst
# sich nachtraeglich nicht zuverlaessig entfernen - GitHub liefert geloeschte
# Objekte weiter per SHA aus. Veroeffentlicht wird deshalb nur der Baum.
set -eu

REPO=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
QUELLE=${1:-HEAD}
NACHRICHT=${2:-}
ZWEIG=${PUBLIC_BRANCH:-public}
cd "$REPO"

# 1. Absender pruefen. Eine private Adresse im Commit ist genau die Art Leck,
#    die man nachtraeglich nicht mehr einfaengt.
MAIL=$(git config user.email 2>/dev/null || true)
case "$MAIL" in
    *users.noreply.github.com) ;;
    *)
        echo "Abbruch: user.email ist keine noreply-Adresse ($MAIL)." >&2
        echo "Setzen mit: git config user.email" \
             "'<id>+<name>@users.noreply.github.com'" >&2
        exit 1 ;;
esac

# 2. Index aus der Quelle bauen und private Reste herausnehmen. Der Arbeitsbaum
#    wird dabei nicht angefasst - alles laeuft ueber einen eigenen Index.
TMPINDEX=$(mktemp)
trap 'rm -f "$TMPINDEX"' EXIT
GIT_INDEX_FILE=$TMPINDEX
export GIT_INDEX_FILE
git read-tree "$QUELLE"
git ls-files --cached \
    | grep -E '(^|/)\.claude/|(^|/)projekt-[a-z0-9-]+\.md$|(^|/)state\.db|\.jsonl$' \
    > "$TMPINDEX.raus" 2>/dev/null || true
while IFS= read -r PFAD; do
    [ -n "$PFAD" ] || continue
    echo "  nehme heraus: $PFAD"
    git rm --cached --quiet -- "$PFAD"
done < "$TMPINDEX.raus"
rm -f "$TMPINDEX.raus"
TREE=$(git write-tree)
unset GIT_INDEX_FILE

# 3. Kandidat bauen - der Zweig wird noch NICHT bewegt.
DATUM=$(date +%Y-%m-%d)
[ -n "$NACHRICHT" ] || NACHRICHT="Stand $DATUM"
if git show-ref --verify --quiet "refs/heads/$ZWEIG"; then
    KANDIDAT=$(printf '%s\n' "$NACHRICHT" | git commit-tree "$TREE" -p "$ZWEIG")
else
    KANDIDAT=$(printf '%s\n' "$NACHRICHT" | git commit-tree "$TREE")
fi

# 4. Leck-Pruefung ueber die GESAMTE Kette, erst danach bewegt sich der Zweig.
#
# Beides hat sich am 2026-08-18 als notwendig erwiesen, nicht als Vorsicht:
# die alte Fassung bewegte den Zweig VOR der Pruefung und prueft nur die
# Spitze. Zwei rote Probelaeufe blieben so als Eltern in der Kette haengen -
# und deren Baeume (mit echten Session-UUIDs und Heimatpfaden) gingen beim
# Push mit ins Netz, waehrend die Spitze gruen war.
for C in $(git rev-list "$KANDIDAT"); do
    if ! python3 "$REPO/tools/leak-check.py" --repo "$REPO" "$C"; then
        echo >&2
        echo "Leck-Pruefung hat bei $(git rev-parse --short "$C") angeschlagen" >&2
        echo "- der Zweig $ZWEIG bleibt unveraendert, NICHT veroeffentlichen." >&2
        exit 2
    fi
done

COMMIT=$KANDIDAT
git branch -f "$ZWEIG" "$COMMIT" >/dev/null
echo "Zweig $ZWEIG steht auf $(git rev-parse --short "$COMMIT") ($NACHRICHT)"

echo
echo "Fertig. Der naechste Schritt passiert von Hand:"
echo "  git push <remote> $ZWEIG:main"
