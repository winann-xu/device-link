"""
模块：repositories.py
功能：数据仓库层 —— 封装所有数据库 CRUD 操作
     - DeviceRepository: 设备增删改查、批量操作、CSV 导入导出
     - HistoryRepository: 状态历史记录、在线率统计
     - AlertRepository: 告警事件管理
     - ChannelRepository: 通知通道管理

所有方法线程安全（通过数据库连接层保证）。

作者：Claude
创建日期：2026-08-07
"""
import sqlite3
import csv
import io
import logging
import threading
from typing import Optional, Tuple
from datetime import datetime, timedelta

logger = logging.getLogger("device-link.repositories")

from .database import get_connection as _get_db_conn

# 全局数据库锁（RLock 支持嵌套调用）：
# 配合连接池化（每线程独立连接 + WAL 模式），序列化所有写操作，
# 防止并发写导致的 "database is locked" 和 C 层访问违例。
_DB_LOCK = threading.RLock()


def _db_sync(method):
    """装饰器：让仓储的公共方法在全局数据库锁内执行。"""
    import functools

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with _DB_LOCK:
            return method(self, *args, **kwargs)

    return wrapper


class DeviceRepository:
    """
    设备数据仓库。
    提供设备 CRUD、批量操作、CSV 导入导出功能。
    线程安全——通过 _lock 保护复合操作。
    """

    def __init__(self, conn: sqlite3.Connection = None):
        """
        初始化仓库。

        参数:
            conn: 已废弃——保留仅为向后兼容。连接改为通过 get_connection()
                  按线程获取，杜绝跨线程共享连接导致的 sqlite3.dll 访问违例。
        """
        # conn 参数忽略——每个线程通过 get_connection() 获取自己的连接
        self._lock = threading.Lock()

    @property
    def _conn(self):
        """获取当前线程的数据库连接。"""
        return _get_db_conn()

    # ==================== 单设备操作 ====================

    def add_device(self, device_dict: dict) -> int:
        """
        添加单台设备。

        参数:
            device_dict: 设备字段字典

        返回:
            新设备的 ID

        异常:
            sqlite3.IntegrityError: 设备名重复
        """
        fields = [
            'name', 'ip_address', 'subnet_mask', 'subsystem_name', 'monitor_method',
            'port', 'check_interval_seconds', 'timeout_ms', 'failure_threshold',
            'recovery_threshold', 'is_enabled', 'is_maintenance'
        ]
        # 默认值——与数据库 schema 默认值保持一致，防止空字符串导致类型错误
        DEFAULTS = {
            'subnet_mask': '', 'subsystem_name': '', 'monitor_method': 'auto',
            'port': 0, 'check_interval_seconds': 30, 'timeout_ms': 3000,
            'failure_threshold': 3, 'recovery_threshold': 2,
            'is_enabled': 1, 'is_maintenance': 0,
        }
        values = [device_dict.get(f, DEFAULTS.get(f, '')) for f in fields]
        placeholders = ','.join(['?'] * len(fields))
        sql = f"INSERT INTO devices ({','.join(fields)}) VALUES ({placeholders})"
        with self._lock:
            cursor = self._conn.execute(sql, values)
            self._conn.commit()
            return cursor.lastrowid

    def update_device(self, device_id: int, fields_dict: dict) -> bool:
        """
        更新设备字段。

        参数:
            device_id: 设备 ID
            fields_dict: 要更新的字段键值对

        返回:
            True 表示更新成功
        """
        if not fields_dict:
            return False
        set_clause = ','.join([f"{k}=?" for k in fields_dict.keys()])
        values = list(fields_dict.values()) + [device_id]
        # 自动更新 updated_at
        sql = f"UPDATE devices SET {set_clause}, updated_at=datetime('now','localtime') WHERE id=?"
        with self._lock:
            self._conn.execute(sql, values)
            self._conn.commit()
        return True

    def delete_device(self, device_id: int) -> bool:
        """删除单台设备（级联删除关联数据）。"""
        with self._lock:
            cursor = self._conn.execute("DELETE FROM devices WHERE id=?", (device_id,))
            self._conn.commit()
            return cursor.rowcount > 0

    def delete_devices(self, device_ids: list) -> int:
        """
        批量删除设备。

        参数:
            device_ids: 设备 ID 列表

        返回:
            实际删除的设备数
        """
        if not device_ids:
            return 0
        placeholders = ','.join(['?'] * len(device_ids))
        with self._lock:
            cursor = self._conn.execute(
                f"DELETE FROM devices WHERE id IN ({placeholders})", device_ids
            )
            self._conn.commit()
            return cursor.rowcount

    def get_device(self, device_id: int) -> Optional[dict]:
        """根据 ID 查询单台设备，返回字典或 None。"""
        row = self._conn.execute("SELECT * FROM devices WHERE id=?", (device_id,)).fetchone()
        return dict(row) if row else None

    # ==================== 批量操作 ====================

    def add_devices_batch(self, device_list: list) -> Tuple[int, list]:
        """
        批量添加设备。
        遇到重复设备名时跳过并记录失败详情，不中断整体导入。

        参数:
            device_list: 设备字典列表

        返回:
            (成功数, 失败详情列表)
        """
        success_count = 0
        failed_list = []
        for i, d in enumerate(device_list):
            try:
                self.add_device(d)
                success_count += 1
            except sqlite3.IntegrityError:
                failed_list.append({
                    'index': i,
                    'name': d.get('name', ''),
                    'reason': '设备名重复'
                })
            except Exception as e:
                failed_list.append({
                    'index': i,
                    'name': d.get('name', ''),
                    'reason': str(e)
                })
        return success_count, failed_list

    def import_from_csv(self, filepath: str) -> Tuple[int, list]:
        """
        从 CSV 文件导入设备列表。
        CSV 表头：设备名,IP,子系统,探测方式,端口,检查间隔,失败阈值,是否启用

        参数:
            filepath: CSV 文件路径

        返回:
            (成功数, 失败详情列表)
        """
        devices = []
        with open(filepath, 'r', encoding='utf-8-sig') as f:
            reader = csv.DictReader(f)
            for row in reader:
                devices.append({
                    'name': row.get('设备名', '').strip(),
                    'ip_address': row.get('IP', '').strip(),
                    'subsystem_name': row.get('子系统', '').strip(),
                    'monitor_method': row.get('探测方式', 'auto').strip(),
                    'port': int(row.get('端口', 0) or 0),
                    'check_interval_seconds': int(row.get('检查间隔', 30) or 30),
                    'failure_threshold': int(row.get('失败阈值', 3) or 3),
                    'is_enabled': 1 if row.get('是否启用', '是').strip() in ('是', '1', 'true', 'yes') else 0,
                })
        return self.add_devices_batch(devices)

    def export_to_csv(self, filepath: str, subsystem_filter: str = None) -> int:
        """
        将设备列表导出为 CSV 文件。

        参数:
            filepath: 输出 CSV 文件路径
            subsystem_filter: 按子系统过滤（可选）

        返回:
            导出的设备数
        """
        devices = self.list_devices(subsystem=subsystem_filter, enabled_only=False)
        if not devices:
            return 0
        with open(filepath, 'w', encoding='utf-8-sig', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(['设备名', 'IP', '子系统', '探测方式', '端口', '检查间隔', '失败阈值', '恢复阈值', '是否启用', '状态'])
            for d in devices:
                writer.writerow([
                    d.get('name', ''), d.get('ip_address', ''), d.get('subsystem_name', ''),
                    d.get('monitor_method', ''), d.get('port', ''), d.get('check_interval_seconds', ''),
                    d.get('failure_threshold', ''), d.get('recovery_threshold', ''),
                    '是' if d.get('is_enabled') else '否', d.get('status', '')
                ])
        return len(devices)

    # ==================== 查询 ====================

    def list_devices(self, subsystem: str = None, enabled_only: bool = False) -> list:
        """
        查询设备列表。

        参数:
            subsystem: 按子系统名过滤（可选）
            enabled_only: 仅返回已启用的设备

        返回:
            设备字典列表
        """
        conditions = []
        params = []
        if subsystem:
            conditions.append("subsystem_name=?")
            params.append(subsystem)
        if enabled_only:
            conditions.append("is_enabled=1")
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        rows = self._conn.execute(
            f"SELECT * FROM devices {where} ORDER BY subsystem_name, name", params
        ).fetchall()
        return [dict(r) for r in rows]

    def list_enabled_devices(self) -> list:
        """返回所有已启用且非维护模式的设备列表。"""
        return self.list_devices(enabled_only=True)

    # ==================== 状态更新 ====================

    def set_device_status(self, device_id: int, status: str, failure_count: int,
                          recovery_count: int = 0, latency_ms: float = 0.0) -> bool:
        """
        更新设备探测状态（原子操作，线程安全）。

        参数:
            device_id: 设备 ID
            status: 新状态 (online/offline/pending_failure/unknown)
            failure_count: 当前连续失败次数
            recovery_count: 当前连续成功次数
            latency_ms: 本次探测延迟（毫秒）

        返回:
            True 表示更新成功
        """
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        with self._lock:
            self._conn.execute(
                """UPDATE devices SET status=?, failure_count=?, recovery_count=?,
                   latency_ms=?, last_check_time=?, updated_at=datetime('now','localtime')
                   WHERE id=?""",
                (status, failure_count, recovery_count, latency_ms, now, device_id)
            )
            self._conn.commit()
        return True

    def record_check_result(self, device_id: int, success: bool, latency_ms: float = 0.0) -> bool:
        """
        将本次探测结果写入 status_history 表。
        每次探测调用一次，用于在线率计算和历史追溯。

        参数:
            device_id: 设备 ID
            success: 本次探测是否成功
            latency_ms: 延迟（毫秒）

        返回:
            True 表示写入成功
        """
        status = 'online' if success else 'offline'
        self._conn.execute(
            "INSERT INTO status_history (device_id, status, latency_ms) VALUES (?,?,?)",
            (device_id, status, latency_ms)
        )
        self._conn.commit()
        return True

    # ==================== 批量状态切换 ====================

    def set_maintenance_batch(self, device_ids: list, is_maintenance: bool) -> int:
        """
        批量设置设备维护模式。

        参数:
            device_ids: 设备 ID 列表
            is_maintenance: True=进入维护，False=退出维护

        返回:
            受影响的设备数
        """
        if not device_ids:
            return 0
        value = 1 if is_maintenance else 0
        placeholders = ','.join(['?'] * len(device_ids))
        with self._lock:
            cursor = self._conn.execute(
                f"UPDATE devices SET is_maintenance=?, updated_at=datetime('now','localtime') WHERE id IN ({placeholders})",
                [value] + device_ids
            )
            self._conn.commit()
            return cursor.rowcount

    def enable_batch(self, device_ids: list, is_enabled: bool) -> int:
        """批量启用/禁用设备。"""
        if not device_ids:
            return 0
        value = 1 if is_enabled else 0
        placeholders = ','.join(['?'] * len(device_ids))
        with self._lock:
            cursor = self._conn.execute(
                f"UPDATE devices SET is_enabled=?, updated_at=datetime('now','localtime') WHERE id IN ({placeholders})",
                [value] + device_ids
            )
            self._conn.commit()
            return cursor.rowcount


class HistoryRepository:
    """状态历史记录仓库，提供在线率统计和数据清理功能。"""

    def __init__(self, conn: sqlite3.Connection = None):
        pass  # 连接通过 _conn 属性按线程获取

    @property
    def _conn(self):
        return _get_db_conn()

    def insert_status(self, device_id: int, status: str, latency_ms: float = 0.0) -> bool:
        """插入一条状态记录。"""
        self._conn.execute(
            "INSERT INTO status_history (device_id, status, latency_ms) VALUES (?,?,?)",
            (device_id, status, latency_ms)
        )
        self._conn.commit()
        return True

    def query_status_range(self, device_id: int, start_time: str, end_time: str) -> list:
        """
        查询设备在指定时间范围内的状态历史。

        参数:
            device_id: 设备 ID
            start_time: 起始时间 (YYYY-MM-DD HH:MM:SS)
            end_time: 结束时间 (YYYY-MM-DD HH:MM:SS)

        返回:
            状态记录字典列表，按时间升序
        """
        rows = self._conn.execute(
            """SELECT * FROM status_history
               WHERE device_id=? AND checked_at BETWEEN ? AND ?
               ORDER BY checked_at ASC""",
            (device_id, start_time, end_time)
        ).fetchall()
        return [dict(r) for r in rows]

    def compute_uptime(self, device_id: int, period: str = 'day') -> float:
        """
        计算设备在线率。

        参数:
            device_id: 设备 ID
            period: 统计周期 'day' | 'week' | 'month'

        返回:
            在线率（0.0 ~ 1.0），无数据时返回 0.0
        """
        days_map = {'day': 1, 'week': 7, 'month': 30}
        days = days_map.get(period, 1)
        since = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d %H:%M:%S')

        total = self._conn.execute(
            "SELECT COUNT(*) FROM status_history WHERE device_id=? AND checked_at >= ?",
            (device_id, since)
        ).fetchone()[0]

        if total == 0:
            return 0.0

        online = self._conn.execute(
            "SELECT COUNT(*) FROM status_history WHERE device_id=? AND checked_at >= ? AND status='online'",
            (device_id, since)
        ).fetchone()[0]

        return online / total

    def compute_overall_uptime(self, period: str = 'day') -> float:
        """
        计算全部设备的整体在线率（历史页"全部设备"统计用）。

        参数:
            period: 统计周期 'day' | 'week' | 'month'

        返回:
            在线率（0.0 ~ 1.0），无数据时返回 0.0
        """
        days_map = {'day': 1, 'week': 7, 'month': 30}
        days = days_map.get(period, 1)
        since = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d %H:%M:%S')

        total = self._conn.execute(
            "SELECT COUNT(*) FROM status_history WHERE checked_at >= ?",
            (since,)
        ).fetchone()[0]

        if total == 0:
            return 0.0

        online = self._conn.execute(
            "SELECT COUNT(*) FROM status_history WHERE checked_at >= ? AND status='online'",
            (since,)
        ).fetchone()[0]

        return online / total

    def get_offline_toplist(self, days: int = 7, limit: int = 10) -> list:
        """
        获取离线时长排行榜（按累计离线次数降序）。

        参数:
            days: 统计最近 N 天
            limit: 返回 TOP N

        返回:
            排行榜列表 [{'device_id': ..., 'name': ..., 'offline_count': ..., 'total_hours': ...}, ...]
        """
        since = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d %H:%M:%S')
        rows = self._conn.execute(
            """SELECT d.id, d.name, d.ip_address, d.subsystem_name,
                      COUNT(sh.id) as offline_count
               FROM devices d
               LEFT JOIN status_history sh ON d.id=sh.device_id
                 AND sh.status='offline' AND sh.checked_at >= ?
               GROUP BY d.id
               ORDER BY offline_count DESC
               LIMIT ?""",
            (since, limit)
        ).fetchall()
        return [dict(r) for r in rows]

    def cleanup_expired(self, retention_days: int = 90) -> int:
        """
        清理超过保留期的历史记录。

        参数:
            retention_days: 保留天数（默认 90 天）

        返回:
            清理的记录数
        """
        cutoff = (datetime.now() - timedelta(days=retention_days)).strftime('%Y-%m-%d %H:%M:%S')
        cursor = self._conn.execute(
            "DELETE FROM status_history WHERE checked_at < ?", (cutoff,)
        )
        self._conn.commit()
        return cursor.rowcount


class AlertRepository:
    """告警事件仓库。"""

    def __init__(self, conn: sqlite3.Connection = None):
        pass  # 连接通过 _conn 属性按线程获取

    @property
    def _conn(self):
        return _get_db_conn()

    def insert_event(self, event_dict: dict) -> int:
        """
        插入告警事件。

        参数:
            event_dict: 事件字段字典

        返回:
            新事件的 ID
        """
        fields = ['device_id', 'event_type', 'message', 'notified_channels',
                   'notify_success', 'digest_id']
        values = [event_dict.get(f, '') for f in fields]
        cursor = self._conn.execute(
            f"INSERT INTO alert_events ({','.join(fields)}) VALUES ({','.join(['?']*len(fields))})",
            values
        )
        self._conn.commit()
        return cursor.lastrowid

    def list_events(self, device_id: int = None, event_type: str = None,
                    acknowledged: bool = None, start_time: str = None,
                    end_time: str = None, limit: int = 100) -> list:
        """
        查询告警事件列表，支持多条件筛选。

        参数:
            device_id: 设备 ID 过滤（可选）
            event_type: 事件类型过滤（可选）
            acknowledged: 确认状态过滤（可选，True=已确认，False=未确认）
            start_time: 起始时间（可选）
            end_time: 结束时间（可选）
            limit: 最大返回条数（默认 100）

        返回:
            事件字典列表，按创建时间降序
        """
        conditions = []
        params = []
        if device_id is not None:
            conditions.append("device_id=?")
            params.append(device_id)
        if event_type:
            conditions.append("event_type=?")
            params.append(event_type)
        if acknowledged is not None:
            conditions.append("is_acknowledged=?")
            params.append(1 if acknowledged else 0)
        if start_time:
            conditions.append("created_at >= ?")
            params.append(start_time)
        if end_time:
            conditions.append("created_at <= ?")
            params.append(end_time)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        rows = self._conn.execute(
            f"SELECT * FROM alert_events {where} ORDER BY created_at DESC LIMIT ?",
            params + [limit]
        ).fetchall()
        return [dict(r) for r in rows]

    def acknowledge(self, event_id: int, user: str = "admin") -> bool:
        """确认告警事件。"""
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        self._conn.execute(
            "UPDATE alert_events SET is_acknowledged=1, ack_by=?, ack_time=? WHERE id=?",
            (user, now, event_id)
        )
        self._conn.commit()
        return True

    def get_unacknowledged_offline_events(self) -> list:
        """
        获取所有未确认的离线告警事件。
        供升级引擎使用——检查是否需要自动升级通知。

        返回:
            未确认离线事件列表
        """
        rows = self._conn.execute(
            """SELECT * FROM alert_events
               WHERE event_type='offline' AND is_acknowledged=0
               ORDER BY created_at DESC"""
        ).fetchall()
        return [dict(r) for r in rows]

    def get_events_in_window(self, since_timestamp: str) -> list:
        """
        获取指定时间窗口内的告警事件。
        供合并引擎使用——收集窗口内的离线事件生成摘要。

        参数:
            since_timestamp: 窗口开始时间

        返回:
            窗口内事件列表
        """
        rows = self._conn.execute(
            """SELECT * FROM alert_events
               WHERE event_type='offline' AND created_at >= ?
               ORDER BY created_at ASC""",
            (since_timestamp,)
        ).fetchall()
        return [dict(r) for r in rows]


    def update_notify_result(self, event_id: int, notify_success: int, channels: str = "") -> bool:
        """更新告警事件的通知投递结果（Bug K 修复：原实现从不落库）。"""
        self._conn.execute(
            "UPDATE alert_events SET notify_success=?, notified_channels=? WHERE id=?",
            (notify_success, channels, event_id)
        )
        self._conn.commit()
        return True


class ChannelRepository:
    """通知通道仓库。"""

    def __init__(self, conn: sqlite3.Connection = None):
        pass  # 连接通过 _conn 属性按线程获取

    @property
    def _conn(self):
        return _get_db_conn()

    def get_enabled_channels(self) -> list:
        """返回所有已启用的通知通道。"""
        rows = self._conn.execute(
            "SELECT * FROM notification_channels WHERE is_enabled=1"
        ).fetchall()
        return [dict(r) for r in rows]

    def save_channel(self, channel_dict: dict) -> bool:
        """保存或更新通知通道配置。"""
        channel_type = channel_dict.get('channel_type', '')
        name = channel_dict.get('name', '')
        existing = self._conn.execute(
            "SELECT id FROM notification_channels WHERE channel_type=? AND name=?",
            (channel_type, name)
        ).fetchone()
        if existing:
            self._conn.execute(
                "UPDATE notification_channels SET config_json=?, is_enabled=? WHERE id=?",
                (channel_dict.get('config_json', '{}'), channel_dict.get('is_enabled', 1), existing['id'])
            )
        else:
            self._conn.execute(
                "INSERT INTO notification_channels (channel_type, name, config_json, is_enabled) VALUES (?,?,?,?)",
                (channel_type, name, channel_dict.get('config_json', '{}'), channel_dict.get('is_enabled', 1))
            )
        self._conn.commit()
        return True

    def update_last_test(self, channel_id: int, success: bool) -> bool:
        """更新通道最近测试结果。"""
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        self._conn.execute(
            "UPDATE notification_channels SET last_test_time=?, last_test_success=? WHERE id=?",
            (now, 1 if success else 0, channel_id)
        )
        self._conn.commit()
        return True

# ==================== Bug I 修复 ====================
# 统一给所有仓储类的公共方法加全局数据库锁，串行化跨线程访问。
for _repo_cls in (DeviceRepository, HistoryRepository, AlertRepository, ChannelRepository):
    for _name, _method in list(vars(_repo_cls).items()):
        if callable(_method) and not _name.startswith("_"):
            setattr(_repo_cls, _name, _db_sync(_method))
