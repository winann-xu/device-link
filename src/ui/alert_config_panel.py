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

    def __init__(self, config: dict, alert_repo, alert_engine=None,
                 config_path=None, device_repo=None, scheduler=None):
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
        self._device_repo = device_repo
        self._scheduler = scheduler
        self._channels = {}
        notify_cfg = config.get('notify', {})
        monitor_cfg = config.get('monitor', {})

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

        self._digest_enabled_cb = QCheckBox()
        self._digest_enabled_cb.setChecked(digest_cfg.get('enabled', True))
        dlayout.addRow("启用告警合并摘要:", self._digest_enabled_cb)

        self._digest_window_spin = QSpinBox()
        self._digest_window_spin.setRange(1, 60)
        self._digest_window_spin.setValue(digest_cfg.get('window_seconds', 300) // 60)
        self._digest_window_spin.setSuffix(" 分钟")
        dlayout.addRow("合并时间窗口:", self._digest_window_spin)

        self._digest_max_spin = QSpinBox()
        self._digest_max_spin.setRange(5, 200)
        self._digest_max_spin.setValue(digest_cfg.get('max_events_per_digest', 50))
        dlayout.addRow("单封摘要最多:", self._digest_max_spin)

        self._digest_critical_cb = QCheckBox()
        self._digest_critical_cb.setChecked(
            digest_cfg.get('send_immediate_if_critical', True))
        dlayout.addRow("紧急绕过（同子系统≥5台立即发送）:", self._digest_critical_cb)

        # 每日离线报告（v1.0.9）：每天 08:00 发送当前离线设备清单
        daily_cfg = notify_cfg.get('daily_report', {})
        self._daily_report_cb = QCheckBox()
        self._daily_report_cb.setChecked(daily_cfg.get('enabled', False))
        dlayout.addRow("每日 08:00 发送离线设备清单:", self._daily_report_cb)

        daily_hint = QLabel(
            "说明：每天 08:00 定时发送一封「当前离线设备清单」邮件；\n"
            "      当天离线设备数量为 0 时不发送；仅统计离线设备，\n"
            "      待定（pending_failure）与维护中设备均不计入。"
        )
        daily_hint.setStyleSheet(
            "color: #999; font-size: 12px; padding: 4px 0 8px 0;"
        )
        daily_hint.setWordWrap(True)
        dlayout.addRow("", daily_hint)

        clayout.addWidget(digest_group)

        # === 全局告警规则 ===
        rule_group = QGroupBox("⚙ 全局告警规则")
        rule_group.setStyleSheet(self._group_style())
        rlayout = QFormLayout(rule_group)

        self._rule_n_slider = QSlider(Qt.Horizontal)
        self._rule_n_slider.setRange(1, 10)
        self._rule_n_slider.setValue(
            monitor_cfg.get('default_failure_threshold', 3))
        n_label = QLabel(str(self._rule_n_slider.value()))
        self._rule_n_slider.valueChanged.connect(
            lambda v: n_label.setText(str(v)))
        rlayout.addRow(QLabel("失败阈值 N:"), self._rule_n_slider)
        rlayout.addRow("", n_label)

        self._rule_m_slider = QSlider(Qt.Horizontal)
        self._rule_m_slider.setRange(1, 5)
        self._rule_m_slider.setValue(
            monitor_cfg.get('default_recovery_threshold', 2))
        m_label = QLabel(str(self._rule_m_slider.value()))
        self._rule_m_slider.valueChanged.connect(
            lambda v: m_label.setText(str(v)))
        rlayout.addRow(QLabel("恢复阈值 M:"), self._rule_m_slider)
        rlayout.addRow("", m_label)

        self._rule_cooldown_combo = QComboBox()
        self._rule_cooldown_combo.addItems(['15 分钟', '30 分钟', '60 分钟'])
        cooldown_seconds = notify_cfg.get('cooldown_seconds', 1800)
        self._rule_cooldown_combo.setCurrentIndex(
            {900: 0, 1800: 1, 3600: 2}.get(cooldown_seconds, 1))
        rlayout.addRow("冷却窗口:", self._rule_cooldown_combo)

        self._rule_escalation_combo = QComboBox()
        self._rule_escalation_combo.addItems(
            ['关闭', '5 分钟', '15 分钟', '30 分钟', '60 分钟'])
        escalation_minutes = notify_cfg.get('escalation_minutes', 15)
        escalation_enabled = notify_cfg.get('escalation_enabled', True)
        self._rule_escalation_combo.setCurrentIndex(
            0 if not escalation_enabled else
            {5: 1, 15: 2, 30: 3, 60: 4}.get(escalation_minutes, 2))
        rlayout.addRow("升级时间:", self._rule_escalation_combo)

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
            msg = f"✅ {channel_type} 测试发送成功（{res.latency_ms:.0f} ms）"
            if not cfg.get('enabled'):
                msg += (
                    "\n\n⚠️ 该通道未勾选『启用』，真实告警不会发送。"
                    "请勾选启用后点击『保存配置』。"
                )
            QMessageBox.information(
                self._widget, "测试结果",
                msg,
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

        # 合并策略
        notify['digest'] = {
            'enabled': self._digest_enabled_cb.isChecked(),
            'window_seconds': self._digest_window_spin.value() * 60,
            'max_events_per_digest': self._digest_max_spin.value(),
            'send_immediate_if_critical': self._digest_critical_cb.isChecked(),
        }
        # 每日离线报告（保留已有 send_time，默认 08:00）
        notify['daily_report'] = {
            'enabled': self._daily_report_cb.isChecked(),
            'send_time': notify.get('daily_report', {}).get('send_time', '08:00'),
        }
        # 冷却/升级
        notify['cooldown_seconds'] = {
            '15 分钟': 900, '30 分钟': 1800, '60 分钟': 3600
        }.get(self._rule_cooldown_combo.currentText(), 1800)
        escalation_text = self._rule_escalation_combo.currentText()
        if escalation_text == '关闭':
            notify['escalation_enabled'] = False
            notify['escalation_minutes'] = 15
        else:
            notify['escalation_enabled'] = True
            notify['escalation_minutes'] = {
                '5 分钟': 5, '15 分钟': 15, '30 分钟': 30, '60 分钟': 60
            }.get(escalation_text, 15)
        # 全局失败/恢复阈值
        failure_n = self._rule_n_slider.value()
        recovery_m = self._rule_m_slider.value()
        self._config.setdefault('monitor', {})
        self._config['monitor']['default_failure_threshold'] = failure_n
        self._config['monitor']['default_recovery_threshold'] = recovery_m

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

        # 全局阈值应用到现有设备（DB）与运行中的状态机（调度器）
        # 修复（v1.0.7.1）：原来逐台 update_device()（每台一次 commit），
        # 上千台设备在探测并发写下保存耗时数分钟、GUI 卡死；
        # 改为单事务批量 UPDATE。
        applied_devices = 0
        if self._device_repo is not None:
            try:
                applied_devices = self._device_repo.apply_global_thresholds_to_db(
                    failure_n, recovery_m
                )
            except Exception as e:
                logger.error(f"应用全局阈值到设备失败: {e}")
        if self._scheduler is not None:
            try:
                self._scheduler.apply_global_thresholds(failure_n, recovery_m)
            except Exception as e:
                logger.error(f"应用全局阈值到调度器失败: {e}")

        n = 0
        if self._alert_engine is not None:
            n = self._alert_engine.reload_channels(self._config)

        esc_desc = '关闭' if not notify.get('escalation_enabled', True) else \
            f'开启（{notify.get("escalation_minutes", 15)} 分钟，每事件最多 3 次）'
        msg = (
            f"配置已保存并生效，当前启用通知通道 {n} 个。\n"
            f"告警合并摘要：{'开' if notify['digest']['enabled'] else '关'}，"
            f"窗口 {notify['digest']['window_seconds'] // 60} 分钟，"
            f"单封最多 {notify['digest']['max_events_per_digest']} 条；\n"
            f"每日离线清单：{'开（每天 08:00）' if notify['daily_report']['enabled'] else '关'}；\n"
            f"告警升级：{esc_desc}；\n"
            f"全局规则：失败阈值 N={failure_n}，恢复阈值 M={recovery_m}，"
            f"已应用到 {applied_devices} 台设备。"
        )
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
        monitor = self._config.get('monitor', {})
        digest = notify.get('digest', {})
        self._digest_enabled_cb.setChecked(digest.get('enabled', True))
        self._digest_window_spin.setValue(
            digest.get('window_seconds', 300) // 60)
        self._digest_max_spin.setValue(
            digest.get('max_events_per_digest', 50))
        self._digest_critical_cb.setChecked(
            digest.get('send_immediate_if_critical', True))
        daily = notify.get('daily_report', {})
        self._daily_report_cb.setChecked(daily.get('enabled', False))
        self._rule_n_slider.setValue(
            monitor.get('default_failure_threshold', 3))
        self._rule_m_slider.setValue(
            monitor.get('default_recovery_threshold', 2))
        self._rule_cooldown_combo.setCurrentIndex(
            {900: 0, 1800: 1, 3600: 2}.get(
                notify.get('cooldown_seconds', 1800), 1))
        esc_enabled = notify.get('escalation_enabled', True)
        self._rule_escalation_combo.setCurrentIndex(
            0 if not esc_enabled else
            {5: 1, 15: 2, 30: 3, 60: 4}.get(
                notify.get('escalation_minutes', 15), 2))
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
