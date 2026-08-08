"""
模块：ping_probe.py
功能：ICMP Ping 探测器
     基于 ping3 库，支持超时内自动重试和错误分类。
     提供成功率/平均延迟等统计指标。

作者：Claude
创建日期：2026-08-07
"""
import time
import logging
from typing import Optional

from .base import ProbeResult

logger = logging.getLogger("device-link.probes.ping")


class PingProbe:
    """
    ICMP Ping 探测器。
    基于 ping3 库，支持超时内自动重试和错误分类（timeout/unreachable/permission_denied）。
    内置统计指标（成功率、平均延迟），用于仪表盘展示。
    """

    def __init__(self, host: str, timeout_ms: int = 3000):
        """
        初始化 Ping 探测器。

        参数:
            host: 目标 IP 地址或主机名
            timeout_ms: 单次探测超时（毫秒），默认 3000
        """
        self._host = host
        self._timeout_ms = timeout_ms
        # 统计计数器
        self._total = 0
        self._success = 0
        self._latencies: list[float] = []  # 最近 100 次成功延迟

    def check(self) -> ProbeResult:
        """
        执行一次 ICMP ping 探测。
        内部策略：
          1. 首次 ping，超时 timeout_ms
          2. 首次失败 → 自动重试 1 次（防瞬时网络抖动导致误判，仅当首次超时时重试）
          3. 总耗时 ≤ timeout_ms × 2 + 500ms（硬上限）

        返回:
            ProbeResult —— 成功/失败、延迟、错误分类
        """
        # 延迟导入——避免无 ping3 时 import 即报错
        try:
            from ping3 import ping, errors as ping_errors
        except ImportError:
            return ProbeResult(
                success=False, error='error',
                error_detail='ping3 库未安装，请执行: pip install ping3'
            )

        timeout_sec = self._timeout_ms / 1000.0
        self._total += 1

        try:
            t0 = time.monotonic()
            # ping3.ping() 返回延迟（秒），失败返回 None 或 False
            rtt = ping(self._host, timeout=int(timeout_sec), unit='seconds')
            elapsed_ms = (time.monotonic() - t0) * 1000.0

            if rtt is not None and rtt is not False:
                # 成功
                latency_ms = rtt * 1000.0  # ping3 返回秒，转毫秒
                self._success += 1
                self._latencies.append(latency_ms)
                if len(self._latencies) > 100:
                    self._latencies.pop(0)
                logger.debug(f"Ping 成功: {self._host} → {latency_ms:.1f}ms")
                return ProbeResult(success=True, latency_ms=latency_ms)

            # 超时或其他失败 → 自动重试 1 次
            logger.debug(f"Ping 首次失败: {self._host}, 自动重试...")
            t1 = time.monotonic()
            rtt2 = ping(self._host, timeout=int(timeout_sec), unit='seconds')
            elapsed2_ms = (time.monotonic() - t1) * 1000.0

            if rtt2 is not None and rtt2 is not False:
                latency_ms = rtt2 * 1000.0
                self._success += 1
                self._latencies.append(latency_ms)
                if len(self._latencies) > 100:
                    self._latencies.pop(0)
                logger.info(f"Ping 重试成功: {self._host} → {latency_ms:.1f}ms")
                return ProbeResult(success=True, latency_ms=latency_ms)

            # 两次都失败
            return ProbeResult(
                success=False, error='timeout',
                error_detail=f'Ping {self._host} 超时（{self._timeout_ms}ms × 2 次重试）'
            )

        except OSError as e:
            err_str = str(e).lower()
            if 'permission' in err_str:
                return ProbeResult(success=False, error='permission_denied',
                                   error_detail=f'ICMP 权限不足: {e}')
            if 'unreachable' in err_str or 'no route' in err_str:
                return ProbeResult(success=False, error='no_route',
                                   error_detail=f'网络不可达: {e}')
            return ProbeResult(success=False, error='error', error_detail=str(e))
        except Exception as e:
            # Bug L 修复：ping3.errors 无 PermissionError 属性，原代码异常分类必被 AttributeError 顶掉
            if isinstance(e, PermissionError) or 'permission' in str(e).lower():
                return ProbeResult(
                    success=False, error='permission_denied',
                    error_detail='ICMP 权限不足：Windows 下请以管理员身份运行，或启用 TCP 端口复核'
                )
            logger.warning(f"Ping 异常: {self._host} -> {e}")
            return ProbeResult(success=False, error='error', error_detail=str(e))

    def get_metrics(self) -> dict:
        """
        返回探测统计指标。

        返回:
            {'success_rate': 成功率(0-1), 'avg_latency_ms': 平均延迟,
             'checks': 总探测次数, 'recent_latencies': 最近 100 次延迟列表}
        """
        avg_lat = sum(self._latencies) / len(self._latencies) if self._latencies else 0.0
        return {
            'success_rate': self._success / self._total if self._total > 0 else 0.0,
            'avg_latency_ms': round(avg_lat, 2),
            'checks': self._total,
            'recent_latencies': self._latencies.copy(),
        }
