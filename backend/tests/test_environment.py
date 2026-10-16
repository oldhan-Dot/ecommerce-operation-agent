"""环境测试：验证健康检查、密码处理、安全约束与异常脱敏。"""

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.config import Settings
from app.core.db import DatabaseProbe, database_connect_args, database_url
from app.main import create_app


def test_liveness_and_unconfigured_readiness(monkeypatch):
    monkeypatch.delenv("DB_HOST", raising=False)
    settings = Settings(_env_file=None, db_host="")
    with TestClient(create_app(settings)) as client:
        live = client.get("/api/v1/health/live")
        assert live.status_code == 200
        assert live.json()["data"]["status"] == "ok"
        response = client.get("/api/v1/health/ready")
        assert response.status_code == 503
        assert response.json()["data"]["code"] == "DB_NOT_CONFIGURED"
        assert response.headers["X-Request-ID"] == response.json()["request_id"]


def test_database_password_is_not_parsed_as_url_components():
    secret = "sample-p@ss:/#% with spaces"
    settings = Settings(_env_file=None, db_host="db.example", db_password=secret)
    url = database_url(settings)
    assert url.password == secret
    assert url.host == "db.example"
    assert secret not in str(url)
    assert secret not in repr(settings)


def test_plaintext_mode_is_dev_only():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, app_env="prod", db_tls_mode="DISABLED")


def test_verified_tls_requires_existing_ca(tmp_path):
    settings = Settings(
        _env_file=None, db_tls_mode="VERIFY_IDENTITY", db_ssl_ca=str(tmp_path / "missing.pem")
    )
    with pytest.raises(ValueError, match="certificate"):
        database_connect_args(settings)


def test_database_errors_are_redacted(monkeypatch):
    from sqlalchemy.exc import OperationalError

    class FailedEngine:
        def connect(self):
            raise OperationalError("secret SQL", {}, Exception("private password"))

    settings = Settings(_env_file=None, db_host="db.example", db_password="private password")
    probe = DatabaseProbe(settings)
    probe.engine = FailedEngine()
    result = probe.check()
    assert result.code == "DB_UNAVAILABLE"
    assert "private password" not in result.message
