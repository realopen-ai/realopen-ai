"""
KPI report pattern — target vs actual scorecard (deterministic).

Creates a KPI / metrics scorecard workbook:

  Scorecard     report title + live summary (KPIs on track / on watch
                / missed via COUNTIF over the status columns, average
                attainment via AVERAGE) + one table per KPI UNIT
                (currency / percent / days / number) so every table
                carries truly unit-aware number formats:

                KPI | Category | Target | Actual | Variance |
                Variance % | Attainment | Status | Better When

                — Variance = Actual - Target; Variance % and
                Attainment are guarded divisions (target/actual zero →
                "n/a"); Attainment is direction-aware (higher-is-better
                → Actual/Target, lower-is-better → Target/Actual); the
                Status word is a nested IF chain (On Track at ≥100%,
                Watch at ≥90%, else Miss) with green/amber/red
                conditional formatting; a clustered Target-vs-Actual
                bar chart covers the largest unit group.

Percent-unit KPIs are stored as decimals (94% → 0.94) so the percent
number format and the attainment math agree. A KPI missing target or
actual is omitted (the stanza's contract — never invent numbers).

Every formula reference is computed from the actual layout rows this
module emits, so off-by-N row math is impossible by construction.
No ROUND() anywhere — display rounding is the number format's job.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.services.patterns.utils import (
    MONEY_FMT,
    PCT_FMT,
    QTY_FMT,
    _currency_fmt,
    _pick,
    to_number,
    to_rate,
)

# Registry key — must match the pattern stanza in
# prompts/pattern_classifier.md.
PATTERN_NAME = "kpi_report"

PATTERN_DESCRIPTION = (
    "Creates a KPI scorecard spreadsheet: target vs actual per metric "
    "with variance, variance %, direction-aware attainment, and an "
    "On Track / Watch / Miss status with green/amber/red coloring, "
    "unit-aware number formats and a target-vs-actual chart. Use for "
    "target-vs-actual performance reports ('Q3 KPIs vs targets', "
    "'metrics scorecard'). Do NOT use it for sales transaction logs "
    "or deal pipelines."
)

# Routing keywords/stems — drive the cheap pre-gate and the classifier
# shortlist (see excel_gen._shortlist_patterns).
PATTERN_KEYWORDS = (
    "kpi",
    "metric",
    "scorecard",
    "variance",
    "attainment",
    "target vs actual",
    "performance report",
    "okr",
    "target",
)

MAX_KPIS = 100

# number formats per unit (currency resolves at build time so the
# request's currency code can bake its symbol in)
UNIT_FORMATS = {
    "number": QTY_FMT,  # "#,##0.##" — ints clean, decimals to 2dp
    "percent": PCT_FMT,  # "0.00%"
    "days": "#,##0",
}
UNIT_TITLES = {
    "currency": "Currency KPIs",
    "percent": "Percentage KPIs",
    "days": "Duration KPIs (days)",
    "number": "Number KPIs",
}

STATUS_ON_TRACK = "On Track"
STATUS_WATCH = "Watch"
STATUS_MISS = "Miss"
STATUS_NA = "n/a"

_STATUS_CF_RULES = [
    {
        "type": "cell_is",
        "operator": "equal",
        "value": STATUS_ON_TRACK,
        "fill": "C6EFCE",
        "font_color": "1E4620",
    },
    {
        "type": "cell_is",
        "operator": "equal",
        "value": STATUS_WATCH,
        "fill": "FFF2CC",
        "font_color": "7F6000",
    },
    {
        "type": "cell_is",
        "operator": "equal",
        "value": STATUS_MISS,
        "fill": "FFC7CE",
        "font_color": "9C0006",
    },
]


def _clean_text(value: Any, limit: int = 200) -> Optional[str]:
    if value is None:
        return None
    s = str(value).strip()
    return s[:limit] if s else None


def _normalize_unit(value: Any) -> str:
    """Loose unit words → the four canonical units (default number)."""
    s = str(value or "").strip().lower()
    if s in ("percent", "percentage", "%", "pct", "rate", "ratio", "share"):
        return "percent"
    if s in ("currency", "money", "usd", "amount", "monetary", "dollar", "euro"):
        return "currency"
    if s in ("days", "day", "duration", "d", "hours", "hrs"):
        return "days"
    return "number"


def _normalize_direction(value: Any) -> bool:
    """→ True when higher is better (the documented default)."""
    if value is None:
        return True
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    s = str(value).strip().lower()
    if s in (
        "lower",
        "lower_is_better",
        "lower is better",
        "false",
        "no",
        "down",
        "minimize",
        "less",
        "minimise",
    ):
        return False
    if s in (
        "higher",
        "higher_is_better",
        "higher is better",
        "true",
        "yes",
        "up",
        "maximize",
        "more",
        "maximise",
    ):
        return True
    return True


# ── Param coercion ────────────────────────────────────────────────────


def _normalize_kpi(entry: Any) -> Optional[dict]:
    """One KPI entry → {name, category, unit, target, actual,
    higher_is_better}, or None when unusable.

    name is required; a KPI missing target OR actual is omitted (the
    stanza's contract); percent-unit values are normalized to decimals
    (94 / "94%" / 0.94 all land at 0.94).
    """
    if not isinstance(entry, dict):
        return None

    name = _clean_text(
        _pick(entry, "name", "kpi", "kpi_name", "metric", "metric_name", "title")
    )
    if name is None:
        return None

    target = to_number(_pick(entry, "target", "target_value", "goal", "plan"))
    actual = to_number(_pick(entry, "actual", "actual_value", "current", "result"))
    if target is None or actual is None:
        return None  # never invent a KPI side

    unit = _normalize_unit(_pick(entry, "unit", "units", "measure", "uom"))
    if unit == "percent":
        target = to_rate(target)
        actual = to_rate(actual)
        if target is None or actual is None:
            return None

    return {
        "name": name,
        "category": _clean_text(_pick(entry, "category", "group", "area")),
        "unit": unit,
        "target": float(target),
        "actual": float(actual),
        "higher_is_better": _normalize_direction(
            _pick(entry, "higher_is_better", "direction", "better", "better_is")
        ),
    }


def coerce_kpi_report_params(params: dict) -> dict:
    """Validate + normalize classifier params; raises ValueError."""
    if not isinstance(params, dict):
        raise ValueError("params must be an object")

    raw = None
    for key in ("kpis", "metrics", "kpis_list", "items", "entries", "rows", "measures"):
        value = params.get(key)
        if value is None:
            continue
        if not isinstance(value, list):
            raise ValueError("kpis must be an array")
        raw = value
        break

    kpis: List[dict] = []
    if raw is not None:
        for entry in raw[:MAX_KPIS]:
            normalized = _normalize_kpi(entry)
            if normalized is not None:
                kpis.append(normalized)

    if not kpis:
        raise ValueError("no usable KPIs provided")

    report_name = _pick(params, "report_name", "name", "title")
    report_name = _clean_text(report_name, 100) or "KPI Scorecard"

    period = _pick(params, "period", "period_label", "quarter", "timeframe")
    period = _clean_text(period, 100)

    notes = _pick(params, "notes", "note")
    notes = notes.strip()[:1000] if isinstance(notes, str) and notes.strip() else None

    return {
        "report_name": report_name,
        "period": period,
        "currency": _currency_fmt(_pick(params, "currency")),
        "kpis": kpis,
        "notes": notes,
    }


# ── Builder ───────────────────────────────────────────────────────────


def build_kpi_report_spec(params: dict) -> dict:
    """KPI scorecard workbook — every formula code-generated.

    Layout (rows computed here, never guessed by a model):

      row 1        title text block
      rows 3..     live summary text blocks (COUNTIF status counts +
                   guarded AVERAGE attainment over the tables below)
      then         one table per KPI unit group (title row, header
                   row, data rows) — Variance, Variance %, Attainment
                   and Status are live formulas per row; the Status
                   column carries the green/amber/red conditional
                   formats; a Target-vs-Actual clustered bar chart
                   floats beside the tables.
    """
    p = coerce_kpi_report_params(params)
    kpis: List[dict] = p["kpis"]
    money = p["currency"] or MONEY_FMT

    def unit_fmt(unit: str) -> str:
        return money if unit == "currency" else UNIT_FORMATS[unit]

    # Group by unit, preserving the units' first-appearance order —
    # each table then carries ONE unit's number formats end to end.
    groups: Dict[str, List[dict]] = {}
    for k in kpis:
        groups.setdefault(k["unit"], []).append(k)

    has_category = any(k["category"] for k in kpis)

    headers = (["KPI", "Category"] if has_category else ["KPI"]) + [
        "Target",
        "Actual",
        "Variance",
        "Variance %",
        "Attainment",
        "Status",
        "Better When",
    ]
    # column letters (the table always starts in column A)
    c_target = "C" if has_category else "B"
    c_actual = "D" if has_category else "C"
    c_var = "E" if has_category else "D"
    c_varpct = "F" if has_category else "E"
    c_att = "G" if has_category else "F"
    c_status = "H" if has_category else "G"

    # ── summary text blocks (cells decided after the table geometry,
    #    so the COUNTIF/AVERAGE ranges below are real layout rows) ────
    blocks: List[dict] = [
        {"cell": "A1", "text": p["report_name"], "bold": True, "font_size": 14},
    ]
    summary_row = 3
    if p["period"]:
        blocks += [
            {"cell": "A3", "text": "Period"},
            {"cell": "B3", "text": p["period"]},
        ]
        summary_row = 4
    summary_end = summary_row + 3  # four summary lines

    # ── one table per unit group ──────────────────────────────────────
    tables: List[dict] = []
    conditional_formats: List[dict] = []
    status_ranges: List[str] = []
    attainment_ranges: List[str] = []
    geo: Dict[str, tuple] = {}

    anchor = summary_end + 2
    for unit, group in groups.items():
        first_r = anchor + 2  # title row anchor, header anchor+1
        last_r = first_r + len(group) - 1
        geo[unit] = (first_r, last_r)

        fmt = unit_fmt(unit)
        rows: List[List[Any]] = []
        for i, k in enumerate(group):
            r = first_r + i
            row_vals: List[Any] = [k["name"]]
            if has_category:
                row_vals.append(k["category"])
            row_vals += [
                k["target"],
                k["actual"],
                f"={c_actual}{r}-{c_target}{r}",  # Variance = Actual - Target
                f'=IF({c_target}{r}=0,"{STATUS_NA}",{c_var}{r}/{c_target}{r})',
            ]
            if k["higher_is_better"]:
                # attainment = actual / target (guarded)
                row_vals.append(
                    f'=IF({c_target}{r}=0,"{STATUS_NA}",'
                    f"{c_actual}{r}/{c_target}{r})"
                )
            else:
                # lower is better: attainment = target / actual (guarded
                # on the ACTUAL side — an actual of 0 is perfect but
                # unratiable)
                row_vals.append(
                    f'=IF({c_actual}{r}=0,"{STATUS_NA}",'
                    f"{c_target}{r}/{c_actual}{r})"
                )
            row_vals.append(
                f'=IF({c_att}{r}="{STATUS_NA}","{STATUS_NA}",'
                f'IF({c_att}{r}>=1,"{STATUS_ON_TRACK}",'
                f'IF({c_att}{r}>=0.9,"{STATUS_WATCH}","{STATUS_MISS}")))'
            )
            row_vals.append("Higher" if k["higher_is_better"] else "Lower")
            rows.append(row_vals)

        status_range = f"{c_status}{first_r}:{c_status}{last_r}"
        status_ranges.append(status_range)
        attainment_ranges.append(f"{c_att}{first_r}:{c_att}{last_r}")
        conditional_formats.append({"range": status_range, "rules": _STATUS_CF_RULES})

        tables.append(
            {
                "start_cell": f"A{anchor}",
                "title": UNIT_TITLES[unit],
                "headers": headers,
                "rows": rows,
                "number_formats": {
                    c_target: fmt,
                    c_actual: fmt,
                    c_var: fmt,
                    c_varpct: PCT_FMT,
                    c_att: PCT_FMT,
                },
            }
        )
        anchor = last_r + 3  # one blank row between tables

    # summary formulas over the REAL status/attainment ranges above
    def _countif(word: str) -> str:
        return "+".join(f'COUNTIF({rng},"{word}")' for rng in status_ranges)

    att_join = ",".join(attainment_ranges)

    blocks += [
        {"cell": f"A{summary_row}", "text": f"KPIs {STATUS_ON_TRACK}"},
        {
            "cell": f"B{summary_row}",
            "text": f"={_countif(STATUS_ON_TRACK)}",
            "number_format": "#,##0",
        },
        {"cell": f"A{summary_row + 1}", "text": f"KPIs on {STATUS_WATCH}"},
        {
            "cell": f"B{summary_row + 1}",
            "text": f"={_countif(STATUS_WATCH)}",
            "number_format": "#,##0",
        },
        {"cell": f"A{summary_row + 2}", "text": "KPIs Missed"},
        {
            "cell": f"B{summary_row + 2}",
            "text": f"={_countif(STATUS_MISS)}",
            "number_format": "#,##0",
        },
        {"cell": f"A{summary_row + 3}", "text": "Average Attainment"},
        {
            "cell": f"B{summary_row + 3}",
            "text": f'=IFERROR(AVERAGE({att_join}),"{STATUS_NA}")',
            "number_format": PCT_FMT,
            "bold": True,
        },
    ]

    # ── chart: target vs actual clustered bars for the largest group ─
    chart_unit = max(groups, key=lambda u: len(groups[u]))
    chart_first, chart_last = geo[chart_unit]
    chart_anchor_col = chr(ord("A") + len(headers) + 1)  # one col of air
    charts = [
        {
            "type": "bar",
            "title": f"Target vs Actual — {UNIT_TITLES[chart_unit]}",
            "anchor": f"{chart_anchor_col}3",
            "width": 16,
            "height": 10,
            "categories_range": f"Scorecard!$A${chart_first}:$A${chart_last}",
            "series": [
                {
                    "name": "Target",
                    "values_range": (
                        f"Scorecard!${c_target}${chart_first}:${c_target}${chart_last}"
                    ),
                },
                {
                    "name": "Actual",
                    "values_range": (
                        f"Scorecard!${c_actual}${chart_first}:${c_actual}${chart_last}"
                    ),
                },
            ],
            "value_numfmt": unit_fmt(chart_unit),
        }
    ]

    sheet_notes = (
        "Variance = Actual - Target. Variance % = Variance / Target and "
        "Attainment = Actual / Target for higher-is-better KPIs, or "
        "Target / Actual for lower-is-better KPIs (costs, churn, "
        "downtime…) — both guarded, a zero denominator shows '"
        + STATUS_NA
        + "'. Status: On Track at 100% attainment or better, Watch from "
        "90%, otherwise Miss — green / amber / red. Percent KPIs are "
        "stored as decimals (94% = 0.94). KPIs are grouped by unit so "
        "each table's number formats match its measures; a KPI missing "
        "a target or an actual is omitted by design. The Better When "
        "column shows which direction counts as good for that KPI."
    )
    if p["notes"]:
        sheet_notes = f"{p['notes']}\n{sheet_notes}"

    column_widths: Dict[str, Any] = {"A": 26}
    if has_category:
        column_widths["B"] = 18
    column_widths.update(
        {
            c_target: 14,
            c_actual: 14,
            c_var: 14,
            c_varpct: 12,
            c_att: 12,
            c_status: 12,
            "I" if has_category else "H": 13,
        }
    )

    scorecard_sheet: Dict[str, Any] = {
        "name": "Scorecard",
        "tab_color": "16304F",
        "freeze_panes": "A2",  # keep the report title pinned
        "column_widths": column_widths,
        "text_blocks": blocks,
        "tables": tables,
        "charts": charts,
        "conditional_formats": conditional_formats,
        "notes": sheet_notes,
    }

    return {
        "filename": "kpi_report.xlsx",
        "sheets": [scorecard_sheet],
    }


# ── Standard pattern entry points (used by the dynamic registry) ──────

coerce_params = coerce_kpi_report_params
build_spec = build_kpi_report_spec
