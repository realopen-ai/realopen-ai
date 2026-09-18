"""
Event planner pattern — ONE dated event (wedding, party, conference…):
countdown, guests & RSVP, vendors & deposits, planning tasks.

Four sheets:

  Event     (navy)    header block — event name / date / days-until
                      (=event_date-TODAY()) / venue / guest count /
                      budget — plus a live "Planning Status" block
                      reading the other sheets with COUNTIF/SUMIF
  Guests    (steel)  Guest | RSVP | Plus Ones — Yes/No/Pending
                      dropdown with green/red/amber colors, RSVP
                      COUNTIF tallies below the table
  Vendors   (steel)  Vendor | Category | Cost | Deposit Paid |
                      Balance Due (=cost-deposit_paid, live) | Status;
                      SUM total row, budget utilization =
                      SUM(costs)/budget_total guarded by IF, and a
                      cost-by-vendor pie chart
  Tasks     (steel)  Task | Owner | Due Date | Days Until Due
                      (=due-TODAY()) | Done flag — % complete =
                      COUNTIF(done,"Yes")/COUNTA(tasks) guarded

Lists only ever contain what the request gives (never invented); thin
blank scaffold rows pad each list to MIN_ROWS so the dropdowns and
formulas keep working as the user adds rows. RSVP defaults to
"Pending", deposit_paid to 0, done to "No" (the stanza's defaults).
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from app.services.patterns.utils import (
    MONEY_FMT,
    PCT_FMT,
    _currency_fmt,
    _pick,
    to_iso_date,
    to_number,
)

# Registry key — must match the pattern stanza in
# prompts/pattern_classifier.md.
PATTERN_NAME = "event_planner"

PATTERN_DESCRIPTION = (
    "One-event planner (wedding, party, conference…): countdown header, "
    "guest list with RSVP tallies, vendors with deposits and budget "
    "utilization, a task checklist with % complete, and a vendor cost "
    "pie chart."
)

# Routing keywords/stems — drive the cheap pre-gate and the classifier
# shortlist (see excel_gen._shortlist_patterns).
PATTERN_KEYWORDS = (
    "event",
    "wedding",
    "party",
    "conference",
    "guest list",
    "guest",
    "rsvp",
    "venue",
    "vendor",
    "celebration",
    "reception",
    "banquet",
    "gala",
    "shower",
)

MAX_GUESTS = 300
MAX_VENDORS = 100
MAX_TASKS = 200
MIN_ROWS = 10  # blank scaffold padding per list sheet

# Design tokens (same palette as the converter).
NAVY = "16304F"
STEEL = "1B3A5C"
MUTED = "5C6470"

# Excel's classic Good / Neutral / Bad conditional-format palettes.
CF_GREEN_FILL = "C6EFCE"
CF_GREEN_TEXT = "1E4620"
CF_AMBER_FILL = "FFF2CC"
CF_AMBER_TEXT = "7F6000"
CF_RED_FILL = "FFC7CE"
CF_RED_TEXT = "9C0006"

DATE_FMT = "yyyy-mm-dd"
INT_FMT = "0"

VENDOR_STATUS_OPTIONS = ("Confirmed", "Pending", "Cancelled")


def _clean_text(value: Any, limit: int) -> Optional[str]:
    if value is None:
        return None
    s = str(value).strip()
    return s[:limit] or None


def _rsvp(value: Any) -> str:
    """RSVP answer → Yes/No/Pending ("Pending" when not answered)."""
    if isinstance(value, str):
        s = value.strip().lower()
        if s in ("yes", "y", "coming", "confirmed", "attending"):
            return "Yes"
        if s in ("no", "n", "declined", "not coming"):
            return "No"
    return "Pending"


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("yes", "y", "true", "t", "1")
    if isinstance(value, (int, float)):
        return bool(value)
    return False


def _to_count(value: Any) -> Optional[int]:
    n = to_number(value)
    if n is None:
        return None
    return max(0, int(round(n)))


def _normalize_guest(entry: Any) -> Optional[dict]:
    if isinstance(entry, str):
        name = entry.strip()
        if not name:
            return None
        return {"name": name[:80], "rsvp": "Pending", "plus_ones": 0}
    if not isinstance(entry, dict):
        return None
    name = _clean_text(_pick(entry, "name", "guest", "guest_name"), 80)
    if not name:
        return None
    plus_ones = _to_count(_pick(entry, "plus_ones", "plus_one", "extras", "companions"))
    return {
        "name": name,
        "rsvp": _rsvp(_pick(entry, "rsvp", "status", "response")),
        "plus_ones": plus_ones or 0,
    }


def _normalize_vendor(entry: Any) -> Optional[dict]:
    if not isinstance(entry, dict):
        return None
    name = _clean_text(_pick(entry, "name", "vendor", "company", "provider"), 80)
    category = _clean_text(_pick(entry, "category", "type", "service"), 60)
    if not name and not category:
        return None
    cost = to_number(_pick(entry, "cost", "price", "amount", "total"))
    deposit = to_number(_pick(entry, "deposit_paid", "deposit", "paid", "paid_amount"))
    return {
        "name": name or category,  # row identity: name, else its category
        "category": category,
        "cost": cost or 0.0,
        "deposit_paid": deposit or 0.0,  # stanza default: 0 when not stated
        "status": _clean_text(_pick(entry, "status", "state"), 40),
    }


def _normalize_task(entry: Any) -> Optional[dict]:
    if isinstance(entry, str):
        task = entry.strip()
        if not task:
            return None
        return {
            "task": task[:200],
            "due_date": None,
            "owner": None,
            "done": False,  # stanza default: done false when not stated
        }
    if not isinstance(entry, dict):
        return None
    task = _clean_text(
        _pick(entry, "task", "name", "title", "todo", "description"), 200
    )
    if not task:
        return None
    return {
        "task": task,
        "due_date": to_iso_date(_pick(entry, "due_date", "due", "date")),
        "owner": _clean_text(
            _pick(entry, "owner", "assigned_to", "assignee", "responsible"), 80
        ),
        "done": _truthy(_pick(entry, "done", "completed", "complete", "is_done")),
    }


def _normalize_list(raw: Any, normalizer, limit: int, what: str) -> List[dict]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError(f"{what} must be an array")
    out: List[dict] = []
    for entry in raw[:limit]:
        normalized = normalizer(entry)
        if normalized is not None:
            out.append(normalized)
    return out


def coerce_event_planner_params(params: dict) -> dict:
    """Validate + normalize classifier params; raises ValueError.

    REQUIRED per the stanza: event_name and event_date. Everything
    else may be absent (lists then simply render as scaffold rows).
    """
    if not isinstance(params, dict):
        raise ValueError("params must be an object")

    event_name = _clean_text(_pick(params, "event_name", "event", "name", "title"), 80)
    if not event_name:
        raise ValueError("event_name is required")

    event_date = to_iso_date(_pick(params, "event_date", "date", "when", "day"))
    if event_date is None:
        raise ValueError("event_date is required (YYYY-MM-DD)")

    venue = _clean_text(_pick(params, "venue", "location", "place", "address"), 120)
    guest_count = _to_count(
        _pick(params, "guest_count", "attendees", "headcount", "invitees")
    )
    budget_total = to_number(_pick(params, "budget_total", "budget", "total_budget"))
    if budget_total is not None:
        budget_total = max(0.0, budget_total)

    currency = _clean_text(_pick(params, "currency"), 10)

    guests = _normalize_list(
        _pick(params, "guests", "guest_list"), _normalize_guest, MAX_GUESTS, "guests"
    )
    vendors = _normalize_list(
        _pick(params, "vendors", "vendor_list"),
        _normalize_vendor,
        MAX_VENDORS,
        "vendors",
    )
    tasks = _normalize_list(
        _pick(params, "tasks", "task_list", "checklist"),
        _normalize_task,
        MAX_TASKS,
        "tasks",
    )

    notes = _pick(params, "notes", "note")
    notes = notes.strip()[:1000] if isinstance(notes, str) and notes.strip() else None

    return {
        "event_name": event_name,
        "event_date": event_date,
        "venue": venue,
        "guest_count": guest_count,
        "budget_total": budget_total,
        "currency": currency,
        "guests": guests,
        "vendors": vendors,
        "tasks": tasks,
        "notes": notes,
    }


def _pair(
    row: int, label: str, value: Any, number_format: Optional[str] = None
) -> List[dict]:
    """A label + value text-block pair in columns A/B."""
    blocks = [{"cell": f"A{row}", "text": label}]
    value_block: Dict[str, Any] = {"cell": f"B{row}", "text": value}
    if number_format:
        value_block["number_format"] = number_format
    blocks.append(value_block)
    return blocks


def _section(row: int, label: str) -> dict:
    return {"cell": f"A{row}", "text": label, "bold": True, "font_color": NAVY}


def build_event_planner_spec(params: dict) -> dict:
    """Event planner workbook — every formula code-generated.

    Layout (rows computed here, never guessed by a model):

    Event sheet:
      rows 4..     header block (name/date/days-until/venue/guests/budget)
      rows +2..    Planning Status (live cross-sheet COUNTIF/SUMIF)
    Guests sheet:
      row 3 header, rows 4.. table (padded to MIN_ROWS), tallies below
    Vendors sheet:
      row 3 header, rows 4.. table (balance-due formulas prefilled),
      SUM total row, budget block below, pie chart anchored H3
    Tasks sheet:
      row 3 header, rows 4.. table (days-until formulas prefilled),
      % complete block below
    """
    p = coerce_event_planner_params(params)
    event_name: str = p["event_name"]
    event_date: str = p["event_date"]
    venue = p["venue"]
    guest_count = p["guest_count"]
    budget_total = p["budget_total"]
    currency = p["currency"]
    guests: List[dict] = p["guests"]
    vendors: List[dict] = p["vendors"]
    tasks: List[dict] = p["tasks"]
    notes = p["notes"]

    money = _currency_fmt(currency) or MONEY_FMT

    # ── list-sheet geometry (tables start at A3 → header row 3) ─────
    gcount = max(len(guests), MIN_ROWS)
    gfirst = 4
    glast = gfirst + gcount - 1

    vcount = max(len(vendors), MIN_ROWS)
    vfirst = 4
    vlast = vfirst + vcount - 1
    vtotal = vlast + 1  # vendors SUM total row

    tcount = max(len(tasks), MIN_ROWS)
    tfirst = 4
    tlast = tfirst + tcount - 1

    # ═════════════════════════════ Event sheet ═════════════════════
    blocks: List[dict] = [
        {
            "cell": "A1",
            "text": event_name,
            "bold": True,
            "font_size": 14,
            "font_color": NAVY,
        },
        {
            "cell": "A2",
            "text": (
                "The countdown, RSVP tallies, vendor costs and task "
                "progress below are live — they update as you edit the "
                "Guests, Vendors and Tasks sheets."
            ),
            "italic": True,
            "font_color": MUTED,
        },
    ]

    row = 4
    name_row = row
    row += 1
    date_row = row
    row += 1
    days_row = row
    row += 1
    venue_row = guest_row = budget_row = None
    if venue:
        venue_row = row
        row += 1
    if guest_count is not None:
        guest_row = row
        row += 1
    if budget_total is not None:
        budget_row = row
        row += 1

    blocks += _pair(name_row, "Event Name", event_name)
    blocks += _pair(date_row, "Event Date", event_date, DATE_FMT)
    blocks += _pair(
        days_row, "Days Until Event", "=B{r}-TODAY()".format(r=date_row), INT_FMT
    )
    if venue_row is not None:
        blocks += _pair(venue_row, "Venue", venue)
    if guest_row is not None:
        blocks += _pair(guest_row, "Guest Count", guest_count, INT_FMT)
    if budget_row is not None:
        blocks += _pair(budget_row, "Budget Total", budget_total, money)

    # Planning Status — live cross-sheet reads. A running row cursor
    # keeps the stack gapless whether or not a budget was stated.
    ps0 = row + 1  # one blank row after the header block
    status_row = ps0 + 1
    blocks.append(_section(ps0, "Planning Status"))
    blocks += _pair(
        status_row,
        "Guests Confirmed",
        '=COUNTIF(Guests!$B${f}:$B${l},"Yes")'.format(f=gfirst, l=glast),
        INT_FMT,
    )
    status_row += 1
    blocks += _pair(
        status_row,
        "Expected Attendees",
        '=COUNTIF(Guests!$B${f}:$B${l},"Yes")'
        '+SUMIF(Guests!$B${f}:$B${l},"Yes",Guests!$C${f}:$C${l})'.format(
            f=gfirst, l=glast
        ),
        INT_FMT,
    )
    status_row += 1
    blocks += _pair(
        status_row,
        "Total Vendor Costs",
        "=Vendors!$C${r}".format(r=vtotal),
        money,
    )
    if budget_row is not None:
        status_row += 1
        blocks += _pair(
            status_row,
            "Budget Utilization",
            '=IF($B${b}>0,Vendors!$C${v}/$B${b},"n/a")'.format(b=budget_row, v=vtotal),
            PCT_FMT,
        )
    status_row += 1
    blocks += _pair(
        status_row,
        "Tasks Complete",
        '=IF(COUNTA(Tasks!$A${f}:$A${l})=0,"n/a",'
        'COUNTIF(Tasks!$E${f}:$E${l},"Yes")/COUNTA(Tasks!$A${f}:$A${l}))'.format(
            f=tfirst, l=tlast
        ),
        PCT_FMT,
    )

    event_sheet: Dict[str, Any] = {
        "name": "Event",
        "tab_color": NAVY,
        "column_widths": {"A": 24, "B": 26},
        "text_blocks": blocks,
        "notes": notes
        or (
            "One event per workbook. The Guests/Vendors/Tasks sheets hold "
            "the editable lists (blank rows are ready for new entries); "
            "this page's countdown and status figures recalculate live."
        ),
    }

    # ═════════════════════════════ Guests sheet ════════════════════
    guest_rows: List[List[Any]] = []
    for i in range(gcount):
        if i < len(guests):
            g = guests[i]
            guest_rows.append([g["name"], g["rsvp"], g["plus_ones"]])
        else:
            guest_rows.append([None, None, None])

    t0 = glast + 3
    guest_blocks: List[dict] = [
        {
            "cell": "A1",
            "text": "Guests",
            "bold": True,
            "font_size": 12,
            "font_color": STEEL,
        }
    ]
    guest_blocks.append(_section(t0, "RSVP Summary"))
    tally_rows: List[Tuple[str, str]] = [
        ("Confirmed", '=COUNTIF(B{f}:B{l},"Yes")'.format(f=gfirst, l=glast)),
        ("Declined", '=COUNTIF(B{f}:B{l},"No")'.format(f=gfirst, l=glast)),
        ("Pending", '=COUNTIF(B{f}:B{l},"Pending")'.format(f=gfirst, l=glast)),
        ("Total On List", "=COUNTA(A{f}:A{l})".format(f=gfirst, l=glast)),
        (
            "Plus Ones (Confirmed)",
            '=SUMIF(B{f}:B{l},"Yes",C{f}:C{l})'.format(f=gfirst, l=glast),
        ),
        (
            "Expected Attendees",
            '=COUNTIF(B{f}:B{l},"Yes")+SUMIF(B{f}:B{l},"Yes",C{f}:C{l})'.format(
                f=gfirst, l=glast
            ),
        ),
    ]
    for i, (label, formula) in enumerate(tally_rows):
        guest_blocks += _pair(t0 + 1 + i, label, formula, INT_FMT)

    guests_sheet: Dict[str, Any] = {
        "name": "Guests",
        "tab_color": STEEL,
        "freeze_panes": "A4",
        "column_widths": {"A": 28, "B": 12, "C": 12},
        "text_blocks": guest_blocks,
        "tables": [
            {
                "start_cell": "A3",
                "headers": ["Guest", "RSVP", "Plus Ones"],
                "rows": guest_rows,
                "number_formats": {"C": INT_FMT},
                "alignments": {"B": "center", "C": "center"},
            }
        ],
        "data_validation": [
            {
                "range": f"B{gfirst}:B{glast}",
                "values": ["Yes", "No", "Pending"],
                "allow_blank": True,
            }
        ],
        "conditional_formats": [
            {
                "range": f"B{gfirst}:B{glast}",
                "rules": [
                    {
                        "type": "cell_is",
                        "operator": "equal",
                        "value": "Yes",
                        "fill": CF_GREEN_FILL,
                        "font_color": CF_GREEN_TEXT,
                    },
                    {
                        "type": "cell_is",
                        "operator": "equal",
                        "value": "No",
                        "fill": CF_RED_FILL,
                        "font_color": CF_RED_TEXT,
                    },
                    {
                        "type": "cell_is",
                        "operator": "equal",
                        "value": "Pending",
                        "fill": CF_AMBER_FILL,
                        "font_color": CF_AMBER_TEXT,
                    },
                ],
            }
        ],
        "notes": (
            "Pick Yes/No/Pending from the RSVP dropdown — colors and the "
            "tallies below the table update automatically. Plus Ones "
            "counts extra people for confirmed guests."
        ),
    }

    # ═════════════════════════════ Vendors sheet ═══════════════════
    vendor_rows: List[List[Any]] = []
    for i in range(vcount):
        r = vfirst + i
        if i < len(vendors):
            v = vendors[i]
            vendor_rows.append(
                [
                    v["name"],
                    v["category"],
                    v["cost"],
                    v["deposit_paid"],
                    '=IF($A{r}="","",C{r}-D{r})'.format(r=r),
                    v["status"],
                ]
            )
        else:
            vendor_rows.append(
                [None, None, None, None, '=IF($A{r}="","",C{r}-D{r})'.format(r=r), None]
            )

    vendor_blocks: List[dict] = [
        {
            "cell": "A1",
            "text": "Vendors",
            "bold": True,
            "font_size": 12,
            "font_color": STEEL,
        }
    ]
    b0 = vtotal + 3
    vendor_blocks.append(_section(b0, "Budget"))
    vendor_blocks += _pair(
        b0 + 1, "Total Deposits Paid", "=D{r}".format(r=vtotal), money
    )
    vendor_blocks += _pair(b0 + 2, "Total Balance Due", "=E{r}".format(r=vtotal), money)
    if budget_row is not None:
        vendor_blocks += _pair(
            b0 + 3,
            "Budget Utilization",
            '=IF(Event!$B${b}>0,C{v}/Event!$B${b},"n/a")'.format(
                b=budget_row, v=vtotal
            ),
            PCT_FMT,
        )
        vendor_blocks += _pair(
            b0 + 4,
            "Remaining Budget",
            "=Event!$B${b}-C{v}".format(b=budget_row, v=vtotal),
            money,
        )

    vendors_sheet: Dict[str, Any] = {
        "name": "Vendors",
        "tab_color": STEEL,
        "freeze_panes": "A4",
        "column_widths": {
            "A": 26,
            "B": 16,
            "C": 14,
            "D": 15,
            "E": 15,
            "F": 13,
        },
        "text_blocks": vendor_blocks,
        "tables": [
            {
                "start_cell": "A3",
                "headers": [
                    "Vendor",
                    "Category",
                    "Cost",
                    "Deposit Paid",
                    "Balance Due",
                    "Status",
                ],
                "rows": vendor_rows,
                "number_formats": {"C": money, "D": money, "E": money},
                "alignments": {"F": "center"},
                "total_row": [
                    "Total",
                    "",
                    "=SUM(C{first_row}:C{last_row})",
                    "=SUM(D{first_row}:D{last_row})",
                    "=SUM(E{first_row}:E{last_row})",
                    "",
                ],
            }
        ],
        "data_validation": [
            {
                # warning style: the request may carry its own status word
                "range": f"F{vfirst}:F{vlast}",
                "values": list(VENDOR_STATUS_OPTIONS),
                "allow_blank": True,
                "error_style": "warning",
            }
        ],
        "conditional_formats": [
            {
                "range": f"E{vfirst}:E{vlast}",
                "rules": [
                    {
                        "type": "cell_is",
                        "operator": "greater_than",
                        "value": 0,
                        "fill": CF_AMBER_FILL,
                        "font_color": CF_AMBER_TEXT,
                    }
                ],
            },
            {
                "range": f"F{vfirst}:F{vlast}",
                "rules": [
                    {
                        "type": "cell_is",
                        "operator": "equal",
                        "value": "Confirmed",
                        "fill": CF_GREEN_FILL,
                        "font_color": CF_GREEN_TEXT,
                    },
                    {
                        "type": "cell_is",
                        "operator": "equal",
                        "value": "Pending",
                        "fill": CF_AMBER_FILL,
                        "font_color": CF_AMBER_TEXT,
                    },
                    {
                        "type": "cell_is",
                        "operator": "equal",
                        "value": "Cancelled",
                        "fill": CF_RED_FILL,
                        "font_color": CF_RED_TEXT,
                    },
                ],
            },
        ],
        "notes": (
            "Balance Due = Cost - Deposit Paid (live; blank rows are "
            "ready for new vendors). The Budget block and the pie chart "
            "track the totals against the event budget."
        ),
    }

    # Pie of the cost mix — one slice per REAL vendor row (blank
    # scaffold rows stay out of the chart).
    if len(vendors) >= 2:
        vlast_real = vfirst + len(vendors) - 1
        vendors_sheet["charts"] = [
            {
                "type": "pie",
                "title": "Vendor Costs",
                "anchor": "H3",
                "width": 12,
                "height": 9,
                "categories_range": "Vendors!A{f}:A{l}".format(f=vfirst, l=vlast_real),
                "series": [
                    {
                        "name": "Cost",
                        "values_range": "Vendors!C{f}:C{l}".format(
                            f=vfirst, l=vlast_real
                        ),
                    }
                ],
            }
        ]

    # ═════════════════════════════ Tasks sheet ═════════════════════
    task_rows: List[List[Any]] = []
    for i in range(tcount):
        r = tfirst + i
        if i < len(tasks):
            t = tasks[i]
            task_rows.append(
                [
                    t["task"],
                    t["owner"],
                    t["due_date"],
                    '=IF(C{r}="","",C{r}-TODAY())'.format(r=r),
                    "Yes" if t["done"] else "No",
                ]
            )
        else:
            task_rows.append(
                [None, None, None, '=IF(C{r}="","",C{r}-TODAY())'.format(r=r), None]
            )

    c0 = tlast + 3
    task_blocks: List[dict] = [
        {
            "cell": "A1",
            "text": "Tasks",
            "bold": True,
            "font_size": 12,
            "font_color": STEEL,
        }
    ]
    task_blocks.append(_section(c0, "Progress"))
    task_blocks += _pair(
        c0 + 1,
        "Tasks Complete",
        '=IF(COUNTA(A{f}:A{l})=0,"n/a",COUNTIF(E{f}:E{l},"Yes")/COUNTA(A{f}:A{l}))'.format(
            f=tfirst, l=tlast
        ),
        PCT_FMT,
    )
    task_blocks += _pair(
        c0 + 2,
        "Tasks Done",
        '=COUNTIF(E{f}:E{l},"Yes")'.format(f=tfirst, l=tlast),
        INT_FMT,
    )
    task_blocks += _pair(
        c0 + 3,
        "Overdue Tasks",
        '=COUNTIFS(C{f}:C{l},"<"&TODAY(),E{f}:E{l},"<>Yes")'.format(f=tfirst, l=tlast),
        INT_FMT,
    )

    tasks_sheet: Dict[str, Any] = {
        "name": "Tasks",
        "tab_color": STEEL,
        "freeze_panes": "A4",
        "column_widths": {"A": 32, "B": 16, "C": 12, "D": 14, "E": 9},
        "text_blocks": task_blocks,
        "tables": [
            {
                "start_cell": "A3",
                "headers": ["Task", "Owner", "Due Date", "Days Until Due", "Done"],
                "rows": task_rows,
                "number_formats": {"C": DATE_FMT, "D": INT_FMT},
                "alignments": {"C": "center", "D": "center", "E": "center"},
            }
        ],
        "data_validation": [
            {
                "range": f"E{tfirst}:E{tlast}",
                "values": ["Yes", "No"],
                "allow_blank": True,
            }
        ],
        "conditional_formats": [
            {
                "range": f"E{tfirst}:E{tlast}",
                "rules": [
                    {
                        "type": "cell_is",
                        "operator": "equal",
                        "value": "Yes",
                        "fill": CF_GREEN_FILL,
                        "font_color": CF_GREEN_TEXT,
                    }
                ],
            },
            {
                "range": f"D{tfirst}:D{tlast}",
                "rules": [
                    {
                        "type": "cell_is",
                        "operator": "less_than",
                        "value": 0,
                        "fill": CF_RED_FILL,
                        "font_color": CF_RED_TEXT,
                    },
                    {
                        "type": "cell_is",
                        "operator": "between",
                        "value": [0, 7],
                        "fill": CF_AMBER_FILL,
                        "font_color": CF_AMBER_TEXT,
                    },
                ],
            },
        ],
        "notes": (
            "Mark tasks done with the Yes/No dropdown — % Complete "
            "recalculates. Days Until Due is live; negative (red) means "
            "the due date has passed."
        ),
    }

    fname = "event_plan"
    safe = re.sub(r"[^\w\s-]", "", event_name)[:40].strip()
    safe = re.sub(r"[\s_-]+", "_", safe).strip("_")
    if safe:
        fname = f"{safe}_event_plan"

    return {
        "filename": f"{fname}.xlsx",
        "sheets": [event_sheet, guests_sheet, vendors_sheet, tasks_sheet],
    }


# ── Standard pattern entry points (used by the dynamic registry) ──────
coerce_params = coerce_event_planner_params
build_spec = build_event_planner_spec
