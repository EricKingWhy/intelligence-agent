# Issue #871：项目级 Skill 生命周期调研与复用判定

日期：2026-10-09。范围：本地目录、project scope、Skill 包管理；不含 Git、global、市场或脚本自动执行。

## 成熟产品与标准

- Agent Skills 规范要求 Skill 是含根级 `SKILL.md` 的目录；`scripts/`、`references/`、`assets/` 等资源按目录随包保存，相对引用以 Skill 根为基准，正文可渐进加载。该规范定义包格式，不定义安装生命周期。来源：[Agent Skills specification](https://agentskills.io/specification)。判定：REUSE 格式与目录，不另造归档格式。
- Claude Code 插件管理把磁盘安装、持久配置选择和当前会话加载状态分开；CLI 可显示待重启状态，提供显式 enable/disable/uninstall。来源：[Plugin overview](https://code.claude.com/docs/en/plugins)、[Install and manage plugins](https://code.claude.com/docs/en/plugins/install)。判定：PORT DESIGN 状态分层与用户路径；不引入 Claude 的 marketplace、hooks 或插件宿主。
- DeepSeek Harness（DSH）插件管理器有安装快照/失败恢复、受管包删除和 `applied` / `restart-required` / `failed` 结果。核验代码：本地浅克隆 `D:\reference\deepseek-harness`，HEAD `5badb15009ae1756c3afe0ae0cef1faafc290ccc`；`packages/boot/plugin-manager/src/index.ts:473-579`（install 与恢复）、`:617-645`（remove）、`:795-818`（状态结果）；`packages/boot/plugin-manager/src/patch.ts:14-42`（原子配置修改）。仓库许可证为 MIT。判定：PORT DESIGN staging/rollback 与状态结果；不直接移植 TypeScript/Cordis 代码。

## 本仓复用阶梯

- REUSE：`src/agent_harness/skills/inspection.py` 的只读包校验、资源清点、边界检查和冲突判定；安装前后均可复用，不执行包脚本。
- REUSE/ADAPT：`src/agent_harness/skills/discovery.py` 的一层目录扫描与 `SkillCatalogEntry.load_body()` 惰性加载；`src/agent_harness/capability/wiring.py` 已把 project Skill 目录接入 Capability。
- REUSE：标准库 `shutil`、`hashlib`、`json`、`tempfile` 与 `os.replace`；原子文件替换模式可对照 `src/agent_harness/tooling/approve_policy.py:238-283`。不新增依赖。
- ADAPT：用小型 Python manifest 保存本地来源、内容摘要、project scope、启用选择和预检摘要；实际运行态从现有受鉴权 host service 的 `/api/skills` 查询，不从保存选择推测。
- BUILD（最小范围）：本仓 CLI 的 install/enable/disable/remove/list 生命周期；DSH 的包管理源代码与本项目语言、API 和运行时不同，不可直接复用。没有复制第三方代码，因此无需在本改动中搬入其许可证文件。

## 票面与规格的待裁决冲突

父票 #868 的测试决策要求 Skill 样例覆盖 scripts/references/assets 并验证 load_skill；本地 #871 的 AC 同样要求相对资源可用。与此同时，T1 `inspect_skill_package()` 对每个非目录 `scripts/` 条目生成 `script-runtime / manual_review` 要求（`src/agent_harness/skills/inspection.py:153-160`、约 `:205-234`），因此此类包状态为 `needs-adaptation`。已批准的 PRD #868 与 spec 08 §6.3 要求未满足的必要兼容项不得启用；把 `needs-adaptation` 包启用会越过该闸门。

裁决（用户 2026-10-09）：遵循已批准兼容闸门；needs-adaptation 包可导入保存但保持禁用，只有 complete 包可启用。用户授权相应调整 #871 验收样例；GitHub issue body 已更新，分别验证完整包的 load_skill 闭环和含脚本包的字节保留/启用拒绝/无副作用。

## 运行时状态查询的凭据边界

`plugins list` 在服务可查询时复用现有 `/api/skills` catalog，并按实际 source 路径判断包是否被当前 Runtime 发现；这不代表 Skill 正文已经通过 `load_skill` 注入 Context。CLI 先核验 loopback、health 与协议版本，再从 `HostTokenStore` 读取签名 token，生成随机 nonce 和 Unix 时间戳。请求不发送 bearer token，而是使用 HMAC-SHA256 对固定域分隔消息签名，字段绑定 `GET /api/skills`、nonce、token 中的 service UUID 与时间戳。主机验证 token 签名、scope、挑战签名和 30 秒新鲜窗口；nonce 一次性消费并在 30 秒后过期，缓存最多保存 4096 个未过期 nonce，满载时拒绝新挑战。响应证明绑定请求挑战、HTTP 状态码和原始 body 的 SHA-256；客户端在解析 JSON 前用 `hmac.compare_digest` 验证。读取使用固定 loopback 路由、受限响应体和绝对截止时间，避免慢滴流无限延长查询。

机制复用 RFC 2104 的 HMAC 与 Python 标准库建议的 `compare_digest`：[RFC 2104](https://www.rfc-editor.org/info/rfc2104/)；[Python `hmac` 文档](https://docs.python.org/3.13/library/hmac.html)。判定：ADAPT 既有 `HostTokenStore`、loopback opener、端点与 health/protocol 检查；REUSE 标准库 HMAC，不新增依赖。该挑战扩展只用于读取 Runtime catalog，不更改既有服务附着 bearer 流程。
