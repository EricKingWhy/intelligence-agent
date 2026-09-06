"""全局运行配置：只定义"运行需要什么"，从 .env 读取。"""

from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings

#: .env 锚定到仓库根（config.py 位于 <root>/src/agent_harness/）——
#: 相对路径 ".env" 依赖 CWD，从其它目录启动 uvicorn/CLI 会静默加载不到
#: 配置，错误推迟到首个请求内才爆（R6-5）。安装为 site-packages 时锚点
#: 无意义但无害（该路径下不存在 .env，退化为环境变量配置）。
_REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    # extra="ignore"：部署机 .env 常混有编辑器/部署工具/其它应用的变量；
    # 默认 extra='forbid' 会把它们当成致命错误让启动直接崩。外键忽略。
    model_config = {"env_file": str(_REPO_ROOT / ".env"), "extra": "ignore"}

    model_provider: str = "deepseek"
    model_name: str = ""
    # SecretStr：与 milvus_token / embedding_api_key 一致——model_dump() / repr()
    # 脱敏为 **********，避免任何把 Settings 转储进日志/调试器/异常页的路径泄漏 live key。
    model_api_key: SecretStr = SecretStr("")
    model_base_url: str = ""

    temperature: float = 0.2
    # 流式守卫（秒，逐项 ≤0 关闭）：idle = N 秒无新 chunk（死连接）；
    # total = 整条流必须 N 秒内完成（慢滴漏，冒烟实测 10 分钟场景）。
    # 超时抛 ModelStallError（瞬时）→ fallback 接管 / 统一失败兜底。
    model_stream_idle_timeout: float = 60.0
    model_stream_total_timeout: float = 600.0
    # 进程级模型调用并发闸（#89）：parent+child 共享上限，防 TPM/QPM 限流与
    # 机器过载。≤0 = 关闭。TPM 令牌桶限流器 DEFER（ADR-0015）。
    model_max_concurrency: int = 3
    # Model Fallback 两级链（ADR-0014 决策 14）：FALLBACK_MODEL_PROVIDER 为空 =
    # 单级（无 fallback）。fallback key 同 SecretStr 脱敏待遇（活密钥）。
    fallback_model_provider: str = ""
    fallback_model_name: str = ""
    fallback_model_api_key: SecretStr = SecretStr("")
    fallback_model_base_url: str = ""
    # jwt_secret 泄漏等于可伪造任意身份，与 API key 同一脱敏待遇。
    jwt_secret: SecretStr | None = None
    milvus_uri: str = ""
    milvus_token: SecretStr = SecretStr("")
    milvus_collection: str = ""
    # Knowledge 域独立 Collection（ADR-0013 决策 13）：必填无默认——
    # 不配 = knowledge capability 缺席降级，绝不静默写默认库。
    knowledge_collection: str = ""
    # sufficient 阈值（ADR-0013 决策 9，用户拍板 0.6：低于此证据太弱，
    # 宁可如实标记不足防幻觉）。hits 照返，标记是诚实信号不是结果开关。
    knowledge_min_score: float = Field(default=0.6, ge=0, le=1)
    # Web Search（ADR-0014 决策 9/10）：TAVILY_API_KEY 为空 = websearch
    # capability OPTIONAL_RUNTIME 降级缺席（不配 = 不联网，绝不静默触网）。
    tavily_api_key: SecretStr = SecretStr("")
    embedding_model: str = ""
    embedding_base_url: str = ""
    embedding_api_key: SecretStr = SecretStr("")
    embedding_dimensions: int = Field(default=1024, gt=0)

    max_context_tokens: int = 200_000
    auto_compact_threshold: float = 0.70
    hard_guard_threshold: float = 0.85
    artifact_overflow_chars: int = 2000
    artifact_store_endpoint: str = ""
    artifact_store_bucket: str = ""
    # S3 密钥泄漏等于丢失整个 artifact bucket 的写权限，同一脱敏待遇。
    artifact_store_access_key: SecretStr = SecretStr("")
    artifact_store_secret_key: SecretStr = SecretStr("")
    artifact_store_region: str = ""

    # Capability / Plugin 显式配置（spec 08 §6 V1）：JSON 字符串，
    # 形状 {"<name>": {"provider": "...", "enabled": bool, "options": {...}}}。
    capabilities: str = ""
    # Skills 全局目录（spec 09 §2）；项目目录是 <workspace>/skills/。
    skill_global_dir: str = ""

    log_level: str = "INFO"
    workspace_dir: str = ".agent/workspace"
    # detached-run 孤儿回收宽限期（秒，ADR-0016 §2.1）：零订阅者连续超过
    # 该时长 → run 被取消收尾（run/failed(reason=orphaned)）。有订阅者期间
    # 不计时；≤0 = 不回收（不推荐：无人观看的 run 会烧到自然终态）。
    run_disconnect_grace_seconds: float = 300.0
    # 会话级可选模型 catalog（ADR-0016 §5，C6）：JSON 数组
    # [{"name", "provider", "model_name", "base_url"?, "api_key"?, "temperature"?}]。
    # api_key 缺省回落 MODEL_API_KEY，base_url 缺省回落 provider 预设。
    # 空 = 无可选模型（GET /api/models 只列默认链，model 参数一律 422）。
    # SecretStr：JSON 里可带条目级 api_key（活密钥），dump()/repr() 一律脱敏。
    agent_models: SecretStr = SecretStr("")

    # Langfuse 旁路观测（ADR-0018 D2/D6，首个 OPTIONAL_OBSERVABILITY 实现）：
    # key 空 = 旁路完全缺席（懒加载，零 import 开销）。key 同 SecretStr 脱敏待遇。
    langfuse_public_key: SecretStr = SecretStr("")
    langfuse_secret_key: SecretStr = SecretStr("")
    langfuse_base_url: str = ""
    # trace 上云内容边界：full=完整输入输出（自有 dev 项目默认）；
    # redacted=只传 metadata + 截断/摘要。非法值按 full 处理并告警。
    langfuse_trace_content: str = "full"
