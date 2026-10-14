"""Check quiz code-block structure without rewriting source code."""

import re

_OPENING_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")


def _closes_fence(line: str, fence: str) -> bool:
    return bool(
        re.fullmatch(rf" {{0,3}}{re.escape(fence[0])}{{{len(fence)},}}[ \t]*", line)
    )


def mask_code_blocks(text: str) -> str:
    """Hide fenced code for prose checks, keeping offsets and line boundaries."""
    fence = None
    masked = []
    for line in text.splitlines(keepends=True):
        if fence is not None:
            inside_code = True
            if _closes_fence(line.rstrip("\r\n"), fence):
                fence = None
        else:
            opening = _OPENING_FENCE.match(line)
            inside_code = opening is not None
            if opening:
                fence = opening[1]
        masked.append(
            "".join(char if char in "\r\n" else " " for char in line)
            if inside_code
            else line
        )
    return "".join(masked)


def validate_code_fences(text: str) -> None:
    """Reject broken Markdown fences, but preserve literal escapes inside code.

    JSON decoding already handles transport escaping. Decoding the text again
    would corrupt examples containing strings such as ``r"\n"``. Ask the model
    to repair ambiguous layout instead of guessing newlines or indentation.
    """
    fence = None
    for line in text.splitlines():
        if fence is not None:
            if _closes_fence(line, fence):
                fence = None
            continue

        # A double-encoded line break around a fence is layout, not code.
        # Only check outside established blocks so code strings stay untouched.
        if re.search(
            r"(?:^|(?<!\\)\\n)[ \t]{0,3}(?:`{3,}|~{3,})[\w+.-]*(?<!\\)\\[nr]",
            line,
        ):
            raise ValueError(
                "Code fences need real line breaks, not literal \\n text. "
                "Encode newlines once in JSON; preserve escapes inside code strings."
            )
        opening = _OPENING_FENCE.match(line)
        if opening:
            fence, info = opening.groups()
            if fence[0] == "`" and "`" in info:
                raise ValueError(
                    "Put the opening fence/language, code, and closing fence on "
                    "separate lines; preserve the code and its indentation."
                )

    if fence is not None:
        raise ValueError(
            "Unclosed code block: put the closing fence on its own line, using "
            "the same character and at least the opening fence length. "
            "Use real newlines and preserve code indentation."
        )
