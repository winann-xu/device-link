# -*- coding: utf-8 -*-
"""性能冒烟：200 台设备（150 在线 + 50 离线），验证单轮全量探测耗时与调度稳定性。"""
import time

import pytest

from src.storage.database import init_database, close_connection
from src.storage.repositories import DeviceRepository, HistoryRepository
from src.core.monitor_scheduler import MonitorScheduler


def test_perf_200_devices(tmp_path):
    conn = init_database(str(tmp_path / "perf.db"))
    try:
        repo = DeviceRepository(conn)
        hist = HistoryRepository(conn)
        devices = []
        for i in range(150):
            devices.append({"name": f"在线-{i}", "ip_address": "127.0.0.1",
                            "subsystem_name": "在线组", "check_interval_seconds": 30,
                            "timeout_ms": 500, "failure_threshold": 3,
                            "recovery_threshold": 2, "is_enabled": 1})
        for i in range(50):
            devices.append({"name": f"离线-{i}", "ip_address": "192.0.2.1",
                            "subsystem_name": "离线组", "check_interval_seconds": 30,
                            "timeout_ms": 500, "failure_threshold": 3,
                            "recovery_threshold": 2, "is_enabled": 1})
        ok, failed = repo.add_devices_batch(devices)
        assert ok == 200 and failed == []

        config = {"monitor": {"max_workers": 50, "jitter_schedule": False,
                              "default_timeout_ms": 500}}
        scheduler = MonitorScheduler(repo.list_enabled_devices(), config, repo, hist)
        t0 = time.monotonic()
        scheduler.start()
        try:
            time.sleep(35)
            health = scheduler.get_health()
            checked = 0
            for d in repo.list_devices():
                if hist.query_status_range(d["id"], "2026-01-01", "2099-01-01"):
                    checked += 1
            elapsed = time.monotonic() - t0
            print(f"35 秒内已探测设备: {checked}/200, 调度健康: {health}")
            assert checked >= 195, f"应有≥195台设备完成首轮探测，实际 {checked}"
            assert elapsed < 45
        finally:
            scheduler.stop()
    finally:
        close_connection()
