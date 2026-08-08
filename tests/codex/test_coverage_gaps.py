# -*- coding: utf-8 -*-
"""补齐核心覆盖率：ping_probe 异常/重试路径、database 路径解析、alert_engine 发送失败路径。"""
import time

import pytest

from src.probes.ping_probe import PingProbe
from src.probes.tcp_probe import TcpProbe
from src.storage import database
from src.storage.database import get_db_path
from src.storage.repositories import AlertRepository
from src.alerts.alert_engine import AlertEngine
from src.core.device_state_machine import StateTransition, DeviceStatus
from src.notify.base_channel import SendResult


@pytest.fixture()
def env(tmp_path):
    conn = database.init_database(str(tmp_path / "gap.db"))
    yield conn
    database.close_connection()


class FailChannel:
    def __init__(self):
        self.attempts = 0

    def get_channel_name(self):
        return "fail"

    def send(self, message):
        self.attempts += 1
        return SendResult(success=False, error="boom", channel="fail")

    def test(self):
        return SendResult(success=False, error="boom", channel="fail")


def test_ping_probe_retry_and_metrics(monkeypatch):
    # 第一次 timeout，第二次成功 → 重试逻辑
    calls = []

    def fake_ping(host, timeout=3, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            return None  # ping3 超时返回 None
        return 0.001

    monkeypatch.setattr("ping3.ping", fake_ping)
    probe = PingProbe("127.0.0.1", 1000)
    r = probe.check()
    assert r.success is True
    assert len(calls) == 2
    m = probe.get_metrics()
    assert m["checks"] >= 1 and m["success_rate"] == 1.0


def test_ping_probe_oserror_classified(monkeypatch):
    def fake_ping(host, timeout=3, **kwargs):
        raise OSError("permission denied")

    monkeypatch.setattr("ping3.ping", fake_ping)
    r = PingProbe("10.0.0.1", 1000).check()
    assert r.success is False
    assert r.error == "permission_denied"


def test_db_path_resolution(tmp_path, monkeypatch):
    # 相对路径基于项目根解析
    p = get_db_path({"storage": {"path": "./data/x.db"}})
    assert str(p).endswith("data" + "\\x.db") or str(p).endswith("data/x.db")
    # 绝对路径原样
    abs_p = str(tmp_path / "abs.db")
    assert get_db_path({"storage": {"path": abs_p}}) == abs_p


def test_alert_engine_retry_and_fail_paths(env):
    alert = AlertRepository(env)
    ch = FailChannel()
    cfg = {"notify": {"cooldown_seconds": 30, "escalation_minutes": 60,
                      "retry_count": 3, "retry_backoff_base_seconds": 0,
                      "digest": {"enabled": False, "window_seconds": 60,
                                 "max_events_per_digest": 50, "send_immediate_if_critical": False}}}
    ae = AlertEngine(cfg, alert, channels=[ch])
    from src.storage.repositories import DeviceRepository
    did = DeviceRepository(env).add_device({"name": "失败通道设备", "ip_address": "10.0.0.7",
                                            "subsystem_name": "MES", "is_enabled": 1})
    ae.on_monitor_event(StateTransition(device_id=did, old_status=DeviceStatus.ONLINE,
                                        new_status=DeviceStatus.OFFLINE, event_type="device_offline",
                                        failure_count=3, downtime_start="x", suppress_alert=False))
    # 3 次尝试全部失败
    assert ch.attempts == 3
    ev = alert.list_events(event_type="offline")[0]
    assert ev["notify_success"] == 0


def test_alert_engine_acknowledge(env):
    alert = AlertRepository(env)
    from src.storage.repositories import DeviceRepository
    did = DeviceRepository(env).add_device({"name": "确认设备", "ip_address": "10.0.0.8",
                                            "subsystem_name": "MES", "is_enabled": 1})
    eid = alert.insert_event({"device_id": did, "event_type": "offline", "message": "x",
                              "notified_channels": "", "notify_success": 0})
    assert alert.acknowledge(eid, "tester") is True
    ev = alert.list_events(event_type="offline")[0]
    assert ev["is_acknowledged"] == 1


def test_alert_engine_escalation(env, monkeypatch):
    from src.storage.repositories import DeviceRepository
    from src.alerts.alert_engine import AlertEngine as AE
    alert = AlertRepository(env)

    class OkChannel:
        def __init__(self):
            self.types = []

        def get_channel_name(self):
            return "ok"

        def send(self, message):
            self.types.append(message.event_type)
            return SendResult(success=True, channel="ok")

    ch = OkChannel()
    cfg = {"notify": {"cooldown_seconds": 30, "escalation_minutes": 1, "retry_count": 1,
                      "retry_backoff_base_seconds": 0,
                      "digest": {"enabled": False, "window_seconds": 60,
                                 "max_events_per_digest": 50, "send_immediate_if_critical": False}}}
    ae = AE(cfg, alert, channels=[ch])
    did = DeviceRepository(env).add_device({"name": "升级设备", "ip_address": "10.0.0.9",
                                            "subsystem_name": "MES", "is_enabled": 1})
    eid = alert.insert_event({"device_id": did, "event_type": "offline", "message": "x",
                              "notified_channels": "", "notify_success": 0,
                              "created_at": "2020-01-01 00:00:00"})
    # insert_event 忽略传入的 created_at（DB 默认当前时间），手动回填模拟旧事件
    env.execute("UPDATE alert_events SET created_at='2020-01-01 00:00:00' WHERE id=?", (eid,))
    env.commit()
    import src.alerts.alert_engine as aem
    monkeypatch.setattr(aem.time, "sleep", lambda s: 0.01)
    import threading
    t = threading.Thread(target=ae.run_escalation_loop, daemon=True)
    t.start()
    deadline = time.time() + 5
    while time.time() < deadline and "escalation" not in ch.types:
        time.sleep(0.2)
    assert "escalation" in ch.types, "超过升级阈值应发送升级通知"
    ae._running = False


def test_digest_send_persists_notify_success(env):
    from src.storage.repositories import DeviceRepository
    from src.alerts.alert_engine import AlertEngine as AE
    from src.core.device_state_machine import StateTransition, DeviceStatus
    alert = AlertRepository(env)

    class OkChannel:
        def get_channel_name(self):
            return "ok"

        def send(self, message):
            return SendResult(success=True, channel="ok")

    cfg = {"notify": {"cooldown_seconds": 30, "escalation_minutes": 60, "retry_count": 1,
                      "retry_backoff_base_seconds": 0,
                      "digest": {"enabled": True, "window_seconds": 2,
                                 "max_events_per_digest": 50, "send_immediate_if_critical": False}}}
    ae = AE(cfg, alert, channels=[OkChannel()])
    ae.run_escalation_loop()
    did = DeviceRepository(env).add_device({"name": "摘要落库设备", "ip_address": "10.0.0.10",
                                            "subsystem_name": "MES", "is_enabled": 1})
    ae.on_monitor_event(StateTransition(device_id=did, old_status=DeviceStatus.ONLINE,
                                        new_status=DeviceStatus.OFFLINE, event_type="device_offline",
                                        failure_count=3, downtime_start="x", suppress_alert=False))
    time.sleep(4)
    ev = alert.list_events(event_type="offline")[0]
    assert ev["notify_success"] == 2, "摘要投递成功后 notify_success 应落库为 2"
