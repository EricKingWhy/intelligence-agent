"""#1200 Phase 1 / 裁决 A1：`GET /api/skills` 增量扩展（不新建端点）。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from agent_harness.config import Settings
from agent_harness.web.app import create_app

MAX_DESCRIPTION = 1536


def _entry(
    name: str,
    *,
    description: str = "desc",
    scope: str = "workspace",
    origin: str = "project",
    plugin: str | None = None,
    enabled: bool = True,
) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        description=description,
        when_to_use="",
        source_path=Path(f"/ws/skills/{name}/SKILL.md"),
        scope=scope,
        origin=origin,
        plugin=plugin,
        enabled=enabled,
    )


class _Capability:
    def __init__(self, entries: list[SimpleNamespace]) -> None:
        self._entries = entries
        self.promoter = None

    def catalog(self):
        return [e for e in self._entries if e.enabled]

    def all_entries(self):
        return list(self._entries)

    def errors(self):
        return []

    def conflicts(self):
        return []


class _FakeAgent:
    def __init__(self, capability: _Capability) -> None:
        self._capability = capability

    async def get_wiring(self):
        return None, SimpleNamespace(skills=self._capability)


def _client(tmp_path: Path, entries: list[SimpleNamespace]) -> TestClient:
    settings = Settings(
        _env_file=None, workspace_dir=str(tmp_path / "ws"), model_api_key="test-key"
    )
    app = create_app(settings)
    app.state.agent = _FakeAgent(_Capability(entries))
    return TestClient(app)


def test_entries_carry_scope_origin_plugin_enabled(tmp_path: Path) -> None:
    client = _client(
        tmp_path,
        [
            _entry("personal-one", scope="global", origin="personal"),
            _entry("project-one", scope="workspace", origin="project"),
            _entry("packaged", scope="workspace", origin="plugin", plugin="acme"),
            _entry("disabled-one", enabled=False),
        ],
    )
    with client:
        body = client.get("/api/skills").json()
    by_name = {s["name"]: s for s in body["skills"]}
    assert by_name["personal-one"]["scope"] == "global"
    assert by_name["personal-one"]["origin"] == "personal"
    assert by_name["project-one"]["scope"] == "workspace"
    assert by_name["project-one"]["origin"] == "project"
    assert by_name["packaged"]["plugin"] == "acme"
    assert by_name["disabled-one"]["enabled"] is False
    assert by_name["personal-one"]["enabled"] is True
    # 既有字段不变
    for entry in body["skills"]:
        assert set(entry) >= {"name", "description", "when_to_use", "source"}
    assert set(body) >= {"skills", "drafts", "errors", "conflicts"}


def test_disabled_skill_listed_but_not_enabled(tmp_path: Path) -> None:
    client = _client(tmp_path, [_entry("code-review", enabled=False)])
    with client:
        body = client.get("/api/skills").json()
    assert [s["name"] for s in body["skills"]] == ["code-review"]
    assert body["skills"][0]["enabled"] is False


def test_description_truncated_to_1536(tmp_path: Path) -> None:
    long = "x" * (MAX_DESCRIPTION + 200)
    client = _client(tmp_path, [_entry("long", description=long)])
    with client:
        body = client.get("/api/skills").json()
    assert len(body["skills"][0]["description"]) == MAX_DESCRIPTION


def test_plugin_omitted_when_absent(tmp_path: Path) -> None:
    client = _client(tmp_path, [_entry("plain")])
    with client:
        body = client.get("/api/skills").json()
    assert "plugin" not in body["skills"][0]


def test_legacy_stub_entries_still_work(tmp_path: Path) -> None:
    """旧测试替身只有 name/description/when_to_use/source_path：必须 getattr 防御。"""

    class _LegacyCapability(_Capability):
        def catalog(self):
            return [
                SimpleNamespace(
                    name="legacy",
                    description="d",
                    when_to_use="",
                    source_path=Path("/ws/legacy/SKILL.md"),
                )
            ]

        def all_entries(self):
            return self.catalog()

    settings = Settings(
        _env_file=None, workspace_dir=str(tmp_path / "ws"), model_api_key="test-key"
    )
    app = create_app(settings)
    app.state.agent = _FakeAgent(_LegacyCapability([]))
    with TestClient(app) as client:
        body = client.get("/api/skills").json()
    entry = body["skills"][0]
    assert entry["name"] == "legacy"
    assert entry["scope"] in {"global", "workspace"}
    assert entry["origin"] in {"personal", "project", "plugin", "oauth"}
    assert entry["enabled"] is True
