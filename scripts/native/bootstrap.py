"""Bootstrap a private launcher interpreter on older Ubuntu/Debian hosts."""

import os
from pathlib import Path
import subprocess
import sys
import venv


def ensure_python():
    if sys.version_info >= (3, 11):
        return
    root = Path(__file__).resolve().parents[2]
    runtime = root / "data/native"
    bootstrap = runtime / "bootstrap"
    executable = "Scripts/python.exe" if os.name == "nt" else "bin/python"
    python = bootstrap / executable
    if not python.exists():
        venv.EnvBuilder(with_pip=True).create(bootstrap)
    target = runtime / "launcher"
    interpreter = target / executable
    if not interpreter.exists():
        subprocess.run([str(python), "-m", "pip", "install", "uv"], check=True)
        subprocess.run(
            [
                str(python),
                "-m",
                "uv",
                "venv",
                "--python",
                "3.12",
                "--seed",
                str(target),
            ],
            env={**os.environ, "UV_PYTHON_INSTALL_DIR": str(runtime / "python")},
            check=True,
        )
    os.execv(str(interpreter), [str(interpreter), *sys.argv])
