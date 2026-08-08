# -*- coding: utf-8 -*-
"""Codex 独立冒烟测试（不依赖 Claude 的测试文件）。
覆盖：数据库/仓储 CRUD、历史在线率、凭据加密、TCP/ICMP 探测、探测链、状态机。
"""
import os
import socket
import tempfile
import threading
import time

import pytest

from src.storage.database import init_database, get_connection, close_connection
from src.storage.repositories import (
    DeviceRepository,
    HistoryRepository,
    AlertRepository,
    ChannelRepository,
)
from src.utils.crypto import encrypt, decrypt
from src.probes.ping_probe import PingProbe
from src.probes.tcp_probe import TcpProbe
from src.core.detection_chain import DetectionChain, ProbeOutcome
from src.core.device_state_machine import DeviceStateMachine, DeviceStatus


@pytest.fixture()
def db(tmp_path):
    path = str(tmp_path / "test.db")
    conn = init_database(path)
    yield conn
    close_connection()


def table_names(conn):
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    return [r[0] for r in rows]


def test_database_tables_created(db):
    names = table_names(db)
    for t in ["subsystems", "devices", "status_history", "alert_rules", "alert_events", "notification_channels"]:
        assert t in names, f"缺少表 {t}"


def test_device_repo_crud(db):
    repo = DeviceRepository(db)
    did = repo.add_device({
        "name": "网关-01", "ip_address": "192.168.50.1",
        "subsystem_name": "MES", "monitor_method": "auto", "port": 80,
    })
    assert did > 0
    d = repo.get_device(did)
    assert d["name"] == "网关-01"
    assert d["ip_address"] == "192.168.50.1"
    assert repo.update_device(did, {"name": "网关-02"})
    assert repo.get_device(did)["name"] == "网关-02"
    assert len(repo.list_devices()) == 1
    assert repo.delete_device(did) is True
    assert repo.get_device(did) is None


def test_device_repo_batch_and_status(db):
    repo = DeviceRepository(db)
    devices = [
        {"name": f"D{i}", "ip_address": f"192.168.50.{i}", "subsystem_name": "MES"}
        for i in range(5)
    ]
    ok, failed = repo.add_devices_batch(devices)
    assert ok == 5 and failed == []
    ids = [d["id"] for d in repo.list_devices()]
    assert len(ids) == 5
    assert repo.set_device_status(ids[0], "offline", 3, 1.2)
    assert repo.record_check_result(ids[0], False, 1.2)
    assert len(repo.list_enabled_devices()) == 5
    assert repo.enable_batch(ids[:2], False) == 2
    assert len(repo.list_enabled_devices()) == 3
    assert repo.delete_devices(ids[:2]) == 2
    assert len(repo.list_devices()) == 3


def test_history_uptime(db):
    repo = DeviceRepository(db)
    hist = HistoryRepository(db)
    did = repo.add_device({"name": "H1", "ip_address": "10.0.0.1"})
    for _ in range(10):
        assert hist.insert_status(did, "online", 1.0)
    for _ in range(2):
        assert hist.insert_status(did, "offline", 0.0)
    assert hist.compute_uptime(did, "day") == pytest.approx(10 / 12, abs=0.01)
    assert len(hist.query_status_range(did, "2000-01-01", "2099-01-01")) == 12


def test_crypto_roundtrip():
    secret = "smtp-password-123"
    cipher = encrypt(secret)
    assert cipher != secret
    assert decrypt(cipher) == secret


def test_tcp_probe_success():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def accept_once():
        try:
            conn, _ = srv.accept()
            conn.close()
        except OSError:
            pass

    t = threading.Thread(target=accept_once, daemon=True)
    t.start()
    r = TcpProbe("127.0.0.1", port, 2000).check()
    assert r.success is True, f"监听端口应成功: {r.error}"
    srv.close()
    t.join(timeout=3)


def test_tcp_probe_refused_classified_online(monkeypatch):
    # 本机环回关闭端口会被网络策略静默丢弃（timeout），连接拒绝分类用 mock 验证
    import src.probes.tcp_probe as tp

    def raise_refused(*args, **kwargs):
        raise ConnectionRefusedError("port closed")

    monkeypatch.setattr(tp.socket, "create_connection", raise_refused)
    r = TcpProbe("127.0.0.1", 8080, 2000).check()
    assert r.success is True  # 连接拒绝 = 设备在线


def test_tcp_probe_oserror_no_route(monkeypatch):
    import src.probes.tcp_probe as tp

    def raise_oserror(*args, **kwargs):
        raise OSError("network unreachable")

    monkeypatch.setattr(tp.socket, "create_connection", raise_oserror)
    r = TcpProbe("10.255.255.1", 80, 2000).check()
    assert r.success is False
    assert r.error == "no_route"


def test_ping_probe_localhost():
    r = PingProbe("127.0.0.1", 2000).check()
    assert r.success is True, f"本机回环 ping 应成功: {r.error} {r.error_detail}"


def test_detection_chain_online_via_icmp():
    chain = DetectionChain({"ip_address": "127.0.0.1", "port": 0}, {})
    outcome = chain.probe()
    assert outcome.is_online is True
    assert outcome.failure_should_count is False


def test_state_machine_offline_recovery():
    dev = {"id": 1, "name": "测试机", "failure_threshold": 2, "recovery_threshold": 2}
    m = DeviceStateMachine(dev)

    ok = ProbeOutcome(is_online=True, failure_should_count=False)
    tr = m.transition(ok)
    assert m.status == DeviceStatus.ONLINE

    # 第一次失败 → pending_failure（不告警）
    fail = ProbeOutcome(is_online=False, failure_should_count=True)
    tr = m.transition(fail)
    assert m.status == DeviceStatus.PENDING_FAILURE
    assert tr is None

    # 第二次失败 → offline（触发告警事件）
    tr = m.transition(fail)
    assert m.status == DeviceStatus.OFFLINE
    assert tr is not None and tr.event_type == "device_offline"

    # 恢复：连续成功 2 次
    assert m.transition(ok) is None  # 恢复计数 1
    tr = m.transition(ok)
    assert m.status == DeviceStatus.ONLINE
    assert tr is not None and tr.event_type == "device_recovered"


def test_state_machine_tcp_recheck_not_counted():
    dev = {"id": 2, "name": "防火墙禁ping", "failure_threshold": 2}
    m = DeviceStateMachine(dev)
    m.transition(ProbeOutcome(is_online=True, failure_should_count=False))

    # ping 失败但 TCP 复核成功 → 不计入 failure
    tr = m.transition(ProbeOutcome(is_online=True, failure_should_count=False, used_method="tcp_fallback"))
    assert m.status == DeviceStatus.ONLINE
    assert tr is None


def test_state_machine_unknown_accumulates_to_offline():
    # 回归测试（Bug D）：从未上线设备连续失败必须累计并最终告警，不得漏报
    dev = {"id": 3, "name": "启动即离线", "failure_threshold": 3, "recovery_threshold": 2}
    m = DeviceStateMachine(dev)
    fail = ProbeOutcome(is_online=False, failure_should_count=True)
    assert m.transition(fail) is None
    assert m.failure_count == 1
    assert m.transition(fail) is None
    assert m.failure_count == 2
    tr = m.transition(fail)
    assert tr is not None and tr.event_type == "device_offline"
    assert m.status == DeviceStatus.OFFLINE
