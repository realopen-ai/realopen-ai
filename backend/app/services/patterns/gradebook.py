"""
Gradebook pattern — class gradebook: students × assessments.

Deterministic workbook (every formula reference computed from the rows
this module emits):

  Gradebook       Student | one column per assessment (blank cell = not
                 yet graded) | Total Points | Overall % | Grade —
                 totals are SUM() over the row's score cells, the
                 overall % is the weighted category average (see Calc),
                 the letter grade a nested IF on the overall %, class
                 averages in the total row. Green/amber/red conditional
                 formatting on Overall %, A/F highlights on Grade, and
                 a bar chart of overall % per student.
  Assessments    reference sheet: one row per assessment (name /
                 category / max score) + the Category Weights table
                 (weight % per category, SUM total row proving 100%).
                 The Category column carries a dropdown sourced from
                 the weights table's category names.
  Calc (hidden)  per-student helper lattices: one column per assessment
                 holding score/max (blank when unscored — AVERAGE over
                 a range skips those), one column per CATEGORY holding
                 the average of its assessments' normalized scores, and
                 row 2 mirroring the weights from the Assessments sheet
                 so the overall % keeps recomputing when weights change.

CALCULATION SEMANTICS (all LIVE formulas — nothing frozen at build
time; edit a score, weight or max score and everything recomputes):

  norm(a)     = score / max_score                    ("" when blank)
  category %  = AVERAGE of norm() over the category's
                assessments — AVERAGE over a RANGE skips blanks, so a
                missing score never drags the average down
  weights     = per-category decimals; equal split (1 / #categories)
                when the request states none. Normalized to sum 1.
  overall %   = SUMPRODUCT(category %s, weights) over the categories
                that have scores, renormalized by the present weights —
                a fully scored student gets the plain weighted average,
                a student missing a whole category is graded on the
                rest ("" only when nothing is scored at all)
  grade       = A ≥ .9, B ≥ .8, C ≥ .7, D ≥ .6, else F

No ROUND() anywhere — display rounding is the number format's job.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from app.services.patterns.utils import (
    PCT_FMT,
    _pick,
    to_number,
)

# Registry key — must match the pattern stanza in
# prompts/pattern_classifier.md.
PATTERN_NAME = "gradebook"

PATTERN_DESCRIPTION = (
    "Class gradebook: students x assessments with per-category weighted "
    "totals, overall percentages, letter grades, a weights reference "
    "sheet and an overall-% chart. Use when the user wants student "
    "grades / class score tracking. Do NOT use for survey or poll "
    "response counts."
)

# Routing keywords/stems — drive the cheap pre-gate and the classifier
# shortlist (see excel_gen._shortlist_patterns).
PATTERN_KEYWORDS = (
    "gradebook",
    "grade",
    "student",
    "class score",
    "report card",
    "homework",
    "quiz",
    "exam",
)

MAX_ASSESSMENTS = 20
MAX_STUDENTS = 60
DEFAULT_MAX_SCORE = 100.0
FALLBACK_CATEGORY = "General"
FALLBACK_CLASS_NAME = "Class"

# Design tokens (same palette as the converter).
NAVY = "16304F"
STEEL = "1B3A5C"
MUTED = "5C6470"

# Excel's classic Good / Bad / Neutral conditional-format palettes.
CF_GREEN_FILL = "C6EFCE"
CF_GREEN_TEXT = "1E4620"
CF_AMBER_FILL = "FFF2CC"
CF_AMBER_TEXT = "7F6000"
CF_RED_FILL = "FFC7CE"
CF_RED_TEXT = "9C0006"


def _slug(text: str, fallback: str) -> str:
    s = re.sub(r"[^\w\s-]", "", str(text).strip().lower())
    s = re.sub(r"[\s_-]+", "_", s).strip("_")
    return s[:40] or fallback


def _clean_name(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


# ── Param coercion ────────────────────────────────────────────────────


def _normalize_assessment(entry: Any) -> Optional[dict]:
    """One assessments entry → {name, category, max_score} or None."""
    if isinstance(entry, str):
        name = _clean_name(entry, 40)
        category, max_score = None, None
    elif isinstance(entry, dict):
        name = _clean_name(_pick(entry, "name", "assessment", "title"), 40)
        category = _clean_name(_pick(entry, "category", "type", "group"), 30)
        max_score = to_number(_pick(entry, "max_score", "max", "points", "out_of"))
    else:
        return None
    if not name:
        return None
    if max_score is None or max_score <= 0:
        max_score = DEFAULT_MAX_SCORE  # stanza: max_score 100 when not stated
    return {
        "name": name,
        "category": category or FALLBACK_CATEGORY,
        "max_score": max_score,
    }


def _normalize_student(entry: Any, n_assessments: int) -> Optional[dict]:
    """One students entry → {name, scores[None|float]} or None.

    scores are aligned positionally to the assessments array (same
    order); null/blank → None (blank cell). Short lists are padded,
    long lists truncated.
    """
    if isinstance(entry, str):
        name = _clean_name(entry, 60)
        raw_scores: List[Any] = []
    elif isinstance(entry, dict):
        name = _clean_name(_pick(entry, "name", "student", "full_name"), 60)
        raw = _pick(entry, "scores", "score_list", "grades", "marks")
        raw_scores = list(raw) if isinstance(raw, list) else []
    else:
        return None
    if not name:
        return None
    scores: List[Optional[float]] = []
    for i in range(n_assessments):
        raw = raw_scores[i] if i < len(raw_scores) else None
        n = to_number(raw)
        scores.append(n if n is not None and n >= 0 else None)
    return {"name": name, "scores": scores}


def coerce_gradebook_params(params: dict) -> dict:
    """Validate + normalize classifier params; raises ValueError."""
    if not isinstance(params, dict):
        raise ValueError("params must be an object")

    class_name = (
        _clean_name(_pick(params, "class_name", "class", "course", "title", "name"), 60)
        or FALLBACK_CLASS_NAME
    )

    raw_assessments = _pick(params, "assessments", "assessment_list", "assignments")
    assessments: List[dict] = []
    if raw_assessments is not None:
        if not isinstance(raw_assessments, list):
            raise ValueError("assessments must be an array")
        for entry in raw_assessments[:MAX_ASSESSMENTS]:
            normalized = _normalize_assessment(entry)
            if normalized is not None:
                assessments.append(normalized)

    raw_students = _pick(params, "students", "student_list", "roster")
    students: List[dict] = []
    if raw_students is not None:
        if not isinstance(raw_students, list):
            raise ValueError("students must be an array")
        for entry in raw_students[:MAX_STUDENTS]:
            normalized = _normalize_student(entry, len(assessments))
            if normalized is not None:
                students.append(normalized)

    # Never invent students or scores — without both sides there is no
    # gradebook to build (caller falls back to the AI spec path).
    if not assessments:
        raise ValueError("no usable assessments")
    if not students:
        raise ValueError("no usable students")

    # Categories in first-appearance order.
    categories: List[str] = []
    for a in assessments:
        if a["category"] not in categories:
            categories.append(a["category"])

    # Weights: per-category decimals when the request states them, else
    # an equal split (1 / #categories) — normalized to sum 1 either way.
    raw_weights = _pick(params, "weights", "category_weights", "weight_by_category")
    weights: Dict[str, float] = {}
    if isinstance(raw_weights, dict):
        lower_map = {c.lower(): c for c in categories}
        for key, value in raw_weights.items():
            cat = lower_map.get(str(key).strip().lower())
            n = to_number(value)
            if cat is None or n is None or n <= 0:
                continue
            weights[cat] = n
    if weights:
        stated_total = sum(weights.values())
        missing = [c for c in categories if c not in weights]
        if stated_total < 1 and missing:
            # partial allocation ("Homework 25%") — the unstated
            # categories share the remainder equally; stated keep face
            # value. Total becomes 1.
            for c in missing:
                weights[c] = (1 - stated_total) / len(missing)
        else:
            # full allocation or a plain ratio ({"Alice": 2, "Bob": 1})
            # — scale so the stated weights sum to 1; unstated get 0.
            weights = {c: weights[c] / stated_total for c in weights}
            for c in missing:
                weights[c] = 0.0
        # iron out float dust so the weights table SUMs to exactly 100%
        s = sum(weights.values())
        if s > 0:
            weights = {c: weights[c] / s for c in categories}
    if not weights:
        share = 1.0 / len(categories)
        weights = {c: share for c in categories}
    weights = {c: weights[c] for c in categories}

    notes = _pick(params, "notes", "note")
    notes = notes.strip()[:1000] if isinstance(notes, str) and notes.strip() else None

    return {
        "class_name": class_name,
        "assessments": assessments,
        "students": students,
        "categories": categories,
        "weights": weights,
        "weights_stated": bool(isinstance(raw_weights, dict) and weights),
        "notes": notes,
    }


# ── Builder ───────────────────────────────────────────────────────────


def build_gradebook_spec(params: dict) -> dict:
    """Gradebook workbook — every formula code-generated.

    Layout (rows computed here, never guessed by a model):

    Gradebook sheet:
      row 1      title block, row 2 usage hint
      row 4      headers: Student | assessment names… | Total Points |
                 Overall % | Grade
      rows 5-    one row per student; Total Points = SUM of the row's
                 score cells; Overall % = weighted category average via
                 the Calc helpers; Grade = nested IF thresholds
      total row  class averages (guarded AVERAGE over the data rows)
    Assessments sheet:
      rows 5-    assessment reference (name / category / max score),
                 category dropdown sourced from the weights table
      rows w0-   Category Weights table (weight % + live COUNTIF of
                 assessments per category) with a SUM total row
    Calc sheet (hidden):
      row 2      weight mirror (live refs to the Assessments weights)
      rows 5-    per-assessment normalized scores (blank when unscored),
                 grouped by category so each category's columns are
                 contiguous; then one column per category with the
                 AVERAGE of its normalized scores
    """
    p = coerce_gradebook_params(params)
    class_name: str = p["class_name"]
    assessments: List[dict] = p["assessments"]
    students: List[dict] = p["students"]
    categories: List[str] = p["categories"]
    weights: Dict[str, float] = p["weights"]
    weights_stated: bool = p["weights_stated"]
    notes = p["notes"]

    n_a = len(assessments)
    n_s = len(students)
    n_cat = len(categories)

    # integral data keeps the classic "85" look; halves need "0.0"
    all_scores = [s for st in students for s in st["scores"] if s is not None]
    integral = all(
        s == int(s) for s in all_scores + [a["max_score"] for a in assessments]
    )
    score_fmt = "0" if integral else "0.0"

    # ── geometry (all refs below are computed from THESE numbers) ──
    GB_R0 = 5  # first Gradebook data row
    GB_RN = 4 + n_s  # last Gradebook data row
    GB_TOT = GB_RN + 1  # noqa: class-average total row
    AS_R0 = 5  # first Assessments data row
    AS_RN = 4 + n_a  # last Assessments data row
    W_TITLE = n_a + 6  # Category Weights table title row
    W_HEADER = W_TITLE + 1
    W_R0 = W_HEADER + 1  # first weights data row
    W_RN = W_R0 + n_cat - 1  # last weights data row
    W_TOT = W_RN + 1  # noqa: weights SUM total row

    def col(idx: int) -> str:
        # 1-based column index → letters (A=1, B=2, …)
        s = ""
        n = idx
        while n > 0:
            n, r = divmod(n - 1, 26)
            s = chr(65 + r) + s
        return s

    student_col = "A"
    score_cols = [col(2 + j) for j in range(n_a)]
    tp_col = col(2 + n_a)  # Total Points
    ov_col = col(3 + n_a)  # Overall %
    gr_col = col(4 + n_a)  # Grade
    last_score_col = score_cols[-1]
    chart_anchor_col = col(5 + n_a)

    # Calc helper columns grouped by category (contiguous per category).
    grouped: List[Tuple[str, List[int]]] = []  # (category, [assessment idx])
    for cat in categories:
        grouped.append(
            (cat, [j for j, a in enumerate(assessments) if a["category"] == cat])
        )
    helper_cols = [col(2 + g) for g in range(n_a)]  # grouped assessment cols
    calc_gap_col = col(2 + n_a)  # spacer column
    cat_cols = [col(3 + n_a + k) for k in range(n_cat)]  # category % cols
    calc_k0, calc_k1 = cat_cols[0], cat_cols[-1]

    # ── Gradebook sheet rows ─────────────────────────────────────────
    gb_rows: List[List[Any]] = []
    for i, st in enumerate(students):
        r = GB_R0 + i
        weight_range = f"Calc!${calc_k0}$2:${calc_k1}$2"
        cats_range = f"Calc!{calc_k0}{r}:{calc_k1}{r}"
        gb_rows.append(
            [
                st["name"],
                # B..  raw scores (None → blank cell = not yet graded)
                *st["scores"],
                # Total Points — SUM over this row's score cells
                f"=SUM({score_cols[0]}{r}:{last_score_col}{r})",
                # Overall % — weighted average over the categories that
                # have scores (denominator = present weights). Fully
                # scored → plain SUMPRODUCT(category %s, weights).
                (
                    f'=IF(SUMPRODUCT(({cats_range}<>"")*{weight_range})=0,"",'
                    f"SUMPRODUCT({cats_range},{weight_range})/"
                    f'SUMPRODUCT(({cats_range}<>"")*{weight_range}))'
                ),
                # Grade — nested IF thresholds on the overall %
                (
                    f'=IF({ov_col}{r}="","",'
                    f'IF({ov_col}{r}>=0.9,"A",'
                    f'IF({ov_col}{r}>=0.8,"B",'
                    f'IF({ov_col}{r}>=0.7,"C",'
                    f'IF({ov_col}{r}>=0.6,"D","F")))))'
                ),
            ]
        )

    def _guarded_avg(colletter: str) -> str:
        return (
            f'=IF(COUNT({colletter}{GB_R0}:{colletter}{GB_RN})=0,"",'
            f"AVERAGE({colletter}{GB_R0}:{colletter}{GB_RN}))"
        )

    gb_total_row: List[Any] = (
        ["Class Average"]
        + [_guarded_avg(c) for c in score_cols]
        + [_guarded_avg(tp_col), _guarded_avg(ov_col), ""]
    )

    weight_note = (
        "as stated in the request"
        if weights_stated
        else "equal split — the request stated no weights"
    )

    gradebook_sheet: Dict[str, Any] = {
        "name": "Gradebook",
        "tab_color": NAVY,
        "freeze_panes": f"B{GB_R0}",  # header rows + student names stay visible
        "column_widths": {
            "A": 26,
            **{c: 13 for c in score_cols},
            tp_col: 12,
            ov_col: 11,
            gr_col: 9,
        },
        "text_blocks": [
            {
                "cell": "A1",
                "text": f"{class_name} Gradebook",
                "bold": True,
                "font_size": 14,
                "font_color": NAVY,
            },
            {
                "cell": "A2",
                "text": (
                    "Enter scores per assessment (blank = not yet graded). "
                    "Total Points, the weighted Overall % and letter grades "
                    "update automatically; category weights live on the "
                    "Assessments sheet."
                ),
                "italic": True,
                "font_color": MUTED,
            },
        ],
        "tables": [
            {
                "start_cell": "A4",
                "headers": ["Student"]
                + [a["name"] for a in assessments]
                + ["Total Points", "Overall %", "Grade"],
                "rows": gb_rows,
                "total_row": gb_total_row,
                "number_formats": {
                    **{c: score_fmt for c in score_cols},
                    tp_col: score_fmt,
                    ov_col: PCT_FMT,
                },
                "alignments": {
                    **{c: "center" for c in score_cols},
                    tp_col: "center",
                    ov_col: "center",
                    gr_col: "center",
                },
            }
        ],
        "conditional_formats": [
            {
                "range": f"{ov_col}{GB_R0}:{ov_col}{GB_RN}",
                "rules": [
                    {
                        "type": "cell_is",
                        "operator": "greater_than_or_equal",
                        "value": 0.9,
                        "fill": CF_GREEN_FILL,
                        "font_color": CF_GREEN_TEXT,
                        "bold": True,
                        "stop_if_true": True,
                    },
                    {
                        "type": "cell_is",
                        "operator": "between",
                        "value": [0.6, 0.9],
                        "fill": CF_AMBER_FILL,
                        "font_color": CF_AMBER_TEXT,
                        "stop_if_true": True,
                    },
                    {
                        "type": "cell_is",
                        "operator": "less_than",
                        "value": 0.6,
                        "fill": CF_RED_FILL,
                        "font_color": CF_RED_TEXT,
                    },
                ],
            },
            {
                "range": f"{gr_col}{GB_R0}:{gr_col}{GB_RN}",
                "rules": [
                    {
                        "type": "cell_is",
                        "operator": "equal",
                        "value": "A",
                        "fill": CF_GREEN_FILL,
                        "font_color": CF_GREEN_TEXT,
                        "bold": True,
                    },
                    {
                        "type": "cell_is",
                        "operator": "equal",
                        "value": "F",
                        "fill": CF_RED_FILL,
                        "font_color": CF_RED_TEXT,
                    },
                ],
            },
        ],
        "charts": [
            {
                "type": "bar",
                "title": "Overall % by Student",
                "anchor": f"{chart_anchor_col}3",
                "width": 16,
                "height": 9,
                "categories_range": f"Gradebook!{student_col}{GB_R0}:{student_col}{GB_RN}",
                "series": [
                    {
                        "name": "Overall %",
                        "values_range": f"Gradebook!{ov_col}{GB_R0}:{ov_col}{GB_RN}",
                    }
                ],
                "value_numfmt": "0%",
            }
        ],
        "notes": notes
        or (
            "Scores: one column per assessment, blank cell = not yet graded "
            "(category averages skip blanks). Total Points sums the row's "
            f"scores; Overall % weights each category average by its weight "
            f"({weight_note} — see the Assessments sheet). Grades: A >= 90%, "
            "B >= 80%, C >= 70%, D >= 60%, else F. The class average row "
            "uses guarded AVERAGEs."
        ),
    }

    # ── Assessments sheet ────────────────────────────────────────────
    as_rows: List[List[Any]] = [
        [a["name"], a["category"], a["max_score"]] for a in assessments
    ]
    weight_rows: List[List[Any]] = []
    for k, cat in enumerate(categories):
        wr = W_R0 + k
        weight_rows.append(
            [
                cat,
                weights[cat],
                f"=COUNTIF($B${AS_R0}:$B${AS_RN},$A{wr})",
            ]
        )
    weights_total_row: List[Any] = [
        "Total",
        f"=SUM(B{W_R0}:B{W_RN})",
        f"=SUM(C{W_R0}:C{W_RN})",
    ]

    assessments_sheet: Dict[str, Any] = {
        "name": "Assessments",
        "tab_color": STEEL,
        "column_widths": {"A": 28, "B": 16, "C": 12},
        "text_blocks": [
            {
                "cell": "A1",
                "text": "Assessments & Category Weights",
                "bold": True,
                "font_size": 12,
                "font_color": STEEL,
            },
        ],
        "tables": [
            {
                "start_cell": "A3",
                "title": "Assessments",
                "headers": ["Assessment", "Category", "Max Score"],
                "rows": as_rows,
                "number_formats": {"C": score_fmt},
                "alignments": {"C": "center"},
            },
            {
                "start_cell": f"A{W_TITLE}",
                "title": "Category Weights",
                "headers": ["Category", "Weight", "Assessments"],
                "rows": weight_rows,
                "total_row": weights_total_row,
                "number_formats": {"B": PCT_FMT, "C": "0"},
                "alignments": {"B": "center", "C": "center"},
            },
        ],
        "data_validation": [
            {
                "range": f"B{AS_R0}:B{AS_RN}",
                "source_range": f"Assessments!$A${W_R0}:$A${W_RN}",
                "allow_blank": True,
                "prompt_title": "Category",
                "prompt": "Pick one of the weighted categories.",
                "error_title": "Unknown category",
                "error": "Choose a category from the weights table.",
            }
        ],
        "notes": (
            "Reference sheet: one row per assessment (max score defaults to "
            "100 when the request stated none). The Category Weights table "
            f"holds the per-category weight ({weight_note}); the Total row "
            "proves they sum to 100%. The Assessments count column is a "
            "live COUNTIF. Weight changes flow into every Overall %."
        ),
    }

    # ── Calc sheet (hidden helpers, rows aligned with the Gradebook) ──
    # Row 2 mirrors the weights (live refs) so Overall % keeps tracking
    # user edits on the Assessments sheet.
    mirror_blocks: List[dict] = []
    for k, cat in enumerate(categories):
        mirror_blocks.append(
            {
                "cell": f"{cat_cols[k]}2",
                "text": f"=Assessments!$B${W_R0 + k}",
                "number_format": PCT_FMT,
            }
        )

    # (h0, h1) helper-column range per category — the helper columns
    # are emitted grouped by category, so each range is contiguous.
    cat_ranges: List[Tuple[str, str]] = []
    _g = 0
    for _cat, _idxs in grouped:
        cat_ranges.append((helper_cols[_g], helper_cols[_g + len(_idxs) - 1]))
        _g += len(_idxs)

    helper_rows: List[List[Any]] = []
    cat_rows: List[List[Any]] = []
    for i in range(n_s):
        r = GB_R0 + i  # Calc data rows run 1:1 with the Gradebook rows
        helper_row: List[Any] = []
        for _cat, idxs in grouped:
            for j in idxs:
                sc = score_cols[j]
                ar = AS_R0 + j
                helper_row.append(
                    f'=IF(Gradebook!{sc}{r}="","",'
                    f"Gradebook!{sc}{r}/Assessments!$C${ar})"
                )
        helper_rows.append(helper_row)

        cat_row: List[Any] = []
        for k, (_cat, _idxs) in enumerate(grouped):
            h0, h1 = cat_ranges[k]
            cat_row.append(f'=IF(COUNT({h0}{r}:{h1}{r})=0,"",AVERAGE({h0}{r}:{h1}{r}))')
        cat_rows.append(cat_row)

    calc_sheet: Dict[str, Any] = {
        "name": "Calc",
        "hidden": True,
        "column_widths": {
            **{c: 12 for c in helper_cols},
            calc_gap_col: 4,
            **{c: 14 for c in cat_cols},
        },
        "text_blocks": [
            {
                "cell": "A1",
                "text": ("Hidden helpers — rows run 1:1 with the Gradebook sheet."),
                "italic": True,
                "font_color": MUTED,
            },
            {
                "cell": "A2",
                "text": (
                    f"Row 2 ({calc_k0}2:{calc_k1}2) mirrors the category "
                    "weights from the Assessments sheet; columns "
                    f"{helper_cols[0]}..{helper_cols[-1]} hold each "
                    "assessment's score/max (blank when unscored); columns "
                    f"{calc_k0}..{calc_k1} the per-category averages "
                    "(AVERAGE skips blanks)."
                ),
                "italic": True,
                "font_color": MUTED,
            },
            *mirror_blocks,
        ],
        "tables": [
            {
                "start_cell": "B4",
                "headers": [
                    assessments[j]["name"] for _c, idxs in grouped for j in idxs
                ],
                "rows": helper_rows,
                "number_formats": {c: PCT_FMT for c in helper_cols},
            },
            {
                "start_cell": f"{cat_cols[0]}4",
                "headers": list(categories),
                "rows": cat_rows,
                "number_formats": {c: PCT_FMT for c in cat_cols},
            },
        ],
        "notes": (
            "Hidden calculation sheet. Per-assessment normalized scores "
            "(score / max, blank when unscored) are grouped by category so "
            "each category's columns are contiguous; the category columns "
            "average them (AVERAGE over a range skips blanks). Row 2 "
            "mirrors the weights live. The Gradebook's Overall % is a "
            "SUMPRODUCT of the category averages and the mirrored weights, "
            "renormalized over the categories that have scores. Extend by "
            "dragging the helper columns down together with the students. "
            "Right-click a tab → Unhide to inspect."
        ),
    }

    return {
        "filename": f"{_slug(class_name, 'class')}_gradebook.xlsx",
        "sheets": [gradebook_sheet, assessments_sheet, calc_sheet],
    }


# ── Standard pattern entry points (used by the dynamic registry) ──────

coerce_params = coerce_gradebook_params
build_spec = build_gradebook_spec
