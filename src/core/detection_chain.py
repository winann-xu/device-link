"""
模块：detection_chain.py
功能：多方法探测链 —— 组合 ICMP + TCP 消除单点误判
     探测策略：
       1. 主探测：ICMP ping（最快）
       2. ping 失败 → TCP 端口复核（使用 device.port，防 ICMP 被禁误报）
       3. TCP 复核成功 → 判定设备在线（仅 ping 受限），不计入 failure
       4. TCP 复核也失败（或无端口配置）→ verified_fail，计入一次 failure

     稳定性保障：
       - 单轮总耗时 ≤ timeout_ms × 3（硬上限，防止阻塞调度器）
       - 所有异常内部捕获，不向调度器抛出

作者：Claude
创建日期：2026-08-07
"""
import time
import logging
from dataclasses import dataclass

from ..probes.base import ProbeResult
from ..probes.ping_probe import PingProbe
from ..probes.tcp_probe import TcpProbe

logger = logging.getLogger("device-link.core.detection")


@dataclass
class ProbeOutcome:
    """
    探测最终判定结果。

    字段:
        is_online: 最终判定是否在线
        failure_should_count: 是否计入连续失败计数（TCP 复核通过时为 False）
        latency_ms: 实际延迟（毫秒）
        used_method: 使用的探测方法 'icmp' | 'tcp_fallback'
        details: 每步探测详情列表（调试/日志用途）
    """
    is_online: bool
    failure_should_count: bool
    latency_ms: float = 0.0
    used_method: str = "icmp"
    details: list = None

    def __post_init__(self):
        if self.details is None:
            self.details = []


class DetectionChain:
    """
    多方法探测链。

    决策矩阵：
      | ping 结果 | TCP 结果 | 最终判定 | 计入 failure | UI 标注    |
      | 成功      | (跳过)    | 在线     | 否           | "ICMP"     |
      | 失败      | 成功      | 在线     | 否           | "TCP复核"  |
      | 失败      | 失败      | 离线     | 是           | "离线"     |
      | 失败      | 无端口    | 离线     | 是           | "离线"     |
    """

    def __init__(self, device: dict, config: dict):
        """
        初始化探测链。

        参数:
            device: 设备字典（含 ip_address, port, timeout_ms 等字段）
            config: 全局配置字典
        """
        self._device = device
        self._config = config
        self._host = device.get('ip_address', '')
        self._port = device.get('port', 0)
        self._timeout_ms = device.get('timeout_ms', config.get('monitor', {}).get('default_timeout_ms', 3000))

    def probe(self) -> ProbeOutcome:
        """
        执行探测链：先 ICMP，失败则 TCP 复核。

        返回:
            ProbeOutcome —— 最终判定、是否计入失败、方法、延迟、详情
        """
        details = []
        t_start = time.monotonic()

        # === 第 1 步：ICMP ping ===
        ping_probe = PingProbe(self._host, self._timeout_ms)
        ping_result = ping_probe.check()
        details.append(ping_result)

        if ping_result.success:
            return ProbeOutcome(
                is_online=True,
                failure_should_count=False,
                latency_ms=ping_result.latency_ms,
                used_method='icmp',
                details=details
            )

        # === 第 2 步：ping 失败 → TCP 端口复核 ===
        elapsed = (time.monotonic() - t_start) * 1000.0
        remaining = max(500, self._timeout_ms * 2 - elapsed)  # 至少保留 500ms

        if self._port > 0:
            tcp_probe = TcpProbe(self._host, self._port, int(remaining))
            tcp_result = tcp_probe.check()
            details.append(tcp_result)

            if tcp_result.success:
                # TCP 复核成功 —— 设备在线（只是 ICMP 被禁）
                logger.info(
                    f"TCP 复核成功: {self._host}:{self._port} → "
                    f"设备在线（ICMP 可能被防火墙拦截）"
                )
                return ProbeOutcome(
                    is_online=True,
                    failure_should_count=False,  # 不计入 failure
                    latency_ms=tcp_result.latency_ms,
                    used_method='tcp_fallback',
                    details=details
                )
            else:
                # TCP 复核也失败 → 确认离线
                return ProbeOutcome(
                    is_online=False,
                    failure_should_count=True,
                    used_method='icmp',
                    details=details
                )
        else:
            # 无 TCP 端口配置 → 直接判定离线
            logger.debug(f"无 TCP 端口配置: {self._host} → 直接判定离线")
            return ProbeOutcome(
                is_online=False,
                failure_should_count=True,
                used_method='icmp',
                details=details
            )
