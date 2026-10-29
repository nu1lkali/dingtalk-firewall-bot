#!/usr/bin/env python3
"""
阿里云轻量应用服务器（SWAS-OPEN 2020-06-01）防火墙封装。

官方 SDK：alibabacloud_swas_open20200601
Endpoint：swas.{region}.aliyuncs.com
相关接口：ListFirewallRules / CreateFirewallRules / DeleteFirewallRules / ModifyFirewallRule(视版本)
"""

from __future__ import annotations

import ipaddress
import re
from typing import Any, Dict, List, Optional, Tuple

from app.utils.logger import logger


class CloudError(RuntimeError):
    """云端调用异常，携带可读信息。"""


# 阿里云 SWAS 的 Port 只接受「起始/结束」格式：
#   TCP/UDP : 1/200、1/65535
#   ICMP    : -1/-1
# 传 "ALL" 会直接报 InvalidPort.ValueNotSupported，所以「全部端口」必须落成 1/65535。
ALL_PORTS = "1/65535"
ICMP_ANY_PORT = "-1/-1"

# RuleProtocol 合法值：TCP / UDP / TCP+UDP / ICMP（GRE 部分地域支持）
_VALID_PROTOCOLS = {"TCP", "UDP", "TCP+UDP", "ICMP", "GRE"}

# 「全部端口」的各种写法，统一收敛（中文输入法下用户会打出「全部端口」「全端口」「全部」「所有」）
_ALL_PORT_ALIASES = {
    "",
    "*",
    "ALL",
    "ALLPORTS",
    "全部",
    "所有",
    "全端口",
    "全部端口",
    "所有端口",
}


# --------------------------------------------------------------------------- 工具
def _is_all_ports(text: str) -> bool:
    return str(text or "").strip().upper() in {a.upper() for a in _ALL_PORT_ALIASES}


def _port_num(text: str, raw: str) -> int:
    """把片段转成 1..65535 的整数，非法时抛 CloudError。"""
    text = str(text or "").strip()
    if not text.isdigit():
        raise CloudError(f"端口格式非法: {raw}")
    value = int(text)
    if not 1 <= value <= 65535:
        raise CloudError(f"端口超出 1-65535 范围: {raw}")
    return value


def normalize_port_value(port: str) -> str:
    """把任意端口写法收敛成阿里云可接受的 Port 字符串。"""
    text = str(port or "").strip()
    if _is_all_ports(text):
        return ALL_PORTS
    if text == ICMP_ANY_PORT:
        return text
    if "-" in text and "/" not in text:
        start, _, end = text.partition("-")
        a, b = _port_num(start, text), _port_num(end, text)
        if a > b:
            raise CloudError(f"端口段起始值不能大于结束值: {text}")
        return f"{a}/{b}"
    if "/" in text:
        start, _, end = text.partition("/")
        a, b = _port_num(start, text), _port_num(end, text)
        if a > b:
            raise CloudError(f"端口段起始值不能大于结束值: {text}")
        return f"{a}" if a == b else f"{a}/{b}"
    return str(_port_num(text, text))


def split_ports(spec: str) -> List[str]:
    """
    把用户输入的端口表达式归一化成「阿里云端口字符串」列表。

    "ALL" / "*" / "" / "全部端口" -> ["1/65535"]    （全端口）
    "22,80,443"                 -> ["22", "80", "443"]
    "8000-9000"                 -> ["8000/9000"]
    "22/22"                     -> ["22"]

    非法输入（"abc" / "70000" / "9000-8000"）抛 CloudError，
    保证 Web 层能转成 400 而不是 500。
    """
    if spec is None:
        return [ALL_PORTS]
    text = str(spec).strip()
    if _is_all_ports(text):
        return [ALL_PORTS]

    result: List[str] = []
    for token in re.split(r"[,，;；\s]+", text):
        token = token.strip()
        if not token:
            continue
        if _is_all_ports(token):
            return [ALL_PORTS]
        normalized = normalize_port_value(token)
        if normalized not in result:
            result.append(normalized)
    return result or [ALL_PORTS]


def port_to_display(port: str) -> str:
    """云端端口格式转展示格式。"""
    text = str(port or "").strip()
    if not text or text.upper() == "ALL" or text == ALL_PORTS:
        return "全部端口"
    if text == ICMP_ANY_PORT:
        return "全部端口（ICMP）"
    return text.replace("/", "-")


def normalize_cidr(value: str) -> Tuple[str, str]:
    """返回 (cidr, ip)；自动补 /32 或 /128。非法输入抛 CloudError。"""
    text = str(value or "").strip()
    if not text:
        raise CloudError("IP 不能为空")
    if "/" not in text:
        try:
            addr = ipaddress.ip_address(text)
        except ValueError as exc:
            raise CloudError(f"非法的 IP 地址: {value}") from exc
        text = f"{text}/{'128' if addr.version == 6 else '32'}"
    try:
        net = ipaddress.ip_network(text, strict=False)
    except ValueError as exc:
        raise CloudError(f"非法的 IP/CIDR: {value}") from exc
    return str(net), str(net.network_address)


def protocol_normalize(value: str) -> str:
    """协议归一化；ALL 表示 TCP+UDP 双栈全放行。"""
    text = (value or "tcp").strip().upper()
    if text in {"ALL", "TCP+UDP", "TCP/UDP", "TCP,UDP"}:
        return "TCP+UDP"
    if text in _VALID_PROTOCOLS:
        return text
    return "TCP"


# --------------------------------------------------------------------------- 客户端
class SwasFirewall:
    """轻量应用服务器防火墙操作。"""

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
        self.endpoint = endpoint or f"swas.{self.region_id}.aliyuncs.com"
        self._client = None

    # ------------------------------------------------------------------ 底层
    @property
    def client(self):
        if self._client is None:
            try:
                from alibabacloud_swas_open20200601.client import Client
                from alibabacloud_tea_openapi import models as open_api_models
            except ImportError as exc:  # pragma: no cover
                raise CloudError(
                    "缺少阿里云 SDK，请执行 pip install alibabacloud_swas_open20200601"
                ) from exc
            config = open_api_models.Config(
                access_key_id=self.access_key_id,
                access_key_secret=self.access_key_secret,
            )
            config.endpoint = self.endpoint
            self._client = Client(config)
        return self._client

    def _models(self):
        from alibabacloud_swas_open20200601 import models

        return models

    def _runtime(self):
        from alibabacloud_tea_util import models as util_models

        runtime = util_models.RuntimeOptions()
        # 阿里云部分地域接口响应较慢，默认 5s 容易误判超时
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

    # ------------------------------------------------------------------ 实例
    def list_instances(self, page_size: int = 50) -> List[Dict[str, Any]]:
        models = self._models()
        request = models.ListInstancesRequest(region_id=self.region_id)
        request.page_size = page_size
        request.page_number = 1
        try:
            resp = self.client.list_instances_with_options(request, self._runtime())
        except Exception as exc:  # noqa: BLE001
            raise CloudError(self._friendly_error(exc)) from exc

        out: List[Dict[str, Any]] = []
        body = getattr(resp, "body", None)
        for item in getattr(body, "instances", None) or []:
            out.append(
                {
                    "instance_id": getattr(item, "instance_id", ""),
                    "name": getattr(item, "instance_name", "") or getattr(item, "instance_id", ""),
                    "status": getattr(item, "status", ""),
                    "public_ip": getattr(item, "public_ip_address", ""),
                    "region_id": self.region_id,
                }
            )
        return out

    # ------------------------------------------------------------------ 规则
    def list_rules(self, instance_id: str) -> List[Dict[str, Any]]:
        models = self._models()
        rules: List[Dict[str, Any]] = []
        page = 1
        while True:
            request = models.ListFirewallRulesRequest(
                instance_id=instance_id, region_id=self.region_id
            )
            request.page_number = page
            request.page_size = 100
            try:
                resp = self.client.list_firewall_rules_with_options(request, self._runtime())
            except Exception as exc:  # noqa: BLE001
                raise CloudError(self._friendly_error(exc)) from exc

            body = getattr(resp, "body", None)
            batch = getattr(body, "firewall_rules", None) or []
            for rule in batch:
                cidr = getattr(rule, "source_cidr_ip", "") or ""
                port = getattr(rule, "port", "") or "ALL"
                rules.append(
                    {
                        "rule_id": getattr(rule, "rule_id", "") or "",
                        "protocol": (getattr(rule, "rule_protocol", "") or "TCP").lower(),
                        "port": "ALL" if str(port).upper() == "ALL" else str(port),
                        "cidr": cidr,
                        "ip": cidr.split("/")[0] if "/" in cidr else cidr,
                        "description": getattr(rule, "remark", "") or "",
                        # policy: accept / drop
                        "policy": (getattr(rule, "policy", "") or "accept").lower(),
                        "instance_id": instance_id,
                        "instance_name": "",
                    }
                )
            total = getattr(body, "total_count", 0) or 0
            if len(batch) < 100 or page * 100 >= total:
                break
            page += 1
        return rules

    def create_rule(
        self,
        instance_id: str,
        cidr: str,
        protocol: str,
        port: str,
        remark: str = "",
    ) -> Dict[str, Any]:
        models = self._models()
        port = normalize_port_value(port)
        try:
            item = models.CreateFirewallRulesRequestFirewallRules(
                rule_protocol=protocol_normalize(protocol),
                port=port,
                source_cidr_ip=cidr,
                remark=(remark or "")[:64],
            )
            request = models.CreateFirewallRulesRequest(
                instance_id=instance_id,
                region_id=self.region_id,
                firewall_rules=[item],
            )
            resp = self.client.create_firewall_rules_with_options(request, self._runtime())
        except Exception as exc:  # noqa: BLE001
            raise CloudError(self._friendly_error(exc)) from exc

        body = getattr(resp, "body", None)
        ids = getattr(body, "firewall_rule_ids", None) or []
        return {
            "rule_id": ids[0] if ids else "",
            "request_id": getattr(body, "request_id", "") or "",
        }

    def delete_rules(self, instance_id: str, rule_ids: List[str]) -> bool:
        ids = [r for r in rule_ids if r]
        if not ids:
            return False
        models = self._models()
        try:
            request = models.DeleteFirewallRulesRequest(
                instance_id=instance_id,
                region_id=self.region_id,
                rule_ids=ids,
            )
            self.client.delete_firewall_rules_with_options(request, self._runtime())
            return True
        except Exception as exc:  # noqa: BLE001
            raise CloudError(self._friendly_error(exc)) from exc

    def enable_rule(self, instance_id: str, rule_id: str, cidr: str = "", remark: str = "") -> bool:
        """启用规则（SDK 原生 EnableFirewallRule）。"""
        models = self._models()
        try:
            request = models.EnableFirewallRuleRequest(
                instance_id=instance_id,
                region_id=self.region_id,
                rule_id=rule_id,
                source_cidr_ip=cidr or None,
                remark=remark or None,
            )
            self.client.enable_firewall_rule_with_options(request, self._runtime())
            return True
        except Exception as exc:  # noqa: BLE001
            raise CloudError(self._friendly_error(exc)) from exc

    def disable_rule(self, instance_id: str, rule_id: str, remark: str = "") -> bool:
        """禁用规则（SDK 原生 DisableFirewallRule）。"""
        models = self._models()
        try:
            request = models.DisableFirewallRuleRequest(
                instance_id=instance_id,
                region_id=self.region_id,
                rule_id=rule_id,
                remark=remark or None,
            )
            self.client.disable_firewall_rule_with_options(request, self._runtime())
            return True
        except Exception as exc:  # noqa: BLE001
            raise CloudError(self._friendly_error(exc)) from exc

    def modify_rule(
        self,
        instance_id: str,
        rule_id: str,
        cidr: str,
        protocol: str,
        port: str,
        remark: str = "",
    ) -> bool:
        """
        优先使用 ModifyFirewallRule；SDK 未提供该接口时退化为「删除后重建」，
        由上层负责把新 rule_id 写回本地库。
        """
        client = self.client
        if hasattr(client, "modify_firewall_rule_with_options"):
            models = self._models()
            try:
                request = models.ModifyFirewallRuleRequest(
                    instance_id=instance_id,
                    region_id=self.region_id,
                    rule_id=rule_id,
                    rule_protocol=protocol_normalize(protocol),
                    port=normalize_port_value(port),
                    source_cidr_ip=cidr,
                    remark=(remark or "")[:64],
                )
                client.modify_firewall_rule_with_options(request, self._runtime())
                return True
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"ModifyFirewallRule 调用失败，回退删除重建: {exc}")

        self.delete_rules(instance_id, [rule_id])
        self.create_rule(instance_id, cidr, protocol, port, remark)
        return True

    def ping(self) -> Dict[str, Any]:
        """健康检查：能否成功列实例。"""
        instances = self.list_instances(page_size=5)
        return {"ok": True, "instance_count": len(instances)}


def build_client(aliyun_config: Optional[Dict[str, Any]] = None) -> SwasFirewall:
    from app.settings import settings

    cfg = aliyun_config or settings.section("aliyun")
    return SwasFirewall(
        access_key_id=cfg.get("access_key_id", ""),
        access_key_secret=cfg.get("access_key_secret", ""),
        region_id=cfg.get("region_id") or "cn-hangzhou",
        endpoint=cfg.get("endpoint") or "",
    )
