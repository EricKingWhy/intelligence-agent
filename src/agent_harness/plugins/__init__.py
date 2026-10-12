"""跨市场插件导入的**包生命周期**族级目录（PRD #868 / ADR-0052）。

只放与包**来源/版本/作用域/启用**有关、且建立在既有 `agent_harness.skills` 安装
机器之上的生命周期实现（当前成员：MCP server 描述包的 `mcp_package_manager`）。

**依赖方向是单向的 `plugins → skills`**（#974）：各车道自己的预检住在自己的车道里，
`skills/` **不得**反向 import `plugins/`（`tests/test_package_boundary.py` 守这条）。
因此两车道的**共享模块**——MCP server 描述预检（`inspect_mcp_servers` /
`attach_mcp_section`）——住在既不属于 skills 也不属于 plugins 的中性顶层模块
`agent_harness.mcp_precheck`，由 Skill 安装闸门与 MCP 包生命周期各自 import；
把折 MCP 段的逻辑直接写进 `skills/package_manager.py` 会让
`skills → plugins → skills` 成环（#875 Standards 轴 P1）。
"""
