"""Small terminal status labels; redirected output stays free of ANSI escapes."""

import os
import sys

STYLES = {
    "info": ("info", "36"),
    "run": ("run", "36"),
    "start": ("start", "36"),
    "success": ("done", "32"),
    "ready": ("ready", "32"),
    "warning": ("warn", "33"),
    "error": ("error", "31"),
}


def log(message, level="info", *, file=None):
    stream = (
        file if file is not None else (sys.stderr if level == "error" else sys.stdout)
    )
    label, color = STYLES[level]
    prefix = f"[{label}]"
    terminal = getattr(stream, "isatty", lambda: False)()
    supported = os.name != "nt" or any(
        os.environ.get(key) for key in ("WT_SESSION", "ANSICON", "TERM")
    )
    if (
        terminal
        and supported
        and "NO_COLOR" not in os.environ
        and os.environ.get("TERM") != "dumb"
    ):
        prefix = f"\033[{color}m{prefix}\033[0m"
    print(f"{prefix} {message}", file=stream, flush=True)
