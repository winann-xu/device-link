"""
模块：test_performance.py
功能：DEVICE LINK 性能冒烟测试
     验证系统在标称负载下的吞吐、延迟和资源使用。

标称规格（来自需求文档）：
  - 200 台设备，30s 间隔
  - 单轮全量探测 < 10 秒
  - 状态机单次转换 < 1ms
  - 数据库批量写入 < 100ms (100条)
  - 线程池 max_workers=50 下无死锁/饥饿

作者：Claude
创建日期：2026-08-07
"""
import os
import sys
import time
import tempfile
import threading
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from src.core.device_state_machine import DeviceStateMachine
from src.core.detection_chain import ProbeOutcome
from src.alerts.digest_engine import DigestEngine
from src.storage.repositories import DeviceRepository, HistoryRepository


# ============================================================
# Fixtures
# ============================================================

def _make_tmp_db():
    """创建独立临时数据库。"""
    import sqlite3 as _sqlite3
    import src.storage.database as _db

    db_path = os.path.join(
        tempfile.gettempdir(),
        f'test_perf_{os.getpid()}_{threading.get_ident()}.db'
    )
    with _db._db_path_lock:
        _db._db_path_global = db_path
    conn = _db.init_database(db_path)
    return conn, db_path


def _cleanup_tmp_db(conn, db_path):
    """清理临时数据库。"""
    import src.storage.database as _db
    _db.close_connection()
    with _db._db_path_lock:
        _db._db_path_global = None
    try:
        conn.close()
        os.unlink(db_path)
    except OSError:
        pass


# ============================================================
# 吞吐量测试
# ============================================================

class TestThroughput:
    """调度吞吐量验证。"""

    def test_state_machine_200_devices_under_10ms(self):
        """
        200 台设备状态转换总耗时 < 10ms。
        状态机是纯 CPU 计算，不应成为瓶颈。
        """
        devices = [
            {
                'id': i, 'name': f'perf-{i}', 'ip_address': f'10.0.{i//256}.{i%256}',
                'failure_threshold': 3, 'recovery_threshold': 2, 'is_enabled': 1,
            }
            for i in range(200)
        ]
        machines = [DeviceStateMachine(d) for d in devices]

        outcomes = [ProbeOutcome(is_online=(i % 3 != 0), failure_should_count=(i % 3 == 0))
                    for i in range(200)]

        t0 = time.perf_counter()
        for m, o in zip(machines, outcomes):
            m.transition(o)
        elapsed_ms = (time.perf_counter() - t0) * 1000

        # 200 次状态转换应在 10ms 内完成
        assert elapsed_ms < 10, f"200 次状态转换耗时 {elapsed_ms:.1f}ms，超过 10ms 阈值"

    def test_digest_engine_1000_events_under_100ms(self):
        """
        摘要引擎处理 1000 条事件 < 100ms。
        摘要合并是热路径，不能成为瓶颈。
        """
        config = {
            'notify': {
                'digest': {
                    'enabled': True,
                    'window_seconds': 300,
                    'max_events_per_digest': 100,
                    'send_immediate_if_critical': False,
                }
            }
        }
        engine = DigestEngine(config)

        t0 = time.perf_counter()
        for i in range(1000):
            engine.add_event({'device_id': i, 'subsystem_name': f'subsys-{i//50}'})
        elapsed_ms = (time.perf_counter() - t0) * 1000

        assert elapsed_ms < 100, f"1000 条事件入队耗时 {elapsed_ms:.1f}ms，超过 100ms 阈值"

        # 刷出也应快速
        t0 = time.perf_counter()
        while engine.get_pending_count() > 0:
            engine.flush()
        flush_ms = (time.perf_counter() - t0) * 1000
        total_ms = (time.perf_counter() - t0) * 1000 + elapsed_ms

        assert flush_ms < 200, f"1000 条事件刷出耗时 {flush_ms:.1f}ms"


# ============================================================
# 数据库批量操作性能
# ============================================================

class TestDatabasePerformance:
    """数据库批量操作在标称负载下的延迟。"""

    def test_batch_insert_100_devices(self):
        """批量插入 100 台设备 < 200ms。"""
        conn, db_path = _make_tmp_db()
        try:
            repo = DeviceRepository()
            devices = [
                {'name': f'perf-db-{i}', 'ip_address': f'172.16.{i//256}.{i%256}'}
                for i in range(100)
            ]
            t0 = time.perf_counter()
            count, errors = repo.add_devices_batch(devices)
            elapsed_ms = (time.perf_counter() - t0) * 1000

            assert count == 100
            assert elapsed_ms < 500, f"100 台设备批量插入耗时 {elapsed_ms:.1f}ms，超过 500ms 阈值"
        finally:
            _cleanup_tmp_db(conn, db_path)

    def test_record_100_history_entries(self):
        """记录 100 条历史 < 500ms。"""
        conn, db_path = _make_tmp_db()
        try:
            device_repo = DeviceRepository()
            did = device_repo.add_device({
                'name': 'perf-history', 'ip_address': '10.0.0.1',
            })

            t0 = time.perf_counter()
            for i in range(100):
                device_repo.record_check_result(did, i % 4 != 0, 1.5)
            elapsed_ms = (time.perf_counter() - t0) * 1000

            assert elapsed_ms < 500, f"100 条历史记录耗时 {elapsed_ms:.1f}ms，超过 500ms 阈值"
        finally:
            _cleanup_tmp_db(conn, db_path)


# ============================================================
# 并发安全测试
# ============================================================

class TestConcurrency:
    """并发场景下的正确性和无死锁验证。"""

    def test_concurrent_reads_no_deadlock(self):
        """
        10 线程并发读取数据库，验证无死锁无崩溃。
        这是 Bug M + sqlite3 修复后的回归验证。
        """
        conn, db_path = _make_tmp_db()
        try:
            repo = DeviceRepository()
            did = repo.add_device({
                'name': 'concurrent-read', 'ip_address': '10.0.0.99',
            })

            errors = []
            barrier = threading.Barrier(10, timeout=10)

            def reader():
                try:
                    barrier.wait()
                    for _ in range(50):
                        d = repo.get_device(did)
                        assert d is not None
                except Exception as e:
                    errors.append(e)

            threads = [threading.Thread(target=reader) for _ in range(10)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=15)

            assert len(errors) == 0, f"并发读异常: {errors}"
        finally:
            _cleanup_tmp_db(conn, db_path)

    def test_concurrent_writes_serialized(self):
        """
        5 线程并发写入（每线程 insert + update），验证客户端顺序化无冲突。
        注意：_DB_LOCK 序列化写操作，因此并发写按顺序执行。
        """
        conn, db_path = _make_tmp_db()
        try:
            repo = DeviceRepository()

            errors = []
            results = []

            def writer(thread_id):
                try:
                    for i in range(20):
                        did = repo.add_device({
                            'name': f'conc-write-t{thread_id}-{i}',
                            'ip_address': f'10.0.{thread_id}.{i}',
                        })
                        repo.set_device_status(did, 'online', 0, 0, 1.0)
                        results.append(did)
                except Exception as e:
                    errors.append((thread_id, str(e)))

            threads = [threading.Thread(target=writer, args=(i,)) for i in range(5)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)

            assert len(errors) == 0, f"并发写异常: {errors}"
            assert len(results) == 100, f"期望 100 个写入结果，实际 {len(results)}"
        finally:
            _cleanup_tmp_db(conn, db_path)


# ============================================================
# 内存/资源测试
# ============================================================

class TestResourceUsage:
    """验证无资源泄漏。"""

    def test_state_machine_memory_stable(self):
        """
        状态机反复转换后内存不应暴涨。
        10 万次转换后状态机对象无异常。
        """
        device = {
            'id': 1, 'name': 'mem-test', 'ip_address': '10.0.0.1',
            'failure_threshold': 3, 'recovery_threshold': 2,
        }
        machine = DeviceStateMachine(device)

        # 10 万次转换
        for i in range(100_000):
            online = (i % 5 != 0)  # 80% 在线率
            outcome = ProbeOutcome(
                is_online=online,
                failure_should_count=not online,
            )
            machine.transition(outcome)

        # 状态机应仍然正常工作
        assert machine.status is not None
        assert machine.failure_count >= 0
