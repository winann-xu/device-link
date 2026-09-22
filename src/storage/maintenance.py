"""
模块：maintenance.py
功能：历史数据保留策略与数据库空间维护
     - 自动清理超过保留期（默认 30 天）的 status_history 与已确认 alert_events
     - 回填/修复历史统计的日汇总表 status_daily（历史页统计的数据源）
     - 分批删除（默认每批 5000 行 + 让出写锁），避免长时间独占写锁影响探测落库
     - 清理后做 WAL checkpoint / 增量回收 / PRAGMA optimize
     - DataRetentionWorker：后台线程，启动即跑一轮，之后按 interval_minutes 周期执行
     - compact_database：整库 VACUUM 回收磁盘空间（需独占，供停机维护/启动阶段调用）

设计要点（为什么是"分批 + 周期 + 单轮上限"）：
    探测每 30 秒给每台设备写一行历史，1000 台设备约 288 万行/天。
    首次上线本策略时，库里可能积压数千万乃至上亿行过期数据，
    一次性 DELETE 会长时间持写锁（探测落库全部报 database is locked），
    因此每轮最多删除 max_rows_per_run 行，多轮持续推进直到追平保留期。

作者：Claude
创建日期：2026-09-22
"""
import json
import logging
import os
import sqlite3
import sys
import threading
import time
from datetime import datetime, timedelta
from typing import Optional

from .database import get_connection

logger = logging.getLogger("device-link.maintenance")

# 产品默认保留期：只保留最近 30 天
DEFAULT_RETENTION_DAYS = 30
# 单批删除行数（批间让锁，控制单次写锁持有时间）
DEFAULT_BATCH_SIZE = 5000
# 批间休眠（毫秒）：给探测落库/UI 写入让出写锁
DEFAULT_BATCH_SLEEP_MS = 50
# 清理周期（分钟）
DEFAULT_INTERVAL_MINUTES = 60
# 单轮最多删除行数（防止首轮长时间占用）
DEFAULT_MAX_ROWS_PER_RUN = 2_000_000
# 旧版出厂默认保留期：老配置里等于该值时按新策略（30 天）处理
_LEGACY_DEFAULT_RETENTION_DAYS = 90

_TIME_FMT = '%Y-%m-%d %H:%M:%S'


def _write_lock():
    """获取仓储层的全局写锁（延迟导入，避免与 repositories 循环依赖）。

    清理线程与探测落库共用同一把进程内写锁：
    分批 DELETE 期间，每批结束即释放锁，探测落库不会被长时间饿死。
    """
    from .repositories import _DB_LOCK
    return _DB_LOCK


def _logs_dir() -> str:
    """日志/状态文件目录（与主程序一致：冻结模式取 exe 目录，开发模式取项目根）。"""
    if getattr(sys, 'frozen', False):
        root = os.path.dirname(os.path.abspath(sys.executable))
    else:
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(root, 'logs')


def cutoff_string(retention_days: int) -> str:
    """返回保留期截止时间字符串（早于该时间的记录将被清理）。"""
    return (datetime.now() - timedelta(days=int(retention_days))).strftime(_TIME_FMT)


# ============================================================
# 配置解析
# ============================================================

def resolve_cleanup_config(config: Optional[dict]) -> dict:
    """解析 storage.cleanup 配置，缺省项用产品默认值补齐。

    兼容旧配置：老版本 config.yaml 只有 storage.history_retention_days（无 cleanup 段）。
    若其值仍是旧版出厂默认 90 天，则按新策略 30 天执行，并写 WARNING 提示；
    用户显式改过的其它值（如 180/365）原样保留。

    参数:
        config: 应用配置字典

    返回:
        规范化后的清理配置字典
    """
    storage_cfg = dict((config or {}).get('storage', {}) or {})
    cleanup = dict(storage_cfg.get('cleanup', {}) or {})

    raw_days = cleanup.get('retention_days', storage_cfg.get('history_retention_days'))
    try:
        days = int(raw_days) if raw_days is not None else DEFAULT_RETENTION_DAYS
    except (TypeError, ValueError):
        days = DEFAULT_RETENTION_DAYS

    if 'cleanup' not in storage_cfg and days == _LEGACY_DEFAULT_RETENTION_DAYS:
        logger.warning(
            "检测到旧版默认保留期 90 天（storage.history_retention_days），"
            "已按新策略自动清理超过 %d 天的历史记录；如需保留更久，"
            "请在 config.yaml 的 storage.cleanup.retention_days 中显式设置。",
            DEFAULT_RETENTION_DAYS,
        )
        days = DEFAULT_RETENTION_DAYS

    def _num(key, default, cast: type = float, minimum=0):
        try:
            return max(minimum, cast(cleanup.get(key, default)))
        except (TypeError, ValueError):
            return default

    return {
        'enabled': bool(cleanup.get('enabled', True)),
        'retention_days': max(1, days),
        'interval_minutes': _num('interval_minutes', DEFAULT_INTERVAL_MINUTES, float, 1),
        'batch_size': _num('batch_size', DEFAULT_BATCH_SIZE, int, 100),
        'batch_sleep_ms': _num('batch_sleep_ms', DEFAULT_BATCH_SLEEP_MS, int, 0),
        'max_rows_per_run': _num('max_rows_per_run', DEFAULT_MAX_ROWS_PER_RUN, int, 1000),
        'alert_events_enabled': bool(cleanup.get('alert_events_enabled', True)),
        # 历史统计日汇总表（status_daily）：回填 + 裁剪
        'daily_stats_enabled': bool(cleanup.get('daily_stats_enabled', True)),
        'daily_stats_rebuild_days': _num('daily_stats_rebuild_days', 2, int, 1),
        'vacuum_enabled': bool(cleanup.get('vacuum_enabled', False)),
        'vacuum_freelist_percent': _num('vacuum_freelist_percent', 20, float, 1),
        'optimize_enabled': bool(cleanup.get('optimize_enabled', True)),
    }


# ============================================================
# 分批清理
# ============================================================

def prune_status_history(conn: sqlite3.Connection, retention_days: int = DEFAULT_RETENTION_DAYS,
                         batch_size: int = DEFAULT_BATCH_SIZE,
                         batch_sleep_ms: int = DEFAULT_BATCH_SLEEP_MS,
                         max_batches: int = 400) -> int:
    """分批删除超过保留期的状态历史记录。

    参数:
        conn: 数据库连接
        retention_days: 保留天数（默认 30）
        batch_size: 单批删除行数
        batch_sleep_ms: 批间休眠毫秒数（让出写锁给探测落库）
        max_batches: 单次调用最多执行的批数（上限保护）

    返回:
        实际删除的记录数
    """
    cutoff = cutoff_string(retention_days)
    deleted_total = 0
    for _ in range(int(max_batches)):
        lock = _write_lock()
        with lock:
            try:
                cursor = conn.execute(
                    """DELETE FROM status_history WHERE id IN (
                           SELECT id FROM status_history
                           WHERE checked_at < ? ORDER BY checked_at LIMIT ?)""",
                    (cutoff, int(batch_size))
                )
                deleted = cursor.rowcount
                conn.commit()
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    pass
                raise
        if deleted <= 0:
            break
        deleted_total += deleted
        if deleted < int(batch_size):
            # 最后一批：已删除全部过期数据
            break
        if batch_sleep_ms:
            time.sleep(batch_sleep_ms / 1000.0)
    return deleted_total


def prune_alert_events(conn: sqlite3.Connection, retention_days: int = DEFAULT_RETENTION_DAYS,
                       batch_size: int = DEFAULT_BATCH_SIZE,
                       batch_sleep_ms: int = DEFAULT_BATCH_SLEEP_MS,
                       max_batches: int = 200) -> int:
    """分批删除超过保留期且【已确认】的告警事件。

    只删已确认事件：未确认的离线事件仍被升级循环（escalation）跟踪，
    清掉会导致升级逻辑失忆，故保留。

    返回:
        实际删除的事件数
    """
    cutoff = cutoff_string(retention_days)
    deleted_total = 0
    for _ in range(int(max_batches)):
        lock = _write_lock()
        with lock:
            try:
                cursor = conn.execute(
                    """DELETE FROM alert_events WHERE id IN (
                           SELECT id FROM alert_events
                           WHERE created_at < ? AND is_acknowledged = 1
                           ORDER BY created_at LIMIT ?)""",
                    (cutoff, int(batch_size))
                )
                deleted = cursor.rowcount
                conn.commit()
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    pass
                raise
        if deleted <= 0:
            break
        deleted_total += deleted
        if deleted < int(batch_size):
            break
        if batch_sleep_ms:
            time.sleep(batch_sleep_ms / 1000.0)
    return deleted_total


def prune_daily_stats(conn: sqlite3.Connection,
                      retention_days: int = DEFAULT_RETENTION_DAYS) -> int:
    """删除超过保留期的日统计行（status_daily）。

    保留期窗口与历史页统计一致：day=今天、week=最近 7 天、month=最近 30 天，
    所以保留 stat_date >= 今天-(retention_days-1)。
    单表行数 = 天数 × 设备数（1000 台 30 天 ≈ 3 万行），直接 DELETE 即可。

    返回:
        删除的行数
    """
    cutoff_date = (datetime.now() - timedelta(days=max(1, int(retention_days)) - 1)) \
        .strftime('%Y-%m-%d')
    lock = _write_lock()
    with lock:
        try:
            cursor = conn.execute(
                "DELETE FROM status_daily WHERE stat_date < ?", (cutoff_date,))
            deleted = cursor.rowcount
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
    return deleted


# ============================================================
# 空间回收与统计
# ============================================================

def database_stats(conn: sqlite3.Connection, exact_history_rows: bool = False) -> dict:
    """数据库体量与保留区间的统计信息（供日志、状态文件、维护命令输出）。

    参数:
        conn: 数据库连接
        exact_history_rows: 是否精确统计 status_history 行数
            （大表 COUNT(*) 需全索引扫描，默认关闭，改用 sqlite_sequence 近似）

    返回:
        {'db_bytes', 'wal_bytes', 'total_bytes', 'page_size', 'page_count', 'freelist_bytes',
         'history_rows', 'oldest_checked_at', 'newest_checked_at', 'auto_vacuum'}
    """
    db_path = conn.execute("PRAGMA database_list").fetchone()[2]
    page_size = conn.execute("PRAGMA page_size").fetchone()[0]
    page_count = conn.execute("PRAGMA page_count").fetchone()[0]
    freelist = conn.execute("PRAGMA freelist_count").fetchone()[0]
    auto_vacuum = conn.execute("PRAGMA auto_vacuum").fetchone()[0]

    def _size(path):
        try:
            return os.path.getsize(path)
        except OSError:
            return 0

    oldest = newest = None
    try:
        row = conn.execute(
            "SELECT MIN(checked_at), MAX(checked_at) FROM status_history").fetchone()
        if row:
            oldest, newest = row[0], row[1]
    except sqlite3.Error:
        pass

    daily_rows = None
    try:
        daily_rows = conn.execute("SELECT COUNT(*) FROM status_daily").fetchone()[0]
    except sqlite3.Error:
        daily_rows = None

    history_rows = None
    if exact_history_rows:
        try:
            history_rows = conn.execute("SELECT COUNT(*) FROM status_history").fetchone()[0]
        except sqlite3.Error:
            history_rows = None
    else:
        try:
            seq = conn.execute(
                "SELECT seq FROM sqlite_sequence WHERE name='status_history'").fetchone()
            history_rows = seq[0] if seq else 0
        except sqlite3.Error:
            history_rows = None

    db_bytes = _size(db_path) if db_path else 0
    wal_bytes = _size(db_path + '-wal') if db_path else 0
    return {
        'db_path': db_path,
        'db_bytes': db_bytes,
        'wal_bytes': wal_bytes,
        # 主库 + WAL 合计：只看主库文件会漏掉尚未 checkpoint 的写入（排障时易误判）
        'total_bytes': db_bytes + wal_bytes,
        'page_size': page_size,
        'page_count': page_count,
        'freelist_bytes': freelist * page_size,
        'history_rows': history_rows,
        'history_rows_exact': bool(exact_history_rows),
        'daily_rows': daily_rows,
        'oldest_checked_at': oldest,
        'newest_checked_at': newest,
        'auto_vacuum': auto_vacuum,
    }


def wal_checkpoint(conn: sqlite3.Connection, mode: str = "TRUNCATE") -> dict:
    """执行 WAL checkpoint（TRUNCATE：把 WAL 内容写回主库并截断 -wal 文件）。"""
    try:
        row = conn.execute(f"PRAGMA wal_checkpoint({mode})").fetchone()
        return {'busy': row[0], 'log_pages': row[1], 'checkpointed': row[2]}
    except sqlite3.Error as e:
        logger.debug(f"WAL checkpoint 失败（忽略）: {e}")
        return {}


def incremental_vacuum(conn: sqlite3.Connection, pages: int = 2000) -> int:
    """增量回收空闲页（仅当 auto_vacuum=INCREMENTAL 时有效，否则返回 0）。

    返回:
        本次回收的页数
    """
    try:
        if conn.execute("PRAGMA auto_vacuum").fetchone()[0] != 2:
            return 0
        before = conn.execute("PRAGMA freelist_count").fetchone()[0]
        conn.execute(f"PRAGMA incremental_vacuum({int(pages)})")
        after = conn.execute("PRAGMA freelist_count").fetchone()[0]
        return max(0, before - after)
    except sqlite3.Error as e:
        logger.debug(f"incremental_vacuum 失败（忽略）: {e}")
        return 0


def optimize(conn: sqlite3.Connection) -> None:
    """PRAGMA optimize：更新查询计划统计，让统计查询稳定选中新索引。"""
    try:
        conn.execute("PRAGMA optimize")
    except sqlite3.Error as e:
        logger.debug(f"PRAGMA optimize 失败（忽略）: {e}")


def compact_database(conn: Optional[sqlite3.Connection] = None) -> dict:
    """VACUUM：整库重写以回收磁盘空间。

    需独占访问，务必在没有其它实例运行时执行（见 --vacuum-now）。
    顺带把 auto_vacuum 切成 INCREMENTAL，之后清理即自动归还空间。

    返回:
        {'before_bytes', 'after_bytes', 'freed_bytes', 'elapsed_s', 'error'}
    """
    conn = conn or get_connection()
    stats_before = database_stats(conn)
    t0 = time.perf_counter()
    result = {
        'before_bytes': stats_before['total_bytes'],
        'after_bytes': stats_before['total_bytes'],
        'freed_bytes': 0,
        'elapsed_s': 0.0,
        'error': None,
    }
    try:
        auto_vacuum = conn.execute("PRAGMA auto_vacuum").fetchone()[0]
        if auto_vacuum == 0:
            # 与 VACUUM 组合可把老库切换成增量回收模式（一次到位）
            conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
        conn.execute("VACUUM")
        wal_checkpoint(conn)
        stats_after = database_stats(conn)
        result['after_bytes'] = stats_after['total_bytes']
        result['freed_bytes'] = max(0, stats_before['total_bytes'] - stats_after['total_bytes'])
    except sqlite3.Error as e:
        result['error'] = str(e)
    result['elapsed_s'] = time.perf_counter() - t0
    if result['error']:
        logger.error(f"数据库压缩失败: {result['error']}")
    else:
        logger.info(
            f"数据库压缩完成: {result['before_bytes'] / 1048576:.1f}MB → "
            f"{result['after_bytes'] / 1048576:.1f}MB "
            f"(回收 {result['freed_bytes'] / 1048576:.1f}MB, "
            f"耗时 {result['elapsed_s']:.1f}s)"
        )
    return result


# ============================================================
# 单轮清理（供后台线程与维护命令共用）
# ============================================================

def run_prune_cycle(conn: Optional[sqlite3.Connection] = None,
                    config: Optional[dict] = None,
                    scheduler=None) -> dict:
    """执行一轮保留期清理：删过期历史 → 删过期已确认告警 → checkpoint/优化。

    参数:
        conn: 数据库连接（默认取当前线程连接）
        config: 应用配置
        scheduler: 可选，MonitorScheduler 实例；开启 vacuum 后用于清理前后暂停探测

    返回:
        统计字典 {'history_deleted', 'alert_deleted', 'history_rows', 'db_bytes',
                 'wal_bytes', 'elapsed_s', 'retention_days', 'auto_vacuum', ...}
    """
    cfg = resolve_cleanup_config(config)
    conn = conn or get_connection()
    t0 = time.perf_counter()

    # 1) 回填/修复历史统计的日汇总表（老库首次升级会重建整个保留期窗口，
    #    只重算最近 2 天；扫描阶段不持锁，探测落库不受影响）
    daily_stats = None
    if cfg['daily_stats_enabled']:
        try:
            from .repositories import HistoryRepository
            daily_stats = HistoryRepository().ensure_daily_stats(
                retention_days=cfg['retention_days'],
                rebuild_days=cfg['daily_stats_rebuild_days'])
            if daily_stats['devices_days']:
                logger.info(
                    f"历史统计日汇总已更新: 重建 {daily_stats['days_rebuilt']} 天, "
                    f"{daily_stats['devices_days']} 条设备-日记录, "
                    f"耗时 {daily_stats['elapsed_s']}s"
                )
        except Exception as e:
            logger.error(f"历史统计日汇总回填失败（不影响清理）: {e}")

    max_batches = max(1, int(cfg['max_rows_per_run'] // cfg['batch_size']))
    history_deleted = prune_status_history(
        conn,
        retention_days=cfg['retention_days'],
        batch_size=cfg['batch_size'],
        batch_sleep_ms=cfg['batch_sleep_ms'],
        max_batches=max_batches,
    )
    alert_deleted = 0
    if cfg['alert_events_enabled']:
        alert_deleted = prune_alert_events(
            conn,
            retention_days=cfg['retention_days'],
            batch_size=cfg['batch_size'],
            batch_sleep_ms=cfg['batch_sleep_ms'],
        )

    # 3) 日汇总表同样按保留期裁剪
    daily_deleted = 0
    if cfg['daily_stats_enabled']:
        try:
            daily_deleted = prune_daily_stats(conn, cfg['retention_days'])
        except Exception as e:
            logger.error(f"日统计清理失败（不影响监控）: {e}")

    vacuum_stats = None
    if (history_deleted or alert_deleted or daily_deleted):
        wal_checkpoint(conn)
        incremental_vacuum(conn)
        if cfg['vacuum_enabled'] and _freelist_percent(conn) >= cfg['vacuum_freelist_percent']:
            paused = False
            if scheduler is not None:
                try:
                    scheduler.pause()
                    paused = True
                    logger.info("已暂停探测，开始 VACUUM 回收磁盘空间")
                except Exception as e:
                    logger.debug(f"暂停调度失败（忽略）: {e}")
            try:
                vacuum_stats = compact_database(conn)
            finally:
                if paused and scheduler is not None:
                    try:
                        scheduler.resume()
                        logger.info("VACUUM 结束，已恢复探测")
                    except Exception:
                        pass

    if cfg['optimize_enabled']:
        optimize(conn)

    stats = database_stats(conn)
    result = {
        'retention_days': cfg['retention_days'],
        'history_deleted': history_deleted,
        'alert_deleted': alert_deleted,
        'daily_deleted': daily_deleted,
        'daily_stats': daily_stats,
        'history_rows': stats['history_rows'],
        'oldest_checked_at': stats['oldest_checked_at'],
        'newest_checked_at': stats['newest_checked_at'],
        'db_bytes': stats['db_bytes'],
        'wal_bytes': stats['wal_bytes'],
        'total_bytes': stats['total_bytes'],
        'freelist_bytes': stats['freelist_bytes'],
        'auto_vacuum': stats['auto_vacuum'],
        'elapsed_s': round(time.perf_counter() - t0, 3),
        'vacuum': vacuum_stats,
    }
    if history_deleted or alert_deleted or daily_deleted:
        logger.info(
            f"历史数据清理完成: 删除 status_history {history_deleted} 行, "
            f"alert_events {alert_deleted} 行, status_daily {daily_deleted} 行"
            f"（保留 {cfg['retention_days']} 天, "
            f"剩余最早记录 {stats['oldest_checked_at']}, "
            f"库 {stats['total_bytes'] / 1048576:.1f}MB, 耗时 {result['elapsed_s']}s）"
        )
        if (history_deleted + alert_deleted) >= cfg['max_rows_per_run']:
            logger.warning(
                "本轮清理达到单轮上限 %d 行，库中仍有超过保留期的数据，"
                "将在后续轮次继续清理。", cfg['max_rows_per_run']
            )
    else:
        logger.debug("历史数据清理：无需删除（无超过保留期的记录）")
    return result


def _freelist_percent(conn: sqlite3.Connection) -> float:
    """空闲页占整库比例（%）。"""
    try:
        page_count = conn.execute("PRAGMA page_count").fetchone()[0]
        freelist = conn.execute("PRAGMA freelist_count").fetchone()[0]
        if page_count <= 0:
            return 0.0
        return freelist * 100.0 / page_count
    except sqlite3.Error:
        return 0.0


# ============================================================
# 后台清理线程
# ============================================================

class DataRetentionWorker:
    """历史数据保留策略后台线程。

    行为：
      - start() 后立即执行一轮，之后每隔 interval_minutes 执行一轮
      - 每轮结果写入 logs/retention_state.json（原子替换），便于长稳观察与排查
      - stop() 可中断等待，最多等 interval 被唤醒一次的时间
    """

    def __init__(self, config: Optional[dict] = None, state_file: Optional[str] = None,
                 scheduler=None, conn_factory=None):
        self._config = config or {}
        self._cfg = resolve_cleanup_config(config)
        self._scheduler = scheduler
        self._conn_factory = conn_factory or get_connection
        self._state_file = state_file or os.path.join(_logs_dir(), 'retention_state.json')
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_result: Optional[dict] = None
        self._total_deleted = 0

    # ---------- 属性 ----------
    @property
    def enabled(self) -> bool:
        return bool(self._cfg['enabled'])

    @property
    def retention_days(self) -> int:
        return int(self._cfg['retention_days'])

    @property
    def last_result(self) -> Optional[dict]:
        return self._last_result

    @property
    def config_snapshot(self) -> dict:
        return dict(self._cfg)

    # ---------- 生命周期 ----------
    def start(self) -> bool:
        """启动后台清理线程（已禁用或已启动时返回 False）。"""
        if not self.enabled:
            logger.info("历史数据自动清理已禁用（storage.cleanup.enabled=false）")
            return False
        if self._thread and self._thread.is_alive():
            return False
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="retention-worker")
        self._thread.start()
        logger.info(
            f"历史数据清理线程已启动: 保留 {self._cfg['retention_days']} 天, "
            f"周期 {self._cfg['interval_minutes']} 分钟, "
            f"单批 {self._cfg['batch_size']} 行"
        )
        return True

    def stop(self, timeout: float = 10.0):
        """停止线程（等待当前批次结束）。"""
        self._stop_event.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=timeout)
        self._thread = None

    # ---------- 执行 ----------
    def run_once(self) -> dict:
        """同步执行一轮清理（测试与维护命令直接调用）。"""
        try:
            conn = self._conn_factory()
            result = run_prune_cycle(conn, self._config, scheduler=self._scheduler)
        except Exception as e:
            logger.error(f"历史数据清理失败（不影响监控）: {e}", exc_info=True)
            result = {'error': str(e), 'history_deleted': 0, 'alert_deleted': 0}
        self._last_result = result
        self._total_deleted += (int(result.get('history_deleted') or 0)
                                + int(result.get('alert_deleted') or 0)
                                + int(result.get('daily_deleted') or 0))
        result['total_deleted'] = self._total_deleted
        result['run_at'] = datetime.now().strftime(_TIME_FMT)
        self._write_state(result)
        return result

    def _loop(self):
        while not self._stop_event.is_set():
            # 每轮重新解析配置（程序内改了 config 字典即刻生效，不必重启线程）
            self._cfg = resolve_cleanup_config(self._config)
            interval_seconds = max(30.0, float(self._cfg['interval_minutes']) * 60.0)
            self.run_once()
            if self._stop_event.wait(interval_seconds):
                break

    def _write_state(self, result: dict):
        """原子写入状态文件（失败不影响业务）。"""
        try:
            os.makedirs(os.path.dirname(self._state_file), exist_ok=True)
            payload = dict(result)
            payload['retention_days'] = self._cfg['retention_days']
            tmp = self._state_file + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self._state_file)
        except Exception as e:
            logger.debug(f"写入清理状态文件失败（忽略）: {e}")


# ============================================================
# 离线维护命令（--vacuum-now）
# ============================================================

def run_maintenance_command(config: Optional[dict] = None,
                            conn: Optional[sqlite3.Connection] = None) -> int:
    from .repositories import HistoryRepository

    """离线维护：清理全部过期数据 + VACUUM 回收磁盘空间。

    必须在主程序未运行时执行：VACUUM 需要独占，若检测到其他实例正在使用
    数据库（写事务探测失败）则直接返回错误码，不改动任何数据。

    返回:
        0 成功；1 检测到其他实例在用数据库；2 执行出错
    """
    conn = conn or get_connection()
    cfg = resolve_cleanup_config(config)

    # 独占探测：其他实例（GUI/CLI）一旦持有写事务，BEGIN IMMEDIATE 会失败
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.rollback()
    except sqlite3.OperationalError as e:
        print(f"[maintenance] 数据库正被占用（{e}），请先退出 DEVICE LINK 主程序后重试。")
        return 1

    before = database_stats(conn, exact_history_rows=True)
    print(f"[maintenance] 清理前: 库 {before['total_bytes'] / 1048576:.1f}MB, "
          f"status_history 约 {before['history_rows']} 行, "
          f"最早记录 {before['oldest_checked_at']}, 保留期 {cfg['retention_days']} 天")

    deleted = prune_status_history(
        conn, retention_days=cfg['retention_days'], batch_size=cfg['batch_size'],
        batch_sleep_ms=cfg['batch_sleep_ms'], max_batches=10_000_000)
    alert_deleted = prune_alert_events(
        conn, retention_days=cfg['retention_days'], batch_size=cfg['batch_size'],
        batch_sleep_ms=cfg['batch_sleep_ms'], max_batches=1_000_000)
    print(f"[maintenance] 已删除 status_history {deleted} 行, "
          f"alert_events {alert_deleted} 行")

    daily = HistoryRepository().ensure_daily_stats(
        retention_days=cfg['retention_days'], force_full=True)
    print(f"[maintenance] 历史统计日汇总已重建: {daily['devices_days']} 条设备-日记录, "
          f"耗时 {daily['elapsed_s']}s")
    daily_deleted = prune_daily_stats(conn, cfg['retention_days'])
    print(f"[maintenance] 已删除 status_daily {daily_deleted} 行")

    vacuum_stats = compact_database(conn)
    after = database_stats(conn, exact_history_rows=True)
    print(f"[maintenance] 清理后: 库 {after['total_bytes'] / 1048576:.1f}MB, "
          f"status_history {after['history_rows']} 行, "
          f"最早记录 {after['oldest_checked_at']}")
    print(f"[maintenance] VACUUM 回收 {vacuum_stats['freed_bytes'] / 1048576:.1f}MB, "
          f"耗时 {vacuum_stats['elapsed_s']:.1f}s")
    return 0 if not vacuum_stats.get('error') else 2
