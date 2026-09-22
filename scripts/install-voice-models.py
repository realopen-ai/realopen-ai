#!/usr/bin/env python3
"""
RealOpen-AI — Host-side voice model installer (thin delegator).

The ONE real installer lives in the backend
(``backend/app/services/voice_model_installer.py``): it lets HuggingFace
handle the model downloads NATURALLY (``huggingface_hub.snapshot_download``
for ASR, ``pocket_tts.TTSModel.load_model`` for TTS) into the PERSISTED
hub cache (``data/huggingface/hub`` — pinned via HF_HOME, survived by the
``./data:/app/data`` bind mount), installs the runtime pip packages into
the persistent wheelhouse (``data/pip-wheels``), and writes the manifest
(``data/models/voice/.manifest.json``). This script simply runs it, so
there is exactly ONE implementation (two drifted implementations was the
root of several setup bugs).

Usage:
    python3 scripts/install-voice-models.py                       # backend venv
    python3 scripts/install-voice-models.py --profile cpu_small
    python3 scripts/install-voice-models.py --asr-only
    python3 scripts/install-voice-models.py --tts-only
    python3 scripts/install-voice-models.py --backend-python /path/to/python

How the backend interpreter is located:
    1. ``--backend-python`` flag (explicit),
    2. ``backend/.venv/bin/python`` (poetry/dev venv),
    3. ``python3`` from PATH when the repo's backend dependencies are
       importable (checked with a lightweight ``-c`` import probe),
    4. fallback: print the exact commands to run (web setup wizard or
       docker compose exec) and exit with code 2.

Exit code 0 on success (including "already installed" skips), non-zero on
failure.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BACKEND_DIR = PROJECT_ROOT / "backend"
os.environ.setdefault("HF_HOME", str(PROJECT_ROOT / "data" / "huggingface"))


def _candidates() -> list[Path]:
    out: list[Path] = []
    venv = BACKEND_DIR / ".venv" / "bin" / "python"
    if venv.exists():
        out.append(venv)
    out.append(Path(sys.executable))
    for name in ("python3", "python"):
        found = shutil.which(name)
        if found:
            out.append(Path(found))
    return out


def _backend_importable(py: Path) -> bool:
    """Cheap probe: can this interpreter import the backend's deps?"""
    probe = (
        "import sys; sys.path.insert(0, %r); "
        "import yaml, httpx; import app.config" % str(BACKEND_DIR)
    )
    try:
        result = subprocess.run(
            [str(py), "-c", probe],
            capture_output=True,
            timeout=60,
            cwd=str(BACKEND_DIR),
        )
        return result.returncode == 0
    except (subprocess.SubprocessError, OSError):
        return False


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Install voice (ASR + TTS) models — delegates to the backend installer"
    )
    parser.add_argument("--profile", default=None, help="hardware profile name")
    parser.add_argument("--asr-only", action="store_true")
    parser.add_argument("--tts-only", action="store_true")
    parser.add_argument(
        "--backend-python",
        default=None,
        help="path to a Python interpreter with the backend dependencies",
    )
    args = parser.parse_args()

    candidates: list[Path] = []
    if args.backend_python:
        candidates.append(Path(args.backend_python))
    candidates.extend(_candidates())

    # Pin the HF cache location for the child process too (the backend
    # module pins it internally as well — belt and braces).
    env = dict(os.environ)
    data_dir = PROJECT_ROOT / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    env.setdefault("HF_HOME", str(data_dir / "huggingface"))

    for py in candidates:
        if not py.exists():
            continue
        if py != Path(sys.executable) and not _backend_importable(py):
            continue
        if py == Path(sys.executable) and not _backend_importable(py):
            continue

        cmd = [
            str(py),
            "-m",
            "app.services.voice_model_installer",
        ]
        if args.profile:
            cmd += ["--profile", args.profile]
        if args.asr_only:
            cmd += ["--asr-only"]
        if args.tts_only:
            cmd += ["--tts-only"]

        print(f"[install-voice-models] {' '.join(cmd)}  (cwd={BACKEND_DIR})")
        try:
            result = subprocess.run(cmd, cwd=str(BACKEND_DIR), env=env)
        except (subprocess.SubprocessError, OSError) as e:
            print(f"[install-voice-models] failed to run {py}: {e}")
            continue
        return result.returncode

    print(
        "[install-voice-models] No interpreter with the backend dependencies "
        "was found.\n\n"
        "Use ONE of these instead:\n"
        "  1. Web setup wizard — start the app and open the setup flow; the\n"
        "     voice models download naturally into data/huggingface/hub.\n"
        "  2. Docker:   docker compose exec backend python -m \\\n"
        "                 app.services.voice_model_installer\n"
        "  3. Dev venv: cd backend && poetry install && python -m \\\n"
        "                 app.services.voice_model_installer\n"
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
