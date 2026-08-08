"""
测试模块：test_digest.py
功能：告警合并/摘要引擎单元测试 —— 覆盖合并规则 1-6

作者：Claude
创建日期：2026-08-07
"""
import pytest
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from src.alerts.digest_engine import DigestEngine


@pytest.fixture
def engine():
    """创建默认配置的 DigestEngine。"""
    return DigestEngine({
        'notify': {
            'digest': {
                'window_seconds': 300,
                'max_events_per_digest': 50,
                'send_immediate_if_critical': True,
            }
        }
    })


class TestDigestEngine:

    def test_empty_flush_returns_none(self, engine):
        """空缓冲区 flush 返回 None。"""
        assert engine.flush() is None

    def test_add_and_flush(self, engine):
        """添加事件后 flush 返回完整批次。"""
        for i in range(5):
            engine.add_event({'device_name': f'd{i}', 'subsystem_name': 'MES'})
        batch = engine.flush()
        assert batch is not None
        assert len(batch.events) == 5
        assert engine.get_pending_count() == 0

    def test_capacity_cap(self, engine):
        """超过 max_events_per_digest 时截断，剩余保留。"""
        capped = DigestEngine({
            'notify': {'digest': {'max_events_per_digest': 3, 'window_seconds': 300}}
        })
        for i in range(5):
            capped.add_event({'device_name': f'd{i}'})
        batch = capped.flush()
        assert len(batch.events) == 3  # 截断到 max=3
        assert capped.get_pending_count() == 2  # 剩余 2 条保留

    def test_should_flush_on_capacity(self, engine):
        """容量满时 should_flush 返回 True。"""
        small = DigestEngine({
            'notify': {'digest': {'max_events_per_digest': 5, 'window_seconds': 999}}
        })
        for i in range(5):
            small.add_event({'device_name': f'd{i}'})
        assert small.should_flush() is True

    def test_critical_bypass(self, engine):
        """同子系统 ≥5 台离线 → 紧急绕过立即发送。"""
        engine._critical_threshold = 5
        digest_id = None
        for i in range(6):
            did = engine.add_event({
                'device_name': f'd{i}',
                'subsystem_name': 'MES',
                'subsystem': 'MES'
            })
            if did:
                digest_id = did
        assert digest_id is not None  # 应该触发紧急

    def test_flush_clears_buffer(self, engine):
        """flush 后缓冲区清空。"""
        engine.add_event({'device_name': 'd1'})
        engine.flush()
        assert engine.get_pending_count() == 0

    def test_multiple_flushes_independent(self, engine):
        """多次 flush 互不影响。"""
        engine.add_event({'device_name': 'd1'})
        b1 = engine.flush()
        assert len(b1.events) == 1

        engine.add_event({'device_name': 'd2'})
        b2 = engine.flush()
        assert len(b2.events) == 1

    def test_buffer_overflow_protection(self, engine):
        """缓冲区超过 200 条时丢弃最早事件。"""
        engine._max_buffer = 10
        for i in range(15):
            engine.add_event({'device_name': f'd{i}'})
        # 缓冲区最多 10 条
        assert engine.get_pending_count() == 10


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
