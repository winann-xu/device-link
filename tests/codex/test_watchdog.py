# -*- coding: utf-8 -*-
"""看门狗单元测试：健康检查超时退出、重启计数与冷却。"""
import time
from datetime import datetime, timedelta

import pytest

from src.watchdog.watchdog_manager import HealthCheckThread, WatchdogProcess


class FakeScheduler:
    def __init__(self, last_tick_iso):
        self._health = {"last_tick": last_tick_iso}

    def get_health(self):
        return self._health


def test_health_check_stale_tick_triggers_exit(monkeypatch):
    exited = []

    def fake_exit(code):
        exited.append(code)
        raise SystemExit(code)  # 终止健康检查线程，避免真实退出测试进程

    monkeypatch.setattr("src.watchdog.watchdog_manager.os._exit", fake_exit)
    stale = (datetime.now() - timedelta(seconds=60)).isoformat()
    t = HealthCheckThread(FakeScheduler(stale), heartbeat_interval=0.05, heartbeat_timeout=5)
    t.start()
    deadline = time.time() + 3
    while time.time() < deadline and not exited:
        time.sleep(0.05)
    assert exited == [1], "调度器心跳超时应触发 os._exit(1)"


def test_health_check_fresh_tick_no_exit(monkeypatch):
    exited = []

    def fake_exit(code):
        exited.append(code)
        raise SystemExit(code)

    monkeypatch.setattr("src.watchdog.watchdog_manager.os._exit", fake_exit)
    fresh = datetime.now().isoformat()
    t = HealthCheckThread(FakeScheduler(fresh), heartbeat_interval=0.05, heartbeat_timeout=5)
    t.start()
    time.sleep(0.4)
    t.stop()
    assert exited == [], "心跳正常时不应退出"


def test_watchdog_restart_limits():
    w = WatchdogProcess(heartbeat_timeout=0.05, max_restarts=2, cooldown=60)
    assert w._can_restart() is True
    w._restart_count = 1
    assert w._can_restart() is True
    w._restart_count = 2
    assert w._can_restart() is False, "达到最大重启次数应停止"
    # 冷却期内不允许重启
    w2 = WatchdogProcess(heartbeat_timeout=0.05, max_restarts=5, cooldown=60)
    w2._last_restart_time = time.time()
    assert w2._can_restart() is False, "冷却期内不应重启"
