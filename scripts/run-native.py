#!/usr/bin/env python3
"""Cross-platform native launcher. See scripts/README.md."""

import subprocess
import sys
from native.cli import main
from native.bootstrap import ensure_python
from native.output import log

if __name__ == "__main__":
    try:
        ensure_python()
        main()
    except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as exc:
        log(f"Native startup failed: {exc}", "error")
        if sys.platform == "win32":
            log(
                "Direct Windows support is experimental. Install WSL with 'wsl --install', restart if requested, then rerun start-native.ps1.",
                "warning",
                file=sys.stderr,
            )
        sys.exit(1)
