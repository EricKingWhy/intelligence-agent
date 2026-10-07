"""W-21 D3 (#815): mount_static honours an explicit build directory.

The Electron shell ships the Vite build as ``<install>/resources/web`` and passes
it as ``Settings.web_dist_dir`` (env ``WEB_DIST_DIR``); the default stays
``<repo>/web/dist`` so every existing deployment is unchanged.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_harness.config import Settings


def _built_ui(root: Path) -> Path:
    dist = root / "web"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>staged-ui</title>", encoding="utf-8")
    (dist / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
    return dist


def test_explicit_directory_is_mounted_and_served(tmp_path: Path) -> None:
    from agent_harness.web.app import mount_static

    app = FastAPI()
    mount_static(app, str(_built_ui(tmp_path)))
    client = TestClient(app)
    index = client.get("/")
    assert index.status_code == 200
    assert "staged-ui" in index.text
    assert client.get("/assets/app.js").status_code == 200


def test_absent_directory_is_a_noop(tmp_path: Path) -> None:
    from agent_harness.web.app import mount_static

    app = FastAPI()
    mount_static(app, str(tmp_path / "absent"))
    assert TestClient(app).get("/").status_code == 404


def test_settings_carry_the_directory_from_the_environment(monkeypatch, tmp_path: Path) -> None:
    dist = _built_ui(tmp_path)
    monkeypatch.setenv("WEB_DIST_DIR", str(dist))
    assert Settings().web_dist_dir == str(dist)


def test_create_prod_app_passes_the_configured_directory(monkeypatch, tmp_path: Path) -> None:
    import agent_harness.web.app as app_module

    seen: list[str | None] = []
    monkeypatch.setattr(app_module, "create_app", lambda settings, **kwargs: FastAPI())
    monkeypatch.setattr(
        app_module, "mount_static", lambda app, web_dist_dir=None: seen.append(web_dist_dir)
    )
    settings = Settings()
    settings.web_dist_dir = str(tmp_path / "web")

    app_module.create_prod_app(settings)

    assert seen == [str(tmp_path / "web")]
