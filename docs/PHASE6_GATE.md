# Real Zilliz Cloud Gate Result

2026-09-04：**PARTIAL / BLOCKED；#56 不可判定 PASS，不应 Close。**

## Environment

- Provider: Zilliz Cloud / Milvus Compatible
- Deployment: Free / Serverless
- Region: aws-eu-central-1
- Endpoint: https://in03-6d682da1f42feea.serverless.aws-eu-central-1.cloud.zilliz.com
- Database: default（SDK 默认，无新增 database 配置）
- Collection: memory_gate_test
- Token: REDACTED

现有 Settings 使用 MILVUS_URI / MILVUS_TOKEN / MILVUS_COLLECTION；Milvus token 为 SecretStr。实际凭证文件是 `D:\intelligence-agent-backend\.env`，`git check-ignore .env` 确认忽略；用户文字中提及的仓库外 `D:\intelligence-agent-backend.env` 不存在。

## Connection

Client initialization / TLS / network / authentication / list_collections：PASS。测试调用正式 MilvusVectorStore.connect()，没有另建 MilvusClient 业务路径。

## Collection

- Creation：未执行，避免在 embedding 模型未确定前猜测维度。
- 正式 schema：id（SHA256 namespace + memory ID）、memory_id、tenant_id（partition key）、user_id、scope、session_id、content、metadata JSON、vector FLOAT_VECTOR。
- Dimension：由注入的 LangChain Embeddings 实际输出维度决定，尚无选定模型。
- Metric / index：COSINE / AUTOINDEX（现有 Adapter）。

## CRUD

- Insert、存在记录的 Query/Get、Vector Search、TopK、Metadata/Filter、ID/payload mapping、Delete：真实 Gate 尚未执行。
- 缺失 Collection 的 Query：PASS，映射 collection_not_found。
- Cleanup：本轮未创建 Collection，未写入或删除任何记录，无测试数据遗留。

## Architecture

复用现有 Settings / MilvusVectorStore。没有新增 ZILLIZ_*、VECTOR_STORE_* 或第二套 MILVUS_* 配置。MemoryCapability、ContextProvider、Runtime 写回已有离线测试；本轮连接测试不等同于完整 Memory 闭环。

认证负例发现 pymilvus 把底层 gRPC UNAUTHENTICATED 包装成 MilvusException(code=2)。Adapter 现在沿 cause 链读取结构化错误码，映射为 authentication；不解析或输出原始错误文本。无效 token 使用独立固定测试值，不修改真实 token。

提交前扫描当前 diff 和新增文件，未发现真实 Milvus token。报告、测试和示例配置均不包含密钥。

## Validation

- 真实：`python -X utf8 -m pytest tests/integration/test_phase6_memory_e2e.py -m integration -q --tb=short` → **2 passed**。
- 真实 failure cases：invalid token / authentication、collection not found 均通过。
- 离线：SDK schema mismatch、embedding 输出维度、身份过滤、包裹的认证错误有针对性覆盖；不替代真实 CRUD 验收。
- 全量离线：502 passed、8 skipped、7 deselected；ruff clean、uv lock --check 通过。

## Remaining Issues

代码、Settings、ADR 和 Engineering Specification 未指定 embedding 模型。需要用户提供模型名、服务 endpoint 及凭证文件路径（本地模型则提供模型名/路径）。不把现有聊天模型默认为 embedding 模型。

模型确定后：接入实际 Embeddings → 按正式 Adapter 初始化测试 Collection → 真实插入/查询/语义或距离/TopK/过滤/删除 → Runtime 自动抽取、outbox 同步、同用户跨 Session 召回、跨用户/租户隔离 → 验证清理。组合入口负责 writer 与 relay 生命周期。

只有上述验收完成后，才能判定 #56 PASS / Close 和 Phase 6 COMPLETED。
