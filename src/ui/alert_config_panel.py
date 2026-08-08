"""
模块：alert_config_panel.py
功能：告警与通知配置页面
     配置邮件/飞书/企微三个通知通道，含测试发送按钮。
     告警合并策略、全局规则、冷却窗口、升级机制。

作者：Claude
创建日期：2026-08-07
"""
import logging

logger = logging.getLogger("device-link.ui.alert_config")


class AlertConfigPanel:
    """
    告警与通知配置页面。
    三通道卡片 + 合并策略 + 全局规则 + 维护窗口管理。
    """

    def __init__(self, config: dict, alert_repo, alert_engine=None, config_path=None):
        from PySide6.QtWidgets import (
            QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
            QTabWidget, QFormLayout, QLineEdit, QSpinBox, QCheckBox,
            QGroupBox, QScrollArea, QFrame, QComboBox, QSlider
        )
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QFont

        self._config = config
        self._alert_repo = alert_repo
        self._alert_engine = alert_engine
        self._config_path = config_path
        self._channels = {}
        notify_cfg = config.get('notify', {})

        self._widget = QWidget()
        layout = QVBoxLayout(self._widget)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(16)

        title = QLabel("🔔 告警与通知配置")
        title.setFont(QFont('Microsoft YaHei', 18, QFont.Bold))
        title.setStyleSheet("color: #333;")
        layout.addWidget(title)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")

        content = QWidget()
        clayout = QVBoxLayout(content)
        clayout.setSpacing(16)

        # === 通知通道（标签页） ===
        tabs = QTabWidget()
        tabs.setStyleSheet("""
            QTabWidget::pane { border: 1px solid #e8e8e8; border-radius: 8px; background: white; }
            QTabBar::tab { padding: 10px 20px; font-size: 14px; }
            QTabBar::tab:selected { border-bottom: 2px solid #1890FF; color: #1890FF; }
        """)

        # 邮件标签页
        email_tab = self._create_channel_tab('email', notify_cfg.get('email', {}))
        feishu_tab = self._create_channel_tab('feishu', notify_cfg.get('feishu', {}))
        wecom_tab = self._create_channel_tab('wecom', notify_cfg.get('wecom', {}))

        tabs.addTab(email_tab, "📧 邮件")
        tabs.addTab(feishu_tab, "💬 飞书")
        tabs.addTab(wecom_tab, "💼 企业微信")

        clayout.addWidget(tabs)

        # === 告警合并策略 ===
        digest_cfg = notify_cfg.get('digest', {})
        digest_group = QGroupBox("📧 告警合并策略")
        digest_group.setStyleSheet(self._group_style())
        dlayout = QFormLayout(digest_group)

        digest_enabled = QCheckBox()
        digest_enabled.setChecked(digest_cfg.get('enabled', True))
        dlayout.addRow("启用告警合并摘要:", digest_enabled)

        window_spin = QSpinBox()
        window_spin.setRange(1, 60)
        window_spin.setValue(digest_cfg.get('window_seconds', 300) // 60)
        window_spin.setSuffix(" 分钟")
        dlayout.addRow("合并时间窗口:", window_spin)

        max_events = QSpinBox()
        max_events.setRange(5, 200)
        max_events.setValue(digest_cfg.get('max_events_per_digest', 50))
        dlayout.addRow("单封摘要最多:", max_events)

        critical_enabled = QCheckBox()
        critical_enabled.setChecked(digest_cfg.get('send_immediate_if_critical', True))
        dlayout.addRow("紧急绕过（同子系统≥5台立即发送）:", critical_enabled)

        clayout.addWidget(digest_group)

        # === 全局告警规则 ===
        rule_group = QGroupBox("⚙ 全局告警规则")
        rule_group.setStyleSheet(self._group_style())
        rlayout = QFormLayout(rule_group)

        n_slider = QSlider(Qt.Horizontal)
        n_slider.setRange(1, 10)
        n_slider.setValue(3)
        n_label = QLabel("3")
        n_slider.valueChanged.connect(lambda v: n_label.setText(str(v)))
        rlayout.addRow(QLabel("失败阈值 N:"), n_slider)
        rlayout.addRow("", n_label)

        m_slider = QSlider(Qt.Horizontal)
        m_slider.setRange(1, 5)
        m_slider.setValue(2)
        m_label = QLabel("2")
        m_slider.valueChanged.connect(lambda v: m_label.setText(str(v)))
        rlayout.addRow(QLabel("恢复阈值 M:"), m_slider)
        rlayout.addRow("", m_label)

        cooldown = QComboBox()
        cooldown.addItems(['15 分钟', '30 分钟', '60 分钟'])
        cooldown.setCurrentIndex(1)
        rlayout.addRow("冷却窗口:", cooldown)

        escalation = QComboBox()
        escalation.addItems(['5 分钟', '15 分钟', '30 分钟', '60 分钟'])
        escalation.setCurrentIndex(2)
        rlayout.addRow("升级时间:", escalation)

        clayout.addWidget(rule_group)

        # === 保存按钮 ===
        save_btn = QPushButton("💾 保存配置")
        save_btn.setStyleSheet("""
            QPushButton { background: #1890FF; color: white; padding: 10px 24px;
                          border-radius: 6px; font-size: 15px; }
            QPushButton:hover { background: #40a9ff; }
        """)
        save_btn.clicked.connect(lambda: self._on_save_config())
        clayout.addWidget(save_btn, alignment=Qt.AlignRight)

        clayout.addStretch()
        scroll.setWidget(content)
        layout.addWidget(scroll, 1)

    def _create_channel_tab(self, channel_type: str, cfg: dict):
        """创建通知通道配置标签页。"""
        from PySide6.QtWidgets import (
            QWidget, QVBoxLayout, QFormLayout, QLineEdit, QSpinBox,
            QCheckBox, QPushButton, QLabel
        )
        from PySide6.QtCore import Qt

        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)

        # 启停开关
        enabled = QCheckBox("启用此通道")
        enabled.setChecked(cfg.get('enabled', False))
        enabled.setStyleSheet("font-size: 16px; font-weight: bold;")
        layout.addWidget(enabled)

        entry = {"enabled": enabled}

        form = QFormLayout()
        form.setSpacing(12)

        if channel_type == 'email':
            smtp_host = QLineEdit(cfg.get('smtp_host', ''))
            smtp_host.setPlaceholderText("smtp.example.com")
            form.addRow("SMTP 服务器:", smtp_host)

            smtp_port = QSpinBox()
            smtp_port.setRange(1, 65535)
            smtp_port.setValue(cfg.get('smtp_port', 465))
            form.addRow("端口:", smtp_port)

            smtp_user = QLineEdit(cfg.get('smtp_user', ''))
            form.addRow("用户名:", smtp_user)

            smtp_pass = QLineEdit('*' * len(cfg.get('smtp_password', '')) if cfg.get('smtp_password') else '')
            smtp_pass.setEchoMode(QLineEdit.Password)
            smtp_pass.setPlaceholderText("输入 SMTP 密码")
            form.addRow("密码:", smtp_pass)

            recipients = QLineEdit(', '.join(cfg.get('recipients', [])) if isinstance(cfg.get('recipients'), list) else cfg.get('recipients', ''))
            recipients.setPlaceholderText("admin@example.com, ops@example.com")
            form.addRow("收件人:", recipients)
            entry.update(host=smtp_host, port=smtp_port, user=smtp_user,
                         passwd=smtp_pass, recipients=recipients)

        elif channel_type in ('feishu', 'wecom'):
            webhook_url = QLineEdit(cfg.get('webhook_url', ''))
            webhook_url.setPlaceholderText(
                "https://open.feishu.cn/open-apis/bot/v2/hook/xxx" if channel_type == 'feishu'
                else "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx"
            )
            form.addRow("Webhook URL:", webhook_url)
            entry["webhook"] = webhook_url

        layout.addLayout(form)

        # 测试按钮
        test_btn = QPushButton("📨 测试发送")
        test_btn.setStyleSheet("""
            QPushButton { background: white; border: 1px solid #1890FF; color: #1890FF;
                          padding: 8px 20px; border-radius: 4px; }
            QPushButton:hover { background: #e6f7ff; }
        """)
        test_btn.clicked.connect(lambda: self._on_test_channel(channel_type))
        layout.addWidget(test_btn)
        layout.addStretch()

        self._channels[channel_type] = entry

        return tab

    def _group_style(self) -> str:
        """分组框样式。"""
        return """
            QGroupBox {
                background: white; border: 1px solid #e8e8e8; border-radius: 8px;
                padding: 16px; margin-top: 8px; font-size: 15px; font-weight: bold;
            }
            QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 4px; }
        """

    def _on_test_channel(self, channel_type: str):
        """读取当前表单配置并发送测试消息，展示真实结果。"""
        from PySide6.QtWidgets import QMessageBox
        try:
            cfg = self._collect_channel_config(channel_type)
        except ValueError as e:
            QMessageBox.warning(self._widget, "测试结果", str(e))
            return

        if channel_type == 'email':
            from ..notify.email_channel import EmailChannel
            ch = EmailChannel(cfg, self._config.get('notify', {}))
        elif channel_type == 'feishu':
            from ..notify.feishu_channel import FeishuChannel
            ch = FeishuChannel(cfg)
        else:
            from ..notify.wecom_channel import WeComChannel
            ch = WeComChannel(cfg)

        res = ch.test()
        if res.success:
            QMessageBox.information(
                self._widget, "测试结果",
                f"✅ {channel_type} 测试发送成功（{res.latency_ms:.0f} ms）",
            )
        else:
            QMessageBox.critical(
                self._widget, "测试结果",
                f"❌ {channel_type} 测试发送失败\n{(res.error or '未知错误')[:400]}",
            )

    def _on_save_config(self):
        """收集表单配置 → 更新内存 → 写入 config.yaml → 重载引擎通道。"""
        from PySide6.QtWidgets import QMessageBox
        notify = self._config.setdefault('notify', {})
        try:
            for t in ('email', 'feishu', 'wecom'):
                notify[t] = self._collect_channel_config(t)
        except ValueError as e:
            QMessageBox.warning(self._widget, "保存配置", f"保存失败：{e}")
            return

        saved = False
        if self._config_path:
            try:
                import os
                import yaml
                os.makedirs(os.path.dirname(self._config_path), exist_ok=True)
                with open(self._config_path, 'w', encoding='utf-8') as f:
                    yaml.safe_dump(self._config, f, allow_unicode=True, sort_keys=False)
                saved = True
            except Exception as e:
                QMessageBox.critical(self._widget, "保存配置", f"写入配置文件失败：{e}")
                return

        n = 0
        if self._alert_engine is not None:
            n = self._alert_engine.reload_channels(self._config)

        msg = f"配置已保存并生效，当前启用通知通道 {n} 个。"
        if not saved:
            msg = "配置已更新到内存（未写入文件）。"
        QMessageBox.information(self._widget, "保存配置", msg)

    def _collect_channel_config(self, channel_type: str) -> dict:
        """从表单读取通道配置；校验必填项。密码为空或仍为掩码时保留旧值。"""
        entry = self._channels.get(channel_type)
        if not entry:
            raise ValueError("通道表单未初始化")
        cfg = dict(self._config.get('notify', {}).get(channel_type, {}))
        cfg['enabled'] = entry['enabled'].isChecked()

        if channel_type == 'email':
            cfg['smtp_host'] = entry['host'].text().strip()
            cfg['smtp_port'] = entry['port'].value()
            cfg['smtp_user'] = entry['user'].text().strip()
            pwd = entry['passwd'].text()
            if pwd and not set(pwd) <= {'*'}:
                from ..utils.crypto import encrypt
                cfg['smtp_password'] = encrypt(pwd)
            cfg.setdefault('smtp_password', '')
            # 465 = 隐式 SSL；其余（587 等）= STARTTLS
            cfg['use_ssl'] = (cfg.get('smtp_port', 465) == 465)
            cfg.setdefault('sender_name', 'DEVICE LINK')
            cfg['recipients'] = [r.strip() for r in entry['recipients'].text().split(',') if r.strip()]
            if cfg['enabled'] and (not cfg['smtp_host'] or not cfg['smtp_user']):
                raise ValueError("请填写 SMTP 服务器、用户名")
            if cfg['enabled'] and not cfg['recipients']:
                raise ValueError("请至少填写一个收件人")
        else:
            cfg['webhook_url'] = entry['webhook'].text().strip()
            if cfg['enabled'] and not cfg['webhook_url']:
                raise ValueError("请填写 Webhook URL")
        return cfg

    def refresh(self):
        """从配置重新填充表单值。"""
        notify = self._config.get('notify', {})
        for t, entry in self._channels.items():
            cfg = notify.get(t, {})
            entry['enabled'].setChecked(cfg.get('enabled', False))
            if t == 'email':
                entry['host'].setText(cfg.get('smtp_host', ''))
                entry['port'].setValue(cfg.get('smtp_port', 465))
                entry['user'].setText(cfg.get('smtp_user', ''))
                pwd = cfg.get('smtp_password', '')
                entry['passwd'].setText('*' * min(len(pwd), 12) if pwd else '')
                rcpts = cfg.get('recipients', [])
                entry['recipients'].setText(', '.join(rcpts) if isinstance(rcpts, list) else str(rcpts or ''))
            else:
                entry['webhook'].setText(cfg.get('webhook_url', ''))

    @property
    def widget(self):
        return self._widget
