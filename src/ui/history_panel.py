"""
模块：history_panel.py
功能：历史查询与在线率统计页面
     在线率概览卡片、状态时间线、离线时长排行榜、告警日志表格。

作者：Claude
创建日期：2026-08-07
"""
import logging
from datetime import datetime, timedelta
from PySide6.QtWidgets import QFrame

logger = logging.getLogger("device-link.ui.history")


class HistoryPanel:
    """
    历史统计页面。
    展示在线率、离线排行榜、告警日志，支持筛选和导出。
    """

    def __init__(self, config: dict, device_repo, history_repo, alert_repo=None):
        from PySide6.QtWidgets import (
            QWidget, QVBoxLayout, QHBoxLayout, QLabel, QTableWidget,
            QTableWidgetItem, QComboBox, QPushButton, QHeaderView, QFrame
        )
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QFont

        self._config = config
        self._device_repo = device_repo
        self._history_repo = history_repo
        self._alert_repo = alert_repo

        self._widget = QWidget()
        layout = QVBoxLayout(self._widget)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(16)

        title = QLabel("📈 历史与统计")
        title.setFont(QFont('Microsoft YaHei', 18, QFont.Bold))
        title.setStyleSheet("color: #333;")
        layout.addWidget(title)

        # === 设备选择 + 时间范围 ===
        top_bar = QHBoxLayout()
        self._device_combo = QComboBox()
        self._device_combo.setMinimumWidth(200)
        self._device_combo.setStyleSheet("padding: 6px; font-size: 14px;")
        self._device_combo.currentIndexChanged.connect(self.refresh)
        top_bar.addWidget(QLabel("设备:"))
        top_bar.addWidget(self._device_combo)

        time_btns = [
            ('今天', 'day'), ('7天', 'week'), ('30天', 'month')
        ]
        for label, period in time_btns:
            btn = QPushButton(label)
            btn.setStyleSheet("""
                QPushButton { background: white; color: #333; border: 1px solid #d9d9d9;
                              border-radius: 4px; }
                QPushButton:hover { border-color: #1890FF; }
            """)
            btn.clicked.connect(lambda checked=False, p=period: self._on_time_range(p))
            top_bar.addWidget(btn)

        top_bar.addStretch()
        refresh_btn = QPushButton("🔄 刷新")
        refresh_btn.clicked.connect(self.refresh)
        top_bar.addWidget(refresh_btn)

        layout.addLayout(top_bar)

        # === 在线率概览卡片 ===
        cards_layout = QHBoxLayout()
        cards_layout.setSpacing(16)
        self._uptime_day = self._create_stat_card("今日在线率", "--")
        self._uptime_week = self._create_stat_card("本周在线率", "--")
        self._uptime_month = self._create_stat_card("本月在线率", "--")
        cards_layout.addWidget(self._uptime_day)
        cards_layout.addWidget(self._uptime_week)
        cards_layout.addWidget(self._uptime_month)
        layout.addLayout(cards_layout)

        # === 离线排行榜 ===
        rank_label = QLabel("📉 离线时长排行榜 (最近 7 天)")
        rank_label.setFont(QFont('Microsoft YaHei', 14, QFont.Bold))
        layout.addWidget(rank_label)

        self._toplist_table = QTableWidget()
        self._toplist_table.setColumnCount(5)
        self._toplist_table.setHorizontalHeaderLabels([
            '排名', '设备名', 'IP', '子系统', '离线次数'
        ])
        self._toplist_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self._toplist_table.setMaximumHeight(200)
        self._toplist_table.setStyleSheet("background: white; border: 1px solid #e8e8e8; border-radius: 4px;")
        layout.addWidget(self._toplist_table)

        # === 告警日志 ===
        log_label = QLabel("📋 告警日志")
        log_label.setFont(QFont('Microsoft YaHei', 14, QFont.Bold))
        layout.addWidget(log_label)

        self._alert_table = QTableWidget()
        self._alert_table.setColumnCount(6)
        self._alert_table.setHorizontalHeaderLabels([
            '时间', '设备', '类型', '消息', '渠道', '确认'
        ])
        self._alert_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self._alert_table.setStyleSheet("background: white; border: 1px solid #e8e8e8; border-radius: 4px;")
        layout.addWidget(self._alert_table, 1)

    def _create_stat_card(self, title: str, value: str) -> QFrame:
        """创建统计卡片。"""
        from PySide6.QtWidgets import QFrame, QVBoxLayout, QLabel
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QFont

        card = QFrame()
        card.setStyleSheet("""
            QFrame { background: white; border: 1px solid #e8e8e8;
                     border-radius: 8px; }
        """)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(8)
        layout.setAlignment(Qt.AlignCenter)

        t = QLabel(title)
        t.setAlignment(Qt.AlignCenter)
        t.setStyleSheet("color: #999; font-size: 13px;")
        layout.addWidget(t)

        v = QLabel(value)
        v.setAlignment(Qt.AlignCenter)
        v.setFont(QFont('Microsoft YaHei', 28, QFont.Bold))
        v.setStyleSheet("color: #1890FF;")
        v.setObjectName(f"stat_{title}")
        layout.addWidget(v)

        return card

    def _on_time_range(self, period: str):
        """时间范围切换。"""
        self.refresh()

    def refresh(self):
        """刷新历史统计数据。"""
        if getattr(self, '_refreshing', False):
            return
        self._refreshing = True
        try:
            devices = self._device_repo.list_devices()
            selected_id = self._device_combo.currentData()
            # 重建下拉列表时屏蔽信号，避免 currentIndexChanged -> refresh 递归（栈溢出）
            self._device_combo.blockSignals(True)
            try:
                self._device_combo.clear()
                self._device_combo.addItem("全部设备", None)
                for d in devices:
                    self._device_combo.addItem(f"{d.get('name', '')} ({d.get('ip_address', '')})", d['id'])
                # 恢复用户之前的选择（若仍存在）
                if selected_id is not None:
                    idx = self._device_combo.findData(selected_id)
                    if idx >= 0:
                        self._device_combo.setCurrentIndex(idx)
            finally:
                self._device_combo.blockSignals(False)

            # 更新在线率卡片
            from PySide6.QtWidgets import QLabel
            for period, card, title in [
                ('day', self._uptime_day, '今日在线率'),
                ('week', self._uptime_week, '本周在线率'),
                ('month', self._uptime_month, '本月在线率'),
            ]:
                try:
                    if selected_id is None:
                        uptime = self._history_repo.compute_overall_uptime(period)
                    else:
                        uptime = self._history_repo.compute_uptime(selected_id, period)
                    label = card.findChild(QLabel, f"stat_{title}")
                    if label:
                        label.setText(f"{uptime * 100:.1f}%")
                except Exception as e:
                    logger.error(f"在线率统计失败({period}): {e}")

            # 离线排行榜
            toplist = self._history_repo.get_offline_toplist(7, 10)
            self._toplist_table.setRowCount(len(toplist))
            for i, item in enumerate(toplist):
                from PySide6.QtWidgets import QTableWidgetItem
                from PySide6.QtCore import Qt
                self._toplist_table.setItem(i, 0, self._item(str(i + 1)))
                self._toplist_table.setItem(i, 1, self._item(item.get('name', '')))
                self._toplist_table.setItem(i, 2, self._item(item.get('ip_address', '')))
                self._toplist_table.setItem(i, 3, self._item(item.get('subsystem_name', '')))
                self._toplist_table.setItem(i, 4, self._item(str(item.get('offline_count', 0))))

            # 告警日志
            if self._alert_repo is not None:
                events = self._alert_repo.list_events(limit=200)
                self._alert_table.setRowCount(len(events))
                from PySide6.QtWidgets import QTableWidgetItem
                from PySide6.QtCore import Qt
                for i, ev in enumerate(events):
                    dev = self._device_repo.get_device(ev.get('device_id', 0)) or {}
                    self._alert_table.setItem(i, 0, self._item(ev.get('created_at', '')))
                    self._alert_table.setItem(i, 1, self._item(dev.get('name', f"#{ev.get('device_id', '')}")))
                    self._alert_table.setItem(i, 2, self._item(ev.get('event_type', '')))
                    self._alert_table.setItem(i, 3, self._item(ev.get('message', '')))
                    self._alert_table.setItem(i, 4, self._item(ev.get('notified_channels', '') or '-'))
                    ack = '已确认' if ev.get('is_acknowledged') else '未确认'
                    self._alert_table.setItem(i, 5, self._item(ack))

        except Exception as e:
            logger.error(f"历史数据刷新失败: {e}")
        finally:
            self._refreshing = False

    def _item(self, text: str):
        from PySide6.QtWidgets import QTableWidgetItem
        from PySide6.QtCore import Qt
        item = QTableWidgetItem(str(text))
        item.setTextAlignment(Qt.AlignCenter)
        return item

    @property
    def widget(self):
        return self._widget
