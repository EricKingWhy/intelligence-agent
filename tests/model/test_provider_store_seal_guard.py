"""#786：provider-store 密封守卫——store 内容敏感的测试必须钉 provider_store_path。

背景（#764/#765 病根）：``ProviderStore.for_settings``（provider_store.py）按
``settings.provider_store_path`` 建库，默认 ``~/.agent-harness/model-providers.json``
（config.py:174，用户级全局作用域，ADR-0032）。测试里不钉路径的构造会把宿主真配置
（自定义 provider）泄进断言——#764 合并树 models ×2 红的机理。

resolve_selection 语义（model/config.py:375-382）决定了**内容敏感面**只有两类：
① provider CRUD 端点（``/api/model-providers``，列表/创建/删除直接反映 store 内容）；
② ``GET /api/models``（返回体合并自定义 provider 条目）；复合 id
（``<provider>:<model_id>``）解析经 ① 的广告条目回传，同样落在这两类文件的形态里。
本守卫把「内容敏感的测试文件必须密封」冻成不变量。construction-time 只读、断言不
触及 store 内容的测试（~60 个 create_app 调用方）按 #764 P3 先例登记为接受残余，
不在本守卫范围（全量扫查结论见 tracker #786 节）。
"""

from __future__ import annotations

import re
from pathlib import Path

TESTS_ROOT = Path(__file__).resolve().parent.parent

# 触及 store 内容敏感面的文件形态。
_SENSITIVE_SURFACE_RX = re.compile(r"api/model-providers|api/models")

# 豁免清单（文件名 → 理由）：形态命中但 store 内容不可达。
# 新增豁免必须写明理由；无理由的命中一律判红。
_EXEMPT: dict[str, str] = {
    "test_catalog_router.py": (
        "依赖全 fake（get_catalog_state 依赖覆盖 + register_catalog_routes 裸 FastAPI，"
        "无 Settings、无真实 store）"
    ),
    "test_provider_store.py": (
        "显式注入 ProviderStore(tmp_path)（Settings 的路径字段不被消费）；"
        "/api/models 仅出现在 docstring 引述"
    ),
    "test_model_catalog.py": (
        "仅 docstring 引述 /api/models；测试对象是 env-catalog 解析，不含 store 面"
    ),
    "test_model_change.py": (
        "仅 docstring 引述 GET /api/models；被测路径是 model_switch 纯函数"
        "（MagicMock state，不经 ProviderStore.for_settings 真实构造）"
    ),
}


def test_store_content_sensitive_tests_are_sealed():
    violations: list[str] = []
    for test_file in sorted(TESTS_ROOT.rglob("test_*.py")):
        source = test_file.read_text(encoding="utf-8", errors="replace")
        if not _SENSITIVE_SURFACE_RX.search(source):
            continue
        rel = test_file.relative_to(TESTS_ROOT).as_posix()
        if test_file.name in _EXEMPT:
            continue
        if "provider_store_path" not in source:
            violations.append(
                f"{rel}：触及 /api/model-providers 或 /api/models 但未钉 "
                "provider_store_path（宿主 HOME 的自定义 provider 会泄进断言）；"
                "比照 tests/web/test_web_models.py 夹具补 "
                "provider_store_path=str(tmp_path / 'model-providers.json')，"
                "或在 _EXEMPT 写明豁免理由"
            )
    assert not violations, (
        "provider-store 密封守卫（#786）发现未密封的内容敏感测试：\n"
        + "\n".join(f"  - {v}" for v in violations)
    )
