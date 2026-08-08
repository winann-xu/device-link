# -*- coding: utf-8 -*-
"""测试配置：把项目根目录加入 sys.path（相对定位，不依赖本机路径）。"""
import os
import sys

PROJECT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)
