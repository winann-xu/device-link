"""
测试模块：test_retention_and_index.py
功能：v1.0.11 两项改造的回归测试
     1) 历史数据自动清理：默认只保留最近 30 天（分批删除、兼容旧配置）
     2) 历史统计索引优化：性能索引到位、冗余索引清除、统计查询命中索引

作者：Claude
创建日期：2026-09-22
"""
import json
import os
import re
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from src.storage.database import (
    init_database, close_connection, ensure_indexes,
    PERFORMANCE_INDEX_DDL, REDUNDANT_INDEXES,
)
from src.storage.repositories import (
    DeviceRepository, HistoryRepository, AlertRepository,
)
from src.storage import maintenance as mnt

TS_FMT = '%Y-%m-%d %H:%M:%S'


def ts(days_ago: float = 0, hours_ago: float = 0) -> str:
    """生成距今指定天/小时的本地时间字符串。"""
    delta = timedelta(days=days_ago, hours=hours_ago)
    return (datetime.now() - delta).strftime(TS_FMT)


def insert_history(conn, device_id, checked_at, status='online', latency=1.0):
    """按指定时间写入一条状态历史（默认值 datetime('now') 无法回溯，故显式指定）。"""
    conn.execute(
        "INSERT INTO status_history (device_id, status, latency_ms, checked_at) VALUES (?,?,?,?)",
        (device_id, status, latency, checked_at)
    )
    conn.commit()


def count_history(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM status_history").fetchone()[0]


@pytest.fixture
def db():
    """独立临时数据库。"""
    fd, path = tempfile.mkstemp(suffix='.db')
    os.close(fd)
    conn = init_database(path)
    yield conn, path
    close_connection()
    for suffix in ('', '-wal', '-shm'):
        try:
            os.unlink(path + suffix)
        except OSError:
            pass


@pytest.fixture
def device_id(db):
    conn, _ = db
    return DeviceRepository().add_device({'name': 'ret-dev', 'ip_address': '10.9.9.9'})


# ============================================================
# 一、保留期配置解析
# ============================================================

class TestRetentionConfig:

    def test_default_retention_is_30_days(self):
        cfg = mnt.resolve_cleanup_config({})
        assert cfg['retention_days'] == 30
        assert cfg['enabled'] is True
        assert cfg['interval_minutes'] == 60
        assert cfg['batch_size'] == 5000
        assert cfg['vacuum_enabled'] is False   # 默认不擅自 VACUUM（会暂停探测）

    def test_legacy_default_90_upgraded_to_30(self, caplog):
        """旧版 config.yaml 只有 history_retention_days: 90（无 cleanup 段）→ 按 30 天执行。"""
        import logging
        with caplog.at_level(logging.WARNING, logger='device-link.maintenance'):
            cfg = mnt.resolve_cleanup_config({'storage': {'history_retention_days': 90}})
        assert cfg['retention_days'] == 30
        assert any('90' in r.message for r in caplog.records)

    def test_legacy_custom_value_honored(self):
        """用户显式改过的保留期（非 90）原样保留。"""
        assert mnt.resolve_cleanup_config(
            {'storage': {'history_retention_days': 180}})['retention_days'] == 180

    def test_new_style_explicit_90_honored(self):
        """新版配置里显式写 cleanup.retention_days: 90 → 尊重用户设置。"""
        cfg = mnt.resolve_cleanup_config(
            {'storage': {'history_retention_days': 90, 'cleanup': {'retention_days': 90}}})
        assert cfg['retention_days'] == 90

    def test_cleanup_can_be_disabled(self):
        cfg = mnt.resolve_cleanup_config(
            {'storage': {'cleanup': {'enabled': False, 'retention_days': 15}}})
        assert cfg['enabled'] is False
        assert cfg['retention_days'] == 15

    def test_bad_values_fall_back_to_defaults(self):
        cfg = mnt.resolve_cleanup_config(
            {'storage': {'cleanup': {'retention_days': 'abc', 'batch_size': None,
                                     'interval_minutes': 'x'}}})
        assert cfg['retention_days'] == 30
        assert cfg['batch_size'] == 5000
        assert cfg['interval_minutes'] == 60

    def test_cutoff_string(self):
        cutoff = mnt.cutoff_string(30)
        assert datetime.strptime(cutoff, TS_FMT) < datetime.now() - timedelta(days=29)


# ============================================================
# 二、分批清理
# ============================================================

class TestPruneStatusHistory:

    def test_only_expired_rows_removed(self, db, device_id):
        conn, _ = db
        insert_history(conn, device_id, ts(days_ago=45))
        insert_history(conn, device_id, ts(days_ago=31))
        insert_history(conn, device_id, ts(days_ago=29))
        insert_history(conn, device_id, ts(hours_ago=1))

        deleted = mnt.prune_status_history(conn, retention_days=30, batch_sleep_ms=0)

        assert deleted == 2
        assert count_history(conn) == 2
        oldest = conn.execute("SELECT MIN(checked_at) FROM status_history").fetchone()[0]
        assert oldest >= mnt.cutoff_string(30)

    def test_batched_delete_covers_all_rows(self, db, device_id):
        """batch_size=2 时要能删完 5 条过期记录（多批循环）。"""
        conn, _ = db
        for days in (60, 50, 40, 35, 31):
            insert_history(conn, device_id, ts(days_ago=days))
        insert_history(conn, device_id, ts(days_ago=1))

        deleted = mnt.prune_status_history(
            conn, retention_days=30, batch_size=2, batch_sleep_ms=0)

        assert deleted == 5
        assert count_history(conn) == 1

    def test_no_expired_data_returns_zero(self, db, device_id):
        conn, _ = db
        insert_history(conn, device_id, ts(days_ago=3))
        assert mnt.prune_status_history(conn, retention_days=30, batch_sleep_ms=0) == 0
        assert count_history(conn) == 1

    def test_empty_table_returns_zero(self, db):
        conn, _ = db
        assert mnt.prune_status_history(conn, retention_days=30, batch_sleep_ms=0) == 0

    def test_cleanup_expired_uses_new_retention(self, db, device_id):
        """仓储层 cleanup_expired：默认保留期改为 30 天，且为分批实现。"""
        conn, _ = db
        insert_history(conn, device_id, ts(days_ago=40))
        insert_history(conn, device_id, ts(days_ago=5))
        hist = HistoryRepository()
        assert hist.cleanup_expired() == 1          # 默认 30 天
        assert count_history(conn) == 1

    def test_cleanup_expired_zero_days_clears_all(self, db, device_id):
        """兼容旧调用语义：retention_days=0 时清空全部历史记录。"""
        conn, _ = db
        insert_history(conn, device_id, ts(hours_ago=2))
        insert_history(conn, device_id, ts(hours_ago=1))
        assert HistoryRepository().cleanup_expired(0) == 2
        assert count_history(conn) == 0


class TestPruneAlertEvents:

    def _insert_event(self, conn, device_id, created_at, ack):
        cur = conn.execute(
            """INSERT INTO alert_events (device_id, event_type, message, is_acknowledged, created_at)
               VALUES (?, 'offline', 'test', ?, ?)""",
            (device_id, 1 if ack else 0, created_at))
        conn.commit()
        return cur.lastrowid

    def test_only_old_acknowledged_events_removed(self, db, device_id):
        conn, _ = db
        old_ack = self._insert_event(conn, device_id, ts(days_ago=40), True)
        old_unack = self._insert_event(conn, device_id, ts(days_ago=40), False)
        recent_ack = self._insert_event(conn, device_id, ts(days_ago=2), True)

        deleted = mnt.prune_alert_events(conn, retention_days=30, batch_sleep_ms=0)

        remaining = {r[0] for r in conn.execute("SELECT id FROM alert_events").fetchall()}
        assert deleted == 1
        assert remaining == {old_unack, recent_ack}   # 未确认事件保留给升级逻辑
        assert old_ack not in remaining


# ============================================================
# 三、索引
# ============================================================

class TestIndexes:

    def _index_names(self, conn):
        return {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'").fetchall()}

    def test_performance_indexes_created_on_init(self, db):
        conn, _ = db
        names = self._index_names(conn)
        missing = [n for n in PERFORMANCE_INDEX_DDL if n not in names]
        assert missing == []

    def test_ensure_indexes_is_idempotent(self, db):
        conn, _ = db
        result = ensure_indexes(conn)
        assert result['created'] == []
        assert result['dropped'] == []

    def test_legacy_device_index_kept(self, db):
        """老的两列索引保留（单设备区间查询/回退统计仍用它），不额外加宽索引。"""
        conn, _ = db
        names = self._index_names(conn)
        assert 'idx_sh_device_time' in names
        assert 'idx_sh_device_time_status' not in names   # 三列覆盖索引体积代价不划算
        assert ensure_indexes(conn)['dropped'] == []
        assert REDUNDANT_INDEXES == ()

    def test_fresh_db_uses_incremental_vacuum(self, db):
        conn, _ = db
        assert conn.execute("PRAGMA auto_vacuum").fetchone()[0] == 2  # INCREMENTAL

    def test_journal_size_limit_applied(self, db):
        conn, _ = db
        assert conn.execute("PRAGMA journal_size_limit").fetchone()[0] == 67108864

    def test_stats_queries_use_indexes(self, db, device_id):
        """历史统计三类查询都必须走索引（不允许全表扫描）。"""
        conn, _ = db
        for days in range(0, 40):
            insert_history(conn, device_id, ts(days_ago=days), 'online')
            insert_history(conn, device_id, ts(days_ago=days), 'offline')

        plans = {
            'overall': conn.execute(
                "EXPLAIN QUERY PLAN SELECT COUNT(*), SUM(status='online') FROM status_history "
                "WHERE checked_at >= ?", ('2026-01-01 00:00:00',)).fetchall(),
            'device': conn.execute(
                "EXPLAIN QUERY PLAN SELECT COUNT(*), SUM(status='online') FROM status_history "
                "WHERE device_id=? AND checked_at >= ?", (device_id, '2026-01-01 00:00:00')).fetchall(),
            'offline_toplist': conn.execute(
                """EXPLAIN QUERY PLAN SELECT d.id, COALESCE(o.cnt, 0) FROM devices d
                   LEFT JOIN (SELECT device_id, COUNT(*) AS cnt FROM status_history
                                WHERE status='offline' AND checked_at >= ?
                                GROUP BY device_id) o ON o.device_id = d.id
                   ORDER BY o.cnt DESC LIMIT 10""",
                ('2026-01-01 00:00:00',)).fetchall(),
            'prune': conn.execute(
                "EXPLAIN QUERY PLAN SELECT id FROM status_history "
                "WHERE checked_at < ? ORDER BY checked_at LIMIT 5000",
                ('2026-01-01 00:00:00',)).fetchall(),
        }
        detail = {k: ' '.join(str(r[-1]) for r in v) for k, v in plans.items()}

        assert 'idx_sh_checked_status' in detail['overall'], detail['overall']
        assert 'idx_sh_device_time' in detail['device'], detail['device']
        assert 'idx_sh_offline' in detail['offline_toplist'], detail['offline_toplist']
        assert 'idx_sh_checked_status' in detail['prune'], detail['prune']
        # 不允许出现"不带索引的全表扫描"（走覆盖索引的索引扫描是允许的，
        # 例如离线排行榜就是扫 idx_sh_offline 这个只含离线行的小索引）
        for name, text in detail.items():
            assert not re.search(r'SCAN status_history(?! USING)', text), (name, text)

    def test_alert_event_queries_use_indexes(self, db, device_id):
        conn, _ = db
        for days in (40, 2):
            conn.execute(
                "INSERT INTO alert_events (device_id, event_type, is_acknowledged, created_at) "
                "VALUES (?, 'offline', 0, ?)", (device_id, ts(days_ago=days)))
        conn.commit()

        plans = {
            'list': conn.execute(
                "EXPLAIN QUERY PLAN SELECT * FROM alert_events ORDER BY created_at DESC LIMIT 200"
            ).fetchall(),
            'escalation': conn.execute(
                "EXPLAIN QUERY PLAN SELECT * FROM alert_events WHERE event_type='offline' "
                "AND is_acknowledged=0 ORDER BY created_at DESC").fetchall(),
        }
        detail = {k: ' '.join(str(r[-1]) for r in v) for k, v in plans.items()}
        # 告警日志按时间倒序取 200 条：走时间索引（SCAN ... USING INDEX 即按索引有序扫描）
        assert 'USING INDEX idx_ae_created_at' in detail['list'], detail['list']
        assert 'USING INDEX idx_ae_pending_escalation' in detail['escalation'], detail['escalation']


# ============================================================
# 四、统计查询正确性
# ============================================================

class TestUptimeSummary:

    def test_summary_matches_individual_methods(self, db, device_id):
        conn, _ = db
        rows = [
            (0.2, 'online'), (0.5, 'offline'),
            (2, 'online'), (2, 'offline'), (3, 'online'),
            (10, 'online'), (12, 'offline'), (20, 'online'), (25, 'online'),
            (40, 'offline'),          # 30 天窗口外，不计入任何周期
        ]
        for days_ago, status in rows:
            insert_history(conn, device_id, ts(days_ago=days_ago), status)

        hist = HistoryRepository()
        summary = hist.compute_uptime_summary(device_id)
        for period in ('day', 'week', 'month'):
            assert summary[period] == pytest.approx(hist.compute_uptime(device_id, period))

    def test_overall_summary_matches_overall_method(self, db):
        conn, _ = db
        repo = DeviceRepository()
        d1 = repo.add_device({'name': 'dev-a', 'ip_address': '10.0.0.1'})
        d2 = repo.add_device({'name': 'dev-b', 'ip_address': '10.0.0.2'})
        for device, offsets in ((d1, [0.1, 1, 5, 20]), (d2, [0.3, 9, 15])):
            for days_ago in offsets:
                insert_history(conn, device, ts(days_ago=days_ago), 'online')
        insert_history(conn, d2, ts(days_ago=2), 'offline')

        hist = HistoryRepository()
        summary = hist.compute_uptime_summary(None)
        for period in ('day', 'week', 'month'):
            assert summary[period] == pytest.approx(hist.compute_overall_uptime(period))

    def test_empty_history_returns_zeros(self, db, device_id):
        summary = HistoryRepository().compute_uptime_summary(device_id)
        assert summary == {'day': 0.0, 'week': 0.0, 'month': 0.0}
        assert HistoryRepository().compute_uptime_summary(None) == {
            'day': 0.0, 'week': 0.0, 'month': 0.0}

    def test_all_offline_is_zero_percent(self, db, device_id):
        conn, _ = db
        insert_history(conn, device_id, ts(days_ago=1), 'offline')
        assert HistoryRepository().compute_uptime_summary(device_id)['day'] == 0.0
        assert HistoryRepository().compute_uptime_summary(device_id)['month'] == 0.0

    def test_offline_toplist_stable_ordering(self, db):
        conn, _ = db
        repo = DeviceRepository()
        for name in ('zeta', 'alpha', 'beta'):
            did = repo.add_device({'name': name, 'ip_address': '10.0.0.9'})
            insert_history(conn, did, ts(days_ago=1), 'offline')
        toplist = HistoryRepository().get_offline_toplist(7, 10)
        assert [t['name'] for t in toplist[:3]] == ['alpha', 'beta', 'zeta']  # 同分按名称稳定


# ============================================================
# 五、后台清理线程
# ============================================================

class TestRetentionWorker:

    def _config(self, **cleanup):
        base = {'enabled': True, 'retention_days': 30, 'interval_minutes': 60,
                'batch_size': 500, 'batch_sleep_ms': 0}
        base.update(cleanup)
        return {'storage': {'cleanup': base}}

    def test_run_once_deletes_and_writes_state(self, db, device_id, tmp_path):
        conn, _ = db
        for days in (40, 35, 31):
            insert_history(conn, device_id, ts(days_ago=days))
        insert_history(conn, device_id, ts(days_ago=1))
        state_file = str(tmp_path / 'retention_state.json')

        worker = mnt.DataRetentionWorker(
            self._config(), state_file=state_file, conn_factory=lambda: conn)
        result = worker.run_once()

        assert result['history_deleted'] == 3
        assert result['retention_days'] == 30
        assert count_history(conn) == 1
        payload = json.loads(open(state_file, encoding='utf-8').read())
        assert payload['history_deleted'] == 3

    def test_start_runs_immediately_then_stops(self, db, device_id, tmp_path):
        """真实线程路径：工作线程通过 get_connection() 取自己的线程本地连接。"""
        conn, _ = db
        insert_history(conn, device_id, ts(days_ago=45))
        worker = mnt.DataRetentionWorker(
            self._config(), state_file=str(tmp_path / 'state.json'))
        assert worker.start() is True
        deadline = time.time() + 15
        while worker.last_result is None and time.time() < deadline:
            time.sleep(0.1)
        worker.stop()

        assert worker.last_result is not None
        assert worker.last_result['history_deleted'] == 1
        assert worker._thread is None

    def test_start_returns_false_when_disabled(self, db):
        worker = mnt.DataRetentionWorker(self._config(enabled=False))
        assert worker.enabled is False
        assert worker.start() is False

    def test_worker_error_does_not_raise(self, tmp_path):
        def boom():
            raise sqlite3.OperationalError('database is locked')
        worker = mnt.DataRetentionWorker(
            self._config(), state_file=str(tmp_path / 'state.json'), conn_factory=boom)
        result = worker.run_once()
        assert 'error' in result
        assert result['history_deleted'] == 0

    def test_retention_days_property(self):
        worker = mnt.DataRetentionWorker(self._config(retention_days=15))
        assert worker.retention_days == 15


# ============================================================
# 六、离线维护命令（--vacuum-now）
# ============================================================

class TestMaintenanceCommand:

    def test_vacuum_now_prunes_and_shrinks(self):
        fd, path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        conn = init_database(path)
        try:
            device_id = DeviceRepository().add_device(
                {'name': 'vac-dev', 'ip_address': '10.1.1.1'})
            # 3 万行过期数据 + 少量近期数据，制造明显的空闲页
            rows = []
            for i in range(30000):
                rows.append((device_id, 'online', 1.0, ts(days_ago=31 + (i % 30))))
            conn.executemany(
                "INSERT INTO status_history (device_id, status, latency_ms, checked_at) "
                "VALUES (?,?,?,?)", rows)
            conn.commit()
            insert_history(conn, device_id, ts(days_ago=1))

            def total_size():
                """主库 + WAL 合计（未 checkpoint 的写入仍留在 -wal 里）。"""
                return sum(os.path.getsize(path + sfx) if os.path.exists(path + sfx) else 0
                           for sfx in ('', '-wal', '-shm'))

            size_before = total_size()

            config = {'storage': {'cleanup': {'retention_days': 30, 'batch_size': 5000,
                                              'batch_sleep_ms': 0}}}
            code = mnt.run_maintenance_command(config, conn)

            assert code == 0
            assert count_history(conn) == 1
            assert total_size() < size_before
        finally:
            close_connection()
            for suffix in ('', '-wal', '-shm'):
                try:
                    os.unlink(path + suffix)
                except OSError:
                    pass

    def test_vacuum_now_refuses_when_db_busy(self):
        fd, path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        conn = init_database(path)
        other = sqlite3.connect(path, timeout=0.2)
        try:
            other.execute("BEGIN IMMEDIATE")   # 模拟另一实例持有写事务
            code = mnt.run_maintenance_command({'storage': {}}, conn)
            assert code == 1
        finally:
            other.rollback()
            other.close()
            close_connection()
            for suffix in ('', '-wal', '-shm'):
                try:
                    os.unlink(path + suffix)
                except OSError:
                    pass


# ============================================================
# 七、性能冒烟：索引生效后统计查询应显著低于阈值
# ============================================================

class TestStatsPerformanceSmoke:

    def test_summary_and_toplist_are_fast_on_50k_rows(self, db):
        conn, _ = db
        repo = DeviceRepository()
        device_ids = [repo.add_device(
            {'name': f'perf-{i}', 'ip_address': f'10.2.{i // 250}.{i % 250}'})
            for i in range(50)]
        rows = []
        for i, did in enumerate(device_ids):
            for day in range(0, 40):
                for hour in range(0, 24):
                    status = 'offline' if (i + day + hour) % 37 == 0 else 'online'
                    rows.append((did, status, 1.0, ts(days_ago=day, hours_ago=hour)))
        conn.executemany(
            "INSERT INTO status_history (device_id, status, latency_ms, checked_at) "
            "VALUES (?,?,?,?)", rows)
        conn.commit()
        total_rows = count_history(conn)
        assert total_rows == 50 * 40 * 24

        hist = HistoryRepository()
        t0 = time.perf_counter()
        for _ in range(10):
            hist.compute_uptime_summary(None)
        overall_elapsed = time.perf_counter() - t0

        t0 = time.perf_counter()
        for did in device_ids[:10]:
            hist.compute_uptime_summary(did)
        per_device_elapsed = time.perf_counter() - t0

        t0 = time.perf_counter()
        hist.get_offline_toplist(30, 10)
        toplist_elapsed = time.perf_counter() - t0

        # 阈值放宽（CI 机器差异大），只用于拦住"退化成全表扫描"的回归
        assert overall_elapsed < 5.0, f'全局统计 10 次耗时 {overall_elapsed:.2f}s'
        assert per_device_elapsed < 2.0, f'单设备统计 10 次耗时 {per_device_elapsed:.2f}s'
        assert toplist_elapsed < 2.0, f'离线排行榜耗时 {toplist_elapsed:.2f}s'


# ============================================================
# 八、每日统计汇总表 status_daily（历史页统计的数据源）
# ============================================================

class TestDailyStats:

    def _daily(self, conn, device_id):
        return conn.execute(
            "SELECT stat_date, total_count, online_count, offline_count, latency_sum "
            "FROM status_daily WHERE device_id=?", (device_id,)).fetchone()

    def test_record_check_bumps_daily_stats(self, db, device_id):
        conn, _ = db
        repo = DeviceRepository()
        repo.record_check(device_id, 'online', 0, 0, latency_ms=3.0, success=True)
        repo.record_check(device_id, 'online', 0, 0, latency_ms=5.0, success=True)
        repo.record_check(device_id, 'offline', 3, 0, latency_ms=0.0, success=False)

        row = self._daily(conn, device_id)
        assert row['stat_date'] == datetime.now().strftime('%Y-%m-%d')
        assert row['total_count'] == 3
        assert row['online_count'] == 2
        assert row['offline_count'] == 1
        assert row['latency_sum'] == pytest.approx(8.0)

    def test_record_check_result_and_insert_status_bump_daily(self, db, device_id):
        conn, _ = db
        DeviceRepository().record_check_result(device_id, True, 1.0)
        HistoryRepository().insert_status(device_id, 'offline', 0.0)
        row = self._daily(conn, device_id)
        assert (row['total_count'], row['online_count'], row['offline_count']) == (2, 1, 1)

    def test_uptime_reads_daily_stats_not_history(self, db, device_id):
        """日统计存在时，统计以日统计为准（即使历史表里另有数据）。"""
        conn, _ = db
        repo = DeviceRepository()
        for _ in range(3):
            repo.record_check_result(device_id, True, 1.0)      # 日统计 + 历史表都写
        conn.execute("DELETE FROM status_history")              # 只清历史表
        conn.commit()

        assert HistoryRepository().compute_uptime(device_id, 'day') == pytest.approx(1.0)

    def test_fallback_when_daily_stats_absent(self, db, device_id):
        """日统计为空（如直接写历史表的老数据/测试）时回退扫描历史表。"""
        conn, _ = db
        insert_history(conn, device_id, ts(hours_ago=1), 'online')
        insert_history(conn, device_id, ts(hours_ago=2), 'offline')
        conn.execute("DELETE FROM status_daily")
        conn.commit()

        assert HistoryRepository().compute_uptime(device_id, 'day') == pytest.approx(0.5)

    def test_partial_daily_stats_falls_back_to_history(self, db, device_id):
        """日汇总只覆盖今天、而历史表有 40 天数据时（刚升级、回填未跑完）：
        week/month 必须回退扫历史表，不能把"只有今天"当成整周/整月。"""
        conn, _ = db
        insert_history(conn, device_id, ts(hours_ago=1), 'online')     # 今天
        insert_history(conn, device_id, ts(days_ago=20), 'offline')    # 20 天前
        insert_history(conn, device_id, ts(days_ago=25), 'offline')
        conn.execute("INSERT INTO status_daily (device_id, stat_date, total_count, online_count, "
                     "offline_count) VALUES (?,?,?,?,?)",
                     (device_id, datetime.now().strftime('%Y-%m-%d'), 1, 1, 0))
        conn.commit()

        hist = HistoryRepository()
        assert hist.compute_uptime(device_id, 'day') == pytest.approx(1.0)      # 今天=1/1
        # 周窗口(最近7天)只有今天有数据 → 1/1；月窗口(最近30天)有 3 条 → 1/3
        assert hist.compute_uptime(device_id, 'month') == pytest.approx(1 / 3)
        summary = hist.compute_uptime_summary(device_id)
        assert summary['month'] == pytest.approx(1 / 3)
        assert summary['week'] == pytest.approx(1.0)

    def test_full_daily_stats_wins_over_history(self, db, device_id):
        """回填完成后（日汇总覆盖到窗口起点）以日汇总为准，不再扫历史表。"""
        conn, _ = db
        insert_history(conn, device_id, ts(days_ago=40), 'offline')   # 窗口外
        insert_history(conn, device_id, ts(days_ago=1), 'online')
        hist = HistoryRepository()
        hist.ensure_daily_stats(retention_days=30, force_full=True)
        conn.execute("DELETE FROM status_history")                     # 只留日汇总
        conn.commit()
        assert hist.compute_uptime(device_id, 'month') == pytest.approx(1.0)

    def test_ensure_daily_stats_backfills_from_history(self, db, device_id):
        conn, _ = db
        today = datetime.now()
        insert_history(conn, device_id, ts(days_ago=0), 'online')
        insert_history(conn, device_id, ts(days_ago=1), 'online')
        insert_history(conn, device_id, ts(days_ago=1), 'offline')
        insert_history(conn, device_id, ts(days_ago=45), 'offline')   # 窗口外

        result = HistoryRepository().ensure_daily_stats(retention_days=30, force_full=True)

        assert result['days_rebuilt'] == 30
        assert result['devices_days'] == 2      # 只有今天 + 昨天在窗口内
        rows = conn.execute(
            "SELECT stat_date, total_count, online_count FROM status_daily "
            "WHERE device_id=? ORDER BY stat_date", (device_id,)).fetchall()
        assert len(rows) == 2
        assert dict(rows[-1]) == {'stat_date': today.strftime('%Y-%m-%d'),
                                  'total_count': 1, 'online_count': 1}

    def test_ensure_daily_stats_overwrites_not_double_counts(self, db, device_id):
        """覆盖式回填：重复调用不会把计数翻倍。"""
        conn, _ = db
        insert_history(conn, device_id, ts(hours_ago=3), 'online')
        insert_history(conn, device_id, ts(hours_ago=4), 'offline')
        hist = HistoryRepository()
        hist.ensure_daily_stats(retention_days=30, force_full=True)
        hist.ensure_daily_stats(retention_days=30, force_full=True)
        hist.ensure_daily_stats(retention_days=30, force_full=True)

        row = conn.execute("SELECT total_count, online_count, offline_count FROM status_daily "
                           "WHERE device_id=?", (device_id,)).fetchone()
        assert tuple(row) == (2, 1, 1)

    def test_ensure_daily_stats_repairs_last_days_only(self, db, device_id):
        """非首次：只重算最近 N 天，不重建整个窗口。"""
        conn, _ = db
        insert_history(conn, device_id, ts(hours_ago=2), 'online')
        hist = HistoryRepository()
        hist.ensure_daily_stats(retention_days=30, force_full=True)
        result = hist.ensure_daily_stats(retention_days=30, rebuild_days=2)
        assert result['days_rebuilt'] == 2

    def test_uptime_summary_from_daily_stats_matches_history(self, db, device_id):
        conn, _ = db
        repo = DeviceRepository()
        for _ in range(7):
            repo.record_check_result(device_id, True, 1.0)
        for _ in range(3):
            repo.record_check_result(device_id, False, 0.0)

        hist = HistoryRepository()
        summary = hist.compute_uptime_summary(device_id)
        # 日统计读数与历史表直接统计一致
        assert summary['day'] == pytest.approx(0.7)
        assert summary['month'] == pytest.approx(0.7)
        assert hist.compute_overall_uptime('day') == pytest.approx(0.7)

    def test_toplist_from_daily_stats(self, db):
        conn, _ = db
        repo = DeviceRepository()
        d1 = repo.add_device({'name': 'alpha', 'ip_address': '10.3.0.1'})
        d2 = repo.add_device({'name': 'beta', 'ip_address': '10.3.0.2'})
        for _ in range(2):
            repo.record_check_result(d1, False, 0.0)
        repo.record_check_result(d2, False, 0.0)
        repo.record_check_result(d1, True, 1.0)

        toplist = HistoryRepository().get_offline_toplist(30, 10)
        assert [t['name'] for t in toplist[:2]] == ['alpha', 'beta']
        assert toplist[0]['offline_count'] == 2
        assert toplist[1]['offline_count'] == 1

    def test_prune_daily_stats_keeps_retention_window(self, db, device_id):
        conn, _ = db
        today = datetime.now()
        for days_ago in (0, 5, 29, 30, 45, 90):
            conn.execute(
                "INSERT INTO status_daily (device_id, stat_date, total_count, online_count) "
                "VALUES (?,?,?,?)",
                (device_id, (today - timedelta(days=days_ago)).strftime('%Y-%m-%d'), 10, 9))
        conn.commit()

        deleted = mnt.prune_daily_stats(conn, retention_days=30)

        # 保留窗口 = 今天起往前 30 个自然日（含今天），即 stat_date >= 今天-29
        assert deleted == 3          # 30 天前、45 天前、90 天前
        left = [r[0] for r in conn.execute(
            "SELECT stat_date FROM status_daily ORDER BY stat_date").fetchall()]
        assert left == [(today - timedelta(days=d)).strftime('%Y-%m-%d')
                        for d in (29, 5, 0)]

    def test_daily_stats_cascade_delete_with_device(self, db, device_id):
        conn, _ = db
        DeviceRepository().record_check_result(device_id, True, 1.0)
        assert conn.execute("SELECT COUNT(*) FROM status_daily").fetchone()[0] == 1
        DeviceRepository().delete_device(device_id)
        assert conn.execute("SELECT COUNT(*) FROM status_daily").fetchone()[0] == 0

    def test_run_prune_cycle_updates_daily_stats(self, db, device_id):
        conn, _ = db
        insert_history(conn, device_id, ts(days_ago=40), 'offline')
        insert_history(conn, device_id, ts(hours_ago=2), 'online')
        cfg = {'storage': {'cleanup': {'retention_days': 30, 'batch_size': 500,
                                       'batch_sleep_ms': 0, 'daily_stats_rebuild_days': 30}}}
        result = mnt.run_prune_cycle(conn, cfg)

        assert result['history_deleted'] == 1
        assert result['daily_stats']['devices_days'] == 1
        row = conn.execute("SELECT total_count, online_count FROM status_daily "
                           "WHERE device_id=?", (device_id,)).fetchone()
        assert tuple(row) == (1, 1)

    def test_indexes_used_for_daily_stats_queries(self, db, device_id):
        conn, _ = db
        repo = DeviceRepository()
        for _ in range(3):
            repo.record_check_result(device_id, True, 1.0)
        plans = {
            'overall': conn.execute(
                "EXPLAIN QUERY PLAN SELECT SUM(total_count), SUM(online_count) "
                "FROM status_daily WHERE stat_date >= ?", ('2026-01-01',)).fetchall(),
            'device': conn.execute(
                "EXPLAIN QUERY PLAN SELECT SUM(total_count), SUM(online_count) "
                "FROM status_daily WHERE device_id=? AND stat_date >= ?",
                (device_id, '2026-01-01')).fetchall(),
            'toplist': conn.execute(
                """EXPLAIN QUERY PLAN SELECT d.id, SUM(sd.offline_count) FROM devices d
                   LEFT JOIN status_daily sd ON sd.device_id = d.id AND sd.stat_date >= ?
                   GROUP BY d.id ORDER BY SUM(sd.offline_count) DESC LIMIT 10""",
                ('2026-01-01',)).fetchall(),
        }
        detail = {k: ' '.join(str(r[-1]) for r in v) for k, v in plans.items()}
        assert 'idx_sd_date' in detail['overall'], detail['overall']
        assert 'sqlite_autoindex_status_daily' in detail['device'] or \
            'PRIMARY KEY' in detail['device'], detail['device']
        assert 'SEARCH sd' in detail['toplist'], detail['toplist']


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
