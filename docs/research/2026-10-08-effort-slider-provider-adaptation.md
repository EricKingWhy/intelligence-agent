# #865 模型推理档位滑杆适配调研

调研日期：2026-10-08。上游 main 的只读远端指针核对到 commit `af723caf3387e64ae28aa69c4fd235b1b662e3ae`。项目代码核对于 worktree HEAD `159bb5276d8510817614023dcd3a9935af8a4c3b`。

## 结论

建议选 `ADAPT`：复用滑杆的交互与视觉思路，档位、顺序和默认值由本项目当前模型目录/能力数据驱动。上游包是 DSH 专用插件，不能直接作为本项目插件安装；它依赖 DSH 的模块加载器、菜单 DOM 和 `modelDirectories` 接口。它的滑杆只把连续拖动吸附到模型公布的离散档位，再调用 DSH 的 `directory.select({ provider, model, reasoningEffort })`，源码没有在此处实现 Claude/OpenAI 等 provider 的参数转换。

Claude Code、Anthropic API、Codex CLI 和 OpenAI API 分别有产品侧控件或请求字段。档位拼写相同不能证明不同厂商/模型有相同语义；官方资料均指出支持档位随模型变化，Claude 还说明档位按模型校准。因此 UI 应呈现当前模型真实可用的档位，不应把固定的通用数值刻度说成所有模型都能执行。

## 成熟产品的控件与请求机制

- **Claude Code（用户侧）**：`/effort` 无参数时打开交互滑块；`/model` 也能在支持的模型上调档。`Enter` 保存为默认，`s` 只用于当前会话；设置按模型保存。模型不支持所选档位时，Claude Code 文档描述的是回退到该模型不高于所选档位的最高支持档。`max` 默认仅限当前会话。[Claude Code 命令参考](https://code.claude.com/docs/en/commands) · [模型与 effort 选择/回退规则](https://code.claude.com/docs/en/model-config)

- **Anthropic API（请求侧）**：在支持 adaptive thinking 的模型上，思考模式用 `thinking: {"type":"adaptive"}`，深度通过 `output_config.effort` 设置；effort 是软性引导，不是固定 token 预算。模型支持范围和默认值有差异，不兼容的参数组合可能返回 400。[Adaptive thinking](https://platform.claude.com/docs/en/build-with-claude/adaptive-thinking) · [Effort 参数及模型行为](https://platform.claude.com/docs/en/build-with-claude/effort) · [不支持组合的错误说明](https://platform.claude.com/docs/en/build-with-claude/thinking-troubleshooting)

- **Codex CLI（用户侧）**：官方 CLI 文档把 `/model` 描述为选择模型和 reasoning effort；`~/.codex/config.toml` 可用 `model_reasoning_effort` 设置默认值，单次运行也能通过 CLI 配置覆盖。配置文档明确写明档位取决于所选模型/客户端。[Codex CLI](https://learn.chatgpt.com/docs/codex/cli) · [配置基础与 `model_reasoning_effort`](https://learn.chatgpt.com/docs/config-file/config-basic) · [配置参考](https://learn.chatgpt.com/docs/config-file/config-reference)

- **OpenAI API（请求侧）**：Responses API 使用 `reasoning: { effort }`；Chat Completions 使用 `reasoning_effort`。可选值及默认值按模型变化，并非每个 reasoning model 都支持全部档位。[Reasoning 指南](https://developers.openai.com/api/docs/guides/reasoning) · [Chat Completions 参数参考](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)

## 上游实现与许可证（固定 commit）

- `lib/client.js:183-225`：从当前 provider/model 的目录元数据读取 `reasoning.efforts`，用 `defaultEffort` 表示模型默认；滑块位置按档位数组顺序吸附。[源码](https://github.com/Microqian2th/dsh-codex-effort-slider/blob/af723caf3387e64ae28aa69c4fd235b1b662e3ae/lib/client.js#L168-L225)
- `lib/client.js:1011-1116`：拖动过程对档位写回降频/合并，松手立即提交；目标档位通过 DSH `directory.select` 更新，并处理失败回滚。[源码](https://github.com/Microqian2th/dsh-codex-effort-slider/blob/af723caf3387e64ae28aa69c4fd235b1b662e3ae/lib/client.js#L1011-L1116)
- `lib/client.js:18-25` 与 `643-663`：DOM 桥识别 DSH 菜单语义锚点；识别失败时不注入，保持宿主菜单可用。此桥接与 DSH 页面结构绑定，不能原样移植。[fail-open 源码](https://github.com/Microqian2th/dsh-codex-effort-slider/blob/af723caf3387e64ae28aa69c4fd235b1b662e3ae/lib/client.js#L18-L25) · [DOM 桥源码](https://github.com/Microqian2th/dsh-codex-effort-slider/blob/af723caf3387e64ae28aa69c4fd235b1b662e3ae/lib/client.js#L643-L663)
- `LICENSE` 是 MIT，版权行是 `Copyright (c) 2026 dsh-codex-effort-slider contributors`；复制实质代码时须保留版权与许可声明。[许可证全文](https://github.com/Microqian2th/dsh-codex-effort-slider/blob/af723caf3387e64ae28aa69c4fd235b1b662e3ae/LICENSE#L1-L21)

## 与本项目的对接边界

当前 `src/agent_harness/model/provider.py` 的工厂返回 `ReasoningChatOpenAI`，将产品档位 `minimal / standard / deep` 分别映射为 OpenAI-compatible `minimal / medium / high`，并把映射后的值作为 `reasoning_effort` 传入模型构造器；该文件证明的是这条 OpenAI-compatible 请求路径，不证明项目已经有原生 Anthropic API provider。（`provider.py:16-43,92-110,125-145 @ 159bb5276d8510817614023dcd3a9935af8a4c3b`）

适配结论：滑杆是离散档位选择器，轨道动画可以连续变化；真正提交的只能是当前模型目录公布的选项。默认值、档位含义和 wire 参数属于不同层，不能仅凭共用 `low / medium / high` 名称就宣称 Claude 与 OpenAI 模型兼容。

**需用户决定的范围**：若 #865 还要加入原生 Anthropic API provider 支持，需要把它作为 provider 能力/参数适配范围明确纳入；现有代码证据仅覆盖 OpenAI-compatible 工厂。
