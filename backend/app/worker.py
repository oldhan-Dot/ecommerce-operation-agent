"""独立 Worker 入口：当前仅检查环境，尚未实现 AI 业务任务执行。"""

import json
import signal
import threading
from datetime import UTC, datetime

from app.core.config import get_settings
from app.core.db import DatabaseProbe


def main() -> None:
    settings = get_settings()
    probe = DatabaseProbe(settings, worker=True)
    # 使用可中断等待配合退出信号，避免轮询休眠阻碍正常退出。
    stopped = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stopped.set())
    previous = None
    print(json.dumps({"event": "worker_started", "mode": "environment_probe_only"}), flush=True)
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
                            "mode": "environment_probe_only",
                        }
                    ),
                    flush=True,
                )
                previous = result.code
            stopped.wait(settings.worker_poll_seconds)
    finally:
        probe.close()
        print(json.dumps({"event": "worker_stopped"}), flush=True)


if __name__ == "__main__":
    main()
