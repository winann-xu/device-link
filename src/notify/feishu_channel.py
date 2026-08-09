"""
模块：feishu_channel.py
功能：飞书机器人 Webhook 通知通道
     发送 interactive 卡片消息到飞书群。
     支持离线告警(红)、恢复通知(绿)、升级通知(橙)、摘要(蓝)四种卡片模板。

卡片结构：飞书卡片 JSON 2.0（schema="2.0"，正文放在 body.elements），
         与自定义机器人官方文档示例一致。

API 文档：https://open.feishu.cn/document/ukTMukTMukTM/ucTM5YjL3ETO24yNxkjN

作者：Claude
创建日期：2026-08-07
"""
import time
import logging

import requests

from .base_channel import BaseNotificationChannel, AlertMessage, SendResult

logger = logging.getLogger("device-link.notify.feishu")


class FeishuChannel(BaseNotificationChannel):
    """
    飞书群机器人 Webhook 通知通道。
    使用 interactive 卡片模板发送美观的通知消息。
    """

    def __init__(self, channel_config: dict):
        """
        初始化飞书通道。

        参数:
            channel_config: 含 webhook_url 字段
        """
        self._webhook_url = channel_config.get('webhook_url', '')
        self._session = requests.Session()

    def get_channel_name(self) -> str:
        return "feishu"

    def test(self) -> SendResult:
        """发送测试消息验证 Webhook URL 有效性。"""
        msg = AlertMessage(
            event_type='test',
            device_name='测试设备',
            ip_address='127.0.0.1',
            message='DEVICE LINK 飞书通知通道测试',
            occurred_at=time.strftime('%Y-%m-%d %H:%M:%S'),
        )
        # 测试必须走真实告警同款卡片路径，否则纯文本通过会掩盖卡片格式问题
        return self.send(msg)

    def send(self, message: AlertMessage) -> SendResult:
        """
        发送飞书卡片通知。

        卡片颜色映射：
          offline → red
          recovery → green
          escalation → orange
          digest → blue
        卡片使用 JSON 2.0 结构：schema="2.0" + body.elements，
        内容块为 markdown（支持换行/加粗/彩色文本），分隔线用 hr。
        """
        t0 = time.monotonic()
        try:
            colors = {
                'offline': 'red',
                'recovery': 'green',
                'escalation': 'orange',
                'digest': 'blue',
                'test': 'blue',
            }
            color = colors.get(message.event_type, 'blue')
            titles = {
                'offline': '设备离线告警',
                'recovery': '设备恢复通知',
                'escalation': '告警升级通知',
                'digest': '告警合并摘要',
                'test': '测试消息',
            }
            title = titles.get(message.event_type, '通知')

            # 构建 extra 内容（JSON 2.0 富文本 markdown 块）
            extra_fields = [
                {
                    "tag": "markdown",
                    "content": f"**{k}**\n{v}",
                }
                for k, v in message.extra.items()
                if isinstance(v, str)
            ]

            payload = {
                "msg_type": "interactive",
                "card": {
                    "schema": "2.0",
                    "config": {
                        "update_multi": True,
                        "enable_forward": False,
                    },
                    "header": {
                        "title": {"tag": "plain_text", "content": title},
                        "template": color
                    },
                    "body": {
                        "direction": "vertical",
                        "elements": [
                            {
                                "tag": "markdown",
                                "content": (
                                    f"**设备：**{message.device_name}\n"
                                    f"**IP：**{message.ip_address}\n"
                                    f"**子系统：**{message.subsystem}\n"
                                    f"**时间：**{message.occurred_at}\n"
                                    f"**详情：**{message.message}"
                                ),
                            },
                            *extra_fields,
                            {
                                "tag": "hr",
                            },
                            {
                                "tag": "markdown",
                                "content": "DEVICE LINK 自动发送",
                                "text_size": "small",
                            },
                        ],
                    },
                }
            }

            resp = self._session.post(
                self._webhook_url, json=payload,
                timeout=10,
                headers={'Content-Type': 'application/json'}
            )
            elapsed = (time.monotonic() - t0) * 1000.0

            if resp.status_code == 200:
                body = resp.json()
                if body.get('code') == 0:
                    logger.info(f"飞书发送成功: {title}, {elapsed:.0f}ms")
                    return SendResult(success=True, channel='feishu', latency_ms=elapsed)
                else:
                    err_msg = f"飞书错误码 {body.get('code')}: {body.get('msg', '')}"
                    logger.error(err_msg)
                    return SendResult(success=False, error=err_msg, channel='feishu')
            else:
                err_msg = f"HTTP {resp.status_code}"
                logger.error(f"飞书请求失败: {err_msg}")
                return SendResult(success=False, error=err_msg, channel='feishu')

        except requests.Timeout:
            return SendResult(success=False, error='飞书请求超时', channel='feishu')
        except Exception as e:
            logger.error(f"飞书发送异常: {e}", exc_info=True)
            return SendResult(success=False, error=str(e), channel='feishu')

    def _send_text(self, content: str) -> SendResult:
        """发送简单文本消息。"""
        try:
            payload = {"msg_type": "text", "content": {"text": content}}
            resp = self._session.post(
                self._webhook_url, json=payload, timeout=10
            )
            if resp.status_code == 200 and resp.json().get('code') == 0:
                return SendResult(success=True, channel='feishu')
            return SendResult(success=False, error=f"飞书返回错误", channel='feishu')
        except Exception as e:
            return SendResult(success=False, error=str(e), channel='feishu')
