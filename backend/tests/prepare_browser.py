"""M1 浏览器验收账号准备脚本。

用途：只在本机专属 eoa_test_* 测试库创建临时管理员，供真实浏览器登录；不操作云库。
运行：设置测试 DB_*、APP_ENV=test 与 E2E_PASSWORD 后，
在 backend 下 uv run python tests/prepare_browser.py。
前置：MySQL 已迁移至 head；DB_HOST=127.0.0.1、DB_PORT=13306；密码不放命令行或日志。
数据位于临时容器，验收后随容器停止丢弃；已存在的账号不会被重置密码。
"""

import os

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import make_engine
from app.core.security import hasher, validate_password
from app.models import Enterprise, User


def main():
    settings = get_settings()
    assert settings.app_env == "test" and settings.db_host == "127.0.0.1"
    assert settings.db_port == 13306 and settings.db_name.startswith("eoa_test_")
    password = os.environ["E2E_PASSWORD"]
    validate_password(password)
    engine = make_engine(settings)
    try:
        with Session(engine) as db:
            assert db.scalar(select(User.id).where(User.login_name == "m1_browser")) is None
            enterprise = Enterprise(name="M1 隔离浏览器验收")
            db.add(enterprise)
            db.flush()
            db.add(
                User(
                    enterprise_id=enterprise.id,
                    login_name="m1_browser",
                    password_hash=hasher.hash(password),
                    role="ADMIN",
                    must_change_password=False,
                )
            )
            db.commit()
        print("M1 浏览器验收账号已准备，仅限本机隔离库")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
