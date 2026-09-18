"""
Survey results pattern — survey / poll / questionnaire tabulation.

Deterministic workbook (every formula reference computed from the rows
this module emits):

  Survey          one block per question (title "Qn. <question> (<type>)"):
                  Option | Responses | Share % rows plus a per-question
                  Total row (SUM of the counts). Share % is a guarded
                  division by the question total; the most-chosen
                  option of every question is highlighted gold.
  Survey2         continuation sheet when the survey has more than six
                  questions (blocks 7-12).
  Summary         Question | Type | Respondents | Average Rating —
                  respondents reference each block's Total cell, the
                  rating average is a live SUMPRODUCT of the option
                  scale (helper column on Calc) and the counts, and
                  ONE summary chart (pie for a rating question, else a
                  horizontal bar) shows the top question's counts.
  Calc (hidden)   one column per RATING question holding the option
                  scale values, aligned 1:1 with that question's option
                  rows on the Survey sheet.

PARAMETER SEMANTICS (per the classifier stanza):

  options   the request's own option list when given; else the default
            scale for the type — rating → the 5-point Agree scale
            (Strongly Agree … Strongly Disagree), yesno → Yes / No /
            No answer. For a choice question without options the
            response keys become the options (numeric keys ascending).
  responses the exact counts the request gives per option (JSON
            numbers; missing options count as 0). A question without
            ANY counts given is OMITTED; counts are never invented.
  scale     a rating option's scale value is its numeric label when the
            label itself is a number ("5" → 5), else its 1-based
            position in the option list (Strongly Agree → 5 … with the
            default scale, positions ARE the classic 1..5 Likert scale).

All formulas are LIVE — edit a count and the shares, totals, summary
and chart update. No ROUND() anywhere (display rounding belongs to the
number format).
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from app.services.patterns.utils import PCT_FMT, _pick, to_number

# Registry key — must match the pattern stanza in
# prompts/pattern_classifier.md.
PATTERN_NAME = "survey_results"

PATTERN_DESCRIPTION = (
    "Survey / poll / questionnaire results tabulation: one block per "
    "question with response counts, guarded share percentages, a summary "
    "sheet with respondents and rating averages, and a chart of the top "
    "question. Use when the user provides survey or poll response counts. "
    "Do NOT use for student grades."
)

# Routing keywords/stems — drive the cheap pre-gate and the classifier
# shortlist (see excel_gen._shortlist_patterns).
PATTERN_KEYWORDS = (
    "survey",
    "poll",
    "questionnaire",
    "respondent",
    "responses",
    "feedback form",
    "vote count",
    "votes",
)

MAX_QUESTIONS = 12
MAX_OPTIONS = 12
QUESTIONS_PER_SHEET = 6  # tables-per-sheet cap → paginate blocks

# Default option lists (stanza: options null → these).
RATING_SCALE = (
    "Strongly Agree",
    "Agree",
    "Neutral",
    "Disagree",
    "Strongly Disagree",
)
YESNO_SCALE = ("Yes", "No", "No answer")

TYPE_LABELS = {"rating": "rating", "choice": "choice", "yesno": "yes/no"}

# Design tokens (same palette as the converter).
NAVY = "16304F"
STEEL = "1B3A5C"
GOLD = "C9A227"
MUTED = "5C6470"

# Excel's classic Good / Bad / Neutral conditional-format palettes.
CF_GOLD_FILL = "FFF2CC"
CF_GOLD_TEXT = "7F6000"


def _slug(text: str, fallback: str) -> str:
    s = re.sub(r"[^\w\s-]", "", str(text).strip().lower())
    s = re.sub(r"[\s_-]+", "_", s).strip("_")
    return s[:40] or fallback


def _clean_text(value: Any, limit: int) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())[:limit]


def _normalize_type(value: Any) -> str:
    s = (
        str(value or "")
        .strip()
        .lower()
        .replace("-", "")
        .replace("_", "")
        .replace(" ", "")
        .replace("/", "")
    )
    aliases = {
        "rating": "rating",
        "scale": "rating",
        "ratingscale": "rating",
        "likert": "rating",
        "stars": "rating",
        "star": "rating",
        "choice": "choice",
        "choices": "choice",
        "multiplechoice": "choice",
        "singlechoice": "choice",
        "select": "choice",
        "options": "choice",
        "mc": "choice",
        "yesno": "yesno",
        "boolean": "yesno",
        "bool": "yesno",
        "binary": "yesno",
        "truefalse": "yesno",
    }
    return aliases.get(s, "choice")


def _numeric_key(key: str) -> Optional[float]:
    try:
        return float(key)
    except (TypeError, ValueError):
        return None


def _scale_value(option: str, position: int) -> float:
    """A rating option's scale value: its numeric label when the label
    itself is a number ("5" → 5), else its 1-based position."""
    n = _numeric_key(str(option).strip())
    return n if n is not None else float(position)


# ── Param coercion ────────────────────────────────────────────────────


def _normalize_question(entry: Any) -> Optional[dict]:
    """One questions entry → usable question dict, or None (omitted).

    A question without any counts given is omitted (stanza rule).
    """
    if not isinstance(entry, dict):
        return None
    text = _clean_text(_pick(entry, "question", "text", "prompt", "title"), 160)
    if not text:
        return None

    qtype = _normalize_type(_pick(entry, "type", "question_type", "kind"))

    raw_responses = _pick(entry, "responses", "counts", "response_counts", "results")
    responses: Dict[str, float] = {}
    if isinstance(raw_responses, dict):
        for key, value in raw_responses.items():
            n = to_number(value)
            if n is None:
                continue
            responses[str(key).strip()] = max(0.0, n)
    if not responses:
        return None  # no counts given → question omitted

    raw_options = _pick(entry, "options", "option_list", "choices", "answers")
    options: List[str] = []
    if isinstance(raw_options, list):
        seen = set()
        for opt in raw_options[:MAX_OPTIONS]:
            s = _clean_text(opt, 60)
            if not s or s.lower() in seen:
                continue
            seen.add(s.lower())
            options.append(s)

    keys = list(responses.keys())
    if not options:
        if qtype == "rating":
            # default 5-point Agree scale unless the request's own labels
            # (the response keys) clearly differ
            lower_scale = {o.lower() for o in RATING_SCALE}
            if keys and not any(k.lower() in lower_scale for k in keys):
                options = keys
            else:
                options = list(RATING_SCALE)
        elif qtype == "yesno":
            lower_scale = {o.lower() for o in YESNO_SCALE}
            if keys and not any(k.lower() in lower_scale for k in keys):
                options = keys
            else:
                options = list(YESNO_SCALE)
        else:
            # choice without options: the response keys ARE the options
            options = keys
        if all(_numeric_key(k) is not None for k in options) and options:
            options = sorted(options, key=_numeric_key)  # "1".."5" ascending
        options = options[:MAX_OPTIONS]
    if not options:
        return None

    counts: List[float] = []
    for opt in options:
        n = responses.get(opt)
        if n is None:
            for key, value in responses.items():
                if key.lower() == opt.lower():
                    n = value
                    break
        counts.append(float(n) if n is not None else 0.0)

    return {
        "question": text,
        "type": qtype,
        "options": options,
        "counts": counts,
        "total": sum(counts),
        # scale values only matter for rating questions
        "scales": [_scale_value(opt, i + 1) for i, opt in enumerate(options)],
    }


def coerce_survey_results_params(params: dict) -> dict:
    """Validate + normalize classifier params; raises ValueError."""
    if not isinstance(params, dict):
        raise ValueError("params must be an object")

    survey_name = (
        _clean_text(
            _pick(params, "survey_name", "survey", "title", "name", "poll_name"), 60
        )
        or "Survey"
    )

    raw_questions = _pick(params, "questions", "question_list", "poll_questions")
    questions: List[dict] = []
    if raw_questions is not None:
        if not isinstance(raw_questions, list):
            raise ValueError("questions must be an array")
        for entry in raw_questions[:MAX_QUESTIONS]:
            normalized = _normalize_question(entry)
            if normalized is not None:
                questions.append(normalized)

    # Never invent counts — without counted questions there is nothing
    # to tabulate (caller falls back to the AI spec path).
    if not questions:
        raise ValueError("no questions with response counts")

    notes = _pick(params, "notes", "note")
    notes = notes.strip()[:1000] if isinstance(notes, str) and notes.strip() else None

    return {"survey_name": survey_name, "questions": questions, "notes": notes}


# ── Builder ───────────────────────────────────────────────────────────


def build_survey_results_spec(params: dict) -> dict:
    """Survey results workbook — every formula code-generated.

    Layout (rows computed here, never guessed by a model):

    Survey / Survey2 sheets:
      row 1      title block, row 2 usage hint
      row 4-     one block per question: table title "Qn. text (type)",
                 header (Option | Responses | Share %), one row per
                 option, Total row (SUM); the next block starts two
                 rows below the previous total row
    Summary sheet:
      rows 5-    one row per question: respondents reference the block
                 total; rating questions get the live SUMPRODUCT average
      total row  SUM of respondents across questions
      chart      ONE summary chart (pie for a rating question, else a
                 horizontal bar) of the top question's counts
    Calc sheet (hidden, rating questions only):
      row 5-     one column per rating question holding the option scale
                 values at exactly that question's option rows
    """
    p = coerce_survey_results_params(params)
    survey_name: str = p["survey_name"]
    questions: List[dict] = p["questions"]
    notes = p["notes"]

    n_q = len(questions)

    # ── data sheets: blocks of ≤6 tables, rows computed up front ────
    # block geometry for question i (0-based, qnum = i+1)
    first_rows: List[int] = []
    last_rows: List[int] = []
    total_rows: List[int] = []
    r = 4
    for q in questions:
        first = r + 2  # title r, header r+1, first option row r+2
        last = first + len(q["options"]) - 1
        total = last + 1
        first_rows.append(first)
        last_rows.append(last)
        total_rows.append(total)
        r = total + 2

    def sheet_of(qnum: int) -> str:
        return "Survey" if qnum <= QUESTIONS_PER_SHEET else "Survey2"

    survey_sheet = _data_sheet(
        title_text=f"{survey_name} — Results",
        title_color=NAVY,
        font_size=14,
        hint=(
            "One block per question. Responses are the counts from the "
            "request; Share % divides by the question total (n/a when "
            "nobody answered). The most-chosen option is highlighted."
        ),
        tab_color=NAVY,
        questions=questions[:QUESTIONS_PER_SHEET],
        first_qnum=1,
        first_rows=first_rows[:QUESTIONS_PER_SHEET],
        last_rows=last_rows[:QUESTIONS_PER_SHEET],
        total_rows=total_rows[:QUESTIONS_PER_SHEET],
        notes=notes
        or (
            "Tabulation of the survey's response counts. Each block totals "
            "with live SUM formulas; Share % is guarded against zero "
            "totals. See the Summary sheet for respondents per question "
            "and average ratings, and the Calc sheet (hidden) for the "
            "rating scale helpers."
        ),
    )
    sheets: List[dict] = [survey_sheet]

    if n_q > QUESTIONS_PER_SHEET:
        rest = questions[QUESTIONS_PER_SHEET:]
        sheets.append(
            _data_sheet(
                title_text=f"{survey_name} — Continued",
                title_color=STEEL,
                font_size=12,
                hint="Questions 7 and onward — same layout as the Survey tab.",
                tab_color=STEEL,
                questions=rest,
                first_qnum=QUESTIONS_PER_SHEET + 1,
                first_rows=first_rows[QUESTIONS_PER_SHEET:],
                last_rows=last_rows[QUESTIONS_PER_SHEET:],
                total_rows=total_rows[QUESTIONS_PER_SHEET:],
                notes=(
                    "Continuation of the Survey sheet (blocks 7+). Shares, "
                    "totals and the Summary references stay live."
                ),
            )
        )

    # ── Calc sheet (hidden): rating scale helper columns ────────────
    rating_idx = [i for i, q in enumerate(questions) if q["type"] == "rating"]
    calc_sheet: Optional[dict] = None
    if rating_idx:
        max_row = max(last_rows[i] for i in rating_idx)
        calc_first = 6  # header row 5, data rows 6.. aligned with blocks
        n_rows = max_row - calc_first + 1
        grid: List[List[Any]] = [[None] * len(rating_idx) for _ in range(n_rows)]
        for c, i in enumerate(rating_idx):
            q = questions[i]
            for k in range(len(q["options"])):
                grid[first_rows[i] - calc_first + k][c] = q["scales"][k]
        calc_sheet = {
            "name": "Calc",
            "hidden": True,
            "column_widths": {_col(c + 1): 10 for c in range(len(rating_idx))},
            "text_blocks": [
                {
                    "cell": "A1",
                    "text": (
                        "Hidden helpers — rating option scale values, "
                        "aligned 1:1 with each question's option rows."
                    ),
                    "italic": True,
                    "font_color": MUTED,
                },
                {
                    "cell": "A2",
                    "text": (
                        "One column per rating question (Qn scale). A "
                        "value is the option's numeric label when it is a "
                        "number, else its 1-based position — the default "
                        "Agree scale therefore reads as the classic 1..5."
                    ),
                    "italic": True,
                    "font_color": MUTED,
                },
            ],
            "tables": [
                {
                    "start_cell": "A5",
                    "headers": [f"Q{i + 1} scale" for i in rating_idx],
                    "rows": grid,
                    "number_formats": {
                        _col(c + 1): "0" for c in range(len(rating_idx))
                    },
                }
            ],
            "notes": (
                "Hidden calculation sheet. Each column holds one rating "
                "question's option scale values at exactly the rows that "
                "question's options occupy on its Survey sheet, so the "
                "Summary's Average Rating is a live "
                "SUMPRODUCT(scale, counts)/total. Right-click a tab → "
                "Unhide to inspect."
            ),
        }

    # ── Summary sheet ────────────────────────────────────────────────
    sm_first = 5
    sm_last = 4 + n_q
    sm_total = sm_last + 1  # noqa
    sm_rows: List[List[Any]] = []
    for i, q in enumerate(questions):
        qnum = i + 1
        sn = sheet_of(qnum)
        tr = total_rows[i]
        if q["type"] == "rating" and calc_sheet is not None:
            c = _col(rating_idx.index(i) + 1)
            avg = (
                f'=IF({sn}!$B${tr}=0,"n/a",'
                f"SUMPRODUCT(Calc!${c}${first_rows[i]}:${c}${last_rows[i]},"
                f"{sn}!$B${first_rows[i]}:$B${last_rows[i]})/{sn}!$B${tr})"
            )
        else:
            avg = "—"
        sm_rows.append(
            [
                f"Q{qnum}. {q['question'][:80]}",
                TYPE_LABELS[q["type"]],
                f"={sn}!$B${tr}",
                avg,
            ]
        )

    # ONE summary chart: the question with the most responses (ties →
    # the first). Pie for a rating question, else a horizontal bar —
    # long option labels read better horizontally.
    top_i = max(range(n_q), key=lambda i: (questions[i]["total"], -i))
    top_q = questions[top_i]
    top_sn = sheet_of(top_i + 1)
    summary_chart = {
        "type": "pie" if top_q["type"] == "rating" else "bar_h",
        "title": f"Q{top_i + 1}: {top_q['question'][:60]}",
        "anchor": "F3",
        "width": 14,
        "height": 9,
        "categories_range": (f"{top_sn}!A{first_rows[top_i]}:A{last_rows[top_i]}"),
        "series": [
            {
                "name": "Responses",
                "values_range": (f"{top_sn}!B{first_rows[top_i]}:B{last_rows[top_i]}"),
            }
        ],
        "value_numfmt": "0",
    }

    summary_sheet: Dict[str, Any] = {
        "name": "Summary",
        "tab_color": STEEL,
        "column_widths": {"A": 46, "B": 12, "C": 14, "D": 14},
        "text_blocks": [
            {
                "cell": "A1",
                "text": f"{survey_name} — Summary",
                "bold": True,
                "font_size": 12,
                "font_color": STEEL,
            },
        ],
        "tables": [
            {
                "start_cell": "A3",
                "title": "Question Summary",
                "headers": ["Question", "Type", "Respondents", "Average Rating"],
                "rows": sm_rows,
                "total_row": [
                    "Total responses",
                    "",
                    f"=SUM(C{sm_first}:C{sm_last})",
                    "",
                ],
                "number_formats": {"C": "0", "D": "0.00"},
                "alignments": {"B": "center", "C": "center", "D": "center"},
            }
        ],
        "charts": [summary_chart],
        "notes": (
            "Respondents reference each question's Total row on the Survey "
            "sheet (live). Average Rating is SUMPRODUCT(scale, counts) / "
            "total using the hidden Calc sheet's scale columns — only "
            "rating questions have one. The chart shows the top question "
            "by total responses."
        ),
    }

    sheets.append(summary_sheet)
    if calc_sheet is not None:
        sheets.append(calc_sheet)

    return {
        "filename": f"{_slug(survey_name, 'survey')}_survey_results.xlsx",
        "sheets": sheets,
    }


def _col(idx: int) -> str:
    """1-based column index → Excel column letters (1 → "A")."""
    s = ""
    n = int(idx)
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def _data_sheet(
    title_text: str,
    title_color: str,
    font_size: int,
    hint: str,
    tab_color: str,
    questions: List[dict],
    first_qnum: int,
    first_rows: List[int],
    last_rows: List[int],
    total_rows: List[int],
    notes: Optional[str],
) -> dict:
    """One data sheet: stacked per-question blocks with live formulas."""
    tables: List[dict] = []
    cfs: List[dict] = []
    for k, q in enumerate(questions):
        qnum = first_qnum + k
        first, last, total = first_rows[k], last_rows[k], total_rows[k]
        rows: List[List[Any]] = []
        for oi, option in enumerate(q["options"]):
            rr = first + oi
            rows.append(
                [
                    option,
                    q["counts"][oi],
                    f'=IF($B${total}>0,B{rr}/$B${total},"n/a")',
                ]
            )
        tables.append(
            {
                "start_cell": f"A{first - 2}",
                "title": f"Q{qnum}. {q['question'][:80]} ({TYPE_LABELS[q['type']]})",
                "headers": ["Option", "Responses", "Share %"],
                "rows": rows,
                "total_row": [
                    "Total",
                    f"=SUM(B{first}:B{last})",
                    f'=IF($B${total}>0,1,"n/a")',
                ],
                "number_formats": {"B": "0", "C": PCT_FMT},
                "alignments": {"B": "center", "C": "center"},
            }
        )
        # gold highlight on the most-chosen option (guarded: needs >0)
        cfs.append(
            {
                "range": f"B{first}:C{last}",
                "rules": [
                    {
                        "type": "formula",
                        "formula": (
                            f"AND($B{first}=MAX($B${first}:$B${last})," f"$B{first}>0)"
                        ),
                        "fill": CF_GOLD_FILL,
                        "font_color": CF_GOLD_TEXT,
                        "bold": True,
                    }
                ],
            }
        )

    return {
        "name": "Survey" if first_qnum == 1 else "Survey2",
        "tab_color": tab_color,
        "freeze_panes": "A4",  # title + hint stay visible while scrolling
        "column_widths": {"A": 34, "B": 14, "C": 12},
        "text_blocks": [
            {
                "cell": "A1",
                "text": title_text,
                "bold": True,
                "font_size": font_size,
                "font_color": title_color,
            },
            {
                "cell": "A2",
                "text": hint,
                "italic": True,
                "font_color": MUTED,
            },
        ],
        "tables": tables,
        "conditional_formats": cfs,
        "notes": notes,
    }


# ── Standard pattern entry points (used by the dynamic registry) ──────

coerce_params = coerce_survey_results_params
build_spec = build_survey_results_spec
