"""Serving the built React dashboard (``dashboard/dist``) with SPA fallback."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from cs2bot import __version__
from cs2bot.api.schemas import RootInfo
from cs2bot.config import Settings
from cs2bot.utils.logging import get_logger
from cs2bot.utils.paths import resolve_path

log = get_logger(__name__)

_NO_CACHE = {"Cache-Control": "no-cache"}
_IMMUTABLE = {"Cache-Control": "public, max-age=31536000, immutable"}


def dashboard_dir(settings: Settings) -> Path | None:
    """The dist directory if dashboard serving is on and ``index.html`` exists there."""
    if not settings.api.serve_dashboard:
        return None
    dist = resolve_path(settings.api.dashboard_dist_dir)
    return dist.resolve() if (dist / "index.html").is_file() else None


def _is_api_path(path: str) -> bool:
    return path == "api" or path.startswith("api/")


def install_dashboard(app: FastAPI, settings: Settings) -> Path | None:
    """Register ``/`` (and the SPA catch-all). Must be called after every API router is included."""
    dist = dashboard_dir(settings)
    if dist is None:
        if settings.api.serve_dashboard:
            log.info(
                "dashboard not built (%s missing): serving the API only", settings.api.dashboard_dist_dir
            )

        @app.get("/", include_in_schema=False, response_model=RootInfo)
        def root_hint() -> RootInfo:
            return RootInfo(
                name="cs2bot API",
                version=__version__,
                dashboard="not built - run `npm install && npm run build` in dashboard/ and restart",
                api_docs="/api/docs",
            )

        return None

    root = dist
    index = root / "index.html"

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa(full_path: str) -> FileResponse:
        if _is_api_path(full_path):
            raise HTTPException(status_code=404, detail="not found")
        if full_path:
            try:
                candidate = (root / full_path).resolve()
            except (OSError, ValueError, RuntimeError):
                raise HTTPException(status_code=404, detail="not found") from None
            if candidate.is_relative_to(root) and candidate.is_file():
                headers = _IMMUTABLE if full_path.startswith("assets/") else _NO_CACHE
                return FileResponse(candidate, headers=headers)
            if "." in full_path.rsplit("/", 1)[-1]:
                # a missing file (e.g. an old hashed asset) - a 404 is clearer than index.html
                raise HTTPException(status_code=404, detail="not found")
        return FileResponse(index, headers=_NO_CACHE)

    log.info("serving the dashboard from %s", root)
    return root
