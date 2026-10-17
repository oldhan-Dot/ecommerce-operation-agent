"""M2 管理员连接测试浏览器验收脚本（Edge CDP）。

用途：真实页面保存配置、阻止未保存参数测试、确认付费、排队、轮询、额度与刷新恢复。
运行：backend 下设置隔离DB_*及E2E_PASSWORD后 uv run python tests/browser_model_calls.py。
前置：APP_ENV=test，本机13306/eoa_test_*已迁移；prepare_browser.py已准备m1_browser账号；
真实API18000、Vite15173已启动，无其他Worker使用该测试库，Edge已安装。
隔离：只有Worker供应商响应替换为受控成功，API和数据库不模拟，不调用真实模型。
截图写.local，账号/密钥不输出；专属浏览器退出后关闭，临时库由调用方整体回收。
"""

import base64
import json
import os
import subprocess
import time
from pathlib import Path
from uuid import uuid4

import httpx
from browser_m1 import LOCAL, Browser
from sqlalchemy.orm import Session
from websockets.sync.client import connect

from app import model_calls
from app.core.config import get_settings
from app.core.db import make_engine
from app.model_provider import ProbeResult


class CheckedBrowser(Browser):
    def wait(self, expression, *, timeout=45):
        try:
            return super().wait(expression, timeout=timeout)
        except Exception:
            screenshot = self.call("Page.captureScreenshot", {"format": "png"})
            (LOCAL / "m2calls-failure.png").write_bytes(base64.b64decode(screenshot["data"]))
            print(json.dumps(self.evaluate("({url:location.href,title:document.title})")))
            raise


def main():
    settings = get_settings()
    assert settings.app_env == "test" and settings.db_host == "127.0.0.1"
    assert settings.db_port == 13306 and settings.db_name.startswith("eoa_test_")
    edge = Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe")
    process = subprocess.Popen(
        [
            str(edge),
            "--headless=new",
            "--disable-gpu",
            "--no-first-run",
            "--remote-debugging-port=19223",
            f"--user-data-dir={LOCAL / ('m2calls-edge-' + uuid4().hex)}",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    engine = make_engine(settings)
    checks, browser = [], None
    try:
        until = time.monotonic() + 20
        tabs = None
        while time.monotonic() < until:
            try:
                tabs = httpx.get("http://127.0.0.1:19223/json").json()
                if any(t.get("type") == "page" and t.get("url") == "about:blank" for t in tabs):
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.2)
        assert tabs
        tab = next(t for t in tabs if t.get("type") == "page" and t.get("url") == "about:blank")
        with connect(tab["webSocketDebuggerUrl"], max_size=16 * 1024 * 1024) as socket:
            browser = CheckedBrowser(socket)
            browser.call("Page.enable")
            browser.call(
                "Emulation.setDeviceMetricsOverride",
                {"width": 1366, "height": 768, "deviceScaleFactor": 1, "mobile": False},
            )
            browser.go("/login")
            browser.wait("!!document.querySelector('input[autocomplete=username]')")
            browser.fill("input[autocomplete=username]", "m1_browser")
            browser.fill("input[autocomplete=current-password]", os.environ["E2E_PASSWORD"])
            browser.click("登录")
            browser.wait("location.pathname !== '/login'")
            browser.go("/settings/model")
            browser.wait("!!document.querySelector('input[autocomplete=new-password]')")
            browser.fill("input[autocomplete=new-password]", "invalid-browser-probe-key")
            browser.wait(
                "[...document.querySelectorAll('button')].some(e => "
                "e.innerText.trim() === '保存配置' && !e.disabled)"
            )
            browser.click("保存配置")
            browser.wait("document.body.innerText.includes('当前配置版本 1')")
            assert browser.evaluate(
                "document.querySelector('input[autocomplete=new-password]').value === ''"
            )
            checks.append("真实管理员保存配置且密钥输入框清空")
            browser.fill("input[placeholder='填写当前账号可用的模型名']", "unsaved-test-model")
            browser.wait(
                "[...document.querySelectorAll('button')].some(e => "
                "e.innerText.trim() === '测试已保存配置' && e.disabled)"
            )
            browser.fill("input[placeholder='填写当前账号可用的模型名']", "deepseek-flash")
            browser.wait(
                "[...document.querySelectorAll('button')].some(e => "
                "e.innerText.trim() === '测试已保存配置' && !e.disabled)"
            )
            checks.append("未保存参数禁止测试")
            browser.click("测试已保存配置")
            browser.wait("document.body.innerText.includes('可能产生模型费用')")
            browser.click("确定")
            browser.wait(
                "document.querySelector('.el-table__body')?.innerText.includes('等待执行')"
            )
            assert browser.evaluate(
                "[...document.querySelectorAll('button')].some(e => "
                "e.innerText.trim() === '测试已保存配置' && e.disabled)"
            )
            checks.append("确认费用后持久排队且阻止重复点击")

            async def controlled(*args):
                return ProbeResult(42, {"total_tokens": 8})

            model_calls.probe = controlled
            with Session(engine, expire_on_commit=False) as db:
                assert model_calls.process_model_call(db, settings)
                assert not model_calls.process_model_call(db, settings)
            browser.wait(
                "document.querySelector('.el-table__body')?.innerText.includes('连接成功')"
            )
            browser.wait("document.body.innerText.includes('已消耗 1 次')")
            checks.append("受控Worker单次执行，真实轮询显示成功和额度")
            browser.go("/settings/model")
            browser.wait(
                "document.querySelector('.el-table__body')?.innerText.includes('连接成功')"
            )
            assert browser.evaluate(
                "!document.body.innerText.includes('invalid-browser-probe-key')"
            )
            checks.append("刷新保留调用记录且无原始密钥")
            screenshot = browser.call(
                "Page.captureScreenshot", {"format": "png", "captureBeyondViewport": True}
            )
            (LOCAL / "m2calls-page.png").write_bytes(base64.b64decode(screenshot["data"]))
            (LOCAL / "m2calls-browser-report.json").write_text(
                json.dumps({"passed": checks}, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(json.dumps({"passed": len(checks), "checks": checks}, ensure_ascii=True))
            browser.call("Browser.close")
            browser = None
    finally:
        engine.dispose()
        if browser:
            try:
                browser.call("Browser.close")
            except Exception:
                pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.terminate()


if __name__ == "__main__":
    main()
