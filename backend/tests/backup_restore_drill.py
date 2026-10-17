"""本机隔离库逻辑备份恢复演练脚本。

用途：用MySQL原生工具备份并恢复到全新临时库，逐表比对行数/校验值、迁移与模型密钥解密。
命令：隔离DB_*、E2E_ROOT_PASSWORD配置后 uv run python tests/backup_restore_drill.py。
前置：APP_ENV=test，本机13306/eoa_test_*，无并发写入；MySQL8原生客户端已安装。
仅创建并删除本脚本随机命名的eoa_test_restore_*库，不覆盖源库；凭据不进参数/输出。
SQL临时文件位于忽略目录.local，finally删除；这是受控本机演练，不替代云端加密异地备份。
"""

import json
import os
import re
import subprocess
from pathlib import Path
from time import monotonic
from uuid import uuid4

import pymysql
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import make_engine
from app.model_settings import decrypt_secret
from app.models import ModelConfig, User


def main():
    settings = get_settings()
    assert settings.app_env == "test" and settings.db_host == "127.0.0.1"
    assert settings.db_port == 13306 and re.fullmatch(r"eoa_test_[a-z0-9_]+", settings.db_name)
    target = "eoa_test_restore_" + uuid4().hex
    assert target != settings.db_name
    local = Path(__file__).resolve().parents[2] / ".local"
    dump = local / (target + ".sql")
    binaries = Path(os.environ.get("MYSQL_BIN", "C:/Program Files/MySQL/MySQL Server 8.0/bin"))
    env = {**os.environ, "MYSQL_PWD": os.environ["E2E_ROOT_PASSWORD"]}
    common = ["--host=127.0.0.1", "--port=13306", "--user=root", "--default-character-set=utf8mb4"]
    started = monotonic()
    root = pymysql.connect(host="127.0.0.1", port=13306, user="root", password=env["MYSQL_PWD"])
    restored = None
    try:
        subprocess.run(
            [
                str(binaries / "mysqldump.exe"),
                *common,
                "--single-transaction",
                "--set-gtid-purged=OFF",
                "--no-tablespaces",
                "--hex-blob",
                f"--result-file={dump}",
                settings.db_name,
            ],
            env=env,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        with root.cursor() as cursor:
            cursor.execute(f"CREATE DATABASE `{target}` CHARACTER SET utf8mb4")
        with dump.open("rb") as source:
            subprocess.run(
                [str(binaries / "mysql.exe"), *common, target],
                stdin=source,
                env=env,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
        comparisons = []
        with root.cursor() as cursor:
            cursor.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema=%s",
                (settings.db_name,),
            )
            tables = [r[0] for r in cursor.fetchall()]
            for table in tables:
                assert re.fullmatch(r"[a-z0-9_]+", table)
                for database in (settings.db_name, target):
                    cursor.execute(f"SELECT COUNT(*) FROM `{database}`.`{table}`")
                    comparisons.append(cursor.fetchone()[0])
                assert comparisons[-1] == comparisons[-2], "恢复后行数不一致"
                cursor.execute(
                    f"CHECKSUM TABLE `{settings.db_name}`.`{table}`, `{target}`.`{table}` EXTENDED"
                )
                checks = cursor.fetchall()
                assert checks[0][1] is not None and checks[0][1] == checks[1][1], (
                    "恢复后校验值不一致"
                )
            cursor.execute(f"SELECT version_num FROM `{target}`.alembic_version")
            revision = cursor.fetchone()[0]
        restored = make_engine(
            settings.model_copy(
                update={
                    "db_name": target,
                    "db_user": "root",
                    "db_password": SecretStr(env["MYSQL_PWD"]),
                }
            )
        )
        with Session(restored) as db:
            owner = db.scalar(select(User).where(User.login_name == "m1_browser"))
            config = db.scalar(
                select(ModelConfig)
                .where(ModelConfig.enterprise_id == owner.enterprise_id)
                .order_by(ModelConfig.config_version.desc())
            )
            assert config and decrypt_secret(config, settings)
        report = {
            "tables_verified": len(tables),
            "revision": revision,
            "model_secret_decrypted": True,
            "duration_seconds": round(monotonic() - started, 2),
            "scope": "本机隔离MySQL逻辑恢复；非云端演练",
        }
        (local / "backup-restore-report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(json.dumps(report, ensure_ascii=True))
    finally:
        if restored:
            restored.dispose()
        try:
            with root.cursor() as cursor:
                cursor.execute(f"DROP DATABASE IF EXISTS `{target}`")
        finally:
            root.close()
            dump.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
