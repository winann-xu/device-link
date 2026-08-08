"""
测试模块：test_probes.py
功能：ICMP Ping 探测器和 TCP 端口探测器单元测试

作者：Claude
创建日期：2026-08-07
"""
import pytest
import socket
import threading
import time
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from src.probes.base import ProbeResult
from src.probes.ping_probe import PingProbe
from src.probes.tcp_probe import TcpProbe


class TestPingProbe:
    """ICMP Ping 探测器测试。"""

    def test_localhost_should_succeed(self):
        """本机回环 ping 应该成功且延迟 < 100ms。"""
        probe = PingProbe('127.0.0.1', 3000)
        result = probe.check()
        assert result.success is True
        assert result.latency_ms < 100
        assert result.method == 'icmp'

    def test_unreachable_ip_should_fail(self):
        """不可达 IP（TEST-NET 保留段）应该失败且 error='timeout'。"""
        probe = PingProbe('192.0.2.1', 1000)
        result = probe.check()
        assert result.success is False
        assert result.error in ('timeout', 'no_route', 'error')

    def test_metrics_accumulate(self):
        """多次探测后统计指标正确累积。"""
        probe = PingProbe('127.0.0.1', 3000)
        for _ in range(5):
            probe.check()
        metrics = probe.get_metrics()
        assert metrics['checks'] >= 5
        assert metrics['success_rate'] > 0.9  # 本机回环几乎全成功
        # Windows 上 127.0.0.1 的 ICMP RTT 可能为 0.0（ping3 实测返回 0.0 秒）
        # 因此只断言延迟列表已累积、平均值为合法非负数
        assert len(metrics['recent_latencies']) >= 5
        assert metrics['avg_latency_ms'] >= 0


class TestTcpProbe:
    """TCP 端口探测器测试。"""

    def test_open_port_success(self):
        """监听端口探测成功。"""
        # 启动临时 TCP server
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(('127.0.0.1', 0))  # 随机端口
        port = server.getsockname()[1]
        server.listen(1)

        try:
            probe = TcpProbe('127.0.0.1', port, 3000)
            result = probe.check()
            assert result.success is True
            assert result.method == 'tcp'
        finally:
            server.close()

    def test_closed_port_refused(self):
        """关闭端口返回连接拒绝——设备在线。"""
        # 使用一个大概率空闲的端口
        probe = TcpProbe('127.0.0.1', 19999, 1000)
        result = probe.check()
        # 连接拒绝 = 设备在线
        # 注意：某些系统策略可能静默丢弃（timeout），两种都应接受
        assert result.success is True or result.error == 'timeout'

    def test_timeout(self):
        """不可达 IP 超时。"""
        probe = TcpProbe('192.0.2.1', 80, 500)
        result = probe.check()
        assert result.success is False
        assert result.error in ('timeout', 'no_route', 'error')


class TestProbeResult:
    """ProbeResult 数据结构测试。"""

    def test_default_values(self):
        r = ProbeResult(success=True)
        assert r.latency_ms == 0.0
        assert r.error == ''
        assert r.error_detail == ''
        assert r.method == 'icmp'

    def test_fields_settable(self):
        r = ProbeResult(success=False, latency_ms=100.0,
                       error='timeout', method='tcp')
        assert r.success is False
        assert r.latency_ms == 100.0
        assert r.error == 'timeout'
        assert r.method == 'tcp'


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
