"""
Timesheet pattern — hours worked per project per weekday, with totals
and optional pay.

Deterministic template for ONE person's week:

  Timesheet      Project | Mon..Sun (dates in the headers) | Total Hours
                 — one row per project/task (8 blank scaffold rows
                 when the request gives none), row totals guarded on
                 COUNT (SUM across the day columns; blank until the
                 row has any hours), a total row with per-day totals
                 and the week's grand total.
                 When an hourly rate is given: an editable ASSUMPTIONS
                 block (rate / overtime threshold / overtime multiplier
                 in named cells B5..B7) and a PAY SUMMARY block whose
                 formulas reference them absolutely ($B$5/$B$6/$B$7):
                 regular hours = MIN(total, threshold), overtime hours
                 = MAX(0, total − regular), regular pay = rate × regular,
                 overtime pay = rate × multiplier × overtime.

CALCULATION SEMANTICS (all LIVE formulas — nothing is frozen):

  row total(r) = IF(COUNT(Mon..Sun)=0, "", SUM(Mon..Sun))
  day total(c) = SUM over the column's exact data rows
  grand total  = SUM over the Total Hours column
  regular      = MIN(grand total, $B$6)
  overtime     = MAX(0, grand total − regular)
  pay          = regular × $B$5 + overtime × $B$5 × $B$7

TEMPLATE MODE: a request with no entries ("create a timesheet")
builds the BLANK weekly timesheet — 8 blank project rows with guarded
totals already in place, the dated Mon..Sun header row, day totals and
the grand total live. Never invents projects or hours (hard rule);
week_start null → the current week's Monday (stanza default); the pay /
assumptions block appears only when an hourly rate exists.

Every formula reference is computed from the actual layout rows this
module emits, so off-by-N row math is impossible by construction. No
ROUND() anywhere — display rounding is the number format's job.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

from app.services.patterns.utils import (
    QTY_FMT,
    _currency_fmt,
    _pick,
    to_iso_date,
    to_number,
)

# Registry key — must match the pattern stanza in
# prompts/pattern_classifier.md.
PATTERN_NAME = "timesheet"

PATTERN_DESCRIPTION = (
    "Timesheet: hours actually worked per project/task per weekday with "
    "row and day totals, a grand total and an optional pay summary "
    "(hourly rate, overtime threshold and multiplier). Use when the user "
    "reports hours worked per day/project — a request with no entries "
    "yet still gets a blank weekly timesheet template. Do not use it for "
    "planned shift rosters or payroll registers with taxes."
)

# Routing keywords/stems — drive the cheap pre-gate and the classifier
# shortlist (see excel_gen._shortlist_patterns).
PATTERN_KEYWORDS = (
    "timesheet",
    "time sheet",
    "timesheets",
    "timecard",
    "hours worked",
    "work hours",
    "worked hours",
    "logged hours",
    "log my hours",
    "billable hours",
    "time log",
)

MAX_PROJECTS = 30
MIN_PROJECT_ROWS = 8  # blank scaffold rows in template mode

# day-key aliases → index 0..6 (Monday..Sunday)
_DAY_KEYS = (
    ("mon", "monday"),
    ("tue", "tuesday", "tues"),
    ("wed", "wednesday"),
    ("thu", "thursday", "thur", "thurs"),
    ("fri", "friday"),
    ("sat", "saturday"),
    ("sun", "sunday"),
)

_DOW = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS_SHORT = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)

# Editable assumption defaults (documented in the sheet notes):
DEFAULT_OVERTIME_THRESHOLD = 40.0  # hours/week before overtime kicks in
DEFAULT_OVERTIME_MULTIPLIER = 1.5  # time-and-a-half

_WS_RE = re.compile(r"\s+")


def _monday_of(d: date) -> date:
    """The Monday of the week containing d (d itself when Monday)."""
    return d - timedelta(days=d.weekday())


# Design tokens (same palette as the converter).
NAVY = "16304F"
GOLD = "C9A227"
MUTED = "5C6470"

# Excel's classic Bad / Neutral conditional-format palettes.
CF_RED_FILL = "FFC7CE"
CF_RED_TEXT = "9C0006"
CF_AMBER_FILL = "FFF2CC"
CF_AMBER_TEXT = "7F6000"


def _clean_text(value: Any, cap: int) -> Optional[str]:
    """Strip + collapse whitespace + cap length; None when empty."""
    if not isinstance(value, str):
        return None
    s = _WS_RE.sub(" ", value.strip())
    return s[:cap] if s else None


def _day_hours(entry: dict) -> List[Optional[float]]:
    """One entries row → 7 day values (None = day not worked)."""
    out: List[Optional[float]] = []
    for aliases in _DAY_KEYS:
        value = None
        for key in aliases:
            if key in entry and entry[key] is not None:
                value = entry[key]
                break
        hours = to_number(value)
        if hours is None:
            hours = None  # day not worked → blank cell (SUM-friendly)
        else:
            hours = max(0.0, min(hours, 24.0))
        out.append(hours)
    return out


def _normalize_entry(entry: Any) -> Optional[dict]:
    """One entries item → {project, hours: [7 floats-or-None]}."""
    if isinstance(entry, str):
        project = _clean_text(entry, 60)
        raw: Dict[str, Any] = {}
    elif isinstance(entry, dict):
        project = _clean_text(
            _pick(entry, "project", "task", "name", "description", "activity"),
            60,
        )
        raw = entry
    else:
        return None
    if not project:
        return None
    return {"project": project, "hours": _day_hours(raw)}


def coerce_timesheet_params(params: dict) -> dict:
    """Validate + normalize classifier params; raises ValueError.

    Template mode: empty/missing entries are FINE — the builder emits
    the blank weekly timesheet (the "create a timesheet" case).
    ValueError only for structurally wrong params (non-object params,
    non-array entries).
    """
    if not isinstance(params, dict):
        raise ValueError("params must be an object")

    entries: List[dict] = []
    raw_entries = _pick(params, "entries", "projects", "tasks", "timesheet")
    if raw_entries is not None:
        if not isinstance(raw_entries, list):
            raise ValueError("entries must be an array")
        for entry in raw_entries[:MAX_PROJECTS]:
            normalized = _normalize_entry(entry)
            if normalized is not None:
                entries.append(normalized)
    # Template mode: n_entries == 0 is fine — blank rows, the user
    # types projects and hours (never invent projects; hard rule).

    week_start = to_iso_date(
        _pick(params, "week_start", "start_date", "week_of", "week_commencing")
    )
    if week_start is None:
        week = _monday_of(date.today())
    else:
        y, m, d = (int(x) for x in week_start.split("-"))
        week = _monday_of(date(y, m, d))  # snap to the week's Monday
    week_start = week.isoformat()

    hourly_rate = to_number(_pick(params, "hourly_rate", "rate", "pay_rate", "wage"))
    if hourly_rate is not None:
        if hourly_rate <= 0:
            hourly_rate = None  # a zero/negative rate means "no pay"
        else:
            hourly_rate = min(hourly_rate, 1e6)

    overtime_multiplier = to_number(
        _pick(params, "overtime_multiplier", "overtime", "multiplier")
    )
    if overtime_multiplier is None:
        overtime_multiplier = DEFAULT_OVERTIME_MULTIPLIER
    else:
        overtime_multiplier = max(1.0, min(overtime_multiplier, 10.0))

    employee = _clean_text(
        _pick(params, "employee", "name", "staff_name", "person"), 60
    )

    notes = _pick(params, "notes", "note")
    notes = _clean_text(notes, 1000)

    return {
        "employee": employee,
        "week_start": week_start,
        "hourly_rate": hourly_rate,
        "overtime_multiplier": overtime_multiplier,
        "currency": _currency_fmt(_pick(params, "currency")),
        "entries": entries,
        "notes": notes,
    }


def build_timesheet_spec(params: dict) -> dict:
    """Timesheet workbook — every formula code-generated.

    Layout (rows computed here, never guessed by a model):

    Timesheet sheet:
      row 1      title · row 2 usage hint
      row 4      ASSUMPTIONS heading (pay only)
      rows 5-7   rate / overtime threshold / overtime multiplier (B5..B7)
      row 9      table headers (pay) or row 4 (no pay)
      rows ..    one row per project (8 blank scaffold rows when the
                 request gives none): Mon..Sun hours + Total Hours
                 (guarded on COUNT — blank until any hours exist)
      row T      total row: per-day totals + grand total
      rows T+2.. PAY SUMMARY block (pay only) — absolute $B$5/$B$6/$B$7
    """
    p = coerce_timesheet_params(params)
    employee = p["employee"]
    week_start = p["week_start"]
    rate = p["hourly_rate"]
    multiplier = p["overtime_multiplier"]
    cur = p["currency"]
    entries: List[dict] = p["entries"]
    notes = p["notes"]

    money = cur or "#,##0.00"
    n = len(entries)
    n_rows = n or MIN_PROJECT_ROWS  # template scaffold rows
    has_pay = rate is not None

    week = date(*(int(x) for x in week_start.split("-")))
    days = [week + timedelta(days=i) for i in range(7)]

    # ── geometry (integers first — every formula is formatted from these)
    header_row = 9 if has_pay else 4
    first = header_row + 1  # first data row
    last = first + n_rows - 1  # last data row
    total_row = last + 1  # totals row
    pay_top = total_row + 2  # PAY SUMMARY heading row

    day_headers = [
        "{dow} {day:02d} {mon}".format(
            dow=_DOW[i], day=days[i].day, mon=_MONTHS_SHORT[days[i].month - 1]
        )
        for i in range(7)
    ]

    title = "Timesheet — Week of {week}".format(week=week.isoformat())
    if employee:
        title = "Timesheet — {who} — Week of {week}".format(
            who=employee, week=week.isoformat()
        )

    blocks: List[dict] = [
        {
            "cell": "A1",
            "text": title,
            "bold": True,
            "font_size": 14,
            "font_color": NAVY,
        },
        {
            "cell": "A2",
            "text": (
                "Enter hours per day — Total Hours, day totals and pay "
                "compute automatically. Days you didn't work stay blank."
            ),
            "italic": True,
            "font_color": MUTED,
        },
    ]

    if has_pay:
        blocks.extend(
            [
                {
                    "cell": "A4",
                    "text": "ASSUMPTIONS",
                    "bold": True,
                    "font_size": 12,
                    "font_color": GOLD,
                },
                {"cell": "A5", "text": "Hourly Rate"},
                {
                    "cell": "B5",
                    "text": rate,
                    "bold": True,
                    "number_format": money,
                },
                {"cell": "A6", "text": "Overtime Threshold (Hours/Week)"},
                {
                    "cell": "B6",
                    "text": DEFAULT_OVERTIME_THRESHOLD,
                    "number_format": QTY_FMT,
                },
                {"cell": "A7", "text": "Overtime Multiplier"},
                {
                    "cell": "B7",
                    "text": multiplier,
                    "number_format": "0.0",
                },
            ]
        )

    rows: List[List[Any]] = []
    for i in range(n_rows):
        r = first + i
        entry = entries[i] if i < n else None
        hours = entry["hours"] if entry else [None] * 7
        rows.append(
            [
                entry["project"] if entry else None,
                hours[0],
                hours[1],
                hours[2],
                hours[3],
                hours[4],
                hours[5],
                hours[6],
                # Total Hours — guarded SUM across the day columns:
                # blank until the row has any hours at all
                '=IF(COUNT(B{r}:H{r})=0,"",SUM(B{r}:H{r}))'.format(r=r),
            ]
        )

    total_day_sums = [
        "=SUM({col}{r0}:{col}{rN})".format(col=col, r0=first, rN=last)
        for col in "BCDEFGH"
    ]

    table: Dict[str, Any] = {
        "start_cell": "A{r}".format(r=header_row),
        "headers": ["Project"] + day_headers + ["Total Hours"],
        "rows": rows,
        "number_formats": {col: QTY_FMT for col in "BCDEFGHI"},
        "alignments": {col: "center" for col in "BCDEFGHI"},
        "total_row": (
            ["Total"]
            + total_day_sums
            + [
                # grand total = SUM over the Total Hours column
                "=SUM(I{r0}:I{rN})".format(r0=first, rN=last)
            ]
        ),
    }

    # PAY SUMMARY block — every assumption reference is absolute
    if has_pay:
        blocks.extend(
            [
                {
                    "cell": "A{r}".format(r=pay_top),
                    "text": "PAY SUMMARY",
                    "bold": True,
                    "font_size": 12,
                    "font_color": GOLD,
                },
                {"cell": "A{r}".format(r=pay_top + 1), "text": "Total Hours"},
                {
                    "cell": "B{r}".format(r=pay_top + 1),
                    "text": "=I{t}".format(t=total_row),
                    "number_format": QTY_FMT,
                },
                {"cell": "A{r}".format(r=pay_top + 2), "text": "Regular Hours"},
                {
                    "cell": "B{r}".format(r=pay_top + 2),
                    "text": "=MIN(B{r},$B$6)".format(r=pay_top + 1),
                    "number_format": QTY_FMT,
                },
                {"cell": "A{r}".format(r=pay_top + 3), "text": "Overtime Hours"},
                {
                    "cell": "B{r}".format(r=pay_top + 3),
                    "text": "=MAX(0,B{tot}-B{reg})".format(
                        tot=pay_top + 1, reg=pay_top + 2
                    ),
                    "number_format": QTY_FMT,
                },
                {"cell": "A{r}".format(r=pay_top + 4), "text": "Regular Pay"},
                {
                    "cell": "B{r}".format(r=pay_top + 4),
                    "text": "=B{reg}*$B$5".format(reg=pay_top + 2),
                    "number_format": money,
                },
                {"cell": "A{r}".format(r=pay_top + 5), "text": "Overtime Pay"},
                {
                    "cell": "B{r}".format(r=pay_top + 5),
                    "text": "=B{ot}*$B$5*$B$7".format(ot=pay_top + 3),
                    "number_format": money,
                },
                {"cell": "A{r}".format(r=pay_top + 6), "text": "Total Pay"},
                {
                    "cell": "B{r}".format(r=pay_top + 6),
                    "text": "=B{reg}+B{ot}".format(reg=pay_top + 4, ot=pay_top + 5),
                    "number_format": money,
                    "bold": True,
                },
            ]
        )

    sheet: Dict[str, Any] = {
        "name": "Timesheet",
        "tab_color": NAVY,
        "freeze_panes": "B{r}".format(r=first),
        "column_widths": {
            "A": 26,
            **{col: 10 for col in "BCDEFGH"},
            "I": 12,
        },
        "text_blocks": blocks,
        "tables": [table],
        "charts": (
            [
                {
                    "type": "bar",
                    "title": "Hours by Project — Week of {week}".format(
                        week=week.isoformat()
                    ),
                    "anchor": "K3",
                    "width": 15,
                    "height": 9,
                    "categories_range": "Timesheet!$A${r0}:$A${rN}".format(
                        r0=first, rN=last
                    ),
                    "series": [
                        {
                            "name": "Total Hours",
                            "values_range": "Timesheet!$I${r0}:$I${rN}".format(
                                r0=first, rN=last
                            ),
                        }
                    ],
                    "value_numfmt": QTY_FMT,
                }
            ]
            if n
            else []  # no projects yet → nothing to chart (never invent)
        ),
        "conditional_formats": [
            # Long working days: >12h red, >8h amber (red rule first —
            # earlier rules win, so a 14h day shows red only)
            {
                "range": "B{r0}:H{rN}".format(r0=first, rN=last),
                "rules": [
                    {
                        "type": "cell_is",
                        "operator": "greater_than",
                        "value": 12,
                        "fill": CF_RED_FILL,
                        "font_color": CF_RED_TEXT,
                        "bold": True,
                        "stop_if_true": True,
                    },
                    {
                        "type": "cell_is",
                        "operator": "greater_than",
                        "value": 8,
                        "fill": CF_AMBER_FILL,
                        "font_color": CF_AMBER_TEXT,
                        "stop_if_true": False,
                    },
                ],
            },
        ],
        "protect": {"unlocked_ranges": ["A{r0}:H{rN}".format(r0=first, rN=last)]},
        "notes": notes
        or (
            "Type the hours you worked per day (blank = day not worked) — "
            "blank rows are ready for more projects; row totals stay "
            "blank until a row has hours. Row totals, the day totals in "
            "the Total row, the grand total and the chart all update. "
            "With an hourly rate, the ASSUMPTIONS cells are editable: "
            "change the rate, the overtime threshold (default 40 "
            "hours/week) or the multiplier (default 1.5 = "
            "time-and-a-half) and the PAY SUMMARY recomputes — Regular "
            "Hours = MIN(total, threshold), Overtime Hours = MAX(0, "
            "total − regular), Overtime Pay = rate × multiplier × "
            "overtime hours. Days over 8 hours highlight amber, over 12 "
            "red."
        ),
    }

    return {"filename": "timesheet.xlsx", "sheets": [sheet]}


# ── Standard pattern entry points (used by the dynamic registry) ──────

coerce_params = coerce_timesheet_params
build_spec = build_timesheet_spec
