#!/usr/bin/env python3
"""
统一配置中心。

优先级（后者覆盖前者）：
    DEFAULTS  <  config/config.yaml（首次启动一次性导入）  <  数据库 settings 表（Web 端写入）

Web 端保存配置后立即写库并热生效，不再依赖本地配置文件。
"""

from __future__ import annotations

import copy
import json
import os
import secrets
import stat
from typing import Any, Dict, List, Optional

import yaml

from app.store import BASE_DIR, store
from app.utils.logger import logger

CONFIG_YAML = os.path.join(BASE_DIR, "config", "config.yaml")
# 可用 APP_SETTINGS_JSON 覆盖，方便用临时库跑演示/测试时不污染真实配置镜像
CONFIG_JSON = os.environ.get("APP_SETTINGS_JSON") or os.path.join(BASE_DIR, "data", "settings.json")
ENV_FILE = os.path.join(BASE_DIR, ".env")

# 每条放行规则在云端的默认生命周期（分钟），0 表示永不过期
DEFERRED_TTL = 1440


def _uid() -> str:
    return secrets.token_hex(16)


DEFAULTS: Dict[str, Any] = {
    "server": {
        "host": "0.0.0.0",
        "port": 5050,
        "admin_user": "admin",
        "admin_password": "admin123",
        "secret_key": "",
        "api_token": "",           # 读写 Token
        "api_token_readonly": "",  # 只读 Token（查询类接口可用，写操作被拒）
        "must_change_password": False,
        "metrics_public": True,    # /metrics 是否允许免登录抓取
    },
    "webhook": {
        "secret": "",
    },
    # 二次验证（TOTP）。与口令 / API Token 叠加，用于高敏感动作的二次确认。
    # scope 决定哪些场景需要动态口令；secret 为 Base32 密钥（敏感，打码显示）。
    "twofa": {
        "enabled": False,
        "secret": "",
        "issuer": "动态云防火墙",
        "account": "admin",
        "window": 1,          # 允许前后漂移的周期数（±1 => 容许 30s 时钟偏差）
        "scope": {
            "web_login": False,  # 网页登录时校验动态口令
            "webhook": False,    # /webhook/ip-change 需携带 X-2FA-Code
            "dingtalk": False,   # 钉钉机器人验证动态口令
        },
        "dingtalk_all": False,       # true=所有机器人指令都要口令；false=仅放行/删除/续期
        "webhook_skip_private": False,  # true=来自内网/回环的 Webhook 免 2FA
        "webhook_trusted_cidrs": [],    # 额外白名单网段（这些来源免 2FA）
    },
    "notify": {
        "enabled": False,          # 规则变更是否推送钉钉
        "on_allow": True,
        "on_delete": True,
        "on_renew": True,
        "on_expire": False,
        "title": "防火墙规则变更",
        # 可用占位符见 app/core/notifier.py
        "template": "{action} {ip} {port}（{protocol}）· 实例 {instance} · 操作人 {actor}",
    },
    "geo": {
        "enabled": True,
        "endpoint": "http://ip-api.com/json/{ip}?lang=zh-CN&fields=status,country,regionName,city,isp",
        "timeout": 4,
    },
    "dingtalk": {
        "enabled": True,
        "app_id": "",
        "agent_id": "",
        "client_id": "",
        "client_secret": "",
        "robot_code": "",
        "mode": "stream",           # stream（免公网，推荐） | callback（HTTP 回调）
        "callback_token": "",
        "callback_aes_key": "",
        "allowed_conversations": [],  # 为空表示不限制群
        "allowed_users": [],          # 为空表示不限制发送人
        "require_at": True,           # 仅处理 @机器人 的消息
        "default_protocol": "tcp",
        "default_ports": "ALL",       # 未指定端口时的放行范围
        "notify_on_webhook": False,   # webhook 放行成功后是否推送钉钉通知
        "notify_conversation_id": "",
        # 注意：默认有效期只有 rules_policy.default_ttl_minutes 一处，
        # 早期版本在 dingtalk 下也放了一份，已废弃（会造成改了一处不生效）
    },
    "public_ip": {
        "enabled": False,             # 开启后由定时任务把「当前公网 IP」同步进防火墙
        "ports": "ALL",
        "protocol": "tcp",
        "ttl_minutes": 0,             # 0 = 跟随 rules_policy.default_ttl_minutes；-1 = 永久放行（不写到期时间）
        "tag": "公网IP",
        "notify_on_change": True,     # 公网 IP 变化并自动跟随成功后，是否推一条钉钉通知
    },
    "aliyun": {
        "enabled": True,
        "provider": "swas",           # swas（轻量应用服务器） | ecs（ECS 安全组）
        "access_key_id": "",
        "access_key_secret": "",
        "region_id": "cn-hangzhou",
        "endpoint": "",               # 留空则自动拼 swas.{region}.aliyuncs.com
        "instances": [],              # [{instance_id, name, enabled, security_group_id}]
    },
    "rules_policy": {
        "auto_cleanup_old_ip": True,   # 新 IP 放行时清理旧的同名旧 IP 规则
        "cleanup_days": 7,
        "max_rules_per_instance": 100,
        "default_ttl_minutes": DEFERRED_TTL,
        "protect_manual_rules": True,  # 自动清理不动手工添加的规则
        "expire_notice_minutes": 10,   # 到期前多少分钟推送提醒，0 = 关闭
    },
    "cron": {
        "enabled": True,
        "timezone": "Asia/Shanghai",
        "jobs": {
            "cleanup_expired": {"enabled": True, "cron": "*/5 * * * *", "name": "过期规则清理"},
            "sync_cloud_rules": {"enabled": True, "cron": "*/15 * * * *", "name": "云端规则同步"},
            "cleanup_stale_ip": {"enabled": True, "cron": "0 3 * * *", "name": "旧 IP 规则清理"},
            "prune_logs": {"enabled": True, "cron": "30 4 * * *", "name": "日志归档清理"},
            "health_probe": {"enabled": True, "cron": "*/10 * * * *", "name": "云凭证健康检查"},
            "daily_report": {"enabled": True, "cron": "0 9 * * *", "name": "每日状态推送"},
            "expiring_notice": {"enabled": True, "cron": "*/5 * * * *", "name": "到期前提醒"},
            # 动态 IP 场景下「每天一次」太慢（尤其主机夜间关机时），默认改成每小时：
            # IP 未变时只做一次本地更新，不调用云端接口，开销可忽略。
            "sync_public_ip": {"enabled": True, "cron": "0 * * * *", "name": "公网 IP 自动放行"},
            "backup_db": {"enabled": True, "cron": "0 5 * * *", "name": "数据库备份"},
        },
    },
    "logging": {
        "keep_days": 30,
        "backup_keep": 7,             # 保留最近 N 份数据库备份
    },
}


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _read_env_file() -> Dict[str, str]:
    env: Dict[str, str] = {}
    if not os.path.exists(ENV_FILE):
        return env
    try:
        with open(ENV_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    except OSError as exc:
        logger.warning(f"读取 .env 失败: {exc}")
    return env


def _resolve_env(value: Any, env: Dict[str, str]) -> Any:
    if isinstance(value, dict):
        return {k: _resolve_env(v, env) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_env(v, env) for v in value]
    if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
        return env.get(value[2:-1], os.environ.get(value[2:-1], ""))
    return value


def _legacy_to_settings() -> Optional[Dict[str, Any]]:
    """把根目录 config/config.yaml + .env 转成新版 settings 结构。"""
    if not os.path.exists(CONFIG_YAML):
        return None
    try:
        with open(CONFIG_YAML, "r", encoding="utf-8") as f:
            legacy = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError) as exc:
        logger.warning(f"导入旧配置失败: {exc}")
        return None

    env = _read_env_file()
    legacy = _resolve_env(legacy, env)
    out: Dict[str, Any] = {}

    webhook = legacy.get("webhook") or {}
    if webhook:
        out["server"] = {"port": int(webhook.get("port") or 5050)}
        out["webhook"] = {"secret": webhook.get("secret_key") or ""}

    # 阿里云轻量应用服务器（SWAS）配置
    providers = legacy.get("cloud_providers") or {}
    swas = providers.get("aliyun_lighthouse") or {}
    if swas:
        instances: List[Dict[str, Any]] = []
        for item in swas.get("instances") or []:
            if item.get("id"):
                instances.append(
                    {
                        "instance_id": item["id"],
                        "name": item.get("description") or item["id"],
                        "enabled": True,
                    }
                )
        out["aliyun"] = {
            "enabled": bool(swas.get("enabled", True)),
            "access_key_id": swas.get("access_key_id") or env.get("ALIYUN_ACCESS_KEY_ID", ""),
            "access_key_secret": swas.get("access_key_secret") or env.get("ALIYUN_ACCESS_KEY_SECRET", ""),
            "region_id": swas.get("region") or "cn-hangzhou",
            "instances": instances,
        }

    rules = legacy.get("rules") or {}
    if rules:
        out["rules_policy"] = {
            "auto_cleanup_old_ip": bool(rules.get("auto_cleanup_old_ip", True)),
            "cleanup_days": int(rules.get("cleanup_days") or 7),
            "max_rules_per_instance": int(rules.get("max_rules_per_group") or 100),
        }
    return out


class Settings:
    """对外暴露的配置对象，内部始终读取合并后的快照。"""

    def __init__(self) -> None:
        self._cache: Dict[str, Any] = {}
        self._migrated = False

    # ------------------------------------------------------------------ 文件镜像
    @staticmethod
    def _read_json_file() -> Optional[Dict[str, Any]]:
        if not os.path.exists(CONFIG_JSON):
            return None
        try:
            with open(CONFIG_JSON, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) and data else None
        except (OSError, ValueError) as exc:
            logger.warning(f"读取 {CONFIG_JSON} 失败: {exc}")
            return None

    @staticmethod
    def _write_json_file(data: Dict[str, Any]) -> None:
        """把配置同步一份到 data/settings.json，方便查看与备份。"""
        try:
            os.makedirs(os.path.dirname(CONFIG_JSON), exist_ok=True)
            tmp = CONFIG_JSON + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, CONFIG_JSON)
            try:
                os.chmod(CONFIG_JSON, stat.S_IRUSR | stat.S_IWUSR)  # 仅属主可读写
            except OSError:
                pass
        except OSError as exc:
            logger.warning(f"写出 {CONFIG_JSON} 失败: {exc}")

    # ------------------------------------------------------------------ 加载
    def load(self) -> Dict[str, Any]:
        data = store.get_setting("app_settings")
        if not data:
            # 1) 优先复用 data/settings.json（可手工编辑后重启生效）
            file_data = self._read_json_file()
            if file_data:
                data = _deep_merge(DEFAULTS, file_data)
                store.set_setting("app_settings", data)
                logger.info("已从 data/settings.json 载入配置")
            else:
                # 2) 首次运行：从旧版 config/config.yaml + .env 迁移
                legacy = _legacy_to_settings()
                data = _deep_merge(DEFAULTS, legacy or {})
                self._migrated = bool(legacy)
                if legacy:
                    logger.info("已从 config/config.yaml 导入旧配置到 Web 配置中心")

        merged = _deep_merge(DEFAULTS, data)
        if self._apply_migrations(merged):
            store.set_setting("app_settings", merged)
        if not merged["server"].get("secret_key"):
            merged["server"]["secret_key"] = _uid()
        if not merged["server"].get("api_token"):
            merged["server"]["api_token"] = _uid()[:24]
        if not merged["server"].get("api_token_readonly"):
            merged["server"]["api_token_readonly"] = _uid()[:24]
        # 只要还在用默认口令，就强制要求改密（改完由 change_password 置回 False）
        if str(merged["server"].get("admin_password") or "") in ("", "admin123"):
            merged["server"]["must_change_password"] = True
        stored_server = (data.get("server") or {}) if isinstance(data, dict) else {}
        if (
            not stored_server.get("api_token")
            or not stored_server.get("secret_key")
            or not stored_server.get("api_token_readonly")
        ):
            store.set_setting("app_settings", merged)
        self._cache = merged
        self._write_json_file(merged)
        return merged

    def reload(self) -> Dict[str, Any]:
        return self.load()

    # ------------------------------------------------------------------ 迁移
    def _apply_migrations(self, merged: Dict[str, Any]) -> bool:
        """
        一次性配置迁移。

        每条迁移执行后写入 ``migrations`` 标记；之后即使用户把值改回去，
        也不会被再次覆盖（标记跟着配置一起持久化）。
        返回 True 表示有改动需要落库。
        """
        marks = dict(merged.get("migrations") or {})
        dirty = False

        # 1) 公网 IP 同步：旧默认「每天 04:00」→ 新默认「每小时」。
        #    动态 IP 场景下每天一次太慢；主机夜间关机时更是天天错过。
        if not marks.get("public_ip_interval"):
            job = ((merged.get("cron") or {}).get("jobs") or {}).get("sync_public_ip")
            if isinstance(job, dict) and job.get("cron") == "0 4 * * *":
                job["cron"] = "0 * * * *"
            marks["public_ip_interval"] = True
            dirty = True

        if dirty:
            merged["migrations"] = marks
        return dirty

    @property
    def data(self) -> Dict[str, Any]:
        if not self._cache:
            self.load()
        return self._cache

    # ------------------------------------------------------------------ 读取
    def get(self, path: str, default: Any = None) -> Any:
        node: Any = self.data
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node if node is not None else default

    def section(self, name: str) -> Dict[str, Any]:
        return dict(self.get(name, {}) or {})

    def masked(self) -> Dict[str, Any]:
        """返回给前端的安全副本：密钥类字段打码。"""
        data = copy.deepcopy(self.data)
        for section, keys in {
            "dingtalk": ["client_secret", "callback_aes_key"],
            "aliyun": ["access_key_secret"],
            "server": ["admin_password", "api_token", "api_token_readonly"],
            "webhook": ["secret"],
            "twofa": ["secret"],
        }.items():
            for key in keys:
                value = str((data.get(section) or {}).get(key) or "")
                (data.setdefault(section, {}))[key] = ("*" * 8 + value[-4:]) if value else ""
        return data

    # ------------------------------------------------------------------ 写入
    def update(self, patch: Dict[str, Any]) -> Dict[str, Any]:
        """
        局部更新配置。patch 结构与完整配置一致，深层合并；
        若某字段传空字符串则视为不修改（前端打码回传时避免误清空密钥）。
        """
        current = copy.deepcopy(self.data)
        cleaned = self._strip_placeholders(patch or {})
        merged = _deep_merge(current, cleaned)
        store.set_setting("app_settings", merged)
        self._cache = _deep_merge(DEFAULTS, merged)
        self._write_json_file(self._cache)
        return self._cache

    def _strip_placeholders(self, patch: Dict[str, Any]) -> Dict[str, Any]:
        sensitive = {
            "client_secret",
            "callback_aes_key",
            "access_key_secret",
            "admin_password",
            "api_token",
            "api_token_readonly",
            "secret",
        }
        out: Dict[str, Any] = {}
        for key, value in patch.items():
            if isinstance(value, dict):
                nested = self._strip_placeholders(value)
                if nested:
                    out[key] = nested
            elif key in sensitive and (value == "" or (isinstance(value, str) and value.startswith("*"))):
                continue
            else:
                out[key] = value
        return out

    # ------------------------------------------------------------------ 便捷
    def enabled_instances(self) -> List[Dict[str, Any]]:
        items = self.get("aliyun.instances", []) or []
        return [i for i in items if i.get("instance_id") and i.get("enabled", True)]

    def instance_name(self, instance_id: str) -> str:
        for item in self.get("aliyun.instances", []) or []:
            if item.get("instance_id") == instance_id:
                return item.get("name") or instance_id
        return instance_id

    def job_config(self, job_id: str) -> Dict[str, Any]:
        return dict((self.get("cron.jobs", {}) or {}).get(job_id, {}) or {})


settings = Settings()
