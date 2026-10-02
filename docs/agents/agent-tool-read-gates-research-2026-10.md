# Agent 实读准入检查能力调研（2026-10）

## 问题与结论

调研问题：ZCode／WorkBuddy 能否依照当前会话的实际文件读取证据，在本 Task 缺少必读依据时，阻止依赖它的工具动作，并阻止 Agent 给出正式完成结论？

**两套随附编码 Agent CLI 都提供工具前置 Hook，可拒绝动作；也提供结束阶段的 Hook，可要求 Agent 继续。因此具备做小型证据准入原型的机制基础。现有证据不足以把它们称为 fail-closed 的生产闸门。** 最大未知是 Hook 能否可靠判定 Read 结果完整、未截断且覆盖所需章节，以及 Hook 未加载、运行失败、超时或会话恢复时是否仍会拒绝相关动作。ZCode 当前版本还明确不执行仓库级 Hook 配置。

这是一份可行性事实调研，不是实现设计、授权变更或验收报告。没有启用任何 Hook，没有改用户配置，也没有运行带 Hook 的会话。

## 版本和证据范围

本项目 v6 实测记录的工具为 ZCode 3.14.4.7912／随附 CLI 0.16.9，以及 WorkBuddy 5.6.2.0／随附 CodeBuddy CLI 2.147.0；请求模型分别为 GLM-5.3-Flash 与 `cline-pass/deepseek-v4.1-flash`（WorkBuddy 响应实际模型标识为 `deepseek/deepseek-v4.1-flash`）。冻结记录见[GLM v6](agents-md-eval-2026-10/zcode-glm-v6.json)和[WorkBuddy v6](agents-md-eval-2026-10/workbuddy-v6-summary.json)。两边的 v6 测试都关闭 Hooks，所以这些记录不能证明开启 Hook 后能修复模型失败。测试使用桌面随附的 CLI／执行引擎；没有验证桌面 GUI 或连续数小时的生产任务。

ZCode 证据来自当前安装包 `D:/DevTools/ZCode/resources/glm/packages/zcode-guide-plugin/skills/diagnosing-hooks/SKILL.md`、同目录的 `zcode-configuration-guide/SKILL.md` 和[官方 Hooks 文档](https://zcode.z.ai/en/docs/hooks)。WorkBuddy 随附包 `D:/DevTools/WorkBuddy/resources/app.asar.unpacked/cli/product.json` 将 CodeBuddy Code 功能问题指向随附 CLI 文档或官方文档；随附代码 `D:/DevTools/WorkBuddy/resources/app.asar.unpacked/cli/dist/codebuddy-lite-wb.mjs` 将用户 settings 路径指向 `WORKBUDDY_CONFIG_DIR` 或 `~/.workbuddy/settings.json`。Hook 机制依据[腾讯云官方 Hooks 文档](https://cloud.tencent.com/document/product/1831/137030)、[权限规则](https://cloud.tencent.com/document/product/1831/137059)、[CodeBuddy CLI 设置](https://www.workbuddy.ai/docs/cli/settings)和[WorkBuddy 配置分离版本说明](https://www.workbuddy.ai/docs/cli/release-notes/v2.48.0)。腾讯文档将 CodeBuddy CLI Hooks 标注为 Beta、最低版本 1.16.0；本机随附 CLI 2.147.0。CodeBuddy 通用文档里的 `.codebuddy/` 路径不能直接推定适用于 WorkBuddy 主应用：v2.48.0 起 WorkBuddy 另用 `.workbuddy/` 配置目录，本机代码也确认其用户 settings 路径；WorkBuddy 桌面主任务是否接入该 CLI Hook runtime，公开资料与本次检查均未证实。查阅日期：2026-10-02。

## 能力对照

| 能力 | ZCode | WorkBuddy 随附 CodeBuddy Code |
| --- | --- | --- |
| 工具调用前拦截 | `PreToolUse` 可 allow／ask／deny，可在 Edit/Write 执行前拒绝。配置 Hook 必须启用；插件 Hook 可启用 Hook runner。 | `PreToolUse` 可用退出码 2 阻断，或用 JSON deny；官方权限文档说明该 Hook 在常规权限判断前运行。CodeBuddy CLI 通用文档列出 `.codebuddy/settings.json` 项目配置；WorkBuddy v2.48.0 起将 WorkBuddy 配置与 CLI 的 `.codebuddy/` 分开，本机代码确认用户 settings 位于 `.workbuddy/`。WorkBuddy 专属项目 Hook 路径与桌面主任务接线仍未证实。 |
| 读取结果证据 | `PostToolUse` 提供结构化 `tool_response`，Hook 输入含临时 `transcript_path`。公开契约没有说明 Read 返回的范围、截断标志及完整性字段。 | `PostToolUse` 输入含 `tool_response` 和 `transcript_path`；官方示例展示 Write，没有说明 Read 的结果体和完整性字段。 |
| 阻止结束 | `Stop` 可检查 `last_assistant_message` 并要求主模型续行；官方说明连续 3 次后强制结束。 | `Stop` 可要求 Agent 继续；`continueOnBlock` 用于条件满足前持续工作。超时、执行预算耗尽或 Hook 异常时的拒绝保证还需实测。 |
| 仓库内共享配置 | **仓库级 Hook 配置当前不生效。** 官方明确说明 `.zcode/config.json` 和 `zcode.json` 中的 hooks 整体忽略。共享方式是经插件分发、由成员安装启用，或各自配置用户级 Hook；设置在新会话开始时读取快照。 | CodeBuddy CLI 通用文档列出项目级 `.codebuddy/settings.json` 和项目脚本路径；WorkBuddy 的官方版本说明和本机包都显示它使用独立 `.workbuddy/` 配置目录。哪些 CLI Hook 设置能随 WorkBuddy 项目共享、桌面主会话是否调用该 Hook runtime，仍未证实。 |
| Hook 故障时阻断 | **文档没有给出 fail-closed 保证。** 专用阻断码可拒绝；其他非零码、无效输出属于可恢复 Hook 错误。Hook 未加载、崩溃或超时不能直接视为已拒绝动作。 | 退出码 2 阻断，其他非零码不阻断；Hook 默认有 60 秒超时。官方文档没有保证超时一定拒绝关联工具调用。 |

## 对“按实际读取准入”的含义

脚本可检查的读取证据应限于：当前会话调用了指定 Read 工具；规范化后的目标路径匹配必读文件；调用成功；返回范围覆盖指定章节；结果未报告截断或读取错误。Glob 找到文件、根文件提及文件名、摘要说“已读”，都不构成读取证据。

工具事件能提供调用和返回结果，**不代表模型理解或遵循了内容**。若规则要求读到 EOF，门禁还要处理 `offset`／`limit` 分页、重复或缺页、别名路径、重试、并行读取、编码和恢复会话。两边公开的 Hook 资料都没有证明这些 Read 完整性细节可在 Hook 输入中无损核验。

任务触发哪些规则也需要可靠确定。若仍由 GLM／DeepSeek 自报“已读”或填 READY，就只是把现有误报搬进 Hook。每 Task 无条件重读全部文档则增加负担，不符合用户已确定的分阶段和按 Task 阅读范围。

所以 `PreToolUse` 有能力让未满足前置条件的相关写操作无法执行；`Stop` 可在结束时检查证据并要求继续。这条路径值得原型验证，但 Hook 是扩展点，不是项目规则解释器。Hook 未启用、失效、用户绕过启动入口等边界尚未证明，不能宣称仅凭 Hook 就形成绝对保障。

## 后续验证范围

若继续，先确认目标是桌面主应用还是随附 CodeBuddy CLI。若目标是 WorkBuddy 桌面，第一步应证明主任务实际走哪个运行器及其 Hook 配置路径；不能以 CLI 支持替代该证据。之后才对本机指定版本做小型原型：确认 Hook 确实注册；分别核验完整成功 Read 放行，以及缺文件、错误路径、截断、读取拒绝、Hook 超时和恢复会话时的处理；再检查不完整任务能否被错误地显示为已完成。测试使用两个指定模型的实际工具调用。

原型通过后再讨论是否作为仓库共享准入规则，以及如何与 V3.1-lite 的 Tracker、Review Coverage 和夜间任务证据衔接。基于 Hook prompt 的小模型判断不应单独承担这道闸门。此文件没有设计或实现 Hook。

目前没有新增脚本或配置，没有更改本机用户设置，也没有操作桌面 UI；未调研服务端或 CI API。
