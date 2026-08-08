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

    def __init__(self, config: dict, alert_repo):
        from PySide6.QtWidgets import (
            QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
            QTabWidget, QFormLayout, QLineEdit, QSpinBox, QCheckBox,
            QGroupBox, QScrollArea, QFrame, QComboBox, QSlider
        )
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QFont

        self._config = config
        self._alert_repo = alert_repo
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

        elif channel_type in ('feishu', 'wecom'):
            webhook_url = QLineEdit(cfg.get('webhook_url', ''))
            webhook_url.setPlaceholderText(
                "https://open.feishu.cn/open-apis/bot/v2/hook/xxx" if channel_type == 'feishu'
                else "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx"
            )
            form.addRow("Webhook URL:", webhook_url)

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
        """测试通知通道连通性（简化版）。"""
        from PySide6.QtWidgets import QMessageBox
        QMessageBox.information(
            self._widget, "测试结果",
            f"{channel_type} 通道测试功能：请在完整 GUI 环境中使用保存配置后的测试按钮。"
        )

    def _on_save_config(self):
        """保存配置（简化版提示）。"""
        from PySide6.QtWidgets import QMessageBox
        QMessageBox.information(self._widget, "提示",
                                 "配置保存功能在完整环境中可用。\n当前界面为预览模式。")

    def refresh(self):
        """刷新页面。"""
        pass

    @property
    def widget(self):
        return self._widget
