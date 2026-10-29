#!/usr/bin/env python3
"""
统一日志：标准库 logging，控制台带颜色，文件按大小滚动。

对外暴露与 loguru 相同的常用接口（info / warning / error / debug / exception），
业务代码无需关心底层实现。
"""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_LOG_FILE = os.path.join(BASE_DIR, "logs", "app.log")

_COLORS = {
    "DEBUG": "\033[36m",
    "INFO": "\033[32m",
    "WARNING": "\033[33m",
    "ERROR": "\033[31m",
    "CRITICAL": "\033[35m",
}
_RESET = "\033[0m"


class _ColorFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        color = _COLORS.get(record.levelname, "")
        record.levelname_c = f"{color}{record.levelname:<8}{_RESET}"
        return super().format(record)


def setup_logger(log_file: str = DEFAULT_LOG_FILE, level: str = "INFO") -> logging.Logger:
    log_dir = os.path.dirname(log_file)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)

    root = logging.getLogger("dcf")
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.handlers.clear()
    root.propagate = False

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(
        _ColorFormatter("%(asctime)s | %(levelname_c)s | %(name)s - %(message)s", "%H:%M:%S")
    )
    root.addHandler(console)

    file_handler = RotatingFileHandler(
        log_file, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s | %(levelname)-8s | %(name)s - %(message)s")
    )
    root.addHandler(file_handler)
    return root


logger = setup_logger(
    log_file=os.environ.get("LOG_FILE", DEFAULT_LOG_FILE),
    level=os.environ.get("LOG_LEVEL", "INFO"),
)
