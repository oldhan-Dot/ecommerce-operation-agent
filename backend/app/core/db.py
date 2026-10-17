"""数据库基础设施：连接配置、连接池与只读健康检查。"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import URL, create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import DeclarativeBase

from app.core.config import BACKEND_DIR, Settings

# 环境就绪检查同时核对迁移版本，避免连接成功却遗漏初始化。
EXPECTED_REVISION = "0013_ai_tasks"


class Base(DeclarativeBase):
    """后续业务模型共享的声明基类，健康检查不自动创建业务表。"""

    pass


@dataclass(frozen=True)
class DatabaseStatus:
    """可向前端返回的检查结果，不携带原始异常或数据库凭据。"""

    ready: bool
    code: str
    message: str


def database_url(settings: Settings) -> URL:
    """按字段构造连接地址，避免密码特殊字符被当作 URL 分隔符。"""
    return URL.create(
        "mysql+pymysql",
        username=settings.db_user,
        password=settings.db_password.get_secret_value(),
        host=settings.db_host,
        port=settings.db_port,
        database=settings.db_name,
        query={"charset": "utf8mb4"},
    )


def database_connect_args(settings: Settings) -> dict[str, Any]:
    """分别设置网络超时和 TLS 校验参数，不因证书错误自动降级。"""
    args: dict[str, Any] = {"connect_timeout": 5, "read_timeout": 10, "write_timeout": 10}
    if settings.db_tls_mode == "VERIFY_IDENTITY":
        ca = Path(settings.db_ssl_ca).expanduser()
        if not ca.is_absolute():
            ca = BACKEND_DIR / ca
        if not ca.is_file():
            raise ValueError("Database CA certificate file is missing")
        args.update(ssl_ca=str(ca), ssl_verify_cert=True, ssl_verify_identity=True)
    elif settings.db_tls_mode == "DISABLED":
        args["ssl_disabled"] = True
    return args


def make_engine(settings: Settings, *, worker: bool = False) -> Engine:
    """创建独立连接池，Worker 不使用额外溢出连接。"""
    if not settings.database_configured:
        raise ValueError("Database settings are incomplete")
    return create_engine(
        database_url(settings),
        connect_args=database_connect_args(settings),
        # 取连接时先检查可用性，并回收长时间闲置的云端连接。
        pool_pre_ping=True,
        pool_recycle=1800,
        pool_size=5,
        max_overflow=0 if worker else 5,
        pool_timeout=5,
        isolation_level="READ COMMITTED",
    )


class DatabaseProbe:
    """按需连接数据库，供 API、命令行与独立 Worker 共用。"""

    def __init__(self, settings: Settings, *, worker: bool = False):
        self.settings = settings
        self.worker = worker
        self.engine: Engine | None = None

    def check(self, *, require_migration: bool = True) -> DatabaseStatus:
        """先验证连接；默认还要求数据库已完成当前环境基线迁移。"""
        if not self.settings.database_configured:
            return DatabaseStatus(False, "DB_NOT_CONFIGURED", "请填写 backend/.env 的数据库配置")
        try:
            if self.engine is None:
                self.engine = make_engine(self.settings, worker=self.worker)
            with self.engine.connect() as connection:
                connection.execute(text("SELECT 1"))
                if require_migration:
                    exists = connection.execute(
                        text(
                            "SELECT COUNT(*) FROM information_schema.tables "
                            "WHERE table_schema = DATABASE() AND table_name = 'alembic_version'"
                        )
                    ).scalar_one()
                    if not exists:
                        return DatabaseStatus(
                            False, "DB_MIGRATION_REQUIRED", "数据库可连接，请执行迁移"
                        )
                    revisions = set(
                        connection.execute(
                            text("SELECT version_num FROM alembic_version")
                        ).scalars()
                    )
                    if revisions != {EXPECTED_REVISION}:
                        return DatabaseStatus(
                            False, "DB_MIGRATION_REQUIRED", "数据库迁移版本不匹配"
                        )
            return DatabaseStatus(True, "OK", "数据库连接及环境迁移版本正常")
        except ValueError:
            return DatabaseStatus(False, "DB_CONFIG_INVALID", "数据库配置或证书文件不正确")
        # 原始连接异常可能包含敏感上下文，只返回脱敏后的固定说明。
        except (SQLAlchemyError, OSError):
            return DatabaseStatus(False, "DB_UNAVAILABLE", "数据库连接失败，请检查地址、网络及凭据")

    def close(self) -> None:
        """在进程退出或应用关闭时释放连接池资源。"""
        if self.engine is not None:
            self.engine.dispose()
