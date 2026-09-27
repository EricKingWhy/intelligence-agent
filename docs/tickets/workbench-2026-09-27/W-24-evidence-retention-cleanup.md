# W-24 · 证据保留、空间显示与显式清理预览
**目标仓库**：intelligence-agent-backend（Artifact / Evidence 查询与删除预览 API）+ intelligence-agent-frontend（占用量与清理预览 UI）；先固定服务端 Contract。

**类型/优先级**：P1 数据生命周期。**依赖**：W-08、W-09。**范围**：既有 Session archive/delete 与 ArtifactStore/Local storage 引用索引、服务端预览/执行接口及审阅 UI；不得让清理动作误删用户工作目录、凭证或未决 Operation 原件。

## 保留契约

首版没有固定天数的自动删除。用户能看到每 Task 的 SessionEvent、Artifact、诊断证据与进度文件占用的近似/精确大小及统计时间。清理前服务端返回将受影响的 `artifact_ref/evidence_id`、哪些验收项会失去可回读原件、是否仍有活动 Task/未决 Ledger/其他 Task 引用；用户必须明确确认。共享或仍被活动/恢复链引用的原件不能清掉，或者必须先生成可恢复快照并证明引用仍可回读。`agent-progress/` 属项目文件，不能由证据清理默认删；工作目录源码绝不自动删。

## 工作指令

优先复用现有 Session archive/delete API 与 ArtifactStore 管理边界；先盘点其现行副作用，避免另建平行删库路径。预览与执行使用相同快照版本/CAS，预览后新增引用导致 409 重算。清理部分失败需记录已删/未删 refs 并让用户重试，UI 不展示“已全部清理”。完成后 W-08 证据投影准确降为“原件已清理/不可复核”，不能保留绿色通过而隐藏原件缺失。

**验收**：两个 Task 共享 ref、Task 仍运行、UNKNOWN Operation、预览与执行间引用变化、磁盘删失败、用户取消、卸载后数据仍在；每例核查不误删 Event/工作目录/凭证。真实 LocalArtifactStore 临时目录操作，必要时 MinIO fake/替代 Provider 测合同；记录前后字节/refs。**不做**：自动 30 天过期、云端协作权限、自动删进度文件。

**成熟参考/复用**：[Pi Session 管理](https://pi.dev/docs/latest/sessions)保存原始历史并由用户控制 Session 删除；[Codex worktree 生命周期](https://learn.chatgpt.com/docs/environments/git-worktrees)在清理前保存可恢复快照。证据引用清理不是这些产品的同一现成功能，属于本项目最小 `BUILD` 生命周期并 `REUSE` 现有 Archive/Artifact 合同。
