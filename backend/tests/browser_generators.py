"""五类生成器真实页面验收脚本（Edge CDP）。

用途：逐类验证专属输入、费用确认、排队、受控Worker生成、真实轮询与类型专属草稿展示。
命令：隔离DB_*、E2E_PASSWORD配置后，backend下 uv run python tests/browser_generators.py。
前置：APP_ENV=test，本机13306/eoa_test_*迁移至head，m1_browser账号已准备；
真实API18000/Vite15173运行，无其他Worker消费该库，uv sync --dev和Edge已安装。
只替换供应商响应；API、页面、权限及数据库真实执行，不访问真实模型、不产生费用。
结束关闭自己的测试浏览器；截图和脱敏结果写.local，不保存Key/Cookie/登录密码。
"""

import base64
import json
import os
import subprocess
import time
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import httpx
from browser_ai_tasks import choose
from browser_m1 import LOCAL, Browser
from sqlalchemy.orm import Session
from test_generators import IDEA, SHOT, ad_output
from websockets.sync.client import connect

from app import ai_tasks, generators, model_calls
from app.core.config import get_settings
from app.core.db import make_engine
from app.daily_data import business_today
from app.models import StoreDaily


def field(browser, label, value):
    browser.evaluate(
        "(() => { const row=[...document.querySelectorAll('.el-form-item')]"
        ".find(e => e.querySelector('label')?.innerText.trim().startsWith("
        + json.dumps(label)
        + "));"
        "const e=row.querySelector('input'); "
        "Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value')"
        ".set.call(e,"
        + json.dumps(value)
        + ");e.dispatchEvent(new Event('input',{bubbles:true})); })()"
    )


def set_period(browser, start, end):
    browser.fill(".el-date-editor input[placeholder='开始日期']", str(start))
    browser.fill(".el-date-editor input[placeholder='结束日期']", str(end))
    key = (
        "document.querySelector('.el-date-editor input').dispatchEvent("
        "new KeyboardEvent('keydown',{key:'Enter',code:'Enter',bubbles:true}))"
    )
    browser.evaluate(key)
    browser.wait("!!document.querySelector('.el-picker-panel')")
    browser.evaluate(key)


def main():
    settings = get_settings()
    assert settings.app_env == "test" and settings.db_host == "127.0.0.1"
    assert settings.db_port == 13306 and settings.db_name.startswith("eoa_test_")
    suffix = uuid4().hex[:8]
    today = business_today()
    with httpx.Client(
        base_url="http://127.0.0.1:18000/api/v1", headers={"Origin": "http://127.0.0.1:5173"}
    ) as http:
        response = http.post(
            "/auth/login", json={"login_name": "m1_browser", "password": os.environ["E2E_PASSWORD"]}
        )
        assert response.status_code == 200
        http.headers["X-CSRF-Token"] = response.json()["data"]["csrf_token"]
        for task in http.get("/ai-tasks?page_size=100").json()["data"]["items"]:
            if task["status"] in {"QUEUED", "RUNNING"}:
                assert http.post(f"/ai-tasks/{task['id']}/cancel").status_code == 200
        store = http.post(
            "/stores", json={"code": suffix, "name": "生成店铺-" + suffix, "platform": "手工"}
        ).json()["data"]
        product = http.post(
            "/products",
            json={
                "store_id": store["id"],
                "code": suffix,
                "name": "生成商品-" + suffix,
                "category": "食品",
                "selling_points": "包装说明",
                "skus": [{"code": suffix, "spec_name": "标准装", "price": "25.00"}],
            },
        ).json()["data"]
        competitor = http.post(
            "/competitors",
            json={
                "store_id": store["id"],
                "code": suffix,
                "name": "对比竞品-" + suffix,
                "platform": "手工",
                "spec": "标准装",
                "price": "30.00",
                "source_note": "手工录入测试来源",
                "collected_at": str(today),
            },
        ).json()["data"]
    engine = make_engine(settings)
    with Session(engine) as db:
        for offset, amount in ((1, "100"), (2, "50")):
            db.add(
                StoreDaily(
                    store_id=int(store["id"]),
                    business_date=today - timedelta(days=offset),
                    paid_amount=Decimal(amount),
                    paid_orders=1,
                    visitors=5,
                    refund_amount=Decimal("0"),
                )
            )
        db.commit()

    async def controlled(config, secret, hosts, snapshot):
        kind = snapshot["data_copy"]["task_type"]
        params = snapshot["input_params"]
        outputs = {
            "COMPETITOR_ANALYSIS": {
                "title": "竞品分析草稿",
                "comparison_refs": ["C1"],
                "opportunities": ["建议验证差异表达"],
                "risks": ["来源需人工复核"],
            },
            "THEME_IDEAS": {
                "title": "主题创意草稿",
                "ideas": [{**IDEA, "name": f"主题{i + 1}"} for i in range(params.get("count", 1))],
            },
            "VIDEO_SCRIPT": {
                "title": "视频脚本草稿",
                "shots": [{**SHOT, "end": params.get("duration_seconds", 30)}],
                "closing": "了解商品",
            },
            "AD_STRATEGY": ad_output([{"channel": "搜索", "amount": "100"}]),
            "BUSINESS_REVIEW": {
                "title": "经营复盘草稿",
                "metric_refs": ["paid_amount"],
                "anomalies": [{"metric_ref": "paid_amount", "reasoning": "建议核对金额表现"}],
                "assumptions": ["原因仍待验证"],
                "next_actions": ["建议核对口径"],
            },
        }
        return generators.Generated(10, {}, generators.validate_output(outputs[kind], snapshot))

    generators.generate = controlled
    edge = "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"
    process = subprocess.Popen(
        [
            edge,
            "--headless=new",
            "--disable-gpu",
            "--no-first-run",
            "--edge-skip-compat-layer-relaunch",
            "--remote-debugging-port=19225",
            f"--user-data-dir={LOCAL / ('five-generators-' + suffix)}",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
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
                        for t in httpx.get("http://127.0.0.1:19225/json").json()
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
            browser.go("/login")
            browser.wait("!!document.querySelector('input[autocomplete=username]')")
            browser.fill("input[autocomplete=username]", "m1_browser")
            browser.fill("input[autocomplete=current-password]", os.environ["E2E_PASSWORD"])
            browser.click("登录")
            browser.wait("location.pathname!='/login'")
            cases = [
                ("竞品分析", "COMPETITOR_ANALYSIS", "竞品来源与对比"),
                ("主题创意", "THEME_IDEAS", "主题创意"),
                ("视频脚本", "VIDEO_SCRIPT", "视频分镜脚本"),
                ("投放建议", "AD_STRATEGY", "预算分配建议"),
                ("经营复盘", "BUSINESS_REVIEW", "待核实的经营异常"),
            ]
            for label, kind, section in cases:
                browser.go("/ai-tasks")
                browser.wait("document.body.innerText.includes('AI 运营工作台')")
                browser.evaluate(
                    "[...document.querySelectorAll('.el-radio-button')].find(e=>e.innerText.trim()==="
                    + json.dumps(label)
                    + ").click()"
                )
                choose(browser, 0, store["name"])
                if kind != "BUSINESS_REVIEW":
                    choose(browser, 1, product["name"])
                    browser.evaluate("document.querySelector('h1').click()")
                if kind == "COMPETITOR_ANALYSIS":
                    choose(browser, 2, competitor["name"])
                    browser.evaluate("document.querySelector('h1').click()")
                else:
                    browser.evaluate(
                        "(() => {const e=document.querySelector('textarea');"
                        "Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value')"
                        ".set.call(e,'生成运营草稿');"
                        "e.dispatchEvent(new Event('input',{bubbles:true}));})()"
                    )
                if kind in {"THEME_IDEAS", "VIDEO_SCRIPT"}:
                    field(browser, "目标人群", "家庭用户")
                if kind == "AD_STRATEGY":
                    field(browser, "总预算", "100")
                    choose(browser, 2, "搜索")
                    browser.evaluate("document.querySelector('h1').click()")
                    set_period(browser, today, today + timedelta(days=6))
                if kind == "BUSINESS_REVIEW":
                    set_period(browser, today - timedelta(days=1), today - timedelta(days=1))
                button = "生成" + label
                browser.wait(
                    "[...document.querySelectorAll('button')].some(e=>e.innerText.trim()==="
                    + json.dumps(button)
                    + "&&!e.disabled)"
                )
                browser.click(button)
                browser.wait("document.body.innerText.includes('执行时消耗企业额度')")
                browser.click("确定")
                browser.wait(
                    "document.querySelector('.el-table__body')?.innerText.includes('等待执行')"
                )
                with Session(engine, expire_on_commit=False) as db:
                    assert ai_tasks.dispatch_task(db, settings)
                    assert model_calls.process_model_call(db, settings)
                browser.wait(
                    "document.querySelector('.el-drawer')?.innerText.includes("
                    + json.dumps(section)
                    + ")"
                )
                screenshot = browser.call(
                    "Page.captureScreenshot", {"format": "png", "captureBeyondViewport": True}
                )
                (LOCAL / f"{kind.lower()}-page.png").write_bytes(
                    base64.b64decode(screenshot["data"])
                )
                checks.append(label + "：专属表单→持久任务→草稿展示")
            (LOCAL / "five-generators-report.json").write_text(
                json.dumps({"passed": checks}, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(json.dumps({"passed": len(checks), "checks": checks}, ensure_ascii=True))
            browser.call("Browser.close")
    finally:
        engine.dispose()
        try:
            info = httpx.get("http://127.0.0.1:19225/json/version", timeout=3).json()
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
