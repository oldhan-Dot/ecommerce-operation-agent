"""独立 Worker：数据库环境检查及持久 CSV 预检，尚不处理 AI 任务。"""

import json
import signal
import threading
from datetime import UTC, datetime

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import DatabaseProbe
from app.imports import process_next_import


def main() -> None:
    settings = get_settings()
    probe = DatabaseProbe(settings, worker=True)
    # 使用可中断等待配合退出信号，避免轮询休眠阻碍正常退出。
    stopped = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stopped.set())
    previous = None
    print(json.dumps({"event": "worker_started", "mode": "import_preview"}), flush=True)
    try:
        while not stopped.is_set():
            result = probe.check()
            # 仅在状态变化时写日志，避免网络故障期间反复刷屏。
            if previous != result.code:
                print(
                    json.dumps(
                        {
                            "event": "worker_database_status",
                            "time": datetime.now(UTC).isoformat(),
                            "code": result.code,
                            "ready": result.ready,
                            "mode": "import_preview",
                        }
                    ),
                    flush=True,
                )
                previous = result.code
            if result.ready:
                try:
                    with Session(probe.engine, expire_on_commit=False) as db:
                        if process_next_import(db, settings):
                            continue
                except SQLAlchemyError:
                    print(json.dumps({"event": "import_preview_database_unavailable"}), flush=True)
            stopped.wait(settings.worker_poll_seconds)
    finally:
        probe.close()
        print(json.dumps({"event": "worker_stopped"}), flush=True)


if __name__ == "__main__":
    main()
