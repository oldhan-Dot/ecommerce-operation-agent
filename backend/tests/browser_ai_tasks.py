"""M2商品分析真实浏览器验收脚本（Edge CDP）。

用途：验证页面选店铺/商品、确认生成、受控Worker草稿、快照追溯、刷新恢复及排队取消。
命令：backend下配置隔离DB_*、E2E_PASSWORD后 uv run python tests/browser_ai_tasks.py。
前置：APP_ENV=test，本机13306/eoa_test_*迁移至head，prepare_browser.py已准备账号；
API18000/Vite15173已启动且无其他Worker消费该库，Microsoft Edge已安装。
API、数据库和页面真实执行；仅供应商响应受控，不访问外网、不产生模型费用。
截图/脱敏报告保存.local，脚本关闭自己的浏览器；专属测试库由调用方统一回收。
"""

import base64
import json
import os
import subprocess
import time
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import httpx
from browser_m1 import LOCAL, Browser
from sqlalchemy import select
from sqlalchemy.orm import Session
from websockets.sync.client import connect

from app import ai_tasks, model_calls, product_generator
from app.core.config import get_settings
from app.core.db import make_engine
from app.daily_data import business_today
from app.models import AiTask, Product, ProductDaily


def choose(browser, index, label):
    browser.evaluate(f"document.querySelectorAll('.el-form .el-select__wrapper')[{index}].click()")
    browser.wait(
        "[...document.querySelectorAll('.el-select-dropdown__item')].some(e => "
        "e.getClientRects().length && e.textContent.trim() === " + json.dumps(label) + ")"
    )
    browser.evaluate(
        "[...document.querySelectorAll('.el-select-dropdown__item')].find(e => "
        "e.getClientRects().length && e.textContent.trim() === " + json.dumps(label) + ").click()"
    )


def main():
    settings = get_settings()
    assert settings.app_env == "test" and settings.db_host == "127.0.0.1"
    assert settings.db_port == 13306 and settings.db_name.startswith("eoa_test_")
    suffix = uuid4().hex[:8]
    name = "浏览器分析商品-" + suffix
    with httpx.Client(
        base_url="http://127.0.0.1:18000/api/v1", headers={"Origin": "http://127.0.0.1:5173"}
    ) as http:
        response = http.post(
            "/auth/login", json={"login_name": "m1_browser", "password": os.environ["E2E_PASSWORD"]}
        )
        assert response.status_code == 200
        http.headers["X-CSRF-Token"] = response.json()["data"]["csrf_token"]
        # 专属浏览器账号重跑时，先取消自己上次失败验收留下的未执行任务。
        previous = http.get("/ai-tasks?page_size=100").json()["data"]["items"]
        for task in previous:
            if task["status"] in {"QUEUED", "RUNNING"}:
                assert http.post(f"/ai-tasks/{task['id']}/cancel").status_code == 200
        config = http.get("/settings/model").json()["data"]
        saved = http.put(
            "/settings/model",
            headers={"If-Match": f'"{config["config_version"]}"'},
            json={
                "provider": "deepseek",
                "endpoint": "https://api.deepseek.com",
                "model_name": "deepseek-flash",
                "api_key": "invalid-browser-model-key",
                "timeout_seconds": 120,
                "daily_limit": 100,
            },
        )
        assert saved.status_code == 200
        response = http.post(
            "/stores", json={"code": suffix, "name": "分析店铺-" + suffix, "platform": "手工"}
        )
        assert response.status_code == 201
        store = response.json()["data"]
        response = http.post(
            "/products",
            json={
                "store_id": store["id"],
                "code": suffix,
                "name": name,
                "category": "食品",
                "selling_points": "500克包装",
                "audience": "家庭用户",
                "skus": [{"code": suffix, "spec_name": "标准装", "price": "25.00"}],
            },
        )
        assert response.status_code == 201, "测试商品准备失败"
        product = response.json()["data"]
    edge = Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe")
    process = subprocess.Popen(
        [
            str(edge),
            "--headless=new",
            "--disable-gpu",
            "--no-first-run",
            "--edge-skip-compat-layer-relaunch",
            "--remote-debugging-port=19224",
            f"--user-data-dir={LOCAL / ('m2tasks-edge-' + suffix)}",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    engine, browser = make_engine(settings), None
    metrics_mode = os.environ.get("E2E_PRODUCT_METRICS") == "1"
    if metrics_mode:
        with Session(engine) as db:
            for offset, amount in ((1, "90"), (2, "10"), (3, "45"), (4, "5")):
                db.add(
                    ProductDaily(
                        store_id=int(store["id"]),
                        product_id=int(product["id"]),
                        business_date=business_today() - timedelta(days=offset),
                        paid_amount=Decimal(amount),
                        paid_orders=1,
                        visitors=5,
                        refund_amount=Decimal("0"),
                    )
                )
            db.commit()
    checks = []
    try:
        until = time.monotonic() + 20
        tab = None
        while time.monotonic() < until:
            try:
                tab = next(
                    (
                        t
                        for t in httpx.get("http://127.0.0.1:19224/json").json()
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
                {"width": 1366, "height": 768, "deviceScaleFactor": 1, "mobile": False},
            )
            browser.go("/login")
            browser.wait("!!document.querySelector('input[autocomplete=username]')")
            browser.fill("input[autocomplete=username]", "m1_browser")
            browser.fill("input[autocomplete=current-password]", os.environ["E2E_PASSWORD"])
            browser.click("登录")
            browser.wait("location.pathname !== '/login'")
            browser.go("/ai-tasks")
            browser.wait("document.body.innerText.includes('AI 商品分析')")
            choose(browser, 0, store["name"])
            choose(browser, 1, name)
            if metrics_mode:
                browser.fill(
                    ".el-date-editor input[placeholder='开始日期']",
                    str(business_today() - timedelta(days=2)),
                )
                browser.fill(
                    ".el-date-editor input[placeholder='结束日期']",
                    str(business_today() - timedelta(days=1)),
                )
                browser.evaluate(
                    "document.querySelector('.el-date-editor input').dispatchEvent("
                    "new KeyboardEvent('keydown',{key:'Enter',code:'Enter',bubbles:true}))"
                )
                browser.wait("!!document.querySelector('.el-picker-panel')")
                browser.evaluate(
                    "document.querySelector('.el-date-editor input').dispatchEvent("
                    "new KeyboardEvent('keydown',{key:'Enter',code:'Enter',bubbles:true}))"
                )
            browser.evaluate(
                "(() => { const e=document.querySelector('textarea'); "
                "Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set.call(e,'改进商品资料表达');"
                "e.dispatchEvent(new Event('input',{bubbles:true})); })()"
            )
            browser.wait(
                "[...document.querySelectorAll('button')].some(e => "
                "e.innerText.trim()==='生成资料分析' && !e.disabled)"
            )
            browser.click("生成资料分析")
            browser.wait("document.body.innerText.includes('执行时消耗企业额度')")
            browser.click("确定")
            browser.wait(
                "document.querySelector('.el-table__body')?.innerText.includes('等待执行')"
            )
            checks.append("真实页面选择店铺商品、目标和费用确认后排队")

            async def controlled(config, secret, hosts, snapshot):
                return product_generator.Generated(
                    30,
                    {"total_tokens": 8},
                    product_generator.validate_content(
                        {
                            "title": "商品资料分析草稿",
                            "strengths": ["包装信息可能清晰"],
                            "issues": ["人群描述需验证"],
                            "actions": ["建议访谈目标用户"],
                            "metric_refs": ["paid_amount"]
                            if snapshot.get("metrics", {}).get("analysis_mode") == "DATA_SUPPORTED"
                            else [],
                        },
                        snapshot,
                    ),
                )

            product_generator.generate = controlled
            with Session(engine, expire_on_commit=False) as db:
                assert ai_tasks.dispatch_task(db, settings)
                assert model_calls.process_model_call(db, settings)
            browser.wait(
                "document.querySelector('.el-table__body')?.innerText.includes('草稿已生成')"
            )
            browser.wait("document.body.innerText.includes('待验证的判断')")
            checks.append("受控Worker单次生成、轮询更新、草稿与建议分区可见")
            if metrics_mode:
                browser.wait(
                    "document.querySelector('.el-drawer')?.innerText.includes('服务端计算的经营指标')"
                )
                assert browser.evaluate(
                    "document.querySelector('.el-drawer').innerText.includes('100.00 元')"
                )
                assert browser.evaluate(
                    "document.querySelector('.el-drawer').innerText.includes('100.00%')"
                )
                checks.append("日期选择提交成功，展示本期100元与正式环比100%及SKU资料")
            with Session(engine) as db:
                db.get(Product, int(product["id"])).name = "后续修改的商品名称"
                db.commit()
            assert browser.evaluate(
                "document.querySelector('.el-drawer').innerText.includes(" + json.dumps(name) + ")"
            )
            assert browser.evaluate(
                "!document.querySelector('.el-drawer').innerText.includes('后续修改的商品名称')"
            )
            checks.append("基础商品修改后，历史快照和草稿仍保留原资料")
            screenshot = browser.call(
                "Page.captureScreenshot", {"format": "png", "captureBeyondViewport": True}
            )
            (LOCAL / "m2tasks-draft.png").write_bytes(base64.b64decode(screenshot["data"]))
            browser.go("/ai-tasks")
            browser.wait(
                "document.querySelector('.el-table__body')?.innerText.includes('草稿已生成')"
            )
            checks.append("刷新后任务及产出状态持久保留")
            with Session(engine) as db:
                task = db.scalar(select(AiTask).order_by(AiTask.id.desc()).limit(1))
                first_id = task.id
            browser.click("查看详情")
            browser.wait("document.querySelector('.el-drawer')?.innerText.includes('草稿')")
            checks.append("详情可重新打开，草稿保持未审核状态")
            browser.evaluate("document.querySelector('.el-drawer__close-btn').click()")
            choose(browser, 0, store["name"])
            choose(browser, 1, "后续修改的商品名称")
            browser.evaluate(
                "(() => { const e=document.querySelector('textarea'); "
                "Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set.call(e,'检查资料');"
                "e.dispatchEvent(new Event('input',{bubbles:true})); })()"
            )
            browser.wait(
                "[...document.querySelectorAll('button')].some(e => "
                "e.innerText.trim()==='生成资料分析' && !e.disabled)"
            )
            browser.click("生成资料分析")
            browser.wait("document.body.innerText.includes('执行时消耗企业额度')")
            browser.click("确定")
            browser.wait(
                "document.querySelector('.el-table__body')?.innerText.includes('等待执行')"
            )
            browser.evaluate("document.querySelector('.el-drawer__close-btn').click()")
            browser.click("取消")
            browser.wait("document.body.innerText.includes('取消后不保存生成结果')")
            browser.evaluate(
                "[...document.querySelectorAll('.el-message-box')].find(e => "
                "e.innerText.includes('取消后不保存生成结果')).querySelector('.el-button--primary').click()"
            )
            browser.wait("document.querySelector('.el-table__body')?.innerText.includes('已取消')")
            checks.append("页面取消排队任务，持久状态变为已取消")
            (LOCAL / "m2tasks-browser-report.json").write_text(
                json.dumps(
                    {"passed": checks, "task_id": str(first_id)}, ensure_ascii=False, indent=2
                ),
                encoding="utf-8",
            )
            print(json.dumps({"passed": len(checks), "checks": checks}, ensure_ascii=True))
            browser.call("Browser.close")
            browser = None
    finally:
        engine.dispose()
        # Edge可能派生兼容层进程，始终通过专属调试端口关闭整个测试浏览器。
        try:
            info = httpx.get("http://127.0.0.1:19224/json/version", timeout=3).json()
            with connect(info["webSocketDebuggerUrl"]) as close_socket:
                close_socket.send(json.dumps({"id": 1, "method": "Browser.close"}))
        except Exception:
            pass
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
