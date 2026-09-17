"""
Equipment maintenance pattern — service schedule with overdue alerts.

Deterministic template for a fleet / machine register:

  Equipment       Equipment | Location | Last Service | Interval (Days) |
                  Next Due | Days Until Due | Status | Responsible —
                  days-until = Next Due − TODAY() (live), status via a
                  nested IF (Overdue < 0 · Due Soon ≤ 30 · Scheduled),
                  red/amber/green conditional formatting. When the
                  request lists past services, a Total Spend column
                  SUMIFs the Service Log per machine.
  Service Log     one row per past service: Date | Equipment | Type |
                  Cost | Notes — with a total row (total maintenance
                  spend).

CALCULATION SEMANTICS:

  days until(r) = IF(next due blank, "", next due − TODAY())   [LIVE]
  status(r)     = IF(days until < 0, "Overdue",
                 IF(days until ≤ 30, "Due Soon", "Scheduled"))  [LIVE]
  spend(e)      = SUMIF(Log equipment, e, Log cost)             [LIVE]

One deliberately frozen value: Next Due defaults to Last Service +
Interval, computed once in Python when the file is created — it's an
editable INPUT default (the alternative would be a formula the user
overwrites anyway); the sheet notes say so. Everything downstream of it
stays live.

Every formula reference is computed from the actual layout rows this
module emits, so off-by-N row math is impossible by construction. No
ROUND() anywhere — display rounding is the number format's job.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

from app.services.patterns.utils import (
    _currency_fmt,
    _pick,
    to_int,
    to_iso_date,
    to_number,
)

# Registry key — must match the pattern stanza in
# prompts/pattern_classifier.md.
PATTERN_NAME = "equipment_maintenance"

PATTERN_DESCRIPTION = (
    "Maintenance / service schedule for equipment: last service, "
    "interval, next due, days-until and overdue alerts with status "
    "colors, plus an optional service log with costs and total spend. "
    "Use for servicing or inspecting machines, vehicles and appliances. "
    "Do not use it for tracking belongings or stock (inventory)."
)

# Routing keywords/stems — drive the cheap pre-gate and the classifier
# shortlist (see excel_gen._shortlist_patterns).
PATTERN_KEYWORDS = (
    "maintenance",
    "service",
    "serviced",
    "overhaul",
    "inspection",
    "equipment",
    "machine",
    "upkeep",
    "service schedule",
    "next service",
    "service due",
    "preventive maintenance",
    "calibrat",
)

MAX_EQUIPMENT = 40
MAX_LOG_ROWS = 200
MIN_LOG_ROWS = 10  # blank filler rows so a fresh log is ready for input

# Days until due that still counts as "Due Soon".
DUE_SOON_WINDOW = 30

# Extra types offered in the Service Log Type dropdown on top of the
# request's own words (deduped, comma-free).
DEFAULT_SERVICE_TYPES = ("Inspection", "Repair", "Preventive")

_LOG_SHEET = "Service Log"
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


def _compute_next_due(
    last_service: Optional[str], interval_days: Optional[int], stated: Optional[str]
) -> Optional[str]:
    """Next due = the user-stated date, else last service + interval.

    The computed value is an INPUT DEFAULT (frozen once here — noted on
    the sheet); days-until and status stay live formulas off it.
    """
    if stated is not None:
        return stated
    if last_service is not None and interval_days is not None:
        y, m, d = (int(x) for x in last_service.split("-"))
        due = date(y, m, d) + timedelta(days=interval_days)
        return due.isoformat()
    return None


def _normalize_equipment(entry: Any) -> Optional[dict]:
    """One equipment entry → normalized dict (next_due computed)."""
    if isinstance(entry, str):
        name = _clean_text(entry, 60)
        raw: Dict[str, Any] = {}
    elif isinstance(entry, dict):
        name = _clean_text(_pick(entry, "name", "equipment", "machine", "item"), 60)
        raw = entry
    else:
        return None
    if not name:
        return None

    last_service = to_iso_date(
        _pick(raw, "last_service", "last_serviced", "serviced", "last_service_date")
    )
    interval_days = to_int(
        _pick(
            raw,
            "interval_days",
            "interval",
            "service_interval",
            "service_interval_days",
        )
    )
    if interval_days is not None:
        interval_days = max(1, min(interval_days, 3650))
    stated_next = to_iso_date(
        _pick(raw, "next_service", "next_due", "next_service_date")
    )

    return {
        "name": name,
        "location": _clean_text(_pick(raw, "location", "site", "place"), 40),
        "last_service": last_service,
        "interval_days": interval_days,
        "next_due": _compute_next_due(last_service, interval_days, stated_next),
        "responsible": _clean_text(
            _pick(raw, "responsible", "owner", "technician", "assigned_to"), 40
        ),
    }


def _normalize_log_entry(entry: Any) -> Optional[dict]:
    """One service_log entry → {date, equipment, type, cost, notes}."""
    if not isinstance(entry, dict):
        return None
    equipment = _clean_text(_pick(entry, "equipment", "machine", "name", "item"), 60)
    entry_date = to_iso_date(_pick(entry, "date", "service_date"))
    if not equipment and entry_date is None:
        return None
    cost = to_number(_pick(entry, "cost", "amount", "price"))
    if cost is not None:
        cost = max(0.0, min(cost, 1e9))
    return {
        "date": entry_date,
        "equipment": equipment,
        "type": _clean_text(_pick(entry, "type", "service_type", "work"), 30),
        "cost": cost,
        "notes": _clean_text(_pick(entry, "notes", "note", "details"), 80),
    }


def coerce_equipment_maintenance_params(params: dict) -> dict:
    """Validate + normalize classifier params; raises ValueError."""
    if not isinstance(params, dict):
        raise ValueError("params must be an object")

    equipment: List[dict] = []
    seen: set = set()
    raw_equipment = _pick(params, "equipment", "machines", "items", "assets")
    if raw_equipment is not None:
        if not isinstance(raw_equipment, list):
            raise ValueError("equipment must be an array")
        for entry in raw_equipment[:MAX_EQUIPMENT]:
            normalized = _normalize_equipment(entry)
            if normalized is None:
                continue
            if normalized["name"].lower() in seen:
                continue
            seen.add(normalized["name"].lower())
            equipment.append(normalized)

    log: List[dict] = []
    raw_log = _pick(params, "service_log", "log", "services", "history")
    if raw_log is not None:
        if not isinstance(raw_log, list):
            raise ValueError("service_log must be an array")
        for entry in raw_log[:MAX_LOG_ROWS]:
            normalized = _normalize_log_entry(entry)
            if normalized is not None:
                log.append(normalized)

    # Machines named only in the service log still belong on the
    # register — their spend would otherwise vanish from every total.
    for entry in log:
        if not entry["equipment"]:
            continue
        if entry["equipment"].lower() in seen:
            continue
        if len(equipment) >= MAX_EQUIPMENT:
            break
        seen.add(entry["equipment"].lower())
        equipment.append(
            {
                "name": entry["equipment"],
                "location": None,
                "last_service": None,
                "interval_days": None,
                "next_due": None,
                "responsible": None,
            }
        )

    if not equipment:
        raise ValueError("equipment (the machines to maintain) required")

    # Dropdown list: the request's own service types + the standard
    # ones, comma/quote-free (commas corrupt Excel's list source).
    types: List[str] = []
    for entry in log:
        if entry["type"] and entry["type"] not in types:
            types.append(entry["type"])
    for extra in DEFAULT_SERVICE_TYPES:
        if extra not in types:
            types.append(extra)
    types = [t for t in types if "," not in t and '"' not in t][:10]

    notes = _pick(params, "notes", "note")
    notes = _clean_text(notes, 1000)

    return {
        "equipment": equipment,
        "service_log": log,
        "types": types,
        "currency": _currency_fmt(_pick(params, "currency")),
        "notes": notes,
    }


def build_equipment_maintenance_spec(params: dict) -> dict:
    """Equipment maintenance workbook — every formula code-generated.

    Layout (rows computed here, never guessed by a model):

    Equipment sheet:
      row 1      title · row 2 usage hint
      row 4      headers (start_cell A4, no table title)
      rows 5..   one row per machine; Days Until Due / Status are live
      row T      total row (service-log sheets only): total spend
    Service Log sheet (when the request lists past services):
      row 4      headers · rows 5.. one row per service + blank fillers
      row T      total maintenance spend
    """
    p = coerce_equipment_maintenance_params(params)
    equipment: List[dict] = p["equipment"]
    log: List[dict] = p["service_log"]
    types: List[str] = p["types"]
    cur = p["currency"]
    notes = p["notes"]

    money = cur or "#,##0.00"
    n = len(equipment)
    has_log = bool(log)
    n_log = max(len(log), MIN_LOG_ROWS) if has_log else 0

    # ── geometry (integers first — every formula is formatted from these)
    eq_r0 = 5  # first Equipment data row
    eq_rN = 4 + n  # last Equipment data row
    eq_total = eq_rN + 1  # noqa: Equipment total row (log only)
    log_r0 = 5  # first Log data row
    log_rN = 4 + n_log  # last Log data row
    log_total = log_rN + 1  # noqa: Log total row

    log_eq_col = f"'{_LOG_SHEET}'!$B${log_r0}:$B${log_rN}"
    log_cost_col = f"'{_LOG_SHEET}'!$D${log_r0}:$D${log_rN}"

    headers = [
        "Equipment",
        "Location",
        "Last Service",
        "Interval (Days)",
        "Next Due",
        "Days Until Due",
        "Status",
        "Responsible",
    ] + (["Total Spend"] if has_log else [])

    eq_rows: List[List[Any]] = []
    for i, item in enumerate(equipment):
        r = eq_r0 + i
        row: List[Any] = [
            item["name"],
            item["location"],
            item["last_service"],
            item["interval_days"],
            # Next Due = user-stated, or Last Service + Interval computed
            # once at build time as an editable input default (see notes)
            item["next_due"],
            # Days Until Due = Next Due − TODAY() (blank-safe)
            '=IF(E{r}="","",E{r}-TODAY())'.format(r=r),
            # Status: Overdue < 0 · Due Soon ≤ 30 · Scheduled otherwise
            '=IF(F{r}="","",IF(F{r}<0,"Overdue",IF(F{r}<={w},"Due Soon","Scheduled")))'.format(
                r=r, w=DUE_SOON_WINDOW
            ),
            item["responsible"],
        ]
        if has_log:
            row.append(
                # Total Spend = SUMIF over the service log per machine
                "=SUMIF({eq},$A{r},{cost})".format(
                    eq=log_eq_col, r=r, cost=log_cost_col
                )
            )
        eq_rows.append(row)

    total_row = None
    if has_log:
        total_row = ["Total", None, None, None, None, None, None, None] + [
            "=SUM(I{r0}:I{rN})".format(r0=eq_r0, rN=eq_rN)
        ]

    numfmts: Dict[str, str] = {
        "C": _DATE_FMT,
        "E": _DATE_FMT,
        "D": _DAYS_FMT,
        "F": _DAYS_FMT,
    }
    if has_log:
        numfmts["I"] = money

    charts: List[dict] = [
        {
            "type": "bar",
            "title": "Days Until Next Service",
            "anchor": "K3",
            "width": 15,
            "height": 9,
            "categories_range": "Equipment!$A${r0}:$A${rN}".format(r0=eq_r0, rN=eq_rN),
            "series": [
                {
                    "name": "Days Until Due",
                    "values_range": "Equipment!$F${r0}:$F${rN}".format(
                        r0=eq_r0, rN=eq_rN
                    ),
                }
            ],
            "value_numfmt": _DAYS_FMT,
        }
    ]
    if has_log and any(entry["cost"] is not None for entry in log):
        charts.append(
            {
                "type": "bar",
                "title": "Maintenance Spend by Equipment",
                "anchor": "K20",
                "width": 15,
                "height": 9,
                "categories_range": "Equipment!$A${r0}:$A${rN}".format(
                    r0=eq_r0, rN=eq_rN
                ),
                "series": [
                    {
                        "name": "Total Spend",
                        "values_range": "Equipment!$I${r0}:$I${rN}".format(
                            r0=eq_r0, rN=eq_rN
                        ),
                    }
                ],
                "value_numfmt": money,
            }
        )

    unlocked = ["A{r0}:E{rN}".format(r0=eq_r0, rN=eq_rN)]
    if n:
        unlocked.append("H{r0}:H{rN}".format(r0=eq_r0, rN=eq_rN))

    equipment_sheet: Dict[str, Any] = {
        "name": "Equipment",
        "tab_color": NAVY,
        "freeze_panes": "A5",
        "column_widths": {
            "A": 26,
            "B": 16,
            "C": 13,
            "D": 12,
            "E": 13,
            "F": 12,
            "G": 12,
            "H": 16,
            **({"I": 14} if has_log else {}),
        },
        "text_blocks": [
            {
                "cell": "A1",
                "text": "Equipment Maintenance Schedule",
                "bold": True,
                "font_size": 14,
                "font_color": NAVY,
            },
            {
                "cell": "A2",
                "text": (
                    "Days Until Due and Status update live — Overdue "
                    "(red) < 0 days, Due Soon (amber) within "
                    f"{DUE_SOON_WINDOW} days, Scheduled (green) later."
                ),
                "italic": True,
                "font_color": MUTED,
            },
        ],
        "tables": [
            {
                "start_cell": "A4",
                "headers": headers,
                "rows": eq_rows,
                "number_formats": numfmts,
                "alignments": {
                    "C": "center",
                    "D": "center",
                    "E": "center",
                    "F": "center",
                    "G": "center",
                    **({"I": "right"} if has_log else {}),
                },
                **({"total_row": total_row} if total_row else {}),
            }
        ],
        "charts": charts,
        "conditional_formats": [
            # status words: red / amber / green
            {
                "range": "G{r0}:G{rN}".format(r0=eq_r0, rN=eq_rN),
                "rules": [
                    {
                        "type": "cell_is",
                        "operator": "equal",
                        "value": "Overdue",
                        "fill": CF_RED_FILL,
                        "font_color": CF_RED_TEXT,
                        "bold": True,
                        "stop_if_true": True,
                    },
                    {
                        "type": "cell_is",
                        "operator": "equal",
                        "value": "Due Soon",
                        "fill": CF_AMBER_FILL,
                        "font_color": CF_AMBER_TEXT,
                        "stop_if_true": True,
                    },
                    {
                        "type": "cell_is",
                        "operator": "equal",
                        "value": "Scheduled",
                        "fill": CF_GREEN_FILL,
                        "font_color": CF_GREEN_TEXT,
                        "stop_if_true": False,
                    },
                ],
            },
            # immediate overdue flag on the day count itself
            {
                "range": "F{r0}:F{rN}".format(r0=eq_r0, rN=eq_rN),
                "rules": [
                    {
                        "type": "cell_is",
                        "operator": "less_than",
                        "value": 0,
                        "fill": CF_RED_FILL,
                        "font_color": CF_RED_TEXT,
                        "bold": True,
                        "stop_if_true": False,
                    }
                ],
            },
        ],
        "protect": {"unlocked_ranges": unlocked},
        "notes": notes
        or (
            "Next Due defaults to Last Service + Interval (computed once "
            "when this file was created — it's an editable input, so "
            "change it whenever the plan shifts). Days Until Due = Next "
            "Due − TODAY() and Status (Overdue / Due Soon within "
            f"{DUE_SOON_WINDOW} days / Scheduled) recalculate every time "
            "the file opens. Leave Interval or Next Due blank on machines "
            "you don't schedule yet — their row simply shows no countdown."
            + (" Total Spend sums the Service Log tab per machine." if has_log else "")
        ),
    }

    if not has_log:
        return {
            "filename": "equipment_maintenance.xlsx",
            "sheets": [equipment_sheet],
        }

    # ═════════════════════════════ Service Log sheet ═════════════════
    log_rows: List[List[Any]] = []
    for j in range(n_log):
        entry = log[j] if j < len(log) else None
        log_rows.append(
            [
                entry["date"] if entry else None,
                entry["equipment"] if entry else None,
                entry["type"] if entry else None,
                entry["cost"] if entry else None,
                entry["notes"] if entry else None,
            ]
        )

    log_sheet: Dict[str, Any] = {
        "name": _LOG_SHEET,
        "tab_color": STEEL,
        "freeze_panes": "A5",
        "column_widths": {"A": 13, "B": 26, "C": 16, "D": 13, "E": 34},
        "text_blocks": [
            {
                "cell": "A1",
                "text": "Service Log",
                "bold": True,
                "font_size": 14,
                "font_color": NAVY,
            },
            {
                "cell": "A2",
                "text": (
                    "One row per past service — the Equipment tab sums "
                    "Cost per machine and the total row shows overall "
                    "maintenance spend."
                ),
                "italic": True,
                "font_color": MUTED,
            },
        ],
        "tables": [
            {
                "start_cell": "A4",
                "headers": ["Date", "Equipment", "Type", "Cost", "Notes"],
                "rows": log_rows,
                "number_formats": {"A": _DATE_FMT, "D": money},
                "alignments": {"A": "center", "C": "center", "D": "right"},
                "total_row": [
                    "Total",
                    None,
                    None,
                    "=SUM(D{r0}:D{rN})".format(r0=log_r0, rN=log_rN),
                    None,
                ],
            }
        ],
        "data_validation": [
            {
                # live range source: machines added on Equipment appear
                # in this dropdown automatically
                "range": "B{r0}:B{rN}".format(r0=log_r0, rN=log_rN),
                "source_range": "Equipment!$A${r0}:$A${rN}".format(r0=eq_r0, rN=eq_rN),
                "allow_blank": True,
                "prompt_title": "Equipment",
                "prompt": "Pick the machine that was serviced.",
                "error_title": "Not on the Equipment tab",
                "error": (
                    "Add the machine on the Equipment tab first — the "
                    "dropdown picks it up automatically."
                ),
                "error_style": "warning",
            },
            {
                "range": "C{r0}:C{rN}".format(r0=log_r0, rN=log_rN),
                "values": types,
                "allow_blank": True,
                "prompt_title": "Service type",
                "prompt": "Inspection, Repair, Preventive — or the request's own words.",
                "error_style": "warning",
            },
        ],
        "protect": {"unlocked_ranges": ["A{r0}:E{rN}".format(r0=log_r0, rN=log_rN)]},
        "notes": (
            "One row per service with its cost — blank rows are ready "
            "for the next entries (Cost sums only when filled). The "
            "Equipment tab's Total Spend column SUMIFs this log per "
            "machine; the total row here is the overall maintenance "
            "spend. Equipment names come live from the Equipment tab; "
            "Type is a dropdown."
        ),
    }

    return {
        "filename": "equipment_maintenance.xlsx",
        "sheets": [equipment_sheet, log_sheet],
    }


# ── Standard pattern entry points (used by the dynamic registry) ──────

coerce_params = coerce_equipment_maintenance_params
build_spec = build_equipment_maintenance_spec
