"""Shared display-only formatting/templates. No authentication or database queries."""

from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from ..attendance.labels import FLAG_NOTES, QUALITY_LABELS, STATUS_LABELS, STATUS_NOTES
from .models import STATUS_LABELS as CURRENT_LABELS
from .models import CurrentStatus
from .service import PERIODS

HERE = Path(__file__).parent
UTC = timezone.utc


def fmt_hours(seconds: float | int | None) -> str:
    if seconds is None:
        return "–"
    minutes = int(round(seconds / 60))
    return f"{minutes // 60}h {minutes % 60:02d}m"


def fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "–"
    total = int(round(seconds))
    return f"{total // 3600}:{total % 3600 // 60:02d}:{total % 60:02d}"


def fmt_minutes(seconds: int | None) -> str:
    return "–" if not seconds else f"{round(seconds / 60)} min"


def fmt_pct(value: float | None, *, fraction: bool = False) -> str:
    if value is None:
        return "–"
    return f"{value * 100 if fraction else value:.1f}%"


def fmt_local(moment: datetime | None, tz: str, fmt: str = "%Y-%m-%d %H:%M") -> str:
    return "–" if moment is None else moment.astimezone(ZoneInfo(tz)).strftime(fmt)


def too_many_attempts(wait_seconds: int) -> str:
    """The same message whether or not the username exists."""
    minutes = max(1, -(-wait_seconds // 60))
    return f"Too many sign-in attempts. Please wait {minutes} minute{'s' if minutes != 1 else ''} and try again."


def fmt_ago(moment: datetime | None, now: datetime) -> str:
    if moment is None:
        return "never"
    seconds = max(0, int((now - moment).total_seconds()))
    if seconds < 90:
        return "just now"
    if seconds < 5400:
        return f"{seconds // 60} min ago"
    if seconds < 172800:
        return f"{seconds // 3600} h ago"
    return f"{seconds // 86400} days ago"


def _templates():  # noqa: ANN202
    import jinja2  # noqa: PLC0415

    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(HERE / "templates")),
        autoescape=True,  # every template, whatever its extension: database text is untrusted
        undefined=jinja2.StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters.update(
        hours=fmt_hours,
        duration=fmt_duration,
        minutes=fmt_minutes,
        pct=fmt_pct,
        local=fmt_local,
        ago=fmt_ago,
    )
    env.globals.update(
        PERIODS=PERIODS,
        STATUS_LABELS=STATUS_LABELS,
        STATUS_NOTES=STATUS_NOTES,
        FLAG_NOTES=FLAG_NOTES,
        QUALITY_LABELS=QUALITY_LABELS,
        CURRENT_LABELS=CURRENT_LABELS,
        CurrentStatus=CurrentStatus,
        WEEKDAYS=("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"),
    )
    return env


# ─── mounting ─────────────────────────────────────────────────────────────
