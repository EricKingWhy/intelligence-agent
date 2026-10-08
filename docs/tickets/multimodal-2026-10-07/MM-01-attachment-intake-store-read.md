# MM-01 · 服务端附件入站：流式上传、内容寻址存储、受控读回

**目标仓库**：intelligence-agent-backend（Python Core / ArtifactStore / FastAPI）。
**类型/优先级**：P0 Contract。**Parent**：#821。
**Blocked by**：None（可立即开始）。

## What to build

任一客户端能把一张图片的字节交给服务端，拿到一个不透明 id；之后能用这个 id 把字节原样取回。
同一张图重复上传只存一份。别的会话拿到这个 id 也取不到内容。文件名声明成 `.png` 但字节不是图片时，
按字节判定被拒绝，而不是相信客户端的声明。

这一票只做「字节进得来、存得住、按授权取得回」，**不**让模型看到图（那是 MM-02）。

## Acceptance criteria

- [ ] 上传端点接受流式请求体（multipart 或 octet-stream），返回
      `{attachment_id, media_type, bytes, width, height, name}`；`attachment_id` 形如 `sha256:<hex>`，
      **不含**路径、裸 URL 或任何可猜测的外部句柄。
- [ ] 该路由不受既有 JSON body 1 MiB 上限约束；既有 JSON 端点的上限与错误行为**逐字不变**（回归测试）。
- [ ] 同一字节序列重复上传得到同一 `attachment_id`，存储只落一份（去重）。
- [ ] 读取端点返回原始字节与正确 `Content-Type`；**未被本 Session 事件引用**的 id 返回 404；
      属于其他 Session 的 id 返回 404（不泄露存在性）。
- [ ] MIME 按 magic bytes 判定；与声明文件名/类型不符时拒绝（4xx）。客户端声明不是权威。
- [ ] 超单张字节上限 → 413，且不落事件、不留存储残留。
- [ ] 落盘走 staging → fsync → 原子发布；半途失败（进程中断/写失败）不产生可被读到的半个文件。
- [ ] ArtifactStore 新增**按字节**的读写与哈希路径，既有文本路径行为不变（回归）。
      MinIO Provider 满足同一字节契约（无 MinIO 环境时用既有替身测试）。
- [ ] 上限配置键落地：单张字节 / 单消息数量 / 单消息总字节 / 像素上限 / 边长上限 / 允许 media types；
      默认值取 DSH 一组（20 MiB、20 张、200 MiB、64M 像素、8192px）。
- [ ] 读取像素尺寸需要解析图片头，但本票**不**做方向/格式/质量归一化。
- [ ] 测试覆盖：上传→读回字节相等、去重、未引用 404、跨 Session 404、伪扩展名拒绝、
      半途失败无残留、既有 JSON 上限回归。

## 复用与来源

- DSH `packages/attachment`（MIT，commit `5badb150`）：`AttachmentId` / `ImageAttachmentRef` /
  `ImageAttachmentLimits` 契约，以及 `attachment-local` 落盘算法（staging、fsync、原子发布、
  hardlink 去重、权限收紧、文件名消毒含 Windows 保留设备名与 255 字节截断）。
  **只移植契约与纯算法**，不移植任何 Cordis 绑定的类。
- 本仓 ArtifactStore：REUSE，做加法式扩展，不替换既有文本语义。
- 判定依据见 `docs/research/2026-10-07-multimodal-image-input-research.md` §7.3-A。

## 明确不做

图片归一化与聚合上限（MM-02 / MM-03）、任何 UI、孤儿附件清理（PRD Out of Scope）、
跨 session 共享附件。
