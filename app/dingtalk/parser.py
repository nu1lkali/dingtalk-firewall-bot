#!/usr/bin/env python3
"""
钉钉群消息指令解析。

支持的书写形式（@机器人 之后的部分）：

    1.2.3.4                     -> 放行该 IP 全部端口
    1.2.3.4 22                  -> 仅放行 TCP 22
    1.2.3.4:22                  -> 同上
    1.2.3.4 22,80,443           -> 多个端口
    1.2.3.4 8000-9000           -> 端口段
    1.2.3.4 53 udp              -> 指定协议
    放行 1.2.3.4 22             -> 带动词，等价
    1.2.3.4 1.2.3.5 22          -> 多个 IP 共用端口
    删除 1.2.3.4 / del 1.2.3.4:22
    列表 / 状态 / 我的IP / 帮助
"""

from __future__ import annotations

import ipaddress
import re
from typing import Any, Dict, List, Optional

IPV4_RE = re.compile(r"(?:\d{1,3}\.){3}\d{1,3}")
IPV6_RE = re.compile(r"(?:[0-9a-fA-F]{1,4}:){2,}[0-9a-fA-F:]*")

ALLOW_WORDS = {"放行", "添加", "通过", "开", "allow", "add", "open", "permit"}
DELETE_WORDS = {"删除", "移除", "删", "del", "delete", "remove", "rm"}
RENEW_WORDS = {"续期", "延期", "延长", "renew", "extend"}
HELP_WORDS = {"帮助", "help", "?", "？", "菜单", "说明", "命令"}
STATUS_WORDS = {"状态", "status", "概览", "统计"}
LIST_WORDS = {"列表", "list", "规则", "rules"}
MYIP_WORDS = {"我的ip", "myip", "本机ip", "我的地址"}

PROTOCOLS = {"tcp", "udp", "icmp", "all", "gre"}

# 续期时长：30m / 24h / 7d，不带单位按「小时」理解（`续期 1.2.3.4 24` = 24 小时）
DURATION_RE = re.compile(r"^(\d{1,4})\s*(m|min|分钟|h|h|小时|d|天)?$")


def _valid_ip(text: str) -> bool:
    try:
        ipaddress.ip_address(text)
        return True
    except ValueError:
        return False


def _extract_ips(text: str) -> List[str]:
    """从文本中抽取合法 IP（IPv4 优先，其次 IPv6）。"""
    found: List[str] = []
    for match in IPV4_RE.findall(text):
        if _valid_ip(match) and match not in found:
            found.append(match)
    if not found:
        for match in IPV6_RE.findall(text):
            if ":" in match and _valid_ip(match) and match not in found:
                found.append(match)
    return found


def _normalize_port_token(token: str) -> Optional[str]:
    """把 '22' / '22,80' / '8000-9000' / '22/22' 归一化为逗号分隔端口表达式。"""
    token = token.strip().strip(",，;；")
    if not token:
        return None
    if token.upper() in {"ALL", "*", "全部", "所有"}:
        return "ALL"
    parts: List[str] = []
    for piece in re.split(r"[,，]", token):
        piece = piece.strip()
        if not piece:
            continue
        if re.fullmatch(r"\d{1,5}", piece):
            if 1 <= int(piece) <= 65535:
                parts.append(str(int(piece)))
        elif re.fullmatch(r"\d{1,5}-\d{1,5}", piece):
            start, _, end = piece.partition("-")
            if 1 <= int(start) <= int(end) <= 65535:
                parts.append(f"{int(start)}-{int(end)}")
        elif re.fullmatch(r"\d{1,5}/\d{1,5}", piece):
            start, _, end = piece.partition("/")
            if start == end:
                parts.append(str(int(start)))
            elif 1 <= int(start) <= int(end) <= 65535:
                parts.append(f"{int(start)}-{int(end)}")
    return ",".join(parts) if parts else None


def parse(text: str) -> Dict[str, Any]:
    """解析指令，返回结构化结果。"""
    raw = (text or "").strip()
    # 去掉 @xxx 与首尾空白
    cleaned = re.sub(r"@\S+", " ", raw)
    cleaned = cleaned.replace("\u2005", " ").replace("\xa0", " ").strip()
    lowered = cleaned.lower()

    result: Dict[str, Any] = {
        "raw": raw,
        "intent": "unknown",
        "ips": [],
        "ports": "",
        "protocol": "",
        "minutes": 0,
        "error": "",
    }

    if not cleaned:
        result["intent"] = "help"
        return result

    tokens = cleaned.split()
    first = tokens[0].lower().strip("：:，,")

    if first in HELP_WORDS or lowered in HELP_WORDS:
        result["intent"] = "help"
        return result
    if first in STATUS_WORDS:
        result["intent"] = "status"
        return result
    if first in LIST_WORDS:
        result["intent"] = "list"
        return result
    if first in MYIP_WORDS:
        result["intent"] = "myip"
        return result

    intent = "allow"
    if first in DELETE_WORDS:
        intent = "delete"
        tokens = tokens[1:]
    elif first in RENEW_WORDS:
        intent = "renew"
        tokens = tokens[1:]
    elif first in ALLOW_WORDS:
        tokens = tokens[1:]

    body = " ".join(tokens)
    ips = _extract_ips(body)
    if not ips:
        result["intent"] = "unknown"
        result["error"] = "未识别到合法 IP 地址"
        return result

    # 续期时长：`续期 1.2.3.4 24h` / `续期 1.2.3.4 30m` / `续期 1.2.3.4 7d`
    minutes = 0
    if intent == "renew":
        for token in body.split():
            if any(ip in token for ip in ips):
                continue
            match = DURATION_RE.match(token.strip())
            if match:
                amount = int(match.group(1))
                unit = (match.group(2) or "h").lower()
                if unit in ("m", "min", "分钟"):
                    factor = 1
                elif unit in ("d", "天"):
                    factor = 1440
                else:
                    factor = 60
                minutes = amount * factor
                break
        result.update({"intent": intent, "ips": ips, "minutes": minutes})
        return result

    # 处理 ip:port 紧贴写法
    port_candidates: List[str] = []
    for ip in ips:
        pattern = re.escape(ip) + r"[:：](\d{1,5}(?:[,，\-/]\d{1,5})?)"
        m = re.search(pattern, body)
        if m:
            normalized = _normalize_port_token(m.group(1))
            if normalized:
                port_candidates.append(normalized)
            body = body.replace(m.group(0), " ")

    protocol = ""
    for token in body.split():
        low = token.lower().strip(",，")
        if low in PROTOCOLS:
            protocol = low
            continue
        if any(ip in token for ip in ips):
            continue
        normalized = _normalize_port_token(token)
        if normalized:
            port_candidates.append(normalized)

    ports = ""
    if port_candidates:
        merged: List[str] = []
        for candidate in port_candidates:
            if candidate == "ALL":
                merged = ["ALL"]
                break
            merged.extend([x for x in candidate.split(",") if x not in merged])
        ports = ",".join(merged)

    result.update(
        {
            "intent": intent,
            "ips": ips,
            "ports": ports,
            "protocol": protocol,
        }
    )
    return result


def help_text() -> str:
    return (
        "### 🤖 IP 放行助手\n\n"
        "**放行（不写端口 = 全端口）**\n"
        "- `1.2.3.4` → 放行该 IP 全部端口\n"
        "- `1.2.3.4 22` → 仅放行 TCP 22\n"
        "- `1.2.3.4 22,80,443` → 多个端口\n"
        "- `1.2.3.4 8000-9000` → 端口段\n"
        "- `1.2.3.4 53 udp` → 指定 UDP\n\n"
        "**其他指令**\n"
        "- `列表` 查看当前放行规则\n"
        "- `状态` 查看系统概览\n"
        "- `续期 1.2.3.4` 延长 24 小时（可写 `24h` / `30m` / `7d`）\n"
        "- `删除 1.2.3.4` 撤销该 IP 的放行\n"
        "- `我的IP` 返回你当前的出口 IP\n"
        "- `帮助` 显示本菜单"
    )
