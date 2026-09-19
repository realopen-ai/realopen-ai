#!/usr/bin/env python3
"""
RealOpen-AI — Host-side voice model installer (STDLIB ONLY).

Downloads the ASR + TTS models selected in profiles.yml into the persistent
data directory so the backend (and the web setup wizard) see them as
installed. profiles.yml is the single source of truth — nothing here
hardcodes a model selection; provider IDs appear only as capability
whitelists (which installer to run).

Usage:
    python3 scripts/install-voice-models.py --profile cpu_small --data-dir ./data
    python3 scripts/install-voice-models.py --profile cpu_small --asr-only
    python3 scripts/install-voice-models.py --profile cpu_small --tts-only

Why a separate host script? setup.sh runs on the HOST where only Python3
(stdlib) is guaranteed — no httpx / huggingface_hub. The backend wizard
(backend/app/services/voice_model_installer.py) implements the same flow
with httpx streaming; both write the SAME manifest
(``data/models/voice/.manifest.json``) so either installer's output is
visible to the other:

    {
      "asr": {
        "provider": "qwen3-asr", "model": "Qwen/Qwen3-ASR-0.6B",
        "revision": "main", "type": "asr", "role": "default_asr",
        "path": "asr/Qwen3-ASR-0.6B",
        "files": {"config.json": 731, ...},
        "total_bytes": ..., "complete": true,
        "installed_at": "...", "source": "https://huggingface.co/<repo>@<rev>"
      },
      "tts": {
        "provider": "pocket-tts", "model": "pocket-tts",
        "language": "english_2026-04", "voice": "mary",
        "type": "tts", "role": "default_tts", "path": "tts/pocket-tts",
        "files": {}, "total_bytes": 0, "complete": true,
        "preloaded": false, "package_installed": true, "installed_at": "..."
      }
    }

Progress is honest: byte-accurate percentages only where file sizes are
known; real output lines otherwise (never invented percentages).
Exit code 0 on success (including "already installed" skips), non-zero on
failure.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


# Same-directory import of the profile helper (hand-rolled YAML parser —
# profiles.yml stays the single source of truth on the host too). The file
# name contains a hyphen, so a plain `import` can't load it — use
# importlib with an explicit location.
def _load_profile_helper():
    import importlib.util

    helper_path = Path(__file__).resolve().parent / "profile-helper.py"
    spec = importlib.util.spec_from_file_location("profile_helper", str(helper_path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


profile_helper = _load_profile_helper()

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HF_BASE_URL = "https://huggingface.co"
MANIFEST_FILENAME = ".manifest.json"

# Provider capability whitelists (which installer implementation to run).
# The *selection* comes from profiles.yml — these are not selections.
VALID_ASR_PROVIDERS = {"qwen3-asr"}
VALID_TTS_PROVIDERS = {"pocket-tts"}

READ_CHUNK = 256 * 1024
PROGRESS_MIN_BYTES = 1024 * 1024
PROGRESS_MIN_SECONDS = 1.0

# Reasons that do NOT wipe the target directory before a reinstall.
KEEP_DIR_REASONS = ("not_installed", "runtime package missing")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dir_name(model_id: str) -> str:
    return model_id.split("/")[-1] or model_id


# ─── Voice config resolution (profiles.yml via profile-helper) ───────────────


def resolve_voice_config(profile: str) -> dict:
    """Merged voice config for a profile: {asr: {...}, tts: {...}} (possibly empty)."""
    profiles_path = PROJECT_ROOT / "profiles.yml"
    data = profile_helper.load_profiles(str(profiles_path))
    return profile_helper.merge_voice_config(data, profile)


# ─── Manifest (same schema as the backend installer) ──────────────────────────


def models_dir(data_dir: Path) -> Path:
    return data_dir / "models" / "voice"


def manifest_path(data_dir: Path) -> Path:
    return models_dir(data_dir) / MANIFEST_FILENAME


def read_manifest(data_dir: Path) -> dict:
    p = manifest_path(data_dir)
    try:
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except (json.JSONDecodeError, OSError) as e:
        print(f"  warning: failed to read manifest {p}: {e}")
    return {}


def write_manifest(data_dir: Path, data: dict) -> None:
    p = manifest_path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(p)


def entry_matches(kind: str, cfg: dict, entry: dict) -> bool:
    if entry.get("model") != cfg.get("model"):
        return False
    if entry.get("provider") != cfg.get("provider"):
        return False
    if kind == "asr" and entry.get("revision") != (cfg.get("revision") or "main"):
        return False
    if kind == "tts" and entry.get("language") != cfg.get("language"):
        return False
    return True


def check_installed(kind: str, cfg: dict, data_dir: Path) -> tuple[bool, str]:
    """Idempotency check for one side (asr/tts). Returns (ok, reason)."""
    entry = read_manifest(data_dir).get(kind)
    if not isinstance(entry, dict) or not entry:
        return False, "not_installed"
    if not entry.get("complete"):
        return False, "incomplete"
    if not entry_matches(kind, cfg, entry):
        return False, "changed"
    rel = entry.get("path")
    files = entry.get("files") or {}
    if not rel:
        return False, "manifest entry has no path"
    if kind == "tts" and not files and not entry.get("preloaded"):
        return True, ""  # package-managed cache — flags carry the state
    if not files:
        return False, "manifest entry records no files"
    root = data_dir / "models" / "voice" / rel
    for fname, size in files.items():
        f = root / fname
        if not f.exists():
            return False, f"missing file: {fname}"
        try:
            if f.stat().st_size != int(size):
                return False, f"size mismatch: {fname}"
        except (OSError, TypeError, ValueError):
            return False, f"unreadable file: {fname}"
    return True, ""


# ─── HF download (stdlib urllib, real byte progress) ─────────────────────────


def _hf_headers() -> dict:
    import os

    headers = {"User-Agent": "RealOpen-AI-setup/1.0"}
    token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def list_hf_files(repo_id: str, revision: str) -> list[tuple[str, "int | None"]]:
    """List repo files with sizes via the public tree API."""
    url = (
        f"{HF_BASE_URL}/api/models/{urllib.parse.quote(repo_id, safe='/')}"
        f"/tree/{urllib.parse.quote(revision, safe='')}"
        "?recursive=true"
    )
    req = urllib.request.Request(url, headers=_hf_headers())
    with urllib.request.urlopen(req, timeout=60) as resp:
        entries = json.loads(resp.read().decode("utf-8"))
    out = []
    for e in entries:
        if not isinstance(e, dict) or e.get("type") != "file":
            continue
        path = e.get("path")
        if not path:
            continue
        size = e.get("size")
        out.append((path, size if isinstance(size, int) else None))
    if not out:
        raise RuntimeError(f"no files listed for {repo_id}@{revision}")
    return out


def download_file(
    url: str,
    dest: Path,
    on_bytes: "callable | None" = None,
) -> int:
    """Stream one file to dest (via .part + rename). Returns bytes written."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    req = urllib.request.Request(url, headers=_hf_headers())
    got = 0
    with urllib.request.urlopen(req, timeout=300) as resp, open(tmp, "wb") as fh:
        while True:
            chunk = resp.read(READ_CHUNK)
            if not chunk:
                break
            fh.write(chunk)
            got += len(chunk)
            if on_bytes:
                on_bytes(len(chunk))
    tmp.replace(dest)
    return got


def install_asr(cfg: dict, data_dir: Path) -> bool:
    """Download the ASR model snapshot with byte-accurate progress."""
    provider = cfg.get("provider")
    if provider not in VALID_ASR_PROVIDERS:
        print(
            f"✗ ASR: unsupported provider '{provider}'. "
            f"Supported: {', '.join(sorted(VALID_ASR_PROVIDERS))}"
        )
        return False

    model = cfg["model"]
    revision = cfg.get("revision") or "main"
    display = _dir_name(model)
    target_dir = models_dir(data_dir) / "asr" / _dir_name(model)

    print(f"→ ASR: {model}@{revision} [{provider}]")
    print(f"   Listing files for {model}@{revision}...")
    try:
        files = list_hf_files(model, revision)
    except Exception as e:
        print(f"✗ ASR: failed to list model files: {e}")
        return False

    total_bytes = sum(size for _, size in files if size is not None)
    all_sizes_known = all(size is not None for _, size in files)
    if all_sizes_known:
        print(f"   {len(files)} files, {total_bytes / (1024 * 1024):.1f} MB total")
    else:
        print(f"   {len(files)} files (some sizes unknown — no percent shown)")

    completed = 0
    last_emit = 0.0
    last_bytes = 0
    files_record: dict[str, int] = {}

    def _on_bytes(n: int) -> None:
        nonlocal completed, last_emit, last_bytes
        completed += n
        now = time.monotonic()
        if all_sizes_known and (
            completed - last_bytes >= PROGRESS_MIN_BYTES
            or now - last_emit >= PROGRESS_MIN_SECONDS
        ):
            last_bytes = completed
            last_emit = now
            pct = int(completed / total_bytes * 100) if total_bytes else 0
            print(
                f"   [{pct:3d}%] {completed / (1024 * 1024):8.1f} MB / "
                f"{total_bytes / (1024 * 1024):.1f} MB",
                flush=True,
            )

    for file_path, expected_size in files:
        dest = target_dir / file_path
        # File-level idempotency: correct-size file is skipped.
        if (
            dest.exists()
            and expected_size is not None
            and dest.stat().st_size == expected_size
        ):
            completed += expected_size
            files_record[file_path] = expected_size
            continue
        if not all_sizes_known:
            print(f"   Downloading {file_path} (size unknown — no percent)")
        url = (
            f"{HF_BASE_URL}/{urllib.parse.quote(model, safe='/')}"
            f"/resolve/{urllib.parse.quote(revision, safe='')}"
            f"/{urllib.parse.quote(file_path, safe='/')}"
        )
        try:
            got = download_file(url, dest, _on_bytes)
        except (urllib.error.URLError, OSError) as e:
            print(f"✗ ASR: download failed for {file_path}: {e}")
            return False
        if expected_size is not None and got != expected_size:
            print(
                f"✗ ASR: size mismatch after download: {file_path} "
                f"({got} bytes, expected {expected_size})"
            )
            return False
        files_record[file_path] = got

    if all_sizes_known and total_bytes:
        print(
            f"   [100%] {completed / (1024 * 1024):.1f} MB / "
            f"{total_bytes / (1024 * 1024):.1f} MB"
        )

    entry = {
        "provider": provider,
        "model": model,
        "revision": revision,
        "type": cfg.get("type") or "asr",
        "role": cfg.get("role") or "default_asr",
        "path": f"asr/{_dir_name(model)}",
        "files": files_record,
        "total_bytes": completed,
        "complete": True,
        "installed_at": _now(),
        "source": f"{HF_BASE_URL}/{model}@{revision}",
    }
    manifest = read_manifest(data_dir)
    manifest["asr"] = entry
    write_manifest(data_dir, manifest)
    print(f"✓ ASR: {display} installed ({len(files_record)} files)")
    return True


# ─── TTS (pocket-tts package install + honest state) ──────────────────────────


def install_tts(cfg: dict, data_dir: Path) -> bool:
    """Install the pocket-tts runtime package (pip) and record honest state.

    The package manages its own model assets (materialized on first load);
    pip gives no byte-accurate progress so real output lines are the honest
    progress indicator. Never fabricated percentages.
    """
    provider = cfg.get("provider")
    if provider not in VALID_TTS_PROVIDERS:
        print(
            f"✗ TTS: unsupported provider '{provider}'. "
            f"Supported: {', '.join(sorted(VALID_TTS_PROVIDERS))}"
        )
        return False

    model = cfg.get("model", "pocket-tts")
    print(f"→ TTS: {model} ({cfg.get('language')}) [{provider}]")

    import importlib.util

    module_name = "pocket_tts"
    package_installed = importlib.util.find_spec(module_name) is not None
    if package_installed:
        print("   pocket_tts package already importable — skipping pip install")
    else:
        cmd = [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--no-cache-dir",
            "--progress-bar",
            "off",
            "pocket-tts",
        ]
        print(f"   Running: {' '.join(cmd)}")
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
        )
        assert proc.stdout is not None
        for raw_line in proc.stdout:
            line = raw_line.decode(errors="replace").strip()
            if line:
                print(f"   {line[:200]}")
        rc = proc.wait()
        if rc != 0:
            print(f"✗ TTS: pip install failed (exit code {rc})")
            # Record the honest failure state in the manifest.
            entry = {
                "provider": provider,
                "model": model,
                "language": cfg.get("language"),
                "voice": cfg.get("voice"),
                "type": cfg.get("type") or "tts",
                "role": cfg.get("role") or "default_tts",
                "path": f"tts/{_dir_name(model)}",
                "files": {},
                "total_bytes": 0,
                "complete": False,
                "preloaded": False,
                "package_installed": False,
                "installed_at": _now(),
            }
            manifest = read_manifest(data_dir)
            manifest["tts"] = entry
            write_manifest(data_dir, manifest)
            return False

    # Best-effort programmatic precache (stdlib-level introspection).
    preloaded = False
    if package_installed or importlib.util.find_spec(module_name):
        try:
            import pocket_tts  # noqa: F401 — presence proven by find_spec

            for name in ("download_model", "download", "fetch_model"):
                fn = getattr(pocket_tts, name, None)
                if callable(fn):
                    try:
                        fn(str(models_dir(data_dir) / "tts" / _dir_name(model)))
                        preloaded = True
                        print(f"   pocket_tts.{name} completed")
                    except Exception as e:
                        print(f"   pocket_tts.{name} failed: {e}")
                    break
        except Exception:
            pass
    if not preloaded:
        print(
            "   pocket-tts exposes no programmatic model-download API; its "
            "assets materialize into the cache on first load"
        )

    files: dict[str, int] = {}
    total_bytes = 0
    tts_dir = models_dir(data_dir) / "tts" / _dir_name(model)
    if preloaded and tts_dir.exists():
        for f in tts_dir.rglob("*"):
            if f.is_file():
                files[str(f.relative_to(tts_dir))] = f.stat().st_size
                total_bytes += f.stat().st_size

    entry = {
        "provider": provider,
        "model": model,
        "language": cfg.get("language"),
        "voice": cfg.get("voice"),
        "type": cfg.get("type") or "tts",
        "role": cfg.get("role") or "default_tts",
        "path": f"tts/{_dir_name(model)}",
        "files": files,
        "total_bytes": total_bytes,
        "complete": True,
        "preloaded": preloaded,
        "package_installed": True,
        "installed_at": _now(),
    }
    manifest = read_manifest(data_dir)
    manifest["tts"] = entry
    write_manifest(data_dir, manifest)
    print(f"✓ TTS: {model} ready (package installed, preloaded={preloaded})")
    return True


# ─── Main ─────────────────────────────────────────────────────────────────────


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Install the voice (ASR + TTS) models selected in profiles.yml "
            "into the persistent data directory."
        )
    )
    parser.add_argument(
        "--profile",
        default=None,
        help="hardware profile (default: HARDWARE_PROFILE from .env, else cpu_small)",
    )
    parser.add_argument(
        "--data-dir",
        default=None,
        help=f"data directory (default: {PROJECT_ROOT / 'data'})",
    )
    parser.add_argument(
        "--asr-only", action="store_true", help="install only the ASR model"
    )
    parser.add_argument(
        "--tts-only", action="store_true", help="install only the TTS runtime"
    )
    args = parser.parse_args(argv)

    data_dir = Path(args.data_dir).resolve() if args.data_dir else PROJECT_ROOT / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    profile = args.profile
    if not profile:
        env_file = PROJECT_ROOT / ".env"
        if env_file.exists():
            for line in env_file.read_text(encoding="utf-8").splitlines():
                if line.startswith("HARDWARE_PROFILE="):
                    profile = line.split("=", 1)[1].strip()
                    break
    profile = profile or "cpu_small"

    print(f"Voice model installer (host) — profile '{profile}', data dir {data_dir}")
    cfg = resolve_voice_config(profile)
    if not cfg.get("asr") and not cfg.get("tts"):
        print("No voice section found in profiles.yml — nothing to install.")
        return 0

    ok = True

    if not args.tts_only:
        asr_cfg = cfg.get("asr") or {}
        if not asr_cfg.get("model"):
            print("✗ ASR: profiles.yml voice.asr has no model")
            ok = False
        else:
            installed, reason = check_installed("asr", asr_cfg, data_dir)
            if installed:
                print(
                    f"✓ ASR: {asr_cfg['model']} already installed and valid — skipping"
                )
            else:
                if reason not in KEEP_DIR_REASONS:
                    target = models_dir(data_dir) / "asr" / _dir_name(asr_cfg["model"])
                    if target.exists():
                        print(f"   Reinstalling ({reason}) — clearing {target}")
                        shutil.rmtree(target, ignore_errors=True)
                elif reason != "not_installed":
                    print(f"   Reinstalling ({reason})")
                if not install_asr(asr_cfg, data_dir):
                    ok = False

    if not args.asr_only:
        tts_cfg = cfg.get("tts") or {}
        if not tts_cfg.get("model"):
            print("✗ TTS: profiles.yml voice.tts has no model")
            ok = False
        else:
            installed, reason = check_installed("tts", tts_cfg, data_dir)
            if installed:
                print(
                    f"✓ TTS: {tts_cfg['model']} already installed and valid — skipping"
                )
            else:
                if reason not in KEEP_DIR_REASONS:
                    target = models_dir(data_dir) / "tts" / _dir_name(tts_cfg["model"])
                    if target.exists():
                        print(f"   Reinstalling ({reason}) — clearing {target}")
                        shutil.rmtree(target, ignore_errors=True)
                elif reason != "not_installed":
                    print(f"   Reinstalling ({reason})")
                if not install_tts(tts_cfg, data_dir):
                    ok = False

    if ok:
        print("Voice models ready.")
    else:
        print("Voice model installation FAILED — see errors above.", file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
