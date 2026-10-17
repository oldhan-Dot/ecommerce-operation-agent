"""独立 Worker：持久模型调用、商品资料生成、任务恢复及 CSV 预检和确认入库。"""

import json
import signal
import threading
import time
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.ai_tasks import dispatch_task
from app.core.config import get_settings
from app.core.db import DatabaseProbe
from app.import_execution import process_next_confirmation
from app.import_retention import cleanup_imports
from app.imports import process_next_import
from app.model_calls import process_model_call
from app.operations import watch_worker


def main() -> None:
    settings = get_settings()
    probe = DatabaseProbe(settings, worker=True)
    # 使用可中断等待配合退出信号，避免轮询休眠阻碍正常退出。
    stopped = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stopped.set())
    previous = None
    next_cleanup = 0.0
    monitor = None
    print(json.dumps({"event": "worker_started", "mode": "csv_and_model_calls"}), flush=True)
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
                            "mode": "csv_and_model_calls",
                        }
                    ),
                    flush=True,
                )
                previous = result.code
            if result.ready:
                if monitor is None:
                    monitor = threading.Thread(
                        target=watch_worker, args=(probe.engine, stopped, uuid4().hex), daemon=True
                    )
                    monitor.start()
                try:
                    with Session(probe.engine, expire_on_commit=False) as db:
                        if time.monotonic() >= next_cleanup:
                            next_cleanup = (
                                time.monotonic() + settings.import_cleanup_interval_seconds
                            )
                            try:
                                counts = cleanup_imports(db, settings)
                                if any(counts.values()):
                                    print(
                                        json.dumps(
                                            {
                                                "event": "OPS_IMPORT_CLEANUP_FAILED"
                                                if counts["failed"]
                                                else "import_cleanup",
                                                **counts,
                                            }
                                        ),
                                        flush=True,
                                    )
                            except SQLAlchemyError:
                                db.rollback()
                                print(
                                    json.dumps(
                                        {
                                            "event": "OPS_IMPORT_CLEANUP_FAILED",
                                            "code": "SYS_DB_UNAVAILABLE",
                                        }
                                    ),
                                    flush=True,
                                )
                        if process_model_call(db, settings):
                            continue
                        if dispatch_task(db, settings):
                            continue
                        if process_next_confirmation(db, settings) or process_next_import(
                            db, settings
                        ):
                            continue
                except SQLAlchemyError:
                    print(json.dumps({"event": "worker_database_unavailable"}), flush=True)
            stopped.wait(settings.worker_poll_seconds)
    finally:
        stopped.set()
        if monitor:
            monitor.join(timeout=15)
        probe.close()
        print(json.dumps({"event": "worker_stopped"}), flush=True)


if __name__ == "__main__":
    main()
