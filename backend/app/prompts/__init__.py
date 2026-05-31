"""
Prompt loader — reads prompt templates from markdown files.

Prompts are stored as MD files inside this package directory.
Each file can contain `{placeholder}` markers that are filled at
runtime using Python's `str.format()`.

IMPORTANT: When writing markdown prompt files, any literal braces that
are NOT format placeholders must be escaped as `{{` and `}}`,
because Python's `str.format()` treats `{foo}` as a key.
"""

from __future__ import annotations

import functools
from pathlib import Path

_PROMPTS_DIR = Path(__file__).parent


@functools.lru_cache(maxsize=16)
def load_prompt(name: str) -> str:
    """Load a prompt template from a markdown file.

    Args:
        name: Prompt name without extension (e.g. `"agent_system"`).
              Looks for `<name>.md` in the prompts directory.

    Returns:
        The raw prompt template string (with `{placeholder}` markers).

    Raises:
        FileNotFoundError: If the prompt file does not exist.
    """
    path = _PROMPTS_DIR / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(f"Prompt file not found: {path}")
    return path.read_text(encoding="utf-8")


def format_prompt(name: str, **kwargs) -> str:
    """Load a prompt template and format it with the given values.

    Args:
        name: Prompt name without extension.
        **kwargs: Values to fill into the template placeholders.

    Returns:
        The formatted prompt string.
    """
    template = load_prompt(name)
    return template.format(**kwargs)
