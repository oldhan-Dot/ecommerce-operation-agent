"""M1 真实浏览器验收脚本（Edge CDP，无需额外下载浏览器/Playwright）。

用途：验证登录、店铺录入、CSV 上传/预检/确认、刷新恢复、真实看板指标、退出及匿名重定向。
运行：backend 下设置 E2E_PASSWORD 后 uv run python tests/browser_m1.py。
前置：prepare_browser.py 已完成；隔离 API/Worker 启动，Vite 15173 代理到隔离 API 18000；
测试数据库必须为本机专属临时库，已有 Microsoft Edge，websockets 依赖由 uvicorn[standard] 提供。
脚本只访问 127.0.0.1:15173；账号/密码不写日志，截图与脱敏报告写入被忽略的 .local。
不模拟 API，不伪造接口返回；测试浏览器使用独立用户目录，结束时关闭该测试实例。
"""

import base64
import csv
import json
import os
import subprocess
import time
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import httpx
from websockets.sync.client import connect

from app.daily_data import business_today

ROOT = Path(__file__).resolve().parents[2]
LOCAL = ROOT / ".local"
BASE = "http://127.0.0.1:15173"


class Browser:
    def __init__(self, websocket):
        self.websocket = websocket
        self.sequence = 0

    def call(self, method, params=None):
        self.sequence += 1
        self.websocket.send(
            json.dumps({"id": self.sequence, "method": method, "params": params or {}})
        )
        while True:
            message = json.loads(self.websocket.recv(timeout=30))
            if message.get("id") == self.sequence:
                if "error" in message:
                    raise AssertionError(f"浏览器命令失败：{method}")
                return message.get("result", {})

    def evaluate(self, expression):
        response = self.call(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True},
        )
        if "exceptionDetails" in response:
            raise AssertionError("浏览器页面操作失败；未输出输入内容")
        return response.get("result", {}).get("value")

    def wait(self, expression, *, timeout=45):
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            if self.evaluate(expression):
                return
            time.sleep(0.15)
        raise AssertionError("页面验收等待超时；未输出可能含账号资料的页面正文")

    def go(self, path):
        self.call("Page.navigate", {"url": BASE + path})
        self.wait("document.readyState === 'complete'")

    def click(self, text):
        self.evaluate(
            "(() => { const button = [...document.querySelectorAll('button')]"
            ".find(e => e.getClientRects().length && e.textContent.trim() === "
            + json.dumps(text)
            + "); if (!button) throw Error('button'); button.click(); })()"
        )

    def fill(self, selector, text):
        self.evaluate(
            "(() => { const input = document.querySelector("
            + json.dumps(selector)
            + "); if (!input) throw Error('input'); "
            "Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(input, "
            + json.dumps(text)
            + "); input.dispatchEvent(new Event('input', { bubbles: true })); })()"
        )

    def field(self, label, text):
        self.evaluate(
            "(() => { const field = [...document.querySelectorAll('.el-dialog .el-form-item')]"
            ".find(e => e.querySelector('label')?.textContent.trim() === "
            + json.dumps(label)
            + "); const input = field?.querySelector('input'); if (!input) throw Error('field'); "
            "Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(input, "
            + json.dumps(text)
            + "); input.dispatchEvent(new Event('input', { bubbles: true })); })()"
        )


def main():
    """使用真实服务与专属浏览器执行闭环，错误不会泄漏密码或 Cookie。"""
    password = os.environ["E2E_PASSWORD"]
    edge = (
        Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)"))
        / "Microsoft/Edge/Application/msedge.exe"
    )
    assert edge.is_file(), "未安装 Microsoft Edge"
    LOCAL.mkdir(exist_ok=True)
    suffix = uuid4().hex[:10]
    profile = LOCAL / f"m1-edge-{suffix}"
    code = "E2E-" + suffix
    name = "M1浏览器验收-" + suffix
    raw = LOCAL / f"m1-browser-{suffix}.csv"
    today = business_today()
    with raw.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["business_date", "paid_amount", "paid_orders", "visitors", "refund_amount"]
        )
        for offset in range(13, -1, -1):
            writer.writerow(
                [
                    (today - timedelta(days=offset + 1)).isoformat(),
                    "1000" if offset < 7 else "800",
                    "10",
                    "200",
                    "100",
                ]
            )
    process = subprocess.Popen(
        [
            str(edge),
            "--headless=new",
            "--disable-gpu",
            "--no-first-run",
            "--no-default-browser-check",
            "--remote-debugging-port=19222",
            f"--user-data-dir={profile}",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    browser = None
    checks = []
    try:
        with httpx.Client(trust_env=False, timeout=3) as client:
            for _ in range(100):
                try:
                    tab = client.put("http://127.0.0.1:19222/json/new?about:blank").json()
                    break
                except (httpx.HTTPError, ValueError):
                    time.sleep(0.15)
            else:
                raise AssertionError("测试浏览器调试端口未启动")
            assert client.get(BASE + "/api/v1/health/ready").status_code == 200
        with connect(tab["webSocketDebuggerUrl"], max_size=16 * 1024 * 1024) as socket:
            browser = Browser(socket)
            browser.call(
                "Emulation.setDeviceMetricsOverride",
                {"width": 1366, "height": 768, "deviceScaleFactor": 1, "mobile": False},
            )
            browser.go("/login")
            browser.wait("!!document.querySelector('input[placeholder=\"请输入账号\"]')")
            browser.fill('input[placeholder="请输入账号"]', "m1_browser")
            browser.fill('input[placeholder="请输入密码"]', password)
            browser.click("登录")
            browser.wait("location.pathname === '/' && document.body.innerText.includes('工作台')")
            cookies = browser.call("Network.getCookies", {"urls": [BASE]})["cookies"]
            assert any(c["httpOnly"] for c in cookies)
            checks.append("真实登录及 HttpOnly 会话")
            browser.go("/stores")
            browser.wait(
                "[...document.querySelectorAll('button')]"
                ".some(e => e.textContent.trim() === '新建店铺')"
            )
            browser.click("新建店铺")
            browser.wait("!!document.querySelector('.el-dialog input')")
            browser.field("店铺名称", name)
            browser.field("店铺编码", code)
            browser.field("平台", "手工验收")
            browser.click("保存")
            browser.wait(
                "document.body.innerText.includes("
                + json.dumps(name)
                + ") && ![...document.querySelectorAll('.el-dialog')]"
                ".some(e => e.getClientRects().length)"
            )
            checks.append("店铺表单真实保存")
            browser.go("/imports")
            browser.wait("!!document.querySelector('input[type=file]')")
            browser.evaluate(
                "[...document.querySelectorAll('.el-form-item')]"
                ".find(e => e.querySelector('label')?.textContent.trim() === '目标店铺')"
                ".querySelector('.el-select__wrapper').click()"
            )
            browser.wait(
                "[...document.querySelectorAll('.el-select-dropdown__item')]"
                ".some(e => e.getClientRects().length && e.textContent.includes("
                + json.dumps(name)
                + "))"
            )
            browser.evaluate(
                "[...document.querySelectorAll('.el-select-dropdown__item')]"
                ".find(e => e.getClientRects().length && e.textContent.includes("
                + json.dumps(name)
                + ")).click()"
            )
            document = browser.call("DOM.getDocument")
            node = browser.call(
                "DOM.querySelector",
                {"nodeId": document["root"]["nodeId"], "selector": "input[type=file]"},
            )
            browser.call("DOM.setFileInputFiles", {"nodeId": node["nodeId"], "files": [str(raw)]})
            browser.click("上传并预检")
            browser.wait(
                "document.body.innerText.includes('预检通过') && "
                "[...document.querySelectorAll('button')]"
                ".some(e => e.getClientRects().length && e.textContent.trim() === '确认入库')"
            )
            checks.append("14 行 CSV 真实上传及 Worker 预检")
            browser.click("确认入库")
            browser.wait("!!document.querySelector('.el-message-box__btns')")
            browser.evaluate(
                "[...document.querySelectorAll('.el-message-box__btns button')].at(-1).click()"
            )
            browser.wait(
                "[...document.querySelectorAll('.el-dialog')]"
                ".some(e => e.getClientRects().length && e.innerText.includes('入库成功'))"
            )
            checks.append("确认后 Worker 事务入库")
            browser.go("/imports")
            browser.wait(
                "document.querySelector('.el-table__body tr')?.innerText.includes('入库成功')"
            )
            checks.append("刷新后批次结果保留")
            browser.go("/dashboard")
            browser.wait(
                "document.body.innerText.includes('¥7000.00') && "
                "document.body.innerText.includes('25.00%')"
            )
            browser.wait("!!document.querySelector('canvas')")
            browser.wait("!document.querySelector('.el-loading-mask')")
            assert browser.evaluate("document.body.innerText.includes('周期数据完整')")
            assert browser.evaluate(
                "document.querySelector('.sidebar-bottom')"
                ".getBoundingClientRect().bottom <= innerHeight"
            )
            checks.append("1366×768 导航与账号区域可见")
            # 等待图表首次绘制动画完成，截图不截取加载遮罩或中间帧。
            browser.evaluate("new Promise(resolve => setTimeout(() => resolve(true), 1000))")
            size = browser.call("Page.getLayoutMetrics")["cssContentSize"]
            screenshot = browser.call(
                "Page.captureScreenshot",
                {
                    "format": "png",
                    "captureBeyondViewport": True,
                    "clip": {
                        "x": 0,
                        "y": 0,
                        "width": size["width"],
                        "height": size["height"],
                        "scale": 1,
                    },
                },
            )
            (LOCAL / "m1-dashboard.png").write_bytes(base64.b64decode(screenshot["data"]))
            checks.append("真实看板 7000.00、环比 25.00%、完整性及图表")
            browser.click("退出登录")
            browser.wait("location.pathname === '/login'")
            browser.go("/dashboard")
            browser.wait("location.pathname === '/login'")
            checks.append("退出及匿名页面重定向")
            (LOCAL / "m1-browser-report.json").write_text(
                json.dumps({"passed": checks}, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(json.dumps({"passed": len(checks), "checks": checks}, ensure_ascii=True))
            browser.call("Browser.close")
            browser = None
    finally:
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
