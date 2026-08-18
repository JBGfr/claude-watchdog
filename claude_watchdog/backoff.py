"""Wartezeit-Berechnung.

Exponentielles Backoff mit Jitter, plus klassenspezifische Sonderregeln.
Der Zufallsgenerator ist injizierbar, damit die Tests deterministisch sind.
"""

from __future__ import annotations

import random
import time
from typing import Optional

from . import config
from .classifier import Classification
from .models import ErrorClass

#: Klassen, bei denen ein erneuter Versuch grundsaetzlich sinnvoll ist.
RETRYABLE = frozenset({
    ErrorClass.USAGE_LIMIT,
    ErrorClass.RATE_LIMIT,
    ErrorClass.API_ERROR,
    ErrorClass.NETWORK,
    ErrorClass.CONTEXT,
    ErrorClass.CRASH,
    ErrorClass.STALLED,
    ErrorClass.UNKNOWN,
})

#: Klassen, die den Versuchszaehler NICHT erhoehen duerfen. Ein Usage-Limit
#: ist kein Fehlversuch des Tasks - sonst brennt ein Kontingentende die
#: Retries eines ansonsten gesunden Tasks auf.
NOT_AN_ATTEMPT = frozenset({ErrorClass.USAGE_LIMIT, ErrorClass.RATE_LIMIT})


def exponential(attempt: int,
                base: Optional[float] = None,
                factor: Optional[float] = None,
                cap: Optional[float] = None,
                jitter: Optional[float] = None,
                rng: Optional[random.Random] = None) -> float:
    """base * factor**attempt, gedeckelt, mit +-jitter Prozent Streuung.

    `attempt` ist 0-basiert: der erste Wiederholversuch bekommt `base`.
    """
    base = config.BACKOFF_BASE if base is None else base
    factor = config.BACKOFF_FACTOR if factor is None else factor
    cap = config.BACKOFF_CAP if cap is None else cap
    jitter = config.BACKOFF_JITTER if jitter is None else jitter

    attempt = max(0, int(attempt))
    try:
        raw = base * (factor ** attempt)
    except OverflowError:
        raw = cap
    raw = min(raw, cap)

    if jitter:
        rng = rng or random
        raw *= 1.0 + rng.uniform(-jitter, jitter)
    return max(0.0, round(raw, 3))


def delay_for(classification: Classification,
              attempt: int,
              now: Optional[float] = None,
              rng: Optional[random.Random] = None) -> tuple[float, Optional[float]]:
    """Liefert (delay_sekunden, absoluter_retry_zeitpunkt_oder_None).

    Bei USAGE_LIMIT wird bis zum Reset-Zeitpunkt gewartet und NICHT vorher
    gepollt. Ohne verwertbare Reset-Zeit greift ein Fallback.
    """
    now = now if now is not None else time.time()
    cls = classification.error_class

    if cls is ErrorClass.USAGE_LIMIT:
        if classification.reset_at and classification.reset_at > now:
            retry_at = classification.reset_at + config.USAGE_LIMIT_RESET_PADDING
            return retry_at - now, retry_at
        return float(config.USAGE_LIMIT_FALLBACK_WAIT), now + config.USAGE_LIMIT_FALLBACK_WAIT

    if cls is ErrorClass.RATE_LIMIT and classification.retry_after:
        delay = float(classification.retry_after) + config.USAGE_LIMIT_RESET_PADDING
        return delay, now + delay

    delay = exponential(attempt, rng=rng)
    return delay, now + delay


def counts_as_attempt(classification: Classification) -> bool:
    return classification.error_class not in NOT_AN_ATTEMPT


def is_retryable(classification: Classification) -> bool:
    return classification.error_class in RETRYABLE
