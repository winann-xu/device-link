"""
模块：crypto.py
功能：凭据加密/解密工具
     使用 AES-256-CBC 加密敏感配置（SMTP 密码、Webhook 密钥等）。
     密钥派生自机器特征 + 固定盐值，每台机器密文不可跨机解密。

作者：Claude
创建日期：2026-08-07
"""
import os
import hashlib
import base64
import secrets
import logging

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.backends import default_backend

logger = logging.getLogger("device-link.crypto")

# 固定盐值 —— 用于密钥派生，不变
_SALT = b'DEVICE_LINK_SALT_2026\x00\x01\x02\x03'

# AES 块大小（字节）
_BLOCK_SIZE = 16  # AES.block_size


def _derive_key() -> bytes:
    """
    从机器特征派生 AES-256 密钥。
    组合：机器 hostname + 固定盐值，SHA-256 哈希后取 32 字节作为密钥。
    同一台机器每次派生出相同的密钥，不同机器密钥不同。

    返回:
        32 字节 AES-256 密钥。
    """
    # 收集机器特征
    machine_id = os.environ.get('COMPUTERNAME', '') or os.uname().nodename
    # 混合固定盐 + 机器名
    material = _SALT + machine_id.encode('utf-8', errors='replace')
    # SHA-256 → 32 字节密钥
    return hashlib.sha256(material).digest()


def encrypt(plaintext: str) -> str:
    """
    使用 AES-256-CBC 加密明文字符串。

    参数:
        plaintext: 待加密的明文字符串

    返回:
        Base64 编码的密文（包含 IV + 密文 + PKCS7 填充）

    异常:
        Exception: 加密失败时抛出，带日志记录
    """
    try:
        key = _derive_key()
        iv = secrets.token_bytes(_BLOCK_SIZE)  # 随机 IV，每次加密不同

        # PKCS7 填充
        plain_bytes = plaintext.encode('utf-8')
        pad_len = _BLOCK_SIZE - (len(plain_bytes) % _BLOCK_SIZE)
        padded = plain_bytes + bytes([pad_len] * pad_len)

        # AES-256-CBC 加密
        cipher = Cipher(algorithms.AES(key), modes.CBC(iv), backend=default_backend())
        encryptor = cipher.encryptor()
        ciphertext = encryptor.update(padded) + encryptor.finalize()

        # IV + 密文 → Base64
        result = base64.b64encode(iv + ciphertext).decode('ascii')
        return result
    except Exception as e:
        logger.error(f"加密失败: {e}")
        raise


def decrypt(ciphertext_b64: str) -> str:
    """
    使用 AES-256-CBC 解密 Base64 编码的密文。

    参数:
        ciphertext_b64: Base64 编码的密文字符串（包含 IV）

    返回:
        解密后的明文字符串

    异常:
        ValueError: 解密失败（密钥不匹配或数据损坏）
    """
    try:
        key = _derive_key()
        raw = base64.b64decode(ciphertext_b64)

        # 前 _BLOCK_SIZE 字节是 IV
        iv = raw[:_BLOCK_SIZE]
        ciphertext = raw[_BLOCK_SIZE:]

        # AES-256-CBC 解密
        cipher = Cipher(algorithms.AES(key), modes.CBC(iv), backend=default_backend())
        decryptor = cipher.decryptor()
        padded = decryptor.update(ciphertext) + decryptor.finalize()

        # 移除 PKCS7 填充
        pad_len = padded[-1]
        if pad_len < 1 or pad_len > _BLOCK_SIZE:
            raise ValueError(f"无效的填充长度: {pad_len}")
        plain_bytes = padded[:-pad_len]

        return plain_bytes.decode('utf-8')
    except Exception as e:
        logger.error(f"解密失败: {e}")
        raise ValueError(f"解密失败: {e}")
