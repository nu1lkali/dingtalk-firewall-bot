#!/usr/bin/env python3
"""
云端客户端工厂：按 `aliyun.provider` 返回 SWAS 或 ECS 实现。

两个实现的方法签名完全一致，`rules_service` 只依赖 `build_client()`，
切换 provider 不需要改业务代码。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from app.cloud.ecs import EcsFirewall
from app.cloud.swas import CloudError, SwasFirewall

PROVIDERS = ("swas", "ecs")


def build_client(aliyun_config: Optional[Dict[str, Any]] = None):
    from app.settings import settings

    cfg = aliyun_config or settings.section("aliyun")
    provider = str(cfg.get("provider") or "swas").lower()
    if provider not in PROVIDERS:
        raise CloudError(f"不支持的云厂商类型: {provider}（可选 {'/'.join(PROVIDERS)}）")

    kwargs = {
        "access_key_id": cfg.get("access_key_id", ""),
        "access_key_secret": cfg.get("access_key_secret", ""),
        "region_id": cfg.get("region_id") or "cn-hangzhou",
        "endpoint": cfg.get("endpoint") or "",
    }
    return EcsFirewall(**kwargs) if provider == "ecs" else SwasFirewall(**kwargs)


def provider_name() -> str:
    from app.settings import settings

    return str(settings.get("aliyun.provider") or "swas").lower()
