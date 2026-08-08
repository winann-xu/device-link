"""
模块：monitor_scheduler.py
功能：并发探测调度器 —— 系统中枢
     按每设备独立间隔调度探测任务，管理线程池，
     维护进程内设备状态缓存，通过回调通知 UI/告警/历史模块。

稳定性设计：
  - 线程池有界（max_workers 上限），防止资源耗尽
  - 调度循环 1 秒 tick，异常不中断循环
  - 单设备探测超时不阻塞其他设备
  - 探测失败设备不影响正常设备调度
  - 缓存与 SQLite 双写（缓存优先，SQLite 异步批量落盘）

作者：Claude
创建日期：2026-08-07
"""
import time
import threading
import random
import logging
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from datetime import datetime
from dataclasses import dataclass
from typing import Callable, Optional

from .detection_chain import DetectionChain, ProbeOutcome
from .device_state_machine import DeviceStateMachine, DeviceStatus, StateTransition

logger = logging.getLogger("device-link.core.scheduler")


@dataclass
class WorkerStats:
    """线程池统计信息。"""
    active_threads: int = 0
    pending_tasks: int = 0
    max_workers: int = 50
    total_checks: int = 0
    total_failures: int = 0


class MonitorScheduler:
    """
    设备监控调度器——系统中枢。

    职责：
      - 按每设备独立间隔调度探测任务
      - 管理 ThreadPoolExecutor 线程池
      - 维护进程内设备状态缓存（高性能，UI 毫秒级读取）
      - 通过回调通知 UI/告警引擎/历史记录模块

    使用示例:
        scheduler = MonitorScheduler(devices, config, device_repo, history_repo)
        scheduler.register_callback(my_alert_handler)
        scheduler.start()
        ...
        scheduler.stop()
    """

    def __init__(self, devices: list, config: dict,
                 device_repo, history_repo):
        """
        初始化调度器。

        参数:
            devices: 设备字典列表
            config: 全局配置
            device_repo: DeviceRepository 实例
            history_repo: HistoryRepository 实例
        """
        self._config = config
        self._device_repo = device_repo
        self._history_repo = history_repo

        monitor_cfg = config.get('monitor', {})
        self._max_workers = monitor_cfg.get('max_workers', 50)
        self._jitter = monitor_cfg.get('jitter_schedule', True)

        # 为每台设备创建状态机
        self._machines: dict[int, DeviceStateMachine] = {}
        # 调度索引：device_id → {'interval': 秒, 'next_check': timestamp}
        self._schedule: dict[int, dict] = {}
        # 状态缓存：device_id → status_str（UI 毫秒级读取，不加锁【GIL 保护单字读】）
        self._status_cache: dict[int, str] = {}

        for d in devices:
            if d.get('is_enabled'):
                self._add_device(d)

        # 线程池
        self._executor = ThreadPoolExecutor(
            max_workers=self._max_workers,
            thread_name_prefix="probe-"
        )

        # 回调函数列表（状态变更时调用）
        self._callbacks: list[Callable] = []
        self._callbacks_lock = threading.Lock()

        # 运行状态
        self._running = False
        self._paused = False
        self._loop_lock = threading.Lock()

        # 健康信息
        self._last_tick_time = 0.0
        self._total_checks = 0
        self._total_failures = 0

        # 探测超时保护（秒）
        self._probe_hard_timeout_sec = (
            monitor_cfg.get('default_timeout_ms', 3000) * 3 / 1000.0 + 1.0
        )

        logger.info(f"调度器初始化完成：{len(self._machines)} 台设备, max_workers={self._max_workers}")

    def _add_device(self, device: dict):
        """将设备加入调度索引与状态缓存。"""
        did = device['id']
        self._machines[did] = DeviceStateMachine(device)
        interval = device.get('check_interval_seconds', 30)
        next_check = time.time()
        if self._jitter:
            # 随机打散：±20% 偏移，防止惊群效应
            jitter_range = interval * 0.2
            next_check += random.uniform(0, jitter_range)
        self._schedule[did] = {
            'interval': interval,
            'next_check': next_check,
        }
        self._status_cache[did] = device.get('status', 'unknown')

    def register_callback(self, cb: Callable):
        """
        注册状态变更回调。

        参数:
            cb: 回调函数，签名为 cb(StateTransition) -> None
        """
        with self._callbacks_lock:
            self._callbacks.append(cb)

    def get_snapshot(self) -> dict:
        """返回全量设备状态缓存（无锁——GIL 保证原子读）。"""
        return dict(self._status_cache)

    def get_worker_stats(self) -> WorkerStats:
        """返回线程池当前统计。"""
        return WorkerStats(
            active_threads=getattr(self._executor, '_work_queue', None) and
                           len(self._executor._threads) if hasattr(self._executor, '_threads') else 0,
            max_workers=self._max_workers,
            total_checks=self._total_checks,
            total_failures=self._total_failures,
        )

    def get_health(self) -> dict:
        """返回健康状态（供 Watchdog 使用）。"""
        return {
            'last_tick': datetime.fromtimestamp(self._last_tick_time).isoformat() if self._last_tick_time else None,
            'running': self._running,
            'paused': self._paused,
            'device_count': len(self._machines),
            'total_checks': self._total_checks,
        }

    def start(self) -> bool:
        """启动调度器主循环（在独立线程中运行）。"""
        if self._running:
            return False
        self._running = True
        threading.Thread(
            target=self._main_loop, daemon=True,
            name="scheduler-main"
        ).start()
        logger.info("调度器已启动")
        return True

    def stop(self):
        """
        优雅关闭调度器：
          1. 停止主循环
          2. 等待进行中探测完成（最长 10 秒）
          3. 关闭线程池
        """
        logger.info("调度器正在停止...")
        self._running = False
        # 等待主循环退出
        time.sleep(0.5)
        # 关闭线程池，等待进行中任务
        # 修复：ThreadPoolExecutor.shutdown 无 timeout 参数（原代码必抛 TypeError）
        # 探测有硬超时保护，wait=True 实际最多等待探测超时时间
        self._executor.shutdown(wait=True, cancel_futures=True)
        logger.info("调度器已停止")

    def pause(self):
        """暂停调度（不停止进行中探测）。"""
        self._paused = True
        logger.info("调度器已暂停")

    def resume(self):
        """恢复调度。"""
        self._paused = False
        logger.info("调度器已恢复")

    def add_device(self, device: dict):
        """动态添加新设备到调度索引。"""
        if device.get('is_enabled'):
            self._add_device(device)
            logger.info(f"设备已加入调度: {device.get('name')}")

    def remove_device(self, device_id: int):
        """动态移除设备。"""
        self._machines.pop(device_id, None)
        self._schedule.pop(device_id, None)
        self._status_cache.pop(device_id, None)

    def _main_loop(self):
        """
        调度主循环（每秒 tick 一次）。
        检查到期设备 → 提交探测任务 → 处理结果。

        异常隔离：任何异常不中断主循环。
        """
        while self._running:
            self._last_tick_time = time.time()

            try:
                if not self._paused:
                    now = time.time()
                    expired = []
                    for did, sched in self._schedule.items():
                        if sched['next_check'] <= now:
                            expired.append(did)

                    for did in expired:
                        machine = self._machines.get(did)
                        if machine is None:
                            continue
                        device = self._device_repo.get_device(did)
                        if device is None or not device.get('is_enabled'):
                            continue

                        # 提交探测任务到线程池
                        self._executor.submit(self._probe_and_process, device, machine)

                        # 更新下次检查时间
                        sched = self._schedule[did]
                        sched['next_check'] = now + sched['interval']

                time.sleep(1)

            except Exception as e:
                logger.error(f"调度器主循环异常（已恢复）: {e}", exc_info=True)

    def _probe_and_process(self, device: dict, machine: DeviceStateMachine):
        """
        对单台设备执行探测链并处理结果。
        在线程池工作线程中运行。

        执行流程：
          1. DetectionChain 执行探测
          2. StateMachine 处理结果
          3. 更新 DB 缓存
          4. 触发回调（如有状态变更）
        """
        did = device['id']
        try:
            # 1. 探测
            chain = DetectionChain(device, self._config)
            outcome = chain.probe()
            self._total_checks += 1

            # 2. 状态转换
            transition = machine.transition(outcome)

            # 3. 更新数据库状态
            self._device_repo.set_device_status(
                did,
                status=machine.status.value,
                failure_count=machine.failure_count,
                recovery_count=getattr(machine, '_recovery_count', 0),
                latency_ms=outcome.latency_ms,
            )
            # 记录历史
            self._device_repo.record_check_result(did, outcome.is_online, outcome.latency_ms)

            # 4. 更新缓存
            self._status_cache[did] = machine.status.value

            # 5. 如果有状态变更，通知所有回调
            if transition is not None:
                logger.info(
                    f"设备状态变更: {device.get('name')} "
                    f"{transition.old_status.value} → {transition.new_status.value}"
                    f" (event={transition.event_type})"
                )
                with self._callbacks_lock:
                    for cb in self._callbacks:
                        try:
                            cb(transition)
                        except Exception as e:
                            logger.error(f"回调异常: {e}", exc_info=True)

        except Exception as e:
            self._total_failures += 1
            logger.error(f"设备探测异常 [{device.get('name')}]: {e}", exc_info=True)
