"""Live Gate（`#307`）：真实模型 + 生产工具 + 一次性工作区的可复跑证据入口。

## 它是什么

`#305`/`#319` 要求五类真实场景在**同一 commit/tree 与配置**上各连跑三次、**3/3 才算过**，
每次失败尝试都要保留。本包就是那个入口的最小可用版本：一个可复跑命令
（`python scripts/live_gate.py run`）+ 一份机器证据（`docs/live_gate/**/evidence.json`）+
一个独立复核命令（`... validate <file>`）。后续票（`#312`–`#318`）往
`evaluation/live_gate/scenarios/` 加场景即可，不改运行器。

## 它不是什么（读这段，别误解）

- **不是安全边界**：它自己就是"跑真实模型"的东西；闸门强度取决于它有没有如实判 FAIL/BLOCKED；
- **不取代** Gate-0 与 `AGENTS.md` §14.10 的完整门禁：它只跑**本功能**的场景，`scope.does_not_cover`
  如实列出没覆盖的面；
- **不接受替身当通过**：`GateOptions.capability_fn` 是测试缝，注入即判 `FAIL`（`seams` 有记录）；
  **非内置场景**同样记缝判 `FAIL` —— "内置"由"定义文件是否真的在 `scenarios/` 下"机械核实
  （`registry.is_builtin`），不认调用方的声明（注册表是进程级可写单例）；
- **缺凭证时不发 run**：判 `BLOCKED` 并落盘证据，绝不写 `PASS`（端点不可用那条路**已经发过**
  一次 `max_tokens=1` 的探测请求 —— "不发"指的是不发那 3 次真实尝试）。

## 三条硬纪律

1. **3/3 不可放宽**：`GATE_ATTEMPTS = 3` 是常量，没有 `--attempts` 开关（放松的入口不留）；
2. **凭证零输出**：能打印的只有**键名**与能力状态（`secrets.credential_names`）；
3. **证据可独立复核**：`validate` 重算判定、重读轨迹、比对 sha256、复扫凭证。
"""

from evaluation.live_gate.capability import check_capability
from evaluation.live_gate.registry import (
    AttemptOutcome,
    LiveScenario,
    ScenarioContext,
    get_scenario,
    list_scenarios,
    register_scenario,
    unregister_scenario,
)
from evaluation.live_gate.runner import GateOptions, GateResult, run_gate
from evaluation.live_gate.schema import (
    GATE_ATTEMPTS,
    SCHEMA_VERSION,
    AttemptRecord,
    LiveEvidence,
    Verdict,
    decide_verdict,
    load_evidence,
)
from evaluation.live_gate.validator import (
    CheckResult,
    ValidationReport,
    validate_evidence,
)

__all__ = [
    "GATE_ATTEMPTS",
    "SCHEMA_VERSION",
    "AttemptOutcome",
    "AttemptRecord",
    "CheckResult",
    "GateOptions",
    "GateResult",
    "LiveEvidence",
    "LiveScenario",
    "ScenarioContext",
    "ValidationReport",
    "Verdict",
    "check_capability",
    "decide_verdict",
    "get_scenario",
    "list_scenarios",
    "load_evidence",
    "register_scenario",
    "run_gate",
    "unregister_scenario",
    "validate_evidence",
]
