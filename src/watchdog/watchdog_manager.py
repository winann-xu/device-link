"""
模块：watchdog_manager.py
功能：看门狗守护 —— 确保 7×24 不中断

三层守护策略：
  第 1 层（内部健康检查）：HealthCheckThread 每 5 秒检查调度器心跳，
      主循环卡死 > 30 秒 → sys.exit(1)，由第 2 层拉起
  第 2 层（守护子进程）：WatchdogProcess 独立子进程，
      命名管道心跳通信，超时 15 秒 → kill → 重启
  第 3 层（开机自启）：shell:startup 快捷方式（用户可选）

作者：Claude
创建日期：2026-08-07
"""
import os
import sys
import time
import json
import threading
import logging
import signal
import subprocess
from datetime import datetime

logger = logging.getLogger("device-link.watchdog")

# 命名管道路径
PIPE_NAME = r'\\.\pipe\DeviceLinkWatchdog'


class HealthCheckThread(threading.Thread):
    """
    第 1 层守护：内部健康检查线程。
    每 5 秒检查调度器心跳，卡死 > 30 秒 → 触发 sys.exit(1)。
    """

    def __init__(self, scheduler, heartbeat_interval: int = 5,
                 heartbeat_timeout: int = 30):
        super().__init__(daemon=True, name="health-check")
        self._scheduler = scheduler
        self._interval = heartbeat_interval
        self._timeout = heartbeat_timeout

    def run(self):
        """主循环：定期检查调度器心跳。"""
        while True:
            time.sleep(self._interval)
            try:
                health = self._scheduler.get_health()
                last_tick = health.get('last_tick')
                if last_tick:
                    last_time = datetime.fromisoformat(last_tick)
                    since_last = (datetime.now() - last_time).total_seconds()
                    if since_last > self._timeout:
                        logger.critical(
                            f"调度器卡死！已 {since_last:.0f} 秒无心跳，触发 sys.exit(1)"
                        )
                        os._exit(1)
            except Exception as e:
                logger.error(f"健康检查异常: {e}")


class WatchdogProcess:
    """
    第 2 层守护：看门狗子进程。
    通过 Windows 命名管道与主进程通信，超时则重启。
    在非 Windows 平台使用简单轮询降级方案。
    """

    def __init__(self, heartbeat_timeout: int = 15,
                 max_restarts: int = 3,
                 cooldown: int = 30):
        self._timeout = heartbeat_timeout
        self._max_restarts = max_restarts
        self._cooldown = cooldown
        self._restart_count = 0
        self._last_restart_time = 0.0
        self._running = False

    def start(self, parent_pid: int):
        """
        启动看门狗监控循环。

        参数:
            parent_pid: 被守护的主进程 PID
        """
        self._running = True
        threading.Thread(
            target=self._monitor_loop, args=(parent_pid,),
            daemon=True, name="watchdog-monitor"
        ).start()
        logger.info(f"看门狗已启动：监控 PID={parent_pid}")

    def stop(self):
        """停止看门狗。"""
        self._running = False

    def _is_process_alive(self, pid: int) -> bool:
        """
        检查进程是否存活（跨平台安全实现）。

        Bug M 修复：原代码用 os.kill(pid, 0) 检查进程存活。
        Unix 上信号 0 是空操作（仅检查权限），但在 Windows 上
        os.kill() 会调用 TerminateProcess() 实际杀死目标进程！
        导致看门狗每 15 秒杀死自己的主进程。

        修复：Windows 用 tasklist 命令查询 PID 是否存在；
        Unix 保持 os.kill(pid, 0) 的空信号检查。
        """
        if sys.platform == 'win32':
            try:
                # /FI 过滤指定 PID，/NH 去掉表头
                result = subprocess.run(
                    ['tasklist', '/FI', f'PID eq {pid}', '/NH'],
                    capture_output=True, text=True, timeout=5,
                    # Bug S 修复：GUI 程序（无控制台）直接 spawn 控制台子进程会
                    # 每 15 秒弹出一个黑色终端窗口，必须加 CREATE_NO_WINDOW
                    creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
                )
                return str(pid) in result.stdout
            except Exception:
                # 查询失败时保守假定进程存活，避免误杀
                return True
        else:
            try:
                os.kill(pid, 0)
                return True
            except OSError:
                return False

    def _monitor_loop(self, parent_pid: int):
        """
        看门狗监控循环。

        每 heartbeat_timeout 秒检查父进程是否存活，
        父进程退出后按冷却策略重启。
        """
        while self._running:
            time.sleep(self._timeout)
            if not self._is_process_alive(parent_pid):
                # 父进程不存在
                logger.warning(f"父进程 PID={parent_pid} 已退出，准备重启...")
                if self._can_restart():
                    self._restart_count += 1
                    self._last_restart_time = time.time()
                    logger.info(f"正在重启 DEVICE LINK（第 {self._restart_count} 次）...")
                    try:
                        # 重新启动主程序
                        exe = sys.executable
                        script = os.path.join(
                            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            'main.py'
                        )
                        subprocess.Popen(
                            [exe, script],
                            creationflags=0x00000008 if sys.platform == 'win32' else 0
                        )
                    except Exception as e:
                        logger.critical(f"重启失败: {e}")
                else:
                    logger.critical(
                        f"连续崩溃 {self._max_restarts} 次，停止重试。"
                        f"请手动检查系统状态。"
                    )
                    break

    def _can_restart(self) -> bool:
        """检查是否允许再次重启。"""
        now = time.time()
        # 冷却检查
        if self._last_restart_time > 0 and (now - self._last_restart_time) < self._cooldown:
            logger.warning(f"冷却中（{(now - self._last_restart_time):.0f}s），不重启")
            return False
        # 最大重启次数
        if self._restart_count >= self._max_restarts:
            return False
        return True


def setup_startup_shortcut(exe_path: str) -> bool:
    """
    创建开机自启快捷方式（shell:startup 目录）。
    仅在 Windows 平台有效。

    参数:
        exe_path: DEVICE-LINK.exe 的完整路径

    返回:
        True 表示创建成功
    """
    if sys.platform != 'win32':
        return False
    try:
        import winshell
        startup = winshell.startup()
        shortcut_path = os.path.join(startup, 'DEVICE LINK.lnk')
        with winshell.shortcut(shortcut_path) as link:
            link.path = exe_path
            link.working_directory = os.path.dirname(exe_path)
            link.description = "DEVICE LINK 内网设备监控告警系统"
            link.icon_location = (exe_path, 0)
        logger.info(f"已创建开机自启快捷方式: {shortcut_path}")
        return True
    except ImportError:
        # winshell 未安装，使用手动创建
        try:
            import pythoncom
            from win32com.client import Dispatch
            startup_dir = os.path.join(
                os.environ.get('APPDATA', ''),
                r'Microsoft\Windows\Start Menu\Programs\Startup'
            )
            shortcut_path = os.path.join(startup_dir, 'DEVICE LINK.lnk')
            shell = Dispatch('WScript.Shell')
            shortcut = shell.CreateShortCut(shortcut_path)
            shortcut.Targetpath = exe_path
            shortcut.WorkingDirectory = os.path.dirname(exe_path)
            shortcut.Description = "DEVICE LINK 内网设备监控告警系统"
            shortcut.IconLocation = exe_path + ',0'
            shortcut.save()
            logger.info(f"已创建开机自启快捷方式: {shortcut_path}")
            return True
        except Exception as e:
            logger.error(f"创建快捷方式失败: {e}")
            return False


def remove_startup_shortcut() -> bool:
    """移除开机自启快捷方式。"""
    if sys.platform != 'win32':
        return False
    try:
        import winshell
        startup = winshell.startup()
        shortcut_path = os.path.join(startup, 'DEVICE LINK.lnk')
        if os.path.exists(shortcut_path):
            os.remove(shortcut_path)
            return True
    except Exception as e:
        logger.error(f"移除快捷方式失败: {e}")
    return False
