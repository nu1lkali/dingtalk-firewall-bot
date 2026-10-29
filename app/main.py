#!/usr/bin/env python3
"""
兼容入口：保留 `python app/main.py` 的启动方式。

新版本统一由根目录 run.py 启动（Web 管理台 + 定时任务 + 钉钉机器人）。
"""

import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

if __name__ == "__main__":
    sys.path.insert(0, BASE_DIR)
    from run import main

    main()
