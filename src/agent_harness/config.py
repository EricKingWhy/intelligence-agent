"""全局运行配置：只定义"运行需要什么"，从 .env 读取。"""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    model_config = {"env_file": ".env"}

    model_provider: str = "deepseek"
    model_name: str = ""
    model_api_key: str = ""
    model_base_url: str = ""

    temperature: float = 0.2

    log_level: str = "INFO"
    workspace_dir: str = ".agent/workspace"
