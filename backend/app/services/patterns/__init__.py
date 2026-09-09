"""
Deterministic workbook patterns — a dynamic, tool-style registry.

WHY THIS PACKAGE EXISTS
──────────────────────
Small models cannot reliably hand-write formula lattices: they emit
formulas whose row references don't match the layout that actually
gets rendered (off by one, two, three rows…). Post-hoc healing can
only patch signatures we can detect structurally.

The fix for the common cases: stop asking the model to write those
formulas at all. A classifier LLM call extracts a handful of scalar
parameters from the brief (numbers, names, line items — the thing
models ARE good at), and a pattern module builds the workbook JSON
spec in code. Every formula reference is computed from the actual row
numbers of the layout emitted by the same code, so an off-by-N is
impossible by construction.

STRUCTURE
────────────────────────────────────
Every pattern lives in its own module file:

    patterns/
      __init__.py        ← this file: dynamic discovery + registry
      utils.py           ← helpers shared by ALL patterns
      amortization.py    ← PATTERN_NAME + build_spec + coerce_params
      invoice.py
      budget.py
      portfolio.py
      …                  ← drop a new file here to add a pattern

Each pattern module must expose:

    PATTERN_NAME        str    registry key — MUST match the pattern
                               stanza in prompts/pattern_classifier.md
    PATTERN_DESCRIPTION str    one-line description for routing docs
    build_spec(params)  dict   params → RAW workbook spec (the exact
                               schema excel_gen validates + normalizes
                               + converts); raises ValueError when
                               required parameters are missing — the
                               caller then falls back to the
                               AI-generated spec path
    coerce_params(params) dict optional param normalizer (tests use it)

Pattern modules must NEVER import from excel_gen (no circular
import) and must never write prompts in code — prompts live in
app/prompts/*.md.

ADDING A NEW PATTERN
────────────────────
1. Create ``<name>.py`` in this folder with PATTERN_NAME,
   PATTERN_DESCRIPTION, build_spec (+ optional coerce_params).
2. Add the routing stanza to prompts/pattern_classifier.md.
3. Mention the domain keywords in excel_gen's _PATTERN_GATE_RE if
   the brief wouldn't otherwise pass the cheap pre-gate.

Nothing else changes — this __init__ discovers the module at import
time and registers it in PATTERN_BUILDERS, exactly like the agent
tool registry picks up a new tool file.
"""

from __future__ import annotations

import importlib
import pkgutil
from types import ModuleType
from typing import Callable, Dict

# Shared helpers re-exported at the package root so pattern modules,
# excel_gen and tests can import everything from one place.
from app.services.patterns.utils import (  # noqa: F401
    CURRENCY_SYMBOLS,
    MONEY_FMT,
    MONTH_FMT,
    PCT_FMT,
    QTY_FMT,
    currency_fmt,
    first_of_next_month,
    fmt_num,
    next_month,
    pick,
    to_int,
    to_iso_date,
    to_number,
    to_rate,
    _currency_fmt,
    _first_of_next_month,
    _fmt_num,
    _next_month,
    _pick,
)

# name → builder(params) → RAW workbook spec dict
PATTERN_BUILDERS: Dict[str, Callable[[dict], dict]] = {}

# name → one-line routing description
PATTERN_DESCRIPTIONS: Dict[str, str] = {}

# Names never hoisted from a pattern module into this package's
# namespace — they are per-module registry concepts.
_REGISTRY_ATTRS = frozenset(
    {
        "PATTERN_NAME",
        "PATTERN_DESCRIPTION",
        "build_spec",
        "coerce_params",
    }
)

# Modules that are infrastructure, not patterns.
_NON_PATTERN_MODULES = frozenset({"__init__", "utils"})


def _register_pattern_module(module: ModuleType, fallback_name: str) -> None:
    """Register one pattern module; hoist its public API.

    The module's own public callables (build_amortization_spec,
    coerce_amortization_params, …) are hoisted into this package's
    namespace so ``from app.services import patterns`` gives callers
    direct access to every pattern's functions.
    """
    name = str(getattr(module, "PATTERN_NAME", "") or "").strip()
    builder = getattr(module, "build_spec", None)
    if not name or not callable(builder):
        return  # not a pattern module — skip silently
    PATTERN_BUILDERS[name] = builder
    PATTERN_DESCRIPTIONS[name] = str(getattr(module, "PATTERN_DESCRIPTION", "") or "")
    for attr in dir(module):
        if attr.startswith("_") or attr in _REGISTRY_ATTRS:
            continue
        obj = getattr(module, attr)
        if callable(obj) and getattr(obj, "__module__", None) == module.__name__:
            # setdefault: keep the first on a (never expected) clash
            globals().setdefault(attr, obj)


def _discover_patterns() -> None:
    """Import every pattern module in this package and register it.

    pkgutil walks THIS package's directory, so a newly dropped
    <name>.py file is picked up automatically on the next import —
    no registration list to maintain (same philosophy as the agent
    tools folder).
    """
    for info in sorted(pkgutil.iter_modules(__path__), key=lambda m: m.name):
        if info.name in _NON_PATTERN_MODULES or info.name.startswith("_"):
            continue
        module = importlib.import_module(f".{info.name}", package=__package__)
        _register_pattern_module(module, info.name)


_discover_patterns()
