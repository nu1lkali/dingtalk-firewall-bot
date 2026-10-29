#!/usr/bin/env python3
"""
IP 归属地查询（带本地缓存）。

为什么不内置离线库：ip2region 的二进制库有十几 MB，塞进仓库不合适。
这里默认走 ip-api.com 的免费接口（无需 Key、支持中文），结果写入 `ip_geo` 表长期缓存，
所以每个 IP 只会查一次；查不到就静默降级，不影响任何主流程。

需要完全离线可以：把 `geo.endpoint` 指向自建服务，或直接关掉 `geo.enabled`。
"""

from __future__ import annotations

import ipaddress
from typing import Any, Dict, Iterable, List

import requests

from app.settings import settings
from app.store import store
from app.utils.logger import logger

DEFAULT_ENDPOINT = "http://ip-api.com/json/{ip}?lang=zh-CN&fields=status,country,regionName,city,isp"


def _is_public(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(str(ip).strip())
    except ValueError:
        return False
    return not (addr.is_private or addr.is_loopback or addr.is_reserved or addr.is_multicast)


def _fetch(ip: str) -> Dict[str, Any]:
    endpoint = settings.get("geo.endpoint") or DEFAULT_ENDPOINT
    timeout = int(settings.get("geo.timeout") or 4)
    url = endpoint.replace("{ip}", ip)
    resp = requests.get(url, timeout=timeout, headers={"User-Agent": "dynamic-cloud-firewall"})
    data = resp.json() or {}
    if str(data.get("status", "")).lower() == "fail":
        return {}
    return {
        "country": data.get("country") or "",
        "region": data.get("regionName") or data.get("region") or "",
        "city": data.get("city") or "",
        "isp": data.get("isp") or "",
    }


def lookup(ip: str, force: bool = False) -> Dict[str, Any]:
    """单个 IP 归属地；失败返回 {}。"""
    ip = str(ip or "").strip()
    if not ip or not _is_public(ip):
        return {}
    if not settings.get("geo.enabled", True):
        return {}

    if not force:
        cached = store.get_geo(ip)
        if cached:
            return {
                "ip": ip,
                "country": cached.get("country", ""),
                "region": cached.get("region", ""),
                "city": cached.get("city", ""),
                "isp": cached.get("isp", ""),
            }

    try:
        info = _fetch(ip)
    except Exception as exc:  # noqa: BLE001 - 归属地只是锦上添花，失败就跳过
        logger.debug(f"IP 归属地查询失败 {ip}: {exc}")
        return {}
    if not info:
        return {}
    store.set_geo(ip, info)
    return {"ip": ip, **info}


def lookup_many(ips: Iterable[str], limit: int = 30) -> Dict[str, Dict[str, Any]]:
    """
    批量查询（先吃缓存，只对没缓存的发请求）。

    limit 限制单次最多查几个新 IP，避免一次请求打爆免费接口的限流。
    """
    ips = [str(i or "").strip() for i in ips]
    ips = [i for i in ips if i and _is_public(i)]
    if not ips:
        return {}

    cached = store.get_geo_many(ips)
    out: Dict[str, Dict[str, Any]] = {}
    pending: List[str] = []
    for ip in ips:
        row = cached.get(ip)
        if row:
            out[ip] = {
                "ip": ip,
                "country": row.get("country", ""),
                "region": row.get("region", ""),
                "city": row.get("city", ""),
                "isp": row.get("isp", ""),
            }
        elif ip not in pending:
            pending.append(ip)

    if not settings.get("geo.enabled", True):
        return out

    for ip in pending[: max(0, limit)]:
        info = lookup(ip)
        if info:
            out[ip] = info
    return out


def describe(info: Dict[str, Any]) -> str:
    """把归属地拼成一句短文本，例如「浙江 杭州 · 电信」。"""
    if not info:
        return ""
    region = " ".join(x for x in (info.get("region", ""), info.get("city", "")) if x)
    parts = [p for p in (region or info.get("country", ""), info.get("isp", "")) if p]
    return " · ".join(parts)
