#!/usr/bin/env python3
"""Leck-Pruefung: findet Privates in einem Git-Baum, bevor er oeffentlich wird."""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
from pathlib import Path

#: Eine UUID gilt als erfunden, wenn jede Gruppe aus einem einzigen wiederholten
#: Zeichen besteht (11111111-2222-3333-4444-555555555555). Alles andere ist
#: verdaechtig: echte Session-IDs sehen genau so aus wie Zufall.
_UUID = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
                   r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
_HOME = re.compile(r"/home/(?!user\b)[a-z][a-z0-9_.-]*")
_MAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_MAC = re.compile(r"\b(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}\b")
_SECRET = re.compile(r"(sk-ant-[A-Za-z0-9_-]{6,}|ghp_[A-Za-z0-9]{20,}"
                     r"|gho_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"
                     r"|AKIA[0-9A-Z]{16}"
                     r"|-----BEGIN [A-Z ]*PRIVATE KEY-----)")

#: Keine Adresse, sondern erlaubt: noreply-Absender, Beispieldomains und
#: systemd-Template-Units wie claude-session@dauertest.service.
_MAIL_OK = re.compile(r"@(users\.noreply\.github\.com|example\.(com|org|net))$"
                      r"|\.(service|socket|timer|target|slice|scope|mount|path)$")
_IP_OK = {"127.0.0.1", "0.0.0.0", "255.255.255.255", "8.8.8.8"}

#: Dateien, die in einem oeffentlichen Baum nichts verloren haben.
_DATEIEN = (
    ("state.db", re.compile(r"(^|/)state\.db")),
    ("Transkript", re.compile(r"\.jsonl$")),
    ("Logdatei", re.compile(r"\.log(\.\d+)?$")),
    ("Arbeitsumgebung", re.compile(r"(^|/)\.claude/")),
    ("Vault-Notiz", re.compile(r"(^|/)projekt-[\w-]+\.md$")),
    ("Laufzeitrest", re.compile(r"(^|/)(daemon\.lock|runs/|STOP$)")),
)


def _erfunden(uuid: str) -> bool:
    return all(len(set(teil.lower())) == 1 for teil in uuid.split("-"))


def _maskiere(text: str) -> str:
    """Fundstellen nie im Klartext ausgeben - der Bericht wandert weiter."""
    if len(text) <= 6:
        return text[:2] + "…"
    return text[:4] + "…" + text[-2:]


def pruefe_text(pfad: str, inhalt: str) -> list[tuple[str, str, int, str]]:
    """Eine Datei pruefen. Rueckgabe: (Art, Pfad, Zeile, Fundstelle)."""
    befunde: list[tuple[str, str, int, str]] = []
    for nr, zeile in enumerate(inhalt.splitlines(), 1):
        for treffer in _UUID.finditer(zeile):
            if not _erfunden(treffer.group()):
                befunde.append(("Session-UUID", pfad, nr, treffer.group()))
        for treffer in _HOME.finditer(zeile):
            befunde.append(("Heimatpfad", pfad, nr, treffer.group()))
        for treffer in _MAIL.finditer(zeile):
            if not _MAIL_OK.search(treffer.group()):
                befunde.append(("E-Mail", pfad, nr, treffer.group()))
        for treffer in _IPV4.finditer(zeile):
            teile = treffer.group().split(".")
            if treffer.group() in _IP_OK:
                continue
            if any(int(t) > 255 for t in teile):
                continue  # Versionsnummer o.ae., keine Adresse
            befunde.append(("IP-Adresse", pfad, nr, treffer.group()))
        for treffer in _MAC.finditer(zeile):
            befunde.append(("MAC-Adresse", pfad, nr, treffer.group()))
        for treffer in _SECRET.finditer(zeile):
            befunde.append(("Geheimnis", pfad, nr, treffer.group()))
    return befunde


def pruefe_baum(repo: Path, ref: str) -> list[tuple[str, str, int, str]]:
    """Den Git-Baum von `ref` pruefen - nicht den Arbeitsbaum."""
    def git(*args: str) -> str:
        return subprocess.run(("git", "-C", str(repo)) + args,
                              capture_output=True, text=True,
                              check=True).stdout

    befunde: list[tuple[str, str, int, str]] = []
    dateien = [z for z in git("ls-tree", "-r", "--name-only", ref).splitlines() if z]
    for pfad in dateien:
        for art, muster in _DATEIEN:
            if muster.search(pfad):
                befunde.append(("Datei: " + art, pfad, 0, pfad))
        roh = subprocess.run(("git", "-C", str(repo), "show", ref + ":" + pfad),
                             capture_output=True, check=True).stdout
        if b"\x00" in roh[:8000]:
            continue  # Binaerdatei, zeilenweise Pruefung sinnlos
        befunde += pruefe_text(pfad, roh.decode("utf-8", "replace"))

    for zeile in git("log", "--format=%ae%n%ce", ref).splitlines():
        if zeile and not _MAIL_OK.search(zeile):
            befunde.append(("Commit-Adresse", "(git log)", 0, zeile))
    return befunde


def selbsttest() -> int:
    """Gegenprobe: an einer Attrappe muss jede Regel anschlagen.

    Ohne diesen Schritt koennte die Pruefung stillschweigend nichts pruefen -
    und ein gruener Haken waere dann das Gefaehrlichste am ganzen Werkzeug.
    """
    erwartet = {"Session-UUID", "Heimatpfad", "E-Mail", "IP-Adresse",
                "MAC-Adresse", "Geheimnis", "Datei: Transkript",
                "Commit-Adresse"}
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        subprocess.run(("git", "-C", tmp, "init", "-q"), check=True)
        # Die Attrappenwerte werden zur Laufzeit zusammengesetzt. Stuenden sie
        # woertlich hier, meldete die Pruefung ihr eigenes Pruefwerkzeug -
        # und ein Werkzeug, das bei jedem Lauf Alarm schlaegt, wird ignoriert.
        (repo / "attrappe.txt").write_text(
            "sitzung 12ab34cd-5e6f-7a8b-" + "9c0d-1e2f3a4b5c6d\n"
            "pfad /home/" + "testnutzer/Desktop\n"
            "post jemand@" + "beispielfirma.de\n"
            "adresse 203.0." + "113.7\n"
            "hardware aa:bb:cc:" + "dd:ee:ff\n"
            "schluessel sk-" + "ant-abcdef123456\n", encoding="utf-8")
        (repo / "verlauf.jsonl").write_text("{}\n", encoding="utf-8")
        (repo / "sauber.txt").write_text(
            "erfundene id 11111111-2222-3333-4444-555555555555\n"
            "pfad /home/user/Desktop\n"
            "post niemand@users.noreply.github.com\n"
            "lokal 127.0.0.1\n"
            "unit claude-session@dauertest.service\n", encoding="utf-8")
        subprocess.run(("git", "-C", tmp, "add", "-A"), check=True)
        subprocess.run(("git", "-C", tmp, "-c", "user.name=Test",
                        "-c", "user.email=test@" + "privat.beispiel.de",
                        "commit", "-q", "-m", "attrappe"), check=True)
        befunde = pruefe_baum(repo, "HEAD")

    gefunden = {art for art, _p, _z, _t in befunde}
    fehlend = erwartet - gefunden
    falsch = [b for b in befunde if b[1] == "sauber.txt"]
    if fehlend:
        print("SELBSTTEST GESCHEITERT - diese Regeln schlagen nicht an:",
              ", ".join(sorted(fehlend)))
        return 1
    if falsch:
        print("SELBSTTEST GESCHEITERT - Fehlalarm auf sauberer Datei:", falsch)
        return 1
    print("Selbsttest bestanden: %d Regeln schlagen an der Attrappe an, "
          "die saubere Datei bleibt still." % len(erwartet))
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("ref", nargs="?", default="HEAD",
                   help="Git-Referenz des zu pruefenden Baums (Vorgabe: HEAD)")
    p.add_argument("--repo", default=".", help="Repo-Verzeichnis")
    p.add_argument("--zeigen", action="store_true",
                   help="Fundstellen im Klartext ausgeben statt maskiert")
    p.add_argument("--selbsttest", action="store_true",
                   help="nur die Gegenprobe an einer Attrappe fahren")
    a = p.parse_args()
    if a.selbsttest:
        return selbsttest()

    befunde = pruefe_baum(Path(a.repo).resolve(), a.ref)
    if not befunde:
        print("Sauber: keine privaten Reste in %s." % a.ref)
        return 0
    print("%d Befunde in %s:" % (len(befunde), a.ref))
    for art, pfad, zeile, text in befunde:
        ort = "%s:%d" % (pfad, zeile) if zeile else pfad
        print("  %-18s %-46s %s"
              % (art, ort, text if a.zeigen else _maskiere(text)))
    return 2


if __name__ == "__main__":
    sys.exit(main())
