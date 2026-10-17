"""备用本机 MySQL 验收库初始化脚本。

用途：Docker 不可用时，为全新独立 mysqld 设置临时账号和测试库，拒绝用户已有数据目录。
运行：隔离 DB_*、APP_ENV=test、E2E_ROOT_PASSWORD、E2E_MYSQL_DATA_DIR 就绪后，
在 backend 下 uv run python tests/prepare_local_mysql.py。
前置：mysqld 仅监听 127.0.0.1:13306，使用带 eoa-m1-mysql- 前缀及专属标记的临时目录；
刚完成 --initialize-insecure，初始 root 无密码；完成后设置临时 root 密码，凭据不输出。
该脚本不操作云数据库；原有 MySQL80 服务和数据目录不使用。
"""

import os
import re
import tempfile
from pathlib import Path

import pymysql

from app.core.config import get_settings


def main():
    settings = get_settings()
    assert settings.app_env == "test" and settings.db_host == "127.0.0.1"
    assert settings.db_port == 13306 and settings.db_user == "eoa_test"
    assert re.fullmatch(r"eoa_test_[a-z0-9_]+", settings.db_name)
    # 旧版 Windows mysqld 不支持部分中文路径时，使用带专属前缀和标记的 ASCII 临时目录。
    expected = Path(os.environ["E2E_MYSQL_DATA_DIR"]).resolve()
    assert expected.parent == Path(tempfile.gettempdir()).resolve()
    assert expected.name.startswith("eoa-m1-mysql-") and (expected / ".eoa-m1-acceptance").is_file()
    with pymysql.connect(host="127.0.0.1", port=13306, user="root", password="") as db:
        with db.cursor() as cursor:
            cursor.execute("SELECT @@datadir, VERSION()")
            directory, version = cursor.fetchone()
            assert Path(directory).resolve() == expected.resolve(), "拒绝操作其他数据库目录"
            cursor.execute(
                f"CREATE DATABASE IF NOT EXISTS `{settings.db_name}` "
                "CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci"
            )
            cursor.execute(
                "CREATE USER IF NOT EXISTS 'eoa_test'@'%%' IDENTIFIED BY %s",
                (settings.db_password.get_secret_value(),),
            )
            cursor.execute(f"GRANT ALL PRIVILEGES ON `{settings.db_name}`.* TO 'eoa_test'@'%'")
            cursor.execute(
                "ALTER USER 'root'@'localhost' IDENTIFIED BY %s", (os.environ["E2E_ROOT_PASSWORD"],)
            )
        db.commit()
    print(f"本机独立验收库已准备，MySQL {version}；未输出凭据")


if __name__ == "__main__":
    main()
