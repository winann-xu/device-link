# -*- coding: utf-8 -*-
"""Codex 独立测试：告警合并引擎 + 三个通知通道（mock 外网）。"""
import time

from src.alerts.digest_engine import DigestEngine
from src.notify.base_channel import AlertMessage
from src.notify.email_channel import EmailChannel
from src.notify.feishu_channel import FeishuChannel
from src.notify.wecom_channel import WeComChannel


def make_event(i, subsystem="MES"):
    return {
        "device_id": i,
        "device_name": f"设备{i}",
        "ip_address": f"192.168.50.{i}",
        "subsystem": subsystem,
        "message": f"设备{i}离线",
        "occurred_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def test_digest_window_merge_and_flush():
    cfg = {"notify": {"digest": {"window_seconds": 60, "max_events_per_digest": 50,
                                  "send_immediate_if_critical": False}}}
    d = DigestEngine(cfg)
    for i in range(10):
        assert d.add_event(make_event(i)) is None
    assert d.get_pending_count() == 10
    assert d.should_flush() is False  # 窗口未到期，不应提前发送
    batch = d.flush()
    assert batch is not None and len(batch.events) == 10
    assert d.get_pending_count() == 0


def test_digest_critical_bypass():
    cfg = {"notify": {"digest": {"window_seconds": 300, "max_events_per_digest": 50,
                                  "send_immediate_if_critical": True}}}
    d = DigestEngine(cfg)
    digest_id = None
    for i in range(5):  # 同子系统 5 台离线 → 紧急绕过
        rid = d.add_event(make_event(i, "DCS"))
        if rid is not None:
            digest_id = rid
    assert digest_id is not None, "同子系统 >=5 台离线应紧急触发"


def test_digest_capacity():
    cfg = {"notify": {"digest": {"window_seconds": 300, "max_events_per_digest": 3,
                                  "send_immediate_if_critical": False}}}
    d = DigestEngine(cfg)
    for i in range(5):
        d.add_event(make_event(i))
    assert d.should_flush() is True  # 容量达到上限应触发发送
    batch = d.flush()
    assert len(batch.events) == 3
    assert d.get_pending_count() == 2  # 剩余进入下一批次


def test_email_channel_payload(monkeypatch):
    import base64
    import email as email_mod
    import re
    sent = {}

    class FakeSMTP:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self):
            pass

        def login(self, u, p):
            sent["login"] = (u, p)

        def sendmail(self, frm, to, msg):
            sent["msg"] = msg

    import smtplib
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    cfg = {"smtp_host": "smtp.test.com", "smtp_port": 587, "smtp_user": "a@b.com",
           "smtp_password": "pass", "use_ssl": False, "sender_name": "DEVICE LINK",
           "recipients": ["x@y.com"]}
    ch = EmailChannel(cfg, {})
    monkeypatch.setattr(ch, "_decrypt_password", lambda: "pass")
    msg = AlertMessage(event_type="offline", device_name="网关-1", ip_address="192.168.50.1",
                       subsystem="MES", message="设备离线", occurred_at="2026-08-07 12:00:00")
    r = ch.send(msg)
    assert r.success is True
    # 解析 MIME：主题（encoded-word）与 HTML body（base64）
    m = email_mod.message_from_string(sent["msg"])
    subject = m["Subject"]
    mime_charset = email_mod.header.decode_header(subject)[0]
    subject_text = mime_charset[0].decode(mime_charset[1] or "utf-8")
    assert "[DEVICE LINK]" in subject_text and "网关-1" in subject_text
    html = ""
    for part in m.walk():
        if part.get_content_type() == "text/html":
            payload = part.get_payload()
            if part["Content-Transfer-Encoding"] == "base64":
                html = base64.b64decode(payload).decode("utf-8", errors="replace")
            else:
                html = payload
    assert "网关-1" in html and "192.168.50.1" in html


def test_feishu_payload(monkeypatch):
    captured = {}

    class FakeResp:
        status_code = 200

        def json(self):
            return {"code": 0}

    class FakeSession:
        def post(self, url, json=None, timeout=None, **kwargs):
            captured["url"] = url
            captured["json"] = json
            return FakeResp()

    ch = FeishuChannel({"webhook_url": "https://open.feishu.cn/hook/xxx"})
    monkeypatch.setattr(ch, "_session", FakeSession())
    msg = AlertMessage(event_type="offline", device_name="网关-1", ip_address="192.168.50.1",
                       subsystem="MES", message="设备离线", occurred_at="2026-08-07 12:00:00")
    r = ch.send(msg)
    assert r.success is True
    payload = captured["json"]
    assert payload["msg_type"] == "interactive"
    card = payload["card"]
    # 飞书卡片 JSON 2.0：schema 声明 + body.elements，组件为 markdown/hr
    assert card["schema"] == "2.0"
    assert card["header"]["template"] == "red"
    assert card["header"]["title"]["content"] == "设备离线告警"
    elements = card["body"]["elements"]
    assert elements[0]["tag"] == "markdown"
    assert "网关-1" in elements[0]["content"]
    assert "192.168.50.1" in elements[0]["content"]
    tags = {e["tag"] for e in elements}
    assert tags == {"markdown", "hr"}


def test_feishu_digest_payload(monkeypatch):
    captured = {}

    class FakeResp:
        status_code = 200

        def json(self):
            return {"code": 0}

    class FakeSession:
        def post(self, url, json=None, timeout=None, **kwargs):
            captured["json"] = json
            return FakeResp()

    ch = FeishuChannel({"webhook_url": "https://open.feishu.cn/hook/xxx"})
    monkeypatch.setattr(ch, "_session", FakeSession())
    msg = AlertMessage(
        event_type="digest",
        device_name="3 台设备离线",
        ip_address="",
        subsystem="",
        message="时间窗口内 3 台设备离线，涉及 2 个子系统",
        occurred_at="2026-08-09 12:00:00",
        extra={
            "离线设备清单": "- 网关-1 (192.168.50.1) [MES]\n- 路由器-2 (192.168.50.2) [SCADA]\n- 服务器-3 (192.168.50.3) [MES]",
            "digest_id": 7,
            "events": [],
        },
    )
    r = ch.send(msg)
    assert r.success is True
    payload = captured["json"]
    card = payload["card"]
    assert card["schema"] == "2.0"
    assert card["header"]["template"] == "blue"
    elements = card["body"]["elements"]
    # 摘要清单必须作为 markdown 内容块完整携带，而非被忽略
    all_content = "\n".join(e.get("content", "") for e in elements if e.get("tag") == "markdown")
    assert "3 台设备离线" in all_content
    assert "网关-1 (192.168.50.1) [MES]" in all_content
    assert "服务器-3 (192.168.50.3) [MES]" in all_content


def test_wecom_payload_and_errcode(monkeypatch):
    captured = {}

    class FakeResp:
        status_code = 200

        def json(self):
            return {"errcode": 0}

    class FakeSession:
        def post(self, url, json=None, timeout=None, **kwargs):
            captured["url"] = url
            captured["json"] = json
            return FakeResp()

    ch = WeComChannel({"webhook_url": "https://qyapi.weixin.qq.com/hook/xxx"})
    monkeypatch.setattr(ch, "_session", FakeSession())
    msg = AlertMessage(event_type="offline", device_name="网关-1", ip_address="192.168.50.1",
                       subsystem="MES", message="设备离线", occurred_at="2026-08-07 12:00:00")
    r = ch.send(msg)
    assert r.success is True
    assert "<font color='warning'>" in captured["json"]["markdown"]["content"]

    # errcode != 0 → 失败并带错误信息
    class FakeRespErr:
        status_code = 200

        def json(self):
            return {"errcode": 93000, "errmsg": "群已解散"}

    class FakeSessionErr:
        def post(self, url, json=None, timeout=None, **kwargs):
            return FakeRespErr()

    monkeypatch.setattr(ch, "_session", FakeSessionErr())
    r2 = ch.send(msg)
    assert r2.success is False
    assert "93000" in r2.error or "解散" in r2.error or "移除" in r2.error
