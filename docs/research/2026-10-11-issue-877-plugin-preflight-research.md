# #877 DSH 推理滑条适配与完整性预检调研

调研日期：2026-10-11。目标仓库：`C:/Users/王浩宇/.codex/worktrees/issue-877-dsh-adapter/intelligence-agent`，核对 HEAD `09e6142cf47b63e94de712bfec51ee6cab2b0ec9`。目标插件浅克隆：`C:/Users/王浩宇/AppData/Local/Temp/issue877-slider-a0fb13c0dd5549e3872c306f3aa5146d`，HEAD `af723caf3387e64ae28aa69c4fd235b1b662e3ae`。DSH 与 Pi 只读克隆路径及 SHA 列于来源表；未执行或安装第三方插件。

## 结论

建议 `ADAPT` 静态兼容清单与 fail-closed 判定，`PORT DESIGN` DSH 插件的交互语义，并复用 #865 已有的本项目模型能力、Composer slider 与 Provider 映射。不要把 DSH bundle/Cordis 插件接入本项目运行时，也不要读取/改写 DSH 私有 DOM。只有固定来源与摘要匹配、每项必要贡献均有本项目映射和验收证据时，才可报告完整兼容并启用；否则保留可安装/可检查状态，标为 `needs-adaptation` 并禁用。

关键差异：目标插件把真实 effort ID `off` 当成第一档并提交给 DSH；#865 的首档 `Default` 是 `null`，Provider 因而省略请求字段。两者不等价。当前 #865 证据未声明模型支持 `off`，所以固定目标包可以被静态识别，但不能被报告为完整兼容。

## 来源与复用判定

| 来源与固定版本 | 机制与对本票的契合 | 与本项目边界的冲突 | 判定 | License |
| --- | --- | --- | --- | --- |
| 目标 `dsh-codex-effort-slider`，commit `af723caf3387e64ae28aa69c4fd235b1b662e3ae`，tree `8a732b7c00ba5da3b06e122c243205db4ab190b1`；Git archive SHA-256 `122917c83671d5e2ec9f759875db071d47cf738c5774f0810a9d428d20ccce0c` | `package.json:2-6,16-24,38-48` 声明版本、MIT、Node ≥22、DSH bundle patch、web client；`README.md:153-155` 从当前 DSH 模型目录读 `reasoning.efforts`，通过 `directory.select` 写回。`lib/client.js:662-700,1298-1312` 在 DSH 模型菜单 DOM 中插入可访问滑条。 | 这是可执行的 DSH/Cordis 包，依赖 DSH 菜单/DOM 和 `directoryFor`；manifest 未声明 DSH/Cordis peer 版本。不得运行代码或通过 DOM 猜测位置。菜单内控件位置不能直接移植，项目已有 Composer 原生控制。 | `PORT DESIGN`：只转译离散档位选择、模型目录驱动和可访问交互；不复用运行包。 | MIT，Copyright 2026 dsh-codex-effort-slider contributors；若复制代码须保留版权/许可。 |
| DSH cookbook + runtime，clone `D:/reference/deepseek-harness`，commit `5badb15009ae1756c3afe0ae0cef1faafc290ccc` | Cookbook `docs/cookbook/extension-cookbook.md:35-37,98-132` 按公开扩展接缝区分 UI、事件与业务映射；`plugin-compatibility.ts:1-5,61-88` 只读 manifest 并用 semver 检查 DSH peer；`compatibility-preflight.ts:63-66,89-117` 冲突时设置 `disabled`。 | DSH preflight 只判断 peer 版本，不证明 UI/业务语义兼容；未解析 manifest 会留给 loader 失败（`:63-66`），比本票完整兼容判据宽松。不可原样照搬该宽松分支。 | `ADAPT`：借鉴静态 manifest 检查和禁用不兼容项；项目侧还须逐贡献核验并对缺/坏清单 fail-closed。 | MIT，Copyright 2026 DeepSeek。 |
| VS Code Extension API 官方文档，2026-10-11 读取 | Manifest 用 `engines.vscode` 表达宿主版本约束、`contributes` 声明贡献、`activationEvents` 声明激活入口；贡献点是 JSON 声明表。适合作为显式贡献清单与目标版本检查的产品范式。[Extension Manifest](https://code.visualstudio.com/api/references/extension-manifest) · [Contribution Points](https://code.visualstudio.com/api/references/contribution-points) · [Activation Events](https://code.visualstudio.com/api/references/activation-events) | VS Code manifest 描述完整可执行 IDE 扩展 API；不能直接用作 DSH 插件格式，也不能证明模型档位或 UI 语义等价。 | `PORT DESIGN`：借鉴声明式兼容目标和贡献表，不引入其扩展宿主/API。 | 仅引用官方文档、未复制代码；所引页面未声明代码 License，不据此主张复用许可。 |
| Claude Code plugins 官方文档，2026-10-11 读取 | `.claude-plugin/plugin.json` 声明组件和元数据；`claude plugin validate` 在运行前检查 manifest、frontmatter、路径；版本字段可固定插件版本。[Plugins](https://code.claude.com/docs/en/plugins) · [Create](https://code.claude.com/docs/en/plugins/create) · [Manifest](https://code.claude.com/docs/en/plugins/manifest-reference) | 官方文档说明 enabled plugin 每个 session 都生效且插件可用用户权限执行任意代码；`defaultEnabled` 默认是 `true`，与本项目显式启用、信任和隔离边界相反。版本字段不校验 SemVer，Git marketplace pin 是 branch/tag，达不到本票不可变 commit pin。 [Security](https://code.claude.com/docs/en/plugins/security) · [Install](https://code.claude.com/docs/en/plugins/install) | `ADAPT`：只借鉴加载前校验、组件路径与清单核验；不采纳默认启用或执行模型。Git 来源固定完整 commit 和内容摘要。 | 仅引用 Anthropic 官方文档，未复制代码；页面未提供可复用代码 License。 |
| Pi，clone `D:/reference/pi`，commit `1b347794e2a630e4359f2584f4eea388145d0ddf` | `packages/coding-agent/src/core/pi-manifest.ts:1-35` 显式列出 `extensions/skills/prompts/themes`；`extensions/loader.ts:720-803,808-855` 按清单/目录发现后加载；`packages/coding-agent/docs/packages.md:3-5,19-21,38,55-72` 声明资源清单、项目信任与固定 Git commit/tag。 | Pi manifest 最终交给可执行模块 loader；自动发现和导入不满足 `plugins inspect` 只读、不执行包代码的约束。 | `PORT DESIGN`：借鉴显式资源枚举；不复用其自动发现/加载器。 | MIT，Copyright 2025 Mario Zechner。 |

## 与 #865 的贡献逐项对账

代码依据以本 worktree HEAD `09e6142cf47b63e94de712bfec51ee6cab2b0ec9` 为准。#865 月档记载实现提交 `1c8935c1`、`7cf6900e`、`5964714e`；定向模型测试 25 passed、API parser 128 passed、UI Vitest 61 passed、Playwright 28 passed，见 `docs/phase_status/2026-10.md:1148-1153`。

| 源包用户可见贡献 | #865 / 本项目现状与证据 | 预检映射结论 |
| --- | --- | --- |
| 当前模型的离散 effort 档、顺序与默认 | `Composer.tsx:413-429` 按模型 `supported` 过滤目录项并采用声明默认；`config.py:108-117,128-163` 要求 supported/default/wire_mapping 自洽。 | 可适配到本项目模型能力数据；不从插件代码推断能力。 |
| 选择档位并写入模型请求 | `provider.py:95-104` 校验所选档位、取模型 wire mapping 并发送 `reasoning_effort`；测试 `tests/model/test_reasoning_effort.py:40-45,71-73` 覆盖 `minimal/standard/deep` 与映射。 | 适配至本项目 Provider 边界；不是调用 DSH `directory.select`。 |
| `Off` 第一档 | 目标 `README.md:174-178` 明确 Off 语义；`test/client.test.mjs:164-168,320-324` 证实拖至左端和 Home 都将 `reasoningEffort="off"` 提交给宿主；`lib/client.js:1117-1121,1220-1234` 按档位 ID 写回。#865 `ReasoningEffortSlider.tsx:89-115,122-125` 将第一档映射为 `null`，并说明 request omits this field；`provider.py:95-102` 对 None 跳过 wire 字段。 | **未映射，属于必要功能缺口**。不能把 `off` 合并为 Default/null。应标 `needs-adaptation` / disabled；仅当模型 capability 明确支持该 ID 且声明 wire 行为、并有验收覆盖后才能计为完整兼容。Off 保持官方灰色（仅装饰）的要求可选，不改变该功能判定。 |
| 键盘、辅助技术 | 源 `lib/client.js:1220-1234,1298-1312` 处理方向键/Home/End，暴露 slider role 与 aria 值；#865 使用原生 `input type="range"`，含 `aria-label`/`aria-valuetext`（`ReasoningEffortSlider.tsx:126-137,181-194`）。 | 交互目的可由原生控件提供；以项目控件验证为准，不移植插件事件代码。 |
| 主题与 reduced motion | 源 `README.md:243-245` 表示 reduced-motion 下放缓而非冻结、配色跟随 DSH 主题；#865 项目 CSS 有浅色主题覆盖（`app.css:7570-7580`），reduced-motion 下关闭 transition/sweep/粒子动画（`:7758-7780`）。 | 主题与无障碍意图已覆盖但动画策略不同；若票面把“放缓”视为必要语义，应保留为 `needs-adaptation`，不能写成行为完全一致。 |

## 最小预检判据

- 固定完整 Git commit `af723caf3387e64ae28aa69c4fd235b1b662e3ae`，并记录 tree `8a732b7c00ba5da3b06e122c243205db4ab190b1` 与上述归档 SHA-256；同时记录 package name/version/license、`dsh.bundle.patch`、`dsh.client` platform/入口与 Node engine。源包没有声明 DSH/Cordis peer 范围，不能虚构精确宿主兼容版本。
- 仅解析静态清单与资源路径/摘要；不 import `main`/`client`、不运行安装脚本、不启动 DSH/Cordis、不访问插件 DOM。缺文件、摘要变化、版本偏移或清单不可解析均不可成为完整兼容候选。
- 每个必需贡献都必须映射到项目已有实现并绑定验收依据。识别成功与启用资格分开；能力不匹配、Off 未映射、必要 reduced-motion 语义差异或证据缺失 → `needs-adaptation` / `unsupported` 且禁用。使用者显式批准新源版本/映射后再预检，不能因下载或解析成功自动启用。

以上结论是调研建议，不修改 #877 票面或产品实现。
