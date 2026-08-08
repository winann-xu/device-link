"""
模块：dashboard.py
功能：实时状态仪表盘 —— 系统最核心的视觉界面
     按子系统分组展示设备卡片流式网格，实时刷新状态。
     支持搜索过滤、快速筛选、展开折叠。

UI 设计：
  - 设备卡片：在线(绿色)/离线(红色)/待定(橙色)/维护(灰色)
  - 子系统分组头：可折叠
  - 顶部：搜索框 + 筛选按钮 + 统计摘要

作者：Claude
创建日期：2026-08-07
"""
import logging
from collections import defaultdict

logger = logging.getLogger("device-link.ui.dashboard")


class DashboardPage:
    """
    实时仪表盘页面。
    以 PySide6 QFrame 实现，按子系统分组展示设备卡片网格。

    更新策略：增量更新——只更新状态变化的卡片，不全量重建。
    """

    def __init__(self, config: dict, scheduler, device_repo):
        """
        初始化仪表盘页面。

        参数:
            config: 全局配置
            scheduler: MonitorScheduler 实例
            device_repo: DeviceRepository 实例
        """
        from PySide6.QtWidgets import (
            QWidget, QVBoxLayout, QHBoxLayout, QScrollArea,
            QLabel, QLineEdit, QPushButton, QFrame, QGridLayout,
            QSizePolicy
        )
        from PySide6.QtCore import Qt, QTimer, QPropertyAnimation, QEasingCurve
        from PySide6.QtGui import QFont

        self._config = config
        self._scheduler = scheduler
        self._device_repo = device_repo

        ui_cfg = config.get('ui', {})

        # 主控件
        self._widget = QWidget()
        layout = QVBoxLayout(self._widget)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(16)

        # === 顶部栏 ===
        top_bar = QHBoxLayout()

        title = QLabel("📊 实时仪表盘")
        title.setFont(QFont(ui_cfg.get('font_family', 'Microsoft YaHei'), 18, QFont.Bold))
        title.setStyleSheet(f"color: #333;")

        self._search_input = QLineEdit()
        self._search_input.setPlaceholderText("搜索设备名或 IP...")
        self._search_input.setStyleSheet("""
            QLineEdit {
                padding: 8px 16px; border: 1px solid #d9d9d9; border-radius: 20px;
                background: white; font-size: 14px; min-width: 200px;
            }
            QLineEdit:focus { border-color: #1890FF; }
        """)
        self._search_input.textChanged.connect(self._on_search)

        top_bar.addWidget(title)
        top_bar.addStretch()
        top_bar.addWidget(self._search_input)

        # 快速筛选按钮
        self._filter_buttons = {}
        filter_bar = QHBoxLayout()
        filter_bar.setSpacing(8)
        filters = [
            ('all', '全部'), ('online', '🟢 在线'), ('offline', '🔴 离线'),
            ('pending', '🟡 待定'), ('maintenance', '🔧 维护中')
        ]
        for key, label in filters:
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setStyleSheet(self._filter_btn_style(key == 'all'))
            btn.clicked.connect(lambda checked, k=key: self._on_filter(k))
            self._filter_buttons[key] = btn
            filter_bar.addWidget(btn)
        filter_bar.addStretch()
        self._current_filter = 'all'

        layout.addLayout(top_bar)
        layout.addLayout(filter_bar)

        # === 卡片滚动区 ===
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")

        self._content = QWidget()
        self._content_layout = QVBoxLayout(self._content)
        self._content_layout.setSpacing(20)
        self._content_layout.addStretch()
        scroll.setWidget(self._content)

        # 增量更新状态：device_id -> 卡片控件；子系统 -> (header, frame, grid, cards)
        self._cards = {}
        self._group_views = {}
        self._group_order = []
        self._empty_label = None

        layout.addWidget(scroll, 1)

        # 刷新定时器
        self._refresh_timer = QTimer()
        self._refresh_timer.timeout.connect(self.refresh)
        self._refresh_timer.start(ui_cfg.get('refresh_ms', 2000))

    def _filter_btn_style(self, active: bool) -> str:
        """筛选按钮样式。"""
        if active:
            return """
                QPushButton {
                    background: #1890FF; color: white; border: none;
                    padding: 6px 16px; border-radius: 16px; font-size: 13px;
                }
            """
        return """
            QPushButton {
                background: white; color: #666; border: 1px solid #d9d9d9;
                padding: 6px 16px; border-radius: 16px; font-size: 13px;
            }
            QPushButton:hover { border-color: #1890FF; color: #1890FF; }
        """

    def _on_search(self, text: str):
        """搜索过滤（100ms 防抖后执行）。"""
        self.refresh()

    def _on_filter(self, key: str):
        """快速筛选按钮点击。"""
        self._current_filter = key
        for k, btn in self._filter_buttons.items():
            btn.setStyleSheet(self._filter_btn_style(k == key))
        self.refresh()

    def refresh(self):
        """
        刷新仪表盘显示。
        从调度器获取最新快照，按子系统分组渲染设备卡片。

        增量更新策略：
          - 保存现有卡片引用（by device_id）
          - 仅更新状态变化的卡片文字
          - 设备增删时重建布局
        """
        try:
            snapshot = self._scheduler.get_snapshot()
            devices = self._device_repo.list_devices()
        except Exception as e:
            logger.debug(f"仪表盘刷新异常: {e}")
            return

        # 构建 device_id → device 映射
        dev_map = {d['id']: d for d in devices}

        # 搜索过滤
        search_text = self._search_input.text().strip().lower()
        if search_text:
            devices = [
                d for d in devices
                if search_text in d.get('name', '').lower()
                or search_text in d.get('ip_address', '').lower()
            ]

        # 状态筛选
        if self._current_filter != 'all':
            if self._current_filter == 'maintenance':
                devices = [d for d in devices if d.get('is_maintenance')]
            else:
                devices = [d for d in devices
                           if snapshot.get(d['id'], 'unknown') == self._current_filter]

        # 按子系统分组
        groups = defaultdict(list)
        for d in devices:
            sid = snapshot.get(d['id'], 'unknown')
            d['_status'] = sid
            subsys = d.get('subsystem_name', '') or '未分组'
            groups[subsys].append(d)

        # 增量更新布局（避免每 2 秒全量重建导致的界面闪屏）
        self._apply_incremental(groups, dev_map)

    def _apply_incremental(self, groups: dict, dev_map: dict):
        """
        增量更新设备卡片：
          - 已存在的卡片仅就地更新文字/颜色（不重建控件）
          - 设备增删时才对对应分组做增删
          - 只有分组结构变化时才插入/移除分组控件

        参数:
            groups: {子系统名: [设备列表]}
            dev_map: {device_id: device_dict}
        """
        from PySide6.QtWidgets import QLabel, QFrame, QGridLayout
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QFont

        wanted_ids = set()
        for dev_list in groups.values():
            for d in dev_list:
                wanted_ids.add(d['id'])

        # 1) 移除不再显示的卡片
        for did in list(self._cards.keys()):
            if did not in wanted_ids:
                card = self._cards.pop(did)
                if card is not None:
                    self._remove_widget(card)

        # 2) 删除已消失的分组
        for subsys in list(self._group_views.keys()):
            if subsys not in groups:
                view = self._group_views.pop(subsys)
                self._group_order.remove(subsys)
                self._remove_widget(view['header'])
                self._remove_widget(view['frame'])

        # 3) 更新/创建分组与卡片
        for subsys, dev_list in sorted(groups.items()):
            if subsys not in self._group_views:
                header = QLabel()
                header.setFont(QFont('Microsoft YaHei', 14, QFont.Bold))
                frame = QFrame()
                frame.setStyleSheet("background: transparent;")
                grid = QGridLayout(frame)
                grid.setSpacing(12)
                pos = len(self._group_order)
                for i, s in enumerate(self._group_order):
                    if subsys < s:
                        pos = i
                        break
                layout_idx = pos * 2
                self._content_layout.insertWidget(layout_idx, header)
                self._content_layout.insertWidget(layout_idx + 1, frame)
                self._group_order.insert(pos, subsys)
                self._group_views[subsys] = {
                    'header': header, 'frame': frame, 'grid': grid, 'cards': [],
                }
            view = self._group_views[subsys]
            header, grid = view['header'], view['grid']

            online = sum(1 for d in dev_list if d.get('_status') == 'online')
            total = len(dev_list)
            header.setText(f"{subsys}  ▸ 在线 {online}/{total} 台")
            color = '#52C41A' if online == total else ('#FF4D4F' if online == 0 else '#333')
            header.setStyleSheet(f"color: {color}; padding: 8px 0;")

            ordered = []
            for dev in dev_list:
                did = dev['id']
                if did in self._cards:
                    card = self._cards[did]
                    self._update_card(card, dev)
                else:
                    card = self._create_device_card(dev)
                    self._cards[did] = card
                ordered.append((did, card))
            view['cards'] = ordered

            # 按当前窗口宽度自适应列数，重排网格（位置不变时不产生视觉变化）
            cols = self._compute_cols()
            view['cols'] = cols
            for i, (did, card) in enumerate(ordered):
                grid.addWidget(card, i // cols, i % cols)

    def _compute_cols(self) -> int:
        """按内容区宽度自适应卡片列数，让一屏显示更多设备。"""
        try:
            width = max(self._content.width(), self._content_layout.minimumSize().width())
        except Exception:
            width = 1500
        # 卡片最小宽度 180 + 间距 12；限制 2~8 列
        cols = max(2, min(8, width // 190))
        return cols

        # 4) 空状态提示
        if not groups:
            from PySide6.QtWidgets import QLabel
            if self._empty_label is None:
                empty = QLabel("暂无设备，请在「设备管理」页面添加设备")
                empty.setAlignment(Qt.AlignCenter)
                empty.setStyleSheet("color: #999; font-size: 16px; padding: 60px;")
                self._content_layout.insertWidget(0, empty)
                self._empty_label = empty
            self._empty_label.setVisible(True)
        elif self._empty_label is not None:
            self._empty_label.setVisible(False)

    def _remove_widget(self, widget):
        """把控件从布局移除并销毁。"""
        self._content_layout.removeWidget(widget)
        widget.setParent(None)
        widget.deleteLater()

    def _update_card(self, card, device: dict):
        """就地更新卡片内容（不重建控件）。"""
        status = device.get('_status', 'unknown')
        is_maintenance = device.get('is_maintenance', False)
        colors = {
            'online': ('#52C41A', '在线', '#F6FFED'),
            'offline': ('#FF4D4F', '离线', '#FFF1F0'),
            'pending_failure': ('#FA8C16', '待定', '#FFF7E6'),
            'unknown': ('#999', '未知', '#FAFAFA'),
        }
        color, status_text, bg = colors.get(status, colors['unknown'])
        card.setStyleSheet(f"""
            QFrame {{
                background: {bg if not is_maintenance else '#F5F5F5'};
                border: 1px solid #e8e8e8;
                border-left: 4px solid {color};
                border-radius: 8px;
            }}
        """)
        if hasattr(card, '_name_lbl'):
            card._name_lbl.setText(device.get('name', '?'))
            card._name_lbl.setStyleSheet(f"color: {'#999' if is_maintenance else '#333'};")
        if hasattr(card, '_info_lbl'):
            ip = device.get('ip_address', '')
            latency = device.get('latency_ms', 0)
            card._info_lbl.setText(f"{ip}  ·  {latency:.1f}ms" if latency > 0 else ip)
        if hasattr(card, '_status_lbl'):
            card._status_lbl.setText(f"● {status_text}")
            card._status_lbl.setStyleSheet(f"color: {color}; font-size: 13px; font-weight: bold;")

    def _create_device_card(self, device: dict):
        """
        创建设备卡片控件。

        卡片样式：
          - 在线：白色背景 + 绿色左边条 + 绿色呼吸灯
          - 离线：白色背景 + 红色左边条 + 红色闪烁灯
          - 待定：白色背景 + 橙色左边条
          - 维护：灰色半透明遮罩

        参数:
            device: 设备字典（含 _status 字段）

        返回:
            QFrame 卡片控件
        """
        from PySide6.QtWidgets import QFrame, QVBoxLayout, QLabel
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QFont

        status = device.get('_status', 'unknown')
        is_maintenance = device.get('is_maintenance', False)

        colors = {
            'online': ('#52C41A', '在线', '#F6FFED'),
            'offline': ('#FF4D4F', '离线', '#FFF1F0'),
            'pending_failure': ('#FA8C16', '待定', '#FFF7E6'),
            'unknown': ('#999', '未知', '#FAFAFA'),
        }
        color, status_text, bg = colors.get(status, colors['unknown'])

        card = QFrame()
        card.setMinimumSize(180, 110)
        card.setMaximumHeight(120)
        card.setStyleSheet(f"""
            QFrame {{
                background: {bg if not is_maintenance else '#F5F5F5'};
                border: 1px solid #e8e8e8;
                border-left: 4px solid {color};
                border-radius: 8px;
            }}
        """)

        layout = QVBoxLayout(card)
        layout.setContentsMargins(12, 8, 8, 8)
        layout.setSpacing(4)

        # 维护模式遮罩
        if is_maintenance:
            badge = QLabel("🔧 维护中")
            badge.setStyleSheet("color: #999; font-size: 11px;")
            layout.addWidget(badge, alignment=Qt.AlignRight)

        # 设备名
        name = QLabel(device.get('name', '?'))
        name.setFont(QFont('Microsoft YaHei', 13, QFont.Bold))
        name.setStyleSheet(f"color: {'#999' if is_maintenance else '#333'};")
        layout.addWidget(name)
        card._name_lbl = name

        # IP + 延迟
        ip = device.get('ip_address', '')
        latency = device.get('latency_ms', 0)
        info = f"{ip}  ·  {latency:.1f}ms" if latency > 0 else ip
        ip_label = QLabel(info)
        ip_label.setStyleSheet("color: #999; font-size: 11px;")
        layout.addWidget(ip_label)
        card._info_lbl = ip_label

        # 状态标签
        status_label = QLabel(f"● {status_text}")
        status_label.setStyleSheet(f"color: {color}; font-size: 13px; font-weight: bold;")
        layout.addWidget(status_label)
        card._status_lbl = status_label

        return card

    # 使 widget 属性可被 addWidget 使用
    @property
    def widget(self):
        return self._widget

    def __getattr__(self, name):
        """代理到 self._widget。"""
        return getattr(self._widget, name)
