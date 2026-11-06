"""Optional native production SPA serving, after API and WebSocket routes."""

from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import FileResponse

from app.services.safe_paths import confined_path


def install_frontend(app, directory):
    if not directory:
        return
    root = Path(directory).resolve()
    if not (root / "index.html").is_file():
        raise RuntimeError("Built frontend missing; run the native setup/build first")

    @app.get("/{path:path}", include_in_schema=False)
    async def frontend(path: str):
        if path.split("/", 1)[0] in {"api", "ws", "metrics"}:
            raise HTTPException(404)
        try:
            target = confined_path(root, path or "index.html")
        except (ValueError, OSError):
            raise HTTPException(404) from None
        if target.is_file():
            return FileResponse(target)
        if path.startswith("assets/"):
            raise HTTPException(404)
        return FileResponse(root / "index.html", headers={"Cache-Control": "no-cache"})
