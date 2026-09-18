"""
Leave tracker pattern — employee annual leave / vacation / PTO balances.

Deterministic template for a small team's leave book:

  Balances        Employee | Department | Entitlement | Carried Over |
                  Booked | Taken | Planned | Remaining | Utilization —
                  entitlements and carried-over days are inputs the user
                  edits; booked/taken/planned are live SUMIF/SUMIFS over
                  the Leave Log; remaining = entitlement + carried −
                  booked; utilization guards division by zero.
  Leave Log       one row per booking: Employee | Type | Start | End |
                  Duration (=end−start+1, blank-safe) | Status — with
                  blank filler rows so a fresh log is ready for input.

CALCULATION SEMANTICS (all LIVE formulas — nothing is frozen):

  booked(e)   = SUMIF(Log employee column, e, Log duration column)
  taken(e)    = SUMIFS(duration, employee = e, status = "Taken")
  planned(e)  = SUMIFS(duration, employee = e, status = "Planned")
  remaining(e)= entitlement + carried − booked (a blank entitlement
                cell coerces to 0, so the row still computes)
  duration(r) = IF(start or end blank, "", end − start + 1) — inclusive
                calendar days; a blank scaffold row stays blank

TEMPLATE MODE: a request with no people yet ("create a leave tracker")
builds the BLANK tracker — 8 blank employee rows on Balances and 10
blank log rows, every formula already in place and guarded on the
row's own key cell (`=IF($A5="","",…)`), dropdowns over all scaffold
rows. Never invents employees or bookings (hard rule), never refuses
for lack of data. Default reference values (Vacation / Sick / Unpaid
leave types, Taken / Planned statuses) are vocabulary, not user data —
they are always emitted.

Every formula reference is computed from the actual layout rows this
module emits, so off-by-N row math is impossible by construction. No
ROUND() anywhere — display rounding is the number format's job.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any, Dict, List, Optional

from app.services.patterns.utils import (
    PCT_FMT,
    QTY_FMT,
    _pick,
    to_int,
    to_iso_date,
    to_number,
)

# Registry key — must match the pattern stanza in
# prompts/pattern_classifier.md.
PATTERN_NAME = "leave_tracker"

PATTERN_DESCRIPTION = (
    "Employee annual leave / vacation / PTO tracker: entitlements, "
    "carried-over days, booked days per person from a leave log, "
    "remaining balances and utilization. Use when a small team tracks "
    "vacation days and who has booked what — a request with no "
    "employees yet still gets a blank tracker template. Do not use it "
    "for staff shift rosters or payroll."
)

# Routing keywords/stems — drive the cheap pre-gate and the classifier
# shortlist (see excel_gen._shortlist_patterns).
PATTERN_KEYWORDS = (
    "leave",
    "pto",
    "vacation",
    "absence",
    "sick",
    "time off",
    "annual leave",
    "leave balance",
    "carried over",
)

MAX_EMPLOYEES = 30
MAX_LOG_ROWS = 200
MIN_LOG_ROWS = 10  # blank filler rows so a fresh log is ready for input
MIN_EMPLOYEE_ROWS = 8  # blank Balances rows in template mode

# Extra types offered in the Type dropdown on top of the request's own
# words (deduped, comma-free — commas would corrupt the list source).
DEFAULT_LEAVE_TYPES = ("Vacation", "Sick", "Unpaid")

_LOG_SHEET = "Leave Log"
_DATE_FMT = "yyyy-mm-dd"
_DAYS_FMT = "0"

# Design tokens (same palette as the converter).
NAVY = "16304F"
STEEL = "1B3A5C"
GOLD = "C9A227"
MUTED = "5C6470"

# Excel's classic Good / Bad / Neutral conditional-format palettes.
CF_GREEN_FILL = "C6EFCE"
CF_GREEN_TEXT = "1E4620"
CF_RED_FILL = "FFC7CE"
CF_RED_TEXT = "9C0006"
CF_AMBER_FILL = "FFF2CC"
CF_AMBER_TEXT = "7F6000"

_WS_RE = re.compile(r"\s+")


def _clean_text(value: Any, cap: int) -> Optional[str]:
    """Strip + collapse whitespace + cap length; None when empty."""
    if not isinstance(value, str):
        return None
    s = _WS_RE.sub(" ", value.strip())
    return s[:cap] if s else None


def _normalize_employee(entry: Any) -> Optional[dict]:
    """One employees entry → {name, department, entitlement, carried}."""
    if isinstance(entry, str):
        name = _clean_text(entry, 60)
        raw: Dict[str, Any] = {}
    elif isinstance(entry, dict):
        name = _clean_text(_pick(entry, "name", "employee", "person", "staff"), 60)
        raw = entry
    else:
        return None
    if not name:
        return None

    entitlement = to_number(_pick(raw, "annual_leave_days", "entitlement", "days"))
    if entitlement is not None:
        entitlement = max(0.0, min(entitlement, 365.0))
    carried = to_number(_pick(raw, "carried_over", "carry_over", "carried"))
    carried = 0.0 if carried is None else max(0.0, min(carried, 365.0))

    return {
        "name": name,
        "department": _clean_text(_pick(raw, "department", "dept"), 40),
        "entitlement": entitlement,
        "carried": carried,
    }


def _normalize_log_entry(entry: Any) -> Optional[dict]:
    """One leave_log entry → {employee, type, start, end, status}."""
    if not isinstance(entry, dict):
        return None
    employee = _clean_text(_pick(entry, "employee", "name", "person", "staff"), 60)
    start = to_iso_date(_pick(entry, "start", "start_date", "from"))
    if not employee or start is None:
        return None
    end = to_iso_date(_pick(entry, "end", "end_date", "to")) or start
    if end < start:  # tolerate a swapped range from a small model
        start, end = end, start
    leave_type = _clean_text(_pick(entry, "type", "leave_type", "category"), 30)
    status_raw = _pick(entry, "status", "state")
    status = (
        "Taken"
        if isinstance(status_raw, str) and status_raw.strip().lower() == "taken"
        else "Planned"
    )
    return {
        "employee": employee,
        "type": leave_type,
        "start": start,
        "end": end,
        "status": status,
    }


def coerce_leave_tracker_params(params: dict) -> dict:
    """Validate + normalize classifier params; raises ValueError.

    Template mode: empty/missing employees and leave_log are FINE —
    the builder emits the blank tracker (the "create a leave tracker"
    case). ValueError only for structurally wrong params (non-object
    params, non-array employees / leave_log).
    """
    if not isinstance(params, dict):
        raise ValueError("params must be an object")

    today = date.today()

    year = to_int(_pick(params, "year", "leave_year"))
    if year is None:
        year = today.year
    year = max(1990, min(year, 2100))

    employees: List[dict] = []
    seen: set = set()
    raw_employees = _pick(params, "employees", "staff", "team")
    if raw_employees is not None:
        if not isinstance(raw_employees, list):
            raise ValueError("employees must be an array")
        for entry in raw_employees[:MAX_EMPLOYEES]:
            normalized = _normalize_employee(entry)
            if normalized is None:
                continue
            if normalized["name"].lower() in seen:
                continue
            seen.add(normalized["name"].lower())
            employees.append(normalized)

    log: List[dict] = []
    raw_log = _pick(params, "leave_log", "log", "bookings", "leave_entries")
    if raw_log is not None:
        if not isinstance(raw_log, list):
            raise ValueError("leave_log must be an array")
        for entry in raw_log[:MAX_LOG_ROWS]:
            normalized = _normalize_log_entry(entry)
            if normalized is not None:
                log.append(normalized)

    # People named only in the log still belong on the Balances sheet —
    # their days would otherwise silently vanish from every total.
    for entry in log:
        if entry["employee"].lower() in seen:
            continue
        if len(employees) >= MAX_EMPLOYEES:
            break
        seen.add(entry["employee"].lower())
        employees.append(
            {
                "name": entry["employee"],
                "department": None,
                "entitlement": None,
                "carried": 0.0,
            }
        )

    # Template mode: no employees and no log entries is FINE — the
    # builder emits blank Balances rows + a blank log, every formula
    # guarded (never invent people; hard rule).

    # Dropdown list: the request's own leave types + the standard ones,
    # comma/quote-free (commas corrupt Excel's inline list source).
    types: List[str] = []
    for entry in log:
        if entry["type"] and entry["type"] not in types:
            types.append(entry["type"])
    for extra in DEFAULT_LEAVE_TYPES:
        if extra not in types:
            types.append(extra)
    types = [t for t in types if "," not in t and '"' not in t][:10]

    notes = _pick(params, "notes", "note")
    notes = _clean_text(notes, 1000)

    return {
        "year": year,
        "employees": employees,
        "leave_log": log,
        "types": types,
        "notes": notes,
    }


def build_leave_tracker_spec(params: dict) -> dict:
    """Leave tracker workbook — every formula code-generated.

    Layout (rows computed here, never guessed by a model):

    Balances sheet:
      row 1      title · row 2 usage hint
      row 4      headers (start_cell A4, no table title)
      rows 5..   one row per employee (8 blank scaffold rows when the
                 request names nobody): inputs A..D, live guarded
                 formulas E..I (`=IF($A5="","",…)`)
      row T      totals (SUM over the exact data rows)
    Leave Log sheet:
      row 4      headers
      rows 5..   one row per booking + blank fillers up to MIN_LOG_ROWS
                 (10 blank rows in template mode)
      row T      total booked days (SUM over the exact data rows)
    """
    p = coerce_leave_tracker_params(params)
    year: int = p["year"]
    employees: List[dict] = p["employees"]
    log: List[dict] = p["leave_log"]
    types: List[str] = p["types"]
    notes = p["notes"]

    n_emp = len(employees)
    n_emp_rows = n_emp or MIN_EMPLOYEE_ROWS  # template scaffold rows
    n_log = max(len(log), MIN_LOG_ROWS)
    has_data = n_emp > 0  # charts only make sense with people

    # ── geometry (integers first — every formula is formatted from these)
    bal_r0 = 5  # first Balances data row
    bal_rN = 4 + n_emp_rows  # last Balances data row
    bal_total = bal_rN + 1  # Balances total row
    log_r0 = 5  # first Log data row
    log_rN = 4 + n_log  # last Log data row
    log_total = log_rN + 1  # noqa: Log total row

    log_emp_col = f"'{_LOG_SHEET}'!$A${log_r0}:$A${log_rN}"
    log_dur_col = f"'{_LOG_SHEET}'!$E${log_r0}:$E${log_rN}"
    log_status_col = f"'{_LOG_SHEET}'!$F${log_r0}:$F${log_rN}"

    # ═════════════════════════════ Balances sheet ════════════════════
    bal_rows: List[List[Any]] = []
    for i in range(n_emp_rows):
        r = bal_r0 + i
        emp = employees[i] if i < n_emp else None
        bal_rows.append(
            [
                emp["name"] if emp else None,
                emp["department"] if emp else None,
                emp["entitlement"] if emp else None,
                emp["carried"] if emp else None,
                # Booked = every log row for this employee (Taken +
                # Planned); blank until the row has a name
                '=IF($A{r}="","",SUMIF({emp},$A{r},{dur}))'.format(
                    emp=log_emp_col, r=r, dur=log_dur_col
                ),
                # Taken / Planned split
                '=IF($A{r}="","",SUMIFS({dur},{emp},$A{r},{status},"Taken"))'.format(
                    dur=log_dur_col, emp=log_emp_col, r=r, status=log_status_col
                ),
                '=IF($A{r}="","",SUMIFS({dur},{emp},$A{r},{status},"Planned"))'.format(
                    dur=log_dur_col, emp=log_emp_col, r=r, status=log_status_col
                ),
                # Remaining = entitlement + carried − booked, blank
                # until the row has a name (a blank entitlement cell
                # coerces to 0 and still computes)
                '=IF($A{r}="","",C{r}+D{r}-E{r})'.format(r=r),
                # Utilization — division-by-zero guarded
                '=IF($A{r}="","",IF((C{r}+D{r})>0,E{r}/(C{r}+D{r}),0))'.format(r=r),
            ]
        )

    bal_sheet: Dict[str, Any] = {
        "name": "Balances",
        "tab_color": NAVY,
        "freeze_panes": "A5",
        "column_widths": {
            "A": 24,
            "B": 18,
            "C": 13,
            "D": 13,
            "E": 12,
            "F": 12,
            "G": 12,
            "H": 13,
            "I": 12,
        },
        "text_blocks": [
            {
                "cell": "A1",
                "text": "Leave Tracker — {year}".format(year=year),
                "bold": True,
                "font_size": 14,
                "font_color": NAVY,
            },
            {
                "cell": "A2",
                "text": (
                    "Edit Entitlement and Carried Over, and log bookings on "
                    "the Leave Log tab — Booked, Taken, Planned, Remaining "
                    "and Utilization update automatically."
                ),
                "italic": True,
                "font_color": MUTED,
            },
        ],
        "tables": [
            {
                "start_cell": "A4",
                "headers": [
                    "Employee",
                    "Department",
                    "Entitlement (Days)",
                    "Carried Over (Days)",
                    "Booked (Days)",
                    "Taken (Days)",
                    "Planned (Days)",
                    "Remaining (Days)",
                    "Utilization",
                ],
                "rows": bal_rows,
                "number_formats": {
                    "C": QTY_FMT,
                    "D": QTY_FMT,
                    "E": QTY_FMT,
                    "F": QTY_FMT,
                    "G": QTY_FMT,
                    "H": QTY_FMT,
                    "I": PCT_FMT,
                },
                "alignments": {c: "center" for c in "CDEFGHI"},
                "total_row": [
                    "Total",
                    None,
                    "=SUM(C{r0}:C{rN})".format(r0=bal_r0, rN=bal_rN),
                    "=SUM(D{r0}:D{rN})".format(r0=bal_r0, rN=bal_rN),
                    "=SUM(E{r0}:E{rN})".format(r0=bal_r0, rN=bal_rN),
                    "=SUM(F{r0}:F{rN})".format(r0=bal_r0, rN=bal_rN),
                    "=SUM(G{r0}:G{rN})".format(r0=bal_r0, rN=bal_rN),
                    "=SUM(H{r0}:H{rN})".format(r0=bal_r0, rN=bal_rN),
                    "=IF((C{t}+D{t})>0,E{t}/(C{t}+D{t}),0)".format(t=bal_total),
                ],
            }
        ],
        "charts": (
            [
                {
                    "type": "bar",
                    "title": "Leave Days by Employee — {year}".format(year=year),
                    "anchor": "K3",
                    "width": 15,
                    "height": 9,
                    "categories_range": "Balances!$A${r0}:$A${rN}".format(
                        r0=bal_r0, rN=bal_rN
                    ),
                    "series": [
                        {
                            "name": "Booked Days",
                            "values_range": "Balances!$E${r0}:$E${rN}".format(
                                r0=bal_r0, rN=bal_rN
                            ),
                        },
                        {
                            "name": "Remaining Days",
                            "values_range": "Balances!$H${r0}:$H${rN}".format(
                                r0=bal_r0, rN=bal_rN
                            ),
                        },
                    ],
                    "value_numfmt": QTY_FMT,
                }
            ]
            if has_data
            else []  # no people yet → nothing to chart (never invent)
        ),
        "conditional_formats": [
            # Remaining < 0 → over-allocated (red); 0..5 days left (amber)
            {
                "range": "H{r0}:H{rN}".format(r0=bal_r0, rN=bal_rN),
                "rules": [
                    {
                        "type": "cell_is",
                        "operator": "less_than",
                        "value": 0,
                        "fill": CF_RED_FILL,
                        "font_color": CF_RED_TEXT,
                        "bold": True,
                        "stop_if_true": True,
                    },
                    {
                        "type": "cell_is",
                        "operator": "between",
                        "value": [0, 5],
                        "fill": CF_AMBER_FILL,
                        "font_color": CF_AMBER_TEXT,
                        "stop_if_true": False,
                    },
                ],
            },
        ],
        "protect": {"unlocked_ranges": ["A{r0}:D{rN}".format(r0=bal_r0, rN=bal_rN)]},
        "notes": notes
        or (
            "Type people into the Employee column (blank rows are ready) "
            "— their formulas fill in as you type. Entitlement and Carried "
            "Over are the editable inputs (leave a blank Entitlement "
            "until you know it — it counts as 0). Booked sums every "
            "Leave Log row for the employee; Taken and Planned split it "
            "by status; Remaining = Entitlement + Carried − Booked; "
            "Utilization = Booked ÷ (Entitlement + Carried), guarded "
            "against a zero denominator. Add bookings on the Leave Log "
            "tab — pick the employee, type and status from the dropdowns; "
            "Duration = End − Start + 1 (inclusive) and fills itself in. "
            "Remaining turns red when someone is over-allocated and "
            "amber when five days or fewer are left."
        ),
    }

    # ═════════════════════════════ Leave Log sheet ═══════════════════
    log_rows: List[List[Any]] = []
    for j in range(n_log):
        r = log_r0 + j
        entry = log[j] if j < len(log) else None
        log_rows.append(
            [
                entry["employee"] if entry else None,
                entry["type"] if entry else None,
                entry["start"] if entry else None,
                entry["end"] if entry else None,
                # Duration = End − Start + 1 (inclusive); blank while
                # Start or End is missing so empty scaffold rows show
                # nothing instead of a column of zeros
                '=IF(OR(C{r}="",D{r}=""),"",D{r}-C{r}+1)'.format(r=r),
                entry["status"] if entry else None,
            ]
        )

    log_sheet: Dict[str, Any] = {
        "name": _LOG_SHEET,
        "tab_color": STEEL,
        "freeze_panes": "A5",
        "column_widths": {"A": 24, "B": 16, "C": 13, "D": 13, "E": 13, "F": 12},
        "text_blocks": [
            {
                "cell": "A1",
                "text": "Leave Log",
                "bold": True,
                "font_size": 14,
                "font_color": NAVY,
            },
            {
                "cell": "A2",
                "text": (
                    "One row per booking — Duration and the Balances tab "
                    "fill themselves in. Status: Taken (already happened) "
                    "or Planned."
                ),
                "italic": True,
                "font_color": MUTED,
            },
        ],
        "tables": [
            {
                "start_cell": "A4",
                "headers": [
                    "Employee",
                    "Type",
                    "Start",
                    "End",
                    "Duration (Days)",
                    "Status",
                ],
                "rows": log_rows,
                "number_formats": {
                    "C": _DATE_FMT,
                    "D": _DATE_FMT,
                    "E": _DAYS_FMT,
                },
                "alignments": {
                    "C": "center",
                    "D": "center",
                    "E": "center",
                    "F": "center",
                },
                "total_row": [
                    "Total",
                    None,
                    None,
                    None,
                    "=SUM(E{r0}:E{rN})".format(r0=log_r0, rN=log_rN),
                    None,
                ],
            }
        ],
        "data_validation": [
            {
                # live range source: employees added on Balances appear here
                "range": "A{r0}:A{rN}".format(r0=log_r0, rN=log_rN),
                "source_range": "Balances!$A${r0}:$A${rN}".format(r0=bal_r0, rN=bal_rN),
                "allow_blank": True,
                "prompt_title": "Employee",
                "prompt": "Pick the person taking the leave.",
                "error_title": "Not on the Balances tab",
                "error": (
                    "Add the employee on the Balances tab first — the "
                    "dropdown picks them up automatically."
                ),
                "error_style": "warning",
            },
            {
                "range": "B{r0}:B{rN}".format(r0=log_r0, rN=log_rN),
                "values": types,
                "allow_blank": True,
                "prompt_title": "Leave type",
                "prompt": "Vacation, Sick, Unpaid — or the request's own words.",
                "error_style": "warning",
            },
            {
                "range": "F{r0}:F{rN}".format(r0=log_r0, rN=log_rN),
                "values": ["Taken", "Planned"],
                "allow_blank": True,
                "prompt_title": "Status",
                "prompt": "Taken = the leave already happened · Planned = booked ahead.",
                "error_title": "Invalid status",
                "error": "Pick Taken or Planned.",
                "error_style": "stop",
            },
        ],
        "conditional_formats": [
            {
                "range": "F{r0}:F{rN}".format(r0=log_r0, rN=log_rN),
                "rules": [
                    {
                        "type": "cell_is",
                        "operator": "equal",
                        "value": "Taken",
                        "fill": CF_GREEN_FILL,
                        "font_color": CF_GREEN_TEXT,
                        "stop_if_true": True,
                    },
                    {
                        "type": "cell_is",
                        "operator": "equal",
                        "value": "Planned",
                        "fill": CF_AMBER_FILL,
                        "font_color": CF_AMBER_TEXT,
                        "stop_if_true": False,
                    },
                ],
            },
        ],
        "protect": {
            "unlocked_ranges": [
                "A{r0}:D{rN}".format(r0=log_r0, rN=log_rN),
                "F{r0}:F{rN}".format(r0=log_r0, rN=log_rN),
            ]
        },
        "notes": (
            "Duration = End − Start + 1 (inclusive calendar days) and "
            "stays blank while Start or End is missing, so empty rows "
            "never inflate the totals. Employee names come live from the "
            "Balances tab; Type and Status are dropdowns. The total row "
            "sums all booked days; the Balances tab splits them into "
            "Taken vs Planned per employee."
        ),
    }

    return {
        "filename": "leave_tracker_{year}.xlsx".format(year=year),
        "sheets": [bal_sheet, log_sheet],
    }


# ── Standard pattern entry points (used by the dynamic registry) ──────

coerce_params = coerce_leave_tracker_params
build_spec = build_leave_tracker_spec
