"""Non-overlapping timelines and interval arithmetic (epoch seconds, UTC).

``normalize`` turns possibly-overlapping activity periods (several devices,
or bad data) into one non-overlapping timeline. Where periods overlap, the
instant takes the single highest-priority status:

    ACTIVE > IDLE > LOCKED > UNKNOWN

(the employee was demonstrably working on *some* device; a definite IDLE /
LOCKED reading beats an uncertain one). Every instant is therefore counted
at most once, whatever the input looks like.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from .models import STATUS_PRIORITY

Interval = tuple[float, float]  # [start, end)


@dataclass(frozen=True)
class Segment:
    start: float
    end: float
    status: str

    @property
    def seconds(self) -> float:
        return self.end - self.start


@dataclass(frozen=True)
class Timeline:
    segments: tuple[Segment, ...]
    overlap_seconds: float  # raw input time that overlapped other input (removed)


def normalize(periods: Iterable[tuple[float, float, str]]) -> Timeline:
    events: list[tuple[float, int, str]] = []
    raw_total = 0.0
    for start, end, status in periods:
        if end <= start or status not in STATUS_PRIORITY:
            continue
        raw_total += end - start
        events.append((start, 1, status))
        events.append((end, -1, status))
    if not events:
        return Timeline((), 0.0)
    events.sort(key=lambda e: (e[0], e[1]))
    counts = dict.fromkeys(STATUS_PRIORITY, 0)
    segments: list[Segment] = []
    prev_t: float | None = None
    for t, delta, status in events:
        if prev_t is not None and t > prev_t:
            current = max((s for s, n in counts.items() if n > 0), key=STATUS_PRIORITY.get, default=None)
            if current is not None:
                if segments and segments[-1].status == current and segments[-1].end == prev_t:
                    segments[-1] = Segment(segments[-1].start, t, current)
                else:
                    segments.append(Segment(prev_t, t, current))
        counts[status] += delta
        prev_t = t
    union = sum(s.seconds for s in segments)
    return Timeline(tuple(segments), max(0.0, raw_total - union))


def merge(intervals: Iterable[Interval]) -> list[Interval]:
    out: list[Interval] = []
    for a, b in sorted(i for i in intervals if i[1] > i[0]):
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def intersect(a: Sequence[Interval], b: Sequence[Interval]) -> list[Interval]:
    out: list[Interval] = []
    i = j = 0
    a, b = merge(a), merge(b)
    while i < len(a) and j < len(b):
        lo, hi = max(a[i][0], b[j][0]), min(a[i][1], b[j][1])
        if hi > lo:
            out.append((lo, hi))
        if a[i][1] < b[j][1]:
            i += 1
        else:
            j += 1
    return out


def clip(timeline: Sequence[Segment], intervals: Sequence[Interval]) -> list[Segment]:
    """The parts of ``timeline`` inside ``intervals``."""
    out: list[Segment] = []
    for seg in timeline:
        for lo, hi in intersect([(seg.start, seg.end)], intervals):
            out.append(Segment(lo, hi, seg.status))
    return sorted(out, key=lambda s: s.start)


def seconds(segments: Iterable[Segment], *statuses: str) -> float:
    return sum(s.seconds for s in segments if not statuses or s.status in statuses)


def total(intervals: Iterable[Interval]) -> float:
    return sum(b - a for a, b in merge(intervals))
