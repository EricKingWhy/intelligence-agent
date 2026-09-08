# Backend B2-MixIn Handoff — context_providers handler 层 422 校验

> **给 Git Integrator 的集成提示词。** 本文档汇总 Ticket B2-MixIn（handler 层 422
> 补强）的实现成果，供集成入 `main` 时审查与验证。

---

## 完成了什么

按集成 AI 批准的混合方案，保留 main 上实现 A（ADR-0020b）的结构不动，只从实现 B
（ADR-0021）借鉴 **handler 层 422 校验**这一个核心优势。

### 为什么需要这一层

实现 A 的 fail-open 把**用户输入校验**和 **capability 故障降级**混为一谈了：
- 不变量 #21（optional capability 故障不拖垮 core）适用于装配失败（配置缺失时
  跳过 provider），这是对的。
- 但用户提交 `context_providers: ["nonexistent"]` 不是 capability 故障——是用户
  输入错误，应该得到明确的 422 反馈（与 `model` 字段 `from_catalog` 422 同模式）。

fail-open 让用户以为选成功了但实际被静默跳过，调试困难。handler 层 422 给出诚实
反馈：未知 id + 当前可用清单。

### 改动（最小 surgical）

只改 `src/agent_harness/web/app.py` 的 `create_session` handler，在 `get_wiring()`
之后、`build_runtime` 之前加一层集合校验：

```python
if req.context_providers is not None:
    wired_names = {
        name for p in wiring.context_providers
        if isinstance(name := getattr(p, "name", None), str) and name
    }
    unknown = [pid for pid in req.context_providers if pid not in wired_names]
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=(
                f"context_providers contains unknown ids {unknown}; "
                f"available: {sorted(wired_names)}"
            ),
        )
```

### 双层语义不矛盾

- **handler 层 422**：主校验——用户输入错误时给出明确反馈。
- **assembly 层 `_select_context_providers` 的 fail-open**：防御性兜底——万一 wiring
  在 422 之后装配降级了（config 变化），assembly 层不应该崩溃。两层互补。

### 没碰什么（Scope Lock §8）

- ❌ `capability/wiring.py` —— 不加 `context_provider_entries` dict
- ❌ `memory/context_provider.py` / `skills/context_provider.py` —— 类属性 `name` 已到位
- ❌ `assembly.py` 的 `_select_context_providers` —— fail-open 语义作为兜底保留
- ❌ Pydantic `_validate_context_providers` —— 继续做形状校验

## 改了哪些文件

```
src/agent_harness/web/app.py                      # create_session handler 加 422 校验（+21 行）
tests/web/test_context_providers_endpoint.py      # 5 条新测试（+63 行）
```

分支：`feat/runtime-context-providers-422`，起点 `origin/main`（`080e1f1`）。
Worktree：`D:\intelligence-agent-runtime`。
Commit：`c3b92ae`。

## 测试结果

```
web + assembly 相关：73 passed（含 5 条新增）
ruff check src/agent_harness/web/app.py tests/web/test_context_providers_endpoint.py：All checks passed
全量 pytest：55 failed / 1185 passed / 32 skipped
```

**关于 55 failed**：全部是预存环境问题，与本次改动无关：
- ~50 个：`Path.read_text(newline=...)` —— Python 3.11 不支持该 kwarg
  （ticket 基线注明「58 个 Path.read_text newline 失败是 Python 3.11 预存问题」）
- ~3 个：`ModuleNotFoundError: No module named 'langfuse'` —— 该 worktree
  未装 langfuse（optional dependency）
- 0 个与 `context_providers` / `web/app.py` / `create_session` 相关

新增 5 条测试覆盖验收标准：

- [x] 未知 id → 422 + detail 含 available 清单
- [x] 合法 id（已装配）→ 不 422
- [x] 空列表 → 不 422（显式选零）
- [x] None（默认）→ 不 422（用默认全量）
- [x] bare 配置传 `["memory"]` → 422（memory 没装配）

## 是否建议合并

✅ **建议合并入 main**——这是最小改动（21 行生产代码 + 63 行测试），不碰实现 A 的
任何结构，只补强用户输入校验。集成风险极低：

- 无 schema 变更（`context_providers: list[str] | None` 契约不变）
- 无 wiring / assembly / provider 类改动
- 与实现 A 的所有现有测试兼容（10 条原有测试全通过）

## 与实现 B（feat/backend a731563）的关系

实现 B（ADR-0021）和我刚提交的 B2-MixIn 解决的是同一个问题，但本 ticket 已明确
采用**混合方案**：保留实现 A 结构 + 借鉴 422。因此：

- **本分支（`feat/runtime-context-providers-422`）是最终采纳的方案**。
- `feat/backend` 的 `a731563`（含 ADR-0021 + ContextProviderEntry + 整套平行
  实现）**已被本混合方案取代**，不应集成入 main。建议 Git Integrator 在处理
  `feat/backend` 时将 `a731563` 的 context_providers 部分视为 superseded
  （其他 commit 如 T5 #135 仍有效）。

## 还剩什么

无后端剩余工作。B2-MixIn 是 B2 冲突的最终解决方案。

下一个依赖 ticket：**F3（前端）**——context_providers 多选控件。需要从新 main
（本分支集成后）开 `feat/frontend-context-providers`。
