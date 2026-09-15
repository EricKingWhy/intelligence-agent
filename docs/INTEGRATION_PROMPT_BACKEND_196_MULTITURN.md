# 集成提示词 — #196 在途输入通道消费侧（后端，feat/backend）

- **Ticket**: [backend] queue/steer 通道「契约在、实现不在」（#196，bug）
- **Branch / commit**: `feat/backend` @ `3d9dc28`
- **Design**: `docs/adr/0030-mid-run-input-steering-queue-durability-and-query-supersede.md`
- **Gate**: ruff check 全绿；全量 pytest 2298 passed / 10 skipped / 0 failed

## 做了什么

issue #196 的根因是「契约层与内存层的测试都是绿的，端到端行为是空的」：在途
run 期间发的消息被 ack 成 `queued`、落一条 `message/queued` 事件，然后**永久静默**
（无人消费、界面不渲染、run 结束后不接力）。本票把消费侧接成可对账闭环：

| 层 | 改动 |
| --- | --- |
| 事件 | `queue/consumed {queue_id, run_id}`、`message/superseded {superseded_seq, carrier}`（`session/event.py`，EVENT_TYPES 白名单同步） |
| 投影 | `derive_messages` 识别 supersede 区间 `[s, 下一条未被取代的 user 消息)`，与 compaction bracket 走同一条 `is_shadowed`（刻意两个列表，保住 summary 下标对齐）；dangling 合成在 shadow 之后 |
| runtime | 循环头（`ContextBuilder.build` 之前）经新端口 `SteerSource` 注入 steer：`user/message{steer_id}` + `steer/applied{applied_seq, run_id}`。**绝不**设 `injected_by`（否则记忆抽取静默丢弃用户的话）；run_id 不匹配的陈旧 steer 丢弃并记日志，由终态驱动当普通输入投递 |
| 驱动 | 唯一投递点 `SessionService.deliver_next_undelivered` / `on_run_terminal`：读**事件流**（`derive.undelivered_inputs`）按到达顺序取 1 条接力开新 run；消费事实（`queue/consumed` / `steer/applied{applied_seq:null}`）在投递成功**之后**写（崩溃窗口选"重复回答"而不是"静默丢失"）。`RunManager` 新增可选 `on_run_terminal` 回调，`_drive` 收口后调用；`aclose()` 关停不再接力 |
| HTTP | `SendMessageRequest` 增 `supersedes_seq` / `queue_id`；新增 `GET /api/sessions/{sid}/queue`、`POST /api/sessions/{sid}/queue/flush`（launched 分支与 `/messages` **共用同一段** SSE 生成器）；`SupersedeTargetInvalid` → 409 |
| 重启 | `scan_interrupted()` 后 `rebuild_message_queues()` 按事件流重建内存镜像（**不自动起 run**）；`cancel_queue` 判据补事件流复核（镜像只是缓存） |
| 生成物 | `web/src/generated/event-types.ts`、`docs/EVENT_VOCABULARY.md` 重新生成 |

## 对 ADR-0030 的实现偏离（已定案）

终态驱动读**事件流**而不是先 drain 内存队列——只有事件流同时看得见 queue 与
steer 的到达顺序（ADR D7），也免掉 ADR §4.7 草案里的 `_requeue_front`：我们从没
把输入弹出内存，`ActiveRunConflict` 竞态下输入留在事件流等下一个终态，无丢失窗口。

## 测了什么

- T4 supersede 投影 11 例（`tests/session/test_derive_supersede.py`），含
  "supersede 区间与 compaction 并存时 summary 不被挤掉"的回归锁。
- T1/T2/T3/T9/T10 端到端 8 例（`tests/session/test_multiturn_delivery.py`）：
  真实 AppState + RunManager 终态回调 + ScriptedModel gate。含 T1 queue 自动接力
  （无客户端动作）、T2 steer 同 run 注入、T3 A+B 都在链上、陈旧 steer 降级投递、
  T9 重启重建不自动起 run、T10 steer 参与记忆抽取。
- T5/T8 HTTP 7 例（`tests/web/test_multiturn_queue_http.py`）：真实 ASGI 服务器。
  含 supersede 非最新/未知 seq/重复 409、`GET /queue`+flush 往返、编辑排队项。

**遗留**：T7（真实 provider 连续 steer 冒烟）与 T11/T12（前端 e2e）属后续票/人工项。

## 前端 #195 依赖本票冻结的契约

- `GET /api/sessions/{sid}/queue` → `{"items": [{queue_id, content, created_at}], "steers": [{steer_id, content, created_at}]}`（数据源是事件流）。
- `POST /messages` 新字段：`supersedes_seq: int|null`（取代最新一条用户消息，非最新/已取代/注入消息 → 409）、`queue_id: str|null`（替换某条排队项）。
- `POST /queue/flush` → 空队列 `{"status":"idle"}`；有 → 与 `/messages` 同形的 SSE 流。
- 前端事件类型已重新生成：`web/src/generated/event-types.ts` 新增 `queue/consumed`、`message/superseded`——**前端 #195 需要把这两个 type 加进 `projection.ts` 的 `EVENT_SEMANTICS`**（缺 key tsc 会红）。
- 前端 shadow 规则与后端 `derive.py` 同构：`[superseded_seq, 下一条未被取代的 user 消息)`。

## 集成注意

1. 本票未推送、未合并——按 §13/§14 由集成 AI 合入 `main` 并验证。
2. 合并后建议跑一次 T7 冒烟（连续两条 steer 在真实 provider 上是否被接受），
   结论回填 ADR-0030 §11。
3. 旧会话（无 `queue/consumed` 词汇）兼容：`undelivered_inputs` 只认 `message/queued`/
   `steer/requested` 的缺席收口，旧事件流按原语义解析，无需迁移。
