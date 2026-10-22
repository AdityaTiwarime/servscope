"""Serving the built dashboard: SPA routing, caching, security headers, path traversal."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from conftest import client_for, settings


@pytest.fixture
def ui_dir(tmp_path: Path) -> Path:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>ServScope</title>")
    (dist / "assets" / "app-3f9a.js").write_text("console.log('ui')")
    (tmp_path / "secret.txt").write_text("outside the dashboard folder")
    return dist


async def test_index_is_served_with_security_headers(clean_db: str, ui_dir: Path) -> None:
    async with client_for(settings(clean_db, ui_dir=ui_dir)) as client:
        r = await client.get("/")
    assert r.status_code == 200 and "<title>ServScope" in r.text
    csp = r.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert "unsafe-inline" not in csp.split("script-src")[1].split(";")[0]
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["cache-control"] == "no-cache"


async def test_dashboard_routes_get_the_page(clean_db: str, ui_dir: Path) -> None:
    async with client_for(settings(clean_db, ui_dir=ui_dir)) as client:
        r = await client.get(f"/traces/{'a' * 32}")
    assert r.status_code == 200 and "<title>ServScope" in r.text


async def test_hashed_assets_are_cached_for_a_year(clean_db: str, ui_dir: Path) -> None:
    async with client_for(settings(clean_db, ui_dir=ui_dir)) as client:
        r = await client.get("/assets/app-3f9a.js")
    assert r.text == "console.log('ui')"
    assert "immutable" in r.headers["cache-control"]


@pytest.mark.parametrize(
    "path", ["/../secret.txt", "/%2e%2e/secret.txt", "/assets/../../secret.txt", "/..%2fsecret.txt"]
)
async def test_files_outside_the_dashboard_are_never_served(
    clean_db: str, ui_dir: Path, path: str
) -> None:
    async with client_for(settings(clean_db, ui_dir=ui_dir)) as client:
        r = await client.get(path)
    assert "outside the dashboard folder" not in r.text


async def test_unknown_api_route_is_a_404_not_the_page(clean_db: str, ui_dir: Path) -> None:
    async with client_for(settings(clean_db, ui_dir=ui_dir)) as client:
        r = await client.get("/api/nope")
        api = await client.get("/api/services")
    assert r.status_code == 404 and r.headers["content-type"] == "application/problem+json"
    assert api.status_code == 200  # real API routes still win over the page


async def test_without_a_dashboard_only_the_api_is_served(make_client: Any) -> None:
    async with make_client() as client:
        assert (await client.get("/")).status_code == 404
