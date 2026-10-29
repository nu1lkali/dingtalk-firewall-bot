#!/usr/bin/env python3
"""
本地自检脚本：不触碰云端，仅验证 Web 接口、配置迁移、指令解析与调度器装配。

    python scripts/smoke_test.py
"""

from __future__ import annotations

import os
import sys
import tempfile

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

PASS, FAIL = [], []

# --------------------------------------------------------------------------- 隔离
# 历史教训：本脚本曾经直接跑在 data/app.db 上，而它包含「强制改密」用例，
# 结果把真实管理员口令改成了 Smoke@2026pw。以下三步保证它永远碰不到生产数据：
#   1) 库指向临时目录（必须在 import app.* 之前设置，Store 在模块导入时就建连接）
#   2) 旧配置也写一份假的到临时目录，既验证迁移逻辑，又不读真实 AK
#   3) 启动时硬校验，万一库仍是 data/app.db 就直接退出
_TMP = tempfile.mkdtemp(prefix="dcf-smoke-")
os.environ["APP_DB_PATH"] = os.path.join(_TMP, "smoke.db")

_FAKE_YAML = os.path.join(_TMP, "config.yaml")
with open(_FAKE_YAML, "w", encoding="utf-8") as _f:
    _f.write(
        "webhook:\n"
        "  port: 5050\n"
        '  secret_key: ""\n'
        "cloud_providers:\n"
        "  aliyun_lighthouse:\n"
        "    enabled: true\n"
        '    access_key_id: "dummy-ak-id"\n'
        '    access_key_secret: "dummy-ak-secret"\n'
        '    region: "cn-hangzhou"\n'
        "    instances:\n"
        '      - id: "i-smoke-0001"\n'
        '        description: "冒烟测试实例"\n'
        "rules:\n"
        "  auto_cleanup_old_ip: true\n"
        "  cleanup_days: 7\n"
    )


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASS.append(name)
        print(f"  [PASS] {name}")
    else:
        FAIL.append(f"{name} {detail}")
        print(f"  [FAIL] {name} {detail}")


def main() -> int:
    print("=" * 62)
    print("动态云防火墙 · 本地自检")
    print("=" * 62)

    print("\n[1] 配置中心与旧配置迁移")
    import app.settings as settings_mod
    from app.settings import settings

    # 断掉真实配置来源：旧 yaml 与 settings.json 都指向临时副本
    settings_mod.CONFIG_YAML = _FAKE_YAML
    settings_mod.CONFIG_JSON = os.path.join(_TMP, "settings.json")

    from app.store import store as _store

    _real_db = os.path.abspath(os.path.join(BASE_DIR, "data", "app.db"))
    if os.path.abspath(_store.db_path) == _real_db:
        print("  [FATAL] 自检脚本指向了真实数据库，已中止（会改掉生产口令）")
        return 2

    data = settings.load()
    check("配置可加载", isinstance(data, dict) and "dingtalk" in data)
    check(
        "旧 config.yaml 中的实例已迁移",
        any(i.get("instance_id") for i in data.get("aliyun", {}).get("instances", [])),
        str(data.get("aliyun", {}).get("instances")),
    )
    check("服务端密钥已生成", bool(data["server"].get("secret_key")))
    check("API Token 已生成", bool(data["server"].get("api_token")))

    print("\n[2] 指令解析")
    from app.dingtalk import parser

    cases = [
        ("1.2.3.4", ("allow", ["1.2.3.4"], "")),
        ("1.2.3.4 22", ("allow", ["1.2.3.4"], "22")),
        ("1.2.3.4:22", ("allow", ["1.2.3.4"], "22")),
        ("1.2.3.4 22,80,443", ("allow", ["1.2.3.4"], "22,80,443")),
        ("1.2.3.4 8000-9000", ("allow", ["1.2.3.4"], "8000-9000")),
        ("放行 1.2.3.4 53 udp", ("allow", ["1.2.3.4"], "53")),
        ("1.2.3.4 1.2.3.5 22", ("allow", ["1.2.3.4", "1.2.3.5"], "22")),
        ("删除 1.2.3.4", ("delete", ["1.2.3.4"], "")),
        ("列表", ("list", [], "")),
        ("状态", ("status", [], "")),
        ("帮助", ("help", [], "")),
    ]
    for text, (intent, ips, ports) in cases:
        got = parser.parse(text)
        check(
            f"解析 `{text}`",
            got["intent"] == intent and got["ips"] == ips and got["ports"] == ports,
            f"-> {got}",
        )
    check(
        "UDP 协议识别",
        parser.parse("1.2.3.4 53 udp")["protocol"] == "udp",
    )
    check("非法输入不误判", parser.parse("你好呀")["intent"] == "unknown")

    print("\n[3] 端口表达式归一化")
    from app.cloud.swas import ALL_PORTS, protocol_normalize, split_ports

    # 阿里云 SWAS 的 Port 只接受 start/end，传 ALL 会报 InvalidPort.ValueNotSupported
    check('"ALL" -> ["1/65535"]', split_ports("ALL") == [ALL_PORTS])
    check('"" -> ["1/65535"]', split_ports("") == [ALL_PORTS])
    check('"22,80" -> ["22","80"]', split_ports("22,80") == ["22", "80"])
    check('"8000-9000" -> ["8000/9000"]', split_ports("8000-9000") == ["8000/9000"])
    check('协议 ALL -> TCP+UDP', protocol_normalize("ALL") == "TCP+UDP")
    check('协议 tcp+udp 保留', protocol_normalize("tcp+udp") == "TCP+UDP")

    print("\n[3.0] 规则状态与删除幂等")
    from app.core.rules_service import GONE_STATUSES, LIVE_STATUSES, _is_not_found

    check("在用状态 = 生效中 + 已禁用", LIVE_STATUSES == ["active", "disabled"])
    check("终态集合完整", GONE_STATUSES == {"deleted", "expired", "stale"})
    check(
        "识别阿里云 NotFound",
        _is_not_found("InvalidRuleId.NotFound code: 404, The specified rule does not exist."),
    )
    check("正常错误不误判", not _is_not_found("InvalidPort.ValueNotSupported"))

    print("\n[3.1] cron 表达式解析")
    from app.cron.cronexpr import describe, next_runs, summarize, validate

    check("每天 03:00 释义", describe("0 3 * * *") == "每天 03:00 执行", describe("0 3 * * *"))
    check(
        "每周一~周五 释义（标准 cron 语义）",
        describe("0 9 * * 1-5") == "每周 周一、周二、周三、周四、周五 的 09:00 执行",
        describe("0 9 * * 1-5"),
    )
    check("每周日 释义", describe("0 9 * * 0") == "每周 周日 的 09:00 执行", describe("0 9 * * 0"))
    check("每 5 分钟释义", describe("*/5 * * * *") == "每 5 分钟执行一次")
    check("非法表达式被拒", validate("60 * * * *")[0] is False)
    check("非法表达式给出提示", "不合法" in describe("bad") or "不合法" in describe("bad expr"))
    runs = next_runs("0 9 * * 1-5", 3)
    check("未来执行时间可预测", len(runs) == 3 and all("09:00:00" in r for r in runs), str(runs))
    check("summarize 结构完整", set(summarize("0 3 * * *").keys()) == {"expr", "valid", "error", "description", "next_runs"})

    print("\n[4] 定时任务装配")
    from app.cron.scheduler import scheduler_service
    from app.cron.jobs import JOB_FUNCS

    check("任务函数齐全", len(JOB_FUNCS) >= 6, str(list(JOB_FUNCS)))
    scheduler_service.start()
    status = scheduler_service.status()
    check("调度器已启动", status["running"] is True)
    check("任务已注册", len([j for j in status["jobs"] if j["scheduled"]]) >= 6)
    check("每个任务都有下次执行时间", all(j["next_run"] or not j["scheduled"] for j in status["jobs"]))
    scheduler_service.shutdown()

    print("\n[5] Web 接口")
    from app.web.app import create_app

    app = create_app(start_services=False)
    app.config.update(TESTING=True)
    client = app.test_client()

    resp = client.get("/api/overview")
    check("未登录访问被拦截", resp.status_code == 401, f"HTTP {resp.status_code}")

    resp = client.post("/api/login", json={"username": "admin", "password": "admin123"})
    check("默认账号可登录", resp.status_code == 200 and resp.get_json().get("ok") is True)
    login_data = (resp.get_json() or {}).get("data") or {}
    check("登录返回「必须改密」标记", login_data.get("must_change_password") is True, str(login_data))

    resp = client.get("/api/rules")
    check("未改默认口令前拒绝访问", resp.status_code == 428, f"HTTP {resp.status_code}")

    resp = client.post(
        "/api/account/password",
        json={"old_password": "admin123", "new_password": "Smoke@2026pw", "confirm_password": "Smoke@2026pw"},
    )
    check("改密接口在锁定期仍可用", resp.status_code == 200, f"HTTP {resp.status_code}")

    resp = client.get("/api/rules")
    check("改密后恢复正常访问", resp.status_code == 200, f"HTTP {resp.status_code}")

    for path in (
        "/api/overview",
        "/api/settings",
        "/api/rules",
        "/api/cron/status",
        "/api/cron/runs",
        "/api/logs",
        "/api/bot/messages",
        "/api/dingtalk/status",
    ):
        resp = client.get(path)
        check(f"GET {path}", resp.status_code == 200, f"HTTP {resp.status_code}")

    resp = client.get("/api/overview")
    payload = resp.get_json()["data"]
    check("概览含统计与状态", "stats" in payload and "cron" in payload and "dingtalk" in payload)

    resp = client.get("/")
    check("首页可访问", resp.status_code == 200 and b"<title>" in resp.data)
    resp = client.get("/login")
    check("登录页可访问", resp.status_code == 200)
    resp = client.get("/health")
    check("健康检查", resp.status_code == 200)

    resp = client.get("/metrics")
    check("Prometheus 指标可抓取", resp.status_code == 200 and b"dcf_rules" in resp.data)
    resp = client.get("/service-worker.js")
    check(
        "Service Worker 挂在根路径且放开 scope",
        resp.status_code == 200 and resp.headers.get("Service-Worker-Allowed") == "/",
        f"HTTP {resp.status_code} / {resp.headers.get('Service-Worker-Allowed')}",
    )
    resp = client.get("/static/manifest.webmanifest")
    check("PWA manifest 可访问", resp.status_code == 200)
    resp = client.get("/static/icon-192.png")
    check("PWA 图标可访问", resp.status_code == 200)

    print("\n[6] 指令卡片渲染")
    from app.dingtalk import render

    card = render.allow_card(
        {
            "ok": True,
            "ips": ["1.2.3.4"],
            "ports": ["ALL"],
            "protocol": "tcp",
            "summary": {"created": 1, "skipped": 0, "failed": 0},
            "results": [
                {
                    "instance_name": "轻量-杭州",
                    "ip": "1.2.3.4",
                    "port": "ALL",
                    "port_display": "全部端口",
                    "status": "success",
                    "message": "放行成功",
                }
            ],
        },
        ttl=1440,
        actor="张三",
    )
    check("放行卡片含标题", "放行成功" in card)
    check("放行卡片含实例", "轻量-杭州" in card)
    check("帮助卡片可渲染", "放行指令" in render.help_card())

    print("\n[7] 纯文本 / markdown 分流")
    simple = render.allow_reply(
        {
            "ok": True,
            "protocol": "TCP+UDP",
            "summary": {"created": 1, "skipped": 0, "failed": 0},
            "results": [{"ip": "203.0.113.42", "port": "1/65535", "status": "success"}],
        },
        ttl=1440,
    )
    check("简单放行走纯文本", isinstance(simple, render.PlainText), type(simple).__name__)
    check("纯文本不含 markdown 反引号", "`" not in str(simple), str(simple))
    check("纯文本点明全部端口", "全部端口" in str(simple), str(simple))

    multi = render.allow_reply(
        {
            "ok": False,
            "protocol": "TCP",
            "summary": {"created": 1, "skipped": 0, "failed": 1},
            "results": [
                {"ip": "203.0.113.42", "port": "22", "status": "success"},
                {"ip": "203.0.113.42", "port": "22", "status": "failed", "message": "x"},
            ],
        },
        ttl=0,
    )
    check("失败场景仍用卡片", not isinstance(multi, render.PlainText), "应为 markdown 卡片")

    check("帮助仍是卡片", not isinstance(render.help_card(), render.PlainText))
    check("无法识别指令用纯文本", isinstance(render.unknown_reply("你好"), render.PlainText))
    check("删除单条用纯文本", isinstance(render.delete_reply(["203.0.113.42"], 1, 0, []), render.PlainText))

    print("\n" + "=" * 62)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        for item in FAIL:
            print("  ✗", item)
        return 1
    print("全部通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
