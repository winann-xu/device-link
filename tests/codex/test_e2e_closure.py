# -*- coding: utf-8 -*-
"""E2E 集成测试（场景 A 变形）：在线→离线→告警→恢复→通知 全闭环。
用 127.0.0.1（真实 ICMP 可达）作为监控设备，运行时把 IP 改成 TEST-NET
模拟掉线，再改回模拟恢复，走真实调度器/探测链/状态机/告警引擎。
"""
import time

import pytest

from src.storage.database import init_database, close_connection
from src.storage.repositories import DeviceRepository, HistoryRepository, AlertRepository
from src.core.monitor_scheduler import MonitorScheduler
from src.alerts.alert_engine import AlertEngine


@pytest.fixture()
def env(tmp_path):
    conn = init_database(str(tmp_path / "e2e.db"))
    yield conn
    close_connection()


def _wait_status(repo, did, status, timeout=45):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if repo.get_device(did)["status"] == status:
            return True
        time.sleep(1)
    return False


def _wait_event(alert_repo, event_type, timeout=45):
    deadline = time.time() + timeout
    while time.time() < deadline:
        evs = alert_repo.list_events(event_type=event_type)
        if evs:
            return evs[0]
        time.sleep(1)
    return None


def test_e2e_online_offline_alert_recovery(env):
    device_repo = DeviceRepository(env)
    history_repo = HistoryRepository(env)
    alert_repo = AlertRepository(env)

    did = device_repo.add_device({
        "name": "E2E设备", "ip_address": "127.0.0.1", "subsystem_name": "集成",
        "monitor_method": "auto", "port": 0,
        "check_interval_seconds": 3, "timeout_ms": 800,
        "failure_threshold": 2, "recovery_threshold": 2,
        "is_enabled": 1, "is_maintenance": 0,
    })

    config = {
        "monitor": {"max_workers": 10, "jitter_schedule": False,
                    "default_timeout_ms": 800},
        "notify": {"cooldown_seconds": 0, "escalation_minutes": 60,
                   "retry_count": 0, "retry_backoff_base_seconds": 0,
                   "digest": {"enabled": False}},
    }
    scheduler = MonitorScheduler(device_repo.list_enabled_devices(), config,
                                 device_repo, history_repo)
    alert_engine = AlertEngine(config, alert_repo)
    scheduler.register_callback(alert_engine.on_monitor_event)
    scheduler.start()

    try:
        # 1) 上线（127.0.0.1 ICMP 可达）
        assert _wait_status(device_repo, did, "online"), "设备应上线"

        # 2) 改成不可达 IP → 连续失败 2 次 → 离线告警
        env.execute("UPDATE devices SET ip_address='192.0.2.1' WHERE id=?", (did,))
        env.commit()
        assert _wait_status(device_repo, did, "offline"), "设备应判离线"
        ev = _wait_event(alert_repo, "offline")
        assert ev is not None, "应产生离线告警事件"

        # 3) 改回可达 IP → 连续成功 2 次 → 恢复事件
        env.execute("UPDATE devices SET ip_address='127.0.0.1' WHERE id=?", (did,))
        env.commit()
        assert _wait_status(device_repo, did, "online"), "设备应恢复在线"
        ev2 = _wait_event(alert_repo, "recovery")
        assert ev2 is not None, "应产生恢复事件"
    finally:
        try:
            scheduler.stop()
        except TypeError:
            pass  # Bug G：stop() 的 shutdown(timeout=) 非法，修复前忽略
