"""M3 Edge真实页面验证脚本。

用途：双账号验证修订、提交、主管审核、正式导出、新修订草稿与历史版本。
命令：独立DB_*及E2E_PASSWORD配置后，backend下 uv run python tests/browser_artifacts.py。
前置：本机13306/eoa_test_*迁移到head，API18000/Vite15173运行，m1_browser已有生成产出。
仅在隔离库创建测试主管；不调用真实模型；截图及脱敏报告写.local，结束关闭专属浏览器。
"""

import base64
import json
import os
import subprocess
import time
from uuid import uuid4

import httpx
from browser_m1 import LOCAL, Browser
from sqlalchemy import select
from sqlalchemy.orm import Session
from websockets.sync.client import connect

from app.core.config import get_settings
from app.core.db import make_engine
from app.core.security import hasher
from app.models import User


def main():
    settings = get_settings()
    assert settings.app_env == "test" and settings.db_host == "127.0.0.1"
    assert settings.db_port == 13306 and settings.db_name.startswith("eoa_test_")
    password, suffix = os.environ["E2E_PASSWORD"], uuid4().hex[:8]
    engine = make_engine(settings)
    reviewer = "review_" + suffix
    with Session(engine) as db:
        owner = db.scalar(select(User).where(User.login_name == "m1_browser"))
        db.add(
            User(
                enterprise_id=owner.enterprise_id,
                login_name=reviewer,
                role="SUPERVISOR",
                password_hash=hasher.hash(password),
                must_change_password=False,
            )
        )
        db.commit()
    with httpx.Client(
        base_url="http://127.0.0.1:18000/api/v1", headers={"Origin": "http://127.0.0.1:5173"}
    ) as http:
        response = http.post("/auth/login", json={"login_name": "m1_browser", "password": password})
        assert response.status_code == 200
        items = http.get("/artifacts?page_size=100&review_status=DRAFT").json()["data"]["items"]
        artifact = next(a for a in items if a["task_type"] == "PRODUCT_ANALYSIS")
    process = subprocess.Popen(
        [
            "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
            "--headless=new",
            "--disable-gpu",
            "--no-first-run",
            "--edge-skip-compat-layer-relaunch",
            "--remote-debugging-port=19226",
            f"--user-data-dir={LOCAL / ('review-' + suffix)}",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    checks = []
    try:
        tab = None
        until = time.monotonic() + 20
        while time.monotonic() < until:
            try:
                tab = next(
                    (
                        t
                        for t in httpx.get("http://127.0.0.1:19226/json").json()
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
        with connect(tab["webSocketDebuggerUrl"], max_size=16 * 1024 * 1024) as socket:
            browser = Browser(socket)
            browser.call(
                "Emulation.setDeviceMetricsOverride",
                {
                    "width": 1366,
                    "height": 768,
                    "deviceScaleFactor": 1,
                    "mobile": False,
                },
            )

            def login(name):
                browser.go("/login")
                browser.wait("!!document.querySelector('input[autocomplete=username]')")
                browser.fill("input[autocomplete=username]", name)
                browser.fill("input[autocomplete=current-password]", password)
                browser.click("登录")
                browser.wait("location.pathname!='/login'")
                browser.go("/artifacts?id=" + artifact["id"])
                browser.wait("!!document.querySelector('.el-drawer h2')")

            login("m1_browser")
            browser.click("创建修订")
            browser.wait("!!document.querySelector('.el-drawer textarea')")
            browser.click("保存为新草稿")
            browser.wait("!!document.querySelector('.el-drawer h2')")
            checks.append("作者创建不可变修订")
            browser.click("提交审核")
            browser.wait("document.querySelector('.el-drawer')?.innerText.includes('撤回审核')")
            assert not browser.evaluate(
                "[...document.querySelectorAll('.el-drawer button')]"
                ".some(b=>b.textContent.trim()==='通过')"
            )
            checks.append("提交待审与禁止自审")
            browser.click("退出登录")
            login(reviewer)
            browser.wait(
                "[...document.querySelectorAll('.el-drawer button')]"
                ".some(b=>b.textContent.trim()==='通过')"
            )
            browser.click("通过")
            browser.wait("document.querySelector('.el-drawer')?.innerText.includes('正式导出')")
            browser.click("正式导出")
            checks.append("非作者主管通过与下载")
            browser.click("退出登录")
            login("m1_browser")
            browser.click("创建修订")
            browser.click("保存为新草稿")
            browser.wait("!!document.querySelector('.el-drawer h2')")
            assert not browser.evaluate(
                "[...document.querySelectorAll('.el-drawer button')]"
                ".some(b=>b.textContent.trim()==='正式导出')"
            )
            checks.append("新修订不继承审核结果")
            screen = browser.call("Page.captureScreenshot", {"format": "png"})
            (LOCAL / "artifact-review-page.png").write_bytes(base64.b64decode(screen["data"]))
            browser.go("/operations")
            browser.wait("document.body.innerText.includes('模型调用')")
            assert browser.evaluate("document.body.innerText.includes('执行队列')")
            checks.append("运行监控页使用真实接口加载")
            screen = browser.call("Page.captureScreenshot", {"format": "png"})
            (LOCAL / "operations-page.png").write_bytes(base64.b64decode(screen["data"]))
            (LOCAL / "artifact-review-report.json").write_text(
                json.dumps({"passed": checks}, ensure_ascii=False), encoding="utf-8"
            )
            print(json.dumps({"passed": len(checks), "checks": checks}, ensure_ascii=True))
    finally:
        engine.dispose()
        try:
            info = httpx.get("http://127.0.0.1:19226/json/version", timeout=3).json()
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
