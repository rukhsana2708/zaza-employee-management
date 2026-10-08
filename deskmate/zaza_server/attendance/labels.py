"""Plain-language labels for attendance statuses, data quality and quality
flags — shared by the Google Sheets export (Phase 6) and the manager
dashboard (Phase 7) so both say the same thing. Wording only: no logic."""

from __future__ import annotations

from .models import AttendanceStatus

STATUS_LABELS = {
    AttendanceStatus.PRESENT: "Present",
    AttendanceStatus.LATE: "Late",
    AttendanceStatus.EARLY_LEAVE: "Early leave",
    AttendanceStatus.LATE_AND_EARLY: "Late and early leave",
    AttendanceStatus.ABSENT: "Absent",
    AttendanceStatus.DAY_OFF: "Day off",
    AttendanceStatus.WORKED_DAY_OFF: "Worked on day off",
    AttendanceStatus.DATA_INCOMPLETE: "Data incomplete",
    AttendanceStatus.NO_SCHEDULE: "No schedule",
    AttendanceStatus.PENDING: "Pending (shift not over)",
}

STATUS_NOTES = {
    AttendanceStatus.DATA_INCOMPLETE: "Not enough reliable data to judge attendance — not counted as absent",
    AttendanceStatus.PENDING: "Shift not finished yet",
    AttendanceStatus.NO_SCHEDULE: "No schedule applies to this date",
}

FLAG_NOTES = {
    "PROVISIONAL": "Provisional (may still change)",
    "UNKNOWN_TIME": "Some monitoring time unknown",
    "START_UNCERTAIN": "Start uncertain (not charged as late)",
    "END_UNCERTAIN": "End uncertain (uncertain part not charged as early leave)",
    "AWAITING_DEVICE_SYNC": "Waiting for a device to sync",
    "NO_DEVICE": "No enabled device",
    "INTERRUPTED_SESSION": "Agent stopped unexpectedly",
    "OVERLAPPING_PERIODS": "Overlapping device data merged (not double counted)",
    "SCHEDULE_OVERLAP": "Shift overlaps another day's shift",
    "DST_ADJUSTED": "Daylight-saving change affected the shift",
}

QUALITY_LABELS = {"COMPLETE": "Complete", "PARTIAL": "Partial", "INSUFFICIENT": "Insufficient"}
