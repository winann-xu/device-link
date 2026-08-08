"""
模块：digest_engine.py
功能：告警合并/摘要引擎
     将时间窗口内的多条离线告警合并为单封摘要邮件发送，
     避免告警风暴淹没管理员。同时支持紧急绕过和容量上限。

合并规则：
  规则 1（时间窗口）：在 window_seconds 内的多条离线告警合并为一封摘要
  规则 2（紧急绕过）：单个子系统 ≥ 5 台设备同时离线 → 忽略窗口，立即发送
  规则 3（容量上限）：单封摘要最多 max_events_per_digest 条，超出进入下一窗口
  规则 4（按子系统分组）：摘要内容按子系统分组，便于定位故障域
  规则 5（恢复不合并）：恢复通知始终单独发送，不参与合并
  规则 6（升级不合并）：升级通知单独发送，不参与合并

作者：Claude
创建日期：2026-08-07
"""
import time
import uuid
import threading
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

logger = logging.getLogger("device-link.alerts.digest")


@dataclass
class DigestBatch:
    """
    一个摘要批次。

    字段:
        digest_id: 批次唯一 ID
        events: 告警事件列表
        window_start: 窗口开始时间
        window_end: 窗口结束时间
        is_urgent: 是否紧急（紧急绕过容量和时间窗口限制）
    """
    digest_id: str
    events: list = field(default_factory=list)
    window_start: float = 0.0
    window_end: float = 0.0
    is_urgent: bool = False


class DigestEngine:
    """
    告警合并/摘要引擎。

    工作流程：
      1. AlertEngine 每次产生 offline 事件 → add_event() 放入缓冲区
      2. 第一个事件到达 → 启动窗口倒计时
      3. 窗口内后续事件 → 追加到同一摘要批次
      4. 紧急条件触发（同子系统 ≥ 5 台）→ 立即发送当前批次
      5. 窗口到期 → 生成摘要，返回 DigestBatch
      6. 容量检查 → 超出部分进入下一窗口
    """

    def __init__(self, config: dict):
        """
        初始化摘要引擎。

        参数:
            config: 全局通知配置字典
        """
        digest_cfg = config.get('notify', {}).get('digest', {})
        self._window_seconds = digest_cfg.get('window_seconds', 300)  # 默认 5 分钟
        self._max_events_per_digest = digest_cfg.get('max_events_per_digest', 50)
        self._send_immediate_if_critical = digest_cfg.get('send_immediate_if_critical', True)
        self._critical_threshold = 5  # 同子系统同时离线 ≥ 5 台

        # 当前批次的缓冲区
        self._buffer: list[dict] = []
        self._digest_id: Optional[str] = None
        self._window_start = 0.0
        self._timer: Optional[threading.Timer] = None

        # 线程安全锁
        self._lock = threading.Lock()

        # 容量上限
        self._max_buffer = 200

    def add_event(self, event: dict) -> Optional[str]:
        """
        将一条离线告警事件放入摘要缓冲区。

        参数:
            event: 告警事件字典（含 device_id, subsystem, device_name, ip_address 等）

        返回:
            如果是紧急触发 → 返回 digest_id；否则返回 None
        """
        with self._lock:
            # 容量保护
            if len(self._buffer) >= self._max_buffer:
                logger.warning(f"摘要缓冲区已满（{self._max_buffer}），丢弃最早事件")
                self._buffer.pop(0)

            now = time.time()

            # 如果是缓冲区第一个事件，启动窗口
            if self._digest_id is None:
                self._digest_id = str(uuid.uuid4())[:8]
                self._window_start = now

            self._buffer.append(event)
            logger.debug(
                f"摘要缓冲区: {len(self._buffer)} 条 (digest={self._digest_id})"
            )

            # 检查紧急绕过条件
            if self._send_immediate_if_critical and self._check_critical():
                logger.warning(f"紧急绕过触发！同子系统 ≥ {self._critical_threshold} 台离线")
                return self._digest_id

            return None

    def should_flush(self) -> bool:
        """
        检查是否应该发送当前批次。
        条件：窗口到期 OR 容量已满。
        """
        with self._lock:
            if not self._buffer:
                return False
            if len(self._buffer) >= self._max_events_per_digest:
                return True
            if self._window_start > 0:
                elapsed = time.time() - self._window_start
                return elapsed >= self._window_seconds
            return False

    def flush(self) -> Optional[DigestBatch]:
        """
        取出当前批次的所有事件，清空缓冲区，返回 DigestBatch。

        返回:
            DigestBatch（如果有事件），或 None（缓冲区为空）
        """
        with self._lock:
            if not self._buffer:
                return None

            # 容量上限：单封摘要最多 max_events_per_digest 条，超出部分留在缓冲区进入下一窗口
            batch_events = list(self._buffer)
            batch_digest_id = self._digest_id or str(uuid.uuid4())[:8]
            overflow = []
            if len(batch_events) > self._max_events_per_digest:
                overflow = batch_events[self._max_events_per_digest:]
                batch_events = batch_events[:self._max_events_per_digest]
                self._buffer = list(overflow)
                self._digest_id = str(uuid.uuid4())[:8]
                self._window_start = time.time()
            else:
                self._buffer.clear()
                self._digest_id = None
                self._window_start = 0.0

            batch = DigestBatch(
                digest_id=batch_digest_id,
                events=batch_events,
                window_start=self._window_start,
                window_end=time.time(),
            )

            logger.info(
                f"摘要批次发送: {len(batch.events)} 条事件 (digest={batch.digest_id}, "
                f"溢出 {len(overflow)} 条进入下一窗口)"
            )
            return batch

    def get_pending_count(self) -> int:
        """返回缓冲区中待发送的事件数。"""
        return len(self._buffer)

    def _check_critical(self) -> bool:
        """
        检查紧急绕过条件：单个子系统 ≥ threshold 台设备同时离线。
        """
        if len(self._buffer) < self._critical_threshold:
            return False
        # 统计各子系统离线设备数
        from collections import Counter
        subsys_count = Counter()
        for e in self._buffer:
            subsys = e.get('subsystem_name', e.get('subsystem', ''))
            if subsys:
                subsys_count[subsys] += 1
        # 任何子系统 ≥ 阈值 → 紧急
        return any(c >= self._critical_threshold for c in subsys_count.values())
