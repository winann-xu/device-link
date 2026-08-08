"""
模块：test_database.py
功能：database.py 覆盖补强 —— 路径解析、连接管理、异常处理
"""
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

import src.storage.database as dbmod
from src.storage.database import get_db_path, get_connection, close_connection, init_database


@pytest.fixture
def clean_global_path():
    with dbmod._db_path_lock:
        old = dbmod._db_path_global
        dbmod._db_path_global = None
    yield
    with dbmod._db_path_lock:
        dbmod._db_path_global = old


def test_get_db_path_default():
    """无配置时使用默认路径（相对项目根解析为绝对）。"""
    p = get_db_path()
    p = os.path.normpath(p)
    assert os.path.isabs(p)
    assert p.endswith(os.path.join('data', 'device-link.db'))


def test_get_db_path_with_config(tmp_path):
    """配置指定路径时优先使用（绝对路径）。"""
    p = str(tmp_path / 'custom.db')
    assert get_db_path({'storage': {'path': p}}) == p


def test_get_db_path_frozen(monkeypatch, tmp_path):
    """冻结模式下相对路径基于 exe 所在目录。"""
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(sys, 'executable', str(tmp_path / 'DEVICE-LINK.exe'))
    p = get_db_path({'storage': {'path': './data/x.db'}})
    p = os.path.normpath(p)
    assert p.startswith(str(tmp_path))
    assert p.endswith(os.path.join('data', 'x.db'))


def test_get_connection_with_config(clean_global_path, tmp_path):
    """get_connection 无 db_path 时从 config 取路径并建表。"""
    p = str(tmp_path / 'conn.db')
    conn = get_connection(config={'storage': {'path': p}})
    try:
        assert conn.execute("SELECT 1").fetchone()[0] == 1
    finally:
        close_connection()


def test_close_connection_idempotent(clean_global_path, tmp_path):
    """重复关闭连接不报错。"""
    get_connection(config={'storage': {'path': str(tmp_path / 'c.db')}})
    close_connection()
    close_connection()  # 第二次应为空操作


def test_init_database_invalid_raises(clean_global_path, tmp_path):
    """对不可用路径初始化应抛异常而非崩溃。"""
    bad = os.path.join(str(tmp_path), 'nonexist_dir_deep', 'sub', 'x.db')
    with pytest.raises(Exception):
        init_database(bad)


def test_init_database_creates_tables(clean_global_path, tmp_path):
    """初始化后核心表存在。"""
    p = str(tmp_path / 't.db')
    conn = init_database(p)
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert {'devices', 'status_history', 'alert_events'} <= tables
    finally:
        close_connection()
