"""
模块：base_channel.py
功能：通知通道抽象基类 —— 所有通知通道必须实现此接口

作者：Claude
创建日期：2026-08-07
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


@dataclass
class AlertMessage:
    """
    告警消息数据结构。
    所有通知通道统一使用此结构生成通知内容。

    字段:
        event_type: 事件类型 —— 'offline'|'recovery'|'escalation'|'digest'|'test'
        device_name: 设备名称
        ip_address: 设备 IP
        subsystem: 所属子系统
        message: 消息正文
        occurred_at: 事件发生时间（ISO8601）
        extra: 附加信息（如 digest 包含的事件列表、离线时长等）
    """
    event_type: str
    device_name: str = ""
    ip_address: str = ""
    subsystem: str = ""
    message: str = ""
    occurred_at: str = ""
    extra: dict = None

    def __post_init__(self):
        if self.extra is None:
            self.extra = {}


@dataclass
class SendResult:
    """
    通知发送结果。

    字段:
        success: 是否发送成功
        error: 失败原因描述
        channel: 通道名称
        latency_ms: 发送耗时（毫秒）
    """
    success: bool
    error: str = ""
    channel: str = ""
    latency_ms: float = 0.0


class BaseNotificationChannel(ABC):
    """
    通知通道抽象基类。
    所有通知通道（邮件/飞书/企微）必须实现此接口：

      send(msg) → SendResult   —— 发送单条通知
      test() → SendResult       —— 测试通道连通性
      get_channel_name() → str  —— 返回通道名称
    """

    @abstractmethod
    def send(self, message: AlertMessage) -> SendResult:
        """
        发送通知消息。

        参数:
            message: AlertMessage 数据结构

        返回:
            SendResult —— 成功/失败 + 错误信息 + 耗时
        """
        ...

    @abstractmethod
    def test(self) -> SendResult:
        """
        测试通道连通性。
        发送一条测试消息到目标地址，验证配置正确。

        返回:
            SendResult —— 成功/失败
        """
        ...

    @abstractmethod
    def get_channel_name(self) -> str:
        """返回通道名称，如 'email'、'feishu'、'wecom'。"""
        ...


class ChannelFactory:
    """
    通道工厂 —— 根据配置创建所有已启用的通知通道实例。
    """

    @staticmethod
    def create_all(config: dict) -> list:
        """
        创建所有已启用通道的实例列表。

        参数:
            config: 全局配置字典

        返回:
            BaseNotificationChannel 实例列表
        """
        from .email_channel import EmailChannel
        from .feishu_channel import FeishuChannel
        from .wecom_channel import WeComChannel

        channels = []
        notify_cfg = config.get('notify', {})

        # 邮件通道
        email_cfg = notify_cfg.get('email', {})
        if email_cfg.get('enabled', False):
            channels.append(EmailChannel(email_cfg, notify_cfg))

        # 飞书通道
        feishu_cfg = notify_cfg.get('feishu', {})
        if feishu_cfg.get('enabled', False):
            channels.append(FeishuChannel(feishu_cfg))

        # 企业微信通道
        wecom_cfg = notify_cfg.get('wecom', {})
        if wecom_cfg.get('enabled', False):
            channels.append(WeComChannel(wecom_cfg))

        return channels
