# -*- coding: utf-8 -*-
"""每日离线报告回归（v1.0.9）：
- 仅统计当前离线设备（pending_failure/维护/未启用不计入）
- 离线数量为 0 时不发送邮件
- 未启用不发送；当天已发送不重复发送
- 发送后状态落盘，重启不重复
"""
import json
import os

from src.storage.database import init_database, close_connection
from src.storage.repositories import DeviceRepository, AlertRepository
from src.alerts.alert_engine import AlertEngine
from src.notify.base_channel import SendResult


class CaptureChannel:
    def __init__(self):
        self.sent = []

    def get_channel_name(self):
        return "capture"

    def send(self, message):
        self.sent.append(message)
        return SendResult(success=True, channel="capture")


def _make_env(tmp_path):
    conn = init_database(str(tmp_path / "daily.db"))
    repo = DeviceRepository(conn)
    alert = AlertRepository(conn)
    return conn, repo, alert


def _cfg(enabled=True, send_time="00:00"):
    return {
        "notify": {
            "digest": {"enabled": False},
            "daily_report": {"enabled": enabled, "send_time": send_time},
            "cooldown_seconds": 0,
            "retry_count": 1,
            "retry_backoff_base_seconds": 0,
        }
    }


def _make_engine(config, repo, alert, state_file, channel=None):
    ch = channel or CaptureChannel()
    return AlertEngine(config, alert, channels=[ch], device_repo=repo,
                       daily_state_file=state_file), ch


def test_list_current_offline_devices_excludes_pending_maintenance_disabled(tmp_path):
    """统计口径：仅 status='offline' 且启用、非维护的设备。"""
    conn, repo, alert = _make_env(tmp_path)
    try:
        d_offline = repo.add_device({"name": "真离线", "ip_address": "10.0.0.1"})
        d_pending = repo.add_device({"name": "待定", "ip_address": "10.0.0.2"})
        d_maint = repo.add_device({"name": "维护", "ip_address": "10.0.0.3"})
        d_disabled = repo.add_device({"name": "禁用", "ip_address": "10.0.0.4",
                                      "is_enabled": 0})

        repo.set_device_status(d_offline, "offline", 3, 0)
        repo.set_device_status(d_pending, "pending_failure", 1, 0)
        repo.set_device_status(d_maint, "offline", 3, 0)
        repo.update_device(d_maint, {"is_maintenance": 1})
        repo.set_device_status(d_disabled, "offline", 3, 0)

        result = repo.list_current_offline_devices()
        names = [d["name"] for d in result]
        assert names == ["真离线"], f"应只统计真离线设备，实际 {names}"
    finally:
        close_connection()


def test_daily_report_sends_when_offline(tmp_path):
    """有离线设备时发送清单邮件并落盘日期。"""
    conn, repo, alert = _make_env(tmp_path)
    state_file = str(tmp_path / "daily_state.json")
    try:
        did = repo.add_device({"name": "离线A", "ip_address": "10.0.0.9",
                               "subsystem_name": "机房"})
        repo.set_device_status(did, "offline", 3, 0)
        engine, ch = _make_engine(_cfg(), repo, alert, state_file)
        engine._maybe_send_daily_report()

        assert len(ch.sent) == 1
        msg = ch.sent[0]
        assert msg.event_type == "daily_report"
        assert msg.device_name == "1 台设备离线"
        assert msg.extra["events"][0]["device_name"] == "离线A"
        with open(state_file, "r", encoding="utf-8") as f:
            assert json.load(f)["last_sent_date"]
    finally:
        close_connection()


def test_daily_report_skips_when_zero_offline(tmp_path):
    """离线设备为 0 时不发送邮件（仅记录状态）。"""
    conn, repo, alert = _make_env(tmp_path)
    state_file = str(tmp_path / "daily_state2.json")
    try:
        engine, ch = _make_engine(_cfg(), repo, alert, state_file)
        engine._maybe_send_daily_report()
        assert ch.sent == [], "0 台离线不应发送邮件"
        with open(state_file, "r", encoding="utf-8") as f:
            assert json.load(f)["last_sent_date"]
    finally:
        close_connection()


def test_daily_report_respects_enabled_and_sent_date(tmp_path):
    """未启用不发送；当天已发送不重复发送。"""
    conn, repo, alert = _make_env(tmp_path)
    state_file = str(tmp_path / "daily_state3.json")
    try:
        did = repo.add_device({"name": "离线B", "ip_address": "10.0.0.8"})
        repo.set_device_status(did, "offline", 3, 0)

        # 未启用
        engine_off, ch_off = _make_engine(_cfg(enabled=False), repo, alert, state_file)
        engine_off._maybe_send_daily_report()
        assert ch_off.sent == [], "未启用不应发送"

        # 启用但今天已发过
        import datetime
        today = datetime.datetime.now().strftime("%Y-%m-%d")
        engine_sent, ch_sent = _make_engine(_cfg(), repo, alert, state_file)
        engine_sent._daily_report_last_sent = today
        engine_sent._maybe_send_daily_report()
        assert ch_sent.sent == [], "当天已发送不应重复发送"
    finally:
        close_connection()


def test_daily_report_respects_send_time(tmp_path):
    """未到发送时间不发送。"""
    conn, repo, alert = _make_env(tmp_path)
    state_file = str(tmp_path / "daily_state4.json")
    try:
        did = repo.add_device({"name": "离线C", "ip_address": "10.0.0.7"})
        repo.set_device_status(did, "offline", 3, 0)
        # 用不可能到达的发送时间，保证"未到点不发送"判定
        engine, ch = _make_engine(_cfg(send_time="99:99"), repo, alert, state_file)
        engine._maybe_send_daily_report()
        assert ch.sent == [], "未到发送时间不应发送"
    finally:
        close_connection()
