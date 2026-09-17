"""
CRM pipeline pattern — deal / lead pipeline tracker (deterministic).

Creates a sales-pipeline workbook for users tracking deals that have
NOT closed yet:

  Pipeline      Deal | Company | Stage | Value | Probability |
                Weighted Value | Owner | Expected Close | Days to
                Close — Weighted Value = Value x Probability when a
                probability is given (else the plain Value), Days to
                Close = Expected Close - TODAY() (negative = overdue),
                the Stage column is a dropdown fed LIVE from the
                Stages sheet, Won rows turn green / Lost rows red and
                overdue expected-close dates turn red. Header frozen +
                auto-filter.
  Stages        topline stats (total pipeline value, expected value,
                deal count, overdue count) + the per-stage summary:
                deal count (COUNTIF), pipeline value (SUMIF) and
                weighted value (SUMIF) per stage, share of pipeline —
                plus a clustered bar chart of pipeline vs weighted
                value by stage. The stage labels in column A are the
                dropdown source for the Pipeline sheet, so editing or
                extending them here updates the dropdown.

Stage words come from the request ("Lead, Qualified, Proposal,
Negotiation, Won, Lost…" or the user's own list); deals without a
stated stage land in an explicit "Unassigned" bucket so every deal is
counted exactly once and the stage totals always add up.

Every formula reference is computed from the actual layout rows this
module emits, so off-by-N row math is impossible by construction.
No ROUND() anywhere — display rounding is the number format's job.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.services.patterns.utils import (
    MONEY_FMT,
    PCT_FMT,
    _currency_fmt,
    _pick,
    to_iso_date,
    to_number,
    to_rate,
)

# Registry key — must match the pattern stanza in
# prompts/pattern_classifier.md.
PATTERN_NAME = "crm_pipeline"

PATTERN_DESCRIPTION = (
    "Creates a sales/deal pipeline tracker spreadsheet: one row per "
    "deal with value, probability (weighted value), stage, owner and "
    "expected close date, a per-stage summary with counts, pipeline "
    "value and weighted value, total pipeline and expected value, a "
    "stage dropdown and a value-by-stage chart. Use for CRM / "
    "pipeline / leads / opportunities ('track my sales pipeline', "
    "'deals in negotiation'). Do NOT use it for completed sales logs."
)

# Routing keywords/stems — drive the cheap pre-gate and the classifier
# shortlist (see excel_gen._shortlist_patterns).
PATTERN_KEYWORDS = (
    "pipeline",
    "crm",
    "deal",
    "lead",
    "prospect",
    "opportunit",
    "funnel",
    "negoti",
    "expected close",
)

# "stages = the request's stage list when it gives one, else null for
# the standard six" (stanza contract).
DEFAULT_STAGES = ("Lead", "Qualified", "Proposal", "Negotiation", "Won", "Lost")

MAX_DEALS = 500  # explicit log rows (matches MAX_ROWS_PER_TABLE)
MAX_STAGES = 12  # cap on the user-provided stage list

_DATE_FMT = "yyyy-mm-dd"
_INT_FMT = "#,##0"
_DAYS_FMT = "0"


def _clean_text(value: Any, limit: int = 200) -> Optional[str]:
    if value is None:
        return None
    s = str(value).strip()
    return s[:limit] if s else None


# ── Param coercion ────────────────────────────────────────────────────


def _normalize_deal(entry: Any) -> Optional[dict]:
    """One deal entry → {name, company, stage, value, probability,
    owner, expected_close}, or None when unusable.

    name and value are required (a deal without a value cannot feed
    the pipeline math); probability is kept only when stated (decimal,
    "30%" or 30 all land at 0.3); stage/company/owner/expected_close
    only when stated.
    """
    if not isinstance(entry, dict):
        return None

    name = _clean_text(
        _pick(entry, "name", "deal", "deal_name", "title", "opportunity", "lead")
    )
    if name is None:
        return None  # never invent a deal

    value = to_number(
        _pick(entry, "value", "amount", "deal_value", "deal_size", "worth")
    )
    if value is None or value < 0:
        return None  # a deal without a value cannot be analyzed

    probability = to_rate(
        _pick(entry, "probability", "prob", "likelihood", "chance", "confidence")
    )
    if probability is not None:
        # clamp garbage probabilities into [0, 1]
        probability = min(max(probability, 0.0), 1.0)

    return {
        "name": name,
        "company": _clean_text(
            _pick(entry, "company", "account", "client", "organization", "org")
        ),
        "stage": _clean_text(_pick(entry, "stage", "phase", "status")),
        "value": value,
        "probability": probability,
        "owner": _clean_text(
            _pick(entry, "owner", "sales_rep", "rep", "assigned_to", "seller")
        ),
        "expected_close": to_iso_date(
            _pick(
                entry,
                "expected_close",
                "close_date",
                "expected_close_date",
                "due_date",
                "closing_date",
            )
        ),
    }


def _normalize_stages(raw: Any) -> Optional[List[str]]:
    """The request's stage list, or None for the standard six."""
    if not isinstance(raw, list):
        return None
    out: List[str] = []
    seen = set()
    for v in raw[:MAX_STAGES]:
        s = _clean_text(v, 60)
        if s is None:
            continue
        key = s.lower()
        if key not in seen:
            seen.add(key)
            out.append(s)
    return out or None


def coerce_crm_pipeline_params(params: dict) -> dict:
    """Validate + normalize classifier params; raises ValueError."""
    if not isinstance(params, dict):
        raise ValueError("params must be an object")

    raw = None
    for key in (
        "deals",
        "opportunities",
        "leads",
        "pipeline",
        "entries",
        "records",
        "items",
    ):
        if isinstance(params.get(key), list):
            raw = params[key]
            break

    deals: List[dict] = []
    if raw is not None:
        for entry in raw[:MAX_DEALS]:
            normalized = _normalize_deal(entry)
            if normalized is not None:
                deals.append(normalized)

    if not deals:
        raise ValueError("no usable deals provided")

    pipeline_name = _pick(params, "pipeline_name", "name", "title")
    pipeline_name = _clean_text(pipeline_name, 100) or "Sales Pipeline"

    notes = _pick(params, "notes", "note")
    notes = notes.strip()[:1000] if isinstance(notes, str) and notes.strip() else None

    return {
        "pipeline_name": pipeline_name,
        "currency": _currency_fmt(_pick(params, "currency")),
        "stages": _normalize_stages(_pick(params, "stages", "stage_list")),
        "deals": deals,
        "notes": notes,
    }


# ── Builder ───────────────────────────────────────────────────────────


def build_crm_pipeline_spec(params: dict) -> dict:
    """Deal pipeline workbook — every formula code-generated.

    Layout (rows computed here, never guessed by a model):

    Pipeline sheet:
      row 1     title text block
      row 3     table headers (start_cell A3, no table title)
      rows 4..  one row per deal; Weighted Value and Days to Close
                are live formulas (TODAY() date math included)
      row N+1   totals (SUM over the exact data rows)

    Stages sheet:
      rows 3..  topline text blocks (live cross-sheet formulas)
      row 8     table headers (start_cell A8, no table title)
      rows 9..  one row per stage: COUNTIF deal count + SUMIF value
                and weighted value over the Pipeline sheet's exact
                row range, keyed on the stage label in column A
      row N+1   totals; column A doubles as the Pipeline dropdown
                source range.
    """
    p = coerce_crm_pipeline_params(params)
    deals: List[dict] = p["deals"]
    money = p["currency"] or MONEY_FMT

    # Stage list: the request's list (or the standard six), extended
    # with any stage words that only appear in deals, plus an explicit
    # "Unassigned" bucket when some deal has no stage — so every deal
    # is counted exactly once and the stage totals always add up.
    stages: List[str] = list(p["stages"] or DEFAULT_STAGES)
    seen = {s.lower() for s in stages}
    for d in deals:
        st = d["stage"]
        if st and st.lower() not in seen:
            seen.add(st.lower())
            stages.append(st)
    if any(not d["stage"] for d in deals):
        stages.append("Unassigned")

    # Stage summary table geometry (rows never shift — the Pipeline
    # sheet's stage dropdown reads this exact range).
    stage_first = 9  # header on row 8 (A8, no table title)
    stage_last = 8 + len(stages)
    stage_total = stage_last + 1

    # ── Pipeline log sheet ────────────────────────────────────────────
    n = len(deals)
    first = 4  # header on row 3 (A3, no table title)
    last = first + n - 1
    total = last + 1  # noqa

    log_rows: List[List[Any]] = []
    for i, d in enumerate(deals):
        r = first + i
        log_rows.append(
            [
                d["name"],
                d["company"],
                d["stage"],
                d["value"],
                d["probability"],
                f'=IF(E{r}="",D{r},D{r}*E{r})',  # weighted = value x prob (else value)
                d["owner"],
                d["expected_close"],
                f'=IF(H{r}="","",H{r}-TODAY())',  # days to close (live TODAY() math)
            ]
        )

    log_notes = (
        "Weighted Value = Value x Probability when a probability is "
        "given, otherwise the plain Value — type a probability into "
        "column E and the weighted value recalculates. Days to Close = "
        "Expected Close - today (negative = overdue; overdue dates and "
        "negative day counts turn red). Stage is chosen from the "
        "dropdown whose options live on the Stages sheet — edit or "
        "extend the list there and the dropdown grows with it. Won "
        "stages turn green and Lost stages red."
    )
    if p["notes"]:
        log_notes = f"{p['notes']}\n{log_notes}"

    pipeline_sheet: Dict[str, Any] = {
        "name": "Pipeline",
        "tab_color": "16304F",
        "freeze_panes": "A4",
        "column_widths": {
            "A": 26,
            "B": 20,
            "C": 14,
            "D": 14,
            "E": 12,
            "F": 15,
            "G": 16,
            "H": 15,
            "I": 13,
        },
        "text_blocks": [
            {"cell": "A1", "text": p["pipeline_name"], "bold": True, "font_size": 14},
        ],
        "tables": [
            {
                "start_cell": "A3",
                "headers": [
                    "Deal",
                    "Company",
                    "Stage",
                    "Value",
                    "Probability",
                    "Weighted Value",
                    "Owner",
                    "Expected Close",
                    "Days to Close",
                ],
                "rows": log_rows,
                "number_formats": {
                    "D": money,
                    "E": PCT_FMT,
                    "F": money,
                    "H": _DATE_FMT,
                    "I": _DAYS_FMT,
                },
                "total_row": [
                    "Total",
                    None,
                    None,
                    f"=SUM(D{first}:D{last})",
                    None,
                    f"=SUM(F{first}:F{last})",
                    None,
                    None,
                    None,
                ],
                "auto_filter": True,
            }
        ],
        "data_validation": [
            {
                # live stage dropdown — options read from the Stages
                # sheet so users can extend the list there
                "range": f"C{first}:C{last}",
                "source_range": f"Stages!$A${stage_first}:$A${stage_last}",
                "allow_blank": True,
            }
        ],
        "conditional_formats": [
            {
                "range": f"C{first}:C{last}",
                "rules": [
                    {
                        "type": "cell_is",
                        "operator": "equal",
                        "value": "Won",
                        "fill": "C6EFCE",
                        "font_color": "1E4620",
                    },
                    {
                        "type": "cell_is",
                        "operator": "equal",
                        "value": "Lost",
                        "fill": "FFC7CE",
                        "font_color": "9C0006",
                    },
                ],
            },
            {
                # expected close already in the past → red
                "range": f"H{first}:H{last}",
                "rules": [
                    {
                        "type": "formula",
                        "formula": f'AND($H{first}<>"",$H{first}<TODAY())',
                        "fill": "FFC7CE",
                        "font_color": "9C0006",
                    }
                ],
            },
            {
                "range": f"I{first}:I{last}",
                "rules": [
                    {
                        "type": "cell_is",
                        "operator": "less_than",
                        "value": 0,
                        "fill": "FFC7CE",
                        "font_color": "9C0006",
                    }
                ],
            },
        ],
        "notes": log_notes,
    }

    # ── Stages summary sheet ──────────────────────────────────────────
    stage_crit = f"Pipeline!$C${first}:$C${last}"
    value_rng = f"Pipeline!$D${first}:$D${last}"
    weighted_rng = f"Pipeline!$F${first}:$F${last}"

    stage_rows: List[List[Any]] = []
    for i, stage in enumerate(stages):
        r = stage_first + i
        if stage == "Unassigned":
            # blank-aware bucket: deals with an EMPTY stage cell land
            # here (COUNTIF/SUMIF with a "" criteria match blanks), so
            # the stage totals always cover every deal.
            stage_rows.append(
                [
                    stage,
                    f'=COUNTIF({stage_crit},"")+COUNTIF({stage_crit},$A{r})',
                    f'=SUMIF({stage_crit},"",{value_rng})'
                    f"+SUMIF({stage_crit},$A{r},{value_rng})",
                    f'=SUMIF({stage_crit},"",{weighted_rng})'
                    f"+SUMIF({stage_crit},$A{r},{weighted_rng})",
                    f"=IF($C${stage_total}=0,0,C{r}/$C${stage_total})",
                ]
            )
        else:
            stage_rows.append(
                [
                    stage,
                    f"=COUNTIF({stage_crit},$A{r})",
                    f"=SUMIF({stage_crit},$A{r},{value_rng})",
                    f"=SUMIF({stage_crit},$A{r},{weighted_rng})",
                    f"=IF($C${stage_total}=0,0,C{r}/$C${stage_total})",
                ]
            )

    stages_sheet: Dict[str, Any] = {
        "name": "Stages",
        "tab_color": "1B3A5C",
        "column_widths": {"A": 18, "B": 10, "C": 16, "D": 16, "E": 14},
        "text_blocks": [
            {"cell": "A1", "text": "Pipeline by Stage", "bold": True, "font_size": 14},
            {"cell": "A3", "text": "Total Pipeline Value"},
            {
                "cell": "B3",
                "text": f"=SUM({value_rng})",
                "number_format": money,
                "bold": True,
            },
            {"cell": "A4", "text": "Expected Value (Weighted)"},
            {
                "cell": "B4",
                "text": f"=SUM({weighted_rng})",
                "number_format": money,
                "bold": True,
            },
            {"cell": "A5", "text": "Deals"},
            {
                "cell": "B5",
                "text": f"=COUNTA(Pipeline!$A${first}:$A${last})",
                "number_format": _INT_FMT,
            },
            {"cell": "A6", "text": "Overdue (Past Expected Close)"},
            {
                "cell": "B6",
                "text": f'=COUNTIF(Pipeline!$I${first}:$I${last},"<0")',
                "number_format": _INT_FMT,
            },
        ],
        "tables": [
            {
                "start_cell": "A8",
                "headers": [
                    "Stage",
                    "Deals",
                    "Pipeline Value",
                    "Weighted Value",
                    "% of Pipeline",
                ],
                "rows": stage_rows,
                "number_formats": {
                    "B": _INT_FMT,
                    "C": money,
                    "D": money,
                    "E": PCT_FMT,
                },
                "total_row": [
                    "Total",
                    f"=SUM(B{stage_first}:B{stage_last})",
                    f"=SUM(C{stage_first}:C{stage_last})",
                    f"=SUM(D{stage_first}:D{stage_last})",
                    f"=SUM(E{stage_first}:E{stage_last})",
                ],
            }
        ],
        "charts": [
            {
                "type": "bar",
                "title": "Pipeline Value by Stage",
                "anchor": "G3",
                "width": 16,
                "height": 9,
                "categories_range": f"Stages!$A${stage_first}:$A${stage_last}",
                "series": [
                    {
                        "name": "Pipeline Value",
                        "values_range": f"Stages!$C${stage_first}:$C${stage_last}",
                    },
                    {
                        "name": "Weighted Value",
                        "values_range": f"Stages!$D${stage_first}:$D${stage_last}",
                    },
                ],
                "value_numfmt": money,
            }
        ],
        "notes": (
            "Deal counts are COUNTIF and values are SUMIF over the "
            "Pipeline sheet's exact logged rows, keyed on the stage "
            "labels in column A — rename a stage here (or on a deal "
            "row) and the summary follows. Deals without a stated stage "
            "sit in the Unassigned bucket so the totals always cover "
            "every deal. The stage labels in column A are also the "
            "dropdown list used by the Pipeline sheet's Stage column: "
            "add a row here and the dropdown grows with it. Overdue "
            "counts deals whose expected close date is already in the "
            "past (live TODAY() comparison)."
        ),
    }

    return {
        "filename": "crm_pipeline.xlsx",
        "sheets": [pipeline_sheet, stages_sheet],
    }


# ── Standard pattern entry points (used by the dynamic registry) ──────

coerce_params = coerce_crm_pipeline_params
build_spec = build_crm_pipeline_spec
