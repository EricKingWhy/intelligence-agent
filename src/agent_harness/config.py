"""全局运行配置：只定义"运行需要什么"，从 .env 读取。"""

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    model_config = {"env_file": ".env"}

    model_provider: str = "deepseek"
    model_name: str = ""
    model_api_key: str = ""
    model_base_url: str = ""

    temperature: float = 0.2
    jwt_secret: str | None = None
    milvus_uri: str = ""
    milvus_token: SecretStr = SecretStr("")
    milvus_collection: str = ""
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
    artifact_store_access_key: str = ""
    artifact_store_secret_key: str = ""
    artifact_store_region: str = ""

    # Capability / Plugin 显式配置（spec 08 §6 V1）：JSON 字符串，
    # 形状 {"<name>": {"provider": "...", "enabled": bool, "options": {...}}}。
    capabilities: str = ""
    # Skills 全局目录（spec 09 §2）；项目目录是 <workspace>/skills/。
    skill_global_dir: str = ""

    log_level: str = "INFO"
    workspace_dir: str = ".agent/workspace"
