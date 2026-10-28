"""Versioned local skill defaults; customized trees are never overwritten."""

import hashlib
import fcntl
import json
import os
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path

from app.services.safe_paths import confined_path

LOCK = threading.RLock()
MANIFEST = ".builtin-skills.json"


@contextmanager
def locked(root):
    with LOCK, confined_path(root, ".builtin-lock").open("a") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def snapshot(root):
    result = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError("Bundled skill trees cannot contain symbolic links")
        if path.is_file():
            result[path.relative_to(root).as_posix()] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    return result


def atomic_write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temporary:
            temporary_path = temporary.name
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path:
            Path(temporary_path).unlink(missing_ok=True)


def sync(root, bundled):
    """Install missing defaults, then upgrade only unchanged owned versions.

    Backups precede replacement. Untracked copies, added/deleted files,
    changed references and disabled/customized SKILL.md files are preserved.
    """
    with locked(root):
        manifest_path = confined_path(root, MANIFEST)
        try:
            previous = json.loads(manifest_path.read_text())
            if previous.get("schema_version") != 1 or not isinstance(
                previous.get("skills"), dict
            ):
                previous = {}
        except (FileNotFoundError, ValueError, AttributeError):
            previous = {}
        manifest = {"schema_version": 1, "skills": dict(previous.get("skills", {}))}
        for source in sorted(bundled.iterdir()):
            if source.is_symlink() or not source.is_dir():
                continue
            try:
                target = confined_path(root, source.name)
            except ValueError:
                continue
            if target.exists() and not target.is_dir():
                continue
            expected = snapshot(source)
            if not expected or "SKILL.md" not in expected:
                continue
            try:
                current = snapshot(target) if target.exists() else {}
            except ValueError:
                continue  # A linked resource never becomes a managed default.
            saved = manifest["skills"].get(source.name, {})
            saved = saved if isinstance(saved, dict) else {}
            baseline = saved.get("files", {})
            pending = saved.get("pending_files", {})
            # A previous interrupted publication may contain only old/new
            # default bytes. Any third-party edit still makes it customized.
            resumable = bool(
                pending and isinstance(baseline, dict) and isinstance(pending, dict)
            ) and (
                baseline.keys() & pending.keys() <= current.keys()
                and current.keys() <= baseline.keys() | pending.keys()
                and all(
                    value in {baseline.get(name), pending.get(name)}
                    for name, value in current.items()
                )
            )
            known = current == expected
            managed = (
                not current or known or resumable or (baseline and current == baseline)
            )
            if managed:
                if current and current != expected:
                    version = hashlib.sha256(
                        json.dumps(current, sort_keys=True).encode()
                    ).hexdigest()
                    backup = confined_path(
                        root, f".builtin-backups/{source.name}/{version}"
                    )
                    for name in current:
                        atomic_write(
                            confined_path(backup, name),
                            confined_path(target, name).read_bytes(),
                        )
                if current != expected:
                    manifest["skills"][source.name] = {
                        "files": baseline if resumable else current,
                        "pending_files": expected,
                    }
                    atomic_write(
                        manifest_path, (json.dumps(manifest, indent=2) + "\n").encode()
                    )
                for name in expected:
                    if current.get(name) != expected[name]:
                        atomic_write(
                            confined_path(target, name),
                            confined_path(source, name).read_bytes(),
                        )
                for name in current.keys() - expected.keys():
                    confined_path(target, name).unlink()
                manifest["skills"][source.name] = {
                    "files": expected,
                    "update_available": False,
                    "customized": False,
                }
            else:
                manifest["skills"][source.name] = {
                    **saved,
                    "update_available": current != expected,
                    "customized": True,
                }
        if manifest != previous:
            atomic_write(
                manifest_path, (json.dumps(manifest, indent=2) + "\n").encode()
            )
        return manifest
