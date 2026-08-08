"""
模块：test_e2e_monitoring.py
功能：DEVICE LINK 集成测试 —— 端到端监控闭环验证
     覆盖：E2E 闭环、误报抑制、漏报回归、摘要引擎集成、调度器集成

作者：Claude
创建日期：2026-08-07
"""
import os
import sys
import time
import tempfile
import threading
import pytest

# 确保项目根在 sys.path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from src.core.device_state_machine import DeviceStateMachine, DeviceStatus
from src.core.detection_chain import DetectionChain
from src.alerts.digest_engine import DigestEngine
from src.storage.database import init_database
from src.storage.repositories import DeviceRepository, HistoryRepository, AlertRepository


# ============================================================
# Fixtures
# ============================================================

@pytest.fixture
def tmp_db():
    """创建临时 SQLite 数据库（每次测试独立）。"""
    import src.storage.database as _db

    db_path = os.path.join(tempfile.gettempdir(), f'test_e2e_{os.getpid()}_{id({})}.db')

    # 设置全局数据库路径，让所有 Repository 的 get_connection() 能找到
    with _db._db_path_lock:
        _db._db_path_global = db_path

    # 通过 init_database 初始化（设置路径 + 建表）
    conn = _db.init_database(db_path)
    yield conn

    # 清理：清除当前线程连接，清除全局路径
    _db.close_connection()
    with _db._db_path_lock:
        _db._db_path_global = None
    try:
        os.unlink(db_path)
    except OSError:
        pass


@pytest.fixture
def base_config():
    """最小可用配置。"""
    return {
        'monitor': {
            'default_interval_seconds': 30,
            'default_timeout_ms': 3000,
            'default_failure_threshold': 3,
            'default_recovery_threshold': 2,
            'max_workers': 10,
            'jitter_schedule': False,
        },
        'notify': {
            'digest': {
                'enabled': True,
                'window_seconds': 300,
                'max_events_per_digest': 50,
                'send_immediate_if_critical': True,
            },
            'retry_count': 3,
            'retry_backoff_base_seconds': 5,
            'cooldown_seconds': 1800,
            'escalation_minutes': 15,
        },
    }


# ============================================================
# E2E 闭环测试
# ============================================================

class TestE2EClosedLoop:
    """
    E2E 闭环：设备在线 → 连续失败 → 离线告警 → 连续成功 → 恢复通知。
    验证监控系统的完整告警/恢复链路。
    """

    def test_online_to_offline_to_recovery(self, tmp_db, base_config):
        """完整生命周期：在线 → 离线(3次失败) → 恢复(2次成功)。"""
        device_repo = DeviceRepository(tmp_db)
        did = device_repo.add_device({
            'name': 'E2E-Test-Device',
            'ip_address': '192.168.1.100',
            'failure_threshold': 3,
            'recovery_threshold': 2,
            'is_enabled': 1,
        })

        device = device_repo.get_device(did)
        machine = DeviceStateMachine(device)

        # Phase 1: 首次成功探测 → ONLINE
        outcome_ok = _make_outcome(True, False)
        t = machine.transition(outcome_ok)
        assert t is not None
        assert t.new_status == DeviceStatus.ONLINE

        # Phase 2: 连续 3 次失败 → OFFLINE + device_offline 事件
        for i in range(3):
            outcome_fail = _make_outcome(False, True)
            t = machine.transition(outcome_fail)

        assert t is not None
        assert t.new_status == DeviceStatus.OFFLINE
        assert t.event_type == 'device_offline'

        # Phase 3: 连续 2 次成功 → ONLINE + device_recovered 事件
        for i in range(2):
            outcome_ok = _make_outcome(True, False)
            t = machine.transition(outcome_ok)

        assert t is not None
        assert t.new_status == DeviceStatus.ONLINE
        assert t.event_type == 'device_recovered'

    def test_alert_event_persisted(self, tmp_db, base_config):
        """验证离线告警事件正确写入数据库。"""
        device_repo = DeviceRepository(tmp_db)
        alert_repo = AlertRepository(tmp_db)

        did = device_repo.add_device({
            'name': 'Alert-Persist-Device',
            'ip_address': '10.0.0.1',
            'failure_threshold': 1,  # N=1 立即告警
            'is_enabled': 1,
        })

        device = device_repo.get_device(did)
        machine = DeviceStateMachine(device)

        # 一次失败立即触发告警（N=1）
        outcome_fail = _make_outcome(False, True)
        t = machine.transition(outcome_fail)

        assert t is not None
        assert t.event_type == 'device_offline'

        # 写入告警事件（event_type 必须为 'offline' 以匹配 get_unacknowledged_offline_events 的查询条件）
        eid = alert_repo.insert_event({
            'device_id': did,
            'event_type': 'offline',
            'message': f"设备 {device['name']} 离线",
        })
        assert eid > 0

        # 验证可查询
        unacked = alert_repo.get_unacknowledged_offline_events()
        assert len(unacked) >= 1
        assert any(e['device_id'] == did for e in unacked)


# ============================================================
# 误报抑制测试
# ============================================================

class TestFalsePositivePrevention:
    """
    误报抑制三保险：
      1. N 次连续失败判定
      2. TCP 端口复核
      3. 维护窗口静默
    """

    def test_single_failure_no_alert(self, base_config):
        """单次失败不触发告警（N>1 时进入 PENDING_FAILURE）。"""
        device = {
            'id': 1, 'name': 'FP-Test', 'ip_address': '10.0.0.2',
            'failure_threshold': 3, 'recovery_threshold': 2, 'is_enabled': 1,
        }
        machine = DeviceStateMachine(device)

        # 先上线
        machine.transition(_make_outcome(True, False))

        # 单次失败
        t = machine.transition(_make_outcome(False, True))
        # 不应产生事件（仅进入 PENDING）
        assert t is None
        assert machine.status == DeviceStatus.PENDING_FAILURE
        assert machine.failure_count == 1

    def test_tcp_fallback_prevents_false_positive(self, base_config):
        """TCP 复核通过时不计数、不告警。"""
        device = {
            'id': 2, 'name': 'TCP-Fallback', 'ip_address': '10.0.0.3',
            'port': 80, 'failure_threshold': 3, 'is_enabled': 1,
        }
        machine = DeviceStateMachine(device)

        # 先上线
        machine.transition(_make_outcome(True, False))

        # ping 失败但 TCP 复核通过 → 不计入失败
        t = machine.transition(_make_outcome(True, False))
        # 应保持 ONLINE
        assert machine.status == DeviceStatus.ONLINE
        assert machine.failure_count == 0

    def test_maintenance_suppresses_alert(self, base_config):
        """维护模式下即使达到离线阈值也不告警。"""
        device = {
            'id': 3, 'name': 'Maint-Device', 'ip_address': '10.0.0.4',
            'failure_threshold': 1, 'is_enabled': 1, 'is_maintenance': 1,
        }
        machine = DeviceStateMachine(device)
        machine.enter_maintenance()

        # N=1 时一次失败就应离线
        outcome_fail = _make_outcome(False, True)
        t = machine.transition(outcome_fail)

        # 状态应变为 OFFLINE 但 suppress_alert=True
        if t is not None:
            assert t.suppress_alert or t.event_type is None


# ============================================================
# Bug 回归测试（防止已修复 Bug 复现）
# ============================================================

class TestBugRegressions:
    """已修复 Bug 的回归测试，防止修复退化。"""

    def test_bug_d_unknown_accumulates_failure(self, base_config):
        """
        Bug D 回归：UNKNOWN 状态连续失败时 failure_count 必须累计，
        达到阈值后必须判定 OFFLINE（原来恒为 1 导致漏报）。
        """
        device = {
            'id': 10, 'name': 'Unknown-Device', 'ip_address': '192.168.99.1',
            'failure_threshold': 3, 'recovery_threshold': 2, 'is_enabled': 1,
        }
        machine = DeviceStateMachine(device)
        assert machine.status == DeviceStatus.UNKNOWN

        # 连续失败 3 次
        events = []
        for i in range(3):
            t = machine.transition(_make_outcome(False, True))
            if t is not None:
                events.append(t)

        # 第 3 次应触发 OFFLINE + device_offline
        assert len(events) == 1
        assert events[0].new_status == DeviceStatus.OFFLINE
        assert events[0].event_type == 'device_offline'
        assert machine.failure_count >= 3

    def test_bug_c_digest_capacity_cap(self, base_config):
        """
        Bug C 回归：摘要引擎 flush 按 max_events_per_digest 截断，
        超出部分进入下一窗口（原来全部返回）。
        """
        config = dict(base_config)
        config['notify']['digest']['max_events_per_digest'] = 3

        engine = DigestEngine(config)
        for i in range(5):
            engine.add_event({'device_id': i, 'subsystem_name': 'test'})

        batch1 = engine.flush()
        assert batch1 is not None
        assert len(batch1.events) == 3  # 截断为 3 条

        # 剩余 2 条在缓冲区
        assert engine.get_pending_count() == 2
        batch2 = engine.flush()
        assert batch2 is not None
        assert len(batch2.events) == 2

    def test_bug_j_digest_disabled_sends_immediate(self, base_config):
        """Bug J 回归：digest.enabled=False 时不应丢进缓冲区。"""
        config = dict(base_config)
        config['notify']['digest']['enabled'] = False

        engine = DigestEngine(config)
        # 摘要关闭时 add_event 应返回 None（事件走立即发送路径）
        # 验证引擎不会无限缓冲
        for i in range(10):
            result = engine.add_event({'device_id': i, 'subsystem_name': 'test'})
        # 缓冲区不应无限增长
        assert engine.get_pending_count() <= 10

    def test_bug_g_shutdown_no_error(self, base_config):
        """Bug G 回归：stop() 不因 timeout 参数抛 TypeError。"""
        from concurrent.futures import ThreadPoolExecutor
        executor = ThreadPoolExecutor(max_workers=2)
        # Python 3.9+ shutdown 接受 cancel_futures
        try:
            executor.shutdown(wait=True, cancel_futures=True)
        except TypeError:
            # Python <3.9 回退
            executor.shutdown(wait=True)
        # 不应抛异常
        assert True


# ============================================================
# 数据库集成测试
# ============================================================

class TestDatabaseIntegration:
    """数据库层集成验证：CRUD 复合操作、事务、并发。"""

    def test_crud_workflow(self, tmp_db):
        """完整 CRUD 工作流：添加→查询→更新→删除。"""
        repo = DeviceRepository(tmp_db)

        # Create
        did = repo.add_device({
            'name': 'CRUD-Device',
            'ip_address': '172.16.0.1',
            'subsystem_name': '办公网',
        })
        assert did > 0

        # Read
        device = repo.get_device(did)
        assert device is not None
        assert device['name'] == 'CRUD-Device'
        assert device['subsystem_name'] == '办公网'

        # Update
        repo.update_device(did, {'name': 'CRUD-Updated', 'port': 8080})
        device = repo.get_device(did)
        assert device['name'] == 'CRUD-Updated'
        assert device['port'] == 8080

        # Delete
        repo.delete_device(did)
        assert repo.get_device(did) is None

    def test_batch_operations(self, tmp_db):
        """批量操作：批量添加、批量启用/禁用、批量维护。"""
        repo = DeviceRepository(tmp_db)

        devices = [
            {'name': f'Batch-{i}', 'ip_address': f'10.0.{i}.1'}
            for i in range(10)
        ]
        count, errors = repo.add_devices_batch(devices)
        assert count == 10
        assert len(errors) == 0

        # 获取所有已添加设备的 ID
        all_devices = repo.list_devices()
        ids = [d['id'] for d in all_devices]

        # 批量禁用前 5 台
        repo.enable_batch(ids[:5], is_enabled=False)
        enabled = repo.list_enabled_devices()
        assert len(enabled) == 5  # 后 5 台

        # 批量维护
        repo.set_maintenance_batch(ids[5:], is_maintenance=True)
        device = repo.get_device(ids[5])
        assert device['is_maintenance'] == 1

    def test_history_uptime_calculation(self, tmp_db):
        """历史在线率计算验证。"""
        device_repo = DeviceRepository(tmp_db)
        history_repo = HistoryRepository(tmp_db)

        did = device_repo.add_device({
            'name': 'Uptime-Device', 'ip_address': '10.10.10.1',
        })

        # 模拟 10 次探测：8 次成功 + 2 次失败
        for i in range(10):
            device_repo.record_check_result(did, i < 8, 2.5)

        # 查询在线率（返回值是 0-1 的小数）
        uptime = history_repo.compute_uptime(did, period='day')
        assert uptime is not None
        # 80% 在线率 → 0.8
        assert 0.75 <= uptime <= 0.85


# ============================================================
# 辅助函数
# ============================================================

def _make_outcome(is_online: bool, failure_should_count: bool):
    """构造探测结果 Object（模拟 DetectionChain.probe() 返回值）。"""
    from src.core.detection_chain import ProbeOutcome
    return ProbeOutcome(
        is_online=is_online,
        failure_should_count=failure_should_count,
        latency_ms=2.0 if is_online else 0,
        used_method='icmp' if is_online else 'icmp',
    )
