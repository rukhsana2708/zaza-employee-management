"""Bounded exponential backoff with jitter.

Defaults: ~5s, 10s, 20s, 40s, ... capped at 300s, each multiplied by a
random factor in [1 - jitter, 1 + jitter] (±20%) so many agents coming back
online don't retry in lockstep. A successful cycle resets the sequence.
"""

from __future__ import annotations

import random
from collections.abc import Callable


class Backoff:
    def __init__(
        self,
        *,
        base_seconds: float = 5.0,
        factor: float = 2.0,
        max_seconds: float = 300.0,
        jitter: float = 0.2,
        rng: Callable[[], float] = random.random,
    ) -> None:
        if base_seconds <= 0 or factor < 1 or max_seconds < base_seconds or not 0 <= jitter < 1:
            raise ValueError("invalid backoff parameters")
        self.base_seconds = base_seconds
        self.factor = factor
        self.max_seconds = max_seconds
        self.jitter = jitter
        self._rng = rng
        self.attempt = 0

    def reset(self) -> None:
        self.attempt = 0

    def next_delay(self, *, at_least: float = 0.0) -> float:
        raw = min(self.max_seconds, self.base_seconds * (self.factor ** self.attempt))
        self.attempt += 1
        jittered = raw * (1 - self.jitter + 2 * self.jitter * self._rng())
        return min(self.max_seconds, max(jittered, min(at_least, self.max_seconds)))
