"""Phase 5 — deterministic attendance and work-time calculations.

- ``models.py``          inputs, outputs, statuses, policy
- ``timeline.py``        non-overlapping activity timeline (no double counting)
- ``schedule.py``        schedule resolution, DST-correct UTC shift bounds, date attribution
- ``calculator.py``      the daily calculation (pure function)
- ``rollup.py``          weekly (ISO Mon–Sun) and monthly roll-ups of daily summaries
- ``store.py``           storage interface + in-memory store (tests)
- ``postgres_store.py``  PostgreSQL store (daily/weekly/monthly_summaries)
- ``summary_service.py`` calculate_daily / calculate_week / calculate_month / recalculate

PostgreSQL summaries are the authoritative figures; Sheets, the dashboard
and charts (Phases 6–8) read them and never recalculate. Formulas:
ARCHITECTURE.md §4.11.
"""

from .models import AttendancePolicy, AttendanceStatus, DataQuality
from .summary_service import SummaryService

__all__ = ["AttendancePolicy", "AttendanceStatus", "DataQuality", "SummaryService"]
