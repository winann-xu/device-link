"""
模块：watchdog_manager.py
功能：看门狗守护 —— 确保 7×24 不中断

三层守护策略：
  第 1 层（内部健康检查）：HealthCheckThread 每 5 秒检查调度器心跳，
      主循环卡死 > heartbeat_timeout → os._exit(1)，由第 2 层拉起
  第 2 层（守护子进程）：WatchdogProcess 由主进程拉起为【真正的独立进程】，
      检测父进程异常退出 → 按持久化状态重启；正常退出（写有标记）不重启；
      连续崩溃达 max_restarts 次（计数跨重启持久化）→ 停止重试
  第 3 层（开机自启）：shell:startup 快捷方式（用户可选）

稳定性加固（v1.0.7）：
  - 第 2 层从“进程内线程”改为“独立子进程”：主进程 os._exit(1) 后
    仍能拉起新实例（原实现线程随主进程一起死，重启路径失效）
  - 重启计数/最近重启时间写入状态文件，跨重启生效，避免无限杀/重启循环
  - 正常退出前写 clean_shutdown 标记，看门狗识别后不再重启
  - 冻结打包（PyInstaller）重启命令为 [exe]，开发模式为 [python, main.py]

作者：Claude
创建日期：2026-08-07
"""
import os
import sys
import time
import json
import threading
import logging
import subprocess
from datetime import datetime

logger = logging.getLogger("device-link.watchdog")


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
        self._stop_event = threading.Event()

    def stop(self):
        """停止健康检查线程（测试/退出时调用，避免守护线程残留）。"""
        self._stop_event.set()

    def run(self):
        """主循环：定期检查调度器心跳。"""
        while not self._stop_event.wait(self._interval):
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


def _project_root() -> str:
    """运行时根目录：冻结打包 = exe 目录；开发模式 = 仓库根目录。"""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )


def _main_script() -> str:
    """主程序脚本路径（开发模式用；冻结打包不需要）。"""
    return os.path.join(_project_root(), 'src', 'main.py')


def _default_state_file() -> str:
    return os.path.join(_project_root(), 'logs', 'watchdog_state.json')


class WatchdogProcess:
    """
    第 2 层守护：看门狗独立子进程。
    由主进程启动为独立进程，轮询父进程存活状态；
    父进程异常退出时按持久化状态重启，正常退出（标记）则不重启。

    注意（v1.0.7）：原实现是主进程内的守护线程——第 1 层 os._exit(1)
    会连看门狗线程一起杀死，重启路径实际从未生效（表现为无限重启）。
    现改为真正的子进程，且重启计数写入状态文件跨重启生效。
    """

    def __init__(self, heartbeat_timeout: int = 15,
                 max_restarts: int = 3,
                 cooldown: int = 30,
                 healthy_threshold: int = 300,
                 state_file: str = None):
        self._timeout = heartbeat_timeout
        self._max_restarts = max_restarts
        self._cooldown = cooldown
        # 父进程存活超过该时长 → 视为稳定，重启计数复位
        self._healthy_threshold = healthy_threshold
        self._state_file = state_file
        self._restart_count = 0
        self._last_restart_time = 0.0
        self._running = False
        self._subprocess = None

    # ==================== 状态文件 ====================

    def _state_path(self) -> str:
        return self._state_file or _default_state_file()

    def _read_state(self) -> dict:
        default = {
            'restart_args': [],
            'restart_count': 0,
            'last_restart_ts': 0.0,
            'clean_shutdown': False,
            'gave_up': False,
        }
        try:
            with open(self._state_path(), 'r', encoding='utf-8') as f:
                data = json.load(f)
            for k, v in default.items():
                data.setdefault(k, v)
            return data
        except Exception:
            return default

    def _write_state(self, state: dict):
        """原子写状态文件（临时文件 + replace，避免半截 JSON）。"""
        path = self._state_path()
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(state, f, ensure_ascii=False)
            os.replace(tmp, path)
        except Exception as e:
            logger.error(f"写入看门狗状态失败: {e}")

    def _load_state(self):
        """从状态文件载入重启计数（独立看门狗进程启动时调用）。"""
        state = self._read_state()
        self._restart_count = int(state.get('restart_count', 0))
        self._last_restart_time = float(state.get('last_restart_ts', 0.0))
        return state

    def _reset_if_healthy(self) -> bool:
        """主进程稳定运行超过 healthy_threshold → 重启计数复位。"""
        state = self._read_state()
        last = float(state.get('last_restart_ts', 0.0))
        if last > 0 and (time.time() - last) > self._healthy_threshold:
            if state.get('restart_count', 0) > 0:
                state['restart_count'] = 0
                self._write_state(state)
                logger.info("主进程稳定运行，重启计数已复位为 0")
            return True
        return False

    # ==================== 命令构造 ====================

    def _restart_command(self) -> list:
        """重启主程序的命令。"""
        if getattr(sys, 'frozen', False):
            return [sys.executable]
        return [sys.executable, _main_script()]

    def _watchdog_command(self, parent_pid: int) -> list:
        """启动独立看门狗子进程的命令。"""
        if getattr(sys, 'frozen', False):
            base = [sys.executable]
        else:
            base = [sys.executable, _main_script()]
        return base + [
            '--watchdog',
            '--parent-pid', str(parent_pid),
            '--heartbeat-timeout', str(self._timeout),
            '--max-restarts', str(self._max_restarts),
            '--cooldown', str(self._cooldown),
            '--state-file', self._state_path(),
        ]

    # ==================== 生命周期 ====================

    def start(self, parent_pid: int, restart_command: list = None):
        """
        启动独立看门狗子进程（由主进程调用）。

        参数:
            parent_pid: 被守护的主进程 PID
            restart_command: 重启主程序用的命令（默认按运行模式推导）
        """
        state = self._read_state()
        state['restart_args'] = restart_command or self._restart_command()
        state['clean_shutdown'] = False
        self._write_state(state)
        cmd = self._watchdog_command(parent_pid)
        try:
            self._subprocess = subprocess.Popen(
                cmd,
                creationflags=subprocess.CREATE_NO_WINDOW
                if sys.platform == 'win32' else 0,
            )
            logger.info(
                f"看门狗（独立进程）已启动：监控 PID={parent_pid}, "
                f"看门狗 PID={self._subprocess.pid}"
            )
        except Exception as e:
            logger.error(f"启动看门狗子进程失败: {e}")

    def stop(self):
        """停止看门狗。"""
        self._running = False

    def mark_clean_shutdown(self):
        """主进程正常退出前调用：写入标记，防止看门狗误重启。"""
        state = self._read_state()
        state['clean_shutdown'] = True
        self._write_state(state)
        logger.info("已写入正常退出标记（看门狗将不再重启）")

    def run_forever(self, parent_pid: int):
        """
        看门狗监控主循环（在 --watchdog 独立子进程内运行）。
        轮询父进程存活；异常退出 → 按状态重启；正常退出 → 结束。
        """
        self._running = True
        state = self._load_state()
        logger.info(
            f"看门狗子进程启动：监控 PID={parent_pid}, "
            f"重启计数={self._restart_count}, 冷却={self._cooldown}s"
        )
        while self._running:
            time.sleep(self._timeout)
            try:
                alive = self._is_process_alive(parent_pid)
                state = self._read_state()
                if alive:
                    # 稳定运行足够久 → 复位重启计数
                    self._reset_if_healthy()
                    continue

                # 父进程已退出
                if state.get('clean_shutdown'):
                    logger.info("检测到正常退出标记，看门狗结束（不重启）")
                    return
                if state.get('gave_up'):
                    logger.critical("此前已停止重试，看门狗结束")
                    return

                logger.warning(f"父进程 PID={parent_pid} 已退出（非正常关闭），准备重启...")
                if not self._can_restart():
                    logger.critical(
                        f"连续崩溃 {self._max_restarts} 次，停止重试。"
                        f"请手动检查系统状态。"
                    )
                    state['gave_up'] = True
                    self._write_state(state)
                    return

                now = time.time()
                if self._last_restart_time > 0 and \
                        (now - self._last_restart_time) < self._cooldown:
                    logger.warning(
                        f"冷却中（{(now - self._last_restart_time):.0f}s），不重启"
                    )
                    continue

                self._restart_count += 1
                self._last_restart_time = now
                state['restart_count'] = self._restart_count
                state['last_restart_ts'] = now
                state['clean_shutdown'] = False
                self._write_state(state)
                logger.info(f"正在重启 DEVICE LINK（第 {self._restart_count} 次）...")
                restart_cmd = state.get('restart_args') or self._restart_command()
                subprocess.Popen(
                    restart_cmd,
                    creationflags=subprocess.CREATE_NO_WINDOW
                    if sys.platform == 'win32' else 0,
                )
                # 重启后本看门狗退出，由新主进程拉起新的看门狗
                return
            except Exception as e:
                logger.error(f"看门狗监控异常: {e}", exc_info=True)

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
                    # 重新应用修复：GUI 程序（无控制台）spawn 控制台子进程会弹黑色终端框
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
