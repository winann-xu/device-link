# -*- coding: utf-8 -*-
"""看门狗状态持久化回归（v1.0.7）：
- 重启计数写入状态文件，跨进程/跨重启生效
- 稳定运行超过阈值后计数复位
- 正常退出标记可写入并被读取
"""
import time

from src.watchdog.watchdog_manager import WatchdogProcess


def _state(restart_count=0, last_restart_ts=0.0, clean=False, gave_up=False):
    return {
        "restart_args": ["C:/x/DEVICE-LINK.exe"],
        "restart_count": restart_count,
        "last_restart_ts": last_restart_ts,
        "clean_shutdown": clean,
        "gave_up": gave_up,
    }


def test_restart_count_persisted_across_instances(tmp_path):
    """新看门狗实例从状态文件读到历史重启计数，达到上限后不再重启。"""
    state_file = str(tmp_path / "wd.json")
    w1 = WatchdogProcess(heartbeat_timeout=0.05, max_restarts=3, cooldown=30,
                         state_file=state_file)
    w1._write_state(_state(restart_count=3))

    w2 = WatchdogProcess(heartbeat_timeout=0.05, max_restarts=3, cooldown=30,
                         state_file=state_file)
    w2._load_state()
    assert w2._restart_count == 3
    assert w2._can_restart() is False, "达到最大重启次数应停止"


def test_reset_count_after_healthy_period(tmp_path):
    """稳定运行超过 healthy_threshold 后，重启计数复位为 0。"""
    state_file = str(tmp_path / "wd2.json")
    w = WatchdogProcess(heartbeat_timeout=0.05, max_restarts=3, cooldown=30,
                        healthy_threshold=0, state_file=state_file)
    w._write_state(_state(restart_count=2, last_restart_ts=time.time() - 10))
    assert w._reset_if_healthy() is True
    assert w._read_state()["restart_count"] == 0

    w3 = WatchdogProcess(heartbeat_timeout=0.05, max_restarts=3, cooldown=30,
                         healthy_threshold=99999, state_file=state_file)
    w3._write_state(_state(restart_count=2, last_restart_ts=time.time() - 10))
    assert w3._reset_if_healthy() is False, "未到阈值不应复位"
    assert w3._read_state()["restart_count"] == 2


def test_clean_shutdown_marker(tmp_path):
    """正常退出标记写入后可被看门狗读取，避免误重启。"""
    state_file = str(tmp_path / "wd3.json")
    w = WatchdogProcess(heartbeat_timeout=0.05, max_restarts=3, cooldown=30,
                        state_file=state_file)
    w._write_state(_state())
    w.mark_clean_shutdown()
    assert w._read_state()["clean_shutdown"] is True


def test_is_startup_shortcut_enabled(tmp_path, monkeypatch):
    """开机自启状态检测：快捷方式存在即视为已启用。"""
    import src.watchdog.watchdog_manager as wm
    lnk = str(tmp_path / "DEVICE LINK.lnk")
    monkeypatch.setattr(wm, "_startup_shortcut_path", lambda: lnk)
    assert wm.is_startup_shortcut_enabled() is False
    with open(lnk, "w", encoding="utf-8") as f:
        f.write("")
    assert wm.is_startup_shortcut_enabled() is True
