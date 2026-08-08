"""
模块：test_false_positive.py
功能：误报/漏报专项测试（项目提示词 Phase 5 Step 22）
      - 误报率 = 0：瞬时抖动/flapping 不产生告警
      - 漏报率 = 0：真实离线必告警
      - 批量离线 20/20 全部送达
"""
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from src.core.device_state_machine import DeviceStateMachine, DeviceStatus
from src.core.detection_chain import ProbeOutcome
from src.storage.database import init_database
from src.storage.repositories import DeviceRepository, AlertRepository


@pytest.fixture
def tmp_db():
    import src.storage.database as _db

    db_path = os.path.join(tempfile.gettempdir(), f'test_fp_{os.getpid()}_{id({})}.db')
    with _db._db_path_lock:
        _db._db_path_global = db_path
    conn = _db.init_database(db_path)
    yield conn
    _db.close_connection()
    with _db._db_path_lock:
        _db._db_path_global = None
    try:
        os.unlink(db_path)
    except OSError:
        pass


def _outcome(is_online, count):
    return ProbeOutcome(is_online=is_online, failure_should_count=count,
                        latency_ms=2.0 if is_online else 0, used_method='icmp')


def _add_device(device_repo, name, ip, threshold=3):
    return device_repo.add_device({
        'name': name, 'ip_address': ip,
        'failure_threshold': threshold, 'recovery_threshold': 2,
        'is_enabled': 1,
    })


class TestFalsePositive:
    """误报率 = 0：瞬时抖动不告警。"""

    def test_flapping_no_false_positive(self, tmp_db):
        device_repo = DeviceRepository(tmp_db)
        did = _add_device(device_repo, 'flap-dev', '10.0.0.1', threshold=3)
        machine = DeviceStateMachine(device_repo.get_device(did))

        # 5 轮“失败1次→恢复”抖动，任何一轮都不该达到 OFFLINE
        for _ in range(5):
            machine.transition(_outcome(False, True))
            t = machine.transition(_outcome(True, False))
            assert t.new_status == DeviceStatus.ONLINE

        # 连续 3 次失败后才应离线（验证阈值不被抖动破坏）
        t = None
        for _ in range(3):
            t = machine.transition(_outcome(False, True))
        assert t.new_status == DeviceStatus.OFFLINE
        assert t.event_type == 'device_offline'

    def test_single_failure_then_success_no_alert(self, tmp_db):
        device_repo = DeviceRepository(tmp_db)
        did = _add_device(device_repo, 'jitter-dev', '10.0.0.2', threshold=2)
        machine = DeviceStateMachine(device_repo.get_device(did))
        machine.transition(_outcome(True, False))
        machine.transition(_outcome(False, True))
        t = machine.transition(_outcome(True, False))
        assert t.new_status == DeviceStatus.ONLINE


class TestFalseNegative:
    """漏报率 = 0：真实离线必告警。"""

    def test_real_offline_always_alerts(self, tmp_db):
        device_repo = DeviceRepository(tmp_db)
        alert_repo = AlertRepository(tmp_db)
        did = _add_device(device_repo, 'offline-dev', '192.0.2.50', threshold=3)
        machine = DeviceStateMachine(device_repo.get_device(did))

        t = None
        for _ in range(3):
            t = machine.transition(_outcome(False, True))
        assert t is not None and t.event_type == 'device_offline'

        eid = alert_repo.insert_event({
            'device_id': did, 'event_type': 'offline',
            'message': f"设备 {device_repo.get_device(did)['name']} 离线",
        })
        assert eid > 0
        unacked = alert_repo.get_unacknowledged_offline_events()
        assert any(e['device_id'] == did for e in unacked)

    def test_batch_offline_20_all_delivered(self, tmp_db):
        """20 台设备同时离线 → 20/20 告警事件全部落库（零漏报）。"""
        device_repo = DeviceRepository(tmp_db)
        alert_repo = AlertRepository(tmp_db)
        ids = [_add_device(device_repo, f'batch-{i:02d}', f'192.0.2.{i+1}', threshold=1)
               for i in range(20)]

        for did in ids:
            machine = DeviceStateMachine(device_repo.get_device(did))
            t = machine.transition(_outcome(False, True))
            assert t is not None and t.event_type == 'device_offline'
            alert_repo.insert_event({
                'device_id': did, 'event_type': 'offline',
                'message': f"设备 {device_repo.get_device(did)['name']} 离线",
            })

        unacked = alert_repo.get_unacknowledged_offline_events()
        assert len(unacked) == 20, f"期望 20 条告警，实际 {len(unacked)}"
        delivered_ids = {e['device_id'] for e in unacked}
        assert delivered_ids == set(ids)
