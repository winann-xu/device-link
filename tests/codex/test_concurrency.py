# -*- coding: utf-8 -*-
"""并发回归测试（Bug I）：多线程并发写 SQLite 不得报错。"""
import threading

from src.storage.database import init_database, close_connection
from src.storage.repositories import DeviceRepository, HistoryRepository, AlertRepository


def test_concurrent_writes(tmp_path):
    conn = init_database(str(tmp_path / "c.db"))
    try:
        repo = DeviceRepository(conn)
        hist = HistoryRepository(conn)
        alert = AlertRepository(conn)
        did = repo.add_device({"name": "并发设备", "ip_address": "10.1.1.1",
                               "subsystem_name": "并发", "is_enabled": 1})
        errors = []

        def worker(n):
            try:
                for _ in range(30):
                    repo.record_check_result(did, True, 1.0)
                    hist.insert_status(did, "online", 1.0)
                    alert.insert_event({"device_id": did, "event_type": "offline",
                                        "message": "x", "notified_channels": "",
                                        "notify_success": 0})
                    repo.set_device_status(did, "online", 0, 0, 1.0)
            except Exception as e:  # noqa: BLE001
                errors.append(f"{n}: {type(e).__name__}: {e}")

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == [], f"并发写出现错误: {errors[:5]}"
        # 数据完整性：8 线程 × 30 次 = 240 条历史
        rows = hist.query_status_range(did, "2026-01-01", "2099-01-01")
        assert len(rows) >= 240, f"历史行数不足: {len(rows)}"
    finally:
        close_connection()
