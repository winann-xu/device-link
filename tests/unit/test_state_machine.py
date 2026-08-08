"""
测试模块：test_state_machine.py
功能：设备状态机单元测试 —— 覆盖全路径、边界测试、线程安全

作者：Claude
创建日期：2026-08-07
"""
import pytest
import threading
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from src.core.device_state_machine import (
    DeviceStateMachine, DeviceStatus, StateTransition
)
from src.core.detection_chain import ProbeOutcome


def make_outcome(is_online: bool, should_count: bool = True, method: str = "icmp") -> ProbeOutcome:
    """快速构造 ProbeOutcome。"""
    return ProbeOutcome(
        is_online=is_online,
        failure_should_count=should_count,
        latency_ms=5.0 if is_online else 0.0,
        used_method=method,
        details=[]
    )


class TestStateMachine:
    """设备状态机测试套件。"""

    def test_unknown_to_online(self):
        """UNKNOWN → 首次探测成功 → ONLINE。"""
        device = {'id': 1, 'name': 'test', 'status': 'unknown', 'failure_threshold': 3}
        sm = DeviceStateMachine(device)
        assert sm.status == DeviceStatus.UNKNOWN

        t = sm.transition(make_outcome(True))
        assert sm.status == DeviceStatus.ONLINE
        assert t is not None
        assert t.old_status == DeviceStatus.UNKNOWN
        assert t.new_status == DeviceStatus.ONLINE

    def test_online_stays_online_on_success(self):
        """ONLINE + 探测成功 → 保持 ONLINE，无状态变更事件。"""
        device = {'id': 2, 'name': 'test2', 'status': 'online', 'failure_threshold': 3}
        sm = DeviceStateMachine(device)
        sm.transition(make_outcome(True))  # 先确保 online

        t = sm.transition(make_outcome(True))
        assert sm.status == DeviceStatus.ONLINE
        assert t is None  # 状态未变，无事件

    def test_online_to_pending_on_single_failure(self):
        """ONLINE + 单次失败 → PENDING_FAILURE（不告警）。"""
        device = {'id': 3, 'name': 'test3', 'status': 'online', 'failure_threshold': 3}
        sm = DeviceStateMachine(device)

        t = sm.transition(make_outcome(False, should_count=True))
        assert sm.status == DeviceStatus.PENDING_FAILURE
        assert t is None  # PENDING 不产生事件（不告警！）

    def test_pending_to_online_on_recovery(self):
        """PENDING_FAILURE + 成功 → ONLINE（恢复）。"""
        device = {'id': 4, 'name': 'test4', 'status': 'online', 'failure_threshold': 3}
        sm = DeviceStateMachine(device)
        sm.transition(make_outcome(False))  # → PENDING

        t = sm.transition(make_outcome(True))
        assert sm.status == DeviceStatus.ONLINE
        assert sm.failure_count == 0
        assert t.event_type is None  # 恢复但不是 device_recovered

    def test_pending_to_offline_after_n_failures(self):
        """PENDING_FAILURE + 连续失败 N 次 → OFFLINE（触发告警！）。"""
        device = {'id': 5, 'name': 'test5', 'status': 'online', 'failure_threshold': 3}
        sm = DeviceStateMachine(device)

        # 第 1 次失败 → PENDING
        sm.transition(make_outcome(False))
        assert sm.status == DeviceStatus.PENDING_FAILURE

        # 第 2 次失败 → 保持 PENDING
        t = sm.transition(make_outcome(False))
        assert sm.status == DeviceStatus.PENDING_FAILURE
        assert t is None

        # 第 3 次失败 → OFFLINE!
        t = sm.transition(make_outcome(False))
        assert sm.status == DeviceStatus.OFFLINE
        assert t.event_type == 'device_offline'

    def test_offline_to_online_after_m_successes(self):
        """OFFLINE + 连续成功 M 次 → ONLINE（恢复通知）。"""
        device = {
            'id': 6, 'name': 'test6', 'status': 'offline',
            'failure_threshold': 3, 'recovery_threshold': 2,
            'failure_count': 3
        }
        sm = DeviceStateMachine(device)
        assert sm.status == DeviceStatus.OFFLINE

        # 第 1 次成功 → 仍 OFFLINE
        t = sm.transition(make_outcome(True))
        assert sm.status == DeviceStatus.OFFLINE
        assert t is None

        # 第 2 次成功 → ONLINE
        t = sm.transition(make_outcome(True))
        assert sm.status == DeviceStatus.ONLINE
        assert t.event_type == 'device_recovered'

    def test_immediate_offline_n1(self):
        """N=1 时，首次失败立即判定离线。"""
        device = {'id': 7, 'name': 'test7', 'status': 'online', 'failure_threshold': 1}
        sm = DeviceStateMachine(device)

        t = sm.transition(make_outcome(False))
        assert sm.status == DeviceStatus.OFFLINE
        assert t.event_type == 'device_offline'

    def test_tcp_fallback_does_not_count(self):
        """TCP 复核通过（failure_should_count=False）不计入失败。"""
        device = {'id': 8, 'name': 'test8', 'status': 'online', 'failure_threshold': 3}
        sm = DeviceStateMachine(device)

        # TCP 复核通过 → 在线，不计入失败
        outcome = make_outcome(True, should_count=False, method='tcp_fallback')
        t = sm.transition(outcome)
        assert sm.status == DeviceStatus.ONLINE
        assert sm.failure_count == 0
        assert t is None

    def test_maintenance_suppress_alert(self):
        """维护模式下告警被抑制。"""
        device = {'id': 9, 'name': 'test9', 'status': 'online', 'failure_threshold': 1}
        sm = DeviceStateMachine(device)
        sm.enter_maintenance()

        t = sm.transition(make_outcome(False))
        assert sm.status == DeviceStatus.OFFLINE
        assert t.suppress_alert is True

    def test_flapping_resilience(self):
        """20 次交替成功/失败不崩溃。"""
        device = {'id': 10, 'name': 'test10', 'status': 'online', 'failure_threshold': 3, 'recovery_threshold': 2}
        sm = DeviceStateMachine(device)

        for i in range(20):
            success = (i % 2 == 0)
            outcome = make_outcome(success, should_count=not success)
            sm.transition(outcome)

        # 不崩溃就是通过

    def test_thread_safety(self):
        """5 线程同时调用 transition() 不崩溃。"""
        device = {'id': 11, 'name': 'test11', 'status': 'online', 'failure_threshold': 3}
        sm = DeviceStateMachine(device)

        errors = []

        def worker():
            for _ in range(100):
                try:
                    sm.transition(make_outcome(True))
                    sm.transition(make_outcome(False))
                except Exception as e:
                    errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0, f"线程安全测试失败: {errors}"


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
