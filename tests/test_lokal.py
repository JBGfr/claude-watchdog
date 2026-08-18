"""Invariantentest: der Watchdog geht nicht ins Netz und startet nur erlaubte Programme."""

from __future__ import annotations

import ast
import sys
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

PROJEKT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJEKT))

#: Produktivcode, der geprueft wird. Tests selbst sind bewusst ausgenommen.
PAKET = PROJEKT / "claude_watchdog"

#: Wurzelnamen gesperrter Module. Geprueft wird die Wurzel des gepunkteten
#: Namens, damit `urllib.request`, `http.client` und `xmlrpc.client`
#: mitgefangen sind, ohne jedes Untermodul einzeln aufzuzaehlen.
GESPERRTE_MODULE = frozenset({
    "socket", "ssl", "urllib", "http", "smtplib", "ftplib", "poplib",
    "imaplib", "telnetlib", "xmlrpc", "asyncio", "webbrowser",
    "requests", "httpx", "aiohttp", "urllib3",
})

#: Einzige Programme, die der Watchdog starten darf. `config.CLAUDE_BIN` und
#: `config.NOTIFY_BIN` sind die Konfigurationsvariablen, die Literale sind die
#: dahinterliegenden Vorgaben.
#:
#: Diese Liste waechst nur mit Begruendung, denn sie ist der eigentliche
#: Pruefmassstab:
#:   claude       - der ueberwachte Lauf selbst, der einzige Weg nach draussen
#:   notify-send  - Desktop-Meldung ueber D-Bus, lokal
#:   systemd-run  - startet den Lauf mit Speichergrenze (Scope oder Dienst)
#:   systemctl    - fragt den User-Manager nach der PID des gestarteten
#:                  Dienstes (Startweg "service"); redet ausschliesslich ueber
#:                  den Unix-Socket des Managers, nie ueber Netz
ERLAUBTE_PROGRAMME = frozenset({
    "config.CLAUDE_BIN", "config.NOTIFY_BIN",
    "claude", "notify-send", "systemd-run", "systemctl",
})

#: Aufrufe, die ein Programm starten.
STARTFUNKTIONEN = frozenset({"run", "Popen", "call", "check_call", "check_output"})

#: Aufrufe, die grundsaetzlich ueber die Shell laufen.
SHELLFUNKTIONEN = frozenset({"getoutput", "getstatusoutput"})

#: Markierung fuer einen Programmnamen, der sich statisch nicht aufloesen
#: laesst. Steht nie in der Allowlist und macht den Test damit rot - lieber
#: ein Fehlalarm als ein unbemerkter Start.
UNBEKANNT = "nicht aufloesbar: %s"


# --------------------------------------------------------------------------
# Quellen
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Quelle:
    """Eine geparste Quelldatei (echt oder Attrappe)."""

    #: Anzeigename in den Befunden, z.B. "recovery.py"
    name: str
    #: Modulname ohne Endung, fuer die Aufloesung von `recovery.foo(...)`
    modul: str
    #: Syntaxbaum; geparst mit ast, ausdruecklich nicht per Regex
    baum: ast.Module


def quelle_aus_text(name: str, text: str) -> Quelle:
    """Quelle aus einem Textstueck bauen - Grundlage der Gegenprobe."""
    return Quelle(name=name, modul=Path(name).stem,
                  baum=ast.parse(text, filename=name))


def paketdateien() -> list[Path]:
    """Alle Python-Dateien des Pakets.

    Bewusst `rglob` und nicht `glob`: ein spaeteres Unterpaket wuerde sonst
    still an der Pruefung vorbeilaufen und der Test bliebe trotzdem gruen.
    """
    return sorted(pfad for pfad in PAKET.rglob("*.py")
                  if "__pycache__" not in pfad.parts)


def echte_quellen() -> list[Quelle]:
    """Den gesamten Produktivcode parsen - mit ast, ausdruecklich nicht per Regex."""
    return [quelle_aus_text(pfad.relative_to(PAKET).as_posix(),
                            pfad.read_text(encoding="utf-8"))
            for pfad in paketdateien()]


# --------------------------------------------------------------------------
# Kleine Baum-Helfer
# --------------------------------------------------------------------------

def _kurz(knoten: Optional[ast.AST]) -> str:
    """Knappe Beschreibung eines Knotens fuer Fehlermeldungen."""
    if knoten is None:
        return "nichts"
    punkt = _punktname(knoten)
    if punkt is not None:
        return punkt
    if isinstance(knoten, ast.Call):
        return "%s(...)" % _kurz(knoten.func)
    if isinstance(knoten, ast.Constant):
        return repr(knoten.value)
    return type(knoten).__name__


def _punktname(knoten: ast.AST) -> Optional[str]:
    """'config.CLAUDE_BIN' aus einem Namens-/Attributknoten, sonst None."""
    if isinstance(knoten, ast.Name):
        return knoten.id
    if isinstance(knoten, ast.Attribute):
        basis = _punktname(knoten.value)
        return None if basis is None else basis + "." + knoten.attr
    return None


def _koerper(bereich: ast.AST) -> list[ast.stmt]:
    """Alle Anweisungen eines Gueltigkeitsbereichs, ohne verschachtelte def/class.

    Verzweigungen (if/try/with/for) gehoeren zum selben Bereich und werden
    mitgenommen, eine innere Funktion nicht - deren Namen sind woanders zu Hause.
    """
    ergebnis: list[ast.stmt] = []
    stapel: list[ast.stmt] = list(getattr(bereich, "body", []))
    while stapel:
        stmt = stapel.pop()
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        ergebnis.append(stmt)
        for feld in ("body", "orelse", "finalbody"):
            stapel.extend(getattr(stmt, feld, []) or [])
        for handler in getattr(stmt, "handlers", []) or []:
            stapel.extend(handler.body)
    return ergebnis


def _zuweisungen(bereich: ast.AST, name: str) -> list[ast.expr]:
    """Alle Werte, die im Bereich an `name` zugewiesen werden."""
    werte: list[ast.expr] = []
    for stmt in _koerper(bereich):
        if isinstance(stmt, ast.Assign):
            for ziel in stmt.targets:
                if isinstance(ziel, ast.Name) and ziel.id == name:
                    werte.append(stmt.value)
        elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
            if isinstance(stmt.target, ast.Name) and stmt.target.id == name:
                werte.append(stmt.value)
    return werte


def _parameter(funktion: ast.AST, name: str) -> tuple[bool, Optional[ast.expr]]:
    """(ist Parameter?, Vorgabewert) fuer einen Parameternamen."""
    args = getattr(funktion, "args", None)
    if args is None:
        return (False, None)
    positionell = list(args.posonlyargs) + list(args.args)
    versatz = len(positionell) - len(args.defaults)
    for i, arg in enumerate(positionell):
        if arg.arg == name:
            return (True, args.defaults[i - versatz] if i >= versatz else None)
    for arg, vorgabe in zip(args.kwonlyargs, args.kw_defaults):
        if arg.arg == name:
            return (True, vorgabe)
    return (False, None)


def _modulaliase(quelle: Quelle) -> dict[str, str]:
    """Namen aus `from . import config, recovery` auf Modulnamen abbilden."""
    aliase: dict[str, str] = {}
    for knoten in quelle.baum.body:
        if isinstance(knoten, ast.ImportFrom) and knoten.level and not knoten.module:
            for alias in knoten.names:
                aliase[alias.asname or alias.name] = alias.name
    return aliase


def _importierte_namen(quelle: Quelle, modul: str) -> dict[str, str]:
    """`from subprocess import run as r` -> {"r": "run"}."""
    namen: dict[str, str] = {}
    for knoten in ast.walk(quelle.baum):
        if isinstance(knoten, ast.ImportFrom) and knoten.module == modul and not knoten.level:
            for alias in knoten.names:
                namen[alias.asname or alias.name] = alias.name
    return namen


class Umgebung:
    """Wo ein Ausdruck steht: Datei, Bereichskette und gebundene Parameter."""

    def __init__(self, quelle: Quelle, bereiche: tuple,
                 bindungen: Optional[dict[str, tuple]] = None):
        self.quelle = quelle
        #: aeusserster Bereich zuerst: Module, ggf. ClassDef, dann FunctionDef
        self.bereiche = bereiche
        #: Parametername -> (Knoten des Arguments, Umgebung des Aufrufers)
        self.bindungen = bindungen or {}

    def klasse(self) -> Optional[ast.ClassDef]:
        for bereich in reversed(self.bereiche):
            if isinstance(bereich, ast.ClassDef):
                return bereich
        return None


# --------------------------------------------------------------------------
# Aufloesung des gestarteten Programms
# --------------------------------------------------------------------------

class Aufloeser:
    """Loest statisch auf, welches Programm ein Startaufruf ausfuehrt.

    Der Programmname steht selten direkt im Aufruf: `subprocess.Popen(cmd)`
    zeigt auf eine Variable, die aus `build_command()` stammt, die wiederum
    eine Liste mit `config.CLAUDE_BIN` an erster Stelle baut. Deshalb wird die
    Kette verfolgt - ueber Zuweisungen, Rueckgabewerte und Parameter.

    Bekannte Grenze: ein Parameter ohne Vorgabewert bleibt unaufloesbar (und
    damit rot), ein Vorgabewert None gilt als "kein Programm" (`binary or
    config.NOTIFY_BIN`). Aufrufstellen werden nicht rueckwaerts verfolgt.
    """

    #: Reissleine gegen Ketten ohne Ende.
    TIEFE = 15

    def __init__(self, quellen: list[Quelle]):
        self.nach_modul = {q.modul: q for q in quellen}
        self.aliase = {q.modul: _modulaliase(q) for q in quellen}

    # ---------------------------------------------------------------- oeffentlich

    def programme(self, knoten: Optional[ast.AST], umg: Umgebung,
                  besucht: frozenset = frozenset(), tiefe: int = 0) -> set[str]:
        """Menge der Programmnamen, die dieser Ausdruck starten kann."""
        if knoten is None:
            return {UNBEKANNT % "kein Argument"}
        if tiefe > self.TIEFE or id(knoten) in besucht:
            return {UNBEKANNT % "Kette zu lang"}
        besucht = besucht | {id(knoten)}

        if isinstance(knoten, ast.Constant):
            if knoten.value is None:
                return set()          # "kein Programm", z.B. Vorgabewert None
            if isinstance(knoten.value, str):
                return {knoten.value}
            return {UNBEKANNT % _kurz(knoten)}
        if isinstance(knoten, (ast.List, ast.Tuple)):
            if not knoten.elts:
                return set()
            return self.programme(knoten.elts[0], umg, besucht, tiefe + 1)
        if isinstance(knoten, ast.Starred):
            return self.programme(knoten.value, umg, besucht, tiefe + 1)
        if isinstance(knoten, ast.BinOp) and isinstance(knoten.op, ast.Add):
            links = self.programme(knoten.left, umg, besucht, tiefe + 1)
            return links or self.programme(knoten.right, umg, besucht, tiefe + 1)
        if isinstance(knoten, ast.BoolOp):
            ergebnis: set[str] = set()
            for wert in knoten.values:
                ergebnis |= self.programme(wert, umg, besucht, tiefe + 1)
            return ergebnis
        if isinstance(knoten, ast.IfExp):
            return (self.programme(knoten.body, umg, besucht, tiefe + 1)
                    | self.programme(knoten.orelse, umg, besucht, tiefe + 1))
        if isinstance(knoten, ast.Attribute):
            return self._attribut(knoten, umg, besucht, tiefe)
        if isinstance(knoten, ast.Name):
            return self._name(knoten, umg, besucht, tiefe)
        if isinstance(knoten, ast.Call):
            return self._aufruf(knoten, umg, besucht, tiefe)
        return {UNBEKANNT % _kurz(knoten)}

    # ------------------------------------------------------------------ intern

    def _attribut(self, knoten: ast.Attribute, umg: Umgebung,
                  besucht: frozenset, tiefe: int) -> set[str]:
        if isinstance(knoten.value, ast.Name) and knoten.value.id == "self":
            return self._selbst(knoten.attr, umg, besucht, tiefe)
        punkt = _punktname(knoten)
        if punkt is not None and knoten.attr.isupper():
            # Konfigurationsvariable wie config.CLAUDE_BIN: der Name selbst ist
            # der Befund, gegen den die Allowlist prueft.
            return {punkt}
        return {UNBEKANNT % _kurz(knoten)}

    def _selbst(self, attribut: str, umg: Umgebung,
                besucht: frozenset, tiefe: int) -> set[str]:
        klasse = umg.klasse()
        if klasse is None:
            return {UNBEKANNT % ("self.%s ausserhalb einer Klasse" % attribut)}
        ergebnis: set[str] = set()
        gefunden = False
        for methode in klasse.body:
            if not isinstance(methode, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for stmt in _koerper(methode):
                if not isinstance(stmt, ast.Assign):
                    continue
                for ziel in stmt.targets:
                    if (isinstance(ziel, ast.Attribute) and ziel.attr == attribut
                            and isinstance(ziel.value, ast.Name)
                            and ziel.value.id == "self"):
                        gefunden = True
                        kind = Umgebung(umg.quelle,
                                        (umg.bereiche[0], klasse, methode))
                        ergebnis |= self.programme(stmt.value, kind, besucht, tiefe + 1)
        if not gefunden:
            return {UNBEKANNT % ("self.%s" % attribut)}
        return ergebnis

    def _name(self, knoten: ast.Name, umg: Umgebung,
              besucht: frozenset, tiefe: int) -> set[str]:
        gebunden = umg.bindungen.get(knoten.id)
        if gebunden is not None:
            wert, aufrufer = gebunden
            return self.programme(wert, aufrufer, besucht, tiefe + 1)
        for bereich in reversed(umg.bereiche):
            werte = _zuweisungen(bereich, knoten.id)
            if werte:
                ergebnis: set[str] = set()
                for wert in werte:
                    ergebnis |= self.programme(wert, umg, besucht, tiefe + 1)
                return ergebnis
            ist_parameter, vorgabe = _parameter(bereich, knoten.id)
            if ist_parameter:
                if vorgabe is None:
                    return {UNBEKANNT % ("Parameter %s" % knoten.id)}
                return self.programme(vorgabe, umg, besucht, tiefe + 1)
        return {UNBEKANNT % ("Name %s" % knoten.id)}

    def _aufruf(self, knoten: ast.Call, umg: Umgebung,
                besucht: frozenset, tiefe: int) -> set[str]:
        ziel = self._funktion(knoten.func, umg)
        if ziel is None:
            return {UNBEKANNT % ("Aufruf %s" % _kurz(knoten.func))}
        quelle, bereiche, funktion = ziel
        kind = Umgebung(quelle, bereiche + (funktion,),
                        self._binde(funktion, knoten, umg))
        rueckgaben = [stmt.value for stmt in _koerper(funktion)
                      if isinstance(stmt, ast.Return) and stmt.value is not None]
        if not rueckgaben:
            return {UNBEKANNT % ("%s gibt nichts zurueck" % funktion.name)}
        ergebnis: set[str] = set()
        for wert in rueckgaben:
            ergebnis |= self.programme(wert, kind, besucht, tiefe + 1)
        return ergebnis

    def _funktion(self, func: ast.AST, umg: Umgebung) -> Optional[tuple]:
        """Aufgerufene Funktion samt Datei und Bereichskette finden."""
        if isinstance(func, ast.Name):
            treffer = self._modulfunktion(umg.quelle, func.id)
            if treffer is not None:
                return treffer
            return None
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            if func.value.id == "self":
                klasse = umg.klasse()
                if klasse is None:
                    return None
                for stmt in klasse.body:
                    if (isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef))
                            and stmt.name == func.attr):
                        return (umg.quelle, (umg.bereiche[0], klasse), stmt)
                return None
            modul = self.aliase.get(umg.quelle.modul, {}).get(func.value.id)
            fremd = self.nach_modul.get(modul) if modul else None
            if fremd is not None:
                return self._modulfunktion(fremd, func.attr)
        return None

    def _modulfunktion(self, quelle: Quelle, name: str) -> Optional[tuple]:
        for stmt in quelle.baum.body:
            if (isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and stmt.name == name):
                return (quelle, (quelle.baum,), stmt)
        return None

    @staticmethod
    def _binde(funktion: ast.AST, aufruf: ast.Call, umg: Umgebung) -> dict[str, tuple]:
        """Parameter der Zielfunktion an die Argumente der Aufrufstelle binden."""
        args = getattr(funktion, "args", None)
        if args is None:
            return {}
        positionell = list(args.posonlyargs) + list(args.args)
        if positionell and positionell[0].arg in ("self", "cls"):
            positionell = positionell[1:]
        bindungen: dict[str, tuple] = {}
        for arg, wert in zip(positionell, aufruf.args):
            bindungen[arg.arg] = (wert, umg)
        erlaubt = {a.arg for a in positionell} | {a.arg for a in args.kwonlyargs}
        for kw in aufruf.keywords:
            if kw.arg in erlaubt:
                bindungen[kw.arg] = (kw.value, umg)
        return bindungen


# --------------------------------------------------------------------------
# Die drei Pruefungen - jede gibt eine Befundliste zurueck
# --------------------------------------------------------------------------

def pruefe_importe(quellen: list[Quelle]) -> list[str]:
    """Befunde zu Importen aus der Sperrliste."""
    befunde: list[str] = []
    for quelle in quellen:
        for knoten in ast.walk(quelle.baum):
            namen: list[str] = []
            if isinstance(knoten, ast.Import):
                namen = [alias.name for alias in knoten.names]
            elif isinstance(knoten, ast.ImportFrom) and knoten.module and not knoten.level:
                namen = [knoten.module]
            for name in namen:
                if name.split(".")[0] in GESPERRTE_MODULE:
                    befunde.append("%s:%d: gesperrter Import '%s'"
                                   % (quelle.name, knoten.lineno, name))
    return befunde


def sammle_startaufrufe(quellen: list[Quelle]) -> list[tuple[Quelle, ast.Call, Umgebung]]:
    """Jeden Programmstart im Code finden, samt Umgebung fuer die Aufloesung."""
    treffer: list[tuple[Quelle, ast.Call, Umgebung]] = []
    for quelle in quellen:
        direkt = _importierte_namen(quelle, "subprocess")

        def besuche(knoten: ast.AST, bereiche: tuple, quelle: Quelle = quelle,
                    direkt: dict[str, str] = direkt) -> None:
            for kind in ast.iter_child_nodes(knoten):
                if isinstance(kind, ast.Call) and _startname(kind.func, direkt):
                    treffer.append((quelle, kind, Umgebung(quelle, bereiche)))
                weiter = bereiche
                if isinstance(kind, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    weiter = bereiche + (kind,)
                besuche(kind, weiter)

        besuche(quelle.baum, (quelle.baum,))
    return treffer


def _startname(func: ast.AST, direkt: dict[str, str]) -> Optional[str]:
    """Name der Startfunktion, wenn dieser Aufruf ein Programm startet."""
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        if func.value.id == "subprocess" and func.attr in STARTFUNKTIONEN:
            return func.attr
    if isinstance(func, ast.Name) and direkt.get(func.id) in STARTFUNKTIONEN:
        return direkt[func.id]
    return None


def _erstes_argument(aufruf: ast.Call) -> Optional[ast.expr]:
    if aufruf.args:
        return aufruf.args[0]
    for kw in aufruf.keywords:
        if kw.arg == "args":
            return kw.value
    return None


def sammle_programme(quellen: list[Quelle]) -> list[tuple[str, int, str]]:
    """(Datei, Zeile, Programmname) fuer jeden Programmstart im Code."""
    aufloeser = Aufloeser(quellen)
    ergebnis: list[tuple[str, int, str]] = []
    for quelle, aufruf, umg in sammle_startaufrufe(quellen):
        namen = aufloeser.programme(_erstes_argument(aufruf), umg)
        if not namen:
            namen = {UNBEKANNT % "leeres Kommando"}
        for name in sorted(namen):
            ergebnis.append((quelle.name, aufruf.lineno, name))
    return ergebnis


def pruefe_programmstarts(quellen: list[Quelle]) -> list[str]:
    """Befunde zu Programmen ausserhalb der Allowlist."""
    return ["%s:%d: startet '%s' - nicht in der Allowlist %s"
            % (datei, zeile, programm, sorted(ERLAUBTE_PROGRAMME))
            for datei, zeile, programm in sammle_programme(quellen)
            if programm not in ERLAUBTE_PROGRAMME]


def pruefe_shell(quellen: list[Quelle]) -> list[str]:
    """Befunde zu shell=True, os.system/os.popen und Shell-Helfern."""
    befunde: list[str] = []
    for quelle in quellen:
        aus_os = _importierte_namen(quelle, "os")
        aus_subprocess = _importierte_namen(quelle, "subprocess")
        for knoten in ast.walk(quelle.baum):
            if not isinstance(knoten, ast.Call):
                continue
            for kw in knoten.keywords:
                if kw.arg != "shell":
                    continue
                aus = isinstance(kw.value, ast.Constant) and kw.value.value is False
                if not aus:
                    befunde.append("%s:%d: shell=%s - die Shell umgeht die Allowlist"
                                   % (quelle.name, knoten.lineno, _kurz(kw.value)))
            gerufen = _punktname(knoten.func)
            if gerufen in ("os.system", "os.popen"):
                befunde.append("%s:%d: %s() startet ueber die Shell"
                               % (quelle.name, knoten.lineno, gerufen))
            elif (isinstance(knoten.func, ast.Attribute)
                  and isinstance(knoten.func.value, ast.Name)
                  and knoten.func.value.id == "subprocess"
                  and knoten.func.attr in SHELLFUNKTIONEN):
                befunde.append("%s:%d: subprocess.%s() startet ueber die Shell"
                               % (quelle.name, knoten.lineno, knoten.func.attr))
            elif isinstance(knoten.func, ast.Name):
                if aus_os.get(knoten.func.id) in ("system", "popen"):
                    befunde.append("%s:%d: os.%s() startet ueber die Shell"
                                   % (quelle.name, knoten.lineno,
                                      aus_os[knoten.func.id]))
                elif aus_subprocess.get(knoten.func.id) in SHELLFUNKTIONEN:
                    befunde.append("%s:%d: subprocess.%s() startet ueber die Shell"
                                   % (quelle.name, knoten.lineno,
                                      aus_subprocess[knoten.func.id]))
    return befunde


# --------------------------------------------------------------------------
# Attrappen fuer die Gegenprobe
# --------------------------------------------------------------------------

ATTRAPPE_NETZ = '''\
"""Attrappe: greift verbotenerweise aufs Netz zu."""

from __future__ import annotations

import socket
import http.client
from urllib import request


def hole(url):
    with request.urlopen(url) as antwort:
        return antwort.read()
'''

ATTRAPPE_FREMDPROGRAMM = '''\
"""Attrappe: startet nicht gelistete Programme."""

from __future__ import annotations

import subprocess


def hole(url):
    return subprocess.run(["curl", "-s", url], capture_output=True)


def scanne(ziel):
    programm = "nmap"
    return subprocess.Popen([programm, "-sV", ziel])
'''

ATTRAPPE_SHELL = '''\
"""Attrappe: umgeht die Allowlist ueber die Shell."""

from __future__ import annotations

import os
import subprocess


def raeume_auf(pfad):
    subprocess.run("rm -rf " + pfad, shell=True)
    os.system("echo hallo")
    return os.popen("whoami").read()
'''

ATTRAPPE_UNDURCHSICHTIG = '''\
"""Attrappe: das Programm steht erst zur Laufzeit fest."""

from __future__ import annotations

import subprocess


def starte(programm):
    return subprocess.Popen([programm, "--los"])
'''

ATTRAPPE_ERLAUBT = '''\
"""Attrappe: startet ausschliesslich erlaubte Programme."""

from __future__ import annotations

import subprocess

from . import config


def frage_sessions_ab():
    return subprocess.run([config.CLAUDE_BIN, "agents", "--json"], capture_output=True)


def melde(titel):
    return subprocess.Popen(["notify-send", titel], shell=False)
'''


def zeile_von(text: str, teil: str) -> int:
    """Zeilennummer des ersten Vorkommens - fuer die Pruefung der Meldungen."""
    for nummer, zeile in enumerate(text.splitlines(), start=1):
        if teil in zeile:
            return nummer
    return -1


def attrappe(name: str, text: str) -> list[Quelle]:
    return [quelle_aus_text(name, text)]


# --------------------------------------------------------------------------
# Der echte Code
# --------------------------------------------------------------------------

class TestEchterCodeIstLokal(unittest.TestCase):
    """claude_watchdog/*.py: kein Netz, keine fremden Programme, keine Shell."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.quellen = echte_quellen()

    def test_es_wird_ueberhaupt_etwas_geprueft(self):
        # Ohne diese Zusicherung waere ein leeres Glob-Ergebnis "gruen".
        dateien = sorted(p.relative_to(PAKET).as_posix() for p in paketdateien())
        self.assertEqual(sorted(q.name for q in self.quellen), dateien)
        self.assertGreater(len(self.quellen), 5)
        self.assertIn("recovery.py", dateien)

    def test_kein_import_aus_der_sperrliste(self):
        self.assertEqual(pruefe_importe(self.quellen), [])

    def test_nur_erlaubte_programme(self):
        self.assertEqual(pruefe_programmstarts(self.quellen), [])

    def test_keine_shell(self):
        self.assertEqual(pruefe_shell(self.quellen), [])

    def test_programmstarts_werden_wirklich_gefunden(self):
        # Sonst koennte die Allowlist-Pruefung gruen sein, weil sie nichts sieht.
        starts = sammle_startaufrufe(self.quellen)
        self.assertGreater(len(starts), 0)
        dateien = {quelle.name for quelle, _, _ in starts}
        for erwartet in ("detector.py", "recovery.py", "notifier.py", "cli.py"):
            self.assertIn(erwartet, dateien)

    def test_die_aufloesung_dringt_bis_zum_programmnamen_durch(self):
        # Der Programmname steht nirgends direkt im Popen-Aufruf. Wenn die
        # Aufloesung stillschweigend aufgibt, bleibt diese Menge leer.
        gefunden = {programm for _, _, programm in sammle_programme(self.quellen)}
        self.assertIn("config.CLAUDE_BIN", gefunden)
        self.assertIn("config.NOTIFY_BIN", gefunden)
        self.assertIn("systemd-run", gefunden)
        self.assertTrue(gefunden.issubset(ERLAUBTE_PROGRAMME),
                        "unerwartete Programme: %s" % sorted(gefunden - ERLAUBTE_PROGRAMME))


# --------------------------------------------------------------------------
# Gegenprobe: schlagen die Pruefungen an Attrappen wirklich an?
# --------------------------------------------------------------------------

class TestGegenprobeNetz(unittest.TestCase):
    """Dieselbe Pruefung, an einer Attrappe mit Netzzugriff."""

    def setUp(self) -> None:
        self.quellen = attrappe("attrappe_netz.py", ATTRAPPE_NETZ)
        self.befunde = pruefe_importe(self.quellen)

    def test_jeder_gesperrte_import_wird_gemeldet(self):
        self.assertEqual(len(self.befunde), 3)

    def test_untermodule_zaehlen_mit(self):
        text = " | ".join(self.befunde)
        for name in ("socket", "http.client", "urllib"):
            self.assertIn(name, text)

    def test_meldung_nennt_datei_und_zeile(self):
        erwartet = "attrappe_netz.py:%d:" % zeile_von(ATTRAPPE_NETZ, "import socket")
        self.assertTrue(any(b.startswith(erwartet) for b in self.befunde),
                        "keine Meldung mit Datei und Zeile: %s" % self.befunde)


class TestGegenprobeFremdprogramm(unittest.TestCase):
    """Dieselbe Pruefung, an einer Attrappe mit curl und nmap."""

    def setUp(self) -> None:
        self.quellen = attrappe("attrappe_fremd.py", ATTRAPPE_FREMDPROGRAMM)
        self.befunde = pruefe_programmstarts(self.quellen)

    def test_beide_fremdstarts_werden_gemeldet(self):
        self.assertEqual(len(self.befunde), 2)

    def test_direkt_genanntes_programm_faellt_auf(self):
        self.assertIn("curl", " | ".join(self.befunde))

    def test_umweg_ueber_eine_variable_hilft_nicht(self):
        self.assertIn("nmap", " | ".join(self.befunde))

    def test_meldung_nennt_datei_und_zeile(self):
        erwartet = "attrappe_fremd.py:%d:" % zeile_von(ATTRAPPE_FREMDPROGRAMM, "curl")
        self.assertTrue(any(b.startswith(erwartet) for b in self.befunde),
                        "keine Meldung mit Datei und Zeile: %s" % self.befunde)

    def test_unaufloesbares_programm_ist_ebenfalls_ein_befund(self):
        # Ein Start, dessen Programm erst zur Laufzeit feststeht, darf nicht
        # als "nichts gefunden" durchgehen.
        befunde = pruefe_programmstarts(
            attrappe("attrappe_undurchsichtig.py", ATTRAPPE_UNDURCHSICHTIG))
        self.assertEqual(len(befunde), 1)
        self.assertIn("nicht aufloesbar", befunde[0])


class TestGegenprobeShell(unittest.TestCase):
    """Dieselbe Pruefung, an einer Attrappe mit shell=True und os.system."""

    def setUp(self) -> None:
        self.quellen = attrappe("attrappe_shell.py", ATTRAPPE_SHELL)
        self.befunde = pruefe_shell(self.quellen)

    def test_alle_drei_shell_wege_werden_gemeldet(self):
        self.assertEqual(len(self.befunde), 3)

    def test_shell_true_faellt_auf(self):
        self.assertIn("shell=True", " | ".join(self.befunde))

    def test_os_system_und_os_popen_fallen_auf(self):
        text = " | ".join(self.befunde)
        self.assertIn("os.system", text)
        self.assertIn("os.popen", text)

    def test_meldung_nennt_datei_und_zeile(self):
        erwartet = "attrappe_shell.py:%d:" % zeile_von(ATTRAPPE_SHELL, "shell=True")
        self.assertTrue(any(b.startswith(erwartet) for b in self.befunde),
                        "keine Meldung mit Datei und Zeile: %s" % self.befunde)


class TestGegenprobeSchlaegtNichtBlindAn(unittest.TestCase):
    """Eine Attrappe, die sich an die Regeln haelt, muss gruen bleiben.

    Ohne diesen Fall koennte jede Pruefung schlicht alles melden und die
    Gegenprobe waere wertlos.
    """

    def setUp(self) -> None:
        self.quellen = attrappe("attrappe_erlaubt.py", ATTRAPPE_ERLAUBT)

    def test_erlaubte_importe_bleiben_still(self):
        self.assertEqual(pruefe_importe(self.quellen), [])

    def test_erlaubte_programme_bleiben_still(self):
        self.assertEqual(pruefe_programmstarts(self.quellen), [])

    def test_shell_false_ist_kein_befund(self):
        self.assertEqual(pruefe_shell(self.quellen), [])

    def test_die_starts_wurden_trotzdem_gesehen(self):
        gefunden = {programm for _, _, programm in sammle_programme(self.quellen)}
        self.assertEqual(gefunden, {"config.CLAUDE_BIN", "notify-send"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
