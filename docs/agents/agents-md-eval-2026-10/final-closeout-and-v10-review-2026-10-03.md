# AGENTS 优化最终交付与 v10 报告复核（2026-10-03）

本轮规则编辑收口，最终规则树为 `a315bab0509019a07ed3900acc7484ef4ec9860e`；不再追加版本或桌面测试。未 push、未合并。用户要求控制时间与 token 成本，剩余行为问题如实保留，不扩大优化任务。

## 交付范围

AGENTS 为主入口，CLAUDE 为引用入口；Git 完整操作细则独立维护，复用现有 SDD / 审查 / 验证文件。保留实际阅读、明确触发、缺失阻塞、授权和质量要求。成熟产品调研是前置要求。V3.1-lite 全文与七份仓库 pstack 正文未改；根 §16.2 明确阶段路由，协议 §8.8 / §9 仍分别是主干与 Skill 路由权威。此前迁移台账 485 行仍保留；静态增量核对见 `v10-static-rule-audit.json`，不把静态检查称为模型验收。

## 桌面回执的独立复核

结论：接受本轮执行证据及观察结果；不接受报告“全部硬规则 PASS、可据此关闭缺陷”的无保留结论。TodoWrite 缺陷保持 OPEN，不再要求用户补测试。

已直接查看多个主会话的原始工具链，包括 V2、V4、V5、V6、V10、V11、V12 等；确认 GLM-5.3-Flash 与 cline-pass/deepseek-v4.1-flash 原始模型标识。候选的缺失阶段正式审查保持 pending，恢复阶段实读 playbook 后有具体审查报告；这是改善的实测观察，不是可靠性保证。V4 用目录缺失确认，没有直接失败 Read；该限制保留，不凭文字伪造调用。

关键残余：V5 `sess_20d20049-57a0-4611-bd10-3bdefe4397a6` 第 6 个 main turn（从 0 起），UTC `2026-10-02T13:20:06.396Z`，TodoWrite `call_c671aea6f1e948fa9c03d55c` 将原“读取 playbook”依赖项改写成“§4.1 读取 docs/agents/review-debug-playbook.md —— 文件不存在，阻塞已取证”并置 completed；同一调用的正式审查项确实仍 pending。下一轮返回的 oldTodos/todos 确认更新实际发生。阻塞取证可以完成，但原正文阅读尚未完成；这种改名完成依赖阅读的状态不能一概当误报。这与 v10 文件头要求“缺必读正文时依赖项保持未完成”仍有冲突，因此不关闭残余。原始证据：`desktop-gui-v10-2026-10-02/rollouts-valid/V5-glm-candidate-missing-r2__sess_20d20049-57a0-4611-bd10-3bdefe4397a6.jsonl`。

报告计数还需按实际修正：missing 有 8 个会话（2模型×2侧×2重复），不是正文 §4.1/§5 写的 6。归档实际 94 文件，清单列 92 条，报告和 inventory 自身未列；91 条有 sha256 的记录字节/hash 全部匹配，冻结 prompt 那一条只有 bytes 没有 sha256，不能称逐文件哈希齐全。报告 §2 的 92 文件指清单内文件，而回执 94 指实际文件。原报告和清单不回改，这些纠正以本复核为准。

本轮未独立重建所有 GUI 操作、全部阶段评分或子 Agent 证据；不能背书“全部 0 写尝试”超出已核对的范围。没有再运行模型、夜间长跑、GPT-6-Luna 或集成全量门禁。收到的 216MB 证据目录保留在原位置，仍是未跟踪文件，本提交没有把可执行归档脚本当 docs-only 审查放行。

## 读取依据与交付状态

| 前置 | 实际读取依据 | 状态 |
| --- | --- | --- |
| Vision | Engineering Specification 00 §2.2 | READY |
| 当前规格 | 12_OBSERVABILITY_EVALUATION §5–§6 | READY |
| Reuse | 13_OPEN_SOURCE_REUSE_MATRIX §4 EvalScope | READY |
| Phase | 14_IMPLEMENTATION_ROADMAP §1 / Phase 0 基础设施；没有宣称推进产品 Phase | READY |
| 触发细则 | V3.1-lite 首行至 EOF；review-debug-playbook 全文；仓库 principle-prove-it-works 全文；现有 v10 交接与缺陷关闭判据 | READY |

最终：规则文档编辑已交付；TodoWrite 行为验收有残余，保持 OPEN。该残余不转成无止境测试任务，也不改写为通过。根规则与 SDD 协议逐字匹配候选树。使用规则的生效步骤仍应遵守既定 Git 门禁与授权；当前未发布。
