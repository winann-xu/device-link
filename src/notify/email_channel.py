"""
模块：email_channel.py
功能：SMTP 邮件通知通道
     使用 smtplib + email.mime 发送 HTML 格式通知邮件。
     支持 SSL(465) 和 STARTTLS(587) 两种模式自动适配。
     HTML 模板内联 CSS，移动端友好。

作者：Claude
创建日期：2026-08-07
"""
import time
import logging
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import Header

from .base_channel import BaseNotificationChannel, AlertMessage, SendResult
from ..utils.crypto import decrypt

logger = logging.getLogger("device-link.notify.email")


class EmailChannel(BaseNotificationChannel):
    """
    SMTP 邮件通知通道。
    支持 SSL(465) / STARTTLS(587) 自动适配。
    HTML 邮件模板包含四种样式：告警(红)/恢复(绿)/升级(橙)/摘要(蓝)。
    """

    def __init__(self, channel_config: dict, notify_config: dict):
        """
        初始化邮件通道。

        参数:
            channel_config: email 通道配置（含 smtp_host, smtp_port, smtp_user, smtp_password 等）
            notify_config: 全局通知配置（含 sender_name, retry_count 等）
        """
        self._smtp_host = channel_config.get('smtp_host', '')
        self._smtp_port = channel_config.get('smtp_port', 465)
        self._smtp_user = channel_config.get('smtp_user', '')
        self._smtp_password_encrypted = channel_config.get('smtp_password', '')
        self._use_ssl = channel_config.get('use_ssl', True)
        self._sender_name = channel_config.get('sender_name', 'DEVICE LINK')
        self._recipients = channel_config.get('recipients', [])
        if isinstance(self._recipients, str):
            self._recipients = [r.strip() for r in self._recipients.split(',') if r.strip()]

    def get_channel_name(self) -> str:
        return "email"

    def test(self) -> SendResult:
        """发送测试邮件验证配置。"""
        msg = AlertMessage(
            event_type='test',
            device_name='测试设备',
            ip_address='127.0.0.1',
            message='如果您收到此邮件，说明 DEVICE LINK 邮件通知通道配置正确。',
            occurred_at=time.strftime('%Y-%m-%d %H:%M:%S'),
        )
        msg.extra['is_test'] = True
        return self.send(msg)

    def send(self, message: AlertMessage) -> SendResult:
        """
        发送通知邮件。

        消息类型决定 HTML 模板：
          - offline: 红色标题栏 + 设备信息表
          - recovery: 绿色标题栏 + 离线时长
          - escalation: 橙色标题栏 + 升级信息
          - digest: 蓝色标题栏 + 汇总表格
          - test: 蓝色标题栏
        """
        t0 = time.monotonic()
        try:
            # 解密 SMTP 密码
            smtp_password = self._decrypt_password()

            # 构造邮件
            html_body = self._render_html(message)
            subject = self._build_subject(message)

            msg = MIMEMultipart('alternative')
            msg['Subject'] = Header(subject, 'utf-8')
            msg['From'] = f"{self._sender_name} <{self._smtp_user}>"
            msg['To'] = ', '.join(self._recipients)
            msg.attach(MIMEText(html_body, 'html', 'utf-8'))

            # 发送
            if self._use_ssl:
                with smtplib.SMTP_SSL(self._smtp_host, self._smtp_port, timeout=15) as smtp:
                    smtp.login(self._smtp_user, smtp_password)
                    smtp.sendmail(self._smtp_user, self._recipients, msg.as_string())
            else:
                with smtplib.SMTP(self._smtp_host, self._smtp_port, timeout=15) as smtp:
                    smtp.starttls()
                    smtp.login(self._smtp_user, smtp_password)
                    smtp.sendmail(self._smtp_user, self._recipients, msg.as_string())

            elapsed = (time.monotonic() - t0) * 1000.0
            logger.info(f"邮件发送成功: {subject} → {len(self._recipients)} 收件人, {elapsed:.0f}ms")
            return SendResult(success=True, channel='email', latency_ms=elapsed)

        except smtplib.SMTPAuthenticationError as e:
            logger.error(f"SMTP 认证失败: {e}")
            return SendResult(success=False, error=f"SMTP 认证失败: {e}", channel='email')
        except smtplib.SMTPConnectError as e:
            logger.error(f"SMTP 连接失败: {e}")
            return SendResult(success=False, error=f"SMTP 连接失败: {e}", channel='email')
        except Exception as e:
            logger.error(f"邮件发送异常: {e}", exc_info=True)
            return SendResult(success=False, error=str(e), channel='email')

    def _decrypt_password(self) -> str:
        """解密 SMTP 密码。如果密码已经是明文则直接返回。"""
        if not self._smtp_password_encrypted:
            return ''
        try:
            return decrypt(self._smtp_password_encrypted)
        except Exception:
            # 可能是明文存储的
            return self._smtp_password_encrypted

    def _build_subject(self, msg: AlertMessage) -> str:
        """构造邮件主题。"""
        tags = {
            'offline': '[严重]',
            'recovery': '[已恢复]',
            'escalation': '[升级]',
            'digest': '[摘要]',
            'daily_report': '[每日清单]',
            'test': '[测试]',
        }
        tag = tags.get(msg.event_type, '')
        dev_name = msg.device_name or '未知设备'
        return f"[DEVICE LINK]{tag} {dev_name}"

    def _render_html(self, msg: AlertMessage) -> str:
        """
        渲染 HTML 邮件正文。
        根据事件类型选择不同模板，内联 CSS 确保各大邮件客户端兼容。
        """
        colors = {
            'offline': ('#FF4D4F', '#FFF1F0', '设备离线告警'),
            'recovery': ('#52C41A', '#F6FFED', '设备恢复通知'),
            'escalation': ('#FA8C16', '#FFF7E6', '告警升级通知'),
            'digest': ('#1890FF', '#E6F7FF', '告警合并摘要'),
            'daily_report': ('#722ED1', '#F9F0FF', '每日离线设备清单'),
            'test': ('#1890FF', '#E6F7FF', '测试邮件'),
        }
        color, bg_color, title = colors.get(msg.event_type, colors['test'])

        extra_info = ''
        if msg.event_type == 'recovery':
            downtime = msg.extra.get('downtime_duration', '未知')
            extra_info = f'<p style="font-size:14px;">离线时长：<b>{downtime}</b></p>'
        elif msg.event_type == 'escalation':
            minutes = msg.extra.get('escalation_minutes', 0)
            extra_info = f'<p style="font-size:14px;">该告警已持续 <b>{minutes} 分钟</b> 未确认，自动升级通知</p>'
        elif msg.event_type in ('digest', 'daily_report'):
            events_list = msg.extra.get('events', [])
            is_daily = (msg.event_type == 'daily_report')
            downtime_header = '<th style="padding:8px; text-align:left;">离线时长</th>' if is_daily else ''
            time_header = '最近探测' if is_daily else '时间'
            rows = ''
            for e in events_list:
                if isinstance(e, dict):
                    time_col = e.get('occurred_at') or e.get('last_check_time', '')
                    downtime = e.get('downtime', '')
                    downtime_cell = (
                        f'<td style="padding:8px; border-bottom:1px solid #f0f0f0;">{downtime}</td>'
                        if is_daily else ''
                    )
                    rows += f"""
                    <tr>
                      <td style="padding:8px; border-bottom:1px solid #f0f0f0;">{e.get('device_name', '')}</td>
                      <td style="padding:8px; border-bottom:1px solid #f0f0f0;">{e.get('ip_address', '')}</td>
                      <td style="padding:8px; border-bottom:1px solid #f0f0f0;">{e.get('subsystem', '')}</td>
                      <td style="padding:8px; border-bottom:1px solid #f0f0f0;">{time_col}</td>
                      {downtime_cell}
                    </tr>"""
            extra_info = f"""
            <table style="width:100%; border-collapse:collapse; margin-top:16px;">
              <thead><tr style='background:#fafafa;'>
                <th style='padding:8px; text-align:left;'>设备</th><th style='padding:8px; text-align:left;'>IP</th>
                <th style='padding:8px; text-align:left;'>子系统</th><th style='padding:8px; text-align:left;'>{time_header}</th>
                {downtime_header}
              </tr></thead>
              <tbody>{rows}</tbody>
            </table>"""

        html = f"""
        <html><body style="font-family: 'Microsoft YaHei', Arial, sans-serif; background:#f5f5f5; padding:20px;">
        <div style="max-width:600px; margin:0 auto; background:#fff; border-radius:8px; overflow:hidden;
                    box-shadow: 0 2px 8px rgba(0,0,0,0.1);">
          <div style="background:{color}; padding:20px; color:#fff;">
            <h2 style="margin:0; font-size:18px;">{title}</h2>
          </div>
          <div style="padding:24px;">
            <table style="width:100%; border-collapse:collapse; margin-bottom:16px;">
              <tr><td style="padding:6px 0; color:#666; width:80px;">设备名</td>
                  <td style="padding:6px 0;"><b>{msg.device_name}</b></td></tr>
              <tr><td style="padding:6px 0; color:#666;">IP 地址</td>
                  <td style="padding:6px 0;">{msg.ip_address}</td></tr>
              <tr><td style="padding:6px 0; color:#666;">子系统</td>
                  <td style="padding:6px 0;">{msg.subsystem}</td></tr>
              <tr><td style="padding:6px 0; color:#666;">时间</td>
                  <td style="padding:6px 0;">{msg.occurred_at}</td></tr>
            </table>
            {extra_info}
            <p style="color:#999; font-size:12px; margin-top:20px;">
              此邮件由 DEVICE LINK 自动发送，请勿回复。
            </p>
          </div>
        </div>
        </body></html>"""
        return html
