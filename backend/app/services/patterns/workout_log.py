"""
Workout log pattern — quantitative training log with volume & e1RM.

A strength-training workbook computed from the classifier's session
entries (one row per exercise performed):

  Log             Date | Exercise | Sets | Reps | Weight | Volume |
                  Est. 1RM — Volume = Sets × Reps × Weight and the
                  Epley estimated 1RM = Weight × (1 + Reps/30) as live
                  formulas on every row, totals row (sets / reps /
                  volume), panes frozen below the header, auto-filter,
                  and personal-record highlighting: any row whose
                  estimated 1RM equals the best ever logged for its
                  exercise turns green
  Summary         one row per distinct exercise — Sessions (COUNTIF),
                  Total Sets (SUMIF), Total Volume (SUMIF), Best e1RM
                  and Avg Weight per Set (SUMPRODUCT over the log,
                  division guarded), totals row, and a line chart of
                  total volume by session date
  Calc (hidden)  one row per distinct session date with
                  SUMIF(Log date column, date, volume column) — the
                  helper lattice the volume-by-date chart reads

CALCULATION SEMANTICS (all LIVE formulas — nothing frozen at build
time; log a new set and every stat updates):

  volume     = Sets × Reps × Weight
  e1RM       = Weight × (1 + Reps / 30)          (Epley)
  sessions   = COUNTIF(log exercise column, exercise)
  total sets = SUMIF(exercise column, exercise, sets column)
  total vol  = SUMIF(exercise column, exercise, volume column)
  best e1RM  = SUMPRODUCT(MAX((exercise = name) × e1RM column))
               (MAX over a filtered range without needing an
               array-entered formula or MAXIFS)
  avg w/set  = SUMPRODUCT((exercise = name) × sets × weight)
               ÷ SUMIF(exercise, name, sets)   — guarded division
  vol by day = SUMIF(log date column, date cell, volume column)
  PR (CF)    = e1RM ≥ best e1RM of that row's exercise

Entries without sets/reps/weight numbers use sets 1, reps 1, weight 0
(stanza default); entries without a date are logged today (noted on
the sheet).

Every formula reference is computed from the actual layout rows this
module emits, so off-by-N row math is impossible by construction. No
ROUND() anywhere — display rounding is the number format's job.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any, Dict, List, Optional

from app.services.patterns.utils import (
    QTY_FMT,
    _pick,
    to_int,
    to_iso_date,
    to_number,
)

# Registry key — must match the pattern stanza in
# prompts/pattern_classifier.md.
PATTERN_NAME = "workout_log"

PATTERN_DESCRIPTION = (
    "Creates a training log: one row per exercise performed with sets, "
    "reps and weight, live Volume (sets × reps × weight) and estimated "
    "1RM (Epley) formulas, personal-record highlighting, a per-exercise "
    "summary with total volume / best e1RM / average weight and a "
    "volume-by-date line chart. Use when the user logs actual workouts "
    "with numbers. Do not use it for ✓/✗ habit trackers without numbers."
)

# Routing keywords/stems — drive the cheap pre-gate and the classifier
# shortlist (see excel_gen._shortlist_patterns).
PATTERN_KEYWORDS = (
    "workout",
    "training log",
    "weight training",
    "strength training",
    "exercise log",
    "gym",
    "bench press",
    "sets reps",
    "1rm",
    "rep max",
)

MAX_SESSIONS = 300  # log rows accepted from the request
MAX_EXERCISES = 40  # distinct exercises on the summary sheet
DEFAULT_LOG_NAME = "Workout Log"

_DATE_FMT = "yyyy-mm-dd"
_ONE_DEC_FMT = "0.0"
_INT_FMT = "0"
_VOL_FMT = QTY_FMT  # "#,##0.##" — thousands separators, no forced .0

# Design tokens (same palette as the converter).
NAVY = "16304F"
STEEL = "1B3A5C"
GOLD = "C9A227"
MUTED = "5C6470"
GREY = "8A94A3"

# Excel's classic Good conditional-format palette.
CF_GREEN_FILL = "C6EFCE"
CF_GREEN_TEXT = "1E4620"
CF_GOLD_FILL = "FFF2CC"
CF_GOLD_TEXT = "7F6000"


# ── Param coercion ────────────────────────────────────────────────────


def _clean_text(raw: Any, limit: int) -> str:
    if not isinstance(raw, str):
        return ""
    return raw.strip()[:limit]


def _slug(name: str) -> str:
    """log_name → workbook filename stem (Workout Log → workout_log)."""
    s = re.sub(r"[^\w\s-]", "", name.lower())
    s = re.sub(r"[\s_-]+", "_", s).strip("_")
    return s or "workout_log"


def _normalize_session(entry: Any, today: date) -> Optional[dict]:
    """One sessions entry → {date, exercise, sets, reps, weight}.

    Entries without sets/reps/weight numbers use sets 1, reps 1,
    weight 0 (stanza default); entries without a usable date are
    logged today.
    """
    if not isinstance(entry, dict):
        return None
    exercise = _clean_text(_pick(entry, "exercise", "name", "movement", "lift"), 60)
    if not exercise:
        return None  # a row without an exercise name cannot be rendered

    iso = to_iso_date(_pick(entry, "date", "day"))
    sets = to_int(_pick(entry, "sets", "set_count"))
    reps = to_int(_pick(entry, "reps", "rep_count", "repetitions"))
    weight = to_number(_pick(entry, "weight", "load", "kg"))

    return {
        "date": iso or today.isoformat(),
        "exercise": exercise,
        "sets": min(max(sets if sets is not None else 1, 1), 99),
        "reps": min(max(reps if reps is not None else 1, 1), 999),
        "weight": min(max(weight if weight is not None else 0.0, 0.0), 10000.0),
    }


def coerce_workout_log_params(params: dict) -> dict:
    """Validate + normalize classifier params; raises ValueError."""
    if not isinstance(params, dict):
        raise ValueError("params must be an object")

    today = date.today()

    raw_name = _pick(params, "log_name", "name", "title")
    log_name = _clean_text(raw_name, 80) if isinstance(raw_name, str) else ""
    log_name = log_name or DEFAULT_LOG_NAME

    raw_sessions = _pick(params, "sessions", "entries", "exercises", "log")
    sessions: List[dict] = []
    if raw_sessions is not None:
        if not isinstance(raw_sessions, list):
            raise ValueError("sessions must be an array")
        for entry in raw_sessions[:MAX_SESSIONS]:
            normalized = _normalize_session(entry, today)
            if normalized is not None:
                sessions.append(normalized)
    if not sessions:
        raise ValueError("workout_log needs at least one session entry")

    # Deterministic log order: chronological, then alphabetical.
    sessions.sort(key=lambda s: (s["date"], s["exercise"]))

    notes = _pick(params, "notes", "note")
    notes = notes.strip()[:1000] if isinstance(notes, str) and notes.strip() else None

    return {
        "log_name": log_name,
        "sessions": sessions,
        "notes": notes,
    }


# ── Builder ───────────────────────────────────────────────────────────


def build_workout_log_spec(params: dict) -> dict:
    """Workout log workbook — every formula code-generated.

    Layout (rows computed here, never guessed by a model):

    Log sheet:
      row 1      title text block
      row 2      usage hint
      row 4      headers: Date | Exercise | Sets | Reps | Weight |
                 Volume | Est. 1RM
      rows 5..   one row per session entry; F = C×D×E, G = E×(1+D/30)
      total row  sums for Sets / Reps / Volume
    Summary sheet:
      row 1      title text block
      row 2      usage hint
      row 4      Per-Exercise table title → header 5, data 6..,
                 total row after; chart anchored at H4
    Calc sheet (hidden):
      row 1      note; table at A3 → header 3, data 4.. — one row per
                 distinct session date with SUMIF volume by date
    """
    p = coerce_workout_log_params(params)
    log_name: str = p["log_name"]
    sessions: List[dict] = p["sessions"]
    notes = p["notes"]

    # ── geometry ────────────────────────────────────────────────────
    r0 = 5  # first log data row
    rN = 4 + len(sessions)  # last log data row
    log_total = rN + 1  # noqa: log totals row

    # Distinct exercises in order of first appearance (user emphasis).
    exercises: List[str] = []
    for s in sessions:
        if s["exercise"] not in exercises:
            exercises.append(s["exercise"])
    exercises = exercises[:MAX_EXERCISES]

    # Distinct session dates, ascending — the Calc lattice.
    distinct_dates: List[str] = sorted({s["date"] for s in sessions})

    # ═════════════════════════════ Log sheet ════════════════════════
    log_rows: List[List[Any]] = []
    for s in sessions:
        r = r0 + len(log_rows)
        log_rows.append(
            [
                s["date"],
                s["exercise"],
                s["sets"],
                s["reps"],
                s["weight"],
                # Volume = sets × reps × weight
                "=C{r}*D{r}*E{r}".format(r=r),
                # Est. 1RM — Epley: weight × (1 + reps / 30)
                "=E{r}*(1+D{r}/30)".format(r=r),
            ]
        )

    log_sheet: Dict[str, Any] = {
        "name": "Log",
        "tab_color": NAVY,
        "freeze_panes": "A5",
        "column_widths": {
            "A": 13,
            "B": 26,
            "C": 8,
            "D": 8,
            "E": 10,
            "F": 12,
            "G": 10,
        },
        "text_blocks": [
            {
                "cell": "A1",
                "text": log_name,
                "bold": True,
                "font_size": 14,
                "font_color": NAVY,
            },
            {
                "cell": "A2",
                "text": (
                    "One row per exercise performed — Volume and the "
                    "estimated 1RM compute automatically; your best-ever "
                    "sets are highlighted green."
                ),
                "italic": True,
                "font_color": MUTED,
            },
        ],
        "tables": [
            {
                "start_cell": "A4",
                "headers": [
                    "Date",
                    "Exercise",
                    "Sets",
                    "Reps",
                    "Weight",
                    "Volume",
                    "Est. 1RM",
                ],
                "rows": log_rows,
                "number_formats": {
                    "A": _DATE_FMT,
                    "C": _INT_FMT,
                    "D": _INT_FMT,
                    "E": _ONE_DEC_FMT,
                    "F": _VOL_FMT,
                    "G": _ONE_DEC_FMT,
                },
                "alignments": {
                    "A": "center",
                    "C": "center",
                    "D": "center",
                    "E": "center",
                    "F": "center",
                    "G": "center",
                },
                "auto_filter": True,
                "total_row": [
                    "Total",
                    None,
                    "=SUM(C{first_row}:C{last_row})".format(first_row=r0, last_row=rN),
                    "=SUM(D{first_row}:D{last_row})".format(first_row=r0, last_row=rN),
                    None,
                    "=SUM(F{first_row}:F{last_row})".format(first_row=r0, last_row=rN),
                    None,
                ],
            }
        ],
        "conditional_formats": [
            {
                # PR: e1RM at (or above) the best e1RM ever logged for
                # that row's exercise → green. SUMPRODUCT(MAX(...))
                # filters the e1RM column by exercise without needing
                # an array-entered formula.
                "range": f"G{r0}:G{rN}",
                "rules": [
                    {
                        "type": "formula",
                        "formula": (
                            "AND($G{r0}>0,$G{r0}>="
                            "SUMPRODUCT(MAX(($B${r0}:$B${rN}=$B{r0})"
                            "*$G${r0}:$G${rN})))"
                        ).format(r0=r0, rN=rN),
                        "fill": CF_GREEN_FILL,
                        "font_color": CF_GREEN_TEXT,
                        "bold": True,
                        "stop_if_true": False,
                    }
                ],
            }
        ],
        "notes": notes
        or (
            "Log each exercise you perform with its sets, reps and "
            "weight. Volume = Sets × Reps × Weight; Est. 1RM uses the "
            "Epley formula Weight × (1 + Reps/30). A set whose estimated "
            "1RM ties your best for that exercise is highlighted green. "
            "Entries logged without a date carry today's date. The "
            "Summary sheet breaks everything down per exercise and plots "
            "total volume by date."
        ),
    }

    # ═════════════════════════════ Summary sheet ════════════════════
    s0 = 6  # first summary data row (title 4, header 5)
    sN = s0 + len(exercises) - 1  # last summary data row
    sum_total = sN + 1  # noqa: summary totals row

    exercise_rows: List[List[Any]] = []
    for i, name in enumerate(exercises):
        r = s0 + i
        exercise_rows.append(
            [
                name,
                # Sessions with this exercise
                "=COUNTIF(Log!$B${r0}:$B${rN},$A{r})".format(r0=r0, rN=rN, r=r),
                # Total sets
                "=SUMIF(Log!$B${r0}:$B${rN},$A{r},Log!$C${r0}:$C${rN})".format(
                    r0=r0, rN=rN, r=r
                ),
                # Total volume
                "=SUMIF(Log!$B${r0}:$B${rN},$A{r},Log!$F${r0}:$F${rN})".format(
                    r0=r0, rN=rN, r=r
                ),
                # Best estimated 1RM (PR) for the exercise
                "=SUMPRODUCT(MAX((Log!$B${r0}:$B${rN}=$A{r})"
                "*Log!$G${r0}:$G${rN}))".format(r0=r0, rN=rN, r=r),
                # Avg weight per set — volume-weighted, guarded division
                "=IF(SUMIF(Log!$B${r0}:$B${rN},$A{r},Log!$C${r0}:$C${rN})=0,"
                '"n/a",'
                "SUMPRODUCT((Log!$B${r0}:$B${rN}=$A{r})"
                "*Log!$C${r0}:$C${rN}*Log!$E${r0}:$E${rN})"
                "/SUMIF(Log!$B${r0}:$B${rN},$A{r},Log!$C${r0}:$C${rN}))".format(
                    r0=r0, rN=rN, r=r
                ),
            ]
        )

    # ═════════════════════════════ Calc sheet (hidden) ══════════════
    # One row per distinct session date: SUMIF of volume by date —
    # the lattice the volume-by-date line chart reads.
    calc_rows: List[List[Any]] = []
    for i, day in enumerate(distinct_dates):
        r = 4 + i
        calc_rows.append(
            [
                day,
                "=SUMIF(Log!$A${r0}:$A${rN},$A{r},Log!$F${r0}:$F${rN})".format(
                    r0=r0, rN=rN, r=r
                ),
            ]
        )
    calc_first = 4
    calc_last = 3 + len(distinct_dates)

    calc_sheet: Dict[str, Any] = {
        "name": "Calc",
        "tab_color": GREY,
        "hidden": True,
        "column_widths": {"A": 13, "B": 14},
        "text_blocks": [
            {
                "cell": "A1",
                "text": "Total volume by session date — chart helper",
                "italic": True,
                "font_color": MUTED,
            }
        ],
        "tables": [
            {
                "start_cell": "A3",
                "headers": ["Date", "Total Volume"],
                "rows": calc_rows,
                "number_formats": {"A": _DATE_FMT, "B": _VOL_FMT},
                "alignments": {"A": "center"},
                "zebra": False,
            }
        ],
        "notes": (
            "Hidden calculation sheet: one row per distinct session date "
            "with SUMIF over the Log sheet's Volume column. The Summary "
            "sheet's volume-by-date chart reads this table. Right-click "
            "a tab → Unhide to inspect it."
        ),
    }

    summary_sheet: Dict[str, Any] = {
        "name": "Summary",
        "tab_color": STEEL,
        "no_freeze": True,
        "column_widths": {
            "A": 26,
            "B": 10,
            "C": 12,
            "D": 14,
            "E": 12,
            "F": 14,
        },
        "text_blocks": [
            {
                "cell": "A1",
                "text": "Summary",
                "bold": True,
                "font_size": 14,
                "font_color": STEEL,
            },
            {
                "cell": "A2",
                "text": (
                    "Per-exercise totals and personal records — everything "
                    "updates live from the Log sheet."
                ),
                "italic": True,
                "font_color": MUTED,
            },
        ],
        "tables": [
            {
                "start_cell": "A4",
                "title": "Per-Exercise Summary",
                "headers": [
                    "Exercise",
                    "Sessions",
                    "Total Sets",
                    "Total Volume",
                    "Best e1RM",
                    "Avg Weight / Set",
                ],
                "rows": exercise_rows,
                "number_formats": {
                    "B": _INT_FMT,
                    "C": _INT_FMT,
                    "D": _VOL_FMT,
                    "E": _ONE_DEC_FMT,
                    "F": _ONE_DEC_FMT,
                },
                "alignments": {
                    "B": "center",
                    "C": "center",
                    "D": "center",
                    "E": "center",
                    "F": "center",
                },
                "total_row": [
                    "Total",
                    "=SUM(B{first_row}:B{last_row})".format(first_row=s0, last_row=sN),
                    "=SUM(C{first_row}:C{last_row})".format(first_row=s0, last_row=sN),
                    "=SUM(D{first_row}:D{last_row})".format(first_row=s0, last_row=sN),
                    None,
                    None,
                ],
            }
        ],
        "charts": [
            {
                "type": "line",
                "title": "Total Volume by Date",
                "anchor": "H4",
                "width": 16,
                "height": 9,
                "categories_range": "Calc!$A${first}:$A${last}".format(
                    first=calc_first, last=calc_last
                ),
                "series": [
                    {
                        "name": "Total Volume",
                        "values_range": "Calc!$B${first}:$B${last}".format(
                            first=calc_first, last=calc_last
                        ),
                    }
                ],
                "value_numfmt": _VOL_FMT,
            }
        ],
        "notes": (
            "Sessions, Total Sets and Total Volume are COUNTIF/SUMIF over "
            "the Log sheet. Best e1RM is the highest estimated 1RM logged "
            "for the exercise (the same rows the Log sheet paints green). "
            "Avg Weight / Set = Σ(sets × weight) ÷ Σsets for the exercise. "
            "The line chart plots total volume per session date (computed "
            "on the hidden Calc sheet)."
        ),
    }

    return {
        "filename": "{slug}.xlsx".format(slug=_slug(log_name)),
        "sheets": [log_sheet, summary_sheet, calc_sheet],
    }


# ── Standard pattern entry points (used by the dynamic registry) ─────

coerce_params = coerce_workout_log_params
build_spec = build_workout_log_spec
