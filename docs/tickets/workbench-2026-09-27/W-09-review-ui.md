# W-09 · 桌面/Web 任务审阅页
**目标仓库**：intelligence-agent-frontend（现有 React Web）。

**类型/优先级**：P1 前端。**依赖**：W-08 服务端契约。**范围**：现有 `web/src/App.tsx`、`web/src/lib/api.ts`、projection/reconnect 组件和测试；只在现有 React 页面增量接入。

## 界面必须回答的六件事

1. 原目标、当前约束/授权与验收项是什么（点开可见原事件来源）；2. Agent 现在执行、暂停、待 reconcile 还是结束；3. 哪些项通过、失败、缺证据或已过期；4. 修改了哪些文件、当前 diff 与测试时快照是否一致；5. 有哪些失败尝试/UNKNOWN Tool/下一步；6. 用户接受、带原因接受、释放目录三种操作分别会发生什么。

## 工作指令

以服务端 Task/Evidence 投影驱动页面；状态文案固定使用“待验证/可交付/已接受”，验证和接受各有标签，不能同绿勾混淆。工具卡链接到现有 Artifact/Operation/SessionEvent；diff 显示 `agent-progress/`，但“暂存/提交”只能由用户显式调用原有能力，首版可仅外部打开。刷新、窗口隐藏再显示、断线重连不丢当前 Task，也不以本地缓存推断通过。审批控件复用原有 ApprovalCard，不建另一套权限组件。

**验收**：Playwright 使用真实 FastAPI + 固定事件夹具逐一截图/断言六状态；手动做一次真实模型运行，点开失败测试、真实 UI 证据、diff、来源 seq，再带原因接受未完成验证；确认刷新后状态同源。`tsc`、vitest、oxlint、Playwright、build 按 V3.1-lite 跑。**不做**：IDE 编辑器、多用户协作、前端私存业务事实。

**成熟参考/复用**：[Codex app](https://openai.com/index/introducing-the-codex-app/)把线程、变更和审阅集中显示；[DeepSeek Desktop](https://github.com/deepseek-ai/deepseek-harness/blob/master/apps/desktop/README.md)复用 Web UI。对本仓是 `PORT DESIGN` 交互、`REUSE` 现有 React/API/Approval 组件。
