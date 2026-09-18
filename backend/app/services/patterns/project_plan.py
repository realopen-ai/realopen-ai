"""
Project plan pattern — task schedule with start & end dates, progress,
status words and milestone flags (deterministic).

One sheet ("Plan", navy):

  row 1      title (project name — Project Plan)
  row 2      usage hint
  row 3      Project Deadline label + date (only when the request gives
             one; the summary's "Days to Deadline" reads this cell)
  row 5      header: Task | Owner | Start | End | Duration (days) |
             Days Remaining | Progress | Status | Milestone
  rows 6..   one row per task — Duration and Days Remaining are LIVE
             formulas (end-start+1 / end-TODAY()), Progress is the
             stated percent as a fraction, Status keeps the request's
             own word (dropdown), Milestone is Yes/No (dropdown)
  below      Summary block: task / completed / in-progress / overdue /
             milestone COUNTIF+COUNTIFS tallies and the overall
             completion = AVERAGE(progress) guarded by COUNT — all
             live formulas over the exact rendered rows
  chart      horizontal bar of task durations (timeline) when the tasks
             carry dates, else a progress bar chart, anchored right of
             the table

TEMPLATE MODE: a request with no tasks ("create a project plan")
builds the BLANK task plan — never invents tasks (hard rule), never
refuses for lack of data. Ten blank scaffold rows carry the same
live guarded Duration / Days-Remaining formulas (blank until both
dates exist), and the Status + Milestone dropdowns, overdue /
progress conditional formats and the Summary tallies all cover the
scaffold band — the workbook comes alive row by row as tasks are
typed in, with the counts legitimately reading 0 over the blanks.
With no tasks there is nothing to chart, so the timeline / progress
chart is skipped.

FILL MODE: when the request states an objective/deadline but no
  tasks, the router sets "fill": true and excel_gen drafts a starter
  task breakdown via prompts/pattern_populator.md BEFORE calling
  the builder. Drafted params flow through coerce_project_plan_params
  like any other; a failed draft falls back to the extracted params
  (blank task rows).


Overdue detection is a formula-type conditional format: end date in
the past AND status <> "Completed" paints the status cell red; the
Days Remaining column turns red when negative and amber inside the
last week. Completed → green, In Progress → amber (classic palettes).
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from app.services.patterns.utils import (
    PCT_FMT,
    _pick,
    to_iso_date,
    to_number,
)

# Registry key — must match the pattern stanza in
# prompts/pattern_classifier.md.
PATTERN_NAME = "project_plan"

PATTERN_DESCRIPTION = (
    "Project plan / task schedule: one row per task with owner, start "
    "& end dates, live duration and days-remaining formulas, progress "
    "%, status colors, overdue detection, milestone tallies and a "
    "duration timeline chart — a request with no tasks still gets a "
    "blank task plan template."
)

# Routing keywords/stems — drive the cheap pre-gate and the classifier
# shortlist (see excel_gen._shortlist_patterns).
PATTERN_KEYWORDS = (
    "project plan",
    "project",
    "milestone",
    "gantt",
    "roadmap",
    "task",
    "deliverable",
    "workstream",
    "deadline",
    "sprint",
)

# Fillable pattern: guidance-only requests (goals, split, frequency…)
# may draft starter sessions via prompts/pattern_populator.md before
# building — see PATTERN_FILLABLE in patterns/__init__.py.
PATTERN_FILL = True

MAX_TASKS = 200
MIN_TASK_ROWS = 10  # blank scaffold rows in template mode

# Design tokens (same palette as the converter).
NAVY = "16304F"
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

STATUS_OPTIONS = ("Not Started", "In Progress", "Completed", "Blocked", "On Hold")

# Common classifier spellings → canonical status words. Anything else
# is kept VERBATIM (the stanza says status comes from the request's
# own words).
_STATUS_ALIASES = {
    "not started": "Not Started",
    "notstarted": "Not Started",
    "todo": "Not Started",
    "to do": "Not Started",
    "open": "Not Started",
    "in progress": "In Progress",
    "inprogress": "In Progress",
    "started": "In Progress",
    "ongoing": "In Progress",
    "wip": "In Progress",
    "active": "In Progress",
    "completed": "Completed",
    "complete": "Completed",
    "done": "Completed",
    "finished": "Completed",
    "closed": "Completed",
    "blocked": "Blocked",
    "block": "Blocked",
    "stuck": "Blocked",
    "on hold": "On Hold",
    "hold": "On Hold",
    "waiting": "On Hold",
    "paused": "On Hold",
    "cancelled": "Cancelled",
    "canceled": "Cancelled",
}


def _normalize_status(value: Any) -> Optional[str]:
    """Status word → canonical spelling; None when not stated."""
    if not isinstance(value, str):
        return None
    s = " ".join(value.split())
    if not s:
        return None
    return _STATUS_ALIASES.get(s.lower(), s)


def _truthy(value: Any) -> bool:
    """Milestone/done flags: JSON booleans plus yes/true strings."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("yes", "y", "true", "t", "1")
    if isinstance(value, (int, float)):
        return bool(value)
    return False


def _normalize_task_entry(entry: Any) -> Optional[dict]:
    """One tasks entry → a row dict or None (unusable).

    Accepts the documented object form and bare task-name strings.
    Dates/progress/status/milestone are None when not stated (stanza).
    """
    if isinstance(entry, str):
        name = entry.strip()
        if not name:
            return None
        return {
            "name": name[:200],
            "owner": None,
            "start": None,
            "end": None,
            "progress": None,
            "status": None,
            "milestone": False,
        }

    if not isinstance(entry, dict):
        return None

    name = str(
        _pick(entry, "name", "task", "title", "activity", "item", "description") or ""
    ).strip()
    if not name:
        return None

    progress = to_number(
        _pick(entry, "progress", "percent_complete", "percent", "completion", "pct")
    )
    if progress is not None:
        # stated 0-100 → fraction for the percent number format
        progress = min(max(progress, 0.0), 100.0) / 100.0

    owner = str(
        _pick(entry, "owner", "assigned_to", "assignee", "responsible", "who") or ""
    ).strip()

    return {
        "name": name[:200],
        "owner": owner[:80] or None,
        "start": to_iso_date(_pick(entry, "start_date", "start", "begins")),
        "end": to_iso_date(
            _pick(entry, "end_date", "end", "due_date", "finish", "target")
        ),
        "progress": progress,
        "status": _normalize_status(_pick(entry, "status", "state")),
        "milestone": _truthy(
            _pick(entry, "milestone", "is_milestone", "milestone_flag")
        ),
    }


def coerce_project_plan_params(params: dict) -> dict:
    """Validate + normalize classifier params; raises ValueError.

    Template mode: empty/missing tasks are FINE — the builder emits
    the blank task plan with scaffold rows (the "create a project
    plan" case). ValueError only for structurally wrong params
    (non-object params, non-array tasks).
    """
    if not isinstance(params, dict):
        raise ValueError("params must be an object")

    project_name = str(
        _pick(params, "project_name", "project", "name", "title") or ""
    ).strip()
    project_name = project_name[:80] or None

    deadline = to_iso_date(_pick(params, "deadline", "due_date", "target_date", "due"))

    raw_tasks = _pick(params, "tasks", "task_list", "items", "lines")
    tasks: List[dict] = []
    if raw_tasks is not None:
        if not isinstance(raw_tasks, list):
            raise ValueError("tasks must be an array")
        for entry in raw_tasks[:MAX_TASKS]:
            normalized = _normalize_task_entry(entry)
            if normalized is not None:
                tasks.append(normalized)
    # Template mode: zero usable tasks is fine — blank scaffold rows
    # (never invent tasks).

    notes = _pick(params, "notes", "note")
    notes = notes.strip()[:1000] if isinstance(notes, str) and notes.strip() else None

    return {
        "project_name": project_name,
        "deadline": deadline,
        "tasks": tasks,
        "notes": notes,
    }


def build_project_plan_spec(params: dict) -> dict:
    """Project plan workbook — every formula code-generated.

    Layout (rows computed here, never guessed by a model):

    Plan sheet:
      row 1      title
      row 2      usage hint
      row 3      Project Deadline label + date (deadline given only)
      row 5      headers A..I
      rows 6..   one row per task; E/F are live formulas (template
                 mode: blank scaffold rows padded to 10, same guarded
                 E/F formulas, dropdowns + CF over the whole band)
      row+3..    Summary block (COUNTIF/COUNTIFS/AVERAGE tallies)
      chart      anchored K5 (durations bar_h, or progress bar) —
                 skipped entirely when there are no tasks
    """
    p = coerce_project_plan_params(params)
    project_name: Optional[str] = p["project_name"]
    deadline: Optional[str] = p["deadline"]
    tasks: List[dict] = p["tasks"]
    notes = p["notes"]
    template_mode = not tasks

    display_title = f"{project_name} — Project Plan" if project_name else "Project Plan"

    # ── geometry (all formulas below reference THESE integers) ──────
    header_row = 5
    first = header_row + 1
    n_rows = len(tasks) if tasks else MIN_TASK_ROWS
    last = first + n_rows - 1
    s0 = last + 3  # Summary section label row

    task_rows: List[List[Any]] = []
    for i, t in enumerate(tasks):
        r = first + i
        task_rows.append(
            [
                t["name"],
                t["owner"],
                t["start"],
                t["end"],
                # Duration (days) — live, blank until both dates exist
                '=IF(OR(C{r}="",D{r}=""),"",D{r}-C{r}+1)'.format(r=r),
                # Days Remaining — live from TODAY()
                '=IF(D{r}="","",D{r}-TODAY())'.format(r=r),
                t["progress"],
                t["status"],
                "Yes" if t["milestone"] else "No",
            ]
        )
    while len(task_rows) < n_rows:  # template scaffold rows
        r = first + len(task_rows)
        task_rows.append(
            [
                None,
                None,
                None,
                None,
                # Duration (days) — same live guard as data rows
                '=IF(OR(C{r}="",D{r}=""),"",D{r}-C{r}+1)'.format(r=r),
                # Days Remaining — same live guard as data rows
                '=IF(D{r}="","",D{r}-TODAY())'.format(r=r),
                None,
                None,
                None,
            ]
        )

    blocks: List[dict] = [
        {
            "cell": "A1",
            "text": display_title,
            "bold": True,
            "font_size": 14,
            "font_color": NAVY,
        },
        {
            "cell": "A2",
            "text": (
                "Enter each task once — Duration, Days Remaining and the "
                "Summary update automatically. Pick Status and Milestone "
                "from the dropdowns; overdue tasks turn red."
                if not template_mode
                else "Blank task plan — type your tasks into the rows below; "
                "Duration, Days Remaining and the Summary update as the "
                "dates come in. Pick Status and Milestone from the "
                "dropdowns; overdue tasks turn red."
            ),
            "italic": True,
            "font_color": MUTED,
        },
    ]
    if deadline:
        blocks += [
            {"cell": "A3", "text": "Project Deadline"},
            {"cell": "B3", "text": deadline, "number_format": DATE_FMT},
        ]

    summary_rows: List[Tuple[str, str, str]] = [
        ("Total Tasks", "=COUNTA(A{f}:A{l})".format(f=first, l=last), INT_FMT),
        (
            "Completed Tasks",
            '=COUNTIF(H{f}:H{l},"Completed")'.format(f=first, l=last),
            INT_FMT,
        ),
        (
            "In Progress Tasks",
            '=COUNTIF(H{f}:H{l},"In Progress")'.format(f=first, l=last),
            INT_FMT,
        ),
        (
            "Overdue Tasks",
            '=COUNTIFS(D{f}:D{l},"<"&TODAY(),H{f}:H{l},"<>Completed")'.format(
                f=first, l=last
            ),
            INT_FMT,
        ),
        ("Milestones", '=COUNTIF(I{f}:I{l},"Yes")'.format(f=first, l=last), INT_FMT),
        (
            "Overall Completion",
            '=IF(COUNT(G{f}:G{l})=0,"n/a",AVERAGE(G{f}:G{l}))'.format(f=first, l=last),
            PCT_FMT,
        ),
    ]
    if deadline:
        summary_rows.append(("Days to Deadline", "=$B$3-TODAY()", INT_FMT))

    blocks.append(
        {"cell": f"A{s0}", "text": "Summary", "bold": True, "font_color": NAVY}
    )
    for i, (label, formula, fmt) in enumerate(summary_rows):
        r = s0 + 1 + i
        blocks.append({"cell": f"A{r}", "text": label})
        blocks.append({"cell": f"B{r}", "text": formula, "number_format": fmt})

    table: Dict[str, Any] = {
        "start_cell": f"A{header_row}",
        "headers": [
            "Task",
            "Owner",
            "Start",
            "End",
            "Duration (days)",
            "Days Remaining",
            "Progress",
            "Status",
            "Milestone",
        ],
        "rows": task_rows,
        "number_formats": {
            "C": DATE_FMT,
            "D": DATE_FMT,
            "E": INT_FMT,
            "F": INT_FMT,
            "G": PCT_FMT,
        },
        "alignments": {"E": "center", "F": "center", "G": "center", "I": "center"},
        "auto_filter": True,
    }

    # ── conditional formats ─────────────────────────────────────────
    conditional_formats: List[dict] = [
        {
            "range": f"H{first}:H{last}",
            "rules": [
                {
                    "type": "cell_is",
                    "operator": "equal",
                    "value": "Completed",
                    "fill": CF_GREEN_FILL,
                    "font_color": CF_GREEN_TEXT,
                    "stop_if_true": True,
                },
                {
                    # overdue: end date in the past AND not completed
                    # (blank end dates never trigger — "" guards)
                    "type": "formula",
                    "formula": 'AND($D{r}<>"",$D{r}<TODAY(),$H{r}<>"Completed")'.format(
                        r=first
                    ),
                    "fill": CF_RED_FILL,
                    "font_color": CF_RED_TEXT,
                },
                {
                    "type": "cell_is",
                    "operator": "equal",
                    "value": "In Progress",
                    "fill": CF_AMBER_FILL,
                    "font_color": CF_AMBER_TEXT,
                },
                {
                    "type": "cell_is",
                    "operator": "equal",
                    "value": "Blocked",
                    "fill": CF_RED_FILL,
                    "font_color": CF_RED_TEXT,
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
            "range": f"F{first}:F{last}",
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
        {
            "range": f"I{first}:I{last}",
            "rules": [
                {
                    "type": "cell_is",
                    "operator": "equal",
                    "value": "Yes",
                    "fill": CF_AMBER_FILL,
                    "font_color": CF_AMBER_TEXT,
                }
            ],
        },
        {
            "range": f"G{first}:G{last}",
            "rules": [
                {
                    "type": "cell_is",
                    "operator": "equal",
                    "value": 1,
                    "fill": CF_GREEN_FILL,
                    "font_color": CF_GREEN_TEXT,
                }
            ],
        },
    ]

    data_validation: List[dict] = [
        {
            # warning (not stop): the request may carry its own status
            # words beyond the standard set
            "range": f"H{first}:H{last}",
            "values": list(STATUS_OPTIONS),
            "allow_blank": True,
            "error_style": "warning",
        },
        {
            "range": f"I{first}:I{last}",
            "values": ["Yes", "No"],
            "allow_blank": True,
        },
    ]

    # ── chart: duration timeline when dates given, else progress ────
    charts: List[dict] = []
    has_dates = any(t["start"] and t["end"] for t in tasks)
    has_progress = any(t["progress"] is not None for t in tasks)
    if has_dates:
        charts.append(
            {
                "type": "bar_h",
                "title": "Task Durations (days)",
                "anchor": "K5",
                "width": 14,
                "height": 9,
                "categories_range": "Plan!A{f}:A{l}".format(f=first, l=last),
                "series": [
                    {
                        "name": "Duration (days)",
                        "values_range": "Plan!E{f}:E{l}".format(f=first, l=last),
                    }
                ],
            }
        )
    elif has_progress:
        charts.append(
            {
                "type": "bar",
                "title": "Progress by Task",
                "anchor": "K5",
                "width": 14,
                "height": 9,
                "categories_range": "Plan!A{f}:A{l}".format(f=first, l=last),
                "series": [
                    {
                        "name": "Progress",
                        "values_range": "Plan!G{f}:G{l}".format(f=first, l=last),
                    }
                ],
                "value_numfmt": "0%",
            }
        )

    sheet_notes = notes or (
        "Blank task plan — one row per task. Duration = End - Start + 1 "
        "and Days Remaining = End - TODAY() are live and stay blank until "
        "both dates exist; Progress is a percent of 100. Status colors: "
        "Completed green, In Progress amber, overdue (past end date, not "
        "Completed) red. The Summary tallies read 0 until tasks are "
        "typed in and update live from there."
        if template_mode
        else "One row per task. Duration = End - Start + 1 and Days Remaining "
        "= End - TODAY() are live; Progress is a percent of 100. Status "
        "colors: Completed green, In Progress amber, overdue (past end "
        "date, not Completed) red. The Summary tallies and the chart "
        "update as you edit."
    )

    sheet_spec: Dict[str, Any] = {
        "name": "Plan",
        "tab_color": NAVY,
        "freeze_panes": f"A{first}",
        "column_widths": {
            "A": 32,
            "B": 16,
            "C": 12,
            "D": 12,
            "E": 13,
            "F": 14,
            "G": 11,
            "H": 14,
            "I": 11,
        },
        "text_blocks": blocks,
        "tables": [table],
        "data_validation": data_validation,
        "conditional_formats": conditional_formats,
        "notes": sheet_notes,
    }
    if charts:
        sheet_spec["charts"] = charts

    fname = "project_plan"
    if project_name:
        safe = re.sub(r"[^\w\s-]", "", project_name)[:40].strip()
        safe = re.sub(r"[\s_-]+", "_", safe).strip("_")
        if safe:
            fname = f"{safe}_project_plan"

    return {
        "filename": f"{fname}.xlsx",
        "sheets": [sheet_spec],
    }


# ── Standard pattern entry points (used by the dynamic registry) ──────
coerce_params = coerce_project_plan_params
build_spec = build_project_plan_spec
