"""Robustes Lesen von JSON-Lines-Dateien (Transkripte und Run-Logs).

Wichtig: Die Datei wird waehrend des Lesens weitergeschrieben. Die letzte
Zeile ist daher regelmaessig unvollstaendig und darf nicht zum Fehler fuehren.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

from . import config


def tail_bytes(path: Path | str, limit: Optional[int] = None) -> str:
    """Liest die letzten `limit` Bytes einer Datei als Text.

    Kaputte Multibyte-Sequenzen am Schnittpunkt werden ersetzt, nicht geworfen.
    """
    limit = limit or config.TAIL_BYTES
    p = Path(path)
    try:
        size = p.stat().st_size
    except OSError:
        return ""
    try:
        with p.open("rb") as fh:
            if size > limit:
                fh.seek(size - limit)
            raw = fh.read()
    except OSError:
        return ""
    return raw.decode("utf-8", errors="replace")


def tail_events(path: Path | str, max_events: int = 40,
                limit: Optional[int] = None) -> list[dict[str, Any]]:
    """Gibt die letzten vollstaendig geparsten JSON-Objekte zurueck.

    Unvollstaendige oder kaputte Zeilen werden stillschweigend uebersprungen -
    bei einer Datei, die gerade beschrieben wird, ist das der Normalfall.
    """
    text = tail_bytes(path, limit)
    if not text:
        return []
    lines = text.split("\n")
    # Erste Zeile kann durch das Byte-Fenster abgeschnitten sein.
    if len(lines) > 1:
        lines = lines[1:]
    events: list[dict[str, Any]] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(obj, dict):
            events.append(obj)
    return events[-max_events:]


def file_size(path: Path | str | None) -> int:
    if not path:
        return 0
    try:
        return os.stat(path).st_size
    except OSError:
        return 0


def file_mtime(path: Path | str | None) -> float:
    if not path:
        return 0.0
    try:
        return os.stat(path).st_mtime
    except OSError:
        return 0.0


def newest_transcript(cwd: str) -> Optional[Path]:
    """Neueste Transkript-Datei im Projektverzeichnis eines cwd.

    Dient als Fallback, wenn zu einem Task keine session_id bekannt ist -
    der Dateiname ist die Session-UUID.
    """
    directory = config.project_dir(cwd)
    if not directory.is_dir():
        return None
    candidates = [p for p in directory.glob("*.jsonl") if p.is_file()]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def session_id_from_path(path: Path | str | None) -> Optional[str]:
    if not path:
        return None
    return Path(path).stem or None


def extract_text(events: list[dict[str, Any]], max_chars: int = 8000) -> str:
    """Sammelt menschenlesbaren Text aus Events fuer die Regex-Klassifikation."""
    chunks: list[str] = []
    for ev in events:
        for key in ("result", "error", "message", "content", "text", "summary"):
            value = ev.get(key)
            if isinstance(value, str):
                chunks.append(value)
            elif isinstance(value, dict):
                content = value.get("content")
                if isinstance(content, str):
                    chunks.append(content)
                elif isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and isinstance(block.get("text"), str):
                            chunks.append(block["text"])
            elif isinstance(value, list):
                for block in value:
                    if isinstance(block, dict) and isinstance(block.get("text"), str):
                        chunks.append(block["text"])
    joined = "\n".join(chunks)
    return joined[-max_chars:]


def last_meaningful_event(events: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Letztes Event, das etwas ueber den Fortschritt aussagt.

    Reine Buchhaltungs-Eintraege (Titel, Snapshots, Modus) werden uebersprungen.
    """
    ignore = {"ai-title", "file-history-snapshot", "mode", "permission-mode",
              "last-prompt", "attachment"}
    for ev in reversed(events):
        if ev.get("type") not in ignore:
            return ev
    return events[-1] if events else None


def progress_marker(events: list[dict[str, Any]], size: int) -> str:
    """Kurzer Fingerabdruck der aktuellen Position im Transkript.

    Wird verwendet, um zu erkennen, ob ein Resume immer wieder an derselben
    Stelle scheitert (Anti-Schleifen-Regel).
    """
    last = last_meaningful_event(events)
    uid = ""
    if last:
        uid = str(last.get("uuid") or last.get("leafUuid") or last.get("type") or "")
    return f"{size}:{uid}"
