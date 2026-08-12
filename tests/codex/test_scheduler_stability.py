# -*- coding: utf-8 -*-
"""调度器稳定性回归（v1.0.7 / v1.0.7.1）：
- 主循环单 tick 提交上限，防止启动/导入后探测惊群
- 主循环提交前读库刷新设备数据，外部编辑下轮生效
- add_device 幂等，重复同步不重置调度时间
- record_check 单事务合并状态写入与历史记录
- 写失败自动回滚（不遗留持锁事务导致 database is locked）
- 缓存先行：落库失败时快照仍更新，仪表盘/状态栏一致
- FK 失败（设备已删除）自动移出调度器
- 批量阈值更新单事务完成
"""
import sqlite3
import time
from unittest.mock import patch

import pytest

from src.core.detection_chain import ProbeOutcome
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


def test_write_failure_rolls_back_transaction(tmp_path):
    """FK 失败后必须回滚：后续写立即成功，不再 database is locked。"""
    conn = init_database(str(tmp_path / "rb.db"))
    try:
        repo = DeviceRepository(conn)
        with pytest.raises(sqlite3.IntegrityError):
            repo.record_check(999999, "online", 0, 0, 1.0, True)
        did = repo.add_device({"name": "rb", "ip_address": "10.0.0.1"})
        t0 = time.monotonic()
        assert repo.record_check(did, "online", 0, 0, 1.0, True) is True
        assert time.monotonic() - t0 < 2, "回滚后写不应再被旧事务阻塞"
    finally:
        close_connection()


def test_apply_global_thresholds_batch(tmp_path):
    """批量阈值更新：单事务一次更新全部设备。"""
    conn = init_database(str(tmp_path / "bt.db"))
    try:
        repo = DeviceRepository(conn)
        for i in range(3):
            repo.add_device({"name": f"d{i}", "ip_address": f"10.0.0.{i}"})
        n = repo.apply_global_thresholds_to_db(5, 3)
        assert n == 3
        for d in repo.list_devices():
            assert d["failure_threshold"] == 5
            assert d["recovery_threshold"] == 3
    finally:
        close_connection()


class _LockedRepo:
    """模拟落库失败（database is locked）。"""

    def __init__(self, real_repo):
        self._real = real_repo

    def get_device(self, did):
        return self._real.get_device(did)

    def record_check(self, *args, **kwargs):
        raise sqlite3.OperationalError("database is locked")


def test_cache_updated_even_if_db_write_fails(tmp_path):
    """缓存先行：落库失败时快照仍更新，UI 状态不因 DB 错误而滞后。"""
    conn, repo, hist, sched = _make_scheduler(tmp_path, 1, cap=3)
    try:
        did = list(sched._machines)[0]
        device = repo.get_device(did)
        machine = sched._machines[did]
        sched._device_repo = _LockedRepo(repo)
        with patch("src.core.monitor_scheduler.DetectionChain") as MC:
            MC.return_value.probe.return_value = ProbeOutcome(
                is_online=True, failure_should_count=False, latency_ms=1.0
            )
            sched._probe_and_process(device, machine)
        assert sched._status_cache[did] == "online", "落库失败也应更新快照"
    finally:
        close_connection()


class _FkRepo:
    """模拟设备已被删除（FOREIGN KEY）。"""

    def __init__(self, real_repo):
        self._real = real_repo

    def get_device(self, did):
        return self._real.get_device(did)

    def record_check(self, *args, **kwargs):
        raise sqlite3.IntegrityError("FOREIGN KEY constraint failed")


def test_fk_error_removes_device_from_scheduler(tmp_path):
    """FK 失败（设备已删除）→ 自动移出调度器与快照。"""
    conn, repo, hist, sched = _make_scheduler(tmp_path, 2, cap=3)
    try:
        sched._device_repo = _FkRepo(repo)
        for did, machine in list(sched._machines.items()):
            device = repo.get_device(did)
            with patch("src.core.monitor_scheduler.DetectionChain") as MC:
                MC.return_value.probe.return_value = ProbeOutcome(
                    is_online=True, failure_should_count=False, latency_ms=1.0
                )
                sched._probe_and_process(device, machine)
        assert len(sched._machines) == 0, "FK 失败的设备应从调度器移除"
        assert len(sched._status_cache) == 0, "快照不应残留已删除设备"
    finally:
        close_connection()
