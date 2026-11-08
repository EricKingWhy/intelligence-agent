# #1542 — Artifact 元数据合同对照

**结论：当前 S3 与 MinIO 的可选元数据差异符合现行合同，本票没有证据支持修改 Provider。** 但 MinIO 缺少 `tool_call_id` 会使依赖该字段的证据归属检查 fail-closed，属于可用性差异；若要求跨 Provider 保留归属能力，应另立合同决策/实现票。

## 复核范围与方法

重查当前树（基线 `8c8f9f2eb5ac896c5ab19c912e7873064ef8c28e`）。Issue 引用的 `master-synthesis.md` 与 `arch/codex-verify-wave1.md` 不在仓库；结论基于当前规格、代码、调用点和实测，不依赖缺失材料。新增离线内存 S3 兼容后端，通过两个 Provider 的公开 `save()` / `load()` 记录 put/get 请求和 get 原始响应；不启动网络服务，不改生产 Provider。

样本为 `abc`、LF、`é`，UTF-8 字节 `6162630ac3a9`，长度 6；独立用标准库 SHA-256 算出的 16 位 ref 为 `37de5c36fc3e9082`。两个 Provider 使用相同 session、内容、MIME 与调用来源。

## 字段合同

| 字段 | 来源与存储 | S3 读回 | MinIO 读回 | 消费者含义 |
|---|---|---|---|---|
| `artifact_id` / ref | UTF-8 内容 SHA-256 前 16 位；key 为 `{session_id}/{artifact_id}` | 元数据携带 id，重算 body hash | 从 ref 与 body 重建并重算 hash | inspect/read 工具凭 ref 找内容；相同内容同 ref |
| `session_id` | save 参数与 Store 固定命名空间必须一致 | JSON 元数据保存，key 也按 session 隔离 | 由 Store 命名空间/key 重建 | 正常调用两边都返回本会话 id |
| `size` | UTF-8 **字节数** | JSON 元数据保存 | 按读回 body 字节数重建 | 样本两边均为 6 |
| `mime_type` | save 输入 | 从 Artifact JSON 元数据恢复 | 从 ContentType 恢复；缺失时回落 `application/octet-stream` | 样本两边均为 `text/plain; charset=utf-8` |
| `content` | save 返回不带内容；load 返回完整 UTF-8 内容 | body 解码 | body 解码 | 两边返回相同内容并做 hash 校验 |
| `source_tool` | 可选字段；缺失为 `None` | 持久化在 Artifact JSON 元数据 | 不持久化，load 为 `None` | 来源另写入 `artifact/created` 事件；进度投影读事件 |
| `tool_call_id` | 可选字段；缺失为 `None` | 持久化并保留 | 不持久化，load 为 `None` | 证据归属核验使用它；缺失时未知并 fail-closed |
| `created_at` | 可选字段；save 时生成 | 持久化并保留 | 不持久化，load 为 `None` | 当前代码检索未发现读取 `Artifact.created_at` 的直接消费者 |

## 权限与完整性

两端都向 SDK 配置 endpoint、凭据、SigV4 与 path-style addressing；S3 另带 region。离线后端核实了客户端参数和 bucket/key 请求。注入 `AccessDenied` 后，两个 Provider 都保留拒绝错误、不伪装为 not-found。将已存 body 改成 `tampered` 后，两边 `load()` 都以 hash mismatch 拒绝。

**未验证真实服务端 ACL、签名和部署权限**：本次使用假凭据和内存后端，没有连接 S3/MinIO 服务。核验范围是本地配置传递、拒绝错误映射和 SDK 适配边界。

## 原始离线请求 / 响应

以下来自 2026-11-08 对两个公开 Provider 方法的实际运行。Body 同时记 UTF-8 与十六进制，可无损复核；S3 的 `Metadata.artifact` 是 SDK 边界上原样传递的 JSON 字符串。没有记录凭据值。

```json
{
  "payload": {
    "utf8": "abc\né",
    "hex": "6162630ac3a9",
    "size": 6,
    "artifact_id": "37de5c36fc3e9082"
  },
  "s3": {
    "sdk": {
      "endpoint_url": "https://s3.example.test",
      "region_name": "test-region-1",
      "credential_configured": true,
      "signature_version": "s3v4",
      "addressing_style": "path"
    },
    "put_request": {
      "Bucket": "s3-bucket",
      "Key": "session-1/37de5c36fc3e9082",
      "ContentType": "text/plain; charset=utf-8",
      "Metadata": {
        "artifact": "{\"artifact_id\": \"37de5c36fc3e9082\", \"session_id\": \"session-1\", \"size\": 6, \"mime_type\": \"text/plain; charset=utf-8\", \"source_tool\": \"shell\", \"tool_call_id\": \"call-42\", \"created_at\": \"2026-11-08T09:49:26.337+00:00\"}"
      },
      "Body_utf8": "abc\né",
      "Body_hex": "6162630ac3a9"
    },
    "get_request": {
      "Bucket": "s3-bucket",
      "Key": "session-1/37de5c36fc3e9082"
    },
    "raw_get_response": {
      "ContentType": "text/plain; charset=utf-8",
      "Metadata": {
        "artifact": "{\"artifact_id\": \"37de5c36fc3e9082\", \"session_id\": \"session-1\", \"size\": 6, \"mime_type\": \"text/plain; charset=utf-8\", \"source_tool\": \"shell\", \"tool_call_id\": \"call-42\", \"created_at\": \"2026-11-08T09:49:26.337+00:00\"}"
      },
      "Body_utf8": "abc\né",
      "Body_hex": "6162630ac3a9"
    },
    "loaded_optional_fields": {
      "source_tool": "shell",
      "tool_call_id": "call-42",
      "created_at": "2026-11-08T09:49:26.337+00:00"
    }
  },
  "minio": {
    "sdk": {
      "endpoint_url": "https://minio.example.test",
      "credential_configured": true,
      "signature_version": "s3v4",
      "addressing_style": "path"
    },
    "put_request": {
      "Bucket": "minio-bucket",
      "Key": "session-1/37de5c36fc3e9082",
      "ContentType": "text/plain; charset=utf-8",
      "Body_utf8": "abc\né",
      "Body_hex": "6162630ac3a9"
    },
    "get_request": {
      "Bucket": "minio-bucket",
      "Key": "session-1/37de5c36fc3e9082"
    },
    "raw_get_response": {
      "ContentType": "text/plain; charset=utf-8",
      "Body_utf8": "abc\né",
      "Body_hex": "6162630ac3a9"
    },
    "loaded_optional_fields": {
      "source_tool": null,
      "tool_call_id": null,
      "created_at": null
    }
  }
}
```

## 合同依据与回裁建议

- Engineering Spec 06 §3 要求 Artifact 保存 metadata/hash/type/size/ref，§4 用 ref 局部读取；没有规定这三个可选归属/时间字段必须被每个 Provider 持久化。
- Reuse Matrix 的 MinIO SDK 是 REUSE + ADAPT，并写明 Artifact contract 不绑定 MinIO；Roadmap Phase 5 列有 MinIO Provider。
- `docs/WORKSPACE_PANEL_TICKETS.md` 的 #185 AC4 明确规定 MinIO 不持久化 `source_tool` / `tool_call_id` / `created_at`，缺失应为 null/省略，不得伪造默认值；与当前实现和实测一致。
- ADR-0006（2026-09-04）称“不做 MinIO”，与现行 Spec 06、Roadmap 和当前代码冲突。按当前规格优先级，它是早期历史决策，不能推翻现行合同；本票不改 ADR 或 Provider。
- `tooling/overflow.py` 把来源和调用 id 写入 `artifact/created` 事件；`session/progress.py` 从事件投影进度。`session/service.py::_artifact_birth_tool_call_ids()` 另读 Store 的 `tool_call_id` 做证据归属；MinIO 缺失时得到 `None`，`session/evidence.py` 将无法确认的归属判为 stale。这保护安全性，但可能降低 MinIO 下证据可复用性。读取工具主要依赖内容与 ref。
- **本票建议**：保留 `None` 与现有 fail-closed 语义，不把差异修成空值，也不合并 Provider。若产品要求 MinIO 可核验 artifact/tool-call 归属，应另开合同/实现票，裁定是否持久化元数据或改由 SessionEvent 提供权威归属。

## 验证

- `PYTHONUTF8=1`、`PYTHONPATH=<当前 worktree>/src` 下运行 `python -m pytest tests/storage -q -rs -p no:randomly`：**169 passed / 81 skipped，9.31s**。跳过原因为 Windows 不允许 `?` 文件名，以及 CJK tokenizer 缺少 gcc/源码；本票新增用例在通过的集合中。
- `python -m ruff check tests/storage/test_remote_artifact_metadata_contract.py`：**All checks passed**。
- 本票新增测试与既有 S3 测试定向组合：**14 passed**。
- Gate-0：代码与双轴审查台账树 `a6bbd22e5ee045d375390d44729397adf1b880d8` / tree `b12e9fe1117fd68255ecba54d97b30adebc0eab0` **7/7 PASS，54.01s**；收据 `docs/gate/a6bbd22e5ee045d375390d44729397adf1b880d8.json`。随后仅追加进度文档。
- 发布前最终 Gate-0 在 `03532fe218ac17aca579448b9a3eb14adebed7c2` / tree `226a090a00171ad4b7aba1e5c24cb2ef36d43048` **7/7 PASS，38.7s**；收据 `docs/gate/03532fe218ac17aca579448b9a3eb14adebed7c2.json`。
- 未使用 Docker 或真实存储服务；离线后端调用两个 Provider 的真实公开方法。


## 发布状态

PR [#1684](https://github.com/EricKingWhy/intelligence-agent/pull/1684) 已创建并保持 OPEN；服务端 CI 待完成。Issue #1542 保持 OPEN；未合并或关闭。