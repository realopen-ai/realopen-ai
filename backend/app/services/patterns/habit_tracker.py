"""
Habit tracker pattern — daily habit / routine tracking (deterministic).

A complete, self-updating habit-tracker workbook for up to 10 habits:

  Tracker        Date | Day | habit 1..10 (✓/x dropdown marks) |
                 Daily Score | Daily % | Notes — dates pre-filled
                 for the current month + the next month, today's row
                 highlighted gold, ✓ green / x red, panes frozen so
                 the dates + headers stay visible while scrolling
  Dashboard      live month stats (overall completion rate, habit
                 completions, perfect days, active habits), best
                 performing + needs-attention habit, per-habit streak
                 table, a completion-rate bar chart and a daily
                 completion trend line chart
  Habits         Habit Name | Target | Active | Start Date | Current
                 Streak | Best Streak | Total Completions |
                 Completion Rate % | Color — users edit only the
                 input columns; every stat is a live formula
  Instructions   plain-English how-to for non-power users
  Calc (hidden)  active-habit flags, the today-anchor, streak helper
                 columns aligned 1:1 with the Tracker rows, and the
                 per-habit stats block the visible sheets reference

CALCULATION SEMANTICS (all LIVE formulas — nothing is frozen at
build time; marking a day updates everything):

  helper(r)   = consecutive ✓ days ending on Tracker row r
                (=IF(mark="✓", helper(r-1)+1, 0))
  today pos   = approximate MATCH of TODAY() in the date column —
                lands exactly on today while the tracker covers it,
                and clamps to the last row once the file goes stale
  current     = helper at today's row when today carries any mark,
                else helper at yesterday's row — an unmarked today
                never breaks a streak; a marked x or a skipped past
                day does
  best        = MAX(helper column) across the whole tracker
  day score   = SUMPRODUCT of ✓ marks weighted by the active-habit
                flags — inactive habits never count; Daily % divides
                by the number of active habits
  month stats = scoped to the tracker's FIRST month block (labeled
                with the month name); streaks span the whole tracker

Every formula reference is computed from the actual layout rows this
module emits, so off-by-N row math is impossible by construction. No
ROUND() anywhere — display rounding is the number format's job.
"""

from __future__ import annotations

import calendar
from datetime import date
from typing import Any, Dict, List, Optional

from app.services.patterns.utils import (
    _pick,
    to_iso_date,
)

# Registry key — must match the pattern stanza in
# prompts/pattern_classifier.md.
PATTERN_NAME = "habit_tracker"

PATTERN_DESCRIPTION = (
    "Creates a daily habit tracker spreadsheet: mark habits ✓/x each "
    "day and get automatic streaks, daily scores, completion rates, a "
    "dashboard with charts, dropdowns, color coding and an "
    "instructions tab. Use when the user wants to track daily habits, "
    "routines or streaks. Do not use it for financial budgets, loans, "
    "invoices or investment portfolios."
)

# Routing keywords/stems — drive the cheap pre-gate and the classifier
# shortlist (see excel_gen._shortlist_patterns).
PATTERN_KEYWORDS = (
    "habit",
    "streak",
    "routine",
)

MAX_HABITS = 10
MONTH_WINDOW = 24  # months a requested month_start may sit away from today

_DATE_FMT = "yyyy-mm-dd"
_PCT_FMT = "0%"
_INT_FMT = "0"

# Tracker column letters for habit 1..10 (fixed layout).
HABIT_LETTERS = ("C", "D", "E", "F", "G", "H", "I", "J", "K", "L")

# Design tokens (same palette as the converter).
NAVY = "16304F"
STEEL = "1B3A5C"
GOLD = "C9A227"
MUTED = "5C6470"
GREY = "8A94A3"

# Excel's classic Good / Bad / Neutral conditional-format palettes —
# instantly readable in every viewer.
CF_GREEN_FILL = "C6EFCE"
CF_GREEN_TEXT = "006100"
CF_RED_FILL = "FFC7CE"
CF_RED_TEXT = "9C0006"
CF_GOLD_FILL = "FFF2CC"
CF_GOLD_TEXT = "7F6000"

# English month names — strftime("%B") is locale-dependent, and the
# workbook language is English regardless of the server's locale.
_MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


# ── Param coercion ────────────────────────────────────────────────────


def _normalize_habit_entry(entry: Any) -> Optional[dict]:
    """One habits entry → {name, target, active, start_date} or None.

    Accepts the documented object form and bare habit-name strings.
    """
    if isinstance(entry, str):
        name = entry.strip()
        target = "Daily"
        active = True
        start = None
    elif isinstance(entry, dict):
        name = str(_pick(entry, "name", "habit", "title") or "").strip()
        target = (
            str(_pick(entry, "target", "frequency", "schedule") or "Daily")
            .strip()
            .capitalize()
        )
        if target not in ("Daily", "Weekdays", "Custom"):
            target = "Daily"
        active = _pick(entry, "active", "enabled")
        active = True if active is None else bool(active)
        start = to_iso_date(_pick(entry, "start_date", "started"))
    else:
        return None

    if not name:
        return None
    return {
        "name": name[:40],
        "target": target,
        "active": active,
        "start_date": start,
    }


def coerce_habit_tracker_params(params: dict) -> dict:
    """Validate + normalize classifier params; raises ValueError."""
    if not isinstance(params, dict):
        raise ValueError("params must be an object")

    today = date.today()

    month_start = to_iso_date(_pick(params, "month_start", "start_date", "start_month"))
    if month_start is not None:
        y, m, _d = (int(x) for x in month_start.split("-"))
        first = date(y, m, 1)
        delta = (first.year - today.year) * 12 + (first.month - today.month)
        if abs(delta) > MONTH_WINDOW:
            first = date(today.year, today.month, 1)
    else:
        first = date(today.year, today.month, 1)

    raw_habits = _pick(params, "habits", "habit_list")
    habits: List[dict] = []
    if raw_habits is not None:
        if not isinstance(raw_habits, list):
            raise ValueError("habits must be an array")
        for entry in raw_habits[:MAX_HABITS]:
            normalized = _normalize_habit_entry(entry)
            if normalized is not None:
                habits.append(normalized)

    if not habits:
        # Blank slate: ten active generic slots the user renames.
        habits = [
            {
                "name": f"Habit {i}",
                "target": "Daily",
                "active": True,
                "start_date": None,
            }
            for i in range(1, MAX_HABITS + 1)
        ]
    else:
        # Pad to the fixed 10-row layout. Filler habits are INACTIVE so
        # daily scores only count the habits the user actually listed;
        # activating a row is a single dropdown flip on the Habits tab.
        while len(habits) < MAX_HABITS:
            habits.append(
                {
                    "name": f"Habit {len(habits) + 1}",
                    "target": "Daily",
                    "active": False,
                    "start_date": None,
                }
            )

    notes = _pick(params, "notes", "note")
    notes = notes.strip()[:1000] if isinstance(notes, str) and notes.strip() else None

    return {"month_start": first, "habits": habits, "notes": notes}


# ── Builder ───────────────────────────────────────────────────────────


def build_habit_tracker_spec(params: dict) -> dict:
    """Habit tracker workbook — every formula code-generated.

    Layout (rows computed here, never guessed by a model):

    Tracker sheet:
      row 1      title
      row 2      usage hint
      row 4      headers: Date | Day | Habits!A5..A14 (live names) |
                 Daily Score | Daily % | Notes
      rows 5..   dates for month 1 + month 2, one row per day;
                 B = weekday (CHOOSE/WEEKDAY), M = day score
                 (SUMPRODUCT over ✓ x active flags), N = M / active
    Habits sheet:
      rows 5-14  one row per habit; stats columns are Calc references
    Calc sheet (hidden):
      C1..L1     active flags (=IF(Habits!C{r}="Yes",1,0)), N1 = sum
      P1         today's row position (approximate MATCH, clamped)
      P2         days elapsed in month 1 (COUNTIF <= TODAY())
      C4 table   streak helpers, rows aligned 1:1 with the Tracker
      A{S} table per-habit stats: current / best / totals / rates
    Dashboard:
      month stats, best/needs-attention INDEX-MATCH picks, the
      streak table (rows 18..27) and two charts
    """
    p = coerce_habit_tracker_params(params)
    first: date = p["month_start"]
    habits: List[dict] = p["habits"]
    notes = p["notes"]

    # Month 2 = the month right after month 1.
    y2, m2 = (first.year + 1, 1) if first.month == 12 else (first.year, first.month + 1)
    n_m1 = calendar.monthrange(first.year, first.month)[1]
    n_m2 = calendar.monthrange(y2, m2)[1]
    n_days = n_m1 + n_m2

    month1_name = f"{_MONTHS[first.month - 1]} {first.year}"
    month2_name = f"{_MONTHS[m2 - 1]} {y2}"

    # ── geometry ────────────────────────────────────────────────────
    r0 = 5  # first data row (Tracker & Calc helpers)
    rM1 = 4 + n_m1  # last data row of month 1
    rN = 4 + n_days  # last data row (both months)
    S = rN + 3  # Calc stats table header row
    habits_row = lambda i: 5 + i  # noqa: Habits sheet row of habit i (0-based)
    stats_row = lambda i: S + 1 + i  # noqa: Calc stats row of habit i (0-based)
    streak_row = lambda i: 18 + i  # noqa: Dashboard streak-table row (0-based)

    dates: List[date] = [date(first.year, first.month, d) for d in range(1, n_m1 + 1)]
    dates += [date(y2, m2, d) for d in range(1, n_m2 + 1)]

    # ═════════════════════════════ Tracker sheet ═══════════════════
    tracker_rows: List[List[Any]] = []
    for i, d in enumerate(dates):
        r = r0 + i
        tracker_rows.append(
            [
                d.isoformat(),  # A Date — converter turns it into a real date
                '=CHOOSE(WEEKDAY(A{r},2),"Mon","Tue","Wed","Thu","Fri","Sat","Sun")'.format(
                    r=r
                ),
                # C..L: the user's ✓/x marks (dropdown + conditional colors)
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                # M Daily Score — ✓ count over ACTIVE habits only
                '=SUMPRODUCT((C{r}:L{r}="✓")*Calc!$C$1:$L$1)'.format(r=r),
                # N Daily % — score ÷ active habits
                "=IF(Calc!$N$1=0,0,M{r}/Calc!$N$1)".format(r=r),
                None,  # O Notes
            ]
        )

    habit_headers = [
        "=Habits!$A${r}".format(r=habits_row(i)) for i in range(MAX_HABITS)
    ]

    tracker_sheet: Dict[str, Any] = {
        "name": "Tracker",
        "tab_color": NAVY,
        "freeze_panes": "C5",  # keep the date/day columns + header rows visible
        "column_widths": {
            "A": 13,
            "B": 7,
            **{letter: 16 for letter in HABIT_LETTERS},
            "M": 10,
            "N": 8,
            "O": 28,
        },
        "text_blocks": [
            {
                "cell": "C1",
                "text": f"Habit Tracker — {month1_name} + {month2_name}",
                "bold": True,
                "font_size": 14,
                "font_color": NAVY,
            },
            {
                "cell": "C2",
                "text": (
                    "Mark each habit ✓ (done) or x (missed) in its column — "
                    "scores, streaks and the Dashboard update automatically. "
                    "See the Instructions tab."
                ),
                "italic": True,
                "font_color": MUTED,
            },
        ],
        "tables": [
            {
                "start_cell": "A4",
                "headers": ["Date", "Day"]
                + habit_headers
                + [
                    "Daily Score",
                    "Daily %",
                    "Notes",
                ],
                "rows": tracker_rows,
                "number_formats": {"A": _DATE_FMT, "M": _INT_FMT, "N": _PCT_FMT},
                "alignments": {
                    "A": "center",
                    "B": "center",
                    "M": "center",
                    "N": "center",
                    **{letter: "center" for letter in HABIT_LETTERS},
                },
            }
        ],
        "data_validation": [
            {
                "range": f"C{r0}:L{rN}",
                "values": ["✓", "x"],
                "allow_blank": True,
                "prompt_title": "Habit mark",
                "prompt": "✓ = done · x = missed · blank = pending",
                "error_title": "Invalid mark",
                "error": "Pick ✓ (done) or x (missed), or clear the cell.",
                "error_style": "stop",
            }
        ],
        # Order matters: ✓/x rules carry stop_if_true so the today-row
        # gold rule can't repaint a colored mark cell.
        "conditional_formats": [
            {
                "range": f"C{r0}:L{rN}",
                "rules": [
                    {
                        "type": "cell_is",
                        "operator": "equal",
                        "value": "✓",
                        "fill": CF_GREEN_FILL,
                        "font_color": CF_GREEN_TEXT,
                        "bold": True,
                        "stop_if_true": True,
                    },
                    {
                        "type": "cell_is",
                        "operator": "equal",
                        "value": "x",
                        "fill": CF_RED_FILL,
                        "font_color": CF_RED_TEXT,
                        "stop_if_true": True,
                    },
                ],
            },
            {
                "range": f"N{r0}:N{rN}",
                "rules": [
                    {
                        "type": "cell_is",
                        "operator": "equal",
                        "value": 1,
                        "fill": CF_GREEN_FILL,
                        "font_color": CF_GREEN_TEXT,
                        "stop_if_true": False,
                    }
                ],
            },
            {
                "range": f"A{r0}:O{rN}",
                "rules": [
                    {
                        "type": "formula",
                        "value": "$A5=TODAY()",
                        "fill": CF_GOLD_FILL,
                        "stop_if_true": False,
                    }
                ],
            },
        ],
        "protect": {
            "unlocked_ranges": [f"C{r0}:L{rN}", f"O{r0}:O{rN}"],
        },
        "notes": notes
        or (
            "Mark habits with ✓ (done) or x (missed); blank leaves a day "
            "pending. Daily Score counts ✓ across ACTIVE habits, Daily % "
            "divides by the number of active habits. Today's row is "
            "highlighted gold. Formula cells are locked — only the mark and "
            "Notes columns are editable."
        ),
    }

    # ═════════════════════════════ Calc sheet (hidden) ═══════════════
    calc_blocks: List[dict] = [
        {
            "cell": "A1",
            "text": "Active flags (1 = habit active) — N1 = number of active habits",
            "italic": True,
            "font_color": MUTED,
        },
        {
            "cell": "A2",
            "text": "P1 = today's row position · P2 = days elapsed in month 1",
            "italic": True,
            "font_color": MUTED,
        },
        {
            "cell": "A3",
            "text": "Streak helpers run 1:1 with Tracker rows; stats tables below",
            "italic": True,
            "font_color": MUTED,
        },
    ]
    for i in range(MAX_HABITS):
        calc_blocks.append(
            {
                "cell": f"{HABIT_LETTERS[i]}1",
                "text": '=IF(Habits!$C${r}="Yes",1,0)'.format(r=habits_row(i)),
            }
        )
    calc_blocks.append({"cell": "N1", "text": "=SUM(C1:L1)"})
    calc_blocks.append(
        {
            "cell": "P1",
            # Approximate MATCH: exact today while the tracker covers it,
            # clamps to the last row when the file goes stale, #N/A → 0
            # only for pathological clock rollback.
            "text": "=IFERROR(MATCH(TODAY(),Tracker!$A${r0}:$A${rN},1),0)".format(
                r0=r0, rN=rN
            ),
        }
    )
    calc_blocks.append(
        {
            "cell": "P2",
            "text": '=COUNTIF(Tracker!$A${r0}:$A${rM1},"<="&TODAY())'.format(
                r0=r0, rM1=rM1
            ),
        }
    )

    # Streak helpers — rows aligned 1:1 with the Tracker's data rows.
    helper_rows: List[List[Any]] = []
    for i in range(n_days):
        r = r0 + i
        row: List[Any] = []
        for letter in HABIT_LETTERS:
            if r == r0:
                row.append('=IF(Tracker!{L}{r}="✓",1,0)'.format(L=letter, r=r))
            else:
                row.append(
                    '=IF(Tracker!{L}{r}="✓",{L}{prev}+1,0)'.format(
                        L=letter, r=r, prev=r - 1
                    )
                )
        helper_rows.append(row)

    # Per-habit stats (rows S+1..S+10, one row per habit).
    stats_rows: List[List[Any]] = []
    for i in range(MAX_HABITS):
        letter = HABIT_LETTERS[i]
        r = stats_row(i)
        # Current streak: the helper value at today's row when today is
        # marked, else yesterday's helper — an unmarked today never
        # breaks a streak. ISTEXT discriminates marks from empty cells
        # (INDEX of an empty cell numeric-coerces to 0, not "").
        current = (
            "=IF($P$1=0,0,"
            "IF(ISTEXT(INDEX(Tracker!{L}${r0}:{L}${rN},$P$1)),"
            "INDEX({L}${r0}:{L}${rN},$P$1),"
            "IF($P$1=1,0,INDEX({L}${r0}:{L}${rN},$P$1-1))))"
        ).format(L=letter, r0=r0, rN=rN)
        stats_rows.append(
            [
                "=Habits!$A${r}".format(r=habits_row(i)),
                current,
                "=MAX({L}{r0}:{L}{rN})".format(L=letter, r0=r0, rN=rN),
                '=COUNTIF(Tracker!{L}{r0}:{L}{rN},"✓")'.format(L=letter, r0=r0, rN=rN),
                '=COUNTIF(Tracker!{L}{r0}:{L}{rM1},"✓")'.format(
                    L=letter, r0=r0, rM1=rM1
                ),
                "=IF($P$2=0,0,E{r}/$P$2)".format(r=r),
                # Attention mask: inactive habits never win "needs
                # attention" (their rate would read as a flat 0).
                '=IF(Habits!$C${hr}="Yes",F{r},9)'.format(hr=habits_row(i), r=r),
            ]
        )

    calc_sheet: Dict[str, Any] = {
        "name": "Calc",
        "tab_color": GREY,
        "hidden": True,
        "column_widths": {"A": 24, "B": 15, "C": 9, "D": 11, "E": 12, "F": 12, "G": 17},
        "text_blocks": calc_blocks,
        "tables": [
            {
                "start_cell": "C4",
                "headers": [f"Habit {i + 1}" for i in range(MAX_HABITS)],
                "rows": helper_rows,
                "number_formats": {},
                "zebra": False,
            },
            {
                "start_cell": f"A{S}",
                "headers": [
                    "Habit",
                    "Current Streak",
                    "Best Streak",
                    "Total ✓",
                    "✓ This Month",
                    "Completion Rate",
                    "Rate (attention mask)",
                ],
                "rows": stats_rows,
                "number_formats": {
                    "B": _INT_FMT,
                    "C": _INT_FMT,
                    "D": _INT_FMT,
                    "E": _INT_FMT,
                    "F": _PCT_FMT,
                },
                "alignments": {
                    "B": "center",
                    "C": "center",
                    "D": "center",
                    "E": "center",
                    "F": "center",
                },
            },
        ],
        "protect": True,
        "notes": (
            "Hidden calculation sheet. Row 1: active-habit flags aligned "
            "with the Tracker's habit columns C..L (N1 counts them). P1 = "
            "today's row position (approximate MATCH — clamps to the last "
            "row once the file goes stale). P2 = days elapsed in month 1. "
            "The streak helpers (C..L) run 1:1 with the Tracker's rows: "
            "each cell counts the consecutive ✓ days ending on its row. "
            "The stats table holds the per-habit numbers the Habits and "
            "Dashboard sheets display. Right-click the Calc tab → Unhide "
            "to inspect or extend them."
        ),
    }

    # ═════════════════════════════ Habits sheet ══════════════════════
    habits_rows: List[List[Any]] = []
    for i, h in enumerate(habits):
        habits_rows.append(
            [
                h["name"],
                h["target"],
                "Yes" if h["active"] else "No",
                h["start_date"] or first.isoformat(),
                "=Calc!$B${r}".format(r=stats_row(i)),
                "=Calc!$C${r}".format(r=stats_row(i)),
                "=Calc!$D${r}".format(r=stats_row(i)),
                "=Calc!$F${r}".format(r=stats_row(i)),
                "",  # Color — free label for the user
            ]
        )

    habits_sheet: Dict[str, Any] = {
        "name": "Habits",
        "tab_color": STEEL,
        "freeze_panes": "A5",
        "column_widths": {
            "A": 26,
            "B": 12,
            "C": 10,
            "D": 13,
            "E": 14,
            "F": 12,
            "G": 16,
            "H": 16,
            "I": 12,
        },
        "text_blocks": [
            {
                "cell": "A1",
                "text": "Habits",
                "bold": True,
                "font_size": 14,
                "font_color": NAVY,
            },
            {
                "cell": "A2",
                "text": (
                    "Edit only Name, Target, Active, Start Date and Color — "
                    "the streak and completion columns update automatically."
                ),
                "italic": True,
                "font_color": MUTED,
            },
        ],
        "tables": [
            {
                "start_cell": "A4",
                "headers": [
                    "Habit Name",
                    "Target",
                    "Active",
                    "Start Date",
                    "Current Streak",
                    "Best Streak",
                    "Total Completions",
                    "Completion Rate %",
                    "Color",
                ],
                "rows": habits_rows,
                "number_formats": {
                    "D": _DATE_FMT,
                    "E": _INT_FMT,
                    "F": _INT_FMT,
                    "G": _INT_FMT,
                    "H": _PCT_FMT,
                },
                "alignments": {
                    "B": "center",
                    "C": "center",
                    "D": "center",
                    "E": "center",
                    "F": "center",
                    "G": "center",
                    "H": "center",
                },
            }
        ],
        "data_validation": [
            {
                "range": "B5:B14",
                "values": ["Daily", "Weekdays", "Custom"],
                "allow_blank": True,
                "prompt_title": "Target",
                "prompt": "When the habit is meant to happen.",
            },
            {
                "range": "C5:C14",
                "values": ["Yes", "No"],
                "allow_blank": True,
                "prompt_title": "Active",
                "prompt": "Yes = counted in the Tracker's daily scores.",
            },
        ],
        "conditional_formats": [
            {
                "range": "E5:E14",
                "rules": [
                    {
                        "type": "cell_is",
                        "operator": "greater_than_or_equal",
                        "value": 7,
                        "fill": CF_GOLD_FILL,
                        "font_color": CF_GOLD_TEXT,
                        "bold": True,
                        "stop_if_true": False,
                    }
                ],
            },
            {
                "range": "H5:H14",
                "rules": [
                    {
                        "type": "cell_is",
                        "operator": "greater_than_or_equal",
                        "value": 0.8,
                        "fill": CF_GREEN_FILL,
                        "font_color": CF_GREEN_TEXT,
                        "stop_if_true": False,
                    }
                ],
            },
        ],
        "protect": {
            "unlocked_ranges": ["A5:A14", "B5:B14", "C5:C14", "D5:D14", "I5:I14"],
        },
        "notes": (
            "Only the Name, Target, Active, Start Date and Color columns are "
            "editable — the stats columns are locked live formulas. Current "
            "Streak = consecutive ✓ days ending today (an unmarked today "
            "doesn't break it; a marked x or a skipped past day does). Best "
            "Streak = longest ✓ run in this file. Total Completions counts "
            "every ✓. Completion Rate = ✓ this month ÷ days elapsed this "
            "month. Set Active = No for unused rows — daily scores on the "
            "Tracker only count active habits."
        ),
    }

    # ═════════════════════════════ Dashboard sheet ═══════════════════
    month_guard = 'COUNTIF(Tracker!$C${r0}:$L${rM1},"✓")'.format(r0=r0, rM1=rM1)
    overall_rate = (
        "=IF((Calc!$P$2*Calc!$N$1)=0,0," "{guard}/(Calc!$P$2*Calc!$N$1))"
    ).format(guard=month_guard)
    best_habit = (
        '=IF({guard}=0,"—",' "INDEX($A$18:$A$27,MATCH(MAX($D$18:$D$27),$D$18:$D$27,0)))"
    ).format(guard=month_guard)
    attention_habit = (
        '=IF({guard}=0,"—",'
        "INDEX($A$18:$A$27,"
        "MATCH(MIN(Calc!$G${s1}:$G${s10}),Calc!$G${s1}:$G${s10},0)))"
    ).format(guard=month_guard, s1=stats_row(0), s10=stats_row(MAX_HABITS - 1))

    dashboard_blocks: List[dict] = [
        {
            "cell": "A1",
            "text": "Habit Dashboard",
            "bold": True,
            "font_size": 16,
            "font_color": NAVY,
        },
        {"cell": "A3", "text": "Today"},
        {
            "cell": "B3",
            "text": "=TODAY()",
            "number_format": _DATE_FMT,
            "bold": True,
        },
        {"cell": "A4", "text": "Month"},
        {"cell": "B4", "text": month1_name, "bold": True},
        {
            "cell": "A6",
            "text": "THIS MONTH",
            "bold": True,
            "font_size": 12,
            "font_color": GOLD,
        },
        {"cell": "A7", "text": "Overall Completion Rate"},
        {
            "cell": "B7",
            "text": overall_rate,
            "number_format": _PCT_FMT,
            "bold": True,
        },
        {"cell": "A8", "text": "Habits Completed"},
        {
            "cell": "B8",
            "text": "=" + month_guard,
            "number_format": _INT_FMT,
            "bold": True,
        },
        {"cell": "A9", "text": "Perfect Days"},
        {
            "cell": "B9",
            "text": "=COUNTIF(Tracker!$N${r0}:$N${rM1},1)".format(r0=r0, rM1=rM1),
            "number_format": _INT_FMT,
        },
        {"cell": "A10", "text": "Active Habits"},
        {"cell": "B10", "text": "=Calc!$N$1", "number_format": _INT_FMT},
        {"cell": "A11", "text": "Total Completions (both months)"},
        {
            "cell": "B11",
            "text": '=COUNTIF(Tracker!$C${r0}:$L${rN},"✓")'.format(r0=r0, rN=rN),
            "number_format": _INT_FMT,
        },
        {"cell": "A13", "text": "Best Performing Habit", "bold": True},
        {"cell": "B13", "text": best_habit, "bold": True, "font_color": CF_GREEN_TEXT},
        {"cell": "A14", "text": "Needs Most Attention", "bold": True},
        {
            "cell": "B14",
            "text": attention_habit,
            "bold": True,
            "font_color": CF_RED_TEXT,
        },
        {
            "cell": "A16",
            "text": "HABIT STREAKS",
            "bold": True,
            "font_size": 12,
            "font_color": GOLD,
        },
    ]

    streak_rows: List[List[Any]] = []
    for i in range(MAX_HABITS):
        streak_rows.append(
            [
                "=Habits!$A${r}".format(r=habits_row(i)),
                "=Calc!$B${r}".format(r=stats_row(i)),
                "=Calc!$C${r}".format(r=stats_row(i)),
                "=Calc!$F${r}".format(r=stats_row(i)),
            ]
        )

    dashboard_sheet: Dict[str, Any] = {
        "name": "Dashboard",
        "tab_color": GOLD,
        "no_freeze": True,
        "column_widths": {"A": 28, "B": 16, "C": 13, "D": 17},
        "text_blocks": dashboard_blocks,
        "tables": [
            {
                "start_cell": "A17",
                "headers": [
                    "Habit",
                    "Current Streak",
                    "Best Streak",
                    "Completion Rate",
                ],
                "rows": streak_rows,
                "number_formats": {"B": _INT_FMT, "C": _INT_FMT, "D": _PCT_FMT},
                "alignments": {"B": "center", "C": "center", "D": "center"},
            }
        ],
        "conditional_formats": [
            {
                "range": "B18:B27",
                "rules": [
                    {
                        "type": "cell_is",
                        "operator": "greater_than_or_equal",
                        "value": 7,
                        "fill": CF_GOLD_FILL,
                        "font_color": CF_GOLD_TEXT,
                        "bold": True,
                        "stop_if_true": False,
                    }
                ],
            },
            {
                "range": "D18:D27",
                "rules": [
                    {
                        "type": "cell_is",
                        "operator": "greater_than_or_equal",
                        "value": 0.8,
                        "fill": CF_GREEN_FILL,
                        "font_color": CF_GREEN_TEXT,
                        "stop_if_true": False,
                    }
                ],
            },
        ],
        "charts": [
            {
                "type": "bar",
                "title": f"Completion Rate by Habit — {month1_name}",
                "anchor": "F3",
                "width": 15,
                "height": 9,
                "categories_range": "Dashboard!$A$18:$A$27",
                "series": [
                    {
                        "name": "Completion Rate",
                        "values_range": "Dashboard!$D$18:$D$27",
                    }
                ],
                "show_values": True,
                "value_numfmt": _PCT_FMT,
            },
            {
                "type": "line",
                "title": f"Daily Completion Trend — {month1_name}",
                "anchor": "F22",
                "width": 15,
                "height": 9,
                "categories_range": f"Tracker!$A${r0}:$A${rM1}",
                "series": [
                    {
                        "name": "Daily Completion %",
                        "values_range": f"Tracker!$N${r0}:$N${rM1}",
                    }
                ],
                "value_numfmt": _PCT_FMT,
            },
        ],
        "protect": True,
        "notes": (
            "Every value updates live from the Tracker. Overall Completion "
            "Rate = ✓ marks this month ÷ (days elapsed × active habits). "
            "Habits Completed counts ✓ marks this month; Perfect Days "
            "counts days where every active habit was ✓. Best / Needs Most "
            "Attention rank habits by this month's completion rate "
            "(inactive habits never win the attention pick). Streaks span "
            "the whole tracker."
        ),
    }

    # ═════════════════════════════ Instructions sheet ════════════════
    instructions_sheet: Dict[str, Any] = {
        "name": "Instructions",
        "tab_color": GREY,
        "column_widths": {"A": 100},
        "text_blocks": _instructions_blocks(month1_name, month2_name),
        "protect": True,
        "notes": (
            "This tab is documentation — every cell is locked. The Tracker, "
            "Dashboard, Habits and (hidden) Calc tabs do the actual work."
        ),
    }

    fname_month = _MONTHS[first.month - 1][:3].lower()
    return {
        "filename": f"habit_tracker_{fname_month}_{first.year}.xlsx",
        "sheets": [
            tracker_sheet,
            dashboard_sheet,
            habits_sheet,
            instructions_sheet,
            calc_sheet,
        ],
    }


def _instructions_blocks(month1_name: str, month2_name: str) -> List[dict]:
    """The how-to tab: short lines, one text block each."""

    def heading(cell: str, text: str) -> dict:
        return {
            "cell": cell,
            "text": text,
            "bold": True,
            "font_size": 12,
            "font_color": STEEL,
        }

    def line(cell: str, text: str) -> dict:
        return {"cell": cell, "text": text}

    return [
        {
            "cell": "A1",
            "text": "How to Use Your Habit Tracker",
            "bold": True,
            "font_size": 14,
            "font_color": NAVY,
        },
        heading("A3", "1. DAILY TRACKING — the Tracker tab"),
        line("A4", "Find today's row: it is highlighted in gold."),
        line(
            "A5", "For each habit you completed, pick ✓ from the dropdown (or type ✓)."
        ),
        line(
            "A6", "Missed one? Mark it x. Leave a day blank while it is still pending."
        ),
        line(
            "A7",
            "Daily Score and Daily % fill in automatically — never type into them.",
        ),
        line(
            "A8", "Use the Notes column for anything worth remembering about the day."
        ),
        heading("A10", "2. SET UP YOUR HABITS — the Habits tab"),
        line("A11", "Rename Habit 1..10 to your own habits in the Name column."),
        line(
            "A12",
            "Set Active = No for rows you don't use — daily scores only count active habits.",
        ),
        line(
            "A13",
            "Target and Start Date are for your reference; Color is a free label.",
        ),
        line(
            "A14",
            "Current Streak, Best Streak, Total Completions and Completion Rate are automatic.",
        ),
        heading("A16", "3. DASHBOARD"),
        line("A17", "Everything on the Dashboard updates live as you mark days."),
        line("A18", "Watch the streaks grow and the daily completion trend climb."),
        heading("A20", "4. MARKS LEGEND"),
        line("A21", "✓ = done (green)      x = missed (red)      blank = pending"),
        heading("A23", "5. STREAKS"),
        line("A24", "Current Streak = consecutive ✓ days ending today."),
        line(
            "A25",
            "An unmarked today never breaks a streak; a marked x or a skipped day does.",
        ),
        line("A26", "Best Streak = your longest ✓ run in this file."),
        heading("A28", "6. ADDING A NEW MONTH"),
        line("A29", f"This file tracks {month1_name} + {month2_name}."),
        line("A30", "For a fresh month, ask the assistant to generate a new tracker."),
        line(
            "A31",
            "To extend by hand: select the Tracker's last row, drag-fill down, then",
        ),
        line(
            "A32",
            "unhide the Calc sheet (right-click a tab → Unhide) and drag its helper",
        ),
        line(
            "A33", "columns C..L down the same number of rows so streaks keep counting."
        ),
        heading("A35", "7. PROTECTION"),
        line("A36", "Formula cells are locked against accidental edits."),
        line(
            "A37",
            "Review → Unprotect Sheet (no password) removes the lock if you need to.",
        ),
    ]


# ── Standard pattern entry points (used by the dynamic registry) ─────

coerce_params = coerce_habit_tracker_params
build_spec = build_habit_tracker_spec
