"""Alembic 迁移入口，沿用应用配置与声明模型元数据。"""

from alembic import context
from app import models  # noqa: F401 -- 注册业务模型，供迁移差异检查使用。
from app.core.config import get_settings
from app.core.db import Base, database_url, make_engine

settings = get_settings()
if not settings.database_configured:
    raise RuntimeError("Database is not configured. Fill backend/.env before running migrations.")

if context.is_offline_mode():
    # 离线模式只生成 SQL，不向数据库提交实际变更。
    context.configure(
        url=database_url(settings),
        target_metadata=Base.metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()
else:
    # 在线迁移显式建立连接，执行完成或失败后都释放连接池。
    engine = make_engine(settings)
    try:
        with engine.connect() as connection:
            context.configure(
                connection=connection, target_metadata=Base.metadata, compare_type=True
            )
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()
