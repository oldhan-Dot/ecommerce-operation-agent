"""本机MySQL8.4开发交付冒烟验证脚本。

用途：验证实际日常库版本、API就绪、管理员登录、Worker心跳及5173真实页面。
命令：backend下 uv run python tests/local_delivery_smoke.py。
前置：APP_ENV=dev、127.0.0.1:13306/ecommerce_agent_local，拒绝其他数据库；Edge已安装。
只读取业务数据及建立/退出登录会话，不创建演示资料，不调用模型，不打印凭据；报告写忽略目录.local。
"""

import base64
import json
import os
import subprocess
import time
from uuid import uuid4

import browser_m1
import httpx
from dotenv import dotenv_values
from sqlalchemy import text
from websockets.sync.client import connect

from app.core.config import get_settings
from app.core.db import make_engine


def main():
    settings = get_settings()
    assert settings.app_env == "dev" and settings.db_host == "127.0.0.1"
    assert settings.db_port == 13306 and settings.db_name == "ecommerce_agent_local"
    credentials = dotenv_values(browser_m1.LOCAL / "local-admin.env")
    login, password = credentials["LOCAL_ADMIN_USERNAME"], credentials["LOCAL_ADMIN_PASSWORD"]
    engine = make_engine(settings)
    try:
        with engine.connect() as db:
            version = db.scalar(text("SELECT VERSION()"))
            assert version.startswith("8.4.")
    finally:
        engine.dispose()
    with httpx.Client(
        base_url="http://127.0.0.1:8000/api/v1", headers={"Origin": "http://127.0.0.1:5173"}
    ) as http:
        assert http.get("/health/ready").status_code == 200
        response = http.post("/auth/login", json={"login_name": login, "password": password})
        assert response.status_code == 200, "本机管理员登录失败（未输出响应及凭据）"
        http.headers["X-CSRF-Token"] = response.json()["data"]["csrf_token"]
        for path in ("/stores", "/artifacts", "/ai-tasks", "/audit-events", "/settings/model"):
            assert http.get(path).status_code == 200
        until = time.monotonic() + 25
        while time.monotonic() < until:
            if http.get("/operations/summary").json()["data"]["worker"]["healthy"]:
                break
            time.sleep(0.5)
        else:
            raise AssertionError("本机Worker心跳未就绪")
        assert http.post("/auth/logout").status_code == 204
    suffix = uuid4().hex[:8]
    process = subprocess.Popen(
        [
            "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
            "--headless=new",
            "--disable-gpu",
            "--no-first-run",
            "--edge-skip-compat-layer-relaunch",
            "--remote-debugging-port=19227",
            f"--user-data-dir={browser_m1.LOCAL / ('delivery-' + suffix)}",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    try:
        tab = None
        until = time.monotonic() + 20
        while time.monotonic() < until:
            try:
                tab = next(
                    (
                        t
                        for t in httpx.get("http://127.0.0.1:19227/json").json()
                        if t.get("type") == "page" and t.get("url") == "about:blank"
                    ),
                    None,
                )
                if tab:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.2)
        assert tab
        browser_m1.BASE = "http://127.0.0.1:5173"
        with connect(tab["webSocketDebuggerUrl"], max_size=16 * 1024 * 1024) as socket:
            browser = browser_m1.Browser(socket)
            browser.call(
                "Emulation.setDeviceMetricsOverride",
                {"width": 1366, "height": 768, "deviceScaleFactor": 1, "mobile": False},
            )
            browser.go("/login")
            browser.wait("!!document.querySelector('input[autocomplete=username]')")
            browser.fill("input[autocomplete=username]", login)
            browser.fill("input[autocomplete=current-password]", password)
            browser.click("登录")
            browser.wait("location.pathname==='/'")
            browser.go("/operations")
            browser.wait("document.body.innerText.includes('Worker 正常')")
            image = browser.call("Page.captureScreenshot", {"format": "png"})
            (browser_m1.LOCAL / "local84-delivery.png").write_bytes(base64.b64decode(image["data"]))
            browser.click("退出登录")
            browser.wait("location.pathname==='/login'")
        report = {
            "mysql_version": version,
            "database": "ecommerce_agent_local",
            "api_ready": True,
            "admin_login": True,
            "worker_healthy": True,
            "browser_login_monitor_logout": True,
            "real_model_calls": 0,
            "environment": "本机Docker，非云服务器",
        }
        (browser_m1.LOCAL / "local84-delivery-report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(json.dumps(report, ensure_ascii=True))
    finally:
        try:
            info = httpx.get("http://127.0.0.1:19227/json/version", timeout=3).json()
            with connect(info["webSocketDebuggerUrl"]) as socket:
                socket.send(json.dumps({"id": 1, "method": "Browser.close"}))
        except Exception:
            pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.terminate()


if __name__ == "__main__":
    main()
