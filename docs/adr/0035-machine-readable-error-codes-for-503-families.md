# ADR-0035 — 503 / 409 家族的机读错误码（判别只按码，不按状态码猜原因）

- **Status**: Accepted
- **Date**: 2026-09-17
- **Deciders**: 本 Agent（机制设计）
- **Related**：Issue **#227**（来源：DSH 设计对照审计 `docs/RESEARCH_DSH_PATTERN_GAP_AUDIT.md` §3 T2）；前置同形工作 **#225**（`/api/memories`）与 ADR-0010「补充（#225）」；`src/agent_harness/web/artifacts.py`（`ARTIFACT_STORAGE_UNAVAILABLE` + 选择口径）；`src/agent_harness/web/app.py`（artifact 内容端点）；`web/src/lib/api.ts`（`ArtifactContentError` / `readErrorBody`）；`web/src/components/ArtifactViewer.tsx`；`docs/BACKEND_CONTRACT_STREAMING_UI.md` §3；AGENTS §16.1

> 本文档是「HTTP 错误码怎么用才不让前端猜原因」这条机制的**唯一完整叙述**。代码注释只写各自那一段代码自己看不出来的操作约束 + 指向本文件的一句指针。

---

## 1. Context

### 1.1 症状形状（还没发作，但已经在代码里）

`getArtifactContent` 此前按**状态码**推断原因：

```ts
if (res.status === 503) throw new ArtifactContentError('no-storage', detail, 503);
```

而**后端从来没给过这个码**——`no-storage` 是前端自己贴的标签。今天它成立，只因"503 在这个端点上
只有一个原因"。后端哪天加了第二个 503 原因（例如"存储可达但鉴权失败"），界面会把一次真实故障
说成"本部署没有可读取的 artifact 存储（部署配置问题）"，用户照着重启、改配置，问题一点没变。

这与 **#225** 是**同一个形状**：那次是 `/api/memories` 的 503 把「没配置」与「装配失败」混在一起，
前端只能按状态码猜，于是给一个真故障加了句"这不是故障"。DSH 那侧的做法是把**有类型的码原样跨线**
（现场复核 `docs/api-gateway.md:127`：`RemoteError` "carrying their own code … which the Gateway
**encodes onto the wire unchanged**; only an unclassified throw folds into `gateway/internal`"）。

### 1.2 为什么"文案"不能替代码

后端文案是**给人看的**，会随措辞改动、会本地化、会为了可读性合并不同原因（本端点今天就把四种
原因收成一句"没有可读存储"）。协议不能建立在文案上：前端用文案分支，后端改一个字就静默失效。
**码是协议，文案是呈现。**

---

## 2. Decision

### 2.1 形状：`detail = {code, message}`（与 #225 同一形状）

artifact 内容端点的 503：

```json
{"detail": {"code": "artifact_storage_unavailable",
            "message": "本部署没有可读取的 artifact 存储：artifact_dir 为空，或对象存储只配了一半"}}
```

- `code` 的字面量来自**唯一常量** `web/artifacts.py::ARTIFACT_STORAGE_UNAVAILABLE`（路由不手写第二份）；
- `message` 与改动前**逐字相同**（呈现不变，只是多了一个机读键）。

### 2.2 前端判别：只认码，未知码走通用失败态

| 响应 | kind | 呈现 |
|---|---|---|
| 503 + `artifact_storage_unavailable` | `no-storage` | 「本部署没有可读取的 artifact 存储」 |
| 503 + **无码**，且 `detail` 是**纯字符串**（旧版后端形状） | `no-storage` | 同上（那时这个端点只有这一个 503 原因——**这条兼容分支的前提写在注释里**） |
| 503 + **对象形状却没有合法码**（畸形体） | `error` | 通用失败态——**"码缺席"不等于"旧版后端"**（两轴审查 P3）：把它读成旧版，就等于把"按状态码猜原因"挪到了"码解析失败"这一侧 |
| 503 + **别的码** | `error` | 通用失败态 + 码与后端 `message` 一起显示 |

第三行是本决定的**判别力所在**：后端新增 503 原因时，界面**必须说"读不到，这是后端给的码"**，
而不是继续断言一个它无从知道的原因。`ArtifactContentError.code` 一路带到界面
（`ArtifactContentState.code` → `ArtifactViewer` 渲染 `.artifact-content-code`）——两轴审查
抓到过一版"文档说会显示、实现把码丢掉"，现在的实现与本文一致。

### 2.3 为什么这个端点只有一个码（不细分四种原因）

`select_artifact_store` 返回 `None` 的四种原因（显式关掉本地落盘 / 路径在本平台非法 /
对象存储半配置 / 可选依赖没装）**无法在调用点区分**——选择器刻意把它们收敛成一个"本部署没有
可用 store"。要细分得改选择器的返回形状（那是另一票的改动面）。**码只承诺"这个端点是哪种
503"**，不承诺"哪一种配置错"——后者由 `message` 与后端 warning 日志承担。诚实的分工，好过编一个
前端按码分支却永远拿不到的细分码。

---

## 3. 逐处判定：前端所有"按状态码推断"的地方

#227 的验收要求把这类地方**列全并逐处判定**（给码 / 登记为"不会多原因"并留注释）。
下面是 `web/src/lib/api.ts` 的全量清单（`grep "res.status ==="` 的结果，逐行核对）。

| 位置 | 现状 | 判定 | 理由 |
|---|---|---|---|
| `apiFetch` 401 | → `UnauthorizedError` + 广播重新登入 | **按据不改** | 401 的语义由 HTTP 自身定义（"需要身份"），不是对**原因**的推断；后端没有第二个 401 原因 |
| `getSessionEvents` 404（:145） | → `NotFoundError('会话不存在')` | **按据不改** | 端点级单一原因，且与路由语义一一对应 |
| `postApproval` 409（:523） | → `AlreadyResolvedError('审批已决（幂等）')` | **登记（潜在同类）** | 今天只有一个 409 原因；**若将来出现第二个（如"审批已过期"）必须同时给它码**，否则界面会把过期说成"已决"。登记在此，不预先改 |
| `postApproval` 404（:524） | → `ApprovalGoneError('该审批已失效…')` | **登记** | 同上（今天单一原因） |
| `listSessionQueue` 404（:818） | → `NotFoundError('会话不存在')` | **按据不改** | 端点级单一原因 |
| `cancelQueueItem` 404（:843） | → `NotFoundError('排队项已取消或已消费')` | **按据不改** | 端点级单一原因 |
| **artifact 内容 404**（:960） | → `gone`（"不在本会话里"） | **登记（已知双原因）** | 后端 404 覆盖"会话不存在"与"不在本会话命名空间"两种原因，前端一律读作"不在本会话里"。**两者对用户导向同一个动作**（换会话 / 刷新看看），且后端刻意不让归属可探测 ⇒ 不改，但登记为"已知不精确" |
| **artifact 内容 503**（:961） | → `no-storage` | **本票修复** | 见 §2 |
| `recoverSession` 404（:1122） | → `RecoverError(404, '会话不存在')` | **按据不改** | 端点级单一原因 |
| `recoverSession` 409 / 其它（:1123） | → `RecoverError(409, detail \|\| '存在需要人工裁决的高风险操作')` | **登记（有兜底编原因）** | 后端给了 `detail` 就用它；**没给 detail 时前端会自己断言一个原因**（"存在需人工裁决的高风险操作"）——与本节同一类风险的**最轻形态**（文案接近通用、且 409 在这个端点上今天只有一个原因）。要给码的话是它，不是现状 |
| `getContextUsage` 404（:1578） | → `NotFoundError('会话不存在')` | **按据不改** | 端点级单一原因 |
| 会话删除 404 / 409 | → `DeleteSessionError`（带 status） | **按据不改** | 注释里写着"404 与 409 导向不同的界面收敛"——**状态码在这里是协议的一部分**（用户显式操作的两个不同结局），不是对原因的推断 |
| `/api/memories` | 码化（#225） | **已按码判别** | `isMemoryFault` / `isMemoryDisabled` |

**纪律（写给下一个加状态码的人）**：新增一个 4xx/5xx 原因**之前**先问"同端点同状态码下现在有几个
原因"——超过一个就必须给码，前端只按码分支；只有一个且不会再有，可以在注释里显式写下这个前提
（§2.2 的无码兼容分支就是这么写的）。

---

## 4. 按据不改 / 未覆盖

1. **`ArtifactQueryError`（422）不带码**：422 是"请求形状非法"这一类，前端已单独立类；它不参与原因推断。
2. **码不带命名空间**：DSH 用 `session/not-found`、`gateway/internal` 这类前缀避免扁平命名空间撞车
   （现场复核 `docs/api-gateway.md:127`）。我们沿用 #225 的扁平风格（`not_configured` / `disabled` /
   `missing_settings` / `init_failed` 四个 `DegradeReason` 码，见 `capability/base.py` 与
   `tests/capability/test_phase7_gate.py`），本票新增第五个 `artifact_storage_unavailable`——
   **新码自带领域词**，撞车风险由"值集合有测试钉住"承担。要改成命名空间就得连带改 #225 的四个码
   （跨端契约变更），本票不做。
3. **`gateway/internal` 那种"未分类异常折成显式通用码"**：我们的 5xx 未分类异常走 FastAPI 默认
   500（无 `detail` 结构），前端按"形状不符"走通用失败态 ⇒ 效果等价，登记不改。
4. **真实浏览器的产物读取复核**：本票只做单测 + 后端 API 测试（真机复核需要一次能产出 artifact 的
   真实 run，而真实模型账户当前冻结）——如后续要做，走一次真机巡检补记。
5. **#225 的四个码没有跨端机械闸门**（登记）：本票给新增的 `artifact_storage_unavailable` 建了
   `tests/web/test_error_code_contract.py`（直接读 `web/src/lib/api.ts` 的字面量与后端常量对账，
   单边改名必红）。#225 的 `not_configured` / `disabled` / `missing_settings` / `init_failed`
   **仍是"两侧各自自证"**——`tests/capability/test_phase7_gate.py` 自己也写着"改名不会让任何门禁
   变红"。同一套闸门可以照搬（读前端 `MEMORY_INIT_FAILED` 与 `DegradeReason` 的值集合），
   属已关单票的加固，本票不越界做。
