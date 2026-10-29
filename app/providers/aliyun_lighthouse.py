from typing import List, Dict, Any
from app.providers.base import BaseProvider
from app.utils.validators import is_ipv6
from app.utils.logger import logger


class AliyunLighthouseProvider(BaseProvider):
    """阿里云轻量应用服务器防火墙管理"""

    def initialize_client(self) -> bool:
        """初始化阿里云轻量服务器客户端"""
        try:
            from alibabacloud_swas_open20200601.client import Client as SwasClient
            from alibabacloud_tea_openapi import models as open_api_models

            access_key_id = self.config.get("access_key_id")
            access_key_secret = self.config.get("access_key_secret")
            region = self.config.get("region")

            if not all([access_key_id, access_key_secret, region]):
                logger.error("阿里云轻量服务器配置不完整")
                return False

            config = open_api_models.Config(
                access_key_id=access_key_id, access_key_secret=access_key_secret
            )
            config.endpoint = f"swas.{region}.aliyuncs.com"
            self.client = SwasClient(config)
            self.region = region
            return True
        except Exception as e:
            logger.error(f"初始化阿里云轻量服务器客户端失败: {e}")
            return False

    def list_firewall_rules(self, instance_id: str) -> List[Dict[str, Any]]:
        """列出实例的防火墙规则"""
        try:
            from alibabacloud_swas_open20200601 import models as swas_models
            from alibabacloud_tea_util import models as util_models

            request = swas_models.ListFirewallRulesRequest()
            request.instance_id = instance_id
            request.region_id = self.region

            runtime = util_models.RuntimeOptions()
            response = self.client.list_firewall_rules_with_options(request, runtime)

            rules = []
            if response.body and response.body.firewall_rules:
                for rule in response.body.firewall_rules:
                    # 正确字段名：source_cidr_ip
                    ip_address = rule.source_cidr_ip or ""
                    ip_version = "IPv4"
                    if ip_address:
                        ip_address = ip_address.replace("/32", "").replace("/128", "")
                        if is_ipv6(ip_address):
                            ip_version = "IPv6"

                    # 解析端口（阿里云返回的端口格式可能是 "80/80" 或 "3306"）
                    ports = []
                    if rule.port and rule.port != "ALL":
                        if "/" in rule.port:
                            parts = rule.port.split("/")
                            if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                                start, end = int(parts[0]), int(parts[1])
                                if start == end:
                                    ports = [start]
                                else:
                                    ports = [start, end]
                        elif rule.port.isdigit():
                            ports = [int(rule.port)]

                    rules.append(
                        {
                            # 正确字段名：rule_id
                            "rule_id": rule.rule_id,
                            # 正确字段名：rule_protocol
                            "protocol": (rule.rule_protocol or "").lower(),
                            "ports": ports,
                            "ip_address": ip_address,
                            # 正确字段名：remark
                            "description": rule.remark or "",
                            "ip_version": ip_version,
                        }
                    )

            return rules
        except Exception as e:
            logger.error(f"获取阿里云轻量服务器防火墙规则失败: {e}")
            return []

    def add_firewall_rule(
        self,
        instance_id: str,
        ip_address: str,
        protocol: str,
        ports: List[int],
        description: str,
    ) -> bool:
        if not self.client:
            return False

        try:
            from alibabacloud_swas_open20200601 import models as swas_models
            from alibabacloud_tea_util import models as util_models

            ip_version = "IPv6" if is_ipv6(ip_address) else "IPv4"
            cidr_suffix = "/128" if ip_version == "IPv6" else "/32"
            cidr_block = f"{ip_address}{cidr_suffix}"

            # 构建端口字符串：阿里云要求用斜线分隔，单端口直接写数字
            if len(ports) == 1:
                port_str = str(ports[0])
            elif len(ports) > 1:
                port_str = f"{min(ports)}/{max(ports)}"
            else:
                port_str = "ALL"

            existing_rules = self.list_firewall_rules(instance_id)

            # 检查规则是否已存在
            for rule in existing_rules:
                if (
                    rule["protocol"] == protocol.lower()
                    and rule["ip_address"] == ip_address
                    and (
                        (len(ports) == 1 and set(rule["ports"]) == set(ports))
                        or (len(ports) > 1 and rule["ports"] == sorted(ports))
                        or (len(ports) == 0 and len(rule["ports"]) == 0)
                    )
                ):
                    logger.info(
                        f"阿里云轻量服务器防火墙规则已存在: {instance_id}, IP: {ip_address}, Port: {port_str}"
                    )
                    return True

            # 创建规则（注意字段名：rule_protocol、source_cidr_ip）
            firewall_rule = swas_models.CreateFirewallRulesRequestFirewallRules()
            firewall_rule.rule_protocol = protocol.upper()
            firewall_rule.port = port_str
            firewall_rule.source_cidr_ip = cidr_block
            firewall_rule.remark = description

            request = swas_models.CreateFirewallRulesRequest()
            request.instance_id = instance_id
            request.region_id = self.region
            request.firewall_rules = [firewall_rule]

            runtime = util_models.RuntimeOptions()
            self.client.create_firewall_rules_with_options(request, runtime)
            logger.info(
                f"阿里云轻量服务器防火墙规则添加成功: {instance_id}, IP: {ip_address}, Port: {port_str}"
            )
            return True
        except Exception as e:
            logger.error(f"阿里云轻量服务器添加防火墙规则失败: {e}")
            return False

    def remove_firewall_rule(self, instance_id: str, rule_id: str) -> bool:
        if not self.client:
            return False

        try:
            from alibabacloud_swas_open20200601 import models as swas_models
            from alibabacloud_tea_util import models as util_models

            request = swas_models.DeleteFirewallRulesRequest()
            request.instance_id = instance_id
            request.region_id = self.region
            request.firewall_rule_ids = [rule_id]

            runtime = util_models.RuntimeOptions()
            self.client.delete_firewall_rules_with_options(request, runtime)
            logger.info(
                f"阿里云轻量服务器防火墙规则删除成功: {instance_id}, Rule: {rule_id}"
            )
            return True
        except Exception as e:
            logger.error(f"阿里云轻量服务器删除防火墙规则失败: {e}")
            return False

    def cleanup_old_firewall_rules(
        self,
        instance_id: str,
        current_ip: str,
        protocol: str,
        ports: List[int],
        days: int = 7,
    ) -> int:
        """清理旧的防火墙规则"""
        removed_count = 0
        current_ip_version = "IPv6" if is_ipv6(current_ip) else "IPv4"

        rules = self.list_firewall_rules(instance_id)
        for rule in rules:
            if (
                rule["protocol"] == protocol.lower()
                and (
                    (len(ports) == 1 and set(rule["ports"]) == set(ports))
                    or (len(ports) > 1 and rule["ports"] == sorted(ports))
                    or (len(ports) == 0 and len(rule["ports"]) == 0)
                )
                and rule["ip_address"] != current_ip
                and "Home" in rule["description"]
                and rule["ip_version"] == current_ip_version
                and rule["rule_id"]
            ):
                if self.remove_firewall_rule(instance_id, rule["rule_id"]):
                    removed_count += 1
                    logger.info(
                        f"已清理阿里云轻量服务器旧防火墙规则: {rule['ip_address']}"
                    )

        return removed_count

    def find_and_remove_old_ip_rules(
        self,
        instance_id: str,
        protocol: str,
        ports: List[int],
        new_ip: str,
    ) -> int:
        """查找并删除旧的IP规则"""
        return self.cleanup_old_firewall_rules(instance_id, new_ip, protocol, ports, 7)

    # 兼容接口
    def list_security_group_rules(self, security_group_id: str) -> List[Dict[str, Any]]:
        return self.list_firewall_rules(security_group_id)

    def add_security_group_rule(
        self,
        ip_address: str,
        security_group_id: str,
        protocol: str,
        ports: List[int],
        description: str = "",
    ) -> bool:
        return self.add_firewall_rule(
            security_group_id, ip_address, protocol, ports, description
        )

    def remove_security_group_rule(self, security_group_id: str, rule_id: str) -> bool:
        return self.remove_firewall_rule(security_group_id, rule_id)
