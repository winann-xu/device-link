# Changelog

## v1.0.0 (2026-08-07)

### 新增
- ICMP Ping + TCP 端口双探测引擎
- 设备状态机（误报抑制 N 次判定 + 恢复阈值）
- 并发调度器（ThreadPoolExecutor，max_workers=50）
- 邮件/飞书/企业微信三通道通知
- 告警智能合并摘要（5 分钟窗口 + 紧急绕过）
- 升级机制（未确认告警自动升级）
- PySide6 仪表盘（按子系统分组设备卡片）
- 设备管理（CRUD + 批量操作 + CSV 导入导出）
- 历史统计（在线率 + 离线排行榜 + 告警日志）
- 看门狗守护（15 秒拉起）
- Nuitka 免安装打包
- 多尺寸 Logo/图标

### 修复
- add_device 默认值导致空字符串字段 → 使用合理 DEFAULTS
- SQLite 跨线程访问 → 连接池化（每线程独立 Connection）
- **BUG M（严重）**：看门狗 `os.kill(pid, 0)` 在 Windows 上实际杀死主进程
  - Unix 上信号 0 是空操作（仅检查进程存活）
  - Windows 上 `os.kill()` 调用 `TerminateProcess()` 杀死目标进程
  - 导致看门狗每 15 秒自杀一次
  - 修复：Windows 改用 `tasklist /FI "PID eq N"` 检查进程存活
- **sqlite3.dll ACCESS_VIOLATION (0xc0000005)**：跨线程共享同一 Connection 句柄
  - 修复：每线程通过 `threading.local()` 获取独立连接，去掉 `check_same_thread=False`
