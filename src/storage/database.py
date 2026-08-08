"""
模块：database.py
功能：SQLite 数据库初始化与管理
     创建 6 张核心表（subsystems/devices/status_history/alert_rules/alert_events/notification_channels），
     配置 WAL 模式、busy_timeout、外键约束。

作者：Claude
创建日期：2026-08-07
"""
import sqlite3
import os
import sys
import threading
import logging
from typing import Optional
from pathlib import Path

logger = logging.getLogger("device-link.database")

# 数据库连接（线程本地存储，保证线程安全）
_local = threading.local()

# 全局数据库路径（init_database 后设置，供 get_connection 在各线程复用）
_db_path_global: Optional[str] = None
_db_path_lock = threading.Lock()

# SQL 建表语句
CREATE_TABLES_SQL = """
-- 子系统表
CREATE TABLE IF NOT EXISTS subsystems (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    sort_order INTEGER DEFAULT 0,
    description TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

-- 设备表（核心表）
CREATE TABLE IF NOT EXISTS devices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    ip_address TEXT NOT NULL,
    subnet_mask TEXT DEFAULT '',
    subsystem_name TEXT DEFAULT '',
    monitor_method TEXT DEFAULT 'auto',
    port INTEGER DEFAULT 0,
    check_interval_seconds INTEGER DEFAULT 30,
    timeout_ms INTEGER DEFAULT 3000,
    failure_threshold INTEGER DEFAULT 3,
    recovery_threshold INTEGER DEFAULT 2,
    is_enabled INTEGER DEFAULT 1,
    is_maintenance INTEGER DEFAULT 0,
    status TEXT DEFAULT 'unknown',
    failure_count INTEGER DEFAULT 0,
    recovery_count INTEGER DEFAULT 0,
    latency_ms REAL DEFAULT 0.0,
    last_check_time TEXT,
    last_status_change_time TEXT,
    last_downtime_start TEXT,
    created_at TEXT DEFAULT (datetime('now','localtime')),
    updated_at TEXT DEFAULT (datetime('now','localtime'))
);

-- 状态历史表（在线率统计核心）
CREATE TABLE IF NOT EXISTS status_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id INTEGER NOT NULL,
    status TEXT NOT NULL,
    latency_ms REAL DEFAULT 0.0,
    checked_at TEXT DEFAULT (datetime('now','localtime')),
    FOREIGN KEY (device_id) REFERENCES devices(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_sh_device_time ON status_history(device_id, checked_at);

-- 告警规则表
CREATE TABLE IF NOT EXISTS alert_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id INTEGER UNIQUE,
    failure_threshold INTEGER DEFAULT 3,
    recovery_threshold INTEGER DEFAULT 2,
    notify_on_recovery INTEGER DEFAULT 1,
    cooldown_seconds INTEGER DEFAULT 1800,
    escalation_minutes INTEGER DEFAULT 15,
    is_enabled INTEGER DEFAULT 1,
    FOREIGN KEY (device_id) REFERENCES devices(id) ON DELETE CASCADE
);

-- 告警事件表
CREATE TABLE IF NOT EXISTS alert_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    message TEXT DEFAULT '',
    notified_channels TEXT DEFAULT '',
    notify_success INTEGER DEFAULT 0,
    is_acknowledged INTEGER DEFAULT 0,
    ack_by TEXT DEFAULT '',
    ack_time TEXT,
    digest_id TEXT,
    created_at TEXT DEFAULT (datetime('now','localtime')),
    FOREIGN KEY (device_id) REFERENCES devices(id) ON DELETE CASCADE
);

-- 通知通道表
CREATE TABLE IF NOT EXISTS notification_channels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_type TEXT NOT NULL,
    name TEXT NOT NULL,
    config_json TEXT DEFAULT '{}',
    is_enabled INTEGER DEFAULT 1,
    last_test_time TEXT,
    last_test_success INTEGER DEFAULT 0
);
"""


def get_db_path(config: Optional[dict] = None) -> str:
    """
    获取数据库文件路径。
    优先使用配置中的路径，否则使用默认值 ./data/device-link.db。

    参数:
        config: 应用配置字典（可选）

    返回:
        数据库文件绝对路径
    """
    if config and 'storage' in config and 'path' in config['storage']:
        db_path = config['storage']['path']
    else:
        db_path = './data/device-link.db'

    # 如果是相对路径，基于项目根目录解析
    if not os.path.isabs(db_path):
        if getattr(sys, 'frozen', False):
            # 打包模式：数据库/日志等运行时数据必须放在 exe 所在目录（便携版要求）
            project_root = Path(sys.executable).parent
        else:
            project_root = Path(__file__).parent.parent.parent
        db_path = os.path.join(project_root, db_path)

    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    return db_path


def get_connection(db_path: Optional[str] = None, config: Optional[dict] = None) -> sqlite3.Connection:
    """
    获取当前线程的数据库连接。
    每个线程获取自己独立的 sqlite3.Connection，杜绝跨线程共享同一连接导致的
    C 层面访问违例（sqlite3.dll ACCESS VIOLATION 0xc0000005）。

    参数:
        db_path: 数据库路径（可选，不提供则使用全局 _db_path_global）
        config: 应用配置（可选）

    返回:
        当前线程的 sqlite3.Connection 实例
    """
    # 每个线程独立判断——threading.local() 确保不同线程看到不同值
    if not hasattr(_local, 'conn') or _local.conn is None:
        if db_path is None:
            with _db_path_lock:
                db_path = _db_path_global
        if db_path is None and config is not None:
            db_path = get_db_path(config)
        if db_path is None:
            db_path = get_db_path()

        # 注意：不使用 check_same_thread=False。
        # 每个线程通过 threading.local() 持有自己独立的连接，
        # 不存在跨线程共享，因此不需要禁用 Python 的线程安全检查。
        _local.conn = sqlite3.connect(db_path)
        _local.conn.row_factory = sqlite3.Row
        # WAL 模式 —— 支持多连接并发读
        _local.conn.execute("PRAGMA journal_mode=WAL")
        # busy_timeout —— 写锁等待 5 秒
        _local.conn.execute("PRAGMA busy_timeout=5000")
        # 外键约束
        _local.conn.execute("PRAGMA foreign_keys=ON")
        logger.debug(f"线程 {threading.current_thread().name} 创建数据库连接: {db_path}")
    return _local.conn


def init_database(db_path: Optional[str] = None, config: Optional[dict] = None) -> sqlite3.Connection:
    """
    初始化数据库：创建所有表、索引，设置全局数据库路径。
    幂等操作——已存在的表不会被重复创建。

    参数:
        db_path: 数据库文件路径（可选）
        config: 应用配置（可选）

    返回:
        数据库连接实例（调用线程的连接）
    """
    global _db_path_global
    conn = get_connection(db_path, config)
    try:
        conn.executescript(CREATE_TABLES_SQL)
        conn.commit()
        logger.info("数据库表初始化完成（6 表 + 索引）")
    except sqlite3.Error as e:
        logger.error(f"数据库初始化失败: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    # 记录全局路径，供其他线程通过 get_connection() 创建自己的连接
    with _db_path_lock:
        if db_path is not None:
            _db_path_global = db_path
        elif _db_path_global is None:
            _db_path_global = get_db_path(config)
    return conn


def close_connection():
    """关闭当前线程的数据库连接。"""
    if hasattr(_local, 'conn') and _local.conn is not None:
        try:
            _local.conn.close()
        except Exception:
            pass
        finally:
            _local.conn = None
        logger.debug("数据库连接已关闭")
