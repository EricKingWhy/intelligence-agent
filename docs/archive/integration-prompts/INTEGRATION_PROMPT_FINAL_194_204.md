# 集成提示词：#194–#204 六个批次全部完成 + 最终全分支 review 修复（feat/backend / feat/frontend）

> 交付对象：Git Integrator（在 `D:\intelligence-agent` 的 `main` 上执行合并）。
> 两端工作已完成、门禁全绿、**均未 push、未创建 PR**——按 AGENTS.md §13/§14 的集成纪律，
> 合并与 push 由 Integrator 决定并执行。

## 1. 两端分支状态（本次交付）

| worktree | branch | 本次新增 commit |
| --- | --- | --- |
| `D:\intelligence-agent-backend` | `feat/backend` | `daf6f4a` → `8d534c7`（#204 + 终审修复 + 进度记录） |
| `D:\intelligence-agent-frontend` | `feat/frontend` | `5e0a396` → `47ea3e4`（#204 + 终审修复） |

完整批次清单（每批都已关单，证据在 PHASE_STATUS.md / SDD_TICKET_TRACKER.md）：

1. #194/#197/#199/#201（前端 UI 批）
2. #196+#195（多轮投递通道，跨端）
3. #200（上下文容量看板，跨端）
4. #202+#198（记忆工具入口 + 档位可观测，后端为主）
5. #203（自定义模型供应商，跨端）
6. #204（项目弹窗收窄 + launch=false，跨端）
7. 最终全分支 review（fixed point = origin/main）+ P1 修复

## 2. 建议的集成顺序（§14.9：一次一个分支）

```bash
cd D:\intelligence-agent
git fetch origin
# ① 先合 backend
git -C D:\intelligence-agent-backend diff origin/main...feat/backend   # 检查
git merge feat/backend          # 在 main 上
# 跑后端完整回归（本机代理失效时 uv 前缀见 §4）
cd D:\intelligence-agent-backend && .venv/Scripts/python.exe -m pytest tests/ -q
# ② backend 稳定后**重新 fetch / diff**（§14.9：之前的冲突判断全部过期），再合 frontend
git merge feat/frontend
cd D:\intelligence-agent-frontend\web && npx tsc -b && npx vitest run && npx playwright test --workers=2 && npx vite build
# ③ 全部通过后才 push
git push origin main
```

## 3. 本次批次的核心契约（冲突仲裁锚点）

### #204（本批主线）
- 后端：`POST /api/sessions` 新增 `launch=false`（默认 true ⇒ 既有 SSE 契约不变）；
  返回 `{session_id, permission_mode}` JSON；`task` 可选（launch=true 仍 422）；
  `X-Permission-Mode` 响应头恒在。裁定冻结在 `docs/design/WEB_UI_BATCH_REDESIGN.md` §5。
- 前端：弹窗职责 = 选目录 + 设默认权限 + 创建空会话（无任务输入框）；成功后
  `selectSession(created.sessionId)` + 焦点落 `#composer-input`；pill 用**响应回传的**
  permission_mode 初始化。
- **冲突预期**：`CreateSessionRequest.task` 从必填变可选（`web/app.py`），
  `tests/test_web_api.py` 空 task 断言已同步更新并写明理由——若与 main 上的改动冲突，
  以"放宽 + 理由写明"一侧为准。

### 最终 review 的后端契约变化
- `ModelConfig.resolve_selection(settings, name, store)` 是**新的统一解析点**：
  catalog 名优先，`<provider>:<model_id>` 走 `from_custom_provider`。三道校验闸
  （create/resume/messages 的 amend model、POST /model 的 assert_model_resolvable）
  与 `assembly.build_runtime` 全部引用它。若 main 上改过 model 解析，以本分支为准。
- `context_usage.skills_provider_tokens(builder)` 是公开共享函数（原 app.py 私有副本
  已删）；RunManager 收口快照现在也带 skills 桶。

## 4. 已知环境注意点

- `pyproject.toml` 新增依赖 `keyring>=24`（已装入 backend venv；main 合并后如缺依赖：
  `env -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy -u ALL_PROXY all_proxy= uv pip install --python .venv/Scripts/python.exe "keyring>=24"`）。
- **测试禁忌**：任何 provider 测试必须注入 `MemoryCredentialStore`（fixture 注入），
  否则假 key 会写进真实 Windows 凭据管理器（#203 实测泄漏过，已清理）。
- 已知 flake（单跑恒绿，全量可 deselect）：`tests/web/test_multiturn_queue_http.py` 的
  `test_get_queue_and_flush_roundtrip` / `test_edit_queued_item_cancel_old_then_queue_new`；
  e2e `r-project-groups.spec.ts:139`（资源竞争型抖动）。
- e2e 必须 `--workers=2`（4 worker 全量并行有竞争抖动）。

## 5. 遗留债务（终审明确记录、未修，不阻塞集成）

- 后端：`SystemCredentialStore.available()` 的 backend 类名字符串匹配（keyring 版本
  变更敏感）；`_classify_failure` 的状态码子串匹配可被 URL/模型名误命中；
  `X-Permission-Mode` 头只在 create SSE，`/messages`/`/queue/flush` 的 SSE 没有
  （pill 契约只锚创建响应，语义成立但不对称）。
- 前端：`getContextProviders`（#201 后无调用方，刻意保留）；队列项「编辑」的乐观
  prefill 不与 cancel 失败对账。
- 以上已记 PHASE_STATUS.md，属后续票。
