#!/usr/bin/env python3
"""公网 IP 探测。"""

from __future__ import annotations

import ipaddress
import re
from typing import Dict, List

import requests

SOURCES: List[Dict[str, str]] = [
    {"name": "ipify", "url": "https://api.ipify.org?format=json", "type": "json", "key": "ip"},
    {"name": "ipinfo", "url": "https://ipinfo.io/ip", "type": "text"},
    {"name": "ipip", "url": "https://myip.ipip.net", "type": "text"},
    {"name": "ifconfig.me", "url": "https://ifconfig.me/ip", "type": "text"},
    {"name": "ipw.cn", "url": "https://4.ipw.cn", "type": "text"},
]

IP_PATTERN = re.compile(r"(?:\d{1,3}\.){3}\d{1,3}")


def _valid(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


def get_public_ip(timeout: int = 6) -> Dict[str, str]:
    """依次尝试多个服务，返回第一个可用结果。"""
    errors = []
    for source in SOURCES:
        try:
            resp = requests.get(
                source["url"],
                timeout=timeout,
                headers={"User-Agent": "curl/8.0 dynamic-cloud-firewall"},
            )
            text = resp.text.strip()
            if source["type"] == "json":
                value = (resp.json() or {}).get(source.get("key", "ip"), "")
            else:
                match = IP_PATTERN.search(text)
                value = match.group(0) if match else text.split()[-1] if text else ""
            value = (value or "").strip()
            if _valid(value):
                return {"ip": value, "source": source["name"]}
            errors.append(f"{source['name']}: 返回无效内容")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{source['name']}: {exc}")
    return {"ip": "", "source": "", "errors": "; ".join(errors)}
