# -*- coding: utf-8 -*-
"""
回归测试（2026-08-08 用户反馈三问题）：
  1. 告警邮件内容完整性：
     - 单设备离线/恢复/升级通知必须带设备名、IP、系统名
     - 摘要（合并）通知必须带详细设备清单（名称/IP/系统名/时间）
  2. 告警配置保存后合并策略/全局规则热更新
  3. 仪表盘状态解析：维护中 > 未启用 > 探测状态，不得显示"未知"
"""
import time

import pytest

from src.storage.database import init_database, close_connection
from src.storage.repositories import DeviceRepository, AlertRepository
from src.core.device_state_machine import StateTransition, DeviceStatus
from src.core.monitor_scheduler import MonitorScheduler
from src.alerts.alert_engine import AlertEngine
from src.notify.base_channel import SendResult
from src.notify.email_channel import EmailChannel
from src.ui.dashboard import resolve_device_status


@pytest.fixture()
def env(tmp_path):
    conn = init_database(str(tmp_path / "content.db"))
    yield conn
    close_connection()


def base_cfg(**notify_overrides):
    cfg = {
        "monitor": {"max_workers": 10, "jitter_schedule": False,
                    "default_timeout_ms": 800},
        "notify": {
            "cooldown_seconds": 30, "escalation_minutes": 60,
            "retry_count": 1, "retry_backoff_base_seconds": 0,
            "digest": {"enabled": True, "window_seconds": 300,
                       "max_events_per_digest": 50,
                       "send_immediate_if_critical": False},
        },
    }
    cfg["notify"].update(notify_overrides)
    return cfg


def tr(did, event_type, suppress=False):
    return StateTransition(
        device_id=did, old_status=DeviceStatus.ONLINE,
        new_status=DeviceStatus.OFFLINE, event_type=event_type,
        failure_count=3, downtime_start="2026-08-08 12:00:00",
        suppress_alert=suppress,
    )


class CaptureChannel:
    def __init__(self):
        self.sent = []

    def get_channel_name(self):
        return "capture"

    def send(self, message):
        self.sent.append(message)
        return SendResult(success=True, channel="capture")

    def test(self):
        return SendResult(success=True, channel="capture")


def _add_device(repo, name, ip, subsys):
    return repo.add_device({
        "name": name, "ip_address": ip, "subsystem_name": subsys,
        "is_enabled": 1,
    })


class TestAlertContentCompleteness:
    """单设备告警/恢复/升级必须带设备名、IP、系统名。"""

    def test_offline_message_has_full_device_info(self, env):
        repo = DeviceRepository(env)
        alert = AlertRepository(env)
        ch = CaptureChannel()
        cfg = base_cfg(**{"digest": {"enabled": False, "window_seconds": 60,
                                     "max_events_per_digest": 50,
                                     "send_immediate_if_critical": False}})
        ae = AlertEngine(cfg, alert, channels=[ch], device_repo=repo)
        did = _add_device(repo, "MES-网关-01", "192.168.50.1", "MES")
        ae.on_monitor_event(tr(did, "device_offline"))
        assert ch.sent, "应发送单设备离线通知"
        msg = ch.sent[0]
        assert msg.event_type == "offline"
        assert msg.device_name == "MES-网关-01"
        assert msg.ip_address == "192.168.50.1"
        assert msg.subsystem == "MES"

    def test_recovery_message_has_full_device_info(self, env):
        repo = DeviceRepository(env)
        alert = AlertRepository(env)
        ch = CaptureChannel()
        ae = AlertEngine(base_cfg(), alert, channels=[ch], device_repo=repo)
        did = _add_device(repo, "打印机-02", "10.0.0.8", "办公")
        ae.on_monitor_event(tr(did, "device_recovered"))
        msg = ch.sent[0]
        assert msg.event_type == "recovery"
        assert msg.device_name == "打印机-02"
        assert msg.ip_address == "10.0.0.8"
        assert msg.subsystem == "办公"

    def test_escalation_message_has_full_device_info(self, env):
        repo = DeviceRepository(env)
        alert = AlertRepository(env)
        ch = CaptureChannel()
        cfg = base_cfg(**{"digest": {"enabled": False, "window_seconds": 60,
                                     "max_events_per_digest": 50,
                                     "send_immediate_if_critical": False}})
        ae = AlertEngine(cfg, alert, channels=[ch], device_repo=repo)
        did = _add_device(repo, "数据库服务器", "172.16.1.10", "机房")
        eid = alert.insert_event({
            "device_id": did, "event_type": "offline", "message": "设备离线告警",
            "notified_channels": "", "notify_success": 0,
        })
        env.execute(
            "UPDATE alert_events SET created_at='2026-08-08 00:00:00' WHERE id=?",
            (eid,))
        env.commit()
        # 直接从未确认事件构造升级消息路径（复用引擎内部逻辑）
        ae._escalation_minutes = 1
        unacked = alert.get_unacknowledged_offline_events()
        assert unacked
        dev = ae._get_device_info(unacked[0]["device_id"])
        assert dev["name"] == "数据库服务器"
        assert dev["ip_address"] == "172.16.1.10"
        assert dev["subsystem_name"] == "机房"

    def test_digest_events_and_email_list_complete(self, env):
        repo = DeviceRepository(env)
        alert = AlertRepository(env)
        ch = CaptureChannel()
        cfg = base_cfg(**{"digest": {"enabled": True, "window_seconds": 3600,
                                     "max_events_per_digest": 50,
                                     "send_immediate_if_critical": False}})
        ae = AlertEngine(cfg, alert, channels=[ch], device_repo=repo)
        for i, name in enumerate(["核心交换机-A", "核心交换机-B", "接入交换机-C"]):
            did = _add_device(repo, name, f"192.168.1.{10 + i}", "网络")
            ae.on_monitor_event(tr(did, "device_offline"))

        batch = ae._digest_engine.flush()
        assert batch is not None
        ae._send_digest(batch)

        digests = [m for m in ch.sent if m.event_type == "digest"]
        assert len(digests) == 1
        events = digests[0].extra["events"]
        assert len(events) == 3
        for e in events:
            assert e["device_name"]
            assert e["ip_address"]
            assert e["subsystem"] == "网络"
        detail = digests[0].extra["离线设备清单"]
        for name in ["核心交换机-A", "核心交换机-B", "接入交换机-C"]:
            assert name in detail
            assert "192.168.1." in detail

        # 邮件 HTML 渲染必须包含每台设备的名称/IP/子系统
        html = EmailChannel({}, {})._render_html(digests[0])
        for name in ["核心交换机-A", "核心交换机-B", "接入交换机-C"]:
            assert name in html
        assert "192.168.1.10" in html
        assert "网络" in html


class TestAlertConfigReload:
    """保存告警配置后合并策略/全局规则必须热生效。"""

    def test_reload_channels_applies_digest_and_rules(self, env):
        repo = DeviceRepository(env)
        alert = AlertRepository(env)
        ae = AlertEngine(base_cfg(), alert, channels=[], device_repo=repo)
        new_cfg = {
            "notify": {
                "cooldown_seconds": 900,
                "escalation_minutes": 5,
                "retry_count": 2,
                "retry_backoff_base_seconds": 1,
                "digest": {"enabled": False, "window_seconds": 600,
                           "max_events_per_digest": 10,
                           "send_immediate_if_critical": False},
            },
        }
        ae.reload_channels(new_cfg)
        assert ae._digest_enabled is False
        assert ae._digest_engine._window_seconds == 600
        assert ae._digest_engine._max_events_per_digest == 10
        assert ae._cooldown_seconds == 900
        assert ae._escalation_minutes == 5
        assert ae._retry_count == 2

    def test_scheduler_global_thresholds_apply(self, env):
        repo = DeviceRepository(env)
        did = _add_device(repo, "阈值设备", "10.0.0.66", "测试")
        dev = repo.get_device(did)
        from src.storage.repositories import HistoryRepository
        hist = HistoryRepository(env)
        sched = MonitorScheduler(
            [dev], base_cfg(), repo, hist)
        machine = sched._machines[did]
        assert machine._failure_threshold == 3
        assert machine._recovery_threshold == 2
        n = sched.apply_global_thresholds(5, 3)
        assert n == 1
        assert machine._failure_threshold == 5
        assert machine._recovery_threshold == 3


class TestDashboardStatusResolution:
    """仪表盘状态灯：维护中 > 未启用 > 探测状态，禁止显示"未知"。"""

    def test_maintenance_device_shows_maintenance(self):
        dev = {"is_enabled": 1, "is_maintenance": 1}
        assert resolve_device_status(dev, "unknown") == "maintenance"
        assert resolve_device_status(dev, "offline") == "maintenance"

    def test_disabled_device_shows_disabled(self):
        dev = {"is_enabled": 0, "is_maintenance": 0}
        assert resolve_device_status(dev, "online") == "disabled"
        # 禁用 + 维护：禁用优先显示未启用（不参与监控）
        dev2 = {"is_enabled": 0, "is_maintenance": 1}
        assert resolve_device_status(dev2, "unknown") == "disabled"

    def test_normal_device_uses_snapshot(self):
        dev = {"is_enabled": 1, "is_maintenance": 0}
        assert resolve_device_status(dev, "online") == "online"
        assert resolve_device_status(dev, "offline") == "offline"
        assert resolve_device_status(dev, "pending_failure") == "pending_failure"
        assert resolve_device_status(dev, "") == "unknown"


class TestEscalationControl:
    """升级风暴修复：总开关可关闭 + 每事件升级次数上限。"""

    @staticmethod
    def _insert_old_event(env, alert, did):
        eid = alert.insert_event({
            "device_id": did, "event_type": "offline", "message": "设备离线告警",
            "notified_channels": "", "notify_success": 0,
        })
        env.execute(
            "UPDATE alert_events SET created_at='2026-08-08 00:00:00' WHERE id=?",
            (eid,))
        env.commit()
        return eid

    def test_escalation_disabled_sends_nothing(self, env, monkeypatch):
        import src.alerts.alert_engine as aem
        from src.storage.repositories import DeviceRepository
        repo = DeviceRepository(env)
        alert = AlertRepository(env)
        did = _add_device(repo, "升级关设备", "10.9.9.1", "测试")
        self._insert_old_event(env, alert, did)

        class Ch:
            def __init__(self):
                self.sent = []

            def get_channel_name(self):
                return "cap"

            def send(self, message):
                self.sent.append(message)
                return SendResult(success=True, channel="cap")

        ch = Ch()
        cfg = base_cfg(**{
            "escalation_enabled": False, "escalation_minutes": 1,
            "escalation_max_count": 3,
            "digest": {"enabled": False, "window_seconds": 60,
                       "max_events_per_digest": 50,
                       "send_immediate_if_critical": False},
        })
        ae = AlertEngine(cfg, alert, channels=[ch], device_repo=repo)
        monkeypatch.setattr(aem.time, "sleep", lambda s: 0.01)
        ae.run_escalation_loop()
        deadline = time.time() + 5
        while time.time() < deadline and not ch.sent:
            time.sleep(0.1)
        ae._running = False
        assert ch.sent == [], "升级关闭后不应发送任何升级邮件"

    def test_escalation_capped_at_max_count(self, env, monkeypatch):
        import src.alerts.alert_engine as aem
        from src.storage.repositories import DeviceRepository
        repo = DeviceRepository(env)
        alert = AlertRepository(env)
        did = _add_device(repo, "升级限次设备", "10.9.9.2", "测试")
        self._insert_old_event(env, alert, did)

        class Ch:
            def __init__(self):
                self.sent = []

            def get_channel_name(self):
                return "cap"

            def send(self, message):
                if message.event_type == "escalation":
                    self.sent.append(message)
                return SendResult(success=True, channel="cap")

        ch = Ch()
        cfg = base_cfg(**{
            "escalation_enabled": True, "escalation_minutes": 1,
            "escalation_max_count": 2,
            "digest": {"enabled": False, "window_seconds": 60,
                       "max_events_per_digest": 50,
                       "send_immediate_if_critical": False},
        })
        ae = AlertEngine(cfg, alert, channels=[ch], device_repo=repo)
        monkeypatch.setattr(aem.time, "sleep", lambda s: 0.01)

        class FakeClock:
            def __init__(self):
                self.t = 1000000.0

            def __call__(self):
                return self.t

        clock = FakeClock()
        monkeypatch.setattr(aem.time, "time", clock)
        ae.run_escalation_loop()

        # 第一次升级
        deadline = time.time() + 5
        while time.time() < deadline and len(ch.sent) < 1:
            time.sleep(0.1)
        assert len(ch.sent) == 1

        # 推进 30 分钟，触发第二次升级
        clock.t += 1801
        deadline = time.time() + 5
        while time.time() < deadline and len(ch.sent) < 2:
            time.sleep(0.1)
        assert len(ch.sent) == 2

        # 再推进 30 分钟：已达上限，不得发第三次
        clock.t += 1801
        time.sleep(1.5)
        ae._running = False
        assert len(ch.sent) == 2, f"每事件升级上限=2，实际 {len(ch.sent)}"
