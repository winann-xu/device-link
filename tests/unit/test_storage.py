"""
测试模块：test_storage.py
功能：数据库和设备仓库 CRUD 测试

作者：Claude
创建日期：2026-08-07
"""
import pytest
import os
import tempfile
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from src.storage.database import init_database, close_connection
from src.storage.repositories import (
    DeviceRepository, HistoryRepository, AlertRepository, ChannelRepository
)


@pytest.fixture
def db():
    """创建临时 SQLite 数据库并初始化。"""
    fd, path = tempfile.mkstemp(suffix='.db')
    os.close(fd)
    conn = init_database(path)
    yield conn, path
    close_connection()
    if os.path.exists(path):
        os.unlink(path)


class TestDeviceRepository:
    """设备仓库 CRUD 测试。"""

    def test_add_and_get(self, db):
        conn, _ = db
        repo = DeviceRepository(conn)
        did = repo.add_device({
            'name': 'test-device', 'ip_address': '192.168.1.1',
            'subsystem_name': 'MES'
        })
        assert did > 0
        dev = repo.get_device(did)
        assert dev['name'] == 'test-device'
        assert dev['ip_address'] == '192.168.1.1'
        assert dev['is_enabled'] == 1  # 默认启用
        assert dev['status'] == 'unknown'

    def test_update(self, db):
        conn, _ = db
        repo = DeviceRepository(conn)
        did = repo.add_device({'name': 'test', 'ip_address': '10.0.0.1'})
        repo.update_device(did, {'name': 'renamed', 'ip_address': '10.0.0.2'})
        dev = repo.get_device(did)
        assert dev['name'] == 'renamed'
        assert dev['ip_address'] == '10.0.0.2'

    def test_delete(self, db):
        conn, _ = db
        repo = DeviceRepository(conn)
        did = repo.add_device({'name': 'del-me', 'ip_address': '1.1.1.1'})
        assert repo.delete_device(did) is True
        assert repo.get_device(did) is None

    def test_delete_batch(self, db):
        conn, _ = db
        repo = DeviceRepository(conn)
        ids = []
        for i in range(5):
            ids.append(repo.add_device({'name': f'd{i}', 'ip_address': f'10.0.0.{i}'}))
        count = repo.delete_devices(ids[:3])
        assert count == 3
        assert repo.get_device(ids[0]) is None
        assert repo.get_device(ids[3]) is not None

    def test_list_devices(self, db):
        conn, _ = db
        repo = DeviceRepository(conn)
        repo.add_device({'name': 'a', 'ip_address': '10.0.0.1', 'subsystem_name': 'MES'})
        repo.add_device({'name': 'b', 'ip_address': '10.0.0.2', 'subsystem_name': 'SCADA'})
        all_devs = repo.list_devices()
        assert len(all_devs) >= 2
        mes_devs = repo.list_devices(subsystem='MES')
        assert all(d['subsystem_name'] == 'MES' for d in mes_devs)

    def test_list_enabled_devices(self, db):
        conn, _ = db
        repo = DeviceRepository(conn)
        repo.add_device({'name': 'enabled', 'ip_address': '10.0.0.1'})
        did = repo.add_device({'name': 'disabled', 'ip_address': '10.0.0.2', 'is_enabled': 0})
        enabled = repo.list_enabled_devices()
        assert any(d['name'] == 'enabled' for d in enabled)
        assert not any(d['name'] == 'disabled' for d in enabled)

    def test_set_device_status(self, db):
        conn, _ = db
        repo = DeviceRepository(conn)
        did = repo.add_device({'name': 'status-test', 'ip_address': '10.0.0.1'})
        repo.set_device_status(did, 'online', 0, 0, 3.5)
        dev = repo.get_device(did)
        assert dev['status'] == 'online'
        assert dev['latency_ms'] == 3.5

    def test_maintenance_batch(self, db):
        conn, _ = db
        repo = DeviceRepository(conn)
        ids = [repo.add_device({'name': f'm{i}', 'ip_address': f'10.0.0.{i}'}) for i in range(3)]
        repo.set_maintenance_batch(ids, True)
        for did in ids:
            assert repo.get_device(did)['is_maintenance'] == 1
        repo.set_maintenance_batch(ids, False)
        for did in ids:
            assert repo.get_device(did)['is_maintenance'] == 0

    def test_enable_batch(self, db):
        conn, _ = db
        repo = DeviceRepository(conn)
        ids = [repo.add_device({'name': f'e{i}', 'ip_address': f'10.0.0.{i}'}) for i in range(3)]
        repo.enable_batch(ids, False)
        for did in ids:
            assert repo.get_device(did)['is_enabled'] == 0
        repo.enable_batch(ids, True)
        for did in ids:
            assert repo.get_device(did)['is_enabled'] == 1

    def test_add_devices_batch(self, db):
        conn, _ = db
        repo = DeviceRepository(conn)
        batch = [
            {'name': 'b1', 'ip_address': '10.0.1.1'},
            {'name': 'b2', 'ip_address': '10.0.1.2'},
        ]
        ok, failed = repo.add_devices_batch(batch)
        assert ok == 2
        assert len(failed) == 0

    def test_record_check_result(self, db):
        conn, _ = db
        repo = DeviceRepository(conn)
        did = repo.add_device({'name': 'h', 'ip_address': '10.0.0.1'})
        repo.record_check_result(did, True, 2.5)
        repo.record_check_result(did, False, 0)
        # 历史记录已写入（通过 HistoryRepository 查询验证）
        hist = HistoryRepository(conn)
        rows = hist.query_status_range(did, '2020-01-01 00:00:00', '2099-01-01 00:00:00')
        assert len(rows) >= 2


class TestHistoryRepository:
    """历史仓库测试。"""

    @pytest.fixture
    def device_id(self, db):
        """创建一个测试设备并返回其 ID。"""
        conn, _ = db
        repo = DeviceRepository(conn)
        return repo.add_device({'name': 'hist-dev', 'ip_address': '10.0.0.99'})

    def test_insert_and_query(self, db, device_id):
        conn, _ = db
        hist = HistoryRepository(conn)
        hist.insert_status(device_id, 'online', 5.0)
        hist.insert_status(device_id, 'offline', 0)
        rows = hist.query_status_range(device_id, '2020-01-01', '2099-01-01')
        assert len(rows) == 2
        assert rows[0]['status'] == 'online'
        assert rows[1]['status'] == 'offline'

    def test_compute_uptime(self, db, device_id):
        conn, _ = db
        repo = DeviceRepository(conn)
        for _ in range(8):
            repo.record_check_result(device_id, True, 1.0)
        for _ in range(2):
            repo.record_check_result(device_id, False, 0)
        hist = HistoryRepository(conn)
        uptime = hist.compute_uptime(device_id, 'day')
        assert 0.7 < uptime < 0.9

    def test_offline_toplist(self, db, device_id):
        conn, _ = db
        repo = DeviceRepository(conn)
        hist = HistoryRepository(conn)
        for _ in range(5):
            repo.record_check_result(device_id, False, 0)
        toplist = hist.get_offline_toplist(7, 10)
        assert len(toplist) >= 1
        assert toplist[0]['name'] == 'hist-dev'

    def test_cleanup_expired(self, db, device_id):
        conn, _ = db
        hist = HistoryRepository(conn)
        count = hist.cleanup_expired(0)
        assert count >= 0


class TestAlertRepository:
    """告警仓库测试。"""

    @pytest.fixture
    def device_id(self, db):
        """创建一个测试设备。"""
        conn, _ = db
        repo = DeviceRepository(conn)
        return repo.add_device({'name': 'alert-dev', 'ip_address': '10.0.0.100'})

    def test_insert_and_list(self, db, device_id):
        conn, _ = db
        alert = AlertRepository(conn)
        eid = alert.insert_event({
            'device_id': device_id, 'event_type': 'offline',
            'message': 'test alert', 'notified_channels': 'email,feishu',
            'notify_success': 2
        })
        assert eid > 0
        events = alert.list_events(device_id=device_id, limit=10)
        assert len(events) >= 1
        assert events[0]['event_type'] == 'offline'

    def test_acknowledge(self, db, device_id):
        conn, _ = db
        alert = AlertRepository(conn)
        eid = alert.insert_event({
            'device_id': device_id, 'event_type': 'offline', 'message': 'ack test'
        })
        alert.acknowledge(eid, 'admin')
        events = alert.list_events(acknowledged=True, limit=10)
        assert any(e['id'] == eid for e in events)

    def test_get_unacknowledged(self, db, device_id):
        conn, _ = db
        alert = AlertRepository(conn)
        alert.insert_event({'device_id': device_id, 'event_type': 'offline'})
        alert.insert_event({'device_id': device_id, 'event_type': 'offline'})
        unacked = alert.get_unacknowledged_offline_events()
        assert len(unacked) >= 2


class TestChannelRepository:
    """通道仓库测试。"""

    def test_save_and_get(self, db):
        conn, _ = db
        ch = ChannelRepository(conn)
        ch.save_channel({'channel_type': 'email', 'name': 'test-email',
                         'config_json': '{}', 'is_enabled': 1})
        enabled = ch.get_enabled_channels()
        assert any(c['name'] == 'test-email' for c in enabled)

    def test_update_last_test(self, db):
        conn, _ = db
        ch = ChannelRepository(conn)
        ch.save_channel({'channel_type': 'feishu', 'name': 'test-feishu'})
        enabled = ch.get_enabled_channels()
        if enabled:
            ch.update_last_test(enabled[0]['id'], True)
            updated = ch.get_enabled_channels()
            assert updated[0]['last_test_success'] == 1


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
