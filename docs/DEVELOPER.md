# DEVICE LINK 开发者文档

> 版本: 1.0.0 | 最后更新: 2026-08-07

## 项目架构

```
device-link/
├── src/                        # 源代码
│   ├── main.py                 # 主入口（配置加载/GUI启动/看门狗/CLI模式）
│   ├── core/                   # 核心引擎
│   │   ├── detection_chain.py  # 多方法探测链（ICMP → TCP 复核）
│   │   ├── device_state_machine.py  # 设备状态机（误报抑制/N次判定）
│   │   └── monitor_scheduler.py     # 并发调度器（ThreadPoolExecutor）
│   ├── probes/                 # 探测方法
│   │   ├── base.py             # 探测基类
│   │   ├── ping_probe.py       # ICMP Ping 探测
│   │   └── tcp_probe.py        # TCP 端口探测
│   ├── alerts/                 # 告警引擎
│   │   ├── alert_engine.py     # 告警路由（摘要/立即/升级/冷却）
│   │   └── digest_engine.py    # 摘要合并引擎（时间窗口/容量上限/紧急绕过）
│   ├── notify/                 # 通知通道
│   │   ├── base_channel.py     # 通道基类
│   │   ├── email_channel.py    # 邮件通道（SMTP SSL）
│   │   ├── feishu_channel.py   # 飞书 Webhook
│   │   └── wecom_channel.py    # 企业微信 Webhook
│   ├── storage/                # 数据层
│   │   ├── database.py         # SQLite 初始化（6表 + WAL/外键/busy_timeout）
│   │   └── repositories.py     # 数据仓库（CRUD/批量/CSV/历史/告警）
│   ├── ui/                     # PySide6 GUI
│   │   ├── main_window.py      # 主窗口（侧边导航/QStackedWidget/托盘）
│   │   ├── dashboard.py        # 实时仪表盘（按子系统设备卡片）
│   │   ├── device_panel.py     # 设备管理（CRUD/批量/CSV导入）
│   │   ├── alert_config_panel.py  # 告警配置（通道管理/规则设置）
│   │   └── history_panel.py    # 历史统计（在线率/离线排行/告警日志）
│   ├── watchdog/               # 看门狗守护
│   │   └── watchdog_manager.py # 双层守护（线程健康检查 + 子进程拉起）
│   └── utils/                  # 工具
│       └── crypto.py           # AES-256-GCM 加密（通知密码存储）
├── config/
│   └── default_config.yaml     # 默认配置（首次运行自动复制）
├── assets/                     # 应用资源（图标等）
├── tests/
│   ├── unit/                   # 单元测试（54项）
│   ├── integration/            # 集成测试（12项）
│   └── perf/                   # 性能测试
├── build/
│   └── packaging.py            # 打包脚本（Nuitka + PyInstaller）
├── docs/
│   ├── user_manual.md          # 用户手册
│   └── DEVELOPER.md            # 本文档
├── requirements.txt
└── README.md
```

## 核心设计

### 探测链（DetectionChain）

```
ICMP Ping ──成功──▶ 判定在线（计入延迟）
    │
   失败
    │
    ▼
TCP 端口复核 ──成功──▶ 判定在线（不计入失败、UI标注"TCP复核"）
    │
   失败/无端口
    │
    ▼
 判定离线（计入连续失败）
```

### 状态机（DeviceStateMachine）

```
UNKNOWN ──首次成功──▶ ONLINE
ONLINE  ──单次失败──▶ PENDING_FAILURE（黄色，不告警）
ONLINE  ──N次失败───▶ OFFLINE（触发告警）
PENDING_FAILURE ──成功──▶ ONLINE（恢复，failure_count清零）
PENDING_FAILURE ──N次失败──▶ OFFLINE（触发告警）
OFFLINE ──M次成功──▶ ONLINE（触发恢复通知）
UNKNOWN ──N次失败──▶ OFFLINE（Bug D修复：原来永不告警，现已修正）
```

### 告警引擎（AlertEngine）

```
探测事件 ──▶ digest.enabled?
              │
         True │         False
              ▼              ▼
       DigestEngine    _send_immediate()
       (5min窗口合并)   (直接发送，Bug J修复)

紧急绕过：同子系统 ≥5 台离线 → 忽略窗口，立即发送
容量上限：max_events_per_digest 截断，溢出进入下一窗口（Bug C修复）
冷却机制：同设备 cooldown_seconds 内不重复告警
升级机制：未确认告警 escalation_minutes 后自动升级通知
```

## 稳定性设计

| 机制 | 实现位置 |
|------|---------|
| 误报抑制 N 次判定 | `device_state_machine.py` |
| TCP 端口复核 | `detection_chain.py` |
| 维护窗口静默 | `device_state_machine.py.enter_maintenance()` |
| 线程安全锁 | `repositories.py._DB_LOCK` + 各状态机 Lock |
| 探测超时保护 | `ping_probe.py` / `tcp_probe.py` 均设 timeout |
| 线程池有界 | `monitor_scheduler.py` max_workers=50 |
| WAL 模式 + busy_timeout | `database.py` |
| 看门狗双层守护 | `watchdog_manager.py` (线程健康检查 + 子进程拉起) |
| 配置热加载 | `main.py` watchdog 监听 config.yaml 变化 |
| 优雅降级 | 配置丢失时内置最小配置兜底 |

## 测试

```bash
# 全部测试
python -m pytest tests/ -v

# 仅单元测试
python -m pytest tests/unit/ -v

# 仅集成测试
python -m pytest tests/integration/ -v

# 覆盖率
python -m pytest tests/ --cov=src --cov-report=term
```

### 测试清单

**单元测试（54项）**: 状态机(11) / 探测(8) / 加密(7) / 摘要引擎(8) / 存储层(20)

**集成测试（12项）**: E2E闭环(2) / 误报抑制(3) / Bug回归(4) / 数据库集成(3)

## 打包

```bash
# Nuitka standalone（首选）
python build/packaging.py

# PyInstaller 备用
python build/packaging.py --pyinstaller

# 清理构建产物
python build/packaging.py --clean
```

### 便携版关键要求
- `sys.frozen` 检测：打包后 `_project_root = Path(sys.executable).parent`
- 首次运行自动复制 default_config.yaml + assets 到 exe 旁目录
- 按文件逐个补齐（不覆盖用户已有配置）
- config/data/logs 全部相对 exe，不写注册表/AppData

## Bug 修复记录（v1.0.0）

| Bug | 描述 | 修复文件 |
|-----|------|---------|
| A | add_device 默认值空字符串导致类型错误 | repositories.py |
| B | SQLite check_same_thread 跨线程访问 | database.py |
| C | 摘要引擎容量上限未实现 | digest_engine.py |
| D | UNKNOWN 状态失败计数不累计（漏报） | device_state_machine.py |
| E | UI 四页面加载失败 | main_window.py, history_panel.py |
| F | PyInstaller frozen 模式路径错误 | main.py, database.py |
| G | shutdown() timeout 参数 TypeError | monitor_scheduler.py |
| H | 摘要泵重复刷新 | alert_engine.py |
| I | 并发 execute 数据踩踏 | repositories.py (_DB_LOCK) |
| J | digest.enabled=False 时不立即发送 | alert_engine.py |
| K | notify_success 不落库 | alert_engine.py, repositories.py |
| L | ping 异常分类被 AttributeError 顶掉 | ping_probe.py |
| **M** | **看门狗 os.kill(pid,0) 在 Windows 上杀死主进程** | **watchdog_manager.py** |
| — | sqlite3.dll ACCESS_VIOLATION 跨线程共享连接句柄 | database.py + repositories.py |

## 开发环境

- Python 3.11+（Windows 10/11 x64）
- 依赖见 requirements.txt
- Linux 可运行单元/集成测试（需 Python 3.8+），GUI 仅 Windows
