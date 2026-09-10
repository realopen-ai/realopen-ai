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

| Pattern         | File               | What it builds                                                                               |
| --------------- | ------------------ | -------------------------------------------------------------------------------------------- |
| `amortization`  | `amortization.py`  | Loan / mortgage repayment schedule (PMT chain, balance, summary, line chart)                 |
| `invoice`       | `invoice.py`       | Professional invoice (seller/client blocks, line items, discount before tax, totals, notes)  |
| `budget`        | `budget.py`        | Income & expense planner (live savings rate, expense-mix pie chart)                          |
| `portfolio`     | `portfolio.py`     | Investment portfolio (holdings or transaction modes, gain/loss, allocation pie, performance) |
| `habit_tracker` | `habit_tracker.py` | Daily habit tracker (✓/x marks, streaks, completion rates, dashboard + charts)               |
| `inventory`     | `inventory.py`     | Inventory tracker (items, low-stock + warranty alerts, lending, dashboard + charts)          |

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
   ├── LLM call with prompts/pattern_classifier.md  → {"pattern": name, "params": {...}}
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

## Anatomy of a pattern module

Every pattern file must expose:

```python
PATTERN_NAME = "inventory"          # registry key — MUST match the stanza in
                                    # prompts/pattern_classifier.md
PATTERN_DESCRIPTION = "..."         # one-line description for routing docs

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
3. **Check the pre-gate** — add domain keywords to `_PATTERN_GATE_RE` in
   `excel_gen.py` if the brief wouldn't otherwise pass the cheap gate.
4. **Write tests** in `tests/test_excel_patterns.py`: param coercion, builder
   layout math (every formula pins to the row the converter will render),
   a converter round-trip, and a routing test with a mocked classifier.

Nothing else changes — `__init__.py` discovers the module at import time and
registers it in `PATTERN_BUILDERS`.

## Verifying a pattern

The strongest check is end-to-end with real recalculation:

1. Build the spec from params, normalize, `_build_xlsx` to a file.
2. `soffice --headless --convert-to xlsx` (LibreOffice recalculates on
   conversion) → reload with `data_only=True` and assert computed values.
3. Render to PDF (`soffice --convert-to pdf`) and inspect the pages —
   charts, conditional formats, protection and print layout all show up.
