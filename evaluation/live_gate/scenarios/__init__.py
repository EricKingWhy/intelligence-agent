"""内置 Live Gate 场景的注册入口（显式调用，不在 import 时产生副作用 —— 见 registry 模块 docstring）。"""

from __future__ import annotations

from evaluation.live_gate.registry import register_scenario
from evaluation.live_gate.scenarios.smoke import SCENARIO as _SMOKE

#: 内置场景清单。后续票（`#312`–`#318`）把自己的场景加到这里即可被 `--list` / `run` 看见。
BUILTIN_SCENARIOS = (_SMOKE,)


def register_builtin_scenarios() -> tuple[str, ...]:
    """注册内置场景（幂等：已注册的跳过）。返回本次可用的场景 id。"""
    from evaluation.live_gate.registry import list_scenarios

    existing = {scenario.id for scenario in list_scenarios()}
    for scenario in BUILTIN_SCENARIOS:
        if scenario.id not in existing:
            register_scenario(scenario)
    return tuple(scenario.id for scenario in BUILTIN_SCENARIOS)
