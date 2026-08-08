"""
模块：wecom_channel.py
功能：企业微信群机器人 Webhook 通知通道
     使用 markdown 消息类型发送通知，支持 <font color> 着色。
     内置限速保护：20 条/分钟，超出自动返回 rate_limited。

API 文档：https://developer.work.weixin.qq.com/document/path/91770

作者：Claude
创建日期：2026-08-07
"""
import time
import json
import logging

import requests

from .base_channel import BaseNotificationChannel, AlertMessage, SendResult

logger = logging.getLogger("device-link.notify.wecom")


class WeComChannel(BaseNotificationChannel):
    """
    企业微信群机器人 Webhook 通知通道。
    使用 markdown 格式发送，消息 > 4096 字节自动截断。
    内置发送计数，超过 20 条/分钟自动返回 rate_limited。
    """

    # 企微 markdown 颜色映射
    COLOR_MAP = {
        'warning': 'warning',   # 橙红色
        'info': 'info',         # 绿色
        'comment': 'comment',   # 灰色
    }

    def __init__(self, channel_config: dict):
        """
        初始化企业微信通道。

        参数:
            channel_config: 含 webhook_url 字段
        """
        self._webhook_url = channel_config.get('webhook_url', '')
        self._session = requests.Session()
        # 限速：20 条/分钟
        self._send_timestamps: list[float] = []
        self._rate_limit = 20
        self._rate_window = 60  # 秒

    def get_channel_name(self) -> str:
        return "wecom"

    def test(self) -> SendResult:
        """发送测试消息验证 Webhook Key 有效性。"""
        return self._send_markdown(
            "DEVICE LINK 企业微信通知通道测试通过 ✅\n> 配置正确，可以正常接收通知。"
        )

    def send(self, message: AlertMessage) -> SendResult:
        """
        发送企微 markdown 通知。

        颜色约定：
          offline → <font color="warning">
          recovery → <font color="info">
          escalation → <font color="warning">
          digest → <font color="info">
          test → <font color="comment">
        """
        t0 = time.monotonic()

        # 限速检查
        if not self._check_rate():
            return SendResult(success=False, error='rate_limited', channel='wecom')

        try:
            colors = {
                'offline': 'warning',
                'recovery': 'info',
                'escalation': 'warning',
                'digest': 'info',
                'test': 'comment',
            }
            c = colors.get(message.event_type, 'comment')

            content = (
                f"## <font color='{c}'>{self._event_title(message.event_type)}</font>\n"
                f"> 设备：<font color='{c}'>{message.device_name}</font>\n"
                f"> IP：{message.ip_address}\n"
                f"> 子系统：{message.subsystem}\n"
                f"> 时间：{message.occurred_at}\n"
                f"> {message.message}\n"
            )

            # 附加信息
            for k, v in message.extra.items():
                if isinstance(v, str):
                    content += f"> {k}：{v}\n"

            return self._send_markdown(content)

        except Exception as e:
            logger.error(f"企微发送异常: {e}", exc_info=True)
            return SendResult(success=False, error=str(e), channel='wecom')

    def _event_title(self, event_type: str) -> str:
        """事件类型对应的中文标题。"""
        return {
            'offline': '设备离线告警',
            'recovery': '设备恢复通知',
            'escalation': '告警升级通知',
            'digest': '告警合并摘要',
            'test': '测试消息',
        }.get(event_type, '通知')

    def _send_markdown(self, content: str) -> SendResult:
        """
        发送 markdown 消息，超过 4096 字节自动截断。
        """
        t0 = time.monotonic()
        try:
            # 截断检查
            content_bytes = content.encode('utf-8')
            if len(content_bytes) > 4096:
                content = content_bytes[:4080].decode('utf-8', errors='replace') + '\n> [内容已截断]'

            payload = {
                "msgtype": "markdown",
                "markdown": {"content": content}
            }

            resp = self._session.post(
                self._webhook_url, json=payload,
                timeout=10,
                headers={'Content-Type': 'application/json'}
            )
            elapsed = (time.monotonic() - t0) * 1000.0

            if resp.status_code == 200:
                body = resp.json()
                errcode = body.get('errcode', -1)
                if errcode == 0:
                    logger.info(f"企微发送成功, {elapsed:.0f}ms")
                    return SendResult(success=True, channel='wecom', latency_ms=elapsed)
                else:
                    # 解析已知错误码
                    err_msgs = {
                        93000: '群聊不存在或机器人已被移除',
                        45009: 'API 调用频率超限（20条/分钟）',
                        93004: '机器人被禁用',
                    }
                    err_msg = err_msgs.get(errcode, f'企微错误码 {errcode}: {body.get("errmsg", "")}')
                    logger.error(err_msg)
                    return SendResult(success=False, error=err_msg, channel='wecom')
            else:
                err_msg = f"HTTP {resp.status_code}"
                logger.error(f"企微请求失败: {err_msg}")
                return SendResult(success=False, error=err_msg, channel='wecom')

        except requests.Timeout:
            return SendResult(success=False, error='企微请求超时', channel='wecom')
        except Exception as e:
            return SendResult(success=False, error=str(e), channel='wecom')

    def _check_rate(self) -> bool:
        """
        检查发送速率是否超限（20 条/60 秒）。
        超过则返回 False，调用方应稍后重试。
        """
        now = time.time()
        # 清理过期的时间戳
        self._send_timestamps = [
            ts for ts in self._send_timestamps
            if now - ts < self._rate_window
        ]
        if len(self._send_timestamps) >= self._rate_limit:
            logger.warning(f"企微发送速率超限: {len(self._send_timestamps)}/{self._rate_limit} per min")
            return False
        self._send_timestamps.append(now)
        return True
