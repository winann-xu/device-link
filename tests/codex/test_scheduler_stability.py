# -*- coding: utf-8 -*-
"""调度器稳定性回归（v1.0.7）：
- 主循环单 tick 提交上限，防止启动/导入后探测惊群
- 主循环提交前读库刷新设备数据，外部编辑下轮生效
- add_device 幂等，重复同步不重置调度时间
- record_check 单事务合并状态写入与历史记录
"""
import time

from src.storage.database import init_database, close_connection
from src.storage.repositories import DeviceRepository, HistoryRepository
from src.core.monitor_scheduler import MonitorScheduler


class FakeExecutor:
    """记录提交任务的假线程池。"""

    def __init__(self):
        self.submitted = []

    def submit(self, fn, device, machine):
        self.submitted.append((device, machine))


def _make_scheduler(tmp_path, n, max_workers=3, cap=3, interval=30, jitter=False):
    conn = init_database(str(tmp_path / "sched.db"))
    repo = DeviceRepository(conn)
    hist = HistoryRepository(conn)
    devices = []
    for i in range(n):
        did = repo.add_device({
            "name": f"D{i}", "ip_address": f"10.0.0.{i}",
            "check_interval_seconds": interval, "is_enabled": 1,
        })
        devices.append(repo.get_device(did))
    config = {
        "monitor": {
            "max_workers": max_workers, "max_submit_per_tick": cap,
            "jitter_schedule": jitter, "default_timeout_ms": 300,
        }
    }
    sched = MonitorScheduler(devices, config, repo, hist)
    sched._executor.shutdown(wait=False, cancel_futures=True)
    sched._executor = FakeExecutor()
    return conn, repo, hist, sched


def _force_all_due(sched):
    for s in sched._schedule.values():
        s['next_check'] = 0.0


def test_tick_submission_cap(tmp_path):
    """单 tick 最多提交 max_submit_per_tick 个任务，后续 tick 继续消化。"""
    conn, repo, hist, sched = _make_scheduler(tmp_path, 10, cap=3)
    try:
        _force_all_due(sched)
        now = time.time()
        sched._do_tick(now)
        assert len(sched._executor.submitted) == 3
        sched._do_tick(now)
        assert len(sched._executor.submitted) == 6
        sched._do_tick(now)
        assert len(sched._executor.submitted) == 9
        sched._do_tick(now)
        assert len(sched._executor.submitted) == 10, "全部设备最终都应被提交"
    finally:
        close_connection()


def test_tick_refreshes_device_from_db(tmp_path):
    """提交前读库刷新：外部编辑 IP 后，下一轮探测即使用新数据。"""
    conn, repo, hist, sched = _make_scheduler(tmp_path, 1, cap=3)
    try:
        did = list(sched._machines)[0]
        _force_all_due(sched)
        sched._do_tick(time.time())
        assert sched._executor.submitted[0][0]['ip_address'] == '10.0.0.0'

        repo.update_device(did, {'ip_address': '10.9.9.9'})
        _force_all_due(sched)
        sched._do_tick(time.time())
        assert sched._executor.submitted[1][0]['ip_address'] == '10.9.9.9'
    finally:
        close_connection()


def test_add_device_idempotent_preserves_schedule(tmp_path):
    """重复 add_device 只刷新缓存，不重建状态机、不重置调度时间。"""
    conn, repo, hist, sched = _make_scheduler(tmp_path, 1, cap=3)
    try:
        did = list(sched._machines)[0]
        before = sched._schedule[did]['next_check']
        sched.add_device(repo.get_device(did))
        assert len(sched._machines) == 1
        assert sched._schedule[did]['next_check'] == before

        new_id = repo.add_device({"name": "new", "ip_address": "10.0.0.99",
                                  "is_enabled": 1})
        sched.add_device(repo.get_device(new_id))
        assert len(sched._machines) == 2
        assert new_id in sched._devices
    finally:
        close_connection()


def test_record_check_single_transaction(tmp_path):
    """record_check 一次调用同时更新设备状态并写入历史。"""
    conn = init_database(str(tmp_path / "rc.db"))
    try:
        repo = DeviceRepository(conn)
        hist = HistoryRepository(conn)
        did = repo.add_device({"name": "rc", "ip_address": "10.0.0.1"})
        assert repo.record_check(did, "online", 0, 0, 3.5, True) is True
        d = repo.get_device(did)
        assert d["status"] == "online"
        assert d["latency_ms"] == 3.5
        rows = hist.query_status_range(did, "2026-01-01", "2099-01-01")
        assert len(rows) == 1
        assert rows[0]["status"] == "online"
        assert rows[0]["latency_ms"] == 3.5
    finally:
        close_connection()
