"""应用配置：统一管理环境变量、敏感字段和环境安全约束。"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 固定从后端目录读取配置，避免启动目录不同而加载错误的 .env。
BACKEND_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """集中读取与校验本机配置，敏感字段使用 SecretStr 避免意外显示。"""

    model_config = SettingsConfigDict(
        env_file=BACKEND_DIR / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    # 开发、测试与生产环境共享配置模型，但应用不同的安全限制。
    app_env: Literal["dev", "test", "staging", "prod"] = "dev"
    # 这里填写云端应用账号的连接参数，不使用数据库 root 账号。
    db_host: str = ""
    db_port: int = Field(default=3306, ge=1, le=65535)
    db_name: str = "ecommerce_agent"
    db_user: str = "ecommerce_app"
    db_password: SecretStr = SecretStr("")
    # 当前开发允许不启用 TLS；启用证书校验时必须提供可信 CA。
    db_tls_mode: Literal["DISABLED", "VERIFY_IDENTITY", "SSH_TUNNEL", "LOCAL"] = "DISABLED"
    db_ssl_ca: str = ""
    # 加密主密钥与会话附加密钥用途不同，不能共用同一个值。
    encryption_key: SecretStr = SecretStr("")
    encryption_key_id: str = "local-v1"
    session_pepper: SecretStr = SecretStr("")
    # 预留同源访问、模型服务白名单、上传目录与 Worker 轮询配置。
    trusted_origins: list[str] = ["http://127.0.0.1:5173", "http://localhost:5173"]
    allowed_model_hosts: list[str] = []
    upload_dir: str = ".local/uploads"
    worker_poll_seconds: int = Field(default=10, ge=1, le=300)

    @property
    def database_configured(self) -> bool:
        """只判断必填项是否齐全，不在读取配置时发起数据库连接。"""
        return bool(
            self.db_host.strip()
            and self.db_name.strip()
            and self.db_user.strip()
            and self.db_password.get_secret_value()
        )

    @model_validator(mode="after")
    def validate_remote_security(self) -> "Settings":
        """防止把开发阶段的明文连接例外带入预发或生产环境。"""
        if self.app_env in {"staging", "prod"} and self.db_tls_mode == "DISABLED":
            raise ValueError("DISABLED database TLS is a development-only exception")
        if self.db_tls_mode == "VERIFY_IDENTITY" and not self.db_ssl_ca:
            raise ValueError("DB_SSL_CA is required when DB_TLS_MODE=VERIFY_IDENTITY")
        return self


# 单进程复用配置实例；修改 .env 后需要重启 API 和 Worker。
@lru_cache
def get_settings() -> Settings:
    return Settings()
