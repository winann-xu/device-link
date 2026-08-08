"""
模块：device_state_machine.py
功能：设备状态机 —— 系统稳定性核心组件
     实现设备在线/离线/待定失败/恢复四种状态转换，
     包含误报抑制（N 次连续失败判定）和恢复阈值机制。
     线程安全——所有状态转换受锁保护。

状态转换规则：
    UNKNOWN         → ONLINE           (首次探测成功)
    ONLINE          → PENDING_FAILURE  (单次失败, 未达告警阈值, 黄色不告警)
    ONLINE          → OFFLINE          (连续失败 ≥ N 次, 触发告警)
    PENDING_FAILURE → ONLINE           (任意成功, failure_count 清零)
    PENDING_FAILURE → OFFLINE          (连续失败 ≥ N 次, 触发告警!)
    OFFLINE         → ONLINE           (连续成功 ≥ M 次, 触发恢复通知)
    任意状态        → 维护模式(状态照常但 suppress_alert=True)

误报抑制三保险：
    1. N 次连续失败（默认 3 次）—— 单次抖动不告警
    2. TCP 端口复核（DetectionChain 层）—— ping 不通但端口通 = 在线
    3. 维护窗口静默 —— suppress_alert=True

作者：Claude
创建日期：2026-08-07
"""
import threading
import logging
from enum import Enum
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from .detection_chain import ProbeOutcome

logger = logging.getLogger("device-link.core.state_machine")


class DeviceStatus(Enum):
    """设备状态枚举。"""
    UNKNOWN = "unknown"           # 尚无探测数据（灰色）
    ONLINE = "online"             # 在线（绿色）
    PENDING_FAILURE = "pending_failure"  # 待定失败（黄色，不告警！）
    OFFLINE = "offline"           # 离线（红色，触发告警）


@dataclass
class StateTransition:
    """
    状态转换结果。

    字段:
        device_id: 设备 ID
        old_status: 旧状态
        new_status: 新状态
        event_type: 事件类型 —— 'device_offline' | 'device_recovered' | None（状态无变化）
        failure_count: 当前连续失败次数
        recovery_count: 当前连续成功次数
        downtime_start: 最近一次掉线开始时间（ISO 格式）
        suppress_alert: 是否抑制告警（维护模式 = True）
    """
    device_id: int
    old_status: DeviceStatus
    new_status: DeviceStatus
    event_type: Optional[str] = None
    failure_count: int = 0
    recovery_count: int = 0
    downtime_start: Optional[str] = None
    suppress_alert: bool = False


class DeviceStateMachine:
    """
    设备状态机 —— 系统稳定性核心组件。

    设计目标：误报率 = 0，漏报率 = 0。
    宁可延迟判定（等待 N 次确认），绝不错误报警。

    单台设备一个实例，内部维护：
      - 当前状态
      - 连续失败计数（用于 N 次阈值判定）
      - 连续成功计数（用于 M 次恢复判定）
      - 掉线开始时间（恢复通知中计算离线时长）
      - 维护模式标志
    """

    def __init__(self, device: dict):
        """
        初始化状态机。

        参数:
            device: 设备字典
        """
        self._device_id = device.get('id', 0)
        self._device_name = device.get('name', 'unknown')

        # 从 DB 状态恢复
        db_status = device.get('status', 'unknown')
        try:
            self._status = DeviceStatus(db_status)
        except ValueError:
            self._status = DeviceStatus.UNKNOWN

        self._failure_count = device.get('failure_count', 0)
        self._recovery_count = device.get('recovery_count', 0)
        self._is_maintenance = bool(device.get('is_maintenance', 0))

        # 阈值配置：每设备可单独配置，未配置则使用全局默认
        self._failure_threshold = device.get('failure_threshold', 3)
        self._recovery_threshold = device.get('recovery_threshold', 2)

        # 掉线开始时间（从 DB 恢复）
        self._downtime_start = device.get('last_downtime_start', None)

        # 线程安全锁
        self._lock = threading.Lock()

    @property
    def status(self) -> DeviceStatus:
        """当前状态（线程安全读取）。"""
        return self._status

    @property
    def failure_count(self) -> int:
        return self._failure_count

    @property
    def device_id(self) -> int:
        return self._device_id

    def transition(self, outcome: ProbeOutcome) -> Optional[StateTransition]:
        """
        处理一次探测结果，执行状态转换。
        线程安全——内部使用锁保护状态变更。

        参数:
            outcome: DetectionChain 的探测判定结果

        返回:
            StateTransition（如果状态有变化），或 None（状态未变）
        """
        with self._lock:
            old_status = self._status
            is_online = outcome.is_online
            should_count = outcome.failure_should_count

            if is_online:
                # === 探测成功 ===

                if self._status == DeviceStatus.OFFLINE:
                    # 离线状态中——累计恢复计数
                    self._recovery_count += 1
                    self._failure_count = 0  # 有一次成功就清零失败计数
                    logger.info(
                        f"[{self._device_name}] 离线中，恢复计数 {self._recovery_count}/{self._recovery_threshold}"
                    )
                    if self._recovery_count >= self._recovery_threshold:
                        # 达到恢复阈值 → 转为在线
                        self._status = DeviceStatus.ONLINE
                        self._recovery_count = 0
                        dt_start = self._downtime_start
                        self._downtime_start = None
                        logger.info(f"[{self._device_name}] OFFLINE → ONLINE（设备恢复！）")
                        return self._make_transition(old_status, 'device_recovered', dt_start)
                    # 还未达到恢复阈值，状态不变
                    return None

                elif self._status == DeviceStatus.PENDING_FAILURE:
                    # 待定失败中——一次成功即可恢复为在线
                    self._status = DeviceStatus.ONLINE
                    self._failure_count = 0
                    logger.info(f"[{self._device_name}] PENDING_FAILURE → ONLINE（恢复）")
                    return self._make_transition(old_status, None)

                else:
                    # ONLINE 或 UNKNOWN 状态 —— 保持/转为在线
                    self._status = DeviceStatus.ONLINE
                    self._failure_count = 0
                    self._recovery_count = 0
                    if old_status != DeviceStatus.ONLINE:
                        logger.info(f"[{self._device_name}] {old_status.value} → ONLINE")
                        return self._make_transition(old_status, None)
                    return None

            else:
                # === 探测失败 ===

                if not should_count:
                    # TCP 复核通过——不计入失败，状态不变
                    return None

                if self._status == DeviceStatus.ONLINE:
                    # 在线状态中首次失败
                    self._failure_count = 1
                    if self._failure_threshold <= 1:
                        # N=1 —— 立即判定离线
                        self._status = DeviceStatus.OFFLINE
                        self._downtime_start = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                        logger.warning(f"[{self._device_name}] ONLINE → OFFLINE（立即告警，N=1）")
                        return self._make_transition(old_status, 'device_offline', self._downtime_start)
                    else:
                        # 进入待定失败状态（不告警！）
                        self._status = DeviceStatus.PENDING_FAILURE
                        logger.info(
                            f"[{self._device_name}] ONLINE → PENDING_FAILURE "
                            f"(失败 {self._failure_count}/{self._failure_threshold})"
                        )
                        return None  # PENDING 不产生事件

                elif self._status == DeviceStatus.PENDING_FAILURE:
                    self._failure_count += 1
                    if self._failure_count >= self._failure_threshold:
                        # 达到失败阈值 → 判定离线！
                        self._status = DeviceStatus.OFFLINE
                        self._downtime_start = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                        logger.warning(
                            f"[{self._device_name}] PENDING_FAILURE → OFFLINE "
                            f"(连续失败 {self._failure_count} 次，触发告警！)"
                        )
                        return self._make_transition(old_status, 'device_offline', self._downtime_start)
                    else:
                        logger.debug(
                            f"[{self._device_name}] 待定中: "
                            f"{self._failure_count}/{self._failure_threshold}"
                        )
                        return None

                elif self._status == DeviceStatus.OFFLINE:
                    # 已经是离线，重置恢复计数，保持离线
                    self._recovery_count = 0
                    self._failure_count += 1
                    return None

                elif self._status == DeviceStatus.UNKNOWN:
                    # 从未上线过的设备：失败计数必须累计，达到阈值后判离线并告警
                    # （修复前恒为 1，导致启动时已离线的设备永远不告警 = 漏报）
                    self._failure_count += 1
                    if self._failure_count >= self._failure_threshold:
                        self._status = DeviceStatus.OFFLINE
                        self._downtime_start = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                        return self._make_transition(old_status, 'device_offline', self._downtime_start)
                    return None

        return None

    def enter_maintenance(self):
        """进入维护模式——告警抑制开启。"""
        self._is_maintenance = True
        self._failure_count = 0
        self._recovery_count = 0
        logger.info(f"[{self._device_name}] 进入维护模式（告警抑制）")

    def exit_maintenance(self):
        """退出维护模式——恢复正常告警。"""
        self._is_maintenance = False
        logger.info(f"[{self._device_name}] 退出维护模式")

    def _make_transition(self, old_status: DeviceStatus,
                          event_type: Optional[str] = None,
                          downtime_start: Optional[str] = None) -> StateTransition:
        """构造状态转换结果。"""
        return StateTransition(
            device_id=self._device_id,
            old_status=old_status,
            new_status=self._status,
            event_type=event_type,
            failure_count=self._failure_count,
            recovery_count=self._recovery_count,
            downtime_start=downtime_start,
            suppress_alert=self._is_maintenance,
        )
