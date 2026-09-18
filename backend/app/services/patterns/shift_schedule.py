"""
Shift schedule pattern — weekly staff rota with shift codes.

Deterministic template for ONE team's week:

  Rota            Staff Member | Role | Mon..Sun (dates in the headers) |
                  Weekly Hours — the visible grid holds clean shift CODES
                  (dropdown sourced from the Codes tab; 8 blank staff
                  rows when the request names nobody); Weekly Hours is
                  guarded on the staff name and pulls the per-staff sum
                  from the hidden Calc sheet.
  Codes           Code | Label | Hours — the editable shift-code
                  reference block (defaults M/E/N/O when the request
                  gives no codes); the Rota dropdowns and the VLOOKUPs
                  read it live, so editing hours recalculates the rota.
  Calc (hidden)   one row per staff member (scaffold rows included),
                  aligned 1:1 with the Rota: column B mirrors the
                  staff name (guarded), per-day cells translate the
                  code into hours via =IF($B5="","",IF(Rota!C5="",0,
                  IFERROR(VLOOKUP(Rota!C5,Codes!$A$5:$C$8,3,FALSE),0)))
                  and column J sums the week. SUMIF can't map code→hours
                  across a row, so the translation lives here and the
                  visible sheet stays clean codes.

CALCULATION SEMANTICS (all LIVE formulas — nothing is frozen):

  mirror(r)     = IF(Rota staff blank, "", Rota staff)
  hours(r, day) = IF(mirror blank, "", IF(code blank, 0,
                   IFERROR(VLOOKUP(code, Codes, 3), 0)))
  weekly(r)     = IF(mirror blank, "", SUM(hours(r, Mon..Sun)))  [Calc J]
  Rota!J(r)     = IF(staff blank, "", Calc!J(r))
  day total     = SUM over the Calc day column
  weekly total  = SUM over the Rota's Weekly Hours column

Default codes when shift_codes is null: M Morning 8h · E Evening 8h ·
N Night 10h · O Off 0h.

TEMPLATE MODE: a request with no staff yet ("make a shift schedule")
builds the BLANK rota — 8 blank staff rows × 7 day columns with the
code dropdowns over every cell, the Codes tab still emitted with the
DEFAULT M/E/N/O reference codes (vocabulary, not user data), and the
hidden Calc lattice extended over the scaffold rows with blank guards
so the workbook is ready the moment names are typed. Never invents
staff (hard rule); week_start null → next Monday (rotas plan the
coming week); the chart appears only when there is staff to chart.

FILL MODE: when the request names staff + constraints (opening
  hours, availability, max hours) but no concrete codes, the router
  sets "fill": true and excel_gen drafts a fair rota via
  prompts/pattern_populator.md BEFORE calling the builder — staff
  names still come only from the request. Drafted params flow
  through coerce_shift_schedule_params like any other; a failed
  draft falls back to the extracted params (blank rota).


Every formula reference is computed from the actual layout rows this
module emits, so off-by-N row math is impossible by construction. No
ROUND() anywhere — display rounding is the number format's job.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple

from app.services.patterns.utils import (
    QTY_FMT,
    _pick,
    to_iso_date,
    to_number,
)

# Registry key — must match the pattern stanza in
# prompts/pattern_classifier.md.
PATTERN_NAME = "shift_schedule"

PATTERN_DESCRIPTION = (
    "Weekly staff shift roster / rota: a staff × days grid of shift "
    "codes with per-staff weekly hours, an editable shift-code reference "
    "tab and dropdowns. Use for weekly rotas and duty rosters — a "
    "request with no staff yet still gets a blank rota template with "
    "the default shift codes. Do not use it for hours actually worked "
    "(timesheet) or leave balances (leave_tracker)."
)

# Routing keywords/stems — drive the cheap pre-gate and the classifier
# shortlist (see excel_gen._shortlist_patterns).
PATTERN_KEYWORDS = (
    "shift",
    "rota",
    "roster",
    "shift schedule",
    "shift plan",
    "duty roster",
    "staff rota",
    "weekly rota",
    "shift pattern",
)

# Fillable pattern: guidance-only requests (goals, split, frequency…)
# may draft starter sessions via prompts/pattern_populator.md before
# building — see PATTERN_FILLABLE in patterns/__init__.py.
PATTERN_FILL = True

MAX_STAFF = 30
MAX_CODES = 12
MIN_STAFF_ROWS = 8  # blank scaffold rows in template mode

# Default shift codes when the request doesn't give its own
# (documented in the pattern stanza: M/E/N/O).
DEFAULT_SHIFT_CODES: Tuple[Tuple[str, str, float], ...] = (
    ("M", "Morning", 8.0),
    ("E", "Evening", 8.0),
    ("N", "Night", 10.0),
    ("O", "Off", 0.0),
)

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

# Rota/Calc day columns (Mon..Sun) — fixed 7-day layout.
DAY_LETTERS = ("C", "D", "E", "F", "G", "H", "I")

_WS_RE = re.compile(r"\s+")


def _monday_of(d: date) -> date:
    """The Monday of the week containing d (d itself when Monday)."""
    return d - timedelta(days=d.weekday())


def _next_monday(today: date) -> date:
    """The Monday of NEXT week (never today)."""
    return today + timedelta(days=(7 - today.weekday()) or 7)


# Design tokens (same palette as the converter).
NAVY = "16304F"
STEEL = "1B3A5C"
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


def _normalize_code(value: Any, code: str) -> Optional[Tuple[str, str, float]]:
    """One shift_codes value → (code, label, hours); None when unusable."""
    label: Optional[str] = None
    hours: Optional[float] = None
    if isinstance(value, dict):
        label = _clean_text(_pick(value, "label", "name", "description"), 30)
        hours = to_number(_pick(value, "hours", "duration", "length"))
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        hours = float(value)
    elif isinstance(value, str):
        label = _clean_text(value, 30)
    if hours is None:
        hours = 0.0
    hours = max(0.0, min(hours, 24.0))
    return (code, label or code, hours)


def _normalize_codes(raw: Any) -> List[Tuple[str, str, float]]:
    """The shift_codes param → ordered [(code, label, hours)]."""
    codes: List[Tuple[str, str, float]] = []
    if isinstance(raw, dict) and raw:
        for key, value in list(raw.items())[:MAX_CODES]:
            code = _clean_text(key, 4)
            if not code:
                continue
            normalized = _normalize_code(value, code.upper())
            if normalized is None:
                continue
            codes.append(normalized)
    if not codes:
        codes = list(DEFAULT_SHIFT_CODES)
    # dedupe by code, keep first occurrence
    seen: set = set()
    unique: List[Tuple[str, str, float]] = []
    for code, label, hours in codes:
        if code in seen:
            continue
        seen.add(code)
        unique.append((code, label, hours))
    return unique[:MAX_CODES]


def _normalize_staff_entry(entry: Any) -> Optional[dict]:
    """One staff entry → {name, role, days: [code-or-None × 7]}."""
    if isinstance(entry, str):
        name = _clean_text(entry, 60)
        raw: Dict[str, Any] = {}
    elif isinstance(entry, dict):
        name = _clean_text(_pick(entry, "name", "staff", "employee", "person"), 60)
        raw = entry
    else:
        return None
    if not name:
        return None

    days: List[Optional[str]] = []
    for aliases in _DAY_KEYS:
        value = None
        for key in aliases:
            if key in raw and raw[key] is not None:
                value = raw[key]
                break
        if isinstance(value, str) and value.strip():
            days.append(value.strip().upper())
        else:
            days.append(None)  # blank day — cell stays empty
    return {
        "name": name,
        "role": _clean_text(_pick(raw, "role", "position", "job"), 30),
        "days": days,
    }


def coerce_shift_schedule_params(params: dict) -> dict:
    """Validate + normalize classifier params; raises ValueError.

    Template mode: empty/missing staff is FINE — the builder emits the
    blank rota with the default shift codes (the "make a shift
    schedule" case). ValueError only for structurally wrong params
    (non-object params, non-array staff).
    """
    if not isinstance(params, dict):
        raise ValueError("params must be an object")

    staff: List[dict] = []
    raw_staff = _pick(params, "staff", "employees", "team", "people")
    if raw_staff is not None:
        if not isinstance(raw_staff, list):
            raise ValueError("staff must be an array")
        for entry in raw_staff[:MAX_STAFF]:
            normalized = _normalize_staff_entry(entry)
            if normalized is not None:
                staff.append(normalized)
    # Template mode: no staff is fine — blank rota rows + default
    # codes (never invent staff; hard rule).

    week_start = to_iso_date(
        _pick(params, "week_start", "start_date", "week_of", "week_commencing")
    )
    if week_start is None:
        week = _next_monday(date.today())  # rotas plan the coming week
    else:
        y, m, d = (int(x) for x in week_start.split("-"))
        week = _monday_of(date(y, m, d))  # snap to the week's Monday
    week_start = week.isoformat()

    codes = _normalize_codes(_pick(params, "shift_codes", "codes"))

    team_name = _clean_text(_pick(params, "team_name", "team", "name"), 60)

    notes = _pick(params, "notes", "note")
    notes = _clean_text(notes, 1000)

    return {
        "team_name": team_name,
        "week_start": week_start,
        "shift_codes": codes,
        "staff": staff,
        "notes": notes,
    }


def build_shift_schedule_spec(params: dict) -> dict:
    """Shift rota workbook — every formula code-generated.

    Layout (rows computed here, never guessed by a model):

    Rota sheet:
      row 1      title · row 2 usage hint
      row 4      headers (start_cell A4, no table title)
      rows 5..   one row per staff member (8 blank scaffold rows when
                 the request names nobody): codes Mon..Sun + Weekly
                 Hours (guarded on the staff name)
      row T      totals: per-day hours (summed from Calc) + week total
    Codes sheet:
      row 4      headers · rows 5.. one row per code (the VLOOKUP source)
    Calc sheet (hidden):
      row 4      headers (start_cell B4 — day columns align with Rota)
      rows 5..   per-day code→hours translations (guarded on the staff
                 mirror in column B) + the guarded weekly SUM
    """
    p = coerce_shift_schedule_params(params)
    team_name = p["team_name"]
    week_start = p["week_start"]
    codes: List[Tuple[str, str, float]] = p["shift_codes"]
    staff: List[dict] = p["staff"]
    notes = p["notes"]

    n = len(staff)
    n_rows = n or MIN_STAFF_ROWS  # template scaffold rows
    k = len(codes)
    week = date(*(int(x) for x in week_start.split("-")))
    days = [week + timedelta(days=i) for i in range(7)]

    # ── geometry (integers first — every formula is formatted from these)
    rota_r0 = 5  # first Rota/Calc data row (aligned 1:1)
    rota_rN = 4 + n_rows  # last Rota/Calc data row
    rota_total = rota_rN + 1  # noqa: Rota totals row
    codes_r0 = 5  # first Codes data row
    codes_rN = 4 + k  # last Codes data row
    vlookup_range = "Codes!$A${r0}:$C${rN}".format(r0=codes_r0, rN=codes_rN)

    day_headers = [
        "{dow} {day:02d} {mon}".format(
            dow=_DOW[i], day=days[i].day, mon=_MONTHS_SHORT[days[i].month - 1]
        )
        for i in range(7)
    ]

    # ═════════════════════════════ Rota sheet ════════════════════════
    rota_rows: List[List[Any]] = []
    for i in range(n_rows):
        r = rota_r0 + i
        person = staff[i] if i < n else None
        rota_rows.append(
            [
                person["name"] if person else None,
                person["role"] if person else None,
                *(person["days"] if person else [None] * 7),
                # Weekly Hours — guarded on the staff name; the Calc
                # sheet holds the code→hours sum
                '=IF($A{r}="","",Calc!J{r})'.format(r=r),
            ]
        )

    title = "Shift Rota — Week of {week}".format(week=week.isoformat())
    if team_name:
        title = "{team} Shift Rota — Week of {week}".format(
            team=team_name, week=week.isoformat()
        )

    rota_sheet: Dict[str, Any] = {
        "name": "Rota",
        "tab_color": NAVY,
        "freeze_panes": "C5",
        "column_widths": {
            "A": 24,
            "B": 16,
            **{letter: 9 for letter in DAY_LETTERS},
            "J": 12,
        },
        "text_blocks": [
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
                    "Pick shift codes on the grid (dropdown) — Weekly "
                    "Hours and the day totals update from the Codes tab."
                ),
                "italic": True,
                "font_color": MUTED,
            },
        ],
        "tables": [
            {
                "start_cell": "A4",
                "headers": (["Staff Member", "Role"] + day_headers + ["Weekly Hours"]),
                "rows": rota_rows,
                "number_formats": {"J": QTY_FMT},
                "alignments": {
                    **{letter: "center" for letter in DAY_LETTERS},
                    "J": "center",
                },
                "total_row": (
                    ["Total Hours", None]
                    + [
                        # per-day total hours = SUM over the Calc day column
                        "=SUM(Calc!{L}{r0}:{L}{rN})".format(
                            L=letter, r0=rota_r0, rN=rota_rN
                        )
                        for letter in DAY_LETTERS
                    ]
                    + [
                        # week total = SUM over the Weekly Hours column
                        "=SUM(J{r0}:J{rN})".format(r0=rota_r0, rN=rota_rN)
                    ]
                ),
            }
        ],
        "data_validation": [
            {
                # live range source: codes added on the Codes tab appear
                # in every day dropdown automatically
                "range": "C{r0}:I{rN}".format(r0=rota_r0, rN=rota_rN),
                "source_range": "Codes!$A${r0}:$A${rN}".format(
                    r0=codes_r0, rN=codes_rN
                ),
                "allow_blank": True,
                "prompt_title": "Shift code",
                "prompt": "Pick a code from the Codes tab (blank = not scheduled).",
                "error_title": "Not in the code list",
                "error": (
                    "Add the code on the Codes tab first — the dropdown "
                    "picks it up automatically."
                ),
                "error_style": "warning",
            }
        ],
        "conditional_formats": [
            # rest days (code O) tinted amber; >48h/week red, >40h amber
            {
                "range": "C{r0}:I{rN}".format(r0=rota_r0, rN=rota_rN),
                "rules": [
                    {
                        "type": "cell_is",
                        "operator": "equal",
                        "value": "O",
                        "fill": CF_AMBER_FILL,
                        "font_color": CF_AMBER_TEXT,
                        "stop_if_true": False,
                    }
                ],
            },
            {
                "range": "J{r0}:J{rN}".format(r0=rota_r0, rN=rota_rN),
                "rules": [
                    {
                        "type": "cell_is",
                        "operator": "greater_than",
                        "value": 48,
                        "fill": CF_RED_FILL,
                        "font_color": CF_RED_TEXT,
                        "bold": True,
                        "stop_if_true": True,
                    },
                    {
                        "type": "cell_is",
                        "operator": "greater_than",
                        "value": 40,
                        "fill": CF_AMBER_FILL,
                        "font_color": CF_AMBER_TEXT,
                        "stop_if_true": False,
                    },
                ],
            },
        ],
        "charts": (
            [
                {
                    "type": "bar",
                    "title": "Weekly Hours by Staff — Week of {week}".format(
                        week=week.isoformat()
                    ),
                    "anchor": "L3",
                    "width": 15,
                    "height": 9,
                    "categories_range": "Rota!$A${r0}:$A${rN}".format(
                        r0=rota_r0, rN=rota_rN
                    ),
                    "series": [
                        {
                            "name": "Weekly Hours",
                            "values_range": "Rota!$J${r0}:$J${rN}".format(
                                r0=rota_r0, rN=rota_rN
                            ),
                        }
                    ],
                    "value_numfmt": QTY_FMT,
                }
            ]
            if n
            else []  # no staff yet → nothing to chart (never invent)
        ),
        "protect": {
            "unlocked_ranges": [
                "A{r0}:B{rN}".format(r0=rota_r0, rN=rota_rN),
                "C{r0}:I{rN}".format(r0=rota_r0, rN=rota_rN),
            ]
        },
        "notes": notes
        or (
            "Pick a shift code for each staff member and day — the "
            "dropdown list comes live from the Codes tab. Weekly Hours "
            "translates each code into hours on the hidden Calc sheet "
            "(VLOOKUP against Codes) and sums the week; the total row "
            "shows scheduled hours per day and the whole week. Add or "
            "edit codes on the Codes tab (Code / Label / Hours) — "
            "dropdowns and every hour count update automatically. A "
            "blank day counts as 0 hours. Weeks over 40 hours highlight "
            "amber, over 48 red; Off days tint amber."
        ),
    }

    # ═════════════════════════════ Codes sheet ═══════════════════════
    codes_rows: List[List[Any]] = [[code, label, hours] for code, label, hours in codes]

    codes_sheet: Dict[str, Any] = {
        "name": "Codes",
        "tab_color": STEEL,
        "freeze_panes": "A5",
        "column_widths": {"A": 10, "B": 18, "C": 10},
        "text_blocks": [
            {
                "cell": "A1",
                "text": "Shift Codes",
                "bold": True,
                "font_size": 14,
                "font_color": NAVY,
            },
            {
                "cell": "A2",
                "text": (
                    "The code → label → hours reference. The Rota "
                    "dropdowns and the weekly-hours math read this tab "
                    "live — edit hours here and the rota recalculates."
                ),
                "italic": True,
                "font_color": MUTED,
            },
        ],
        "tables": [
            {
                "start_cell": "A4",
                "headers": ["Code", "Label", "Hours"],
                "rows": codes_rows,
                "number_formats": {"C": QTY_FMT},
                "alignments": {"A": "center", "C": "center"},
            }
        ],
        "protect": {
            "unlocked_ranges": ["A{r0}:C{rN}".format(r0=codes_r0, rN=codes_rN)]
        },
        "notes": (
            "One row per shift code. Code is what appears in the Rota "
            "grid dropdowns; Hours is what the weekly totals count for "
            "that code. Changing Hours here recalculates every staff "
            "member's weekly hours (via the hidden Calc sheet)."
        ),
    }

    # ═════════════════════════════ Calc sheet (hidden) ═══════════════
    calc_rows: List[List[Any]] = []
    for i in range(n_rows):
        r = rota_r0 + i
        day_cells = [
            # code → hours: no staff yet → blank; a blank day counts 0;
            # unknown codes count 0 (the dropdown keeps the grid to
            # valid codes anyway)
            (
                '=IF($B{r}="","",IF(Rota!{L}{r}="",0,IFERROR('
                "VLOOKUP(Rota!{L}{r},{codes},3,FALSE),0)))"
            ).format(r=r, L=letter, codes=vlookup_range)
            for letter in DAY_LETTERS
        ]
        calc_rows.append(
            [
                # staff mirror for readability (rows align 1:1 with
                # Rota); guarded so a blank rota row reads ""
                '=IF(Rota!$A${r}="","",Rota!$A${r})'.format(r=r),
                *day_cells,
                # weekly hours = SUM across the day columns, blank
                # until the row has a staff member
                '=IF($B{r}="","",SUM(C{r}:I{r}))'.format(r=r),
            ]
        )

    calc_sheet: Dict[str, Any] = {
        "name": "Calc",
        "hidden": True,
        "freeze_panes": "C5",
        "column_widths": {"B": 24, **{letter: 9 for letter in DAY_LETTERS}, "J": 12},
        "text_blocks": [
            {
                "cell": "A1",
                "text": "Hidden calculation sheet — per-day code→hours translations.",
                "italic": True,
                "font_color": MUTED,
            },
            {
                "cell": "A2",
                "text": (
                    "Rows run 1:1 with the Rota: each day cell looks its "
                    "code up in Codes (column 3 = hours); column J sums "
                    "the week and feeds the Rota's Weekly Hours column."
                ),
                "italic": True,
                "font_color": MUTED,
            },
        ],
        "tables": [
            {
                "start_cell": "B4",
                "headers": ["Staff"] + day_headers + ["Weekly Hours"],
                "rows": calc_rows,
                "number_formats": {
                    **{letter: QTY_FMT for letter in DAY_LETTERS},
                    "J": QTY_FMT,
                },
                "alignments": {
                    **{letter: "center" for letter in DAY_LETTERS},
                    "J": "center",
                },
                "zebra": False,
            }
        ],
        "protect": True,
        "notes": (
            "Hidden calculation sheet. Day columns C..I run 1:1 with the "
            "Rota's rows and translate each shift code into hours "
            "(VLOOKUP against the Codes tab, third column). Column J sums "
            "each staff member's week; the Rota's Weekly Hours column and "
            "its total row reference these cells. Right-click a tab → "
            "Unhide to inspect."
        ),
    }

    return {
        "filename": "shift_schedule.xlsx",
        "sheets": [rota_sheet, codes_sheet, calc_sheet],
    }


# ── Standard pattern entry points (used by the dynamic registry) ──────

coerce_params = coerce_shift_schedule_params
build_spec = build_shift_schedule_spec
