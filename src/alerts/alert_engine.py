"""
模块：alert_engine.py
功能：告警规则引擎
     接收状态机产生的 StateTransition 事件，按规则决定是否触发通知。
     管理告警确认、冷却、升级状态，协调 DigestEngine 合并摘要。

处理流程：
  收到 device_offline 事件
    → 检查维护模式（是 → 跳过不发）
    → 检查冷却窗口（在冷却期 → 只记录，不发通知）
    → 放入 DigestEngine 缓冲区
    → DigestEngine 判断立即发送 or 等待合并
    → 发送时并行调用所有启用通道
    → 记录 alert_event（含 digest_id 绑定）

  收到 device_recovered 事件
    → 立即单独发送恢复通知（不合并）
    → 清除设备冷却状态

作者：Claude
创建日期：2026-08-07
"""
import time
import threading
import logging
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from .digest_engine import DigestEngine, DigestBatch
from ..notify.base_channel import (
    BaseNotificationChannel, AlertMessage, SendResult, ChannelFactory
)
from ..core.device_state_machine import StateTransition

logger = logging.getLogger("device-link.alerts.engine")


class AlertEngine:
    """
    告警规则引擎。

    职责：
      - 接收状态机产生的 StateTransition
      - 按规则决定是否触发通知、是否合并、是否升级
      - 管理告警确认、冷却状态
      - 协调 DigestEngine 实现智能摘要

    使用示例:
        engine = AlertEngine(config, alert_repo, channel_factory)
        scheduler.register_callback(engine.on_monitor_event)
    """

    def __init__(self, config: dict, alert_repo, channels: list = None):
        """
        初始化告警引擎。

        参数:
            config: 全局配置
            alert_repo: AlertRepository 实例
            channels: 通知通道列表（可选，不提供则从 config 创建）
        """
        self._config = config
        self._alert_repo = alert_repo
        self._notify_cfg = config.get('notify', {})

        # 创建通知通道
        if channels:
            self._channels = channels
        else:
            self._channels = ChannelFactory.create_all(config)

        # 摘要引擎
        self._digest_engine = DigestEngine(config)
        self._digest_enabled = self._notify_cfg.get('digest', {}).get('enabled', False)

        # 冷却状态：device_id → last_alert_timestamp
        self._cooldown: dict[int, float] = {}
        self._cooldown_seconds = self._notify_cfg.get('cooldown_seconds', 1800)

        # 升级状态：event_id → {'escalated_at': ..., 'count': ...}
        self._escalation_state: dict[int, dict] = {}
        self._escalation_minutes = self._notify_cfg.get('escalation_minutes', 15)

        # 重试配置
        self._retry_count = self._notify_cfg.get('retry_count', 3)
        self._retry_backoff = self._notify_cfg.get('retry_backoff_base_seconds', 5)

        # 线程安全锁
        self._lock = threading.Lock()

        # 升级检查线程
        self._escalation_thread: Optional[threading.Thread] = None
        # 摘要泵线程（修复 Bug H：DigestEngine 窗口定时器从未启动，
        # 导致摘要通知永不发送；用 2 秒泵轮询窗口/容量条件）
        self._digest_pump_thread: Optional[threading.Thread] = None
        self._running = True

        logger.info(
            f"告警引擎初始化：{len(self._channels)} 个通知通道, "
            f"冷却={self._cooldown_seconds}s, 升级={self._escalation_minutes}min"
        )

    def on_monitor_event(self, transition: StateTransition):
        """
        接收状态机产生的事件，触发相应的告警处理。

        参数:
            transition: 状态转换结果
        """
        if transition.event_type == 'device_offline':
            self._handle_offline(transition)
        elif transition.event_type == 'device_recovered':
            self._handle_recovery(transition)

    def _handle_offline(self, transition: StateTransition):
        """
        处理设备离线事件。

        流程：
          1. 维护模式检查
          2. 冷却窗口检查
          3. 记录 alert_event
          4. 放入 DigestEngine
          5. 检查是否需要立即发送
        """
        did = transition.device_id

        # 1. 维护模式跳过
        if transition.suppress_alert:
            logger.info(f"设备 {did} 处于维护模式，跳过离线告警")
            return

        # 2. 冷却窗口检查
        with self._lock:
            now = time.time()
            last = self._cooldown.get(did, 0)
            if now - last < self._cooldown_seconds:
                logger.debug(f"设备 {did} 在冷却期，跳过重复告警")
                return
            self._cooldown[did] = now

        # 3. 记录事件
        event = {
            'device_id': did,
            'event_type': 'offline',
            'message': f"设备离线告警",
            'notified_channels': '',
            'notify_success': 0,
        }
        event_id = self._alert_repo.insert_event(event)
        logger.info(f"离线告警已记录: event_id={event_id}")

        # 4. 摘要开启 → 缓冲合并；摘要关闭 → 立即发送（Bug J 修复）
        if self._digest_enabled:
            digest_id = self._digest_engine.add_event({
                'event_id': event_id,
                'device_id': did,
                'event_type': 'offline',
                'occurred_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'downtime_start': transition.downtime_start,
            })

            # 5. 紧急条件触发 → 立即发送
            if digest_id is not None:
                batch = self._digest_engine.flush()
                if batch:
                    self._send_digest(batch)
        else:
            self._send_immediate(AlertMessage(
                event_type='offline',
                device_name=str(did),
                occurred_at=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            ), event_id)

    def _handle_recovery(self, transition: StateTransition):
        """
        处理设备恢复事件。
        恢复通知单独发送，不参与合并（实时性要求高）。
        """
        did = transition.device_id

        # 清除冷却状态
        with self._lock:
            self._cooldown.pop(did, None)

        # 计算离线时长
        downtime_duration = '未知'
        if transition.downtime_start:
            try:
                start = datetime.fromisoformat(transition.downtime_start)
                delta = datetime.now() - start
                hours = delta.seconds // 3600
                minutes = (delta.seconds % 3600) // 60
                downtime_duration = f"{hours}小时{minutes}分钟"
            except Exception:
                pass

        # 记录事件
        event = {
            'device_id': did,
            'event_type': 'recovery',
            'message': f"设备已恢复在线",
            'notified_channels': '',
            'notify_success': 0,
        }
        event_id = self._alert_repo.insert_event(event)
        logger.info(f"恢复事件已记录: event_id={event_id}")

        # 立即发送恢复通知
        self._send_immediate(AlertMessage(
            event_type='recovery',
            device_name=str(did),
            occurred_at=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            extra={'downtime_duration': downtime_duration}
        ), event_id)

    def acknowledge(self, event_id: int, user: str = "admin") -> bool:
        """
        确认告警事件。确认后升级检查停止。

        返回:
            True 表示确认成功
        """
        with self._lock:
            self._escalation_state.pop(event_id, None)
        return self._alert_repo.acknowledge(event_id, user)

    def get_active_alerts(self) -> list:
        """返回所有未确认的告警事件。"""
        return self._alert_repo.get_unacknowledged_offline_events()

    def reload_channels(self, config: dict):
        """按最新配置重建通知通道（GUI 保存配置后调用）。"""
        from ..notify.base_channel import ChannelFactory
        self._channels = ChannelFactory.create_all(config)
        logger.info(f"通知通道已重载：{len(self._channels)} 个")
        return len(self._channels)

    def run_escalation_loop(self):
        """
        启动升级检查后台线程（每 60 秒扫描一次）。
        未确认的离线告警超过 escalation_minutes → 自动升级通知。
        """
        if self._escalation_thread and self._escalation_thread.is_alive():
            return

        def _loop():
            while True:
                time.sleep(60)
                try:
                    unacked = self._alert_repo.get_unacknowledged_offline_events()
                    for evt in unacked:
                        eid = evt['id']
                        created_str = evt.get('created_at', '')
                        if not created_str:
                            continue
                        try:
                            created = datetime.fromisoformat(created_str)
                            elapsed = (datetime.now() - created).total_seconds() / 60.0
                        except Exception:
                            continue

                        if elapsed >= self._escalation_minutes:
                            with self._lock:
                                state = self._escalation_state.get(eid, {
                                    'escalated_at': 0, 'count': 0
                                })
                                # 30 分钟内最多升级 1 次
                                if time.time() - state.get('escalated_at', 0) < 1800:
                                    continue
                                state['escalated_at'] = time.time()
                                state['count'] = state.get('count', 0) + 1
                                self._escalation_state[eid] = state

                            msg = AlertMessage(
                                event_type='escalation',
                                device_name=str(evt.get('device_id', '')),
                                occurred_at=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                                extra={'escalation_minutes': int(elapsed)}
                            )
                            self._send_immediate(msg, eid)
                            logger.warning(
                                f"告警升级: event_id={eid}, 已持续 {elapsed:.0f} 分钟"
                            )
                except Exception as e:
                    logger.error(f"升级检查异常: {e}")

        self._escalation_thread = threading.Thread(
            target=_loop, daemon=True, name="escalation-loop"
        )
        self._escalation_thread.start()
        self._digest_pump_thread = threading.Thread(
            target=self._digest_pump, daemon=True, name="digest-pump"
        )
        self._digest_pump_thread.start()
        logger.info("告警升级检查已启动（每 60 秒扫描）")

    def _digest_pump(self):
        """摘要泵：每 2 秒检查窗口到期/容量上限，触发摘要发送。"""
        while self._running:
            try:
                if self._digest_engine.should_flush():
                    batch = self._digest_engine.flush()
                    if batch:
                        self._send_digest(batch)
            except Exception:
                logger.exception("摘要泵异常（已恢复）")
            time.sleep(2)

    # ==================== 内部方法 ====================

    def _send_immediate(self, message: AlertMessage, event_id: int):
        """立即发送单条通知（不经过摘要合并）。"""
        success_count = 0
        channel_names = []
        for ch in self._channels:
            result = self._send_with_retry(ch, message)
            if result.success:
                success_count += 1
                channel_names.append(ch.get_channel_name())

        notify_success = 2 if success_count == len(self._channels) else (1 if success_count > 0 else 0)
        self._alert_repo.update_notify_result(
            event_id, notify_success, ','.join(channel_names)
        ) if message.event_type != 'recovery' else None
        self._alert_repo.acknowledge(event_id) if message.event_type == 'recovery' else None

    def _send_digest(self, batch: DigestBatch):
        """
        发送摘要批次。
        生成汇总邮件/消息，并行调用所有通道。
        """
        events = batch.events
        event_ids = [e.get('event_id') for e in events if e.get('event_id')]

        msg = AlertMessage(
            event_type='digest',
            device_name=f"{len(events)} 台设备离线",
            message=f"时间窗口内 {len(events)} 台设备离线，涉及多个子系统",
            occurred_at=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            extra={'events': events, 'digest_id': batch.digest_id}
        )

        success_count = 0
        channel_names = []
        for ch in self._channels:
            result = self._send_with_retry(ch, msg)
            if result.success:
                success_count += 1
                channel_names.append(ch.get_channel_name())

        # 更新事件的通知状态
        notify_success = 2 if success_count == len(self._channels) else (1 if success_count > 0 else 0)
        for eid in event_ids:
            self._alert_repo.update_notify_result(eid, notify_success, ','.join(channel_names))
        logger.info(
            f"摘要已发送: digest={batch.digest_id}, "
            f"{len(events)} 条事件, {success_count}/{len(self._channels)} 通道成功"
        )

    def _send_with_retry(self, channel: BaseNotificationChannel,
                          message: AlertMessage) -> SendResult:
        """
        发送通知并带重试（指数退避）。
        至少发送 1 次，最多重试 retry_count 次。
        """
        last_result = None
        for attempt in range(max(1, self._retry_count)):
            result = channel.send(message)
            if result.success:
                return result
            last_result = result
            if attempt < self._retry_count - 1:
                wait = self._retry_backoff * (2 ** attempt)
                logger.warning(
                    f"{channel.get_channel_name()} 发送失败 (尝试 {attempt+1}/{self._retry_count}), "
                    f"{wait}s 后重试..."
                )
                time.sleep(wait)
        return last_result or SendResult(success=False, error='全部重试失败')
