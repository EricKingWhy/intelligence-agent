"""W-21 D3 (#815): mount_static honours an explicit build directory.

The Electron shell ships the Vite build as ``<install>/resources/web`` and passes
it as ``Settings.web_dist_dir`` (env ``WEB_DIST_DIR``); the default stays
``<repo>/web/dist`` so every existing deployment is unchanged.
"""

from __future__ import annotations

import os
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


def test_default_prefers_the_build_next_to_the_bundled_runtime(monkeypatch, tmp_path: Path) -> None:
    """W-21 D5 (#817): ``<resources>/python/python.exe`` ⇒ ``<resources>/web``.

    The service started by the TUI (or attached to by the shell) must serve the
    desktop window without anyone telling it where the install put the renderer
    build, so the default derives it from the runtime's own location.
    """
    from agent_harness.web import app as app_module

    resources = tmp_path / "resources"
    built = _built_ui(resources)
    interpreter = resources / "python" / ("python.exe" if os.name == "nt" else "python")
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text("", encoding="utf-8")
    monkeypatch.setattr(app_module.sys, "executable", str(interpreter))
    monkeypatch.setattr(app_module, "_repo_web_dist", lambda: tmp_path / "no-repo-build")

    app = FastAPI()
    app_module.mount_static(app)
    index = TestClient(app).get("/")
    assert index.status_code == 200
    assert "staged-ui" in index.text
    assert app_module._bundled_web_dist() == built


def test_default_falls_back_to_the_repo_build(monkeypatch, tmp_path: Path) -> None:
    """dev 形态：运行时旁边没有产物时仍用 ``<repo>/web/dist``（既有行为）。"""
    from agent_harness.web import app as app_module

    built = _built_ui(tmp_path / "repo")
    monkeypatch.setattr(app_module, "_bundled_web_dist", lambda: tmp_path / "absent")
    monkeypatch.setattr(app_module, "_repo_web_dist", lambda: built)

    app = FastAPI()
    app_module.mount_static(app)
    assert TestClient(app).get("/").status_code == 200


def test_explicit_directory_never_falls_back(monkeypatch, tmp_path: Path) -> None:
    """显式目录说话算话：目录不存在就是不挂载，不回落到任何默认候选（W-21 D3 语义）。"""
    from agent_harness.web import app as app_module

    monkeypatch.setattr(app_module, "_bundled_web_dist", lambda: _built_ui(tmp_path / "bundle"))

    app = FastAPI()
    app_module.mount_static(app, str(tmp_path / "absent"))
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
