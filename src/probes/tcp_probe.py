"""
模块：tcp_probe.py
功能：TCP 端口探测器
     通过原生 socket 连接验证目标端口是否可达。
     核心用途：当 ICMP ping 被防火墙禁用时，作为复核手段判断设备是否存活。

连接拒绝(ConnectionRefusedError) ≠ 设备离线，说明 TCP 栈正常，设备存活。

作者：Claude
创建日期：2026-08-07
"""
import socket
import time
import logging
from typing import Optional

from .base import ProbeResult

logger = logging.getLogger("device-link.probes.tcp")


class TcpProbe:
    """
    TCP 端口探测器。
    通过 socket.create_connection() 验证目标端口可达性。
    零外部依赖（仅用 Python 标准库 socket）。
    """

    def __init__(self, host: str, port: int, timeout_ms: int = 3000):
        """
        初始化 TCP 探测器。

        参数:
            host: 目标 IP 地址
            port: 目标端口号
            timeout_ms: 连接超时（毫秒），默认 3000
        """
        self._host = host
        self._port = port if port > 0 else 80  # 默认 80（未配置端口时）
        self._timeout_ms = timeout_ms

    def check(self) -> ProbeResult:
        """
        执行一次 TCP 端口探测。

        判断逻辑：
          - socket 连接成功 → success=True, latency_ms=握手耗时
          - ConnectionRefusedError → success=True（设备在线！端口未监听而已）
            这是关键：连接拒绝说明目标 IP 的 TCP 栈响应了 RST，
            证明设备存活，只是该端口没有服务监听。
          - socket.timeout → success=False, error='timeout'（设备可能离线）
          - OSError → success=False, error='no_route'（网络不可达）

        返回:
            ProbeResult 结构
        """
        timeout_sec = self._timeout_ms / 1000.0
        try:
            t0 = time.monotonic()
            sock = socket.create_connection(
                (self._host, self._port), timeout=timeout_sec
            )
            elapsed_ms = (time.monotonic() - t0) * 1000.0
            sock.close()
            logger.debug(f"TCP 连接成功: {self._host}:{self._port} → {elapsed_ms:.1f}ms")
            return ProbeResult(
                success=True, latency_ms=elapsed_ms, method='tcp'
            )

        except ConnectionRefusedError:
            # 设备在线！端口拒绝连接 = TCP 栈正常，设备存活
            elapsed_ms = (time.monotonic() - t0) * 1000.0
            logger.debug(f"TCP 连接拒绝（设备在线）: {self._host}:{self._port}")
            return ProbeResult(
                success=True, latency_ms=elapsed_ms, method='tcp',
                error_detail='connection_refused'
            )

        except socket.timeout:
            logger.debug(f"TCP 超时: {self._host}:{self._port}")
            return ProbeResult(
                success=False, error='timeout', method='tcp',
                error_detail=f'TCP 连接超时（{self._timeout_ms}ms）'
            )

        except OSError as e:
            logger.debug(f"TCP 网络不可达: {self._host}:{self._port} -> {e}")
            return ProbeResult(
                success=False, error='no_route', method='tcp',
                error_detail=str(e)
            )

        except Exception as e:
            logger.warning(f"TCP 探测异常: {self._host}:{self._port} -> {e}")
            return ProbeResult(
                success=False, error='error', method='tcp',
                error_detail=str(e)
            )
