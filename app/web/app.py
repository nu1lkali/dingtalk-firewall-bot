#!/usr/bin/env python3
"""
Flask 应用：只提供 REST API 与静态页面托管，页面逻辑全部在 app/static 下。
"""

from __future__ import annotations

import functools
import hmac
import os
import secrets
from typing import Any, Dict

import requests
from flask import Flask, jsonify, redirect, request, send_from_directory, session

from app.cloud.factory import build_client, provider_name
from app.cloud.swas import CloudError, port_to_display
from app.core import geo, rules_service
from app.core.iputil import get_public_ip
from app.cron.cronexpr import DEFAULT_TZ, describe, summarize, validate
from app.cron.jobs import JOB_FUNCS
from app.cron.scheduler import scheduler_service
from app.dingtalk.bot import handle_callback, stream_runner, verify_callback_signature
from app.dingtalk.client import DingTalkError, build_api, notify
from app.settings import BASE_DIR, settings
from app.store import DB_PATH, now_str, store
from app.utils.logger import logger
from app.web import metrics

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "static")
PUBLIC_PATHS = ("/login", "/health", "/metrics", "/webhook/ip-change", "/dingtalk/callback")
BACKUP_DIR = os.path.join(os.path.dirname(os.path.abspath(DB_PATH)), "backups")


# --------------------------------------------------------------------------- 鉴权
#: 改密前仍允许访问的接口（否则用户会被锁在门外）
PASSWORD_EXEMPT = {
    "/api/me",
    "/api/logout",
    "/api/account/password",
    "/api/account/status",
}


def _token_level() -> str:
    """
    判定请求携带的凭据等级：
      "session" —— 已登录会话，拥有全部权限
      "write"   —— 读写 Token
      "read"    —— 只读 Token
      ""        —— 未认证
    """
    if session.get("user"):
        return "session"
    token = request.headers.get("X-Api-Token") or request.args.get("token")
    if not token:
        return ""
    token = str(token)
    write_token = str(settings.get("server.api_token", "") or "")
    read_token = str(settings.get("server.api_token_readonly", "") or "")
    if write_token and hmac.compare_digest(token, write_token):
        return "write"
    if read_token and hmac.compare_digest(token, read_token):
        return "read"
    return ""


def _is_authed(level: str = "read") -> bool:
    got = _token_level()
    if not got:
        return False
    if level == "write":
        return got in ("session", "write")
    return True


def _password_change_required() -> bool:
    if session.get("user"):
        return bool(settings.get("server.must_change_password", False))
    return False


def login_required(func, level: str = "write"):
    """level="write" 的接口只接受会话或读写 Token；"read" 接口也接受只读 Token。"""

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        got = _token_level()
        if not got:
            return jsonify({"ok": False, "message": "未登录或登录已过期", "code": "UNAUTHORIZED"}), 401
        if level == "write" and got == "read":
            # 已认证但权限不足：给 403 而不是 401，免得使用者以为 Token 填错了
            return (
                jsonify(
                    {
                        "ok": False,
                        "message": "当前使用的是只读 Token，不能执行写操作",
                        "code": "READONLY_TOKEN",
                    }
                ),
                403,
            )
        if _password_change_required() and request.path not in PASSWORD_EXEMPT:
            return (
                jsonify(
                    {
                        "ok": False,
                        "message": "请先修改初始口令",
                        "code": "PASSWORD_CHANGE_REQUIRED",
                    }
                ),
                428,
            )
        return func(*args, **kwargs)

    return wrapper


def read_only(func):
    """查询类接口：读写 Token 与只读 Token 都能用。"""
    return login_required(func, level="read")


def ok(data: Any = None, message: str = "") -> Any:
    payload = {"ok": True, "message": message}
    if data is not None:
        payload["data"] = data
    return jsonify(payload)


def fail(message: str, status: int = 400, **extra) -> Any:
    payload = {"ok": False, "message": message}
    payload.update(extra)
    return jsonify(payload), status


def _safe_int(value: Any, default: int, low: int = 1, high: int = 1000) -> int:
    """把 query/body 里的字符串转成受控整数，非法输入回落到默认值（不让它变成 500）。"""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _client_ip() -> str:
    addr = (request.remote_addr or "").strip()
    # 反代场景下 Flask 已按 X-Forwarded-For 解析好 remote_addr；这里只做回环归一化
    return "127.0.0.1" if addr in ("::1", "localhost") else addr


def _is_loopback() -> bool:
    return _client_ip().startswith("127.")


# --------------------------------------------------------------------------- 应用
def bootstrap_services() -> Dict[str, Any]:
    """启动内置调度器与钉钉长连接。"""
    scheduler_service.start()
    stream_runner.start()
    store.log("服务启动", message=f"调度器 {len(scheduler_service.status().get('jobs', []))} 个任务，钉钉模式 {settings.get('dingtalk.mode')}")
    return {"cron": scheduler_service.status(), "dingtalk": stream_runner.status()}


def create_app(start_services: bool = False) -> Flask:
    app = Flask(
        __name__,
        static_folder=os.path.abspath(STATIC_DIR),
        static_url_path="/static",
    )
    settings.load()
    app.config["SECRET_KEY"] = settings.get("server.secret_key") or "change-me"
    app.config["JSON_AS_ASCII"] = False
    app.json.ensure_ascii = False
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

    register_page_routes(app)
    register_auth_routes(app)
    register_settings_routes(app)
    register_rule_routes(app)
    register_cloud_routes(app)
    register_cron_routes(app)
    register_log_routes(app)
    register_backup_routes(app)
    register_dingtalk_routes(app)
    register_webhook_routes(app)
    if start_services:
        bootstrap_services()
    return app


# --------------------------------------------------------------------------- 页面
def _read_static(app: Flask, name: str) -> str:
    path = os.path.join(app.static_folder, name)
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def register_page_routes(app: Flask) -> None:
    @app.get("/")
    def index():
        if not _is_authed():
            return redirect("/login")
        return send_from_directory(app.static_folder, "index.html")

    @app.get("/login")
    def login_page():
        return send_from_directory(app.static_folder, "login.html")

    @app.get("/service-worker.js")
    def service_worker():
        """
        Service Worker 必须挂在站点根路径才能接管全站；
        放 /static 下的话默认 scope 只有 /static/，所以这里单独给一条路由并放开 scope 限制。
        """
        resp = app.response_class(
            _read_static(app, "service-worker.js"), mimetype="application/javascript"
        )
        resp.headers["Service-Worker-Allowed"] = "/"
        resp.headers["Cache-Control"] = "no-cache, max-age=0"
        return resp

    @app.get("/favicon.ico")
    def favicon():
        return ("", 204)

    @app.get("/health")
    def health():
        return jsonify(
            {
                "status": "healthy",
                "time": now_str(),
                "provider": provider_name(),
                "cron": bool(scheduler_service.status().get("running")),
                "dingtalk": stream_runner.status(),
                "instances": len(settings.enabled_instances()),
            }
        )

    @app.get("/metrics")
    def prometheus_metrics():
        """Prometheus 抓取端点（文本格式，零依赖）。"""
        if not settings.get("server.metrics_public", True) and not _is_authed("read"):
            return ("forbidden", 403)
        return app.response_class(metrics.render(), mimetype="text/plain; version=0.0.4")


# --------------------------------------------------------------------------- 登录
def register_auth_routes(app: Flask) -> None:
    # 登录失败限流：同一来源 5 次失败后进入冷却，防止默认口令被在线爆破
    failures: Dict[str, Dict[str, float]] = {}
    MAX_FAILS = 5
    LOCK_SECONDS = 300

    def _too_many(addr: str) -> bool:
        item = failures.get(addr)
        if not item:
            return False
        import time

        if time.time() - item["at"] > LOCK_SECONDS:
            failures.pop(addr, None)
            return False
        return item["count"] >= MAX_FAILS

    @app.post("/api/login")
    def login():
        data = request.get_json(silent=True) or {}
        user = str(data.get("username", "")).strip()
        password = str(data.get("password", ""))
        addr = _client_ip()

        if _too_many(addr):
            store.log("登录失败", target=user, status="failed", level="WARNING", message=f"来源 {addr} 已锁定")
            return fail("失败次数过多，请 5 分钟后再试", 429)

        expected_user = str(settings.get("server.admin_user", "admin"))
        expected_pass = str(settings.get("server.admin_password", "admin123"))
        # 定长比较，避免按响应时间猜测口令
        user_ok = hmac.compare_digest(user, expected_user)
        pass_ok = hmac.compare_digest(password, expected_pass)

        if user_ok and pass_ok:
            failures.pop(addr, None)
            session["user"] = user
            store.log("登录", target=user, message=f"来源 {addr}")
            return ok(
                {
                    "user": user,
                    "must_change_password": bool(settings.get("server.must_change_password", False)),
                }
            )

        import time

        item = failures.setdefault(addr, {"count": 0, "at": time.time()})
        item["count"] += 1
        item["at"] = time.time()
        store.log("登录失败", target=user, status="failed", level="WARNING", message=f"来源 {addr}")
        return fail("用户名或密码错误", 401)

    @app.post("/api/logout")
    def logout():
        session.clear()
        return ok(message="已退出")

    @app.get("/api/me")
    def me():
        if not _is_authed():
            return fail("未登录", 401, code="UNAUTHORIZED")
        return ok(
            {
                "user": session.get("user") or "api-token",
                "level": _token_level(),
                "must_change_password": _password_change_required(),
            }
        )

    @app.get("/api/account/status")
    @login_required
    def account_status():
        return ok(
            {
                "user": session.get("user") or "api-token",
                "level": _token_level(),
                "must_change_password": _password_change_required(),
                "using_default_password": str(settings.get("server.admin_password", "")) in ("", "admin123"),
            }
        )

    @app.post("/api/account/password")
    @login_required
    def change_password():
        """修改管理台口令；改完自动解除「必须改密」状态。"""
        data = request.get_json(silent=True) or {}
        old = str(data.get("old_password", ""))
        new = str(data.get("new_password", ""))
        confirm = str(data.get("confirm_password", new))

        if _token_level() != "session":
            return fail("请先用账号登录后再修改口令", 403)
        if not hmac.compare_digest(old, str(settings.get("server.admin_password", ""))):
            return fail("原口令不正确", 400)
        if len(new) < 8:
            return fail("新口令至少 8 位", 400)
        if new in ("admin123", "12345678", "password"):
            return fail("新口令过于简单，请换一个", 400)
        if new != confirm:
            return fail("两次输入的新口令不一致", 400)
        if hmac.compare_digest(new, old):
            return fail("新口令不能与原口令相同", 400)

        settings.update({"server": {"admin_password": new, "must_change_password": False}})
        store.log("修改口令", target=session.get("user", ""), message="管理台口令已更新")
        return ok({"must_change_password": False}, message="口令已更新")

    @app.get("/api/account/tokens")
    @login_required
    def account_tokens():
        """
        返回明文令牌。

        仅限网页会话：令牌本身就是凭据，不能拿一个令牌去换另一个令牌。
        """
        if _token_level() != "session":
            return fail("只有网页登录状态可以查看令牌", 403)
        return ok(
            {
                "api_token": settings.get("server.api_token", ""),
                "api_token_readonly": settings.get("server.api_token_readonly", ""),
            }
        )

    @app.post("/api/account/tokens/rotate")
    @login_required
    def rotate_token():
        """轮换令牌：旧值立即失效，适合怀疑泄露时使用。"""
        if _token_level() != "session":
            return fail("只有网页登录状态可以重置令牌", 403)
        level = str((request.get_json(silent=True) or {}).get("level") or "write").lower()
        key = "api_token_readonly" if level == "read" else "api_token"
        new_value = secrets.token_hex(12)
        settings.update({"server": {key: new_value}})
        store.log(
            "重置令牌",
            target="只读 Token" if level == "read" else "读写 Token",
            message="已轮换，旧令牌立即失效",
        )
        return ok(
            {
                "api_token": settings.get("server.api_token", ""),
                "api_token_readonly": settings.get("server.api_token_readonly", ""),
            },
            message="已重置，记得更新使用该令牌的脚本",
        )


# --------------------------------------------------------------------------- 设置
def register_settings_routes(app: Flask) -> None:
    @app.get("/api/settings")
    @login_required
    def get_settings():
        return ok(settings.masked())

    @app.put("/api/settings")
    @login_required
    def update_settings():
        patch = request.get_json(silent=True) or {}
        before = settings.data
        try:
            merged = settings.update(patch)
        except Exception as exc:  # noqa: BLE001
            return fail(f"保存失败: {exc}")
        changed = {
            key
            for key in (
                "cron",
                "dingtalk",
                "aliyun",
                "rules_policy",
                "logging",
                "webhook",
                "server",
                "notify",
                "geo",
            )
            if before.get(key) != merged.get(key)
        }
        # 通过设置页改了口令，同样解除「必须改密」
        new_password = str((patch.get("server") or {}).get("admin_password") or "")
        if new_password and not new_password.startswith("*"):
            merged = settings.update({"server": {"must_change_password": False}})
            store.log("修改口令", target=session.get("user", ""), message="已通过系统设置更新口令")
        restarted = []
        if "cron" in changed:
            scheduler_service.reload()
            restarted.append("定时任务")
        if "dingtalk" in changed:
            stream_runner.restart()
            restarted.append("钉钉机器人")
        if "aliyun" in changed:
            # provider / 凭证变了：rules_service 每次调用都会重新 build_client，无需额外清理
            restarted.append("云端客户端")
        store.log(
            "保存配置",
            target=",".join(sorted(changed)) or "无变化",
            message=f"已保存，重载：{'、'.join(restarted) if restarted else '无'}",
        )
        return ok(settings.masked(), message="配置已保存")

    @app.post("/api/settings/test/aliyun")
    @login_required
    def test_aliyun():
        result = rules_service.health_probe()
        if result["ok"]:
            return ok(result["detail"], message="阿里云凭证可用")
        return fail(str(result["detail"]), 400)

    @app.post("/api/settings/test/dingtalk")
    @login_required
    def test_dingtalk():
        data = request.get_json(silent=True) or {}
        try:
            api = build_api()
            info = api.ping()
        except DingTalkError as exc:
            return fail(str(exc))
        conversation = data.get("conversation_id") or settings.get("dingtalk.notify_conversation_id")
        sent = False
        if conversation:
            try:
                api.send_group_markdown(
                    conversation,
                    "连通性测试",
                    "### ✅ 钉钉机器人连通性测试\n\n凭证有效，可正常向本群推送消息。",
                )
                sent = True
            except DingTalkError as exc:
                return fail(f"凭证有效，但发送消息失败: {exc}")
        return ok({"token": info, "sent": sent}, message="凭证有效" + ("，测试消息已发送" if sent else ""))


# --------------------------------------------------------------------------- 规则
def register_rule_routes(app: Flask) -> None:
    @app.get("/api/rules")
    @read_only
    def list_rules():
        """
        本地规则列表（分页）。

        默认只返回「在用」规则（生效中 + 已禁用），已删除/已过期不再混在列表里。
        返回 `{items, total, page, page_size, pages, tags}`，前端按页渲染。
        """
        status = request.args.get("status", "current")
        keyword = request.args.get("keyword", "").strip()
        instance_id = request.args.get("instance_id") or None
        tag = request.args.get("tag") or None
        page = _safe_int(request.args.get("page"), 1, 1, 100000)
        page_size = _safe_int(request.args.get("page_size"), 20, 1, 200)

        if status in ("current", "live"):
            scope = {"status_in": rules_service.LIVE_STATUSES}
        elif status == "gone":
            scope = {"status_in": sorted(rules_service.GONE_STATUSES)}
        else:
            scope = {"status": status}

        total = store.count_rules(keyword=keyword, instance_id=instance_id, tag=tag, **scope)
        rows = store.list_rules(
            keyword=keyword,
            instance_id=instance_id,
            tag=tag,
            limit=page_size,
            offset=(page - 1) * page_size,
            **scope,
        )
        for row in rows:
            row["port_display"] = port_to_display(row["port"])

        return ok(
            {
                "items": rows,
                "total": total,
                "page": page,
                "page_size": page_size,
                "pages": max(1, (total + page_size - 1) // page_size),
                "tags": store.list_tags(),
            }
        )

    @app.get("/api/tags")
    @read_only
    def list_tags():
        return ok(store.list_tags())

    @app.post("/api/rules")
    @login_required
    def create_rule():
        data = request.get_json(silent=True) or {}
        instance_ids = [i for i in (data.get("instance_ids") or []) if i]
        instance_id = data.get("instance_id") or ""
        if not instance_id and not instance_ids:
            return fail("请选择实例")
        try:
            result = rules_service.create_rule_manual(
                instance_id=instance_id,
                ip=data.get("ip", ""),
                ports=data.get("ports") or "ALL",
                protocol=data.get("protocol") or "tcp",
                description=data.get("description") or "",
                ttl_minutes=_safe_int(data.get("ttl_minutes"), 0, 0, 525600),
                actor=session.get("user", "web"),
                instance_ids=instance_ids or None,
                tag=str(data.get("tag") or "").strip()[:32],
            )
        except CloudError as exc:
            return fail(str(exc))
        return (ok(result) if result.get("ok") else fail(result.get("message", "新增失败")))

    @app.put("/api/rules/<int:rule_pk>")
    @login_required
    def update_rule(rule_pk: int):
        data = request.get_json(silent=True) or {}
        try:
            result = rules_service.update_rule_manual(
                rule_pk,
                ip=data.get("ip"),
                ports=data.get("ports"),
                protocol=data.get("protocol"),
                description=data.get("description"),
                ttl_minutes=(_safe_int(data["ttl_minutes"], 0, 0, 525600) if data.get("ttl_minutes") not in (None, "") else None),
                actor=session.get("user", "web"),
            )
        except CloudError as exc:
            return fail(str(exc))
        return ok(result) if result.get("ok") else fail(result.get("message", "更新失败"))

    @app.delete("/api/rules/<int:rule_pk>")
    @login_required
    def delete_rule(rule_pk: int):
        result = rules_service.delete_rule(rule_pk, actor=session.get("user", "web"))
        return ok(result) if result.get("ok") else fail(result.get("message", "删除失败"))

    @app.post("/api/rules/<int:rule_pk>/restore")
    @login_required
    def restore_rule(rule_pk: int):
        """把软删除的历史记录重新下发到云端。"""
        result = rules_service.restore_rule(rule_pk, actor=session.get("user", "web"))
        return ok(result) if result.get("ok") else fail(result.get("message", "恢复失败"))

    @app.delete("/api/rules/<int:rule_pk>/purge")
    @login_required
    def purge_rule(rule_pk: int):
        """从本地数据库彻底移除一条历史记录（不影响云端）。"""
        result = rules_service.purge_rule(rule_pk, actor=session.get("user", "web"))
        return ok(result) if result.get("ok") else fail(result.get("message", "清理失败"))

    @app.post("/api/rules/<int:rule_pk>/toggle")
    @login_required
    def toggle_rule(rule_pk: int):
        data = request.get_json(silent=True) or {}
        result = rules_service.toggle_rule(
            rule_pk, bool(data.get("enable")), actor=session.get("user", "web")
        )
        return ok(result) if result.get("ok") else fail(result.get("message", "操作失败"))

    @app.post("/api/rules/<int:rule_pk>/renew")
    @login_required
    def renew_rule(rule_pk: int):
        """续期：从当前时间起延长 N 分钟（默认 24 小时）。"""
        data = request.get_json(silent=True) or {}
        minutes = _safe_int(data.get("minutes"), 1440, 1, 525600)
        result = rules_service.renew_rule(rule_pk, minutes, actor=session.get("user", "web"))
        return ok(result) if result.get("ok") else fail(result.get("message", "续期失败"))

    @app.post("/api/rules/renew")
    @login_required
    def renew_rules_batch():
        """批量续期：按 ID 列表一次性延长。"""
        data = request.get_json(silent=True) or {}
        ids = data.get("ids") or []
        minutes = _safe_int(data.get("minutes"), 1440, 1, 525600)
        if not isinstance(ids, list) or not ids:
            return fail("请选择要续期的规则")
        done, failed = [], []
        for rule_pk in ids[:200]:
            try:
                res = rules_service.renew_rule(int(rule_pk), minutes, actor=session.get("user", "web"))
            except (TypeError, ValueError):
                failed.append({"id": rule_pk, "message": "非法 ID"})
                continue
            if res.get("ok"):
                done.append(rule_pk)
            else:
                failed.append({"id": rule_pk, "message": res.get("message", "")})
        return ok(
            {"renewed": len(done), "failed": failed},
            message=f"已续期 {len(done)} 条" + (f"，失败 {len(failed)} 条" if failed else ""),
        )


# --------------------------------------------------------------------------- 云端
def register_cloud_routes(app: Flask) -> None:
    @app.get("/api/cloud/rules")
    @login_required
    def cloud_rules():
        instance_id = request.args.get("instance_id")
        try:
            if instance_id:
                rules = rules_service.list_cloud_rules(instance_id)
                return ok({"instance_id": instance_id, "rules": rules})
            return ok(rules_service.list_all_cloud_rules())
        except CloudError as exc:
            return fail(str(exc))

    @app.post("/api/cloud/sync")
    @login_required
    def cloud_sync():
        result = rules_service.sync_all_instances()
        return ok(result, message=f"已同步 {result['synced_rules']} 条规则")

    @app.get("/api/cloud/instances")
    @login_required
    def cloud_instances():
        try:
            client = build_client()
            remote = client.list_instances()
        except CloudError as exc:
            return fail(str(exc))
        configured = {i["instance_id"] for i in settings.get("aliyun.instances", []) or []}
        for item in remote:
            item["configured"] = item["instance_id"] in configured
        return ok(remote)

    @app.get("/api/cloud/diff")
    @read_only
    def cloud_diff():
        """本地镜像 vs 云端规则的差异（漂移检测）。"""
        instance_id = request.args.get("instance_id")
        try:
            if instance_id:
                return ok(rules_service.cloud_diff(instance_id))
            return ok(rules_service.diff_all_instances())
        except CloudError as exc:
            return fail(str(exc))

    @app.post("/api/geo/lookup")
    @read_only
    def geo_lookup():
        """批量查 IP 归属地（命中本地缓存则不发请求）。"""
        data = request.get_json(silent=True) or {}
        ips = data.get("ips")
        if not isinstance(ips, list):
            ips = [data.get("ip")] if data.get("ip") else []
        ips = [str(i).strip() for i in ips if i][:60]
        if not ips:
            return ok({})
        return ok(geo.lookup_many(ips))

    @app.post("/api/cloud/rule-action")
    @login_required
    def cloud_rule_action():
        """
        针对云端规则的操作入口：delete / enable / disable / update。
        云端规则可能不在本地镜像里（例如控制台手工创建），这里会自动补建本地记录。
        """
        data = request.get_json(silent=True) or {}
        instance_id = (data.get("instance_id") or "").strip()
        rule_id = (data.get("rule_id") or "").strip()
        action = (data.get("action") or "").strip()
        if not instance_id or not rule_id:
            return fail("缺少 instance_id 或 rule_id")

        rows = [
            r
            for r in store.find_rules(instance_id=instance_id, status_in=["active", "disabled"])
            if r["rule_id"] == rule_id
        ]
        if rows:
            rule_pk = rows[0]["id"]
        else:
            rule_pk = store.add_rule(
                {
                    "instance_id": instance_id,
                    "instance_name": settings.instance_name(instance_id),
                    "rule_id": rule_id,
                    "ip": data.get("ip", ""),
                    "cidr": data.get("cidr", ""),
                    "protocol": data.get("protocol", "tcp"),
                    "port": data.get("port", "ALL"),
                    "description": data.get("description", ""),
                    "status": "active",
                    "source": "cloud",
                    "actor": session.get("user", "web"),
                }
            )

        if action == "delete":
            result = rules_service.delete_rule(rule_pk, actor=session.get("user", "web"))
        elif action in ("enable", "disable"):
            result = rules_service.toggle_rule(
                rule_pk, action == "enable", actor=session.get("user", "web")
            )
        elif action == "update":
            result = rules_service.update_rule_manual(
                rule_pk,
                ip=data.get("ip"),
                ports=data.get("ports") or data.get("port"),
                protocol=data.get("protocol"),
                description=data.get("description"),
                actor=session.get("user", "web"),
            )
        else:
            return fail(f"不支持的操作: {action}")
        return ok(result) if result.get("ok") else fail(result.get("message", "操作失败"))

    @app.get("/api/ip/current")
    @login_required
    def current_ip():
        result = get_public_ip()
        if not result.get("ip"):
            return fail(f"获取公网 IP 失败: {result.get('errors', '')}")
        return ok(result)

    @app.post("/api/ip/allow")
    @login_required
    def ip_allow():
        """一键：获取本机公网 IP -> 通过 webhook 放行。"""
        data = request.get_json(silent=True) or {}
        ip = (data.get("ip") or "").strip()
        source = "manual"
        if not ip:
            detected = get_public_ip()
            ip = detected.get("ip", "")
            source = detected.get("source", "")
            if not ip:
                return fail(f"获取公网 IP 失败: {detected.get('errors', '')}")

        ports = data.get("ports") or settings.get("dingtalk.default_ports") or "ALL"
        protocol = data.get("protocol") or "tcp"
        ttl = data.get("ttl_minutes")
        ttl = int(ttl) if ttl not in (None, "") else int(settings.get("rules_policy.default_ttl_minutes", 1440))

        if data.get("via_webhook", True):
            url = f"http://127.0.0.1:{settings.get('server.port', 5050)}/webhook/ip-change"
            headers = {"Content-Type": "application/json"}
            secret = settings.get("webhook.secret", "")
            if secret:
                headers["X-Webhook-Secret"] = secret
            try:
                resp = requests.post(
                    url,
                    json={
                        "ipv4": ip,
                        "ports": ports,
                        "protocol": protocol,
                        "ttl_minutes": ttl,
                        "domain": "web-manual",
                    },
                    headers=headers,
                    timeout=30,
                )
                body = resp.json()
                return ok(
                    {"ip": ip, "source": source, "webhook": body, "status_code": resp.status_code},
                    message=body.get("message", "已通过 webhook 放行"),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"调用本地 webhook 失败，改为直接放行: {exc}")

        try:
            result = rules_service.allow_ip(
                ip,
                ports=ports,
                protocol=protocol,
                description="Web 手动放行",
                source="web",
                actor=session.get("user", "web"),
                ttl_minutes=ttl,
            )
        except CloudError as exc:
            return fail(str(exc))
        return ok({"ip": ip, "source": source, "direct": result}, message=result.get("message", ""))


# --------------------------------------------------------------------------- 定时任务
def register_cron_routes(app: Flask) -> None:
    @app.get("/api/cron/status")
    @login_required
    def cron_status():
        return ok(scheduler_service.status())

    @app.post("/api/cron/reload")
    @login_required
    def cron_reload():
        scheduler_service.reload()
        store.log("重载定时任务", message="已按最新配置重建调度")
        return ok(scheduler_service.status(), message="调度已重载")

    @app.post("/api/cron/<job_id>/run")
    @login_required
    def cron_run(job_id: str):
        result = scheduler_service.run_now(job_id)
        return ok(result) if result.get("ok") else fail(result.get("message", "执行失败"))

    @app.get("/api/cron/runs")
    @login_required
    def cron_runs():
        limit = _safe_int(request.args.get("limit"), 50, 1, 1000)
        job_id = request.args.get("job_id") or None
        return ok(store.list_task_runs(limit=limit, job_id=job_id))

    @app.post("/api/cron/preview")
    @login_required
    def cron_preview():
        """可视化配置器用：校验表达式并返回中文说明 + 未来执行时间。"""
        data = request.get_json(silent=True) or {}
        expr = (data.get("expr") or "").strip()
        tz = data.get("timezone") or settings.get("cron.timezone") or DEFAULT_TZ
        info = summarize(expr, count=_safe_int(data.get("count"), 5, 1, 10), timezone=tz)
        return ok(info) if info["valid"] else fail(info["description"], 400, data=info)

    @app.put("/api/cron/jobs/<job_id>")
    @login_required
    def cron_update_job(job_id: str):
        """单独保存某个任务的 cron / 开关，不必整表提交设置。"""
        if job_id not in JOB_FUNCS:
            return fail("任务不存在", 404)
        data = request.get_json(silent=True) or {}
        patch: Dict[str, Any] = {}
        if "cron" in data:
            expr = (data.get("cron") or "").strip()
            tz = settings.get("cron.timezone") or DEFAULT_TZ
            ok_expr, err = validate(expr, tz)
            if not ok_expr:
                return fail(f"cron 表达式不合法：{err}", 400)
            patch["cron"] = expr
        if "enabled" in data:
            patch["enabled"] = bool(data.get("enabled"))
        if "name" in data and str(data.get("name")).strip():
            patch["name"] = str(data.get("name")).strip()[:32]
        if not patch:
            return fail("没有需要更新的字段", 400)

        current = settings.get(f"cron.jobs.{job_id}", {}) or {}
        merged = {**current, **patch}
        settings.update({"cron": {"jobs": {job_id: merged}}})
        scheduler_service.reload()
        job_name = merged.get("name") or job_id
        store.log(
            "修改任务计划",
            target=job_name,
            message=f"cron={merged.get('cron','')}（{describe(merged.get('cron',''))}）· "
            f"{'启用' if merged.get('enabled', True) else '停用'}",
        )
        return ok(
            {
                "job": {
                    "id": job_id,
                    "name": job_name,
                    "cron": merged.get("cron", ""),
                    "cron_desc": describe(merged.get("cron", "")),
                    "enabled": bool(merged.get("enabled", True)),
                },
                "scheduler": scheduler_service.status(),
            },
            message="已保存并重载调度",
        )


# --------------------------------------------------------------------------- 日志
def _security_warnings() -> list:
    """把「上线前必须改」的配置问题显性化，避免默认口令/裸奔 webhook 悄悄跑到生产。"""
    warnings = []
    if str(settings.get("server.admin_password", "")) in ("", "admin123"):
        warnings.append(
            {
                "level": "high",
                "key": "weak_password",
                "message": "管理台仍在使用默认口令 admin123，请立刻在「系统设置」中修改。",
            }
        )
    if not settings.get("webhook.secret", ""):
        warnings.append(
            {
                "level": "medium",
                "key": "webhook_no_secret",
                "message": "Webhook 未配置密钥，目前只接受本机调用；外部 DDNS 回调需要先设置密钥。",
            }
        )
    if not settings.get("server.api_token", ""):
        warnings.append(
            {
                "level": "low",
                "key": "no_api_token",
                "message": "未配置 API Token，脚本调用只能走登录会话。",
            }
        )
    if not settings.get("notify.enabled", False):
        warnings.append(
            {
                "level": "low",
                "key": "notify_off",
                "message": "规则变更通知未开启，别人改了白名单不会有人知道。",
            }
        )
    if not any(i.get("security_group_id") for i in settings.get("aliyun.instances", []) or []) and provider_name() == "ecs":
        warnings.append(
            {
                "level": "medium",
                "key": "ecs_no_sg",
                "message": "已切到 ECS 模式，但实例配置里没填安全组 ID（instance_id 应填 sg-xxx）。",
            }
        )
    return warnings


def register_log_routes(app: Flask) -> None:
    @app.get("/api/logs")
    @login_required
    def logs():
        return ok(
            store.list_logs(
                limit=_safe_int(request.args.get("limit"), 200, 1, 1000),
                keyword=request.args.get("keyword", ""),
                level=request.args.get("level", ""),
            )
        )

    @app.get("/api/bot/messages")
    @login_required
    def bot_messages():
        return ok(store.list_bot_messages(limit=_safe_int(request.args.get("limit"), 100, 1, 1000)))

    @app.get("/api/overview")
    @login_required
    def overview():
        sched = scheduler_service.status()
        return ok(
            {
                "stats": rules_service.stats(),
                "server_time": now_str(),
                "aliyun": {
                    "enabled": bool(settings.get("aliyun.enabled")),
                    "region_id": settings.get("aliyun.region_id"),
                    "key_ready": bool(settings.get("aliyun.access_key_id")),
                    "instances": settings.get("aliyun.instances", []) or [],
                },
                "dingtalk": stream_runner.status(),
                "cron": {
                    "running": sched.get("running"),
                    "job_count": sched.get("job_count", 0),
                    "running_jobs": sched.get("running_jobs", 0),
                    "started_at": sched.get("started_at", ""),
                },
                "recent_logs": store.list_logs(limit=6),
                "recent_bot": store.list_bot_messages(limit=6),
                "recent_tasks": store.list_task_runs(limit=6),
                "security_warnings": _security_warnings(),
            }
        )


# --------------------------------------------------------------------------- 备份
def _backup_files() -> list:
    if not os.path.isdir(BACKUP_DIR):
        return []
    items = []
    for name in sorted(os.listdir(BACKUP_DIR), reverse=True):
        if not (name.startswith("app-") and name.endswith(".db")):
            continue
        path = os.path.join(BACKUP_DIR, name)
        stat = os.stat(path)
        items.append(
            {
                "name": name,
                "size_kb": round(stat.st_size / 1024, 1),
                "created_at": __import__("datetime").datetime.fromtimestamp(stat.st_mtime).strftime(
                    "%Y-%m-%d %H:%M:%S"
                ),
            }
        )
    return items


def register_backup_routes(app: Flask) -> None:
    @app.post("/api/backup/run")
    @login_required
    def backup_run():
        from app.cron.jobs import job_backup_db

        status, message = job_backup_db()
        if status != "success":
            return fail(message)
        store.log("手动备份", message=message)
        return ok({"files": _backup_files()}, message=message)

    @app.get("/api/backups")
    @read_only
    def backup_list():
        return ok({"files": _backup_files(), "dir": BACKUP_DIR})

    @app.get("/api/backups/<path:name>")
    @login_required
    def backup_download(name: str):
        # 只允许下载 backups 目录下的 .db，防止路径穿越
        safe = os.path.basename(name)
        if not (safe.startswith("app-") and safe.endswith(".db")):
            return fail("非法文件名", 400)
        if not os.path.isfile(os.path.join(BACKUP_DIR, safe)):
            return fail("备份不存在", 404)
        return send_from_directory(BACKUP_DIR, safe, as_attachment=True)


# --------------------------------------------------------------------------- 钉钉
def register_dingtalk_routes(app: Flask) -> None:
    @app.get("/api/dingtalk/status")
    @login_required
    def dingtalk_status():
        return ok(stream_runner.status())

    @app.get("/api/dingtalk/conversations")
    @login_required
    def dingtalk_conversations():
        """返回机器人收到过消息的群会话，供主动推送选择 openConversationId。"""
        return ok(store.list_conversations())

    @app.post("/api/dingtalk/diagnose")
    @login_required
    def dingtalk_diagnose():
        """一键排查：凭证 → 长连接 → 订阅主题 → 是否真的收到帧。"""
        cfg = settings.section("dingtalk")
        checks = []

        checks.append(
            {
                "name": "机器人已启用",
                "ok": bool(cfg.get("enabled")),
                "detail": f"模式 {cfg.get('mode', 'stream')}",
            }
        )
        checks.append(
            {
                "name": "Client ID / Secret 已配置",
                "ok": bool(cfg.get("client_id") and cfg.get("client_secret")),
                "detail": (cfg.get("client_id") or "未填写"),
            }
        )

        try:
            build_api().ping()
            checks.append({"name": "获取 AccessToken", "ok": True, "detail": "凭证有效"})
        except DingTalkError as exc:
            checks.append({"name": "获取 AccessToken", "ok": False, "detail": str(exc)})

        state = stream_runner.status()
        checks.append(
            {
                "name": "Stream 长连接已建立",
                "ok": bool(state.get("connected")),
                "detail": state.get("endpoint") or state.get("last_error") or "未连接",
            }
        )
        topics = state.get("topics") or []
        checks.append(
            {
                "name": "已订阅机器人消息主题",
                "ok": bool(topics),
                "detail": "、".join(topics) or "未订阅",
            }
        )
        frames = state.get("frames", 0)
        checks.append(
            {
                "name": "收到过原始消息帧",
                "ok": frames > 0,
                "detail": f"累计 {frames} 帧，最后 {state.get('last_frame_at') or '从未'}",
            }
        )
        checks.append(
            {
                "name": "已解析出机器人指令",
                "ok": state.get("messages", 0) > 0,
                "detail": f"累计 {state.get('messages', 0)} 条",
            }
        )
        checks.append(
            {
                "name": "已配置阿里云实例",
                "ok": len(settings.enabled_instances()) > 0,
                "detail": f"{len(settings.enabled_instances())} 台",
            }
        )

        tips = []
        if not state.get("connected"):
            tips.append("长连接未建立：检查 Client ID / Client Secret 是否复制完整（不要带空格）。")
        elif frames == 0:
            tips.extend(
                [
                    "长连接正常但**一个消息帧都没收到**，说明钉钉没有把群消息推给这个应用。按顺序确认：",
                    "① 群里 @ 的机器人是不是本应用创建的机器人（群设置 → 机器人 → 看来源应用名称）；",
                    "② 开发者后台 → 应用 → 机器人 → **消息接收模式必须选 Stream**（选 HTTP 回调时消息走公网地址，Stream 收不到）；",
                    "③ 应用是否已「创建版本 → 发布」并通过审批（未发布的消息不会推给 Stream）；",
                    "④ 你的企微/钉钉账号是否在应用「可见范围」内。",
                ]
            )
        elif state.get("messages", 0) == 0:
            tips.append(
                "收到了帧但没解析出指令：可能是消息被「仅处理 @机器人」过滤，或 topic 不匹配。"
                f"最后收到的 topic = {state.get('last_topic') or '未知'}，请查看 logs/app.log 里的帧内容。"
            )

        return ok(
            {
                "checks": checks,
                "status": state,
                "tips": tips,
                "recent_messages": store.list_bot_messages(5),
                "recent_frames_log": "logs/app.log",
            }
        )

    @app.post("/api/dingtalk/restart")
    @login_required
    def dingtalk_restart():
        state = stream_runner.restart()
        store.log("重启机器人", message="已重启钉钉长连接")
        return ok(state, message="已重启")

    @app.post("/api/dingtalk/send")
    @login_required
    def dingtalk_send():
        data = request.get_json(silent=True) or {}
        text = (data.get("text") or "").strip()
        conversation = (data.get("conversation_id") or "").strip() or settings.get(
            "dingtalk.notify_conversation_id", ""
        )
        if not text:
            return fail("消息内容不能为空")
        if not conversation:
            return fail("请填写目标会话 openConversationId")
        try:
            build_api().send_group_markdown(conversation, data.get("title") or "测试消息", text)
        except DingTalkError as exc:
            return fail(str(exc))
        return ok(message="已发送")


# --------------------------------------------------------------------------- Webhook
def register_webhook_routes(app: Flask) -> None:
    @app.post("/webhook/ip-change")
    def webhook_ip_change():
        secret = settings.get("webhook.secret", "")
        if secret:
            if not hmac.compare_digest(str(request.headers.get("X-Webhook-Secret") or ""), str(secret)):
                store.log("Webhook", status="failed", level="WARNING", message="密钥校验失败")
                return fail("Invalid secret", 401)
        elif not _is_loopback():
            # 没配密钥时只信任本机调用（例如 Web 端「一键放行」），
            # 否则任何人只要能访问到本服务，就能把任意 IP 加进白名单。
            store.log(
                "Webhook",
                status="failed",
                level="WARNING",
                message=f"未配置密钥，拒绝来自 {_client_ip()} 的外部调用",
            )
            return fail(
                "Webhook 未配置密钥，已拒绝外部调用。请在「系统设置 → Webhook」中设置密钥。",
                403,
                code="WEBHOOK_NO_SECRET",
            )

        data = request.get_json(silent=True) or {}
        ipv4 = (data.get("ipv4") or data.get("ip") or "").strip()
        ipv6 = (data.get("ipv6") or "").strip()
        if not ipv4 and not ipv6:
            return fail("缺少 ip / ipv4 / ipv6 字段")

        ports = data.get("ports") or settings.get("dingtalk.default_ports") or "ALL"
        protocol = data.get("protocol") or "tcp"
        ttl = data.get("ttl_minutes")
        ttl = int(ttl) if ttl not in (None, "") else int(settings.get("rules_policy.default_ttl_minutes", 1440))
        domain = data.get("domain", "")

        outcomes: Dict[str, Any] = {}
        all_ok = True
        for label, ip in (("IPv4", ipv4), ("IPv6", ipv6)):
            if not ip:
                continue
            try:
                result = rules_service.allow_ip(
                    ip,
                    ports=ports,
                    protocol=protocol,
                    description=f"Webhook {domain}".strip(),
                    source="webhook",
                    actor=domain or "webhook",
                    ttl_minutes=ttl,
                )
            except CloudError as exc:
                store.log(
                    "Webhook",
                    target=ip,
                    status="failed",
                    level="WARNING",
                    message=f"参数非法: {exc}",
                )
                return fail(f"{label} 参数非法: {exc}", 400)
            outcomes[label] = result
            all_ok = all_ok and bool(result.get("ok"))

        summary = " | ".join(
            f"{k}: {v.get('message', '')}" for k, v in outcomes.items()
        )
        if settings.get("dingtalk.notify_on_webhook"):
            try:
                notify(
                    f"### 🔔 IP 变更放行\n\n- 域名：`{domain or '-'}`\n"
                    f"- IP：`{ipv4 or '-'}` / `{ipv6 or '-'}`\n"
                    f"- 结果：{summary}",
                    title="IP 变更放行",
                )
            except Exception:  # noqa: BLE001
                pass

        return (
            jsonify({"ok": all_ok, "success": all_ok, "message": summary, "results": outcomes}),
            200 if all_ok else 207,
        )

    @app.post("/dingtalk/callback")
    def dingtalk_callback():
        cfg = settings.section("dingtalk")
        if cfg.get("mode") != "callback":
            return fail("当前未启用 HTTP 回调模式", 403)
        token = cfg.get("callback_token") or ""
        timestamp = request.headers.get("timestamp", "")
        sign = request.headers.get("sign", "")
        if token:
            if not verify_callback_signature(timestamp, sign, token):
                logger.warning("钉钉回调验签失败")
                return fail("sign invalid", 401)
        data = request.get_json(silent=True) or {}
        return jsonify(handle_callback(data, client_ip=request.remote_addr or ""))
