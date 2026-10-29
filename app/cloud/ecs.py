#!/usr/bin/env python3
"""
阿里云 ECS 安全组防火墙封装。

与 `app/cloud/swas.py` 暴露**完全相同的方法签名**，因此 `rules_service` 不用改一行代码
就能在「轻量应用服务器防火墙」和「ECS 安全组」之间切换（设置里 aliyun.provider）。

两边的语义差异（已在本层抹平）：

| 概念 | SWAS | ECS 安全组 | 本层处理 |
|---|---|---|---|
| instance_id | 实例 ID | 当成安全组 ID（`sg-xxx`） | 直接用，配置时填安全组 ID |
| 规则身份 | RuleId | 新版有 SecurityGroupRuleId，老版没有 | 没有时合成 `proto|port|cidr` |
| TCP+UDP | 单条规则 | 必须拆成 tcp、udp 两条 | 建时拆、读时合 |
| 启用/禁用 | 原生支持 | 不支持（只有 accept/drop） | 抛 CloudError，上层降级为删除+快照 |
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from app.cloud.swas import (
    ALL_PORTS,
    CloudError,
    normalize_cidr,
    normalize_port_value,
    protocol_normalize,
)
from app.utils.logger import logger

_ECS_PROTOCOLS = {"TCP": "tcp", "UDP": "udp", "ICMP": "icmp", "GRE": "gre", "TCP+UDP": "tcp+udp"}

# 复合 rule_id 分隔符：tcp|22|1.2.3.4/32
_SEP = "|"


def _composite(proto: str, port: str, cidr: str) -> str:
    return _SEP.join([proto, port, cidr])


def _parse_composite(rule_id: str) -> Optional[Tuple[str, str, str]]:
    parts = str(rule_id or "").split(_SEP)
    if len(parts) != 3:
        return None
    return parts[0], parts[1], parts[2]


class EcsFirewall:
    """ECS 安全组操作。instance_id 参数即 security_group_id。"""

    provider = "aliyun_ecs"

    def __init__(
        self,
        access_key_id: str,
        access_key_secret: str,
        region_id: str = "cn-hangzhou",
        endpoint: str = "",
    ):
        if not access_key_id or not access_key_secret:
            raise CloudError("阿里云 AccessKey 未配置")
        self.access_key_id = access_key_id
        self.access_key_secret = access_key_secret
        self.region_id = region_id or "cn-hangzhou"
        self.endpoint = endpoint or f"ecs.{self.region_id}.aliyuncs.com"
        self._client = None

    # ------------------------------------------------------------------ 底层
    @property
    def client(self):
        if self._client is None:
            try:
                from alibabacloud_ecs20140526.client import Client
                from alibabacloud_tea_openapi import models as open_api_models
            except ImportError as exc:  # pragma: no cover
                raise CloudError(
                    "缺少阿里云 ECS SDK，请执行 pip install alibabacloud_ecs20140526"
                ) from exc
            config = open_api_models.Config(
                access_key_id=self.access_key_id,
                access_key_secret=self.access_key_secret,
            )
            config.endpoint = self.endpoint
            self._client = Client(config)
        return self._client

    def _models(self):
        from alibabacloud_ecs20140526 import models

        return models

    def _runtime(self):
        from alibabacloud_tea_util import models as util_models

        runtime = util_models.RuntimeOptions()
        runtime.connect_timeout = 10 * 1000
        runtime.read_timeout = 30 * 1000
        runtime.autoretry = True
        runtime.max_attempts = 3
        return runtime

    @staticmethod
    def _friendly_error(exc: Exception) -> str:
        for attr in ("message", "msg"):
            value = getattr(exc, attr, None)
            if value:
                code = getattr(exc, "code", "") or ""
                return f"{code} {value}".strip()
        return str(exc)

    # ------------------------------------------------------------------ 安全组
    def list_instances(self, page_size: int = 50) -> List[Dict[str, Any]]:
        """ECS 下「实例」= 安全组。"""
        models = self._models()
        request = models.DescribeSecurityGroupsRequest(region_id=self.region_id)
        request.page_size = page_size
        request.page_number = 1
        try:
            resp = self.client.describe_security_groups_with_options(request, self._runtime())
        except Exception as exc:  # noqa: BLE001
            raise CloudError(self._friendly_error(exc)) from exc

        body = getattr(resp, "body", None)
        out: List[Dict[str, Any]] = []
        for group in getattr(body, "security_groups", None) or []:
            out.append(
                {
                    "instance_id": getattr(group, "security_group_id", ""),
                    "name": getattr(group, "security_group_name", "") or getattr(group, "security_group_id", ""),
                    "status": "Available",
                    "public_ip": getattr(group, "description", "") or "",
                    "rule_count": getattr(group, "rule_count", 0),
                    "region_id": self.region_id,
                }
            )
        return out

    def _permissions(self, security_group_id: str) -> List[Dict[str, Any]]:
        models = self._models()
        request = models.DescribeSecurityGroupAttributeRequest(
            region_id=self.region_id,
            security_group_id=security_group_id,
            direction="ingress",
        )
        try:
            resp = self.client.describe_security_group_attribute_with_options(
                request, self._runtime()
            )
        except Exception as exc:  # noqa: BLE001
            raise CloudError(self._friendly_error(exc)) from exc

        body = getattr(resp, "body", None)
        perms = getattr(body, "permissions", None)
        items = getattr(perms, "permission", None) or []
        out: List[Dict[str, Any]] = []
        for item in items:
            cidr = getattr(item, "source_cidr_ip", "") or ""
            out.append(
                {
                    "rule_id": getattr(item, "security_group_rule_id", "") or "",
                    "protocol": (getattr(item, "ip_protocol", "") or "tcp").lower(),
                    "port": str(getattr(item, "port_range", "") or ALL_PORTS),
                    "cidr": cidr,
                    "ip": cidr.split("/")[0] if "/" in cidr else cidr,
                    "description": getattr(item, "description", "") or "",
                    "policy": (getattr(item, "policy", "") or "accept").lower(),
                    "priority": getattr(item, "priority", 1),
                }
            )
        return out

    def list_rules(self, instance_id: str) -> List[Dict[str, Any]]:
        """
        读安全组入方向规则。

        ECS 把 TCP+UDP 拆成两条，这里按 (port, cidr, 备注) 合并回一条逻辑规则，
        保证与 SWAS 视图一致——上层 `allow_ip` 的「已存在」判断才不会有偏差。
        """
        perms = self._permissions(instance_id)
        buckets: Dict[Tuple[str, str, str], List[Dict[str, Any]]] = {}
        for item in perms:
            if item["policy"] != "accept":
                continue  # drop 规则不是「放行」，不纳入白名单视图
            key = (item["port"], item["cidr"], item["description"])
            buckets.setdefault(key, []).append(item)

        out: List[Dict[str, Any]] = []
        for (port, cidr, desc), items in buckets.items():
            protos = {i["protocol"] for i in items}
            if protos == {"tcp", "udp"}:
                out.append(
                    {
                        "rule_id": _composite("tcp+udp", port, cidr),
                        "protocol": "tcp+udp",
                        "port": port,
                        "cidr": cidr,
                        "ip": cidr.split("/")[0] if "/" in cidr else cidr,
                        "description": desc,
                        "policy": "accept",
                        "instance_id": instance_id,
                        "instance_name": "",
                    }
                )
            else:
                for item in items:
                    merged = dict(item)
                    merged["rule_id"] = item["rule_id"] or _composite(
                        item["protocol"], item["port"], item["cidr"]
                    )
                    merged["instance_id"] = instance_id
                    merged["instance_name"] = ""
                    out.append(merged)
        return out

    # ------------------------------------------------------------------ 写操作
    def _authorize(self, instance_id: str, cidr: str, protocol: str, port: str, remark: str) -> None:
        models = self._models()
        request = models.AuthorizeSecurityGroupRequest(
            region_id=self.region_id,
            security_group_id=instance_id,
            ip_protocol=protocol,
            port_range=port,
            source_cidr_ip=cidr,
            description=(remark or "")[:64] or None,
            policy="accept",
            priority=1,
        )
        try:
            self.client.authorize_security_group_with_options(request, self._runtime())
        except Exception as exc:  # noqa: BLE001
            raise CloudError(self._friendly_error(exc)) from exc

    def _revoke(self, instance_id: str, cidr: str, protocol: str, port: str) -> None:
        models = self._models()
        request = models.RevokeSecurityGroupRequest(
            region_id=self.region_id,
            security_group_id=instance_id,
            ip_protocol=protocol,
            port_range=port,
            source_cidr_ip=cidr,
        )
        try:
            self.client.revoke_security_group_with_options(request, self._runtime())
        except Exception as exc:  # noqa: BLE001
            raise CloudError(self._friendly_error(exc)) from exc

    def create_rule(
        self,
        instance_id: str,
        cidr: str,
        protocol: str,
        port: str,
        remark: str = "",
    ) -> Dict[str, Any]:
        proto = protocol_normalize(protocol)
        port = normalize_port_value(port)
        cidr, _ = normalize_cidr(cidr)
        if proto == "TCP+UDP":
            self._authorize(instance_id, cidr, "tcp", port, remark)
            self._authorize(instance_id, cidr, "udp", port, remark)
            rule_id = _composite("tcp+udp", port, cidr)
        else:
            lower = _ECS_PROTOCOLS.get(proto, "tcp")
            self._authorize(instance_id, cidr, lower, port, remark)
            rule_id = _composite(lower, port, cidr)
        return {"rule_id": rule_id, "request_id": ""}

    def delete_rules(self, instance_id: str, rule_ids: List[str]) -> bool:
        ids = [r for r in (rule_ids or []) if r]
        if not ids:
            return False
        ok = False
        for rule_id in ids:
            parsed = _parse_composite(rule_id)
            if not parsed:
                # 拿到的是云端原生 rule id，反查一次再删
                target = next(
                    (r for r in self.list_rules(instance_id) if r["rule_id"] == rule_id), None
                )
                if not target:
                    raise CloudError(
                        f"InvalidSecurityGroupRule.NotFound code: 404, rule {rule_id} not found"
                    )
                parsed = (target["protocol"], target["port"], target["cidr"])
            proto, port, cidr = parsed
            if proto == "tcp+udp":
                self._revoke(instance_id, cidr, "tcp", port)
                self._revoke(instance_id, cidr, "udp", port)
            else:
                self._revoke(instance_id, cidr, proto, port)
            ok = True
        return ok

    def enable_rule(self, instance_id: str, rule_id: str, cidr: str = "", remark: str = "") -> bool:
        raise CloudError("ECS 安全组不支持「启用」语义，请使用恢复/重建")

    def disable_rule(self, instance_id: str, rule_id: str, remark: str = "") -> bool:
        raise CloudError("ECS 安全组不支持「禁用」语义，请使用删除")

    def modify_rule(
        self,
        instance_id: str,
        rule_id: str,
        cidr: str,
        protocol: str,
        port: str,
        remark: str = "",
    ) -> bool:
        """安全组没有「改一条」的原生接口，统一走删除 + 重建。"""
        self.delete_rules(instance_id, [rule_id])
        self.create_rule(instance_id, cidr, protocol, port, remark)
        return True

    def ping(self) -> Dict[str, Any]:
        groups = self.list_instances(page_size=5)
        return {"ok": True, "instance_count": len(groups), "provider": "ecs"}


def _self_check() -> None:  # pragma: no cover - 仅手工调试用
    logger.info("ECS provider 已就绪（未发起真实调用）")
