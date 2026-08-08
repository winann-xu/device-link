"""
模块：base.py
功能：探测器基类 —— 定义统一的探测结果数据结构
     所有探测器（ICMP/TCP）均返回 ProbeResult，确保上层调度器不关心具体实现。

作者：Claude
创建日期：2026-08-07
"""
from dataclasses import dataclass


@dataclass
class ProbeResult:
    """
    探测结果数据类，所有探测器统一返回此结构。

    字段:
        success: 探测是否成功（设备可达）
        latency_ms: 成功时的往返延迟（毫秒），失败为 0.0
        error: 失败原因分类 —— timeout | unreachable | no_route | permission_denied | error
        error_detail: 详细错误信息（日志用，不展示给用户）
        method: 探测方法标识 —— icmp | tcp
    """
    success: bool
    latency_ms: float = 0.0
    error: str = ""
    error_detail: str = ""
    method: str = "icmp"
