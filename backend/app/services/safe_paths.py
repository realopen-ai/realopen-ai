"""Constrain application-managed files to a trusted filesystem directory.

Reject symlinks in every relative component, including dangling links. This
guards against pre-existing links, not concurrent mutation by a hostile OS user.
"""

from pathlib import Path


def confined_path(root: Path, relative: str | Path) -> Path:
    root = Path(root).absolute()
    relative = Path(relative)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("Unsafe file path")
    target = root / relative
    # Check the root too: application storage must not itself be redirected.
    current = root
    if current.is_symlink():
        raise ValueError("Symbolic links are not allowed")
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("Symbolic links are not allowed")
    if not target.resolve().is_relative_to(root.resolve()):
        raise ValueError("File path escapes storage directory")
    return target


def bounded_text(path: Path, limit: int) -> str:
    """Read at most limit + 1 bytes, even if a file grows after stat."""
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("Text file is too large")
    return data.decode("utf-8")
