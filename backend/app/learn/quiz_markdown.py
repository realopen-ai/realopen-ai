"""Check quiz code-block structure without rewriting source code."""

import re

_OPENING_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")


def _closes_fence(line: str, fence: str) -> bool:
    return bool(
        re.fullmatch(rf" {{0,3}}{re.escape(fence[0])}{{{len(fence)},}}[ \t]*", line)
    )


def _misplaced_opening(line):
    """Find an unmatched inline opener, ignoring complete inline code spans."""
    runs = list(re.finditer(r"`+|~{3,}", line))
    index = 0
    while index < len(runs):
        run = runs[index]
        closing = next(
            (j for j in range(index + 1, len(runs)) if runs[j][0] == run[0]), None
        )
        if closing is not None:
            index = closing + 1
            continue
        if (
            len(run[0]) >= 3
            and line[: run.start()].strip()
            and re.fullmatch(r"[\w+.-]*[ \t]*", line[run.end() :])
        ):
            return run.start() + 1
        index += 1
    return None


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
    opened_on = None
    for line_number, line in enumerate(text.splitlines(), 1):
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
        if not opening and (column := _misplaced_opening(line)) is not None:
            raise ValueError(
                f"Misplaced opening code fence at line {line_number}, column {column}: "
                "put a newline before the opening fence. Its language tag belongs on "
                "that line, code starts on the next line, and the closing fence needs "
                "its own line. Preserve code indentation and literal string escapes."
            )
        if opening:
            opened_on = line_number
            fence, info = opening.groups()
            if fence[0] == "`" and "`" in info:
                raise ValueError(
                    "Put the opening fence/language, code, and closing fence on "
                    "separate lines; preserve the code and its indentation."
                )

    if fence is not None:
        raise ValueError(
            f"Unclosed code block opened on line {opened_on}: put the closing fence on its own line, using "
            "the same character and at least the opening fence length. "
            "Use real newlines and preserve code indentation."
        )
