"""Constrain application-managed files to a trusted filesystem directory.

Reject symlinks in every relative component, including dangling links. This
guards against pre-existing links, not concurrent mutation by a hostile OS user.
"""

import os
from pathlib import Path


def confined_path(root: Path, relative: str | Path) -> Path:
    root = Path(os.path.abspath(root))
    relative = Path(relative)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise ValueError("Unsafe file path")
    # Normalize BEFORE prefix validation. Include the trailing separator so
    # /storage-extra never passes a check against /storage. Keep this explicit:
    # security analyzers recognize abspath/realpath + startswith as a guard.
    target_name = os.path.abspath(os.path.join(str(root), str(relative)))
    root_prefix = os.path.join(str(root), "")
    if not target_name.startswith(root_prefix):
        raise ValueError("File path escapes storage directory")
    target = Path(target_name)
    # Check the root too: application storage must not itself be redirected.
    current = root
    if current.is_symlink():
        raise ValueError("Symbolic links are not allowed")
    for part in relative.parts:
        current_name = os.path.abspath(os.path.join(str(current), part))
        if not current_name.startswith(root_prefix):
            raise ValueError("File path escapes storage directory")
        current = Path(current_name)
        if current.is_symlink():
            raise ValueError("Symbolic links are not allowed")
    resolved_name = os.path.realpath(target)
    resolved_prefix = os.path.join(os.path.realpath(root), "")
    if not resolved_name.startswith(resolved_prefix):
        raise ValueError("File path escapes storage directory")
    return Path(resolved_name)


def bounded_text(path: Path, limit: int) -> str:
    """Read at most limit + 1 bytes, even if a file grows after stat."""
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("Text file is too large")
    return data.decode("utf-8")
