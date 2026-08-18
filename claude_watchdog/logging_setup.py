"""Strukturiertes Logging als JSON Lines mit Rotation.

Jede Entscheidung des Watchdogs landet hier als eine Zeile, damit im
Nachhinein nachvollziehbar ist, warum etwas passiert ist.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import sys
import time
from typing import Any

from . import config

_STD_ATTRS = {
    "args", "asctime", "created", "exc_info", "exc_text", "filename",
    "funcName", "levelname", "levelno", "lineno", "module", "msecs",
    "message", "msg", "name", "pathname", "process", "processName",
    "relativeCreated", "stack_info", "thread", "threadName", "taskName",
}


class JsonLinesFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": round(record.created, 3),
            "time": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STD_ATTRS and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        try:
            return json.dumps(payload, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return json.dumps({"ts": payload["ts"], "level": payload["level"],
                               "msg": payload["msg"], "note": "payload not serializable"})


def setup(verbose: bool = False, to_console: bool = True) -> logging.Logger:
    config.ensure_dirs()
    root = logging.getLogger("cw")
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    root.handlers.clear()
    root.propagate = False

    file_handler = logging.handlers.RotatingFileHandler(
        config.LOG_FILE,
        maxBytes=config.LOG_MAX_BYTES,
        backupCount=config.LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setFormatter(JsonLinesFormatter())
    root.addHandler(file_handler)

    if to_console:
        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s",
                                               datefmt="%H:%M:%S"))
        root.addHandler(console)
    return root


def get(name: str) -> logging.Logger:
    return logging.getLogger(f"cw.{name}")
