[claude-code:unrecognized_model] {"model":"dolphin/deepseek-ai/DeepSeek-V4.1-Flash","query_source":"sdk"}
Both axes returned and I've cross-verified their load-bearing claims myself. Consolidated report below.
> 来源：`~/workspace/system/dispatch/890-review2.log` 的**逐字副本**（仓外文件，落仓只为让台账行的
> 更正指针从仓库内可达——第三轮复审 P4-3）。除本段说明外，正文未作任何编辑。
> 落仓日期 2026-10-09，落仓提交见 `docs/review_ledger.d/` 对应的更正行。
>
> ⚠ 本文件首行 `[claude-code:unrecognized_model] {"model":"dolphin/…",…}` 是派工 harness 打给
> CLI 的模型名 warning（该 warning 本身无害，请求照常成功；同族现象见 `AGENTS.md` 2026-10-09 条），
> 是日志原文的一部分，**不是**本报告的正文——读报告从下一行起。

---

# #890 独立复审报告（两轴并列汇总）

审查对象：`53585080..dd1f5d70`（diff 1456 行）。两轴各自 fresh sub-agent 独立作业、上下文互不污染；我另做了一轮独立事实核验（只读仓库，最终树 worktree `~/workspace/intelligence-agent-wt/890-ws-auth`，HEAD 确为 `dd1f5d70`）。凡我未能亲手验证的点均明确标注。

## 一、上一轮问题逐条复核

| 上轮 | 判定 | 依据（最终树行号） |
|---|---|---|
| **P1-1** 浏览器无凭据通道 | **已修复** | `app.py:1717` `_bearer_from_subprotocols`（解码 `:1735`）→ 复用 `_resolve_identity`（`:1897`）；`:1741` `_negotiate_subprotocol` 只回显业务子协议，token 子协议永不回显；`wsStream.ts:97` `wsSubprotocols()` 发 `[业务, 前缀+b64url(token)]`。编解码互逆含非 ASCII（`TextEncoder`→utf-8 decode） |
| **P2-1** AC3「行为不变」不实 | **已修复** | 「零影响」措辞已删，三处如实披露行为变更：`research…md:62-65`、测试文件头、PR 草稿。我 grep 确认旧「零影响」句已不在（现存的都是「**不是**零影响」的否定式） |
| **P3-1** `_IdentityRejected.status` 死字段 | **已修复** | `app.py:1758` `__init__` 只设 `self.detail`；全树无 `rejected.status`；HTTP 调用处写死 `status_code=401`，docstring 与代码相符 |
| **P3-2** 测试头「判别力说明」例数/机制不符 | **基本修复（残留见新 P4）** | 已拆两条接缝（真实 uvicorn/httpx2 = 2 例；starlette TestClient = 31 例），例数 33 经我手工点数吻合（20 个 `test_` 函数 + 5 处 parametrize 展开 13）；残留一处过度收窄（下 P4） |
| **P3-3** 材料数字不自洽（22/23） | **已修复** | 台账行统一为「23 例（终态读数）」并注明 22 为该笔当时数、修回批加到 33 |
| **P3-4** Origin 策略两份 + 文案漂移 | **部分修复** | 判据确实共用（`projects.py:64` `origin_is_local` / `:96` `check_trusted_origin`，两端都调）；但**文案没共用**——WS 丢弃返回值、另写一条硬编码字面量（见新 P3-A） |
| **P3-5** 畸形 `Origin` → 500 | **已修复** | `projects.py:67-70` `try/except ValueError: return False`；并有 `test_unconfigured_ignores_malformed_origin_instead_of_500`。判据方向不变（仍 fail-closed） |
| **P4-1** `_headers()` 死代码 | **已修复** | 最终树零命中 `_headers` |
| **P4-2** `websocket.py` docstring 过度承诺 | **已修复** | 已改写为「调用方已判完」，并显式写明「**不是**本函数从不接受未认证连接」 |
| **P4-3** 「四种 401 形状（含非 Bearer）」错标 | **已修复** | 该用例 docstring 现为 5 项且指「非 Bearer 另见 `test_refuses_bearer_that_is_not_bearer_scheme`」 |
| **P4-4** uvicorn 起停样板不复用 / 命名漂移 | **已修复（可接受）** | `_shutdown` / `_recv_until` / `_OneTurnModel` 已复用；`_start_server` 仍本地复刻，但签名确不同且 docstring 自陈理由 |
| **P4-5** Origin 原样入日志 + 中文 close reason | **已修复** | `app.py:1872` `(origin or "")[:120]`；`:1706`/`:1877` `WS_ORIGIN_DENIED_REASON` 固定短 ASCII |
| **P4-6** gate 读数落盘污染 | **部分修复（残余见新 P3-B）** | 中间读数的清理做了，但**最终树仍无覆盖交付 tip 的读数** |

## 二、新发现问题

### P2-A｜`_negotiate_subprotocol` 回显客户端自选的任意子协议（无白名单）
- **文件行号**：`src/agent_harness/web/app.py:1741-1755`（回显循环 `:1752-1754`），经 `:1882` 注入 `accept`
- **问题**：业务子协议不在列表时，回显**第一个非 token 前缀的**子协议 —— 即客户端自带什么就回显什么。服务端因此声称支持一个它并未实现的协议（违反子协议协商的基本约定）。
- **复现/推理**：`client.websocket_connect("/api/ws", subprotocols=["other.product.v9"])` ⇒ 101 带 `Sec-WebSocket-Protocol: other.product.v9`；base 树无回显。**未实测**（本会话无执行权限），但循环逻辑可直接读出。安全影响：无（回显值来自同一客户端、不回显给他人、不构成注入）；影响面是协议一致性 + 第三方客户端的行为变更。
- **修复建议**（一行）：`return WS_BUSINESS_SUBPROTOCOL if WS_BUSINESS_SUBPROTOCOL in subprotocols else None`

### P3-A｜三处 docstring 声称「HTTP/WS 共用文案」，代码里 WS 根本没碰那份文案
- **文件行号**：`projects.py:85`（「跨源拒的**唯一文案**（HTTP 403 detail 与 WS 日志共用，防两处漂移）」）、`projects.py:108`（「策略、文案、常量不再各写一份」）、`app.py:1867`（`if check_trusted_origin(...) is not None:` —— **返回值被丢弃**）、`app.py:1871-1874`（WS 另写一条字面量日志）
- **依据**：`cross_origin_reason` 全树唯一调用者是 `projects.py:112`（HTTP 侧）。这三处正是上一轮 P3-1/P3-2 的同族——注释断言了一个代码里不存在的机制。
- **修复建议**：让 WS 也用 `cross_origin_reason` 拼日志，或把三处措辞降级为「共用**判据**；出口文案两侧各一」。

### P3-B｜交付树 `dd1f5d70` 没有任何覆盖它的 Gate-0 读数
- **文件行号**：`docs/gate/c04c871163c8596196e108ba975e45476e77627e.json:4`（`"sha": "c04c8711…"`）
- **推理**：最终树 `docs/gate/` 下与本票相关的读数**只剩 `c04c8711…json` 一个**（我实测 `ls` 过滤 8 个候选 SHA，仅此一个命中）。按 reflog，`c04c8711` 在 `12d22afd` **之前**，而 `6cf091a0` 起才是本票 diff 的主体（子协议通道 + P1/P2/P3/P4 修回）。`evidence.txt:21` 自己也把最终 `gate0.py` 6/6 标为 **NOT_RUN**。
- **后果**：派工简报（`890-review-brief.md:110`）与台账把 `c04c8711` 称作「交付 tip 的读数」是错误标注——它测的是**fixback 之前**的树，不覆盖本票实际交付的 `src`/`web`/`tests`。文件名本身没撒谎，是引用它的人撒了谎。
- **修复建议**：对 `dd1f5d70` 补跑干净沙箱 Gate-0 并落盘；在此之前订正简报/台账措辞。

### P3-C｜调研文档「三家 / 三行」与来源清单实际四行不符，License 段漏 Kubernetes
- **文件行号**：`docs/research/2026-10-09-ws-auth-handshake-product-research.md:82`（「补了 Channels / Phoenix / Socket.IO **三行**」）、`:87`（「**三家**均为宽松许可」）；对照 `docs/agents/reference-sources.md:108-111`（实际 **4 行**，含 Kubernetes）
- **依据**：我逐行核对了来源清单——Kubernetes 行确实存在，而调研文档既未计入行数、也未做 License 披露（k8s 是 Apache-2.0）。这正是本票 P1-1 所采纳方案的来源，读者按文档复核会漏掉它。结论「未逐字复制 ⇒ 不新增 THIRD_PARTY_NOTICES」大概率仍成立，但枚举与披露不完整。

### P4 级
1. **uvicorn 行号范围差一行**：`app.py:1835` 与 `research…md:67` 均写 `websockets_impl.py:295-303`；实测本仓 `uvicorn 0.52.4` 该分支在 **296-304**（`:296` 判 `websocket.close`、`:302` `initial_response = (FORBIDDEN, [], b"")`、`:304` `closed_event.set()`）。结论正确，行号定位到邻行。
2. **台账首行超硬上限且命中 lint**：`890-ws-auth-53585080-60467fa8.tsv:1` 整行 1123 字符 > 协议 §8.5 上限 800，且 `accept()` 触发 `empty_parens`（机器读数在 `coverage-final-dd1f5d70.log:160-162`）。默认 warn 故 lane 仍 PASS，但属**新行**，而该 lint 的既定做法是「新行从严、存量登记」。建议压缩并把 `accept()` 写进反引号。
3. **台账行指向不存在的交付物**：同一行「验收 1-4 逐条证据见 **PR 描述**」——本票未开 PR（`evidence.txt:21` push/PR = NOT_RUN）；「独立两轴审查（商汤 glm-5.3-flash…）：待回填」的模型名与用户本票期间定的「审查用同工具/同模型 fresh 会话」口径不符（同一占位亦见于 `890-ws-auth-ac1-evidence-….tsv`）。
4. **HTTP 403 文案改词**：`projects.py:84` 把 detail 由「宿主侧 API（项目 / 目录列举）」改为「…/ **WS 会话流**」。我核实全仓只有 `tests/web/test_workspace_files_api.py:771` 断言该文案且是子串匹配（`"拒绝跨源访问" in detail`），无测试破坏；但这是 HTTP 面对外可见字符串的**未声明改动**，且 WS 侧实际上只在未配密钥时才受来源闸保护。
5. **WS 凭据拒的 reason 未抽常量**：`app.py:1859` 内联 `"websocket credential rejected"`，与 `:1706` 具名常量 `WS_ORIGIN_DENIED_REASON` 不对称——两者受同一条 ASGI `reason` 约束。
6. **测试文件头过度收窄**：`tests/web/test_ws_auth.py`「这 31 例覆盖的是**判据矩阵**……不是状态码」——这 31 例里还有放行行为/协商断言（`_ping_pong`、`ws.accepted_subprotocol`），它们恰恰是 over-fix 反锚，被这句话缩小了判别力。
7. **子协议通道对第三方反代的依赖未落文档**：若反代不转发升级请求的 `Sec-WebSocket-Protocol`（或 101 的回显），浏览器侧会静默降级到 SSE。我核实**本仓两条既有代理都转发**该头（`desktop/src/service-proxy.ts:71-77` 逐头透传、`web/vite.config.ts` 有 `ws:true`），故风险限于第三方反代；仅建议在部署文档写明该头必须转发。

## 三、验收标准 1–4 verdict

| AC | verdict | 依据 |
|---|---|---|
| **1 无凭据不能读/不能写** | **满足** | 拦截在 `accept()` 之前（`app.py:1789` → `:1814`）；全仓唯一 WS 路由；拒绝路径结构上到不了 `websocket.py` 的 `accept()`；测试用真实 uvicorn + 真会话对照（事件条数「一条不增」+ 带凭据对照增）。两侧 WS 后端（`websockets_impl.py:296-304`、`websockets_sansio_impl.py:462-471`）均确认握手前 close ⇒ **403** 且不建连（我自己核到） |
| **2 有凭据行为不变** | **满足** | 既有 WS 测试 878/879 绿（唯一红 `test_metrics_slow_resampling…` 计时断言 0.87 vs 0.8，隔离证据充分：该文件与 `metrics.py` 区间零改动；`webblock-ab` 整块 base 846/0 vs fix 879/0）；新增子协议通道经同一 `_resolve_identity`，无第二套判据。**残留**：P2-A 的回显行为对第三方客户端是变更 |
| **3 未配密钥行为不变** | **部分满足** | 语义已写清且**如实披露**为行为变更（跨源握手由放行改拒绝，受影响旧用法 `file://` / sandboxed iframe）。收紧方向正确（封 drive-by），但字面「不变」不成立 |
| **4 双侧测试有鉴别力** | **满足** | 33 例、两条接缝、断言强度分别说明；红证 base 15 failed / 8 passed（同文件逐字节）⇒ 非事后补绿。例数经我手工点数吻合 |

## 四、两轴是否清零

**两轴均未清零。** 安全骨架本身经得起核（拦截点、fail-closed 完备性、无旁路、HTTP 面零回归），两轴各自独立核到同一结论；上一轮的 P1-1 确实修好了。残余集中在**新引入的子协议通道**（P2-A 越权回显）、**注释与代码相反的声称**（P3-A，P3-1/P3-2 同族复发）与**交付面证据错位**（P3-B）。

- `P0 = 0`
- `P1 = 0`
- `P2 = 1`（P2-A）
- `P3 = 3`（P3-A / P3-B / P3-C）
- `P4 = 7`

**未验证声明**：本会话无 Bash/Python 执行权限，以下为静态推理而非实测——`binascii.Error ⊂ ValueError` 的继承关系（CPython 文档事实，编译型模块源码读不到）、畸形 base64 的解码容错边界、P2-A 的实连回显、P3-B 的逐提交删除归属。测试读数采信 `evidence.txt` 与沙箱日志（`verify-…log` / `webblock-ab-…log` / `coverage-final-dd1f5d70.log`）原文，标 NOT_RUN 的三项（全量 pytest / 全量 Playwright E2E / 干净沙箱 gate0 6/6）一律未采信，结论不依赖它们。

**材料一处未解自洽性**（不影响任何 verdict，登记备查）：本轮 diff 只把 `docs/gate/c04c8711…json` 列为新增，而上一轮 diff 曾把 `05907fa8` / `6d8c0135` / `ae17d775` 三份读数列为新增；本轮 diff 内**无任何删除 hunk**，两者无法同时为真。我无 git 执行权限，未能定位根因，故不据此下结论。

REVIEW_EXIT=0
REREVIEW_EXIT=0
