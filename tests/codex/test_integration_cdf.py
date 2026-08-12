# -*- coding: utf-8 -*-
"""集成测试：C=维护窗口静默、D=摘要合并、F=冷却抑制。"""
import time

import pytest

from src.storage.database import init_database, close_connection
from src.storage.repositories import DeviceRepository, HistoryRepository, AlertRepository
from src.core.monitor_scheduler import MonitorScheduler
from src.core.device_state_machine import StateTransition, DeviceStatus
from src.alerts.alert_engine import AlertEngine


@pytest.fixture()
def env(tmp_path):
    conn = init_database(str(tmp_path / "i.db"))
    yield conn
    close_connection()


def base_config(**notify_overrides):
    cfg = {"monitor": {"max_workers": 10, "jitter_schedule": False, "default_timeout_ms": 800},
           "notify": {"cooldown_seconds": 30, "escalation_minutes": 60, "retry_count": 1,
                      "retry_backoff_base_seconds": 0,
                      "digest": {"enabled": True, "window_seconds": 300,
                                 "max_events_per_digest": 50, "send_immediate_if_critical": False}}}
    cfg["notify"].update(notify_overrides)
    return cfg


def tr(did, event_type, suppress=False):
    return StateTransition(device_id=did, old_status=DeviceStatus.ONLINE,
                           new_status=DeviceStatus.OFFLINE, event_type=event_type,
                           failure_count=3, downtime_start="2026-08-07 12:00:00",
                           suppress_alert=suppress)


class CaptureChannel:
    def __init__(self):
        self.sent = []

    def send(self, message):
        self.sent.append(message)
        from src.notify.base_channel import SendResult
        return SendResult(success=True, channel="capture")


def test_maintenance_window_silent(env):
    repo = DeviceRepository(env)
    hist = HistoryRepository(env)
    alert = AlertRepository(env)
    did = repo.add_device({"name": "维护设备", "ip_address": "192.0.2.1", "subsystem_name": "测试",
                           "check_interval_seconds": 3, "timeout_ms": 800,
                           "failure_threshold": 1, "recovery_threshold": 2,
                           "is_enabled": 1, "is_maintenance": 1})
    sched = MonitorScheduler(repo.list_enabled_devices(), base_config(), repo, hist)
    ae = AlertEngine(base_config(), alert)
    sched.register_callback(ae.on_monitor_event)
    sched.start()
    try:
        deadline = time.time() + 30
        while time.time() < deadline:
            if repo.get_device(did)["status"] == "offline":
                break
            time.sleep(1)
        assert repo.get_device(did)["status"] == "offline", "设备应判定离线"
        time.sleep(3)
        assert alert.list_events() == [], "维护模式不应产生任何告警事件"
    finally:
        try:
            sched.stop()
        except TypeError:
            pass


def test_digest_merge_10_to_1(env):
    repo = DeviceRepository(env)
    alert = AlertRepository(env)
    channel = CaptureChannel()
    cfg = base_config(**{"digest": {"enabled": True, "window_seconds": 5,
                                    "max_events_per_digest": 50,
                                    "send_immediate_if_critical": False}})
    ae = AlertEngine(cfg, alert, channels=[channel])
    ae.run_escalation_loop()
    for i in range(10):
        did = repo.add_device({"name": f"D{i}", "ip_address": f"10.0.0.{i}",
                               "subsystem_name": "MES", "is_enabled": 1})
        ae.on_monitor_event(tr(did, "device_offline"))
    time.sleep(9)  # 窗口 5s + 摘要泵轮询最长 2s + 落库余量：固定 9s 避免时序抖动
    digests = [m for m in channel.sent if m.event_type == "digest"]
    assert len(digests) == 1, f"10 条离线应合并为 1 封摘要，实际 {len(digests)}"
    assert len(digests[0].extra.get("events", [])) == 10


def test_cooldown_suppresses_repeat_alert(env):
    repo = DeviceRepository(env)
    alert = AlertRepository(env)
    cfg = base_config(cooldown_seconds=30)
    ae = AlertEngine(cfg, alert, channels=[CaptureChannel()])
    did = repo.add_device({"name": "冷却设备", "ip_address": "10.0.0.99",
                           "subsystem_name": "MES", "is_enabled": 1})
    ae.on_monitor_event(tr(did, "device_offline"))
    # 冷却期内再次离线（无恢复）→ 不重复告警
    ae.on_monitor_event(tr(did, "device_offline"))
    offline_events = alert.list_events(event_type="offline")
    assert len(offline_events) == 1, "冷却期内重复离线不应再次告警"
