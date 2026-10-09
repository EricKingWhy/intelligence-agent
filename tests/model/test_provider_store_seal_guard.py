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
触及 store 内容的测试（全 tests/ 共 70 个 create_app 调用文件、111 处调用点，
2026-10-07 实测）按 #764 P3 先例登记为接受残余，不在本守卫范围（全量扫查结论见
tracker #786 节）。

已知范围限制（#808 收口后状态，非豁免理由）：扫描面已覆盖 tests/ 全部 ``*.py``
（排除 ``__pycache__``），conftest/helper/双替身盲区已闭合（#808 实测 49 个非
test_*.py 文件零敏感字面量）；判据是文件级正则、不解析语法树，文件内个别用例未钉
不可见，注释文本恰好带 ``=`` 会被赋值形态判据误判为已钉（漏报方向残余）；正则只认
字面 URL 与调用形态，动态拼接路径（f-string 拼端点等）不命中；HTTP 载荷里的复合 id
（POST model= 载荷等）依赖"全 tests/ 零复合 id 字面量"前提，触达时应在对应文件
直接钉路径。
"""

from __future__ import annotations

import re
from pathlib import Path

TESTS_ROOT = Path(__file__).resolve().parent.parent

# 触及 store 内容敏感面的文件形态：端点 URL 字面量（①provider CRUD、②GET
# /api/models）+ 内容触达机制的代码 API 形态（#808 纳入）——resolve_selection 是
# 复合 id 解析的统一入口（model/config.py），ProviderStore.for_settings 是按
# settings 建库的直调面。支单单独冻结：测试里逐支断言存活（#808 G6），任一支
# 对全 tests/ 零命中即红。改支单 = 改守卫语义，须与下面冻结集同步并过审查。
_SENSITIVE_SURFACE_BRANCHES: tuple[str, ...] = (
    "api/model-providers",
    "api/models",
    "resolve_selection",
    r"ProviderStore\.for_settings",
)
# 冻结集是独立硬编码字面量（先例 tests/tooling/test_review_coverage_lint.py:90），
# 不从支单派生：派生冻结集的相等断言恒真，拦不住静默删支（#808 r2 F-1 实证：
# 删 resolve_selection 支静默绿）。支单任何增删都对不上字面量即红。
_FROZEN_SURFACE_BRANCHES = frozenset(
    {"api/model-providers", "api/models", "resolve_selection",
     r"ProviderStore\.for_settings"}
)
_SENSITIVE_SURFACE_RX = re.compile("|".join(_SENSITIVE_SURFACE_BRANCHES))

# 密封判据是赋值形态，不是"字符串在场"（#808 收紧）：注释/docstring 只提及
# provider_store_path 一词不算钉；判据不解析语法树，注释文本恰好带 ``=`` 时会
# 误判为已钉（漏报方向残余，见模块 docstring"已知范围限制"）；``==`` 比较形态
# （只 assert 不注入）不算已钉，(?!=) 负向排除（#808 r2 F-2）。
_SEAL_RX = re.compile(r"provider_store_path\s*=(?!=)")

# 豁免清单（相对 tests/ 的 posix rel path → 理由；#808 从 basename 改为 rel path
# 键，杜绝同名异目录文件被静默豁免）。形态命中但 store 内容不可达才可豁免；
# 新增豁免必须写明理由，无理由的命中一律判红。
_EXEMPT: dict[str, str] = {
    "web/test_catalog_router.py": (
        "依赖全 fake（get_catalog_state 依赖覆盖 + register_catalog_routes 裸 FastAPI，"
        "无 Settings、无真实 store）"
    ),
    "model/test_provider_store.py": (
        "显式注入 ProviderStore(tmp_path)（Settings 的路径字段不被消费）；"
        "/api/models 为 docstring 引述、resolve_selection 为直调形态，两类命中的"
        "解析对象都是注入的 store"
    ),
    "model/test_model_catalog.py": (
        "仅 docstring 引述 /api/models；测试对象是 env-catalog 解析，不含 store 面"
    ),
    "model/test_reasoning_effort.py": (
        "resolve_selection 调用均用 catalog 名（无冒号 ⇒ 走 from_catalog，"
        "不经 from_custom_provider/store）；测试对象是 reasoning_effort 解析，不含 store 面"
    ),
    "model/test_provider_store_seal_guard.py": (
        "守卫自身——docstring/消息引用端点字面量，无 store 面；消除「靠违规消息"
        "恰好含 provider_store_path 字样通过」的意外自洽"
    ),
}

# 正控样本（#786 P2-1，#808 扩为双正控 + 逐支存活断言；路径相对 tests/ 根）：
# 扫描器空转（正则死亡/范围改坏）或控制文件被删改时，守卫必须响亮红，而不是
# 静默绿。文件级正控钉住扫描面命中：web/test_web_models.py 仅经 api/models 支
# 命中（该支死亡必红）；web/test_model_providers.py 多支命中——单支死亡对它
# 不可见，由 test 里的逐支存活断言（G6）兜底。
_POSITIVE_CONTROLS: frozenset[str] = frozenset(
    {"web/test_web_models.py", "web/test_model_providers.py"}
)


def test_store_content_sensitive_tests_are_sealed():
    hit_sources: dict[str, str] = {}
    violations: list[str] = []
    for test_file in sorted(TESTS_ROOT.rglob("*.py")):
        if "__pycache__" in test_file.parts:
            continue
        source = test_file.read_text(encoding="utf-8", errors="replace")
        rel = test_file.relative_to(TESTS_ROOT).as_posix()
        if not _SENSITIVE_SURFACE_RX.search(source):
            continue
        hit_sources[rel] = source
        if rel in _EXEMPT:
            continue
        if not _SEAL_RX.search(source):
            violations.append(
                f"{rel}：触及敏感面（/api/model-providers、/api/models 或 "
                "resolve_selection / ProviderStore.for_settings 形态）但未钉 "
                "provider_store_path（宿主 HOME 的自定义 provider 会泄进断言）；"
                "比照 tests/web/test_web_models.py 夹具补 "
                "provider_store_path=str(tmp_path / 'model-providers.json')，"
                "或在 _EXEMPT 写明豁免理由"
            )
    missing_controls = sorted(_POSITIVE_CONTROLS - hit_sources.keys())
    assert not missing_controls, (
        "密封守卫正控失效（#786/#808）：扫描面/正则未命中已知敏感文件 "
        f"{missing_controls}——守卫在空转，本轮结果不可信；"
        "请核实 _SENSITIVE_SURFACE_RX 与 rglob 范围未被改坏"
    )
    assert frozenset(_SENSITIVE_SURFACE_BRANCHES) == _FROZEN_SURFACE_BRANCHES, (
        "敏感面支单被增删（#808 G6）：改支单 = 改守卫语义，须显式更新冻结集"
        "并说明理由，静默增删不可接受"
    )
    dead_branches = [
        branch
        for branch in _SENSITIVE_SURFACE_BRANCHES
        if not any(re.search(branch, src) for src in hit_sources.values())
    ]
    assert not dead_branches, (
        "密封守卫正则支单死亡（#808 G6）：_SENSITIVE_SURFACE_BRANCHES 中 "
        f"{dead_branches} 对全 tests/ 零命中——该支已检测不到任何敏感文件，"
        "守卫对该面空转；核实是支单笔误还是敏感面真的移出了扫描范围"
    )
    assert not violations, (
        "provider-store 密封守卫（#786）发现未密封的内容敏感测试：\n"
        + "\n".join(f"  - {v}" for v in violations)
    )
