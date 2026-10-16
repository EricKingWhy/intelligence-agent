# #1048 更强文件写隔离后端调研

- 日期：2026-10-14
- 范围：调查 NTFS 硬链接越界的成熟隔离后端；未修改生产实现、Issue 票面或标签。
- GitHub：[#1048](https://github.com/EricKingWhy/intelligence-agent/issues/1048) 仍 OPEN，标签 `bug` / `P2` / `in-progress`。
- 固定点：`origin/main` / `codex/1048-ntfs-hardlinks` = `e36b8d5e43827267938aca796d8814234a1c7117`。

## 结论

NTFS 硬链接是同一个文件对象的多个目录名。凡是把宿主目录中文件对象授权给 sandbox 写入的机制，只要保留了 workspace 内的别名，就不能仅凭“被打开的名字在 workspace 内”保证外部别名不变。微软文档明确说明，任一 hard-link 名称都能打开并修改同一文件。

Codex ACL、DSH restricted-token + DACL、AppContainer 的 DACL 授权和当前 MXC PSEC 路径授权都不是这个对象别名问题的完整解法。Codex 的原子文件替换能保护由该 helper 完成的某些文件写入；它不约束受控 shell 对 workspace 中普通硬链接文件的写入。本地真实 NTFS/PSEC 探针已复现外部内容被改写。

Hypervisor 或容器内核隔离本身也不够：若给 guest 一个可写的宿主目录映射，文件变化仍回写宿主。较强候选需要把工作区放进 guest/容器私有卷，通过受控复制导入和导出，不能把宿主 workspace 作为可写 bind mount。

最省新代码的候选是复用本仓既有 DockerSandbox 的每会话 named volume。它与 SPEC 的“Host Project → explicit import/copy → persistent Docker volume → /workspace”一致。源码显示容器只挂接名为 `agent-harness-<session>` 的 Docker volume，没有绑定宿主 workspace 路径；`copy_in` 以 `tarfile(..., dereference=False)` 传输目录。本机 tar 探针确认：workspace 内 hard-link alias 以 tar hardlink entry 保留，workspace 外的 alias 被归档成独立普通文件。因此导入阶段有望保留内部链接并切断外部对象身份；Docker daemon 未运行，容器端解包、实际链接关系、恢复和导出路径还没有端到端验证。

## 成熟方案比较

| 候选 | 实际机制 | 对 #1048 的结果 | 复用判断 |
|---|---|---|---|
| Codex / DSH ACL、AppContainer、MXC PSEC | 对授权目录/对象或路径实施访问检查；Windows ACL / AppContainer 的授权最终落到文件对象 DACL。 | 不满足硬链接 AC。一个 workspace 名称获得写权后，同对象的外部别名仍能观察到该对象的写入。Codex ACL 的本机 NTFS 探针已直接验证。 | `DEFER` 作为 #1048 修复；保留它们作为路径/权限层机制，不宣称 hard-link isolation。 |
| Windows Sandbox 可写 MappedFolders | VM 内运行，但将宿主目录映射为可写共享目录。微软警告共享目录可能影响宿主，且 Sandbox 关闭后写入仍保留。 | 更强 guest 边界没有取消被映射文件的宿主写语义；文档没有给出“外部硬链接目标不变”保证。不能作为已满足 AC 的候选。 | `DEFER` 可写宿主映射。 |
| Hyper-V isolated Windows Container + 宿主 bind mount | 每个容器有独立 VM/kernel；bind mount 仍把宿主路径共享到容器。微软说明 Hyper-V bind mount 的权限是只读/读写，读写改动落在宿主目录。 | 硬链接对象别名仍跨过共享文件接口。Hypervisor 增强内核边界，但宿主挂载仍是显式可写通道。 | `DEFER` 可写宿主 bind mount。 |
| 本仓 DockerSandbox + named volume | 当前实现为每 session 创建持久 Docker volume，并挂到容器 `/workspace`；容器停止保留 volume。导入使用 tar，不直接 bind mount 用户目录。 | 最有希望的现有实现。外部 host hardlink 不进入 volume 的对象身份；内部 hardlink 的 tar entry 在本机归档探针中保留。容器解包与完整 round-trip 尚未实测。 | `ADAPT` 既有后端 / 优先做真实探针；避免新增 Windows ACL 或新的 sandbox 内核。 |
| Microsoft winapp CLI + Windows Sandbox 私有工作区 | guest 内执行；通过 `push/pull` 传文件，不向 guest 应用暴露任意可写宿主目录。公开文档拒绝 symlink/junction，但没有 hardlink 语义说明；CLI 当前为 Public Preview。 | 设计方向正确，但无法仅凭文档确认内部 hardlink 保留、并发导出安全或通用 shell/持久工作区契约。Windows Sandbox 也要求受支持的 Windows edition，且一台机器同时仅一个实例。 | `PORT DESIGN` 仅作为 Windows guest + mediated transfer 参考；不直接依赖预览 CLI。 |

## 方案依据

| 独立一手来源 | 机制与证据 | 与本仓契合及复用判定 |
|---|---|---|
| Microsoft Learn：[`fsutil hardlink`](https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/fsutil-hardlink)；[Hard Links and Junctions](https://learn.microsoft.com/en-us/windows/win32/fileio/hard-links-and-junctions) | 多个名字引用同一文件；从任一链接名打开都能修改该文件；hard link 只能位于同一卷。 | 解释当前 PSEC/Codex ACL 复现；私有 guest/volume 通过卷边界切断与宿主 NTFS 文件对象的链接。判定：现状 `DEFER`，私有卷路线 `ADAPT`。 |
| Microsoft Learn：[AppContainer isolation](https://learn.microsoft.com/en-us/windows/win32/secauthz/appcontainer-isolation)；[Windows Sandbox configuration](https://learn.microsoft.com/en-us/windows/security/application-security/application-isolation/windows-sandbox/windows-sandbox-configure-using-wsb-file) | AppContainer 管理读写文件访问；Windows Sandbox 的 writable mapped folder 会让更改持久回到宿主，文档直接警告可能影响宿主。 | 更换 token/VM 类型不能替代断开宿主文件对象共享。判定：`DEFER` 作为本票硬链接修复。 |
| Microsoft Learn：[Windows Container isolation modes](https://learn.microsoft.com/en-us/virtualization/windowscontainers/manage-containers/hyperv-container)；[Persistent storage](https://learn.microsoft.com/en-us/virtualization/windowscontainers/manage-containers/persistent-storage) | Hyper-V container 提供独立 VM/kernel；宿主 bind mount 仍是显式 read-write 映射。 | 独立内核不能使可写宿主目录映射变安全。判定：`DEFER` 直接挂载方案。 |
| Docker 官方：[Volumes](https://docs.docker.com/engine/storage/volumes/)；[Storage](https://docs.docker.com/engine/storage) | Docker volume 由 daemon 管理并跨容器生命周期持久；bind mount 则是宿主路径的直接链接，host/container 可同时修改。 | 复用本仓已有 volume 路径，禁止把待保护的用户目录改成 rw bind mount。判定：`ADAPT`。现成路径和代码来自本仓 `e36b8d5e43827267938aca796d8814234a1c7117`：`src/agent_harness/sandbox/docker.py:136-143`、`:870-883`。 |
| Microsoft Learn：[winapp CLI Windows Sandbox execution](https://learn.microsoft.com/en-us/windows/apps/dev-tools/winapp-cli/sandbox-execution)；[CLI overview](https://learn.microsoft.com/en-us/windows/apps/dev-tools/winapp-cli/) | 使用 guest-private 内容与受控 push/pull；不提供宿主目录映射作为工作区；传输限制链接类型；产品明确标注 Public Preview。 | 证明成熟厂商采用 mediated transfer 思路，但不能复用其通用 sandbox 组件或宣称 hardlink AC 已满足。判定：`PORT DESIGN`。 |
| 本仓 SPEC：`goal/Lightweight_Observable_Agent_Harness_Spec/docs/spec/05_SANDBOX_CODING_TOOLS.md §3-4`；Reuse Matrix §1、§4 | 推荐显式导入/复制到 persistent Docker volume；Sandbox Contract 保持薄。 | Docker named volume 复用现有 Sandbox seam，最符合现有冻结方向。公开行为和实际产品语义仍需 real Docker 探针确认。 |

没有复制第三方代码；无代码 License 义务。Windows Sandbox / winapp CLI 仅作架构行为参考，不增加其预览包依赖。

## 本机证据与限制

- #1048 已有的 NTFS + 实际 MXC PSEC 回归：外部 hard-link alias 写入会改变外部 canary；内部-only hard link 写入通过；初始化期间加入外部 link 的保护用例失败。Focused 结果为 **2 failed / 1 passed**，失败是复现目标，不能记为产品修复通过。
- Codex ACL 的隔离探针此前已证实：递归 OI/CI allow ACE 扩展到既有 workspace hard-link 所指的文件对象后，受限子进程可通过 workspace alias 改写外部路径内容。故不直接移植该 ACL 后端解决 #1048。
- 本轮 tar 构造探针在 Windows Python 3.11.15 上生成：外部别名 entry 为普通文件；两个内部别名中后一个为 tar hard-link entry，指向 workspace 内首个成员。它验证的是本仓 `copy_in` 的归档阶段，不等同于 Docker daemon 解包端到端验证。
- 当前 host 报告 Windows Home；Windows Sandbox 命令不可用。Docker CLI 存在，但 `docker info` 无法连接 `dockerDesktopLinuxEngine`，`docker-desktop` WSL distro 为 Stopped。故未运行实际 Docker volume/PSEC 替代探针；不把文档或 tar 阶段证据冒充真实后端验收。

## 兼容代价与裁决前状态（研究首稿）

1. Docker 私有 volume 是 Linux workspace：shell、路径大小写、执行工具和文件系统语义与 Windows PSEC 不同；用户可观察/编辑的是 sandbox workspace API 中的副本，不是原始 NTFS 目录的即时视图。
2. 本仓有 `copy_in`，但当前 Sandbox Contract 未定义对称的安全整树导出/同步语义。若 #1048 要保留对原宿主目录的透明双向同步，必须另行设计、测试 mediated export；直接 rw bind mount 会重新打开硬链接边界。
3. 若必须保留 Windows 原生程序并让用户原始目录即时可写，没有找到已核实的成熟后端能同时保证外链安全与内链兼容。较强设计方向是 guest/private volume + 受控文件导入/导出；其导出、并发和 hard-link group 语义尚未证明，属于架构范围变化。

**裁决前建议**：先对已有 DockerSandbox named-volume 路径跑一次真实 Windows Docker daemon 探针，验证整目录导入的内外 hard-link 关系、容器内写入对宿主 canary 的影响、restart 保留和 fail-closed；同时确认本产品是否接受 Linux workspace 与副本式交互。若通过且可接受，再将 #1048 从“修补 PSEC 对原宿主目录的 hardlink 授权”改为“复用私有 volume workspace 并经 broker 传输”；若不能接受，则必须重新定票为 PSEC 对外链文件拒绝启动，不能称路径 grant 已消除硬链接逃逸。

## 参考实现与材料

- 本仓 #1048 票面和 AC：<https://github.com/EricKingWhy/intelligence-agent/issues/1048>
- Codex/MXC 与本仓 PSEC 复用及边界：`docs/research/2026-10-12-issue-729-psec-reuse-decision.md`
- 当前硬链接回归探针：`tests/sandbox/test_windows_psec_sandbox.py`（只新增测试，无生产修复）

## 用户裁决与后续验收边界（2026-10-14）

用户批准采用本仓已有 Docker named volume 作为私有 workspace，接受 Linux 文件系统语义与副本式导入/导出；导出必须进入全新目标目录，已存在目标拒绝覆盖。旧 Windows PSEC 映射在下次恢复时自动将当前宿主目录导入同会话的私有 volume；只有导入完整成功后才能原子切换映射。Docker 不可用或迁移失败时 fail-closed，不能继续把旧 PSEC 沙箱交还给会话，也不回退到 local。

GitHub #1048 已更新标题和验收，保留 bug/P2/in-progress。TDD 入口已由用户确认：真实 Docker Engine + WorkspaceRegistry.create/get；验证 NTFS 外链和内部 hard-link pair、容器 shell 写入、volume 恢复不重导入、旧映射迁移、失败关闭、fork 和 Sandbox 路径调用。安全导出采用 fresh destination 与拒绝覆盖语义。当前尚未修改生产实现，daemon/volume end-to-end 验证仍待完成。正式 Engineering Specification 保持冻结；架构决策记录在 ADR-0054。
