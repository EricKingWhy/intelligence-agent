"""内置 Live Gate 场景的注册入口（显式调用，不在 import 时产生副作用 —— 见 registry 模块 docstring）。"""

from __future__ import annotations

from evaluation.live_gate.registry import register_scenario
from evaluation.live_gate.scenarios.long_task import SCENARIO as _LONG_TASK
from evaluation.live_gate.scenarios.pause_resume import SCENARIO as _PAUSE_RESUME
from evaluation.live_gate.scenarios.smoke import SCENARIO as _SMOKE

#: 内置场景清单。后续票（`#313`–`#318`）把自己的场景加到这里即可被 `--list` / `run` 看见。
BUILTIN_SCENARIOS = (_SMOKE, _LONG_TASK, _PAUSE_RESUME)


def register_builtin_scenarios() -> tuple[str, ...]:
    """注册内置场景（幂等：已注册的跳过）。返回本次可用的场景 id。

    `builtin=True` 不是"声明"而是"申请核实"（`registry._is_shipped`：定义文件必须真的在
    本目录下）——只有核实过的场景才允许产出 `PASS`，否则替身场景能往 `docs/live_gate/`
    写出一份看起来完全正常的通过证据。
    """
    from evaluation.live_gate.registry import list_scenarios

    existing = {scenario.id for scenario in list_scenarios()}
    for scenario in BUILTIN_SCENARIOS:
        if scenario.id not in existing:
            register_scenario(scenario, builtin=True)
    return tuple(scenario.id for scenario in BUILTIN_SCENARIOS)
