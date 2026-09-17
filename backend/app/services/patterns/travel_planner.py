"""
Travel planner pattern — ONE trip: day-by-day itinerary, bookings
(flights, hotels, transport) with costs, and a trip cost summary.

Three sheets:

  Trip        (navy)   header block — trip name / destination / start
                      & end dates / trip length (=end-start+1) /
                      travelers / countdown (=start-TODAY()) — plus a
                      live Cost Summary: total bookings =SUM, total
                      activities est. =SUM, total trip cost, paid to
                      date =SUMIF(paid,"Yes",cost), outstanding =
                      total bookings - paid, per-traveler share =
                      total / travelers guarded by IF
  Itinerary   (steel) Day | Date | Time | Activity | Estimated Cost
                      with a SUM total row and a day-by-day estimated
                      cost bar chart (when activities carry costs)
  Bookings    (steel) Type | Provider | Date | Cost | Status | Paid
                      ("Yes"/"No") with a SUM total row, status colors
                      and dropdowns

Lists only ever contain what the request gives (never invented); thin
blank scaffold rows pad the tables to MIN_ROWS so the totals, dropdowns
and the chart keep working as the user adds rows. Travelers defaults
to 1; paid is "Yes" only when stated; estimated_cost only when stated.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from app.services.patterns.utils import (
    MONEY_FMT,
    _currency_fmt,
    _pick,
    to_int,
    to_iso_date,
    to_number,
)

# Registry key — must match the pattern stanza in
# prompts/pattern_classifier.md.
PATTERN_NAME = "travel_planner"

PATTERN_DESCRIPTION = (
    "One-trip planner: day-by-day itinerary with estimated costs, "
    "bookings (flights, hotels…) with paid/outstanding tracking, live "
    "trip cost summary with per-traveler share, countdown and a daily "
    "cost bar chart."
)

# Routing keywords/stems — drive the cheap pre-gate and the classifier
# shortlist (see excel_gen._shortlist_patterns).
PATTERN_KEYWORDS = (
    "trip",
    "itinerary",
    "travel",
    "vacation",
    "flight",
    "hotel",
    "booking",
    "sightseeing",
    "excursion",
    "holiday",
    "getaway",
)

MAX_BOOKINGS = 100
MAX_ACTIVITIES = 200
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

BOOKING_STATUS_OPTIONS = ("Confirmed", "Pending", "Cancelled")


def _clean_text(value: Any, limit: int) -> Optional[str]:
    if value is None:
        return None
    s = str(value).strip()
    return s[:limit] or None


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("yes", "y", "true", "t", "1")
    if isinstance(value, (int, float)):
        return bool(value)
    return False


def _normalize_booking(entry: Any) -> Optional[dict]:
    if not isinstance(entry, dict):
        return None
    btype = _clean_text(_pick(entry, "type", "kind", "category", "service"), 60)
    provider = _clean_text(
        _pick(entry, "provider", "name", "company", "vendor", "airline", "agency"), 80
    )
    if not btype and not provider:
        return None
    cost = to_number(_pick(entry, "cost", "price", "amount", "total"))
    status = _clean_text(_pick(entry, "status", "state"), 40)
    return {
        "type": btype,
        "provider": provider,
        "date": to_iso_date(_pick(entry, "date", "booking_date")),
        "cost": cost or 0.0,
        "status": status,
        "paid": _truthy(_pick(entry, "paid", "is_paid")),  # true only when stated
    }


def _normalize_activity(entry: Any) -> Optional[dict]:
    if isinstance(entry, str):
        activity = entry.strip()
        if not activity:
            return None
        return {
            "day": None,
            "date": None,
            "time": None,
            "activity": activity[:200],
            "estimated_cost": None,  # only when stated
        }
    if not isinstance(entry, dict):
        return None
    activity = _clean_text(
        _pick(entry, "activity", "name", "description", "title", "what"), 200
    )
    if not activity:
        return None
    return {
        "day": to_int(_pick(entry, "day", "day_number", "day_no")),
        "date": to_iso_date(_pick(entry, "date")),
        "time": _clean_text(_pick(entry, "time", "start_time"), 20),
        "activity": activity,
        "estimated_cost": to_number(
            _pick(entry, "estimated_cost", "est_cost", "cost", "price")
        ),
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


def coerce_travel_planner_params(params: dict) -> dict:
    """Validate + normalize classifier params; raises ValueError.

    Required: at least one booking or activity (a trip with nothing
    to plan can't be templated — the caller falls back to the AI path).
    Travelers defaults to 1; dates/name/destination optional.
    """
    if not isinstance(params, dict):
        raise ValueError("params must be an object")

    trip_name = _clean_text(_pick(params, "trip_name", "name", "title"), 80)
    destination = _clean_text(
        _pick(params, "destination", "city", "place", "location"), 80
    )
    start_date = to_iso_date(
        _pick(params, "start_date", "start", "begins", "departure", "departing")
    )
    end_date = to_iso_date(
        _pick(params, "end_date", "end", "return", "returning", "finish")
    )

    travelers = to_int(
        _pick(
            params,
            "travelers",
            "travellers",
            "people",
            "party",
            "party_size",
            "group_size",
        )
    )
    if travelers is None:
        travelers = 1  # stanza default
    travelers = min(max(travelers, 1), 99)

    currency = _clean_text(_pick(params, "currency"), 10)

    bookings = _normalize_list(
        _pick(params, "bookings", "booking_list"),
        _normalize_booking,
        MAX_BOOKINGS,
        "bookings",
    )
    activities = _normalize_list(
        _pick(params, "activities", "activity_list", "itinerary"),
        _normalize_activity,
        MAX_ACTIVITIES,
        "activities",
    )
    if not bookings and not activities:
        raise ValueError("no bookings or activities — nothing to plan")

    notes = _pick(params, "notes", "note")
    notes = notes.strip()[:1000] if isinstance(notes, str) and notes.strip() else None

    return {
        "trip_name": trip_name,
        "destination": destination,
        "start_date": start_date,
        "end_date": end_date,
        "travelers": travelers,
        "currency": currency,
        "bookings": bookings,
        "activities": activities,
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


def build_travel_planner_spec(params: dict) -> dict:
    """Travel planner workbook — every formula code-generated.

    Layout (rows computed here, never guessed by a model):

    Trip sheet:
      rows 4..     header block (name/destination/dates/length/
                   travelers/countdown)
      rows +2..    Cost Summary (SUM / SUMIF / guarded division)
    Itinerary sheet:
      row 3 header, rows 4.. table (padded to MIN_ROWS), SUM total
      row, day-cost bar chart anchored G3
    Bookings sheet:
      row 3 header, rows 4.. table, SUM total row, status dropdowns
    """
    p = coerce_travel_planner_params(params)
    trip_name = p["trip_name"]
    destination = p["destination"]
    start_date = p["start_date"]
    end_date = p["end_date"]
    travelers: int = p["travelers"]
    currency = p["currency"]
    bookings: List[dict] = p["bookings"]
    activities: List[dict] = p["activities"]
    notes = p["notes"]

    money = _currency_fmt(currency) or MONEY_FMT

    # ── list-sheet geometry (tables start at A3 → header row 3) ─────
    bcount = max(len(bookings), MIN_ROWS)
    bfirst = 4
    blast = bfirst + bcount - 1

    icount = max(len(activities), MIN_ROWS)
    ifirst = 4
    ilast = ifirst + icount - 1

    # ═════════════════════════════ Trip sheet ══════════════════════
    title = trip_name or (f"{destination} Trip" if destination else "Trip Plan")

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
                "The countdown and the cost summary are live — edit the "
                "Itinerary and Bookings sheets and every total here "
                "recalculates, including the per-traveler share."
            ),
            "italic": True,
            "font_color": MUTED,
        },
    ]

    row = 4
    name_row = dest_row = start_row = end_row = length_row = countdown_row = None
    if trip_name:
        name_row = row
        row += 1
    if destination:
        dest_row = row
        row += 1
    if start_date:
        start_row = row
        row += 1
    if end_date:
        end_row = row
        row += 1
    if start_date and end_date:
        length_row = row
        row += 1
    travelers_row = row
    row += 1
    if start_date:
        countdown_row = row
        row += 1

    if name_row is not None:
        blocks += _pair(name_row, "Trip Name", trip_name)
    if dest_row is not None:
        blocks += _pair(dest_row, "Destination", destination)
    if start_row is not None:
        blocks += _pair(start_row, "Start Date", start_date, DATE_FMT)
    if end_row is not None:
        blocks += _pair(end_row, "End Date", end_date, DATE_FMT)
    if length_row is not None:
        blocks += _pair(
            length_row,
            "Trip Length (days)",
            '=IF(OR($B${s}="",$B${e}=""),"n/a",$B${e}-$B${s}+1)'.format(
                s=start_row, e=end_row
            ),
            INT_FMT,
        )
    blocks += _pair(travelers_row, "Travelers", travelers, INT_FMT)
    if countdown_row is not None:
        blocks += _pair(
            countdown_row,
            "Countdown (Days Until Trip)",
            "=$B${s}-TODAY()".format(s=start_row),
            INT_FMT,
        )

    # Cost Summary — all live formulas over the rendered rows
    cs0 = row + 1  # one blank row after the header block
    r_book = cs0 + 1
    r_act = cs0 + 2
    r_total = cs0 + 3
    r_paid = cs0 + 4
    r_out = cs0 + 5
    r_share = cs0 + 6

    blocks.append(_section(cs0, "Cost Summary"))
    blocks += _pair(
        r_book,
        "Total Bookings",
        "=SUM(Bookings!$D${f}:$D${l})".format(f=bfirst, l=blast),
        money,
    )
    blocks += _pair(
        r_act,
        "Total Activities Est.",
        "=SUM(Itinerary!$E${f}:$E${l})".format(f=ifirst, l=ilast),
        money,
    )
    blocks += _pair(
        r_total,
        "Total Trip Cost",
        "=B{a}+B{b}".format(a=r_book, b=r_act),
        money,
    )
    blocks += _pair(
        r_paid,
        "Paid To Date",
        '=SUMIF(Bookings!$F${f}:$F${l},"Yes",Bookings!$D${f}:$D${l})'.format(
            f=bfirst, l=blast
        ),
        money,
    )
    blocks += _pair(
        r_out,
        "Outstanding",
        "=B{a}-B{b}".format(a=r_book, b=r_paid),
        money,
    )
    blocks += _pair(
        r_share,
        "Per-Traveler Share",
        '=IF($B${t}=0,"n/a",B{c}/$B${t})'.format(t=travelers_row, c=r_total),
        money,
    )

    trip_sheet: Dict[str, Any] = {
        "name": "Trip",
        "tab_color": NAVY,
        "column_widths": {"A": 26, "B": 20},
        "text_blocks": blocks,
        "notes": notes
        or (
            "One trip per workbook. The Itinerary sheet plans the days, "
            "the Bookings sheet tracks what you paid; the countdown and "
            "every cost figure here recalculate live. Per-Traveler Share "
            "divides the total trip cost by the Travelers count."
        ),
    }

    # ═════════════════════════════ Itinerary sheet ═════════════════
    itinerary_rows: List[List[Any]] = []
    for i in range(icount):
        if i < len(activities):
            a = activities[i]
            itinerary_rows.append(
                [a["day"], a["date"], a["time"], a["activity"], a["estimated_cost"]]
            )
        else:
            itinerary_rows.append([None, None, None, None, None])

    itinerary_sheet: Dict[str, Any] = {
        "name": "Itinerary",
        "tab_color": STEEL,
        "freeze_panes": "A4",
        "column_widths": {"A": 8, "B": 12, "C": 10, "D": 36, "E": 16},
        "text_blocks": [
            {
                "cell": "A1",
                "text": "Itinerary",
                "bold": True,
                "font_size": 12,
                "font_color": STEEL,
            }
        ],
        "tables": [
            {
                "start_cell": "A3",
                "headers": ["Day", "Date", "Time", "Activity", "Estimated Cost"],
                "rows": itinerary_rows,
                "number_formats": {"A": INT_FMT, "B": DATE_FMT, "E": money},
                "alignments": {"A": "center", "B": "center", "C": "center"},
                "total_row": [
                    "Total",
                    "",
                    "",
                    "",
                    "=SUM(E{first_row}:E{last_row})",
                ],
            }
        ],
        "notes": (
            "One row per planned activity — Day is the trip day number, "
            "Estimated Cost only when you know it. The total row and the "
            "Trip sheet's activity estimate stay live."
        ),
    }

    # Day-by-day estimated cost bar chart — only when activities carry
    # costs; categories prefer day numbers, else dates, else names.
    n_real = len(activities)
    if n_real and any(a["estimated_cost"] is not None for a in activities):
        last_real = ifirst + n_real - 1
        if any(a["day"] is not None for a in activities):
            cats = "Itinerary!A{f}:A{l}".format(f=ifirst, l=last_real)
        elif any(a["date"] for a in activities):
            cats = "Itinerary!B{f}:B{l}".format(f=ifirst, l=last_real)
        else:
            cats = "Itinerary!D{f}:D{l}".format(f=ifirst, l=last_real)
        itinerary_sheet["charts"] = [
            {
                "type": "bar",
                "title": "Estimated Cost by Day",
                "anchor": "G3",
                "width": 14,
                "height": 9,
                "categories_range": cats,
                "series": [
                    {
                        "name": "Estimated Cost",
                        "values_range": "Itinerary!E{f}:E{l}".format(
                            f=ifirst, l=last_real
                        ),
                    }
                ],
                "value_numfmt": money,
            }
        ]

    # ═════════════════════════════ Bookings sheet ══════════════════
    booking_rows: List[List[Any]] = []
    for i in range(bcount):
        if i < len(bookings):
            b = bookings[i]
            booking_rows.append(
                [
                    b["type"],
                    b["provider"],
                    b["date"],
                    b["cost"],
                    b["status"],
                    "Yes" if b["paid"] else "No",
                ]
            )
        else:
            booking_rows.append([None, None, None, None, None, None])

    bookings_sheet: Dict[str, Any] = {
        "name": "Bookings",
        "tab_color": STEEL,
        "freeze_panes": "A4",
        "column_widths": {"A": 14, "B": 24, "C": 12, "D": 14, "E": 13, "F": 9},
        "text_blocks": [
            {
                "cell": "A1",
                "text": "Bookings",
                "bold": True,
                "font_size": 12,
                "font_color": STEEL,
            }
        ],
        "tables": [
            {
                "start_cell": "A3",
                "headers": ["Type", "Provider", "Date", "Cost", "Status", "Paid"],
                "rows": booking_rows,
                "number_formats": {"C": DATE_FMT, "D": money},
                "alignments": {"C": "center", "E": "center", "F": "center"},
                "total_row": [
                    "Total",
                    "",
                    "",
                    "=SUM(D{first_row}:D{last_row})",
                    "",
                    "",
                ],
            }
        ],
        "data_validation": [
            {
                # warning style: the request may carry its own status word
                "range": f"E{bfirst}:E{blast}",
                "values": list(BOOKING_STATUS_OPTIONS),
                "allow_blank": True,
                "error_style": "warning",
            },
            {
                "range": f"F{bfirst}:F{blast}",
                "values": ["Yes", "No"],
                "allow_blank": True,
            },
        ],
        "conditional_formats": [
            {
                "range": f"E{bfirst}:E{blast}",
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
            {
                "range": f"F{bfirst}:F{blast}",
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
        ],
        "notes": (
            "One row per flight/hotel/train/car rental — flip Paid to Yes "
            "when settled and the Trip sheet's Paid/Outstanding figures "
            "update. Status colors: Confirmed green, Pending amber, "
            "Cancelled red."
        ),
    }

    fname = "trip_planner"
    base = trip_name or destination or ""
    if base:
        safe = re.sub(r"[^\w\s-]", "", base)[:40].strip()
        safe = re.sub(r"[\s_-]+", "_", safe).strip("_")
        if safe:
            fname = f"{safe}_trip"
    else:
        fname = "travel_planner"

    return {
        "filename": f"{fname}.xlsx",
        "sheets": [trip_sheet, itinerary_sheet, bookings_sheet],
    }


# ── Standard pattern entry points (used by the dynamic registry) ──────
coerce_params = coerce_travel_planner_params
build_spec = build_travel_planner_spec
