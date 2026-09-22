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
import time
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
-- 设备名唯一约束（防止同名设备混入，GUI 已有友好提示，需约束到位）
-- 用 CREATE UNIQUE INDEX IF NOT EXISTS 而非 ALTER TABLE，避免旧库已有重名时报错中断
CREATE UNIQUE INDEX IF NOT EXISTS idx_devices_name_unique ON devices(name);

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

-- 每日统计汇总表（v1.0.11）：历史统计页的数据源
-- 每次探测以 (设备, 日期) 为键累加计数；历史页统计读这张小表
-- （天数 × 设备数，1000 台 × 30 天 ≈ 3 万行），不再扫描 status_history
-- （1000 台 30 秒间隔时 status_history 30 天就有近 1 亿行）。
-- WITHOUT ROWID：主键即存储顺序，按设备查统计是一次连续读。
CREATE TABLE IF NOT EXISTS status_daily (
    device_id INTEGER NOT NULL,
    stat_date TEXT NOT NULL,
    total_count INTEGER DEFAULT 0,
    online_count INTEGER DEFAULT 0,
    offline_count INTEGER DEFAULT 0,
    latency_sum REAL DEFAULT 0.0,
    updated_at TEXT DEFAULT (datetime('now','localtime')),
    PRIMARY KEY (device_id, stat_date),
    FOREIGN KEY (device_id) REFERENCES devices(id) ON DELETE CASCADE
) WITHOUT ROWID;
-- 全局统计（不按设备过滤）按日期区间扫描
CREATE INDEX IF NOT EXISTS idx_sd_date ON status_daily(stat_date);

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

# ============================================================
# 性能索引（v1.0.11 历史统计加速）
# ------------------------------------------------------------
# 统计查询主要走日汇总表 status_daily（见 maintenance/repositories），
# 这里的索引服务于：
#   1) 保留期分批清理：WHERE checked_at < ? ORDER BY checked_at LIMIT n
#      → 必须以 checked_at 打头；带 status 列可让"全局在线率"回退统计变成索引内计数
#   2) 全域在线率回退：WHERE checked_at>=? [AND status='online']（同上）
#   3) 离线时长统计：status='offline' 的部分索引（离线行占比极小，索引体积远小于全表）
#   单设备区间查询继续用老的 idx_sh_device_time(device_id, checked_at)，不额外加宽索引
# 单独抽出成脚本的原因：老库首次建索引可能要几十秒到数分钟，
# 放在 ensure_indexes() 里可计时并写启动日志，不会静默拖慢启动。
# ============================================================
PERFORMANCE_INDEX_DDL = {
    'idx_sh_checked_status':
        "CREATE INDEX IF NOT EXISTS idx_sh_checked_status "
        "ON status_history(checked_at, status)",
    'idx_sh_offline':
        "CREATE INDEX IF NOT EXISTS idx_sh_offline "
        "ON status_history(device_id, checked_at) WHERE status='offline'",
    'idx_ae_created_at':
        "CREATE INDEX IF NOT EXISTS idx_ae_created_at ON alert_events(created_at)",
    'idx_ae_pending_escalation':
        "CREATE INDEX IF NOT EXISTS idx_ae_pending_escalation "
        "ON alert_events(event_type, is_acknowledged, created_at)",
    'idx_ae_device_created':
        "CREATE INDEX IF NOT EXISTS idx_ae_device_created ON alert_events(device_id, created_at)",
}

# 需要保留但不再由本模块创建的索引（老库已有；新库由 CREATE_TABLES_SQL 创建）：
#   idx_sh_device_time(device_id, checked_at) —— 单设备区间查询/回退统计
#     （实测：三列覆盖索引要多占约 1/3 索引空间，而历史页统计已改读日汇总表，
#      不值得为罕见回退路径长期付出这个体积代价）
# 没有被新索引覆盖的冗余索引：无
REDUNDANT_INDEXES = ()


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
        # 是否全新数据库：auto_vacuum 只能在建表前设置（老库需 VACUUM 才能切换）
        _is_new_db = (not os.path.exists(db_path)) or os.path.getsize(db_path) == 0
        _local.conn = sqlite3.connect(db_path)
        _local.conn.row_factory = sqlite3.Row
        # 新库启用增量回收（必须在 journal_mode / 建表之前执行：
        # 切 WAL 或写入 schema 会把 auto_vacuum 现值固化进库头，之后再设只算"待生效"，
        # 需要 VACUUM 才落地）→ 清理历史后 incremental_vacuum 能把空闲页还给文件系统
        if _is_new_db:
            _local.conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
        # WAL 模式 —— 支持多连接并发读
        _local.conn.execute("PRAGMA journal_mode=WAL")
        # busy_timeout —— 写锁等待 5 秒
        _local.conn.execute("PRAGMA busy_timeout=5000")
        # 外键约束
        _local.conn.execute("PRAGMA foreign_keys=ON")
        # WAL 文件上限 64MB：checkpoint 后自动截断，避免 data/ 下 -wal 文件长期膨胀
        _local.conn.execute("PRAGMA journal_size_limit=67108864")
        logger.debug(f"线程 {threading.current_thread().name} 创建数据库连接: {db_path}")
    return _local.conn


def ensure_indexes(conn: sqlite3.Connection) -> dict:
    """确保性能索引到位，并移除被覆盖的冗余索引（v1.0.11）。

    幂等：已存在的索引不会重建（大表建索引耗时可达分钟级，启动日志会记录耗时）。

    参数:
        conn: 数据库连接

    返回:
        {'created': [索引名...], 'dropped': [索引名...], 'elapsed_s': float}
    """
    existing = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'")}
    created = []
    t0 = time.perf_counter()
    for name, ddl in PERFORMANCE_INDEX_DDL.items():
        if name in existing:
            continue
        conn.execute(ddl)
        created.append(name)
    dropped = []
    for name in REDUNDANT_INDEXES:
        if name in existing:
            conn.execute(f"DROP INDEX IF EXISTS {name}")
            dropped.append(name)
    if created or dropped:
        conn.commit()
    elapsed = time.perf_counter() - t0
    if created:
        logger.info(f"性能索引已创建（{elapsed:.1f}s）: {', '.join(created)}")
    if dropped:
        logger.info(f"已移除冗余索引: {', '.join(dropped)}（已被覆盖索引包含，省磁盘）")
    return {'created': created, 'dropped': dropped, 'elapsed_s': elapsed}


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
        logger.info("数据库表初始化完成（7 表 + 索引）")
        # 性能索引 + 冗余索引清理（老库首次升级会在这里补索引，日志带耗时）
        _idx = ensure_indexes(conn)
        logger.info(
            f"索引检查完成: 新建 {len(_idx['created'])} 个, "
            f"移除冗余 {len(_idx['dropped'])} 个, 耗时 {_idx['elapsed_s']:.2f}s"
        )
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
