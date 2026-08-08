"""
模块：device_panel.py
功能：设备管理面板 —— 支持 CRUD、批量操作、CSV 导入导出、测试探测

作者：Claude
创建日期：2026-08-07
"""
import logging
import csv
import os

logger = logging.getLogger("device-link.ui.device_panel")


class DevicePanelPage:
    """
    设备管理页面。
    表格展示所有设备，支持增删改、批量操作、CSV 导入导出。
    """

    def __init__(self, config: dict, device_repo, scheduler):
        """
        初始化设备管理页面。

        参数:
            config: 全局配置
            device_repo: DeviceRepository 实例
            scheduler: MonitorScheduler 实例
        """
        from PySide6.QtWidgets import (
            QWidget, QVBoxLayout, QHBoxLayout, QTableWidget, QTableWidgetItem,
            QHeaderView, QPushButton, QFileDialog, QMessageBox, QLabel, QFrame
        )
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QFont

        self._config = config
        self._device_repo = device_repo
        self._scheduler = scheduler

        self._widget = QWidget()
        layout = QVBoxLayout(self._widget)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        # === 标题 ===
        title = QLabel("⚙ 设备管理")
        title.setFont(QFont('Microsoft YaHei', 18, QFont.Bold))
        title.setStyleSheet("color: #333;")
        layout.addWidget(title)

        # === 工具栏 ===
        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)

        btn_style = """
            QPushButton {
                background: white; border: 1px solid #d9d9d9; border-radius: 4px;
                padding: 6px 14px; color: #333; font-size: 13px;
            }
            QPushButton:hover { border-color: #1890FF; color: #1890FF; }
        """

        actions = [
            ('add', '➕ 添加设备', self._on_add),
            ('edit', '✏ 编辑', self._on_edit),
            ('delete', '🗑 批量删除', self._on_delete_batch),
            ('import_csv', '📥 导入CSV', self._on_import_csv),
            ('export_csv', '📤 导出CSV', self._on_export_csv),
            ('test', '🔍 测试探测', self._on_test_probe),
            ('maintenance', '🔧 批量维护', self._on_maintenance_batch),
        ]
        self._tool_buttons = {}
        for key, label, handler in actions:
            btn = QPushButton(label)
            btn.setStyleSheet(btn_style)
            btn.clicked.connect(handler)
            toolbar.addWidget(btn)
            self._tool_buttons[key] = btn
        toolbar.addStretch()
        layout.addLayout(toolbar)

        # === 设备表格 ===
        self._table = QTableWidget()
        self._table.setColumnCount(9)
        self._table.setHorizontalHeaderLabels([
            '', '设备名', 'IP地址', '子系统', '探测方式', '间隔(s)', '状态', '最近检查', '操作'
        ])
        self._table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self._table.setColumnWidth(0, 30)   # 复选框
        self._table.setColumnWidth(8, 120)  # 操作按钮
        self._table.setSelectionBehavior(QTableWidget.SelectRows)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._table.setStyleSheet("""
            QTableWidget { background: white; border: 1px solid #e8e8e8; border-radius: 4px; }
        """)
        layout.addWidget(self._table, 1)

    def refresh(self):
        """刷新设备列表。"""
        try:
            devices = self._device_repo.list_devices()
        except Exception as e:
            logger.error(f"刷新设备列表失败: {e}")
            return

        self._table.setRowCount(0)
        self._table.setRowCount(len(devices))

        for row, dev in enumerate(devices):
            # 复选框
            from PySide6.QtWidgets import QTableWidgetItem, QCheckBox, QPushButton
            from PySide6.QtCore import Qt
            from PySide6.QtGui import QColor

            cb = QCheckBox()
            cb.setProperty('device_id', dev['id'])
            self._table.setCellWidget(row, 0, cb)

            # 设备名
            self._table.setItem(row, 1, self._item(dev.get('name', '')))
            # IP
            self._table.setItem(row, 2, self._item(dev.get('ip_address', '')))
            # 子系统
            self._table.setItem(row, 3, self._item(dev.get('subsystem_name', '')))
            # 探测方式
            self._table.setItem(row, 4, self._item(dev.get('monitor_method', 'auto')))
            # 间隔
            self._table.setItem(row, 5, self._item(str(dev.get('check_interval_seconds', 30))))
            # 状态（带颜色）
            status = dev.get('status', 'unknown')
            colors = {'online': '#52C41A', 'offline': '#FF4D4F', 'pending_failure': '#FA8C16'}
            status_item = self._item(status)
            # Bug N 修复：Qt.GlobalColor 不接受颜色字符串（'grey'/'#52C41A' 都会抛
            # ValueError），设备面板整表渲染失败。改用 QColor（支持 hex/颜色名）。
            status_item.setForeground(QColor(colors.get(status, '#8C8C8C')))
            self._table.setItem(row, 6, status_item)
            # 最近检查
            self._table.setItem(row, 7, self._item(dev.get('last_check_time', '-')))
            # 操作按钮
            edit_btn = QPushButton("编辑")
            edit_btn.setStyleSheet("background: #1890FF; color: white; padding: 2px 8px; font-size: 12px;")
            edit_btn.clicked.connect(lambda checked=False, did=dev['id']: self._on_edit_device(did))
            self._table.setCellWidget(row, 8, edit_btn)

    def _item(self, text: str):
        """创建表格单元格，居中显示。"""
        from PySide6.QtWidgets import QTableWidgetItem
        from PySide6.QtCore import Qt
        item = QTableWidgetItem(str(text))
        item.setTextAlignment(Qt.AlignCenter)
        return item

    def _selected_ids(self) -> list:
        """返回所有被勾选的设备 ID。"""
        ids = []
        for row in range(self._table.rowCount()):
            cb = self._table.cellWidget(row, 0)
            if cb and cb.isChecked():
                ids.append(cb.property('device_id'))
        return ids

    def _on_add(self):
        """添加设备对话框（简化版）。"""
        from PySide6.QtWidgets import (
            QDialog, QFormLayout, QLineEdit, QSpinBox, QComboBox,
            QDialogButtonBox, QMessageBox
        )
        dialog = QDialog(self._widget)
        dialog.setWindowTitle("添加设备")
        layout = QFormLayout(dialog)

        name_edit = QLineEdit()
        ip_edit = QLineEdit()
        subsys_edit = QLineEdit()
        method_combo = QComboBox()
        method_combo.addItems(['auto', 'ping', 'tcp'])
        port_spin = QSpinBox()
        port_spin.setRange(0, 65535)
        port_spin.setValue(0)
        interval_spin = QSpinBox()
        interval_spin.setRange(5, 3600)
        interval_spin.setValue(30)
        threshold_spin = QSpinBox()
        threshold_spin.setRange(1, 10)
        threshold_spin.setValue(3)

        layout.addRow("设备名 *:", name_edit)
        layout.addRow("IP地址 *:", ip_edit)
        layout.addRow("子系统:", subsys_edit)
        layout.addRow("探测方式:", method_combo)
        layout.addRow("TCP端口:", port_spin)
        layout.addRow("检查间隔(s):", interval_spin)
        layout.addRow("失败阈值:", threshold_spin)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addRow(buttons)

        if dialog.exec() == QDialog.Accepted:
            name = name_edit.text().strip()
            ip = ip_edit.text().strip()
            if not name or not ip:
                QMessageBox.warning(self._widget, "错误", "设备名和IP不能为空")
                return
            try:
                self._device_repo.add_device({
                    'name': name,
                    'ip_address': ip,
                    'subsystem_name': subsys_edit.text().strip(),
                    'monitor_method': method_combo.currentText(),
                    'port': port_spin.value(),
                    'check_interval_seconds': interval_spin.value(),
                    'failure_threshold': threshold_spin.value(),
                })
                self.refresh()
            except Exception as e:
                if 'UNIQUE' in str(e) or 'unique' in str(e):
                    QMessageBox.warning(self._widget, "错误", f"设备名已存在: {name}")
                else:
                    QMessageBox.critical(self._widget, "错误", f"添加失败: {e}")

    def _on_edit(self):
        """编辑选中设备。"""
        ids = self._selected_ids()
        if len(ids) != 1:
            return
        self._on_edit_device(ids[0])

    def _on_edit_device(self, device_id: int):
        """编辑指定设备。"""
        dev = self._device_repo.get_device(device_id)
        if not dev:
            return
        from PySide6.QtWidgets import (
            QDialog, QFormLayout, QLineEdit, QSpinBox, QComboBox, QDialogButtonBox, QMessageBox
        )
        dialog = QDialog(self._widget)
        dialog.setWindowTitle(f"编辑设备 - {dev.get('name', '')}")
        layout = QFormLayout(dialog)

        name_edit = QLineEdit(dev.get('name', ''))
        ip_edit = QLineEdit(dev.get('ip_address', ''))
        subsys_edit = QLineEdit(dev.get('subsystem_name', ''))
        interval_spin = QSpinBox()
        interval_spin.setRange(5, 3600)
        interval_spin.setValue(dev.get('check_interval_seconds', 30))
        threshold_spin = QSpinBox()
        threshold_spin.setRange(1, 10)
        threshold_spin.setValue(dev.get('failure_threshold', 3))

        layout.addRow("设备名:", name_edit)
        layout.addRow("IP:", ip_edit)
        layout.addRow("子系统:", subsys_edit)
        layout.addRow("检查间隔(s):", interval_spin)
        layout.addRow("失败阈值:", threshold_spin)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addRow(buttons)

        if dialog.exec() == QDialog.Accepted:
            self._device_repo.update_device(device_id, {
                'name': name_edit.text().strip(),
                'ip_address': ip_edit.text().strip(),
                'subsystem_name': subsys_edit.text().strip(),
                'check_interval_seconds': interval_spin.value(),
                'failure_threshold': threshold_spin.value(),
            })
            self.refresh()

    def _on_delete_batch(self):
        """批量删除选中设备。"""
        ids = self._selected_ids()
        if not ids:
            return
        from PySide6.QtWidgets import QMessageBox
        reply = QMessageBox.question(
            self._widget, "确认删除",
            f"确定要删除 {len(ids)} 台设备吗？\n此操作不可撤销。",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No
        )
        if reply == QMessageBox.Yes:
            count = self._device_repo.delete_devices(ids)
            QMessageBox.information(self._widget, "完成", f"已删除 {count} 台设备")
            self.refresh()

    def _on_import_csv(self):
        """导入 CSV 文件。"""
        from PySide6.QtWidgets import QFileDialog, QMessageBox
        path, _ = QFileDialog.getOpenFileName(
            self._widget, "选择 CSV 文件", "", "CSV 文件 (*.csv)"
        )
        if path:
            ok, failed = self._device_repo.import_from_csv(path)
            msg = f"✅ 成功导入 {ok} 台设备"
            if failed:
                msg += f"\n❌ 失败 {len(failed)} 条:\n" + '\n'.join(
                    f"  第{f['index']+1}行: {f['reason']}" for f in failed[:10]
                )
            QMessageBox.information(self._widget, "导入结果", msg)
            self.refresh()

    def _on_export_csv(self):
        """导出 CSV 文件。"""
        from PySide6.QtWidgets import QFileDialog, QMessageBox
        path, _ = QFileDialog.getSaveFileName(
            self._widget, "导出 CSV", "devices.csv", "CSV 文件 (*.csv)"
        )
        if path:
            count = self._device_repo.export_to_csv(path)
            QMessageBox.information(self._widget, "完成", f"已导出 {count} 台设备")

    def _on_test_probe(self):
        """测试探测选中设备。"""
        ids = self._selected_ids()
        if len(ids) != 1:
            return
        from PySide6.QtWidgets import QMessageBox
        dev = self._device_repo.get_device(ids[0])
        if not dev:
            return
        from src.probes.ping_probe import PingProbe
        result = PingProbe(dev['ip_address']).check()
        if result.success:
            QMessageBox.information(self._widget, "探测结果",
                                     f"{dev['name']} ({dev['ip_address']})\n✅ 在线 - {result.latency_ms:.1f}ms")
        else:
            QMessageBox.warning(self._widget, "探测结果",
                                 f"{dev['name']} ({dev['ip_address']})\n❌ 离线 - {result.error_detail}")

    def _on_maintenance_batch(self):
        """批量设置维护模式。"""
        ids = self._selected_ids()
        if not ids:
            return
        from PySide6.QtWidgets import QMessageBox
        reply = QMessageBox.question(
            self._widget, "维护模式",
            "设置为维护模式 (是) 还是取消维护 (否)?",
            QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel, QMessageBox.Cancel
        )
        if reply == QMessageBox.Cancel:
            return
        is_maintenance = (reply == QMessageBox.Yes)
        count = self._device_repo.set_maintenance_batch(ids, is_maintenance)
        QMessageBox.information(self._widget, "完成",
                                 f"已将 {count} 台设备{'设为' if is_maintenance else '取消'}维护模式")

    @property
    def widget(self):
        return self._widget
