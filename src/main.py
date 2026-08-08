"""
模块：main.py
功能：DEVICE LINK 主入口
     负责：配置加载→数据库初始化→设备加载→调度器启动→告警引擎启动→看门狗→UI启动

作者：Claude
创建日期：2026-08-07
"""
import os
import sys
import json
import logging
import argparse
from pathlib import Path

# 将项目根目录加入 sys.path（确保免安装运行也能正确 import）
if getattr(sys, 'frozen', False):
    # 打包模式：运行时数据目录 = exe 所在目录（便携版硬性要求）
    _project_root = Path(sys.executable).parent
    _bundle_root = Path(getattr(sys, '_MEIPASS', str(_project_root)))
    # 首次运行：把内置 config/assets 按文件补齐到 exe 目录。
    # 修复：解压包自带空 config/（.gitkeep）时，按“目录是否存在”判断会跳过拷贝 → 首启必崩；
    # 改为按“目标文件是否存在”逐个补齐，且不覆盖用户已有配置。
    import shutil
    for _sub in ('config', 'assets'):
        _src = _bundle_root / _sub
        _dst = _project_root / _sub
        if _src.is_dir():
            _dst.mkdir(parents=True, exist_ok=True)
            for _f in _src.iterdir():
                _target = _dst / _f.name
                if not _target.exists():
                    try:
                        shutil.copy2(_f, _target)
                    except OSError:
                        pass
else:
    _project_root = Path(__file__).parent.parent
sys.path.insert(0, str(_project_root))

from src.storage.database import init_database, get_db_path
from src.storage.repositories import (
    DeviceRepository, HistoryRepository, AlertRepository, ChannelRepository
)
from src.core.monitor_scheduler import MonitorScheduler
from src.alerts.alert_engine import AlertEngine
from src.watchdog.watchdog_manager import HealthCheckThread, WatchdogProcess

logger = logging.getLogger("device-link")


def load_config(config_path: str = None) -> dict:
    """
    加载 YAML 配置文件。
    如果文件不存在，从 config/default_config.yaml 读取并复制。

    参数:
        config_path: 配置文件路径（可选，默认 config/config.yaml）

    返回:
        配置字典
    """
    import yaml

    if config_path is None:
        config_path = os.path.join(_project_root, 'config', 'config.yaml')
    default_config = os.path.join(_project_root, 'config', 'default_config.yaml')

    # 如果用户配置不存在，从默认配置复制
    if not os.path.exists(config_path):
        try:
            import shutil
            os.makedirs(os.path.dirname(config_path), exist_ok=True)
            if os.path.exists(default_config):
                shutil.copy(default_config, config_path)
                logger.info(f"已创建默认配置文件: {config_path}")
            else:
                logger.warning(f"默认配置文件不存在: {default_config}，写入最小配置")
                with open(config_path, 'w', encoding='utf-8') as f:
                    yaml.safe_dump({
                    "app": {"name": "DEVICE LINK", "version": "1.0.1",
                            "start_minimized": True, "minimize_to_tray": True,
                            "single_instance": True},
                    "monitor": {"default_interval_seconds": 30,
                                "default_timeout_ms": 3000,
                                "default_failure_threshold": 3,
                                "default_recovery_threshold": 2,
                                "max_workers": 50, "jitter_schedule": True},
                    "watchdog": {"enabled": True, "heartbeat_interval_seconds": 5,
                                 "heartbeat_timeout_seconds": 15,
                                 "max_restart_attempts": 3,
                                 "restart_cooldown_seconds": 30},
                    "notify": {"digest": {"enabled": True, "window_seconds": 300,
                                          "max_events_per_digest": 50,
                                          "send_immediate_if_critical": True},
                               "retry_count": 3, "retry_backoff_base_seconds": 5,
                               "cooldown_seconds": 1800, "escalation_minutes": 15},
                    "storage": {"engine": "sqlite", "path": "./data/device-link.db",
                                "history_retention_days": 90},
                    "logging": {"level": "INFO", "path": "./logs/device-link.log",
                                "max_size_mb": 100, "backup_count": 5},
                    "ui": {"theme": "light", "accent_color": "#1890FF",
                           "font_family": "Microsoft YaHei", "refresh_ms": 2000,
                           "tray_notify_on_event": True,
                           "card_border_radius": 8, "enable_animations": True},
                    }, f, allow_unicode=True)
        except Exception as e:
            # 安装目录不可写：用内存默认配置继续运行，不崩溃
            logger.error(f"无法写入配置文件 {config_path}: {e}，使用默认配置运行")

    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f) or {}
    except Exception as e:
        logger.error(f"配置文件解析失败: {e}")
        config = {}
    return config


def setup_logging(config: dict):
    """配置日志系统。"""
    log_cfg = config.get('logging', {})
    level = getattr(logging, log_cfg.get('level', 'INFO'))
    log_path = log_cfg.get('path', './logs/device-link.log')
    if not os.path.isabs(log_path):
        log_path = os.path.join(_project_root, log_path)

    handlers = []
    try:
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        handlers.append(logging.FileHandler(log_path, encoding='utf-8'))
    except Exception as e:
        # 安装目录不可写时降级到用户可写目录，保证日志一定可用
        fallback_dir = os.path.join(
            os.environ.get('LOCALAPPDATA', os.path.expanduser('~')),
            'DEVICE-LINK', 'logs')
        try:
            os.makedirs(fallback_dir, exist_ok=True)
            fallback_path = os.path.join(fallback_dir, 'device-link.log')
            handlers.append(logging.FileHandler(fallback_path, encoding='utf-8'))
            print(f"[setup_logging] 默认日志路径不可写，降级到: {fallback_path} ({e})")
        except Exception as e2:
            print(f"[setup_logging] 日志初始化失败: {e2}")

    handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(
        level=level,
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
        handlers=handlers,
    )
    logger.info(f"日志已初始化: level={logging.getLevelName(level)}, path={log_path}, "
                f"exe_dir={_project_root}")


def main():
    """主函数。"""
    parser = argparse.ArgumentParser(description='DEVICE LINK 内网设备监控告警系统')
    parser.add_argument('--config', help='配置文件路径')
    parser.add_argument('--no-watchdog', action='store_true', help='禁用看门狗')
    parser.add_argument('--cli', action='store_true', help='命令行模式（不启动 GUI）')
    args = parser.parse_args()

    # 1. 加载配置
    config = load_config(args.config)
    config_path = args.config or os.path.join(_project_root, 'config', 'config.yaml')

    # 2. 配置日志
    setup_logging(config)
    logger.info("=" * 50)
    logger.info(f"DEVICE LINK v{config.get('app', {}).get('version', '1.0.0')} 启动中...")

    # 3. 数据库初始化（设置全局路径 + 建表，各线程通过 get_connection() 获取独立连接）
    db_path = get_db_path(config)
    conn = init_database(db_path, config)
    logger.info(f"数据库已初始化: {db_path}")

    # 4. 创建仓库实例（每个线程自动获取自己的连接，杜绝跨线程共享导致的 sqlite3.dll 崩溃）
    device_repo = DeviceRepository()
    history_repo = HistoryRepository()
    alert_repo = AlertRepository()
    channel_repo = ChannelRepository()

    # 5. 加载设备列表
    devices = device_repo.list_enabled_devices()
    if not devices:
        logger.info("暂无已启用设备。请通过 GUI 或配置文件添加设备。")

    # 6. 启动调度器
    scheduler = MonitorScheduler(devices, config, device_repo, history_repo)

    # 7. 启动告警引擎
    alert_engine = AlertEngine(config, alert_repo)
    scheduler.register_callback(alert_engine.on_monitor_event)
    alert_engine.run_escalation_loop()

    # 8. 启动看门狗
    if not args.no_watchdog:
        watchdog_cfg = config.get('watchdog', {})
        if watchdog_cfg.get('enabled', True):
            # 第 1 层：内部健康检查
            health_thread = HealthCheckThread(
                scheduler,
                watchdog_cfg.get('heartbeat_interval_seconds', 5),
                watchdog_cfg.get('heartbeat_timeout_seconds', 30),
            )
            health_thread.start()
            logger.info("看门狗（第 1 层）已启动")

            # 第 2 层：看门狗子进程
            watchdog = WatchdogProcess(
                watchdog_cfg.get('heartbeat_timeout_seconds', 15),
                watchdog_cfg.get('max_restart_attempts', 3),
                watchdog_cfg.get('restart_cooldown_seconds', 30),
            )
            watchdog.start(os.getpid())
            logger.info("看门狗（第 2 层）已启动")

    # 9. 启动调度器
    scheduler.start()

    # 10. 启动 UI 或 CLI 模式
    if args.cli:
        # CLI 模式：持续运行，Ctrl+C 退出
        logger.info("CLI 模式运行中，按 Ctrl+C 退出...")
        try:
            while True:
                import time
                time.sleep(1)
        except KeyboardInterrupt:
            logger.info("收到退出信号")
    else:
        # GUI 模式：启动 PySide6 界面
        try:
            from src.ui.main_window import MainWindow
            from PySide6.QtWidgets import QApplication
            from PySide6.QtCore import Qt

            app = QApplication(sys.argv)
            app.setApplicationName("DEVICE LINK")
            app.setOrganizationName("DEVICE-LINK")

            window = MainWindow(config, scheduler, device_repo, history_repo, alert_repo,
                                alert_engine=alert_engine, config_path=config_path)
            window.show()

            if config.get('app', {}).get('start_minimized', False):
                window.hide()

            logger.info("GUI 已启动")
            sys.exit(app.exec())

        except ImportError as e:
            logger.warning(f"PySide6 不可用({e})，切换到 CLI 模式")
            print("PySide6 未安装，使用 CLI 模式。运行 --cli 以静默启动。")
            try:
                while True:
                    import time
                    snapshot = scheduler.get_snapshot()
                    online = sum(1 for s in snapshot.values() if s == 'online')
                    offline = sum(1 for s in snapshot.values() if s == 'offline')
                    print(f"\r设备: 在线 {online} | 离线 {offline} | 共 {len(snapshot)} 台", end='')
                    time.sleep(2)
            except KeyboardInterrupt:
                print("\n退出")

    # 11. 清理
    scheduler.stop()
    logger.info("DEVICE LINK 已退出")


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        import datetime
        import traceback
        tb = traceback.format_exc()
        fatal_dir = os.path.join(
            os.environ.get('LOCALAPPDATA', os.path.expanduser('~')), 'DEVICE-LINK')
        fatal = os.path.join(fatal_dir, 'fatal.log')
        try:
            os.makedirs(fatal_dir, exist_ok=True)
            with open(fatal, 'a', encoding='utf-8') as f:
                f.write(f"\n[{datetime.datetime.now()}] {tb}\n")
        except Exception:
            pass
        try:
            from PySide6.QtWidgets import QApplication, QMessageBox
            app = QApplication.instance() or QApplication([])
            QMessageBox.critical(None, "DEVICE LINK 启动失败",
                                 f"启动过程发生错误：\n{e}\n\n详情见 {fatal}")
        except Exception:
            pass
        raise
