"""Mount the built web SPA onto the FastAPI app."""

from __future__ import annotations

import re
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles


def _strip_crossorigin(html_bytes: bytes) -> bytes:
    """Remove ``crossorigin`` attributes from script/link tags."""
    html = html_bytes.decode("utf-8")
    html = re.sub(r'\s+crossorigin(?:="[^"]*")?', "", html)
    return html.encode("utf-8")


def setup_frontend(app: FastAPI, build_path: Path) -> bool:
    """Mount frontend static files. Returns True if the build was found."""
    if not build_path.exists():
        return False

    assets_dir = build_path / "assets"
    if assets_dir.exists():
        app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

    @app.get("/")
    async def serve_root():
        content = (build_path / "index.html").read_bytes()
        content = _strip_crossorigin(content)
        return Response(
            content=content,
            media_type="text/html",
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )

    @app.get("/{full_path:path}")
    async def serve_frontend(full_path: str):
        file_path = (build_path / full_path).resolve()

        if not file_path.is_relative_to(build_path.resolve()):
            return Response(status_code=403)

        if file_path.is_file():
            return FileResponse(file_path)

        content = (build_path / "index.html").read_bytes()
        content = _strip_crossorigin(content)
        return Response(
            content=content,
            media_type="text/html",
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )

    return True
