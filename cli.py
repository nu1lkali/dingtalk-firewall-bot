#!/usr/bin/env python3
"""
动态云防火墙 · 命令行工具

给运维脚本 / CI / 临时排障用：不需要浏览器，也不依赖第三方库（纯 urllib）。

用法示例：
    export DCF_SERVER=http://127.0.0.1:5050
    export DCF_TOKEN=<系统设置里的读写 Token>

    python cli.py status
    python cli.py list --status current --limit 20
    python cli.py allow 203.0.113.42 --ports 22 --ttl 120 --tag 家里
    python cli.py allow 203.0.113.42 --ports 80,443 --instance ins-xxx
    python cli.py renew 203.0.113.42 --minutes 1440
    python cli.py deny 203.0.113.42 --ports 22
    python cli.py diff
    python cli.py tags

只读 Token 也能跑 list / status / diff / tags，写操作会被服务端拒绝。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

DEFAULT_SERVER = os.environ.get("DCF_SERVER", "http://127.0.0.1:5050")
DEFAULT_TOKEN = os.environ.get("DCF_TOKEN", "")


# --------------------------------------------------------------------------- HTTP
class ApiError(Exception):
    pass


class Client:
    def __init__(self, server: str, token: str, timeout: int = 30) -> None:
        self.server = server.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _request(self, method: str, path: str, body: Optional[Dict[str, Any]] = None) -> Any:
        url = self.server + path
        data = None
        headers = {"Accept": "application/json"}
        if self.token:
            headers["X-Api-Token"] = self.token
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
                status = resp.status
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            status = exc.code
        except urllib.error.URLError as exc:
            raise ApiError(f"无法连接 {self.server}: {exc.reason}") from exc

        try:
            payload = json.loads(raw) if raw.strip() else {}
        except ValueError:
            raise ApiError(f"服务端返回了非 JSON 内容（HTTP {status}）：{raw[:200]}")

        if status >= 400 or payload.get("ok") is False:
            message = payload.get("message") or f"HTTP {status}"
            raise ApiError(message)
        return payload.get("data", payload)

    def get(self, path: str) -> Any:
        return self._request("GET", path)

    def post(self, path: str, body: Optional[Dict[str, Any]] = None) -> Any:
        return self._request("POST", path, body or {})

    def put(self, path: str, body: Optional[Dict[str, Any]] = None) -> Any:
        return self._request("PUT", path, body or {})

    def delete(self, path: str) -> Any:
        return self._request("DELETE", path)


# --------------------------------------------------------------------------- 输出
def _fmt_rule(row: Dict[str, Any]) -> str:
    port = row.get("port_display") or str(row.get("port") or "").replace("/", "-")
    if port in ("1-65535", "ALL"):
        port = "全部端口"
    tag = row.get("tag") or "-"
    expire = row.get("expires_at") or "长期"
    return "{:<16} {:<14} {:<6} {:<18} {:<10} {}".format(
        row.get("ip", ""),
        port,
        (row.get("protocol") or "tcp").upper(),
        (row.get("instance_name") or row.get("instance_id") or "-")[:18],
        tag[:10],
        expire,
    )


def print_rules(rows: List[Dict[str, Any]]) -> None:
    if not rows:
        print("（没有匹配的规则）")
        return
    head = "{:<16} {:<14} {:<6} {:<18} {:<10} {}".format("IP", "端口", "协议", "实例", "标签", "到期时间")
    print(head)
    print("-" * len(head))
    for row in rows:
        print(_fmt_rule(row))


def require_token(args) -> Client:
    if not args.token:
        print("缺少 Token：请用 --token 指定，或设置环境变量 DCF_TOKEN", file=sys.stderr)
        raise SystemExit(2)
    return Client(args.server, args.token, timeout=args.timeout)


def find_rules(client: Client, ip: str, ports: Optional[str] = None) -> List[Dict[str, Any]]:
    data = client.get("/api/rules?" + urllib.parse.urlencode({"keyword": ip, "status": "current", "page_size": 200}))
    rows = [r for r in (data.get("items") or []) if r.get("ip") == ip]
    if ports:
        wanted = {p.strip().replace("-", "/") for p in ports.split(",") if p.strip()}
        rows = [r for r in rows if str(r.get("port", "")).replace("-", "/") in wanted]
    return rows


def instance_ids(client: Client, explicit: Optional[str]) -> List[str]:
    if explicit:
        return [x.strip() for x in explicit.split(",") if x.strip()]
    cfg = client.get("/api/settings") or {}
    items = ((cfg.get("aliyun") or {}).get("instances") or [])
    return [i["instance_id"] for i in items if i.get("instance_id") and i.get("enabled", True)]


# --------------------------------------------------------------------------- 子命令
def cmd_status(client: Client, args) -> int:
    data = client.get("/api/overview")
    stats = data.get("stats") or {}
    cron = data.get("cron") or {}
    bot = data.get("dingtalk") or {}
    print("生效规则     {}".format(stats.get("active_rules", 0)))
    print("涉及 IP      {}".format(stats.get("unique_ips", 0)))
    print("即将过期     {}".format(stats.get("expiring_soon", 0)))
    print("纳管实例     {}".format(stats.get("instances", 0)))
    print("定时任务     {}".format("运行中" if cron.get("running") else "未运行"))
    print("钉钉机器人   {}".format("已连接" if bot.get("connected") else (bot.get("last_error") or "未连接")))
    print("服务器时间   {}".format(data.get("server_time", "")))
    for warn in data.get("security_warnings") or []:
        print("! {} {}".format(warn.get("level", ""), warn.get("message", "")))
    return 0


def cmd_list(client: Client, args) -> int:
    params = {"status": args.status, "page_size": min(200, args.limit)}
    if args.tag:
        params["tag"] = args.tag
    if args.keyword:
        params["keyword"] = args.keyword
    data = client.get("/api/rules?" + urllib.parse.urlencode(params))
    rows = data.get("items") or []
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    print_rules(rows)
    print("\n共 {} 条（当前显示 {} 条）".format(data.get("total", len(rows)), len(rows)))
    return 0


def cmd_allow(client: Client, args) -> int:
    ids = instance_ids(client, args.instance)
    if not ids:
        print("没有可用实例：请先在管理台「系统设置 → 纳管实例」里添加", file=sys.stderr)
        return 3
    body = {
        "instance_ids": ids,
        "ip": args.ip,
        "ports": args.ports,
        "protocol": args.protocol,
        "ttl_minutes": args.ttl,
        "tag": args.tag or "",
        "description": args.desc or "cli",
    }
    data = client.post("/api/rules", body)
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        summary = (data or {}).get("summary") or {}
        print("已提交：新增 {} 条 · 已存在 {} 条 · 失败 {} 条".format(
            summary.get("created", 0), summary.get("skipped", 0), summary.get("failed", 0)))
        for row in (data or {}).get("results") or []:
            print("  {:<6} {} {} {}".format(
                row.get("status", ""), row.get("instance_name") or row.get("instance_id"),
                row.get("ip", ""), row.get("message", "")))
    return 0


def cmd_deny(client: Client, args) -> int:
    rows = find_rules(client, args.ip, args.ports)
    if not rows:
        print("没有找到 {} 的生效规则".format(args.ip))
        return 0
    failed = 0
    for row in rows:
        try:
            client.delete("/api/rules/{}".format(row["id"]))
            print("已删除 {} : {}".format(row["ip"], row.get("port_display") or row.get("port")))
        except ApiError as exc:
            failed += 1
            print("删除失败 {} : {} —— {}".format(row["ip"], row.get("port"), exc), file=sys.stderr)
    return 1 if failed else 0


def cmd_renew(client: Client, args) -> int:
    rows = find_rules(client, args.ip, args.ports)
    if not rows:
        print("没有找到 {} 的生效规则".format(args.ip))
        return 0
    failed = 0
    for row in rows:
        try:
            client.post("/api/rules/{}/renew".format(row["id"]), {"minutes": args.minutes})
            print("已续期 {} : {}（+{} 分钟）".format(row["ip"], row.get("port_display") or row.get("port"), args.minutes))
        except ApiError as exc:
            failed += 1
            print("续期失败 {} : {} —— {}".format(row["ip"], row.get("port"), exc), file=sys.stderr)
    return 1 if failed else 0


def cmd_tags(client: Client, args) -> int:
    rows = client.get("/api/tags") or []
    if not rows:
        print("（还没有使用任何标签）")
        return 0
    for row in rows:
        print("{:<16} {} 条".format(row.get("tag", ""), row.get("total", 0)))
    return 0


def cmd_diff(client: Client, args) -> int:
    data = client.get("/api/cloud/diff") if not args.instance else client.get(
        "/api/cloud/diff?instance_id=" + urllib.parse.quote(args.instance))
    items = data.get("instances") or ([data] if data.get("instance_id") else [])
    total = 0
    for inst in items:
        only_local = inst.get("only_local") or []
        only_cloud = inst.get("only_cloud") or []
        changed = inst.get("changed") or []
        total += len(only_local) + len(only_cloud) + len(changed)
        name = inst.get("instance_name") or inst.get("instance_id")
        if inst.get("healthy"):
            print("{}  一致（本地 {} / 云端 {}）".format(name, inst.get("local_total", 0), inst.get("cloud_total", 0)))
            continue
        print("{}  本地 {} / 云端 {} —— 漂移 {} · 外部 {} · 变更 {}".format(
            name, inst.get("local_total", 0), inst.get("cloud_total", 0),
            len(only_local), len(only_cloud), len(changed)))
        for row in only_local:
            print("  [本地有] {} : {}".format(row.get("ip"), row.get("port")))
        for row in only_cloud:
            print("  [云端有] {} : {}".format(row.get("ip") or row.get("cidr"), row.get("port")))
        for row in changed:
            print("  [不一致] 本地 {} : {} → 云端 {} : {}".format(
                row["local"].get("ip"), row["local"].get("port"),
                row["cloud"].get("ip"), row["cloud"].get("port")))
    for err in data.get("errors") or []:
        print("! {}: {}".format(err.get("instance_id"), err.get("message")), file=sys.stderr)
    return 0 if total == 0 else 4


# --------------------------------------------------------------------------- 入口
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dcf",
        description="动态云防火墙命令行工具（走 API Token，不依赖浏览器）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="环境变量：DCF_SERVER（默认 http://127.0.0.1:5050）、DCF_TOKEN",
    )
    parser.add_argument("--server", default=DEFAULT_SERVER, help="管理台地址")
    parser.add_argument("--token", default=DEFAULT_TOKEN, help="API Token（读写或只读）")
    parser.add_argument("--timeout", type=int, default=30, help="请求超时秒数")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出（便于脚本消费）")

    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("status", help="查看系统概览")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("list", help="列出放行规则")
    p.add_argument("--status", default="current", choices=["current", "active", "disabled", "gone", "all"])
    p.add_argument("--tag", help="按标签筛选")
    p.add_argument("--keyword", help="按 IP / 备注 / 端口搜索")
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("allow", help="放行一个 IP")
    p.add_argument("ip")
    p.add_argument("--ports", default="ALL", help="ALL / 22 / 80,443 / 8000-9000")
    p.add_argument("--protocol", default="tcp")
    p.add_argument("--ttl", type=int, default=0, help="有效期分钟，0 表示长期")
    p.add_argument("--tag", help="标签")
    p.add_argument("--instance", help="指定实例 ID（逗号分隔），默认下发到全部启用实例")
    p.add_argument("--desc", help="备注")
    p.set_defaults(func=cmd_allow)

    p = sub.add_parser("deny", help="撤销放行")
    p.add_argument("ip")
    p.add_argument("--ports", help="只删指定端口")
    p.set_defaults(func=cmd_deny)

    p = sub.add_parser("renew", help="续期")
    p.add_argument("ip")
    p.add_argument("--minutes", type=int, default=1440)
    p.add_argument("--ports", help="只续指定端口")
    p.set_defaults(func=cmd_renew)

    p = sub.add_parser("tags", help="查看标签统计")
    p.set_defaults(func=cmd_tags)

    p = sub.add_parser("diff", help="比对本地镜像与云端规则")
    p.add_argument("--instance", help="只比对一个实例")
    p.set_defaults(func=cmd_diff)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0

    client = require_token(args)
    try:
        return args.func(client, args)
    except ApiError as exc:
        print("请求失败：{}".format(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
