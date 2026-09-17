# Workbook Patterns

Deterministic Excel templates — **AI extracts parameters, code builds the workbook.**

## Why this exists

Small models cannot reliably hand-write formula lattices: they emit formulas whose
row references don't match the layout that actually gets rendered (off by one, two,
three rows…). Post-hoc healing can only patch signatures we can detect structurally.

The fix for the common cases: **stop asking the model to write those formulas at all.**
A classifier LLM call extracts a handful of scalar parameters from the brief (numbers,
names, line items — the thing models _are_ good at), and a pattern module builds the
workbook JSON spec in code. Every formula reference is computed from the actual row
numbers of the layout emitted by the same code, so an off-by-N is impossible by
construction.

## The patterns

| Pattern                 | File                       | What it builds                                                                               |
| ----------------------- | -------------------------- | -------------------------------------------------------------------------------------------- |
| `amortization`          | `amortization.py`          | Loan / mortgage repayment schedule (PMT chain, balance, summary, line chart)                 |
| `invoice`               | `invoice.py`               | Professional invoice (seller/client blocks, line items, discount before tax, totals, notes)  |
| `budget`                | `budget.py`                | Income & expense planner (live savings rate, expense-mix pie chart)                          |
| `portfolio`             | `portfolio.py`             | Investment portfolio (holdings or transaction modes, gain/loss, allocation pie, performance) |
| `habit_tracker`         | `habit_tracker.py`         | Daily habit tracker (✓/x marks, streaks, completion rates, dashboard + charts)               |
| `inventory`             | `inventory.py`             | Inventory tracker (items, low-stock + warranty alerts, lending, dashboard + charts)          |
| `savings_goal`          | `savings_goal.py`          | One savings goal projection (cumulative balance chain, interest, months-to-goal, chart)      |
| `net_worth`             | `net_worth.py`             | Assets vs liabilities statement (net worth, category rollups, charts)                        |
| `break_even`            | `break_even.py`            | Break-even analysis (fixed/variable costs, sensitivity table, profit chart)                  |
| `cashflow_forecast`     | `cashflow_forecast.py`     | Month-by-month cash flow projection (opening/closing balance chain, one-offs, chart)         |
| `expense_report`        | `expense_report.py`        | Dated expense log for reimbursement (per-category totals, net due, pie)                      |
| `price_list`            | `price_list.py`            | Product pricing catalog (markup/margin formulas, optional VAT, category subtotals)           |
| `receivables`           | `receivables.py`           | Accounts-receivable tracker (outstanding, days overdue, aging buckets, charts)               |
| `payroll`               | `payroll.py`               | Payroll register (hourly/monthly gross, tax, deductions, net pay, chart)                     |
| `sales_tracker`         | `sales_tracker.py`         | Sales log + analysis (revenue/profit formulas, SUMIF by product/channel/month)               |
| `crm_pipeline`          | `crm_pipeline.py`          | Deal pipeline (weighted value, stage summary, dropdowns, chart)                              |
| `kpi_report`            | `kpi_report.py`            | KPI scorecard (direction-aware attainment, status colors, chart)                             |
| `leave_tracker`         | `leave_tracker.py`         | Employee leave balances (entitlements, SUMIF booked, log sheet)                              |
| `timesheet`             | `timesheet.py`             | Hours per project per weekday (totals, optional pay with rate block)                         |
| `shift_schedule`        | `shift_schedule.py`        | Weekly staff rota (code grid, code→hours VLOOKUP on Calc sheet)                              |
| `equipment_maintenance` | `equipment_maintenance.py` | Service schedule (next due, days-until, overdue alerts, service log)                         |
| `gradebook`             | `gradebook.py`             | Class gradebook (weighted category averages, letter grades, CF)                              |
| `survey_results`        | `survey_results.py`        | Survey tabulation (per-question counts, guarded shares, charts)                              |
| `expense_split`         | `expense_split.py`         | Group cost splitting (paid/owed/balances, zero-sum, settlement suggestions)                  |
| `project_plan`          | `project_plan.py`          | Project task plan (durations, days remaining, overdue CF, timeline chart)                    |
| `event_planner`         | `event_planner.py`         | One event (countdown, guests/RSVP, vendors/deposits, tasks checklist)                        |
| `travel_planner`        | `travel_planner.py`        | One trip (itinerary, bookings, cost summary, per-traveler share)                             |
| `content_calendar`      | `content_calendar.py`      | Social/editorial content plan (status dropdowns, per-platform summary, chart)                |
| `meal_planner`          | `meal_planner.py`          | Weekly meal grid + shopping list (to-buy formulas, unit dropdowns)                           |
| `workout_log`           | `workout_log.py`           | Training log (volume, Epley e1RM, per-exercise summary, volume chart)                        |
| `subscription_tracker`  | `subscription_tracker.py`  | Recurring subscriptions (cycle equivalents, renewal countdown, totals)                       |

## How the machinery works

```
user brief
   │
   ▼
excel_gen.generate_spreadsheet(brief, requirements)
   │
   ├── _PATTERN_GATE_RE ── cheap keyword/digit gate (skips classifier when
   │                        the brief can't be a templated document)
   ▼
_try_pattern_spec()
   ├── _shortlist_patterns(brief)   → ranked keyword shortlist (≤ MAX_SHORTLIST)
   ├── LLM call with _classifier_system_prompt(shortlist)
   │     → sees ONLY the shortlisted stanzas from
   │       prompts/pattern_classifier.md + the mandatory "none" stanza
   │     → {"pattern": name, "params": {...}}
   ├── PATTERN_BUILDERS[name].build_spec(params)     → RAW workbook spec
   ├── validate_workbook_spec(spec)                  → errors → fall back to AI path
   └── _normalize_spec(spec)                         → normalized spec
   │
   ▼  (no pattern matched / params insufficient / anything failed)
AI path: _generate_workbook_json → the original LLM-generated spec route
   │
   ▼
_SheetWriter (openpyxl) → .xlsx
```

Key properties:

- **Deterministic** — same params, same workbook, every formula ref computed from the
  rows the builder itself renders.
- **Never raises** — a pattern that fails (missing params, invalid values, unexpected
  exception) silently falls back to the AI-generated spec path.
- **Dynamic registry** — this folder is scanned at import time; dropping a new file in
  registers its pattern, exactly like the agent tools folder.
- **Two-stage routing** — with 30+ patterns the classifier only sees the
  keyword-shortlisted stanzas, so the prompt stays six-pattern-sized no
  matter how large the registry grows.

## Anatomy of a pattern module

Every pattern file must expose:

```python
PATTERN_NAME = "inventory"          # registry key — MUST match the stanza in
                                    # prompts/pattern_classifier.md
PATTERN_DESCRIPTION = "..."         # one-line description for routing docs
PATTERN_KEYWORDS = ("inventory", "stock", ...)  # lowercase words/stems — they
                                    # drive the pre-gate + the classifier
                                    # shortlist (routing!), so a pattern
                                    # without keywords is unreachable

def build_spec(params: dict) -> dict:
    """params → RAW workbook spec (the exact schema excel_gen validates,
    normalizes and converts). Raises ValueError when required parameters
    are missing — the caller then falls back to the AI spec path."""

def coerce_params(params: dict) -> dict:
    """Optional param normalizer: validates/normalizes classifier output
    (models quote numbers, add %, invent alias keys…). Tests target it."""
```

Conventions:

- **Never import from `excel_gen`** (no circular imports) — import shared helpers
  from `patterns.utils` instead.
- **Never write prompts in code** — routing text lives in `app/prompts/*.md`
  and is loaded with `get_prompt(...)`. PLEASE do not hardcode prompt text in the pattern modules.
- **No `ROUND()` in formulas** — rounding belongs to the number format
  (`#,##0.00` etc.); ROUND in formulas accumulates decimal-level drift.
- **Layout math is local** — compute row numbers first (`r0`, `rN`, …), then
  emit every formula with `.format(...)` from _those_ numbers.
- **Live formulas over frozen values** — the workbook should keep updating
  when the user edits it.
- Hidden `Calc` sheets hold helper lattices when the visible math needs
  rank/sanitize columns (see `habit_tracker.py` / `inventory.py` for examples).

## Shared helpers (`utils.py`)

`to_number` / `to_rate` / `to_int` / `to_iso_date` (param coercion),
`pick` (alias lookup), `currency_fmt` (currency code → Excel number format),
`fmt_num` (compact literals inside formulas), `next_month` /
`first_of_next_month` (date math). Keep this module generic — nothing
pattern-specific belongs there.

## Adding a new pattern

1. **Create `<name>.py`** in this folder with `PATTERN_NAME`,
   `PATTERN_DESCRIPTION`, `build_spec` (+ optional `coerce_params`).
   Start by copying an existing pattern close to your domain.
2. **Add the routing stanza** to `prompts/pattern_classifier.md`:
   one `- "name" — description` line with the exact params JSON shape
   and the use / do-not-use rules. The registry key must match the stanza
   name exactly.
3. **Routing keywords** — list `PATTERN_KEYWORDS` in the module: single
   words/stems match English suffixes ("budget" → "budgets"), multi-word
   phrases match verbatim. The union regex `_PATTERN_GATE_RE` and the
   shortlist ranking build themselves from the registry — no excel_gen
   edit needed.
4. **Write tests** in a `tests/test_patterns_*.py` file: param coercion,
   builder layout math (every formula pins to the row the converter will
   render), a converter round-trip, and a routing test with a mocked
   classifier.

Nothing else changes — `__init__.py` discovers the module at import time and
registers it in `PATTERN_BUILDERS`.

## Verifying a pattern

The strongest check is end-to-end with real recalculation:

1. Build the spec from params, normalize, `_build_xlsx` to a file.
2. Flag `wb.calculation.fullCalcOnLoad = True` on a copy (openpyxl), then
   `soffice --headless --convert-to xlsx --outdir <DIFFERENT_DIR> <copy>` —
   LibreOffice hard-recalculates on load. **Gotchas (proven by experiment):**
   plain conversion does NOT recalculate without the flag, and soffice
   silently no-ops when the output dir equals the source dir. Reload with
   `data_only=True` and assert computed values (scaffold formulas like
   `=IF($B8="","",…)` legitimately cache no value — don't flag those).
3. Render to PDF (`soffice --convert-to pdf`) and inspect the pages —
   charts, conditional formats, protection and print layout all show up.
