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
import os
import sys
import json
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

    def __init__(self, config: dict, alert_repo, channels: list = None,
                 device_repo=None, daily_state_file: str = None):
        """
        初始化告警引擎。

        参数:
            config: 全局配置
            alert_repo: AlertRepository 实例
            channels: 通知通道列表（可选，不提供则从 config 创建）
        """
        self._config = config
        self._alert_repo = alert_repo
        self._device_repo = device_repo
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
        # 升级开关与每事件升级次数上限（修复：升级邮件无限重复轰炸）
        self._escalation_enabled = self._notify_cfg.get('escalation_enabled', True)
        self._escalation_max_count = self._notify_cfg.get('escalation_max_count', 3)

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
        # 每日离线报告（v1.0.9）：每天 send_time 发送当前离线设备清单
        self._daily_report_thread: Optional[threading.Thread] = None
        self._daily_state_file = daily_state_file
        self._daily_report_last_sent = self._load_daily_state()
        self._running = True

        logger.info(
            f"告警引擎初始化：{len(self._channels)} 个通知通道, "
            f"冷却={self._cooldown_seconds}s, "
            f"升级={'开' if self._escalation_enabled else '关'}"
            f"({self._escalation_minutes}min, 每事件最多{self._escalation_max_count}次)"
        )
        if not self._channels:
            logger.warning(
                "告警引擎当前无启用通知通道，真实告警不会发送到任何渠道。"
                "请在『告警与通知配置』勾选启用通道并点击『保存配置』。"
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

        # 设备信息：单设备告警与摘要清单都必须带上名称/IP/系统名
        dev = self._get_device_info(did)

        # 4. 摘要开启 → 缓冲合并；摘要关闭 → 立即发送（Bug J 修复）
        if self._digest_enabled:
            digest_id = self._digest_engine.add_event({
                'event_id': event_id,
                'device_id': did,
                'device_name': dev.get('name', ''),
                'ip_address': dev.get('ip_address', ''),
                'subsystem': dev.get('subsystem_name', ''),
                'subsystem_name': dev.get('subsystem_name', ''),
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
                device_name=dev.get('name') or str(did),
                ip_address=dev.get('ip_address', ''),
                subsystem=dev.get('subsystem_name', ''),
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

        dev = self._get_device_info(did)

        # 立即发送恢复通知
        self._send_immediate(AlertMessage(
            event_type='recovery',
            device_name=dev.get('name') or str(did),
            ip_address=dev.get('ip_address', ''),
            subsystem=dev.get('subsystem_name', ''),
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
        """
        按最新配置重建通知通道并刷新告警规则（GUI 保存配置后调用）。
        合并策略/冷却/升级/重试参数一并热更新，无需重启程序。
        """
        from ..notify.base_channel import ChannelFactory
        self._config = config
        self._notify_cfg = config.get('notify', {})
        self._channels = ChannelFactory.create_all(config)
        self._digest_engine = DigestEngine(config)
        self._digest_enabled = self._notify_cfg.get('digest', {}).get('enabled', False)
        self._cooldown_seconds = self._notify_cfg.get('cooldown_seconds', 1800)
        self._escalation_minutes = self._notify_cfg.get('escalation_minutes', 15)
        self._escalation_enabled = self._notify_cfg.get('escalation_enabled', True)
        self._escalation_max_count = self._notify_cfg.get('escalation_max_count', 3)
        self._retry_count = self._notify_cfg.get('retry_count', 3)
        self._retry_backoff = self._notify_cfg.get('retry_backoff_base_seconds', 5)
        logger.info(
            f"通知通道已重载：{len(self._channels)} 个，"
            f"摘要={'开' if self._digest_enabled else '关'} "
            f"(窗口={self._digest_engine._window_seconds}s, "
            f"上限={self._digest_engine._max_events_per_digest}), "
            f"冷却={self._cooldown_seconds}s, "
            f"升级={'开' if self._escalation_enabled else '关'}"
            f"({self._escalation_minutes}min, 每事件最多{self._escalation_max_count}次)"
        )
        return len(self._channels)

    def run_escalation_loop(self):
        """
        启动升级检查后台线程（每 60 秒扫描一次）。
        未确认的离线告警超过 escalation_minutes → 自动升级通知。
        """
        if self._escalation_thread and self._escalation_thread.is_alive():
            return

        def _loop():
            while self._running:
                time.sleep(60)
                try:
                    # 升级总开关：关闭后不再扫描发送（仍保留线程，避免重启）
                    if not self._escalation_enabled:
                        continue
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
                                # 每事件升级次数上限：达到后不再发送，避免无限轰炸
                                if state.get('count', 0) >= self._escalation_max_count:
                                    continue
                                state['escalated_at'] = time.time()
                                state['count'] = state.get('count', 0) + 1
                                self._escalation_state[eid] = state

                            dev = self._get_device_info(evt.get('device_id', ''))
                            msg = AlertMessage(
                                event_type='escalation',
                                device_name=dev.get('name') or str(evt.get('device_id', '')),
                                ip_address=dev.get('ip_address', ''),
                                subsystem=dev.get('subsystem_name', ''),
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
        self._daily_report_thread = threading.Thread(
            target=self._daily_report_loop, daemon=True, name="daily-report"
        )
        self._daily_report_thread.start()
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

    # ==================== 每日离线报告 ====================

    def _daily_report_loop(self):
        """每日报告循环：每 30 秒检查是否到达发送时间（当天只发一次）。"""
        while self._running:
            try:
                self._maybe_send_daily_report()
            except Exception:
                logger.exception("每日离线报告异常（已恢复）")
            time.sleep(30)

    def _maybe_send_daily_report(self):
        """
        到点发送每日离线设备清单。
        规则：
          - 未启用 → 不发送
          - 当前时间未到 send_time → 等待
          - 当天已发送过（状态文件记录）→ 跳过（防重复）
          - 当前离线设备 0 台 → 不发送邮件，仅记录日志
          - 只统计 devices.status='offline'（pending_failure/维护中不计入）
        """
        notify_cfg = self._config.get('notify', {})
        daily_cfg = notify_cfg.get('daily_report', {})
        if not daily_cfg.get('enabled', False):
            return
        send_time = str(daily_cfg.get('send_time', '08:00')).strip()
        now = datetime.now()
        if now.strftime('%H:%M') < send_time:
            return
        today = now.strftime('%Y-%m-%d')
        if self._daily_report_last_sent == today:
            return
        if self._device_repo is None:
            logger.warning("每日离线报告：未注入 device_repo，无法统计离线设备，跳过")
            return

        devices = self._device_repo.list_current_offline_devices()
        if not devices:
            self._mark_daily_sent(today)
            logger.info(
                f"每日离线报告：{today} 当前离线设备 0 台，不发送邮件"
            )
            return

        dev_lines = []
        events = []
        for d in devices:
            name = d.get('name') or f"#{d.get('id', '')}"
            ip = d.get('ip_address') or '-'
            subsys = d.get('subsystem_name') or '未分组'
            last_check = d.get('last_check_time') or '-'
            downtime = self._format_downtime(d.get('last_downtime_start'))
            dev_lines.append(
                f"- {name} ({ip}) [{subsys}] 最近探测: {last_check}"
                + (f"，离线时长: {downtime}" if downtime else "")
            )
            events.append({
                'device_name': name,
                'ip_address': ip,
                'subsystem': subsys,
                'last_check_time': last_check,
                'downtime': downtime,
            })

        msg = AlertMessage(
            event_type='daily_report',
            device_name=f"{len(devices)} 台设备离线",
            message=(
                f"截至 {now.strftime('%Y-%m-%d %H:%M:%S')}，当前 "
                f"{len(devices)} 台设备离线（待定/维护中不计入）。"
            ),
            occurred_at=now.strftime('%Y-%m-%d %H:%M:%S'),
            extra={
                'events': events,
                '离线设备清单': '\n'.join(dev_lines),
            },
        )

        success_count = 0
        channel_names = []
        for ch in self._channels:
            result = self._send_with_retry(ch, msg)
            if result.success:
                success_count += 1
                channel_names.append(ch.get_channel_name())
        self._mark_daily_sent(today)
        logger.info(
            f"每日离线报告已发送: {today}, {len(devices)} 台设备离线, "
            f"{success_count}/{len(self._channels)} 通道成功"
        )

    def _format_downtime(self, downtime_start) -> str:
        """将离线开始时间格式化为持续时长；无法解析时返回空串。"""
        if not downtime_start:
            return ''
        try:
            start = datetime.fromisoformat(str(downtime_start))
            delta = datetime.now() - start
            if delta.total_seconds() < 0:
                return ''
            hours = int(delta.total_seconds() // 3600)
            minutes = int((delta.total_seconds() % 3600) // 60)
            if hours > 0:
                return f"{hours}小时{minutes}分钟"
            return f"{minutes}分钟"
        except Exception:
            return ''

    def _mark_daily_sent(self, today: str):
        """记录当天已发送（内存 + 状态文件）。"""
        self._daily_report_last_sent = today
        self._save_daily_state(today)

    def _daily_state_path(self) -> str:
        """每日报告状态文件路径（记录最近发送日期，防止重复发送）。"""
        if self._daily_state_file:
            return self._daily_state_file
        if getattr(sys, 'frozen', False):
            root = os.path.dirname(sys.executable)
        else:
            root = os.path.dirname(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            )
        return os.path.join(root, 'logs', 'daily_report_state.json')

    def _load_daily_state(self) -> str:
        """读取最近一次发送日期（无记录返回空串）。"""
        try:
            with open(self._daily_state_path(), 'r', encoding='utf-8') as f:
                data = json.load(f)
            return str(data.get('last_sent_date', ''))
        except Exception:
            return ''

    def _save_daily_state(self, today: str):
        """原子写每日报告状态。"""
        path = self._daily_state_path()
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump({'last_sent_date': today}, f, ensure_ascii=False)
            os.replace(tmp, path)
        except Exception as e:
            logger.error(f"写入每日报告状态失败: {e}")

    # ==================== 内部方法 ====================

    def _get_device_info(self, device_id) -> dict:
        """
        查询设备名称/IP/系统名。
        未注入 device_repo 或查询失败时降级为 ID 占位，保证不阻塞告警流程。
        """
        if self._device_repo is not None:
            try:
                dev = self._device_repo.get_device(device_id)
                if dev:
                    return dev
            except Exception as e:
                logger.warning(f"查询设备信息失败 (id={device_id}): {e}")
        return {'name': str(device_id), 'ip_address': '', 'subsystem_name': ''}

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

        # 生成纯文本清单（飞书/企微只渲染字符串字段，邮件用结构化 events）
        dev_lines = []
        for e in events:
            name = e.get('device_name') or f"#{e.get('device_id', '')}"
            ip = e.get('ip_address') or '-'
            subsys = e.get('subsystem') or e.get('subsystem_name') or '未分组'
            occurred = e.get('occurred_at', '')
            dev_lines.append(f"- {name} ({ip}) [{subsys}] {occurred}".rstrip())

        subsys_set = {
            e.get('subsystem') or e.get('subsystem_name') or '未分组'
            for e in events
        }
        msg = AlertMessage(
            event_type='digest',
            device_name=f"{len(events)} 台设备离线",
            message=(
                f"时间窗口内 {len(events)} 台设备离线，涉及 "
                f"{len(subsys_set)} 个子系统"
            ),
            occurred_at=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            extra={
                'events': events,
                'digest_id': batch.digest_id,
                '离线设备清单': '\n'.join(dev_lines),
            }
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
