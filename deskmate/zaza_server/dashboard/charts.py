"""Server-rendered SVG charts (no JavaScript, no chart library, no CDN).

:func:`render` turns a :class:`ChartData` into a list of plain shapes
(rect / line / text with numeric coordinates); ``templates/_charts.html``
writes them as inline SVG. Every piece of text — employee names,
application names — is placed as SVG *text content* through the
auto-escaping template, so nothing from the database becomes markup or
script, and the strict Content-Security-Policy is unchanged (no inline
scripts or styles: colours come from CSS classes in ``dashboard.css``).

Accessibility: each chart has a title and description (``role="img"``,
``aria-labelledby``), a legend, values printed as text, a hover ``<title>``
on every bar, patterns besides colour for each series, and the same numbers
in a data table below it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .analysis import ChartData

WIDTH = 640
LABEL = 190      # left column for category labels (horizontal charts)
VALUE_PAD = 76   # right room for value text
ROW = 26
PATTERNED = {"idle", "unknown", "locked", "early", "absent", "incomplete"}


def fmt_value(value: float | None, unit: str, *, compact: bool = True) -> str:
    if value is None:
        return "–"
    if unit == "days":
        return f"{int(value)} day{'s' if int(value) != 1 else ''}" if not compact else f"{int(value)}"
    if compact:
        return f"{value:.1f} h"
    minutes = int(round(value * 60))
    return f"{minutes // 60}h {minutes % 60:02d}m"


def _short(text: str, limit: int = 28) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


@dataclass
class Svg:
    id: str
    title: str
    description: str
    width: int
    height: int
    unit: str
    kind: str
    note: str
    empty: bool
    empty_message: str
    all_zero: bool
    legend: list[tuple[str, str]] = field(default_factory=list)   # (key, label)
    patterns: list[str] = field(default_factory=list)              # series keys with a pattern
    shapes: list[dict] = field(default_factory=list)
    table_headers: list[str] = field(default_factory=list)
    table_rows: list[list[str]] = field(default_factory=list)


def _table(data: ChartData) -> tuple[list[str], list[list[str]]]:
    unit = " (hours)" if data.unit == "h" else ""
    headers = [""] + [s.label + unit for s in data.series]
    rows = [[cat] + [fmt_value(v, data.unit, compact=False) for v in vals]
            for cat, vals in zip(data.categories, data.values, strict=True)]
    return headers, rows


def _axis(maximum: float, unit: str) -> list[float]:
    if maximum <= 0:
        return [0.0]
    if unit == "days":
        step = max(1, int(maximum // 4) or 1)
        return [float(v) for v in range(0, int(maximum) + step, step)][:6]
    return [maximum * i / 4 for i in range(5)]


def render(data: ChartData, prefix: str = "") -> Svg:
    cid = f"{prefix}{data.key}"
    headers, rows = _table(data)
    svg = Svg(cid, data.title, data.description, WIDTH, 60, data.unit, data.kind, data.note, data.is_empty,
              data.empty, data.all_zero, [(s.key, s.label) for s in data.series],
              [s.key for s in data.series if s.key in PATTERNED], [], headers, rows)
    if data.is_empty:
        return svg
    if data.kind == "columns":
        _columns(svg, data)
    else:
        _horizontal(svg, data)
    return svg


def _horizontal(svg: Svg, data: ChartData) -> None:
    span = WIDTH - LABEL - VALUE_PAD
    if data.kind == "stacked":
        maximum = max((sum(v or 0 for v in vals) for vals in data.values), default=0)
    else:
        maximum = max((v or 0 for vals in data.values for v in vals), default=0)
    scale = span / maximum if maximum > 0 else 0
    group = ROW if data.kind != "grouped" else 12 * len(data.series) + 12
    top = 8
    shapes = svg.shapes
    for i, (cat, vals) in enumerate(zip(data.categories, data.values, strict=True)):
        y = top + i * group
        shapes.append({"t": "text", "x": LABEL - 8, "y": y + group / 2 + 4, "cls": "c-label", "anchor": "end",
                       "text": _short(cat), "title": cat})
        if data.kind == "stacked":
            x = LABEL
            for s, v in zip(data.series, vals, strict=True):
                w = (v or 0) * scale
                if w > 0:
                    tip = f"{cat} — {s.label}: {fmt_value(v, data.unit, compact=False)}"
                    shapes.append({"t": "rect", "x": x, "y": y + 4, "w": w, "h": ROW - 10, "cls": f"c-{s.key}",
                                   "pattern": s.key if s.key in PATTERNED else None, "title": tip})
                    x += w
            total = sum(v or 0 for v in vals)
            shapes.append({"t": "text", "x": x + 6, "y": y + ROW / 2 + 4, "cls": "c-value", "anchor": "start",
                           "text": fmt_value(total, data.unit)})
        else:
            height = ROW - 10 if data.kind == "bar" else 10
            for j, (s, v) in enumerate(zip(data.series, vals, strict=True)):
                by = y + 4 if data.kind == "bar" else y + 6 + j * 12
                w = (v or 0) * scale
                tip = f"{cat} — {s.label}: {fmt_value(v, data.unit, compact=False)}"
                if w > 0:
                    shapes.append({"t": "rect", "x": LABEL, "y": by, "w": w, "h": height, "cls": f"c-{s.key}",
                                   "pattern": s.key if s.key in PATTERNED else None, "title": tip})
                shapes.append({"t": "text", "x": LABEL + w + 6, "y": by + height - 1, "cls": "c-value",
                               "anchor": "start", "text": fmt_value(v, data.unit)})
    bottom = top + len(data.categories) * group + 4
    for tick in _axis(maximum, data.unit):
        x = LABEL + tick * scale
        shapes.append({"t": "line", "x1": x, "y1": top, "x2": x, "y2": bottom, "cls": "c-grid"})
        shapes.append({"t": "text", "x": x, "y": bottom + 14, "cls": "c-axis", "anchor": "middle",
                       "text": fmt_value(tick, data.unit)})
    svg.height = int(bottom + 24)


def _columns(svg: Svg, data: ChartData) -> None:
    left, top, plot_h = 56, 10, 180
    n = len(data.categories)
    span = WIDTH - left - 10
    step = span / n
    maximum = max((v or 0 for vals in data.values for v in vals), default=0)
    scale = plot_h / maximum if maximum > 0 else 0
    base = top + plot_h
    shapes = svg.shapes
    for tick in _axis(maximum, data.unit):
        y = base - tick * scale
        shapes.append({"t": "line", "x1": left, "y1": y, "x2": WIDTH - 10, "y2": y, "cls": "c-grid"})
        shapes.append({"t": "text", "x": left - 6, "y": y + 4, "cls": "c-axis", "anchor": "end",
                       "text": fmt_value(tick, data.unit)})
    every = max(1, -(-n // 10))  # at most ~10 date labels
    for i, (cat, vals) in enumerate(zip(data.categories, data.values, strict=True)):
        v = vals[0]
        x = left + i * step
        if v is not None and v > 0:
            h = v * scale
            shapes.append({"t": "rect", "x": x + step * 0.15, "y": base - h, "w": step * 0.7, "h": h,
                           "cls": "c-active", "pattern": None,
                           "title": f"{cat} — {data.series[0].label}: {fmt_value(v, data.unit, compact=False)}"})
        if i % every == 0:
            shapes.append({"t": "text", "x": x + step / 2, "y": base + 16, "cls": "c-axis", "anchor": "middle",
                           "text": cat})
    shapes.append({"t": "line", "x1": left, "y1": base, "x2": WIDTH - 10, "y2": base, "cls": "c-baseline"})
    svg.height = base + 28
