"""
模块：main_window.py
功能：DEVICE LINK 主窗口
     使用 PySide6 构建，包含系统托盘、侧边导航、QStackedWidget 多页面切换。
     界面美观——深色侧边栏、圆角卡片、QSS 全局样式。

作者：Claude
创建日期：2026-08-07
"""
import os
import sys
import logging
from pathlib import Path

logger = logging.getLogger("device-link.ui")


class MainWindow:
    """
    DEVICE LINK 主窗口。

    窗口结构：
      ┌─────────────────────────────────────────┐
      │  🔗 DEVICE LINK  v1.0.0    [_][□][×]   │
      ├────────────┬────────────────────────────┤
      │ 侧边导航   │  QStackedWidget 主内容区     │
      │ [实时仪表盘]│                            │
      │ [设备管理]  │                            │
      │ [告警配置]  │                            │
      │ [历史统计]  │                            │
      ├────────────┴────────────────────────────┤
      │ ● 监控中 │ 在线 X/Y │ 状态栏              │
      └─────────────────────────────────────────┘

    关键行为：
      - 点击 × → 隐藏到托盘（不退出）
      - 托盘退出 → 确认对话框 → 停止调度器 → 退出
      - start_minimized → 启动直接进托盘
    """

    def __init__(self, config: dict, scheduler, device_repo, history_repo, alert_repo,
                 alert_engine=None, config_path=None):
        """
        初始化主窗口。

        参数:
            config: 全局配置
            scheduler: MonitorScheduler 实例
            device_repo: DeviceRepository 实例
            history_repo: HistoryRepository 实例
            alert_repo: AlertRepository 实例
        """
        from PySide6.QtWidgets import (
            QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
            QStackedWidget, QListWidget, QListWidgetItem,
            QLabel, QPushButton, QStatusBar, QSystemTrayIcon,
            QMenu, QMessageBox, QApplication, QFrame
        )
        from PySide6.QtCore import Qt, QTimer, QSize
        from PySide6.QtGui import QIcon, QAction, QFont
        from PySide6.QtWidgets import QGraphicsDropShadowEffect

        self._config = config
        self._scheduler = scheduler
        self._device_repo = device_repo
        self._history_repo = history_repo
        self._alert_repo = alert_repo
        self._alert_engine = alert_engine
        self._config_path = config_path

        ui_cfg = config.get('ui', {})
        self._accent_color = ui_cfg.get('accent_color', '#1890FF')

        # 创建 QApplication（在外部创建，这里获取实例）
        self._qt_app = QApplication.instance()

        # === 主窗口 ===
        self._window = QMainWindow()
        self._window.setWindowTitle(f"DEVICE LINK v{config.get('app', {}).get('version', '1.0.0')}")
        self._window.setMinimumSize(1100, 700)
        self._window.resize(1200, 800)

        # 应用图标
        icon_path = self._find_icon()
        if icon_path:
            self._window.setWindowIcon(QIcon(icon_path))

        # === 中心控件 ===
        central = QWidget()
        self._window.setCentralWidget(central)
        main_layout = QHBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # === 侧边导航栏 ===
        self._nav = QListWidget()
        self._nav.setFixedWidth(200)
        self._nav.setObjectName('sideNav')
        self._nav.setFont(QFont(ui_cfg.get('font_family', 'Microsoft YaHei'), 13))

        # 导航项
        nav_items = [
            ('📊  实时仪表盘', 0),
            ('⚙  设备管理', 1),
            ('🔔  告警配置', 2),
            ('📈  历史统计', 3),
        ]
        for label, idx in nav_items:
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, idx)
            item.setSizeHint(QSize(180, 48))
            self._nav.addItem(item)

        self._nav.setCurrentRow(0)

        # === 主内容区 ===
        self._stack = QStackedWidget()
        self._stack.setObjectName('mainContent')

        # 导入页面
        self._pages = []

        try:
            from .dashboard import DashboardPage
            self._dashboard_page = DashboardPage(config, scheduler, device_repo)
            self._stack.addWidget(self._dashboard_page._widget)
            self._pages.append(self._dashboard_page)
        except Exception as e:
            logger.error(f"仪表盘页面加载失败: {e}")
            self._stack.addWidget(QLabel("仪表盘加载失败"))

        try:
            from .device_panel import DevicePanelPage
            self._device_panel = DevicePanelPage(config, device_repo, scheduler)
            self._stack.addWidget(self._device_panel._widget)
            self._pages.append(self._device_panel)
        except Exception as e:
            logger.error(f"设备管理页面加载失败: {e}")
            self._stack.addWidget(QLabel("设备管理加载失败"))

        try:
            from .alert_config_panel import AlertConfigPanel
            self._alert_config_panel = AlertConfigPanel(
                config, alert_repo,
                alert_engine=getattr(self, '_alert_engine', None),
                config_path=getattr(self, '_config_path', None),
                device_repo=device_repo,
                scheduler=scheduler,
            )
            self._stack.addWidget(self._alert_config_panel._widget)
            self._pages.append(self._alert_config_panel)
        except Exception as e:
            logger.error(f"告警配置页面加载失败: {e}")
            self._stack.addWidget(QLabel("告警配置加载失败"))

        try:
            from .history_panel import HistoryPanel
            self._history_panel = HistoryPanel(config, device_repo, history_repo, alert_repo)
            self._stack.addWidget(self._history_panel._widget)
            self._pages.append(self._history_panel)
        except Exception as e:
            logger.error(f"历史统计页面加载失败: {e}")
            self._stack.addWidget(QLabel("历史统计加载失败"))

        # 导航切换事件
        self._nav.currentRowChanged.connect(self._on_nav_changed)

        # 布局
        main_layout.addWidget(self._nav)
        main_layout.addWidget(self._stack, 1)

        # === 状态栏 ===
        self._status_bar = QStatusBar()
        self._status_bar.setObjectName('statusBar')
        self._window.setStatusBar(self._status_bar)
        self._update_status_bar()

        # 状态栏定时刷新
        self._status_timer = QTimer()
        self._status_timer.timeout.connect(self._update_status_bar)
        self._status_timer.start(2000)  # 每 2 秒刷新

        # === 系统托盘 ===
        self._tray = QSystemTrayIcon()
        if icon_path:
            self._tray.setIcon(QIcon(icon_path))
        self._tray.setToolTip("DEVICE LINK 内网设备监控")

        # 托盘菜单
        from ..watchdog.watchdog_manager import is_startup_shortcut_enabled
        tray_menu = QMenu()
        show_action = tray_menu.addAction("打开主界面")
        show_action.triggered.connect(self.show)
        pause_action = tray_menu.addAction("暂停监控")
        pause_action.setCheckable(True)
        pause_action.toggled.connect(self._on_pause_toggle)
        self._autostart_action = tray_menu.addAction("开机自启")
        self._autostart_action.setCheckable(True)
        self._autostart_action.setChecked(is_startup_shortcut_enabled())
        self._autostart_action.toggled.connect(self._on_autostart_toggled)
        tray_menu.addSeparator()
        quit_action = tray_menu.addAction("退出")
        quit_action.triggered.connect(self._on_quit)
        self._tray.setContextMenu(tray_menu)
        self._tray.activated.connect(self._on_tray_activated)
        self._tray.show()

        # 关闭按钮行为
        self._window.closeEvent = self._on_close_event

        # 启动最小化
        if config.get('app', {}).get('start_minimized', False):
            self._window.hide()
            self._tray.showMessage(
                "DEVICE LINK", "已最小化到系统托盘，双击图标打开主界面",
                QSystemTrayIcon.Information, 3000
            )

        # 应用 QSS 样式
        self._apply_stylesheet()

        logger.info("主窗口初始化完成")

    # ==================== 导航 ====================

    def _on_nav_changed(self, row: int):
        """侧边导航切换页面。"""
        if 0 <= row < self._stack.count():
            self._stack.setCurrentIndex(row)
            # 刷新目标页面（_stack.widget 返回的是 QWidget，refresh 在面板类上）
            if row < len(self._pages) and hasattr(self._pages[row], 'refresh'):
                try:
                    self._pages[row].refresh()
                except Exception as e:
                    logger.error(f"页面刷新异常: {e}")

    # ==================== 状态栏 ====================

    def _update_status_bar(self):
        """更新状态栏信息。"""
        try:
            snapshot = self._scheduler.get_snapshot()
            total = len(snapshot)
            online = sum(1 for s in snapshot.values() if s == 'online')
            offline = sum(1 for s in snapshot.values() if s == 'offline')
            pending = sum(1 for s in snapshot.values() if s == 'pending_failure')
            self._status_bar.showMessage(
                f"● 监控中 │ 在线 {online}/{total} │ 离线 {offline} │ 待定 {pending}"
            )
        except Exception:
            self._status_bar.showMessage("● 监控中")

    # ==================== 托盘 ====================

    def _on_tray_activated(self, reason):
        """托盘图标双击 → 显示主窗口。"""
        from PySide6.QtWidgets import QSystemTrayIcon
        if reason == QSystemTrayIcon.DoubleClick:
            self.show()

    def _on_pause_toggle(self, checked: bool):
        """暂停/恢复监控。"""
        if checked:
            self._scheduler.pause()
        else:
            self._scheduler.resume()

    def _on_autostart_toggled(self, checked: bool):
        """开机自启开关：创建/移除启动文件夹快捷方式。"""
        from PySide6.QtWidgets import QMessageBox
        from ..watchdog.watchdog_manager import (
            setup_startup_shortcut, remove_startup_shortcut,
        )
        try:
            if checked:
                ok = setup_startup_shortcut(sys.executable)
            else:
                ok = remove_startup_shortcut()
            if checked and not ok:
                self._autostart_action.setChecked(False)
                QMessageBox.warning(
                    self._window, "开机自启",
                    "创建开机自启快捷方式失败，请检查权限后重试。",
                )
        except Exception as e:
            self._autostart_action.setChecked(False)
            logger.error(f"开机自启设置失败: {e}")

    def _on_close_event(self, event):
        """点击 × → 最小化到托盘（不退出）。"""
        if self._config.get('app', {}).get('minimize_to_tray', True):
            self._window.hide()
            self._tray.showMessage(
                "DEVICE LINK", "已最小化到系统托盘，右键图标可退出",
                icon=self._tray.MessageIcon.Information if hasattr(self._tray, 'MessageIcon') else 0,
                msecs=3000
            )
            event.ignore()  # 不关闭窗口
        else:
            self._on_quit()

    def _on_quit(self):
        """退出应用。"""
        from PySide6.QtWidgets import QMessageBox

        # 检查是否有活动告警
        active = self._alert_repo.get_unacknowledged_offline_events()
        if active:
            reply = QMessageBox.question(
                self._window, "确认退出",
                f"当前有 {len(active)} 条未确认告警。\n退出后将停止监控，确定要退出吗？",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No
            )
            if reply != QMessageBox.Yes:
                return

        logger.info("用户请求退出")
        self._scheduler.stop()
        self._tray.hide()
        self._qt_app.quit()

    # ==================== 公共方法 ====================

    def show(self):
        """显示主窗口。"""
        self._window.show()
        self._window.raise_()
        self._window.activateWindow()

    def hide(self):
        """隐藏主窗口到托盘。"""
        self._window.hide()

    # ==================== 样式 ====================

    def _find_icon(self) -> str:
        """查找图标文件路径。"""
        candidates = [
            os.path.join(os.path.dirname(__file__), '..', '..', 'assets', 'icon.ico'),
            os.path.join(os.path.dirname(__file__), 'assets', 'icon.ico'),
        ]
        for p in candidates:
            if os.path.exists(p):
                return os.path.abspath(p)
        return ""

    def _apply_stylesheet(self):
        """应用全局 QSS 样式表。"""
        accent = self._accent_color
        qss = f"""
        /* 全局样式 */
        QMainWindow {{
            background-color: #f0f2f5;
        }}
        QWidget {{
            font-family: 'Microsoft YaHei', 'SimHei', 'Arial', sans-serif;
            font-size: 14px;
        }}

        /* 侧边导航栏 */
        #sideNav {{
            background-color: #001529;
            color: #ffffffcc;
            border: none;
            outline: none;
        }}
        #sideNav::item {{
            padding: 12px 20px;
            border-left: 3px solid transparent;
            color: #ffffffcc;
        }}
        #sideNav::item:hover {{
            background-color: #002140;
            color: #ffffff;
        }}
        #sideNav::item:selected {{
            background-color: {accent};
            color: #ffffff;
            border-left: 3px solid #00D4FF;
        }}

        /* 主内容区 */
        #mainContent {{
            background-color: #f0f2f5;
            padding: 16px;
        }}

        /* 状态栏 */
        QStatusBar {{
            background-color: #ffffff;
            border-top: 1px solid #e8e8e8;
            padding: 4px 12px;
            font-size: 12px;
            color: #666;
        }}

        /* 按钮 */
        QPushButton {{
            background-color: {accent};
            color: white;
            border: none;
            padding: 8px 16px;
            border-radius: 4px;
            font-size: 14px;
        }}
        QPushButton:hover {{
            background-color: #40a9ff;
        }}
        QPushButton:pressed {{
            background-color: #096dd9;
        }}

        /* 卡片 */
        .card {{
            background-color: white;
            border-radius: 8px;
            padding: 16px;
        }}

        /* 表格 */
        QTableWidget {{
            background-color: white;
            border: 1px solid #e8e8e8;
            border-radius: 4px;
            gridline-color: #f0f0f0;
        }}
        QHeaderView::section {{
            background-color: #fafafa;
            border: none;
            border-bottom: 1px solid #e8e8e8;
            padding: 8px;
            font-weight: bold;
        }}

        /* 滚动条 */
        QScrollBar:vertical {{
            width: 6px;
            background: transparent;
        }}
        QScrollBar::handle:vertical {{
            background: #ccc;
            border-radius: 3px;
        }}
        QScrollBar::handle:vertical:hover {{
            background: #aaa;
        }}
        """
        self._qt_app.setStyleSheet(qss)
