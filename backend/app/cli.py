"""本机诊断命令：检查数据库连接和环境迁移状态。"""

import argparse
import json
from dataclasses import asdict
from getpass import getpass
from uuid import uuid4

from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import DatabaseProbe, make_engine
from app.core.security import hasher, validate_password
from app.models import AuditEvent, Enterprise, User


def create_admin(username: str, enterprise_name: str) -> int:
    """交互创建管理员；密码不接受命令行参数、不输出，也没有默认值。"""
    from app.schemas import Login

    try:
        Login(login_name=username, password="validate-name")
        password = getpass("管理员密码（12—64字符，至少两类字符）: ")
        validate_password(password)
        if password != getpass("再次输入密码: "):
            print("两次密码不一致，未创建账号")
            return 1
        engine = make_engine(get_settings())
        try:
            with engine.connect() as connection:
                if connection.scalar(text("SELECT GET_LOCK('eoa.bootstrap', 10)")) != 1:
                    print("初始化正在执行，请稍后重试")
                    return 1
                connection.commit()
                try:
                    with Session(connection, expire_on_commit=False) as db:
                        enterprise = db.scalar(select(Enterprise).order_by(Enterprise.id).limit(1))
                        if not enterprise:
                            enterprise = Enterprise(name=enterprise_name)
                            db.add(enterprise)
                            db.flush()
                        if db.scalar(
                            select(User.id).where(
                                User.enterprise_id == enterprise.id, User.login_name == username
                            )
                        ):
                            print("账号已存在，未修改任何密码")
                            return 1
                        user = User(
                            enterprise_id=enterprise.id,
                            login_name=username,
                            password_hash=hasher.hash(password),
                            role="ADMIN",
                            must_change_password=False,
                        )
                        db.add(user)
                        db.flush()
                        db.add(
                            AuditEvent(
                                enterprise_id=enterprise.id,
                                actor_id=user.id,
                                action="ADMIN_BOOTSTRAPPED",
                                target_type="users",
                                target_id=str(user.id),
                                request_id=uuid4().hex,
                                result="SUCCEEDED",
                                redacted_diff={},
                            )
                        )
                        db.commit()
                        print("管理员创建成功；请用刚设置的账号密码登录")
                finally:
                    connection.execute(text("SELECT RELEASE_LOCK('eoa.bootstrap')"))
        finally:
            engine.dispose()
        return 0
    except (ValueError, SQLAlchemyError):
        print("创建失败，请核对账号、密码要求、数据库连接及迁移状态；未输出敏感配置")
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Environment diagnostics (no credentials printed)")
    parser.add_argument("command", choices=["check-db", "create-admin"])
    parser.add_argument("--connection-only", action="store_true")
    parser.add_argument("--username", default="admin")
    parser.add_argument("--enterprise-name", default="我的企业")
    args = parser.parse_args()
    if args.command == "create-admin":
        return create_admin(args.username, args.enterprise_name)
    probe = DatabaseProbe(get_settings())
    try:
        # 只检查连接时不要求迁移已完成，用于首次初始化前的连通性验证。
        result = probe.check(require_migration=not args.connection_only)
        # 只打印脱敏状态，不输出配置对象、密码或原始驱动异常。
        print(json.dumps(asdict(result), ensure_ascii=False))
        return 0 if result.ready else 1
    finally:
        probe.close()


if __name__ == "__main__":
    raise SystemExit(main())
