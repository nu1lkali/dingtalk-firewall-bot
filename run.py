#!/usr/bin/env python3
"""
动态云防火墙 · IP 放行管理平台
==============================

统一入口：启动 Web 管理后台、内置定时任务调度器与钉钉机器人长连接。

用法：
    python run.py                 # 默认读取配置中的 host/port
    python run.py --port 8080     # 临时覆盖端口
    python run.py --no-cron       # 不启动定时任务
    python run.py --no-bot        # 不启动钉钉机器人
"""

from __future__ import annotations

import argparse
import os
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)


def parse_args():
    parser = argparse.ArgumentParser(description="动态云防火墙 IP 放行管理平台")
    parser.add_argument("--host", default="", help="监听地址，默认取配置")
    parser.add_argument("--port", type=int, default=0, help="监听端口，默认取配置")
    parser.add_argument("--no-cron", action="store_true", help="不启动内置定时任务")
    parser.add_argument("--no-bot", action="store_true", help="不启动钉钉机器人")
    parser.add_argument("--debug", action="store_true", help="Flask 调试模式")
    return parser.parse_args()


def banner(host: str, port: int, cron: bool, bot: bool) -> None:
    line = "=" * 58
    print(line)
    print("  动态云防火墙 · IP 放行管理平台")
    print(line)
    print(f"  管理后台      http://{'127.0.0.1' if host in ('0.0.0.0', '') else host}:{port}")
    print(f"  内置定时任务 {'已启用' if cron else '已关闭'}")
    print(f"  钉钉机器人   {'已启用' if bot else '已关闭'}")
    print(f"  Webhook     POST /webhook/ip-change")
    print(line)


def main() -> None:
    args = parse_args()

    from app.settings import settings
    from app.web.app import create_app

    settings.load()
    host = args.host or settings.get("server.host", "0.0.0.0")
    port = args.port or int(settings.get("server.port", 5050))

    if args.no_cron:
        settings.update({"cron": {"enabled": False}})
    if args.no_bot:
        settings.update({"dingtalk": {"enabled": False}})

    app = create_app(start_services=True)
    banner(host, port, not args.no_cron, not args.no_bot)

    if args.debug:
        app.run(host=host, port=port, debug=True, use_reloader=False, threaded=True)
        return

    try:
        from waitress import serve

        serve(app, host=host, port=port, threads=12, channel_timeout=60)
    except ImportError:
        print("[WARN] 未安装 waitress，使用 Flask 内置服务器（生产环境建议 pip install waitress）")
        app.run(host=host, port=port, debug=False, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
