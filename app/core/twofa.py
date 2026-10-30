#!/usr/bin/env python3
"""
二次验证（TOTP / RFC 6238）模块。

设计目标：与现有 Token / 口令鉴权叠加一层「动态口令」，用于对高敏感动作做二次确认。
* 密钥可自动生成，也可由管理员自定义（Base32）。
* 用 otpauth:// 链接生成二维码，供 1Password / Google Authenticator 等客户端扫描绑定。
* 通过 settings.twofa 管理开关与适用范围（网页登录 / Webhook / 钉钉机器人）。
* 纯标准库实现 TOTP，QR 仅依赖可选的 segno（缺失时降级为只返回 otpauth 链接）。

本模块只负责「算法 + 配置读取 + 口令校验」，不直接处理 HTTP，
具体接口在 app/web/app.py，机器人集在 app/dingtalk/bot.py。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import re
import secrets
import struct
import time
from typing import Any, Dict, List, Tuple
from urllib.parse import quote, urlencode

# --------------------------------------------------------------------------- 常量
DIGITS = 6
PERIOD = 30          # 每个口令有效期（秒）
DEFAULT_WINDOW = 1   # 允许前后漂移的周期数（±1 => 最多容许 30s 时钟偏差）
DEFAULT_ISSUER = "动态云防火墙"

#: 可配置的适用范围
SCOPES = ("web_login", "webhook", "dingtalk")

_B32_ALPHABET = re.compile(r"^[A-Z2-7]+$")

#: 机器人消息里显式携带动态口令：code=123456 / otp:123456 / 验证码 123456
_CODE_RE = re.compile(r"(?:code|otp|totp|验证码)\s*[=:：]?\s*(\d{6})", re.IGNORECASE)
#: 也支持 #123456 的简写
_HASH_CODE_RE = re.compile(r"#(\d{6})(?!\d)")


# --------------------------------------------------------------------------- 密钥
def generate_secret(length_bytes: int = 20) -> str:
    """生成 Base32 密钥（20 字节 = 160 bit，兼容所有认证器）。"""
    return base64.b32encode(secrets.token_bytes(length_bytes)).decode("ascii").rstrip("=")


def normalize_secret(secret: str) -> str:
    """去空格、转大写、补齐 Base32 填充位。"""
    text = re.sub(r"\s+", "", str(secret or "")).upper().rstrip("=")
    pad = (-len(text)) % 8
    return text + ("=" * pad)


def is_valid_secret(secret: str) -> bool:
    text = re.sub(r"\s+", "", str(secret or "")).upper().rstrip("=")
    if not text or not _B32_ALPHABET.match(text):
        return False
    try:
        base64.b32decode(normalize_secret(text), casefold=True)
        return True
    except Exception:  # noqa: BLE001
        return False


def mask_secret(secret: str) -> str:
    """给前端展示用的掩码，例如 ``••••••ABCD``（只露最后 4 位）。"""
    text = re.sub(r"\s+", "", str(secret or "")).upper().rstrip("=")
    if not text:
        return ""
    tail = text[-4:] if len(text) > 4 else text
    return "•" * min(len(text) - len(tail), 8) + tail


# --------------------------------------------------------------------------- TOTP
def _hotp(key: bytes, counter: int, digits: int = DIGITS) -> str:
    msg = struct.pack(">Q", counter)
    digest = hmac.new(key, msg, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    chunk = digest[offset : offset + 4]
    value = struct.unpack(">I", chunk)[0] & 0x7FFFFFFF
    return str(value % (10 ** digits)).zfill(digits)


def totp(secret: str, at: float | None = None, digits: int = DIGITS, period: int = PERIOD) -> str:
    """根据密钥与时间计算当前动态口令。"""
    if not is_valid_secret(secret):
        return ""
    key = base64.b32decode(normalize_secret(secret), casefold=True)
    counter = int((time.time() if at is None else at) // period)
    return _hotp(key, counter, digits)


# 语义别名：网页/日志里更易读
current_code = totp


def verify(
    secret: str,
    code: str,
    window: int = DEFAULT_WINDOW,
    digits: int = DIGITS,
    period: int = PERIOD,
) -> bool:
    """校验动态口令，允许前后 ``window`` 个周期的时钟漂移。"""
    if not is_valid_secret(secret):
        return False
    candidate = re.sub(r"\s+", "", str(code or ""))
    if not candidate.isdigit() or len(candidate) != digits:
        return False
    key = base64.b32decode(normalize_secret(secret), casefold=True)
    counter = int(time.time() // period)
    for offset in range(-abs(int(window)), abs(int(window)) + 1):
        if hmac.compare_digest(_hotp(key, counter + offset, digits), candidate):
            return True
    return False


def seconds_remaining(period: int = PERIOD) -> int:
    return period - int(time.time()) % period


# --------------------------------------------------------------------------- 二维码
def otpauth_uri(
    secret: str,
    account: str = "admin",
    issuer: str = DEFAULT_ISSUER,
    digits: int = DIGITS,
    period: int = PERIOD,
) -> str:
    """生成认证器可识别的 otpauth:// 链接。"""
    if not is_valid_secret(secret):
        return ""
    label = quote(f"{issuer}:{account or 'admin'}", safe="")
    params = {
        "secret": re.sub(r"\s+", "", secret).upper().rstrip("="),
        "issuer": issuer or DEFAULT_ISSUER,
        "algorithm": "SHA1",
        "digits": str(digits),
        "period": str(period),
    }
    return f"otpauth://totp/{label}?" + urlencode(params, quote_via=quote)


def qr_svg(uri: str, scale: int = 6) -> str:
    """
    把 otpauth 链接渲染为内联 SVG；segno 缺失时返回空串（前端降级显示链接）。

    omitsize=True 是关键：segno 默认只输出 width/height（如 198x198）而**不带 viewBox**，
    前端 CSS 把它撑成 100% 填充容器时不会等比缩放，只会把右下角裁掉一大块（二维码显示不全）。
    带上 viewBox 后，SVG 才会随容器自适应缩放。
    """
    if not uri:
        return ""
    try:
        import segno  # type: ignore
    except Exception:  # noqa: BLE001
        return ""
    qr = segno.make(uri, error="m")
    return qr.svg_inline(
        scale=scale, border=2, dark="#0f172a", light="#ffffff", omitsize=True
    )


# --------------------------------------------------------------------------- 配置
def config() -> Dict[str, Any]:
    from app.settings import settings

    return dict(settings.get("twofa", {}) or {})


def secret() -> str:
    return str(config().get("secret") or "")


def is_required(scope: str) -> bool:
    """判断某个场景是否需要动态口令（总开关 + 已配置密钥 + 该场景开关）。"""
    cfg = config()
    if not cfg.get("enabled"):
        return False
    if not is_valid_secret(cfg.get("secret") or ""):
        return False
    scopes = cfg.get("scope") or {}
    return bool(scopes.get(scope))


def window() -> int:
    try:
        return abs(int(config().get("window", DEFAULT_WINDOW)))
    except (TypeError, ValueError):
        return DEFAULT_WINDOW


def dingtalk_all() -> bool:
    """机器人严格模式：true 表示所有指令都要口令，false 只拦变更类指令。"""
    return bool(config().get("dingtalk_all"))


# --------------------------------------------------------------------------- 来源白名单（Webhook 免 2FA）
def _safe_ip(text: str):
    try:
        return ipaddress.ip_address(str(text or "").strip())
    except ValueError:
        return None


def is_internal_ip(ip: str) -> bool:
    """回环 / 私有网段（含 10/8、172.16/12、192.168/16、fc00::/7 等）。"""
    addr = _safe_ip(ip)
    if addr is None:
        return False
    return bool(addr.is_loopback or addr.is_private)


def ip_in_cidrs(ip: str, cidrs) -> bool:
    addr = _safe_ip(ip)
    if addr is None:
        return False
    for entry in cidrs or []:
        try:
            net = ipaddress.ip_network(str(entry).strip(), strict=False)
        except ValueError:
            continue
        if addr.version == net.version and addr in net:
            return True
    return False


def normalize_cidrs(items) -> Tuple[List[str], List[str]]:
    """校验并清洗网段白名单，返回 (合法, 非法)。"""
    cleaned: List[str] = []
    invalid: List[str] = []
    for item in items or []:
        text = str(item).strip()
        if not text:
            continue
        try:
            ipaddress.ip_network(text, strict=False)
            cleaned.append(text)
        except ValueError:
            invalid.append(text)
    return cleaned, invalid


def webhook_client_trusted(ip: str) -> bool:
    """判断该来源是否可跳过 Webhook 的 2FA（白名单网段 / 内网开关）。"""
    cfg = config()
    if ip_in_cidrs(ip, cfg.get("webhook_trusted_cidrs") or []):
        return True
    if cfg.get("webhook_skip_private") and is_internal_ip(ip):
        return True
    return False


def status(*, include_uri: bool = False) -> Dict[str, Any]:
    """返回给前端的 2FA 状态（不含明文密钥）。"""
    cfg = config()
    raw = str(cfg.get("secret") or "")
    payload: Dict[str, Any] = {
        "enabled": bool(cfg.get("enabled")),
        "has_secret": is_valid_secret(raw),
        "secret_masked": mask_secret(raw),
        "issuer": cfg.get("issuer") or DEFAULT_ISSUER,
        "account": cfg.get("account") or "admin",
        "window": window(),
        "scope": {
            "web_login": bool((cfg.get("scope") or {}).get("web_login")),
            "webhook": bool((cfg.get("scope") or {}).get("webhook")),
            "dingtalk": bool((cfg.get("scope") or {}).get("dingtalk")),
        },
        "dingtalk_all": bool(cfg.get("dingtalk_all")),
        "webhook_skip_private": bool(cfg.get("webhook_skip_private")),
        "webhook_trusted_cidrs": list(cfg.get("webhook_trusted_cidrs") or []),
        "qrcode_available": _segno_available(),
    }
    if include_uri and payload["has_secret"]:
        uri = otpauth_uri(
            raw,
            account=payload["account"],
            issuer=payload["issuer"],
        )
        payload["otpauth_uri"] = uri
        payload["qr_svg"] = qr_svg(uri)
    return payload


def _segno_available() -> bool:
    try:
        import segno  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


# --------------------------------------------------------------------------- 机器人口令提取
def extract_code(text: str) -> Tuple[str, str]:
    """
    从机器人消息里取出动态口令，并从文本中剥离，返回 ``(code, cleaned_text)``。

    支持 ``1.2.3.4 22 code=123456`` / ``放行 1.2.3.4 #123456`` 等写法。
    """
    raw = text or ""
    match = _CODE_RE.search(raw) or _HASH_CODE_RE.search(raw)
    if not match:
        return "", raw
    code = match.group(1)
    cleaned = (raw[: match.start()] + " " + raw[match.end() :]).strip()
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    return code, cleaned
