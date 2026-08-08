# 🔗 DEVICE LINK

内网网络设备监控与告警系统 — 实时掌握各子系统网络设备在线状态，离线秒级告警、恢复自动通知。

## 功能

- **实时监控**：ICMP Ping + TCP 端口双探测，多方法复核消除误报
- **智能告警**：邮件/飞书/企业微信三通道通知，5分钟窗口智能合并摘要
- **美观仪表盘**：PySide6 现代 UI，按子系统分组卡片，状态呼吸灯动画
- **误报抑制**：N 次连续失败判定 + TCP 复核 + 维护窗口三保险
- **7×24 稳定**：Watchdog 看门狗 15 秒拉起，崩溃自动恢复
- **免安装便携**：解压即用，零系统污染

## 快速开始

```bash
# 开发环境
pip install -r requirements.txt
python src/main.py           # GUI 模式
python src/main.py --cli     # CLI 模式

# 打包
python build/packaging.py    # Nuitka standalone
```

## 系统要求

- Windows 10/11 (64位)
- Python 3.11+ (开发环境)

## 技术栈

Python 3.11 | PySide6 | SQLite | ping3 | Nuitka | cryptography | aiosmtpd

## 许可证

内部使用
