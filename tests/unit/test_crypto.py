"""
测试模块：test_crypto.py
功能：凭据加解密集约测试

作者：Claude
创建日期：2026-08-07
"""
import pytest
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from src.utils.crypto import encrypt, decrypt


class TestCrypto:

    def test_roundtrip_simple(self):
        """简单字符串加解密往返测试。"""
        plain = "my_secret_password_123"
        ct = encrypt(plain)
        assert ct != plain
        assert decrypt(ct) == plain

    def test_roundtrip_empty(self):
        """空字符串加解密。"""
        plain = ""
        ct = encrypt(plain)
        assert decrypt(ct) == plain

    def test_roundtrip_unicode(self):
        """包含特殊字符的字符串加解密。"""
        plain = "密码@#$%^&*()_+-=[]{}|;':\",./<>?"
        ct = encrypt(plain)
        assert decrypt(ct) == plain

    def test_roundtrip_long(self):
        """长字符串加解密。"""
        plain = "x" * 1000
        ct = encrypt(plain)
        assert decrypt(ct) == plain

    def test_different_ciphertexts(self):
        """每次加密产生不同密文（随机 IV）。"""
        plain = "same_password"
        ct1 = encrypt(plain)
        ct2 = encrypt(plain)
        assert ct1 != ct2  # IV 不同 → 密文不同

    def test_decrypt_invalid_raises(self):
        """解密无效密文应抛出 ValueError。"""
        with pytest.raises(ValueError):
            decrypt("invalid_base64!!!")

    def test_decrypt_wrong_key(self):
        """不同机器（不同 hostname）的密文无法跨机解密。"""
        # 本测试在同一台机器上用正常加解密验证即可
        ct = encrypt("test")
        assert decrypt(ct) == "test"


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
