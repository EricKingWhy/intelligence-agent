# ADR-0058 — 持久审批规则迁出工作区：控制目录 + 逐引擎可写性矩阵

**Status**: Proposed（2026-10-26 随 #1487 施工；PR 见 Tracker #1487 段）
**Date**: 2026-10-26
**Related**: GitHub #1487（本票）；#684（approve_policy v1，项目根存储形态的来源）；
`docs/agents/684-design-proposal.md` §8（F21 显式授权 / F22 无模糊匹配）；
ADR-0041 D6（三概念分离）；ADR-0051（默认权限矩阵与审批层诚实边界）；
#1510（Sandbox symlink fence，围栏语义先例）

---

## 1. Context

#684 v1 把持久审批规则（「以后都允许」）存 `<project_root>/.agent-harness/approve-policy.json`。
该位置在 **agent 执行进程的领地内**：

- `write` / `edit` 工具直接可达（就在 workspace 里）；
- `bash`（local 引擎）与 harness 同进程同权；
- 每次工具调用前 `find_match` 现读文件（`approve_policy.py` `load()`），**改写即生效**。

审批规则 = 授权凭据。被授权者能改写「授权自己被放行的条件」= 自授权漏洞：
agent（或被诱导的命令）向该文件追加一条 `command` 档规则，下一次同命令直接
跳过人工审批。票面裁决 24-A：迁出工作区，且验收口径 = **执行进程确实写不到**，
不是「文件搬了家」。

## 2. Decision

**D1 — 控制目录 + 项目哈希文件名。** 控制文件 =
`default_control_root() / project_key(project_root) + ".json"`，其中
`default_control_root()` = env `AGENT_HARNESS_APPROVE_POLICY_ROOT` 覆盖，默认
`<home>/.agent-harness/approve-policy/`；`project_key` = resolved 项目根的
sha256 hex。形态对标 Claude Code `~/.claude/projects`（per-project 用户级存储，
不进项目树）；与仓内先例 `Settings.provider_store_path`（`<home>/.agent-harness/`）
同根。旧位路径保留为 `legacy_path`（只读探测 + 迁移源）。

**D2 — env 是唯一注入口。** 三处生产构造点（`ToolExecutor.__init__` 回落、
CLI `_approve_policy_store`、Web `approve_policy_store()`）都在进程内构造
store、没有可逐点传参的缝；`control_root` 构造参数只服务测试内联注入，部署
钉根与测试隔离统一走 env。三站点读写同一控制文件由测试钉死（不变量 #22
「不维护第二套真相」在本域的落点）。

**D3 — 迁移显式、严格、幂等。** `load()` 永不读旧位（自动信任 agent 可达
空间里的文件 = 信任 agent 自建文件）。唯一入口 CLI `approvals policy
import-legacy`（用户手势，F21 在迁移面的落点）；`import_legacy()` 逐条过
`_parse_legacy_rule`：`from_dict` 全字段 + `created_at` 非空且可按 ISO 8601
解析 + command 档 key 无换行（与 `add_rule` 同红线）。不合规跳过计数不中断；
按 id 去重且**控制侧已有同 id 时控制侧胜**；重复执行零导入。`list` 命令在
`legacy_present` 时附提示行（只探测不读取）。控制文件在场后旧位内容零效力。
旧文件**在场但整体读不动 / 坏 JSON / 形态不对** ⇒ `LegacyImportError`（CLI
如实报错 + exit 2 + 不给删除建议）——「读不动」与「没有规则」必须区分，
否则用户会按「迁移完成可删除」的提示删掉可能含可恢复规则的坏文件。
迁移默认**二次确认**（对齐 `remove`）：无 `--yes` 只打印将导入清单
（`preview_legacy`，零改动）并 exit 2；`--yes` 才写入——旧位格式公开，
agent 可预埋全字段合规的规则，逐条核对清单把 F21 手势从文件粒度下沉到
规则粒度。有跳过条目时收回「可手动删除」建议（未导入内容可能仍可恢复）。

**D4 — 逐引擎可写性矩阵（验收口径）。**

| 引擎 | 控制文件可达性 | 证据 |
| --- | --- | --- |
| write / edit（全引擎） | **不可达**：Sandbox 路径围栏 resolve 后必须在 workspace 内；workspace 内 symlink 指向控制文件同样被拒（#1510 fence 语义） | `test_write_tool_cannot_reach_control_file` / `test_edit_tool_cannot_reach_control_file` / `..._via_workspace_symlink` |
| bash @ docker | **不可达**：容器只挂 workspace 具名卷（`docker.py` volumes 构造），host home 不进容器 | `test_docker_agent_cannot_reach_control_dir`（canary 全盘 grep 为空；无 daemon 主机 skip） |
| bash @ local | **已知残余**：与 harness 同进程同权，无 OS 隔离层的引擎无法靠目录位置防同权写。如实登记，不声称 | ADR 本行；无测试（不可测——它是定义上的同权） |

**D5 — 同族边界（登记不声称）**：workspace 恰为 home（或控制根的祖先目录）
时，文件工具围栏对控制文件失效——位置防不了「领地包含控制根」的配置。与
local 同权残余同族，属部署面约束（别把整个 home 当 workspace）。

## 3. 未采纳方案（逐条留痕）

| 方案 | 为什么没做 |
| --- | --- |
| 保留工作区存储 + 文件签名/校验和 | 裁决 24-A 明确不叠加签名；签名密钥仍要放在执行进程够不到的地方，等于把本 ADR 问题重述一遍 |
| Runtime 启动时自动迁移旧位 | 静默信任 agent 可达空间内容；自动写用户级目录也是未获手势的副作用 |
| 迁移后删除旧位文件 | 删除是不可逆动作，留给用户在命令输出明示后手动执行（命令输出含此提示） |
| 控制目录放 `XDG_CONFIG_HOME` 形态 | 仓内先例（`provider_store_path`）用 `<home>/.agent-harness/`；不为一票引入第二套 home 布局 |
| env 之外再加 per-request 注入缝 | 三构造点都在进程内、无请求面可挂；多缝只增加「构造出口径不一致」的表面积（正是本票要消灭的两套真相风险） |

## 4. Consequences

**正向**：授权凭据离开被授权者的写可达域（write/edit/docker 全闭）；per-project
隔离让多项目规则互不串扰（旧位靠目录天然隔离，迁出后靠哈希文件名保持）；
hermeticity 反而变好（旧测试全靠 tmp 项目根隔离，新测试 env 钉根即可）。

**代价 / 风险**：

- 规则文件不再随项目提交 / diff——损失「项目共享级规则」能力。这是裁决
  24-A 接受的取舍：可提交的授权文件 = 仓库里躺着可被 PR 改动的授权面，
  与迁出动机直接冲突。团队共享规则若将来有需求，应走带确认的显式分发，
  而不是回到工作区存储。
- `project_key` 按 resolved 路径哈希：项目移动 / 重命名后控制文件失联
  （规则「丢失」回到逐调用审批，fail-closed 方向，无安全风险；用户可经
  旧位文件手动迁移——把旧文件拷到新项目根再跑 import-legacy 即可）。
- local bash 同权残余（D4 第三行）：消除它需要进程级隔离（独立 UID /
  job object），属新工程票范围，不在本票。
- `ToolExecutor` 构造期经 `default_control_root()` 间接依赖 `Path.home()`
  可解析（env 未设时）：home 不可解析的进程会在构造期 fail-fast。与仓内先例
  `config.py`（模块导入期即调 `Path.home()`）同类的进程级要求，非新失败类，
  登记不修（A 轴审查 P4-3）。

**未闭合（解除条件）**：

- **迁移甄别不了「格式合规的伪造旧位文件」**（B 轴审查 P3-2）：无签名
  （裁决 24-A 不做）就不可能自动证明旧位内容来自 Runtime；缓解 = 默认二次
  确认（逐条清单可见）+ 命令输出的来源警示行。票面 AC#2「agent 自建文件
  不被导入为授权」的准确口径 = **不被静默导入**（必须经用户逐条核对后的
  显式手势）。**解除条件** = 出现规则签名 / Runtime 产物登记链的独立需求。
- docker 引擎的可写性证据在有 daemon 环境补跑（本机无 daemon，测试 skip
  且挂载面构造已核）：**解除条件** = CI 或开发者主机提供 daemon 后该用例
  实跑一次并在 Tracker 回填读数。
- symlink 围栏用例在无建链特权主机（Windows 非 Developer Mode）skip：
  **解除条件** = 有 POSIX / 开发者模式环境实跑（#1510 的 fence 测试族同款
  环境依赖）。
