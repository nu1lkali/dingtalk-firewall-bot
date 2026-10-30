#!/usr/bin/env python3
"""
动态云防火墙 · 完整测试套件
============================

与 smoke_test.py 的区别：
  * smoke_test 只做「能不能跑起来」的冒烟检查；
  * 本套件用**假云端**（内存版 SWAS）覆盖业务的完整生命周期，
    并强制校验真实阿里云 API 的约束（Port 必须是 start/end、NotFound 规则等），
    因此可以在不碰线上环境的前提下复现线上出现过的 400/404 问题。

运行：
    python scripts/test_suite.py            # 全部
    python scripts/test_suite.py -k webhook # 只跑名字含 webhook 的用例

设计要点：
  1. 所有数据落在临时目录的 SQLite 里（APP_DB_PATH），不污染 data/app.db；
  2. 同时劫持 settings 的 config.yaml / settings.json 路径，避免读到真实凭证；
  3. rules_service.build_client 被替换为假客户端，测试全程不发起任何云调用。
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

_TMP = tempfile.mkdtemp(prefix="dcf-test-")
os.environ["APP_DB_PATH"] = os.path.join(_TMP, "test.db")

import app.settings as settings_mod  # noqa: E402

# 断掉真实配置来源（否则会读到 data/settings.json 里的真 AccessKey）
settings_mod.CONFIG_JSON = os.path.join(_TMP, "settings.json")
settings_mod.CONFIG_YAML = os.path.join(_TMP, "config.yaml")

from app.cloud.swas import (  # noqa: E402
    ALL_PORTS,
    CloudError,
    normalize_cidr,
    normalize_port_value,
    port_to_display,
    protocol_normalize,
    split_ports,
)
from app.core import rules_service, twofa  # noqa: E402
from app.settings import settings  # noqa: E402
from app.store import store, now_str  # noqa: E402

INSTANCE = "i-test-0001"
TEST_PASSWORD = "Test@2026dcf"

# --------------------------------------------------------------------------- 假云端
_PORT_RE = re.compile(r"^-?\d+/-?\d+$|^\d+$")


class FakeSwas:
    """
    内存版 SWAS 防火墙，行为对齐真实 API：

    * Port 必须是 ``start/end`` 或单端口，传 ``ALL`` 直接 400（线上真实踩过的坑）；
    * ``RuleProtocol`` 只接受 TCP / UDP / TCP+UDP / ICMP / GRE；
    * 删除不存在的 rule_id 返回 404 NotFound。
    """

    def __init__(self) -> None:
        self.rules = []
        self.calls = []
        self._seq = 0
        self.supports_enable = True
        self.list_error = ""
        self.track_ports = []

    # -- 内部
    def _next_id(self) -> str:
        self._seq += 1
        return f"rule-{self._seq:04d}"

    def _check_port(self, port) -> None:
        text = str(port)
        if not _PORT_RE.match(text):
            raise CloudError(
                f"InvalidPort.ValueNotSupported code: 400, The specified parameter Port is invalid. ({text})"
            )

    def _check_proto(self, protocol) -> None:
        if str(protocol).upper() not in {"TCP", "UDP", "TCP+UDP", "ICMP", "GRE"}:
            raise CloudError(f"InvalidRuleProtocol code: 400 ({protocol})")

    # -- 对外接口（与 SwasFirewall 同名）
    def list_rules(self, instance_id: str):
        self.calls.append(("list_rules", instance_id))
        if self.list_error:
            raise CloudError(self.list_error)
        return [dict(r) for r in self.rules if r["instance_id"] == instance_id]

    def create_rule(self, instance_id, cidr, protocol, port, remark=""):
        self._check_port(port)
        self._check_proto(protocol)
        self.track_ports.append(str(port))
        self.calls.append(("create_rule", instance_id, cidr, protocol, port))
        for r in self.rules:
            if r["instance_id"] == instance_id and r["cidr"] == cidr \
                    and r["port"] == str(port) and r["protocol"] == str(protocol).lower():
                # 真实 API 对该组合会返回 Duplicate
                raise CloudError("RuleAlreadyExists code: 400, duplicated rule")
        rid = self._next_id()
        self.rules.append(
            {
                "rule_id": rid,
                "instance_id": instance_id,
                "cidr": cidr,
                "ip": cidr.split("/")[0],
                "protocol": str(protocol).lower(),
                "port": str(port),
                "description": remark,
                "policy": "accept",
                "instance_name": "",
            }
        )
        return {"rule_id": rid, "request_id": "req-fake"}

    def delete_rules(self, instance_id, rule_ids):
        self.calls.append(("delete_rules", instance_id, list(rule_ids)))
        for rid in rule_ids:
            hit = [r for r in self.rules if r["rule_id"] == rid]
            if not hit:
                raise CloudError(
                    "InvalidRuleId.NotFound code: 404, The specified rule does not exist."
                )
        self.rules = [r for r in self.rules if r["rule_id"] not in rule_ids]
        return True

    def enable_rule(self, instance_id, rule_id, cidr="", remark=""):
        if not self.supports_enable:
            raise CloudError("InvalidAction code: 400, EnableFirewallRule not supported")
        self.calls.append(("enable_rule", rule_id))
        return True

    def disable_rule(self, instance_id, rule_id, remark=""):
        if not self.supports_enable:
            raise CloudError("InvalidAction code: 400, DisableFirewallRule not supported")
        self.calls.append(("disable_rule", rule_id))
        return True

    def modify_rule(self, instance_id, rule_id, cidr, protocol, port, remark=""):
        self._check_port(port)
        self._check_proto(protocol)
        self.calls.append(("modify_rule", rule_id, cidr, protocol, port))
        for r in self.rules:
            if r["rule_id"] == rule_id:
                r.update(
                    cidr=cidr,
                    ip=cidr.split("/")[0],
                    protocol=str(protocol).lower(),
                    port=str(port),
                    description=remark,
                )
                return True
        raise CloudError("InvalidRuleId.NotFound code: 404")

    def ping(self):
        return {"ok": True, "instance_count": 1}

    def list_instances(self, page_size=50):
        return [{"instance_id": INSTANCE, "name": "测试实例", "status": "Running", "public_ip": "1.1.1.1"}]

    # -- 测试辅助
    def reset(self) -> None:
        self.rules.clear()
        self.calls.clear()
        self.track_ports.clear()
        self.list_error = ""
        self.supports_enable = True


FAKE = FakeSwas()


def setUpModule() -> None:
    settings.load()
    settings.update(
        {
            "aliyun": {
                "enabled": True,
                "access_key_id": "test-ak",
                "access_key_secret": "test-sk",
                "region_id": "cn-hangzhou",
                "instances": [{"instance_id": INSTANCE, "name": "测试实例", "enabled": True}],
            },
            "webhook": {"secret": ""},
            "rules_policy": {"auto_cleanup_old_ip": True, "default_ttl_minutes": 60},
            "dingtalk": {"default_ports": "ALL", "default_protocol": "tcp", "default_ttl_minutes": 60},
            "cron": {"enabled": True, "timezone": "Asia/Shanghai"},
        }
    )
    rules_service.build_client = lambda *a, **k: FAKE  # type: ignore[assignment]


class BaseCase(unittest.TestCase):
    def setUp(self) -> None:
        FAKE.reset()
        store._execute("DELETE FROM rules")
        store._execute("DELETE FROM logs")

    # -- 工具
    def make_row(self, **over) -> dict:
        data = {
            "instance_id": INSTANCE,
            "instance_name": "测试实例",
            "rule_id": "",
            "ip": "203.0.113.42",
            "cidr": "203.0.113.42/32",
            "protocol": "tcp",
            "port": "22",
            "description": "AUTO-ALLOW test",
            "status": "active",
            "source": "web",
            "actor": "tester",
            "expires_at": "",
        }
        data.update(over)
        return data


# --------------------------------------------------------------------------- 1. 端口/协议归一化
class PortNormalizeTest(BaseCase):
    def test_all_ports_maps_to_range(self):
        self.assertEqual(split_ports("ALL"), [ALL_PORTS])
        self.assertEqual(split_ports(""), [ALL_PORTS])
        self.assertEqual(split_ports("*"), [ALL_PORTS])
        self.assertEqual(split_ports("全部端口"), [ALL_PORTS])

    def test_range_and_list(self):
        self.assertEqual(split_ports("8000-9000"), ["8000/9000"])
        self.assertEqual(split_ports("22,80,443"), ["22", "80", "443"])
        self.assertEqual(split_ports("22/22"), ["22"])
        self.assertEqual(split_ports("22/80"), ["22/80"])

    def test_invalid_port_raises_cloud_error_not_value_error(self):
        """回归：以前 int("abc") 会抛 ValueError，Web 层只 catch CloudError → HTTP 500。"""
        for bad in ("abc", "22abc", "8000-", "-9000", "0", "70000", "9000-8000", "22,,/"):
            with self.subTest(bad=bad):
                with self.assertRaises(CloudError):
                    split_ports(bad)

    def test_normalize_port_value(self):
        self.assertEqual(normalize_port_value("ALL"), ALL_PORTS)
        self.assertEqual(normalize_port_value(None), ALL_PORTS)
        self.assertEqual(normalize_port_value("8000-9000"), "8000/9000")
        self.assertEqual(normalize_port_value("22"), "22")

    def test_protocol_normalize(self):
        self.assertEqual(protocol_normalize("ALL"), "TCP+UDP")
        self.assertEqual(protocol_normalize("tcp+udp"), "TCP+UDP")
        self.assertEqual(protocol_normalize("icmp"), "ICMP")
        self.assertEqual(protocol_normalize("bogus"), "TCP")
        self.assertEqual(protocol_normalize(None), "TCP")

    def test_port_display(self):
        self.assertEqual(port_to_display(ALL_PORTS), "全部端口")
        self.assertEqual(port_to_display("8000/9000"), "8000-9000")
        self.assertEqual(port_to_display("22"), "22")


# --------------------------------------------------------------------------- 2. IP/CIDR
class CidrTest(BaseCase):
    def test_normalize_cidr(self):
        self.assertEqual(normalize_cidr("1.2.3.4"), ("1.2.3.4/32", "1.2.3.4"))
        self.assertEqual(normalize_cidr("1.2.3.0/24"), ("1.2.3.0/24", "1.2.3.0"))
        self.assertEqual(normalize_cidr("2001:db8::1"), ("2001:db8::1/128", "2001:db8::1"))

    def test_invalid_ip_raises_cloud_error_not_value_error(self):
        """回归：update_rule_manual 直接调 normalize_cidr，非法 IP 以前会 500。"""
        for bad in ("1.2.3.999", "not-an-ip", "1.2.3", ""):
            with self.subTest(bad=bad):
                with self.assertRaises(CloudError):
                    normalize_cidr(bad)


# --------------------------------------------------------------------------- 3. 放行主链路
class AllowFlowTest(BaseCase):
    def test_allow_all_ports_uses_range_and_tcp_udp(self):
        """回归：全端口曾直接用 "ALL" 下发，阿里云报 InvalidPort.ValueNotSupported。"""
        res = rules_service.allow_ip("203.0.113.42", ports="ALL")
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["protocol"], "TCP+UDP")
        self.assertEqual(FAKE.rules[0]["port"], ALL_PORTS)
        self.assertEqual(FAKE.rules[0]["protocol"], "tcp+udp")
        self.assertIn(ALL_PORTS, FAKE.track_ports)

    def test_allow_specific_port_keeps_tcp(self):
        res = rules_service.allow_ip("203.0.113.42", ports="22")
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["protocol"], "TCP")
        self.assertEqual(FAKE.rules[0]["port"], "22")

    def test_allow_is_idempotent(self):
        first = rules_service.allow_ip("203.0.113.42", ports="22")
        second = rules_service.allow_ip("203.0.113.42", ports="22")
        self.assertEqual(first["summary"]["created"], 1)
        self.assertEqual(second["summary"]["skipped"], 1)
        self.assertEqual(len(FAKE.rules), 1)
        self.assertEqual(len(store.find_rules(ip="203.0.113.42")), 1)

    def test_allow_multiple_ips_single_port(self):
        res = rules_service.allow_ip("203.0.113.42,203.0.113.43", ports="22")
        self.assertTrue(res["ok"], res)
        self.assertEqual(len(FAKE.rules), 2)

    def test_allow_multiple_ports(self):
        res = rules_service.allow_ip("203.0.113.42", ports="22,80")
        self.assertTrue(res["ok"], res)
        self.assertEqual(sorted(r["port"] for r in FAKE.rules), ["22", "80"])

    def test_allow_rejects_bad_ip(self):
        with self.assertRaises(CloudError):
            rules_service.allow_ip("999.1.1.1", ports="22")

    def test_allow_rejects_bad_port(self):
        """参数非法必须抛 CloudError（Web 层能转成 400），而不是 ValueError。"""
        with self.assertRaises(CloudError):
            rules_service.allow_ip("203.0.113.42", ports="http")

    def test_allow_no_instance_returns_friendly_error(self):
        settings.update({"aliyun": {"instances": []}})
        try:
            res = rules_service.allow_ip("203.0.113.42", ports="22")
            self.assertFalse(res["ok"])
            self.assertIn("没有可用的阿里云实例", res["message"])
        finally:
            settings.update(
                {"aliyun": {"instances": [{"instance_id": INSTANCE, "name": "测试实例", "enabled": True}]}}
            )

    def test_allow_writes_expiry(self):
        rules_service.allow_ip("203.0.113.42", ports="22", ttl_minutes=30)
        row = store.find_rules(ip="203.0.113.42")[0]
        self.assertTrue(row["expires_at"], "应写入到期时间")

    def test_upsert_local_distinguishes_protocol(self):
        """回归：本地记录按 ip+port 匹配，忽略 protocol → TCP/UDP 同端口互相覆盖。"""
        rules_service.allow_ip("203.0.113.42", ports="53", protocol="tcp")
        rules_service.allow_ip("203.0.113.42", ports="53", protocol="udp")
        rows = [r for r in store.find_rules(ip="203.0.113.42") if r["status"] == "active"]
        self.assertEqual(len(rows), 2, f"TCP/UDP 两条记录应并存，实际 {rows}")

    def test_upsert_local_reuses_disabled_row(self):
        """已禁用但云端仍在的规则再次放行时，应复用记录而不是插入重复行。"""
        created = FAKE.create_rule(INSTANCE, "203.0.113.14/32", "TCP", "22", "x")
        pk = store.add_rule(self.make_row(rule_id=created["rule_id"], ip="203.0.113.14",
                                          cidr="203.0.113.14/32", status="disabled"))
        rules_service.allow_ip("203.0.113.14", ports="22")
        rows = store.find_rules(ip="203.0.113.14")
        self.assertEqual(len(rows), 1, f"不应插入重复记录，实际 {rows}")
        self.assertEqual(rows[0]["id"], pk)
        self.assertEqual(rows[0]["status"], "active")

    def test_upsert_local_does_not_touch_other_ip_row(self):
        """回归：ip / port 过滤失效时，放行新 IP 会改到「该实例最新一条」规则上。"""
        other = store.add_rule(
            self.make_row(rule_id="other-rule", ip="203.0.113.77", cidr="203.0.113.77/32",
                          port=ALL_PORTS, protocol="tcp+udp")
        )
        res = rules_service.allow_ip(
            "203.0.113.78", ports="ALL", protocol="tcp", ttl_minutes=60, cleanup_old=False
        )
        self.assertTrue(res["ok"], res)
        self.assertEqual(store.get_rule(other)["ip"], "203.0.113.77", "不能把别的 IP 的行改成本次 IP")
        new_rows = store.find_rules(ip="203.0.113.78", status_in=["active"])
        self.assertEqual(len(new_rows), 1, f"新 IP 应各自一行，实际 {new_rows}")
        self.assertTrue(new_rows[0]["expires_at"])


# --------------------------------------------------------------------------- 4. 删除 / 恢复 / 清理
class DeleteRestoreTest(BaseCase):
    def _seed(self, **over) -> int:
        created = FAKE.create_rule(INSTANCE, "203.0.113.42/32", "TCP", "22", "AUTO-ALLOW seed")
        return store.add_rule(self.make_row(rule_id=created["rule_id"], **over))

    def test_delete_soft_and_idempotent(self):
        pk = self._seed()
        first = rules_service.delete_rule(pk)
        self.assertTrue(first["ok"], first)
        row = store.get_rule(pk)
        self.assertEqual(row["status"], "deleted")
        self.assertEqual(row["rule_id"], "", "软删除应清空 rule_id，避免再次下发")
        self.assertEqual(FAKE.rules, [], "云端应已删除")

        second = rules_service.delete_rule(pk)
        self.assertTrue(second["ok"], "重复删除必须幂等成功")
        self.assertTrue(second.get("already"))

    def test_delete_cloud_not_found_is_success(self):
        """回归：阿里云返回 InvalidRuleId.NotFound 时以前判为失败。"""
        pk = store.add_rule(self.make_row(rule_id="ghost-rule-id"))
        res = rules_service.delete_rule(pk)
        self.assertTrue(res["ok"], res)
        self.assertEqual(store.get_rule(pk)["status"], "deleted")

    def test_delete_missing_row_is_ok(self):
        self.assertTrue(rules_service.delete_rule(999999)["ok"])

    def test_restore_recreates_in_cloud(self):
        pk = self._seed()
        rules_service.delete_rule(pk)
        self.assertEqual(FAKE.rules, [])
        res = rules_service.restore_rule(pk)
        self.assertTrue(res["ok"], res)
        self.assertEqual(len(FAKE.rules), 1)
        row = store.get_rule(pk)
        self.assertEqual(row["status"], "active")
        self.assertTrue(row["rule_id"], "恢复后应回填新的 rule_id")

    def test_restore_reuses_existing_cloud_rule(self):
        pk = self._seed()
        rules_service.delete_rule(pk)
        FAKE.create_rule(INSTANCE, "203.0.113.42/32", "TCP", "22", "手工建的同款规则")
        res = rules_service.restore_rule(pk)
        self.assertTrue(res["ok"], res)
        self.assertEqual(len(FAKE.rules), 1, "云端已有同款规则时不应重复创建")
        self.assertIn("重新关联", res["message"])

    def test_restore_clears_expired_ttl(self):
        pk = self._seed(expires_at="2000-01-01 00:00:00")
        rules_service.delete_rule(pk)
        res = rules_service.restore_rule(pk)
        self.assertTrue(res["ok"], res)
        self.assertEqual(store.get_rule(pk)["expires_at"], "", "已过期的到期时间应清空，否则会被定时任务秒删")

    def test_purge_only_for_gone(self):
        pk = self._seed()
        refused = rules_service.purge_rule(pk)
        self.assertFalse(refused["ok"], "在用规则不允许物理删除")

        rules_service.delete_rule(pk)
        done = rules_service.purge_rule(pk)
        self.assertTrue(done["ok"], done)
        self.assertIsNone(store.get_rule(pk))


# --------------------------------------------------------------------------- 5. 启停 / 编辑
class ToggleEditTest(BaseCase):
    def test_toggle_disable_native(self):
        created = FAKE.create_rule(INSTANCE, "203.0.113.42/32", "TCP", "22", "AUTO-ALLOW")
        pk = store.add_rule(self.make_row(rule_id=created["rule_id"]))
        res = rules_service.toggle_rule(pk, False)
        self.assertTrue(res["ok"], res)
        self.assertEqual(store.get_rule(pk)["status"], "disabled")
        self.assertEqual(len(FAKE.rules), 1, "原生禁用不应删除云端规则")

    def test_toggle_disable_fallback_snapshot(self):
        FAKE.supports_enable = False
        created = FAKE.create_rule(INSTANCE, "203.0.113.42/32", "TCP", "22", "AUTO-ALLOW")
        pk = store.add_rule(self.make_row(rule_id=created["rule_id"]))
        res = rules_service.toggle_rule(pk, False)
        self.assertTrue(res["ok"], res)
        row = store.get_rule(pk)
        self.assertEqual(row["status"], "disabled")
        self.assertEqual(row["rule_id"], "")
        self.assertTrue(row["snapshot"], "回退删除时应保留快照")

    def test_enable_recreate_normalizes_legacy_port(self):
        """回归：历史行 port 可能是 "ALL"，启用时原样下发会再次触发 InvalidPort 400。"""
        pk = store.add_rule(
            self.make_row(rule_id="", port="ALL", protocol="ALL", status="disabled")
        )
        res = rules_service.toggle_rule(pk, True)
        self.assertTrue(res["ok"], res)
        self.assertEqual(FAKE.rules[0]["port"], ALL_PORTS)
        self.assertEqual(FAKE.rules[0]["protocol"], "tcp+udp")

    def test_enable_already_active_refused(self):
        created = FAKE.create_rule(INSTANCE, "203.0.113.42/32", "TCP", "22", "AUTO-ALLOW")
        pk = store.add_rule(self.make_row(rule_id=created["rule_id"]))
        self.assertFalse(rules_service.toggle_rule(pk, True)["ok"])

    def test_update_multi_port_splits_rules(self):
        """一条规则改成多端口时，云端一条规则只能一个端口段 -> 拆成多条。"""
        created = FAKE.create_rule(INSTANCE, "203.0.113.42/32", "TCP", "22", "AUTO-ALLOW")
        pk = store.add_rule(self.make_row(rule_id=created["rule_id"]))
        res = rules_service.update_rule_manual(pk, ports="80,443")
        self.assertTrue(res["ok"], res)
        ports = sorted(str(r["port"]) for r in FAKE.rules)
        self.assertIn("80", ports)
        self.assertIn("443", ports)
        self.assertEqual(len(store.find_rules(status_in=["active"])), 2)

    def test_update_invalid_ip_raises_cloud_error(self):
        created = FAKE.create_rule(INSTANCE, "203.0.113.42/32", "TCP", "22", "AUTO-ALLOW")
        pk = store.add_rule(self.make_row(rule_id=created["rule_id"]))
        with self.assertRaises(CloudError):
            rules_service.update_rule_manual(pk, ip="999.1.1.1")

    def test_update_success(self):
        created = FAKE.create_rule(INSTANCE, "203.0.113.42/32", "TCP", "22", "AUTO-ALLOW")
        pk = store.add_rule(self.make_row(rule_id=created["rule_id"]))
        res = rules_service.update_rule_manual(pk, ip="203.0.113.99", ports="8080", protocol="udp")
        self.assertTrue(res["ok"], res)
        row = store.get_rule(pk)
        self.assertEqual(row["ip"], "203.0.113.99")
        self.assertEqual(row["port"], "8080")
        self.assertEqual(row["protocol"], "udp")
        self.assertEqual(FAKE.rules[0]["port"], "8080")

    def test_update_requires_active(self):
        pk = store.add_rule(self.make_row(status="disabled"))
        self.assertFalse(rules_service.update_rule_manual(pk, ports="22")["ok"])


# --------------------------------------------------------------------------- 6. 定时任务
class CleanupTest(BaseCase):
    def test_expired_cleanup(self):
        created = FAKE.create_rule(INSTANCE, "203.0.113.42/32", "TCP", "22", "AUTO-ALLOW")
        pk = store.add_rule(self.make_row(rule_id=created["rule_id"], expires_at="2000-01-01 00:00:00"))
        result = rules_service.cleanup_expired_rules()
        self.assertEqual(result["removed"], 1)
        self.assertEqual(store.get_rule(pk)["status"], "expired")
        self.assertEqual(FAKE.rules, [])

    def test_expired_cleanup_tolerates_cloud_not_found(self):
        """回归：云端已不存在的规则做清理时会一直失败、永远清不掉。"""
        pk = store.add_rule(self.make_row(rule_id="ghost", expires_at="2000-01-01 00:00:00"))
        result = rules_service.cleanup_expired_rules()
        self.assertEqual(result["failed"], 0, "NotFound 不应计为失败")
        self.assertEqual(store.get_rule(pk)["status"], "expired")

    def test_stale_cleanup(self):
        created = FAKE.create_rule(INSTANCE, "203.0.113.42/32", "TCP", "22", "AUTO-ALLOW old")
        pk = store.add_rule(
            self.make_row(rule_id=created["rule_id"], updated_at="2000-01-01 00:00:00")
        )
        result = rules_service.cleanup_stale_ip_rules()
        self.assertEqual(result["removed"], 1)
        self.assertEqual(store.get_rule(pk)["status"], "stale")

    def test_stale_cleanup_skips_when_policy_off(self):
        settings.update({"rules_policy": {"auto_cleanup_old_ip": False}})
        try:
            self.assertTrue(rules_service.cleanup_stale_ip_rules().get("skipped"))
        finally:
            settings.update({"rules_policy": {"auto_cleanup_old_ip": True}})

    def test_stale_cleanup_ignores_manual_rules(self):
        pk = store.add_rule(
            self.make_row(rule_id="", description="手动添加", updated_at="2000-01-01 00:00:00")
        )
        rules_service.cleanup_stale_ip_rules()
        self.assertEqual(store.get_rule(pk)["status"], "active", "手工规则不应被自动清理")


# --------------------------------------------------------------------------- 7. 镜像同步 / 统计
class MirrorTest(BaseCase):
    def test_replace_marks_missing_as_deleted(self):
        pk = store.add_rule(self.make_row(rule_id="gone-rule"))
        store.replace_instance_rules(INSTANCE, [])
        self.assertEqual(store.get_rule(pk)["status"], "deleted")

    def test_replace_keeps_disabled_state(self):
        pk = store.add_rule(self.make_row(rule_id="r-keep", status="disabled"))
        store.replace_instance_rules(
            INSTANCE,
            [
                {
                    "rule_id": "r-keep",
                    "ip": "203.0.113.42",
                    "cidr": "203.0.113.42/32",
                    "protocol": "tcp",
                    "port": "22",
                    "description": "x",
                }
            ],
        )
        self.assertEqual(store.get_rule(pk)["status"], "disabled", "同步不应把已禁用规则复活")

    def test_replace_adds_new_cloud_rule(self):
        store.replace_instance_rules(
            INSTANCE,
            [
                {
                    "rule_id": "r-new",
                    "ip": "203.0.113.20",
                    "cidr": "203.0.113.20/32",
                    "protocol": "tcp",
                    "port": "80",
                    "description": "控制台手工创建",
                }
            ],
        )
        rows = store.find_rules(rule_id="r-new")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"], "cloud-sync")

    def test_replace_is_idempotent(self):
        """回归：云端同一条规则被反复同步时，本地不能一行行地涨。"""
        cloud = [
            {
                "rule_id": "r-dup",
                "ip": "203.0.113.9",
                "cidr": "203.0.113.9/32",
                "protocol": "tcp+udp",
                "port": ALL_PORTS,
                "description": "公网 IP 自动跟随",
            }
        ]
        for _ in range(6):
            store.replace_instance_rules(INSTANCE, cloud)
        rows = store.find_rules(ip="203.0.113.9", status_in=["active"])
        self.assertEqual(len(rows), 1, f"重复同步不应新增行，实际 {len(rows)} 行")

    def test_replace_adopts_row_with_stale_rule_id(self):
        """rule_id 对不上时按 IP+协议+端口 收编，而不是再插一行。"""
        pk = store.add_rule(
            self.make_row(
                rule_id="old-id",
                ip="203.0.113.10",
                cidr="203.0.113.10/32",
                protocol="tcp+udp",
                port=ALL_PORTS,
                tag="公网IP",
                expires_at="2026-11-04 10:00:00",
                auto_renew=1,
            )
        )
        merged = store.replace_instance_rules(
            INSTANCE,
            [
                {
                    "rule_id": "new-id",
                    "ip": "203.0.113.10",
                    "cidr": "203.0.113.10/32",
                    "protocol": "tcp+udp",
                    "port": ALL_PORTS,
                    "description": "公网 IP 自动跟随",
                }
            ],
        )
        self.assertEqual(merged, 0, "收编不算合并")
        rows = store.find_rules(ip="203.0.113.10", status_in=["active"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], pk, "应命中原有行而不是新建")
        self.assertEqual(rows[0]["rule_id"], "new-id")
        self.assertEqual(rows[0]["tag"], "公网IP", "收编时不能丢掉本地标签")
        self.assertEqual(rows[0]["auto_renew"], 1, "收编时不能丢掉自动续期")

    def test_dedupe_merges_duplicate_rows(self):
        """同 IP+协议+端口的重复行应合并成一条，元数据取最全/最晚。"""
        base = dict(ip="203.0.113.11", cidr="203.0.113.11/32",
                    protocol="tcp+udp", port=ALL_PORTS)
        keeper = store.add_rule(
            self.make_row(rule_id="dup", tag="公网IP", expires_at="2026-01-01 00:00:00", **base)
        )
        store.add_rule(self.make_row(rule_id="dup", auto_renew=1, expires_at="2026-01-02 00:00:00", **base))
        store.add_rule(self.make_row(rule_id="dup", expires_at="2026-01-03 00:00:00", **base))

        removed = store.dedupe_duplicate_rules(instance_id=INSTANCE)
        self.assertEqual(removed, 2)
        active = store.find_rules(ip="203.0.113.11", status_in=["active"])
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["id"], keeper)
        self.assertEqual(active[0]["expires_at"], "2026-01-03 00:00:00", "取最晚到期时间")
        self.assertEqual(active[0]["auto_renew"], 1, "任一行开着续期就保留")
        self.assertEqual(active[0]["tag"], "公网IP")

    def test_stats(self):
        FAKE.create_rule(INSTANCE, "203.0.113.42/32", "TCP", "22", "x")
        store.add_rule(self.make_row(rule_id="rule-0001"))
        store.add_rule(self.make_row(ip="203.0.113.43", port="80", status="disabled"))
        stats = rules_service.stats()
        self.assertEqual(stats["active_rules"], 1)
        self.assertEqual(stats["disabled_rules"], 1)
        self.assertEqual(stats["total_rules"], 2)
        self.assertEqual(stats["unique_ips"], 1)


# --------------------------------------------------------------------------- 8. 指令解析
class ParserTest(BaseCase):
    def test_intents(self):
        from app.dingtalk import parser

        cases = {
            "帮助": "help",
            "help": "help",
            "状态": "status",
            "列表": "list",
            "我的IP": "myip",
            "203.0.113.42": "allow",
            "放行 203.0.113.42 22": "allow",
            "删除 203.0.113.42": "delete",
            "rm 203.0.113.42 22": "delete",
            "你好呀": "unknown",
        }
        for text, intent in cases.items():
            with self.subTest(text=text):
                self.assertEqual(parser.parse(text)["intent"], intent)

    def test_port_forms(self):
        from app.dingtalk import parser

        self.assertEqual(parser.parse("203.0.113.42:22")["ports"], "22")
        self.assertEqual(parser.parse("203.0.113.42 22,80")["ports"], "22,80")
        self.assertEqual(parser.parse("203.0.113.42 8000-9000")["ports"], "8000-9000")
        self.assertEqual(parser.parse("203.0.113.42 53 udp")["protocol"], "udp")
        self.assertEqual(parser.parse("203.0.113.42 全部")["ports"], "ALL")

    def test_multi_ip(self):
        from app.dingtalk import parser

        got = parser.parse("203.0.113.42 203.0.113.43 22")
        self.assertEqual(got["ips"], ["203.0.113.42", "203.0.113.43"])
        self.assertEqual(got["ports"], "22")

    def test_at_prefix_stripped(self):
        from app.dingtalk import parser

        got = parser.parse("@IP放行助手 203.0.113.42 22")
        self.assertEqual(got["intent"], "allow")
        self.assertIn("203.0.113.42", got["ips"])

    def test_empty_means_help(self):
        from app.dingtalk import parser

        self.assertEqual(parser.parse("")["intent"], "help")
        self.assertEqual(parser.parse("   ")["intent"], "help")


# --------------------------------------------------------------------------- 9. cron
class CronTest(BaseCase):
    def test_validate(self):
        from app.cron.cronexpr import validate

        self.assertTrue(validate("0 3 * * *")[0])
        self.assertTrue(validate("*/5 * * * *")[0])
        self.assertFalse(validate("60 * * * *")[0], "分钟 60 越界应被拒")
        self.assertFalse(validate("* * *")[0], "段数不足应被拒")
        self.assertFalse(validate("")[0])

    def test_describe(self):
        from app.cron.cronexpr import describe

        self.assertEqual(describe("0 3 * * *"), "每天 03:00 执行")
        self.assertEqual(describe("*/5 * * * *"), "每 5 分钟执行一次")
        self.assertEqual(describe("0 9 * * 1-5"), "每周 周一、周二、周三、周四、周五 的 09:00 执行")
        self.assertEqual(describe("0 9 * * 0"), "每周 周日 的 09:00 执行")
        self.assertIn("不合法", describe("bad"))

    def test_weekday_semantics_match_standard_cron(self):
        """回归：APScheduler 的 day_of_week 0=周一，与标准 cron(0=周日) 相反。"""
        from app.cron.cronexpr import next_runs

        runs = next_runs("0 9 * * 1-5", 5)
        self.assertEqual(len(runs), 5)
        for text in runs:
            weekday = __import__("datetime").datetime.strptime(text, "%Y-%m-%d %H:%M:%S").weekday()
            self.assertLess(weekday, 5, f"{text} 落在周末，星期语义错误")

    def test_next_runs_count_capped(self):
        from app.cron.cronexpr import next_runs

        self.assertEqual(len(next_runs("0 3 * * *", 100)), 10, "count 上限 10，防滥用")

    def test_summarize(self):
        from app.cron.cronexpr import summarize

        info = summarize("0 3 * * *")
        self.assertEqual(set(info), {"expr", "valid", "error", "description", "next_runs"})
        self.assertTrue(info["valid"])

    # -- 启动补跑：NAS 夜间关机后，开机要补上当天的日级任务
    def _svc(self):
        from app.cron.scheduler import SchedulerService

        return SchedulerService()

    def _moment(self, hour: int, minute: int = 0):
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo("Asia/Shanghai")).replace(
            hour=hour, minute=minute, second=0, microsecond=0
        )

    def test_low_frequency_detection(self):
        from app.cron.cronexpr import make_trigger

        svc, now = self._svc(), self._moment(9, 30)
        self.assertTrue(svc._is_low_frequency(make_trigger("0 4 * * *", "Asia/Shanghai"), now))
        self.assertTrue(svc._is_low_frequency(make_trigger("30 4 * * *", "Asia/Shanghai"), now))
        self.assertFalse(svc._is_low_frequency(make_trigger("*/5 * * * *", "Asia/Shanghai"), now))
        self.assertFalse(svc._is_low_frequency(make_trigger("*/15 * * * *", "Asia/Shanghai"), now))

    def test_missed_daily_job_is_detected(self):
        """上次是昨天，今天 04:00 的触发点已过 → 判定为错过，补跑时刻应为 04:00。"""
        from app.cron.cronexpr import make_trigger

        svc, now = self._svc(), self._moment(9, 30)
        trigger = make_trigger("0 4 * * *", "Asia/Shanghai")
        missed = svc.missed_fire_time(trigger, now - timedelta(days=1), now)
        self.assertIsNotNone(missed, "关机错过 04:00 应被识别")
        self.assertEqual(missed.strftime("%H:%M"), "04:00")

    def test_fresh_daily_job_is_not_missed(self):
        """今天 04:05 刚跑过 → 下一次是明天 04:00，不应补跑。"""
        from app.cron.cronexpr import make_trigger

        svc, now = self._svc(), self._moment(9, 30)
        trigger = make_trigger("0 4 * * *", "Asia/Shanghai")
        last = now.replace(hour=4, minute=5)
        self.assertIsNone(svc.missed_fire_time(trigger, last, now))


# --------------------------------------------------------------------------- 10. 配置中心
class SettingsTest(BaseCase):
    def test_secrets_masked(self):
        settings.update({"aliyun": {"access_key_secret": "very-secret-value"}})
        masked = settings.masked()
        self.assertNotIn("very-secret-value", masked["aliyun"]["access_key_secret"])
        self.assertTrue(masked["aliyun"]["access_key_secret"].startswith("*"))

    def test_roundtrip_masked_value_keeps_original(self):
        """前端把打码值原样提交回来时不能把真密钥覆盖成星号。"""
        settings.update({"aliyun": {"access_key_secret": "very-secret-value"}})
        masked = settings.masked()["aliyun"]["access_key_secret"]
        settings.update({"aliyun": {"access_key_secret": masked}})
        self.assertEqual(settings.get("aliyun.access_key_secret"), "very-secret-value")

    def test_empty_value_keeps_original(self):
        settings.update({"aliyun": {"access_key_secret": "keep-me"}})
        settings.update({"aliyun": {"access_key_secret": ""}})
        self.assertEqual(settings.get("aliyun.access_key_secret"), "keep-me")

    def test_deep_merge_keeps_siblings(self):
        before = settings.get("cron.jobs.cleanup_expired.cron")
        settings.update({"cron": {"jobs": {"cleanup_expired": {"enabled": False}}}})
        try:
            self.assertEqual(settings.get("cron.jobs.cleanup_expired.cron"), before)
            self.assertFalse(settings.get("cron.jobs.cleanup_expired.enabled"))
            self.assertTrue(settings.get("cron.jobs.prune_logs.cron"))
        finally:
            settings.update({"cron": {"jobs": {"cleanup_expired": {"enabled": True}}}})


# --------------------------------------------------------------------------- 11. 渲染
class RenderTest(BaseCase):
    def test_no_fake_example_ip_left(self):
        """回归：用户要求帮助卡片别再用 1.2.3.4。"""
        from app.dingtalk import render

        for text in (render.help_card(), render.unknown_card("x"), render.unknown_reply("x"),
                     render.myip_card(""), render.list_card([])):
            with self.subTest(text=text[:20]):
                self.assertNotIn("1.2.3.4", text)

    def test_plain_vs_card_split(self):
        from app.dingtalk import render

        ok = render.allow_reply(
            {
                "ok": True,
                "protocol": "TCP+UDP",
                "summary": {"created": 1, "skipped": 0, "failed": 0},
                "results": [{"ip": "203.0.113.42", "port": "1/65535", "status": "success"}],
            },
            ttl=60,
        )
        self.assertIsInstance(ok, render.PlainText)
        self.assertIn("全部端口", ok)
        self.assertNotIn("`", ok)

        fail = render.allow_reply(
            {
                "ok": False,
                "protocol": "TCP",
                "summary": {"created": 1, "skipped": 0, "failed": 1},
                "results": [
                    {"ip": "203.0.113.42", "port": "22", "status": "success"},
                    {"ip": "203.0.113.42", "port": "22", "status": "failed", "message": "x"},
                ],
            }
        )
        self.assertNotIsInstance(fail, render.PlainText, "有失败时应降级成卡片展示明细")

    def test_no_rows_gives_plain_error(self):
        from app.dingtalk import render

        reply = render.allow_reply({"ok": False, "message": "没有可用的阿里云实例", "results": []})
        self.assertIsInstance(reply, render.PlainText)
        self.assertIn("没有可用的阿里云实例", reply)


# --------------------------------------------------------------------------- 12. Web API
class WebApiTest(BaseCase):
    @classmethod
    def setUpClass(cls):
        from app.web.app import create_app

        # 注意：默认口令 admin123 会触发「强制改密」闸门（428），
        # 所以测试环境直接给一个强口令，改密流程另有专门用例覆盖。
        settings.update(
            {"server": {"admin_user": "admin", "admin_password": TEST_PASSWORD, "must_change_password": False}}
        )
        cls.app = create_app(start_services=False)
        cls.app.config.update(TESTING=True)
        cls.client = cls.app.test_client()
        cls.client.post("/api/login", json={"username": "admin", "password": TEST_PASSWORD})

    def test_requires_login(self):
        anon = self.app.test_client()
        for path in ("/api/rules", "/api/settings", "/api/overview"):
            with self.subTest(path=path):
                self.assertEqual(anon.get(path).status_code, 401)

    def test_index_redirects_when_anonymous(self):
        anon = self.app.test_client()
        self.assertEqual(anon.get("/").status_code, 302)

    def test_login_wrong_password(self):
        anon = self.app.test_client()
        resp = anon.post("/api/login", json={"username": "admin", "password": "nope"})
        self.assertEqual(resp.status_code, 401)

    def test_rule_list_filters(self):
        store.add_rule(self.make_row(status="active", ip="203.0.113.30"))
        store.add_rule(self.make_row(status="deleted", ip="203.0.113.31"))

        data = self.client.get("/api/rules?status=current").get_json()["data"]
        self.assertEqual({r["ip"] for r in data["items"]}, {"203.0.113.30"})
        self.assertEqual(data["total"], 1)

        gone = self.client.get("/api/rules?status=gone").get_json()["data"]
        self.assertEqual({r["ip"] for r in gone["items"]}, {"203.0.113.31"})

        self.assertTrue(all("port_display" in r for r in data["items"]))

    def test_create_rule_all_ports(self):
        resp = self.client.post(
            "/api/rules",
            json={"instance_id": INSTANCE, "ip": "203.0.113.40", "ports": "ALL", "protocol": "tcp"},
        )
        payload = resp.get_json()
        self.assertTrue(payload["ok"], payload)
        self.assertEqual(FAKE.rules[0]["port"], ALL_PORTS)

    def test_create_rule_invalid_port_returns_400_not_500(self):
        """回归：以前 split_ports 抛 ValueError，Flask 直接 500。"""
        resp = self.client.post(
            "/api/rules",
            json={"instance_id": INSTANCE, "ip": "203.0.113.41", "ports": "http"},
        )
        self.assertEqual(resp.status_code, 400, resp.data[:200])
        self.assertFalse(resp.get_json()["ok"])

    def test_update_rule_invalid_ip_returns_400_not_500(self):
        created = FAKE.create_rule(INSTANCE, "203.0.113.42/32", "TCP", "22", "x")
        pk = store.add_rule(self.make_row(rule_id=created["rule_id"], ip="203.0.113.42",
                                          cidr="203.0.113.42/32"))
        resp = self.client.put(f"/api/rules/{pk}", json={"ip": "999.1.1.1"})
        self.assertEqual(resp.status_code, 400, resp.data[:200])

    def test_delete_then_restore_then_purge(self):
        created = FAKE.create_rule(INSTANCE, "203.0.113.43/32", "TCP", "22", "x")
        pk = store.add_rule(self.make_row(rule_id=created["rule_id"], ip="203.0.113.43",
                                          cidr="203.0.113.43/32"))

        self.assertEqual(self.client.delete(f"/api/rules/{pk}").status_code, 200)
        self.assertEqual(self.client.delete(f"/api/rules/{pk}").status_code, 200, "重复删除应幂等")

        self.assertEqual(self.client.post(f"/api/rules/{pk}/restore").status_code, 200)
        self.assertEqual(store.get_rule(pk)["status"], "active")

        self.client.delete(f"/api/rules/{pk}")
        self.assertEqual(self.client.delete(f"/api/rules/{pk}/purge").status_code, 200)
        self.assertIsNone(store.get_rule(pk))

    def test_bad_limit_does_not_500(self):
        """回归：int(request.args['limit']) 遇到 'abc' 直接 500。"""
        for path in ("/api/logs?limit=abc", "/api/cron/runs?limit=abc", "/api/bot/messages?limit=abc"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200)

    def test_cron_preview(self):
        ok = self.client.post("/api/cron/preview", json={"expr": "0 3 * * *"}).get_json()
        self.assertTrue(ok["ok"])
        self.assertEqual(ok["data"]["description"], "每天 03:00 执行")

        bad = self.client.post("/api/cron/preview", json={"expr": "bad"})
        self.assertEqual(bad.status_code, 400)

    def test_cron_job_update_validates(self):
        resp = self.client.put("/api/cron/jobs/cleanup_expired", json={"cron": "bad expr"})
        self.assertEqual(resp.status_code, 400)
        resp = self.client.put("/api/cron/jobs/not_exist", json={"cron": "0 3 * * *"})
        self.assertEqual(resp.status_code, 404)

    def test_settings_masks_secret(self):
        settings.update({"aliyun": {"access_key_secret": "super-secret"}})
        data = self.client.get("/api/settings").get_json()["data"]
        self.assertNotIn("super-secret", data["aliyun"]["access_key_secret"])

    def test_webhook_rejects_when_secret_missing_and_remote(self):
        """未配置 secret 时，webhook 不应对公网开放（否则谁都能放行任意 IP）。"""
        settings.update({"webhook": {"secret": ""}})
        resp = self.client.post(
            "/webhook/ip-change",
            json={"ipv4": "203.0.113.50"},
            environ_base={"REMOTE_ADDR": "8.8.8.8"},
        )
        self.assertEqual(resp.status_code, 403, resp.data[:200])
        self.assertEqual(FAKE.rules, [], "未授权请求不应真的放行")

    def test_webhook_local_allowed_without_secret(self):
        settings.update({"webhook": {"secret": ""}})
        resp = self.client.post(
            "/webhook/ip-change",
            json={"ipv4": "203.0.113.51", "ports": "22"},
            environ_base={"REMOTE_ADDR": "127.0.0.1"},
        )
        self.assertEqual(resp.status_code, 200, resp.data[:300])
        self.assertEqual(len(FAKE.rules), 1)

    def test_webhook_secret_required_when_configured(self):
        settings.update({"webhook": {"secret": "s3cr3t"}})
        try:
            bad = self.client.post("/webhook/ip-change", json={"ipv4": "203.0.113.52"})
            self.assertEqual(bad.status_code, 401)
            good = self.client.post(
                "/webhook/ip-change",
                json={"ipv4": "203.0.113.52", "ports": "22"},
                headers={"X-Webhook-Secret": "s3cr3t"},
            )
            self.assertEqual(good.status_code, 200, good.data[:300])
        finally:
            settings.update({"webhook": {"secret": ""}})

    def test_ip_allow_direct(self):
        resp = self.client.post(
            "/api/ip/allow",
            json={"ip": "203.0.113.53", "ports": "22", "via_webhook": False},
        )
        self.assertEqual(resp.status_code, 200, resp.data[:300])
        self.assertTrue(FAKE.rules)

    def test_cloud_rule_action_creates_local_mirror(self):
        created = FAKE.create_rule(INSTANCE, "203.0.113.54/32", "TCP", "22", "手工")
        resp = self.client.post(
            "/api/cloud/rule-action",
            json={
                "instance_id": INSTANCE,
                "rule_id": created["rule_id"],
                "action": "delete",
                "ip": "203.0.113.54",
                "port": "22",
            },
        )
        self.assertEqual(resp.status_code, 200, resp.data[:300])
        rows = store.find_rules(ip="203.0.113.54")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "deleted")


# --------------------------------------------------------------------------- 8. 新功能（分页 / 标签 / 续期 / 差异 / 令牌 / 备份 / 指标）
class NewFeaturesTest(BaseCase):
    @classmethod
    def setUpClass(cls):
        from app.web.app import create_app

        settings.update(
            {"server": {"admin_user": "admin", "admin_password": TEST_PASSWORD, "must_change_password": False}}
        )
        cls.app = create_app(start_services=False)
        cls.app.config.update(TESTING=True)
        cls.client = cls.app.test_client()
        cls.client.post("/api/login", json={"username": "admin", "password": TEST_PASSWORD})

    # -- 分页
    def test_rules_pagination_envelope(self):
        for i in range(25):
            store.add_rule(self.make_row(ip="203.0.113.%d" % (100 + i), port=str(1000 + i)))
        data = self.client.get("/api/rules?page=2&page_size=10").get_json()["data"]
        self.assertEqual(data["total"], 25)
        self.assertEqual(data["pages"], 3)
        self.assertEqual(data["page"], 2)
        self.assertEqual(len(data["items"]), 10)

    def test_page_size_is_clamped(self):
        """page_size=9999 不能让服务端一次吐全表。"""
        data = self.client.get("/api/rules?page_size=9999").get_json()["data"]
        self.assertLessEqual(data["page_size"], 200)
        data2 = self.client.get("/api/rules?page=abc&page_size=xyz").get_json()["data"]
        self.assertEqual(data2["page"], 1)

    # -- 标签
    def test_tag_filter_and_tags_summary(self):
        store.add_rule(self.make_row(ip="203.0.113.201", tag="home"))
        store.add_rule(self.make_row(ip="203.0.113.202", tag="home"))
        store.add_rule(self.make_row(ip="203.0.113.203", tag="office"))

        data = self.client.get("/api/rules?tag=home").get_json()["data"]
        self.assertEqual({r["ip"] for r in data["items"]}, {"203.0.113.201", "203.0.113.202"})
        self.assertEqual(data["total"], 2)

        tags = self.client.get("/api/tags").get_json()["data"]
        counts = {t["tag"]: t["total"] for t in tags}
        self.assertEqual(counts.get("home"), 2)
        self.assertEqual(counts.get("office"), 1)

    def test_keyword_also_matches_tag(self):
        store.add_rule(self.make_row(ip="203.0.113.210", tag="客户A"))
        store.add_rule(self.make_row(ip="203.0.113.211", tag="客户B"))
        data = self.client.get("/api/rules?keyword=客户A").get_json()["data"]
        self.assertEqual([r["ip"] for r in data["items"]], ["203.0.113.210"])

    def test_create_rule_with_tag_and_multi_instance(self):
        FKEYS = {"instance_ids": [INSTANCE], "ip": "203.0.113.212", "ports": "22",
                 "protocol": "tcp", "tag": "batch", "ttl_minutes": 0}
        resp = self.client.post("/api/rules", json=FKEYS)
        self.assertEqual(resp.status_code, 200, resp.data[:300])
        rows = store.find_rules(ip="203.0.113.212")
        self.assertEqual(rows[0]["tag"], "batch")

    # -- 续期
    def test_renew_extends_expiry(self):
        from datetime import datetime, timedelta

        past = (datetime.now() - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
        pk = store.add_rule(self.make_row(ip="203.0.113.220", expires_at=past))
        resp = self.client.post("/api/rules/%d/renew" % pk, json={"minutes": 60})
        self.assertEqual(resp.status_code, 200, resp.data[:300])
        row = store.get_rule(pk)
        self.assertGreater(row["expires_at"], past)
        self.assertEqual(row["notified_at"], "")

    def test_renew_rejects_gone_rule(self):
        pk = store.add_rule(self.make_row(ip="203.0.113.221", status="expired"))
        resp = self.client.post("/api/rules/%d/renew" % pk, json={"minutes": 60})
        self.assertEqual(resp.status_code, 400)

    def test_batch_renew(self):
        ids = [store.add_rule(self.make_row(ip="203.0.113.%d" % (230 + i))) for i in range(3)]
        resp = self.client.post("/api/rules/renew", json={"ids": ids, "minutes": 30})
        self.assertEqual(resp.status_code, 200, resp.data[:300])
        self.assertEqual(resp.get_json()["data"]["renewed"], 3)

    def test_expiring_notice_marks_notified(self):
        from datetime import datetime, timedelta

        soon = (datetime.now() + timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
        pk = store.add_rule(self.make_row(ip="203.0.113.240", expires_at=soon))
        rows = store.expiring_soon_rules(10)
        self.assertTrue(any(r["id"] == pk for r in rows))
        rules_service.notify_expiring(rows, 10)
        self.assertNotEqual(store.get_rule(pk)["notified_at"], "")
        # 已提醒过的不该再次进入待提醒队列
        self.assertFalse(any(r["id"] == pk for r in store.expiring_soon_rules(10)))

    # -- 指标
    def test_metrics_has_series_and_no_secret(self):
        store.add_rule(self.make_row(ip="203.0.113.241"))
        resp = self.client.get("/metrics")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertIn("dcf_rules", body)
        self.assertIn("dcf_uptime_seconds", body)
        self.assertNotIn("admin123", body)
        self.assertNotIn("access_key", body.lower())

    def test_metrics_can_be_private(self):
        settings.update({"server": {"metrics_public": False}})
        try:
            anon = self.app.test_client()
            self.assertEqual(anon.get("/metrics").status_code, 403)
        finally:
            settings.update({"server": {"metrics_public": True}})

    # -- 令牌分权
    def test_readonly_token_cannot_write(self):
        settings.update({"server": {"api_token": "write-token-t", "api_token_readonly": "read-token-t"}})
        anon = self.app.test_client()
        ro = {"X-Api-Token": "read-token-t"}
        rw = {"X-Api-Token": "write-token-t"}

        self.assertEqual(anon.get("/api/rules", headers=ro).status_code, 200)
        self.assertEqual(anon.get("/api/tags", headers=ro).status_code, 200)
        denied = anon.post("/api/rules", headers=ro, json={"instance_id": INSTANCE, "ip": "203.0.113.250"})
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(denied.get_json().get("code"), "READONLY_TOKEN")
        # 读写 Token 可以写
        allowed = anon.post("/api/rules", headers=rw, json={"instance_id": INSTANCE, "ip": "203.0.113.251"})
        self.assertEqual(allowed.status_code, 200, allowed.data[:300])

    def test_tokens_endpoint_needs_session(self):
        settings.update({"server": {"api_token": "write-token-t2"}})
        anon = self.app.test_client()
        resp = anon.get("/api/account/tokens", headers={"X-Api-Token": "write-token-t2"})
        self.assertEqual(resp.status_code, 403)

    # -- 备份
    def test_backup_run_creates_file(self):
        resp = self.client.post("/api/backup/run")
        self.assertEqual(resp.status_code, 200, resp.data[:300])
        files = resp.get_json()["data"]["files"]
        self.assertTrue(files, "备份后应该至少有一份文件")
        self.assertTrue(files[0]["name"].startswith("app-"))
        listing = self.client.get("/api/backups").get_json()["data"]["files"]
        self.assertTrue(any(f["name"] == files[0]["name"] for f in listing))

    def test_backup_download_rejects_bad_name(self):
        resp = self.client.get("/api/backups/notabackup.txt")
        self.assertIn(resp.status_code, (400, 404))

    # -- 归属地（缓存优先，不联网）
    def test_geo_cache_avoids_network(self):
        from app.core import geo

        # 用真实公网地址：203.0.113.0/24 是文档保留段，会被 _is_public 过滤掉
        store.set_geo("223.5.5.5", {"country": "中国", "region": "浙江", "city": "杭州", "isp": "电信"})
        original = geo._fetch
        geo._fetch = lambda ip: (_ for _ in ()).throw(AssertionError("命中缓存还去查网络"))
        try:
            info = geo.lookup("223.5.5.5")
        finally:
            geo._fetch = original
        self.assertEqual(info["city"], "杭州")
        self.assertEqual(geo.describe(info), "浙江 杭州 · 电信")

    def test_geo_skips_private_ip(self):
        from app.core import geo

        self.assertEqual(geo.lookup("192.168.1.1"), {})
        self.assertEqual(geo.lookup("127.0.0.1"), {})

    # -- 通知模板
    def test_notify_template_unknown_placeholder_safe(self):
        from app.core import notifier

        row = {"ip": "203.0.113.1", "port": "22", "protocol": "tcp",
               "instance_name": "x", "actor": "tester"}
        line = notifier.render_line("allow", row, "{ip} {port} {nope} {action}")
        self.assertIn("203.0.113.1", line)
        self.assertNotIn("{nope}", line)
        self.assertIn("放行", line)

    def test_notify_disabled_by_default(self):
        """默认不推送，避免测试/新部署时误发群消息。"""
        self.assertFalse(settings.get("notify.enabled", False))

    # -- 强制改密
    def test_password_gate_blocks_until_changed(self):
        settings.update({"server": {"admin_password": "admin123", "must_change_password": True}})
        try:
            c = self.app.test_client()
            c.post("/api/login", json={"username": "admin", "password": "admin123"})
            self.assertEqual(c.get("/api/rules").status_code, 428)
            # 改密接口必须豁免，否则用户被锁死在外面
            changed = c.post(
                "/api/account/password",
                json={"old_password": "admin123", "new_password": "NewPass@2026",
                      "confirm_password": "NewPass@2026"},
            )
            self.assertEqual(changed.status_code, 200, changed.data[:300])
            self.assertEqual(c.get("/api/rules").status_code, 200)
        finally:
            settings.update({"server": {"admin_password": TEST_PASSWORD, "must_change_password": False}})

    def test_password_policy_rejects_weak(self):
        c = self.app.test_client()
        c.post("/api/login", json={"username": "admin", "password": TEST_PASSWORD})
        weak = c.post("/api/account/password",
                      json={"old_password": TEST_PASSWORD, "new_password": "admin123",
                            "confirm_password": "admin123"})
        self.assertEqual(weak.status_code, 400)
        mismatch = c.post("/api/account/password",
                          json={"old_password": TEST_PASSWORD, "new_password": "GoodPass@2026",
                                "confirm_password": "GoodPass@2027"})
        self.assertEqual(mismatch.status_code, 400)


    # -- 续期方式（renew_mode）透传
    def test_create_rule_api_persists_renew_mode(self):
        resp = self.client.post(
            "/api/rules",
            json={"instance_ids": [INSTANCE], "ip": "203.0.113.231", "ports": "22",
                  "protocol": "tcp", "auto_renew": True, "renew_mode": "same", "ttl_minutes": 0},
        )
        self.assertEqual(resp.status_code, 200, resp.data[:300])
        rows = store.find_rules(ip="203.0.113.231")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["renew_mode"], "same")
        self.assertEqual(rows[0]["auto_renew"], 1)

# --------------------------------------------------------------------------- 自动续期 + 公网 IP 跟随
class RenewAndPublicIpTest(BaseCase):
    """覆盖：规则级自动续期（IP 不变顺延 / 变了跟随）、每日公网 IP 同步、过期清理先续期。

    全程不碰公网：用猴子补丁把 ``_probe_public_ip`` 固定成假 IP，避免测试触发真实探测。
    """

    def setUp(self) -> None:
        super().setUp()
        self._orig_probe = rules_service._probe_public_ip
        rules_service._probe_public_ip = lambda *a, **k: "203.0.113.88"
        self._orig_ttl = settings.get("rules_policy.default_ttl_minutes")
        self._orig_pub = settings.section("public_ip").get("enabled")
        settings.update(
            {
                "rules_policy": {"default_ttl_minutes": 120, "auto_cleanup_old_ip": True},
                "public_ip": {
                    "enabled": True,
                    "ports": "ALL",
                    "protocol": "tcp",
                    "ttl_minutes": 0,
                    "tag": "公网IP",
                },
            }
        )

    def tearDown(self) -> None:
        rules_service._probe_public_ip = self._orig_probe
        settings.update(
            {
                "rules_policy": {"default_ttl_minutes": self._orig_ttl},
                "public_ip": {"enabled": self._orig_pub},
            }
        )

    def _expired_row(self, **over) -> dict:
        past = (datetime.now() - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
        return self.make_row(expires_at=past, auto_renew=1, **over)

    def test_auto_renew_flag_persists_and_toggles(self):
        pk = store.add_rule(self.make_row(auto_renew=1))
        self.assertEqual(store.get_rule(pk)["auto_renew"], 1)
        res = rules_service.update_rule_manual(pk, auto_renew=False)
        self.assertTrue(res["ok"])
        self.assertEqual(store.get_rule(pk)["auto_renew"], 0)

    def test_auto_renew_extends_when_ip_unchanged(self):
        pk = store.add_rule(
            self._expired_row(ip="203.0.113.88", cidr="203.0.113.88/32")
        )
        before_cloud = len(FAKE.rules)
        result = rules_service.auto_renew_due_rules()
        row = store.get_rule(pk)
        self.assertEqual(result["renewed"], 1)
        self.assertEqual(result["followed"], 0)
        # IP 没变：只在本地顺延有效期，云端规则不动
        self.assertEqual(len(FAKE.rules), before_cloud)
        self.assertTrue(row["expires_at"] > now_str())

    def test_auto_renew_follows_when_ip_changed(self):
        pk = store.add_rule(
            self._expired_row(
                ip="203.0.113.42", cidr="203.0.113.42/32", rule_id="old-rule-42"
            )
        )
        FAKE.rules.append(
            {
                "rule_id": "old-rule-42",
                "instance_id": INSTANCE,
                "cidr": "203.0.113.42/32",
                "ip": "203.0.113.42",
                "protocol": "tcp",
                "port": "ALL",
                "description": "x",
                "policy": "accept",
                "instance_name": "",
            }
        )
        before = len(FAKE.rules)
        result = rules_service.auto_renew_due_rules()
        row = store.get_rule(pk)
        self.assertEqual(result["followed"], 1)
        self.assertEqual(row["ip"], "203.0.113.88")
        self.assertEqual(row["cidr"], "203.0.113.88/32")
        # 旧云端规则删 1 条、新规则建 1 条 → 净数量不变
        self.assertEqual(len(FAKE.rules), before)

    def test_sync_public_ip_creates_and_is_idempotent(self):
        r1 = rules_service.sync_public_ip_rule()
        self.assertTrue(r1["ok"])
        self.assertEqual(r1["created"], 1)
        rows = store.find_rules(status_in=["active"], source="public_ip")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["auto_renew"], 1)
        self.assertEqual(rows[0]["ip"], "203.0.113.88")
        # 第二次：IP 没变，只刷新有效期，不新建云端规则
        before_cloud = len(FAKE.rules)
        r2 = rules_service.sync_public_ip_rule()
        self.assertTrue(r2["ok"])
        self.assertEqual(r2["created"], 0)
        self.assertEqual(r2["refreshed"], 1)
        self.assertEqual(len(FAKE.rules), before_cloud)

    def test_sync_public_ip_replaces_stale_ip(self):
        pk = store.add_rule(
            self.make_row(
                ip="203.0.113.10",
                cidr="203.0.113.10/32",
                rule_id="stale-10",
                source="public_ip",
                auto_renew=1,
            )
        )
        FAKE.rules.append(
            {
                "rule_id": "stale-10",
                "instance_id": INSTANCE,
                "cidr": "203.0.113.10/32",
                "ip": "203.0.113.10",
                "protocol": "tcp",
                "port": "ALL",
                "description": "x",
                "policy": "accept",
                "instance_name": "",
            }
        )
        rules_service._probe_public_ip = lambda *a, **k: "203.0.113.77"
        result = rules_service.sync_public_ip_rule()
        self.assertTrue(result["ok"])
        self.assertEqual(result["created"], 1)
        self.assertEqual(result["replaced"], 1)
        rows = store.find_rules(status_in=["active"], source="public_ip")
        self.assertEqual(len([r for r in rows if r["ip"] == "203.0.113.77"]), 1)
        self.assertEqual(len([r for r in rows if r["ip"] == "203.0.113.10"]), 0)

    def test_public_ip_ttl_parsing(self):
        """-1 = 永久；0 = 跟随全局默认；正数 = 原样使用。"""
        ttl_of = rules_service._public_ip_ttl
        self.assertEqual(ttl_of({"ttl_minutes": -1}), 0, "负数应表示永久（不写到期时间）")
        self.assertEqual(ttl_of({"ttl_minutes": -30}), 0)
        self.assertEqual(ttl_of({"ttl_minutes": 0}), 120, "0 应跟随 default_ttl_minutes=120")
        self.assertEqual(ttl_of({"ttl_minutes": 1440}), 1440)
        self.assertEqual(ttl_of({}), 120, "缺省按 0 处理")

    def test_public_ip_permanent_rule_never_expires(self):
        """ttl_minutes=-1 生成的规则没有到期时间，重复同步也不能把到期时间塞回来。"""
        settings.update({"public_ip": {"ttl_minutes": -1}})
        try:
            first = rules_service.sync_public_ip_rule()
            self.assertTrue(first["ok"])
            self.assertEqual(first["expires_at"], "")
            rows = store.find_rules(status_in=["active"], source="public_ip")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["expires_at"], "")

            # IP 未变化的第二次同步：永久标记必须保留
            rules_service.sync_public_ip_rule()
            rows = store.find_rules(status_in=["active"], source="public_ip")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["expires_at"], "", "永久规则被回填了到期时间")

            # 永久规则不参与过期清理
            result = rules_service.cleanup_expired_rules()
            self.assertEqual(result["total"], 0)
            self.assertEqual(store.find_rules(status_in=["active"], source="public_ip")[0]["status"], "active")
        finally:
            settings.update({"public_ip": {"ttl_minutes": 0}})

    def test_permanent_rule_still_follows_ip_change(self):
        """永久放行只是不按到期回收；IP 变了照样要删旧建新，且新规则仍是永久。"""
        settings.update({"public_ip": {"ttl_minutes": -1}})
        try:
            rules_service._probe_public_ip = lambda *a, **k: "203.0.113.88"
            rules_service.sync_public_ip_rule()
            self.assertEqual(len(store.find_rules(status_in=["active"], source="public_ip")), 1)

            rules_service._probe_public_ip = lambda *a, **k: "203.0.113.99"
            res = rules_service.sync_public_ip_rule()
            self.assertTrue(res["ok"])
            self.assertEqual(res["created"], 1)
            self.assertEqual(res["replaced"], 1)

            rows = store.find_rules(status_in=["active"], source="public_ip")
            self.assertEqual([r["ip"] for r in rows], ["203.0.113.99"], "永久规则也应跟随新 IP")
            self.assertEqual(rows[0]["expires_at"], "", "跟随新 IP 后仍应保持永久")
        finally:
            settings.update({"public_ip": {"ttl_minutes": 0}})

    def test_ip_change_pushes_notification(self):
        """IP 变化并跟随成功 → 推一条钉钉通知；开关关闭后不再推。"""
        from app.dingtalk import client as ding_client

        sent = []
        orig = ding_client.notify
        ding_client.notify = lambda text, title="": bool(sent.append(text)) or True
        try:
            pk = store.add_rule(
                self.make_row(
                    ip="203.0.113.10", cidr="203.0.113.10/32",
                    rule_id="stale-10n", source="public_ip", auto_renew=1,
                )
            )
            FAKE.rules.append(
                {"rule_id": "stale-10n", "instance_id": INSTANCE, "cidr": "203.0.113.10/32",
                 "ip": "203.0.113.10", "protocol": "tcp", "port": "ALL",
                 "description": "x", "policy": "accept", "instance_name": ""}
            )
            rules_service._probe_public_ip = lambda *a, **k: "203.0.113.77"
            result = rules_service.sync_public_ip_rule()
            self.assertTrue(result["ok"], "先确保放行本身成功")
            self.assertEqual(len(sent), 1, "IP 变化应推送一条通知")
            self.assertIn("203.0.113.77", sent[0], "通知里要有新 IP")
            self.assertIn("203.0.113.10", sent[0], "通知里要有旧 IP")
            self.assertTrue(store.get_rule(pk)["status"] != "active", "旧规则应已回收")

            # 关闭开关 → 不再推送
            settings.update({"public_ip": {"notify_on_change": False}})
            sent.clear()
            rules_service._probe_public_ip = lambda *a, **k: "203.0.113.78"
            rules_service.sync_public_ip_rule()
            self.assertEqual(sent, [], "关闭通知开关后不应再推送")
        finally:
            ding_client.notify = orig
            settings.update({"public_ip": {"notify_on_change": True}})

    def test_cleanup_renews_before_deleting(self):
        # 到期且开启自动续期的规则，过期清理应当先续期而不是直接删除
        pk = store.add_rule(
            self._expired_row(ip="203.0.113.88", cidr="203.0.113.88/32")
        )
        result = rules_service.cleanup_expired_rules()
        row = store.get_rule(pk)
        self.assertEqual(row["status"], "active", "自动续期后不应被清理删除")
        self.assertTrue(row["expires_at"] > now_str())
        self.assertEqual(result.get("auto_renew", {}).get("renewed", 0), 1)


    def test_auto_renew_same_mode_keeps_ip(self):
        # 「原 IP 续期」：即便公网 IP 已变化，也只顺延有效期、保持原 IP、不动云端
        pk = store.add_rule(
            self._expired_row(ip="203.0.113.42", cidr="203.0.113.42/32", renew_mode="same")
        )
        before_cloud = len(FAKE.rules)
        result = rules_service.auto_renew_due_rules()
        row = store.get_rule(pk)
        self.assertEqual(result["renewed_same"], 1)
        self.assertEqual(result["followed"], 0)
        # IP 保持不变，云端规则数量不变（没删、没建）
        self.assertEqual(row["ip"], "203.0.113.42")
        self.assertEqual(len(FAKE.rules), before_cloud)
        self.assertTrue(row["expires_at"] > now_str())

    def test_auto_renew_follow_mode_explicit(self):
        # 显式 follow 模式 + 公网 IP 变了 → 跟随新 IP 重建
        pk = store.add_rule(
            self._expired_row(
                ip="203.0.113.42", cidr="203.0.113.42/32",
                rule_id="old-rule-42b", renew_mode="follow",
            )
        )
        FAKE.rules.append(
            {"rule_id": "old-rule-42b", "instance_id": INSTANCE, "cidr": "203.0.113.42/32",
             "ip": "203.0.113.42", "protocol": "tcp", "port": "ALL",
             "description": "x", "policy": "accept", "instance_name": ""}
        )
        before = len(FAKE.rules)
        result = rules_service.auto_renew_due_rules()
        row = store.get_rule(pk)
        self.assertEqual(result["followed"], 1)
        self.assertEqual(result["renewed_same"], 0)
        self.assertEqual(row["ip"], "203.0.113.88")
        self.assertEqual(len(FAKE.rules), before)

    def test_update_rule_manual_sets_and_preserves_renew_mode(self):
        pk = store.add_rule(self.make_row(auto_renew=1))
        # 仅改续期方式
        res = rules_service.update_rule_manual(pk, renew_mode="same")
        self.assertTrue(res["ok"])
        self.assertEqual(store.get_rule(pk)["renew_mode"], "same")
        # 仅切换自动续期开关时，renew_mode 不应被翻回默认
        res = rules_service.update_rule_manual(pk, auto_renew=False)
        self.assertTrue(res["ok"])
        self.assertEqual(store.get_rule(pk)["auto_renew"], 0)
        self.assertEqual(store.get_rule(pk)["renew_mode"], "same")

class LegacyDbMigrationTest(unittest.TestCase):
    """老库升级：rules 表没有 tag/notified_at 时也必须能启动。

    曾经的 bug：_SCHEMA 里直接 CREATE INDEX ... ON rules(tag)，而 executescript
    在 _migrate() 之前执行，老库会直接 no such column: tag 起不来。
    """

    def test_old_schema_db_can_boot(self):
        import sqlite3
        from app.store import Store

        path = os.path.join(_TMP, "legacy.db")
        con = sqlite3.connect(path)
        con.executescript(
            """
            CREATE TABLE rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                provider TEXT NOT NULL DEFAULT 'aliyun_swas',
                instance_id TEXT NOT NULL,
                instance_name TEXT DEFAULT '',
                rule_id TEXT DEFAULT '',
                ip TEXT NOT NULL,
                cidr TEXT DEFAULT '',
                protocol TEXT NOT NULL DEFAULT 'tcp',
                port TEXT NOT NULL DEFAULT 'ALL',
                description TEXT DEFAULT '',
                status TEXT NOT NULL DEFAULT 'active',
                source TEXT NOT NULL DEFAULT 'web',
                actor TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                expires_at TEXT DEFAULT '',
                snapshot TEXT DEFAULT ''
            );
            INSERT INTO rules(instance_id, ip, created_at, updated_at)
            VALUES('i-old', '1.1.1.1', '2026-01-01 00:00:00', '2026-01-01 00:00:00');
            """
        )
        con.commit()
        con.close()

        s = Store(path)  # 不抛异常即通过
        cols = {r["name"] for r in s._conn.execute("PRAGMA table_info(rules)")}
        self.assertIn("tag", cols)
        self.assertIn("notified_at", cols)
        self.assertEqual(s.count_rules(), 1)
        idx = [r[0] for r in s._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_rules_tag'")]
        self.assertEqual(idx, ["idx_rules_tag"])

        # 再开一次，验证迁移幂等
        Store(path)
        s2 = Store(path)
        self.assertEqual(s2.count_rules(), 1)


# --------------------------------------------------------------------------- 13. 二次验证（2FA / TOTP）
class TwoFactorTest(BaseCase):
    @classmethod
    def setUpClass(cls):
        from app.web.app import create_app

        settings.update(
            {"server": {"admin_user": "admin", "admin_password": TEST_PASSWORD, "must_change_password": False}}
        )
        _reset_twofa()
        cls.app = create_app(start_services=False)
        cls.app.config.update(TESTING=True)
        cls.client = cls.app.test_client()
        cls.client.post("/api/login", json={"username": "admin", "password": TEST_PASSWORD})

    def setUp(self):
        super().setUp()
        # 每个用例都从「未启用、无密钥」开始；tearDown 也复位，避免污染其他测试类
        _reset_twofa()
        self.client = self.app.test_client()
        self.client.post("/api/login", json={"username": "admin", "password": TEST_PASSWORD})

    def tearDown(self):
        _reset_twofa()

    # -- 算法层
    def test_totp_matches_rfc6238_vectors(self):
        import base64

        secret = base64.b32encode(b"12345678901234567890").decode()
        self.assertEqual(twofa.totp(secret, at=59, digits=8), "94287082")
        self.assertEqual(twofa.totp(secret, at=1111111109, digits=8), "07081804")

    def test_generate_and_verify(self):
        secret = twofa.generate_secret()
        self.assertTrue(twofa.is_valid_secret(secret))
        code = twofa.totp(secret)
        self.assertTrue(twofa.verify(secret, code))
        self.assertFalse(twofa.verify(secret, "abc"))
        self.assertFalse(twofa.verify("not-base32!", code))
        self.assertFalse(twofa.verify(secret, ""))

    def test_otpauth_uri_and_qr(self):
        secret = twofa.generate_secret()
        uri = twofa.otpauth_uri(secret, account="admin", issuer="测试发行方")
        self.assertTrue(uri.startswith("otpauth://totp/"))
        self.assertIn("secret=", uri)
        self.assertIn("issuer=", uri)
        self.assertIn("<svg", twofa.qr_svg(uri))

    def test_extract_code_variants(self):
        code, cleaned = twofa.extract_code("1.2.3.4 22 code=123456")
        self.assertEqual(code, "123456")
        self.assertNotIn("123456", cleaned)
        code2, _ = twofa.extract_code("放行 1.2.3.4 #654321")
        self.assertEqual(code2, "654321")
        code3, same = twofa.extract_code("1.2.3.4 22")
        self.assertEqual(code3, "")
        self.assertEqual(same, "1.2.3.4 22")

    # -- 接口层
    def test_api_generate_and_status(self):
        data = self.client.post("/api/2fa/generate", json={}).get_json()["data"]
        self.assertTrue(data["secret"])
        self.assertIn("qr_svg", data)
        status = self.client.get("/api/2fa/status").get_json()["data"]
        self.assertTrue(status["has_secret"])
        self.assertFalse(status["enabled"])
        self.assertNotIn("secret", status)  # 只返回掩码，不吐明文

    def test_api_enable_requires_secret(self):
        resp = self.client.put("/api/2fa/config", json={"enabled": True})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json().get("code"), "NO_SECRET")

    def test_api_verify_and_enable(self):
        secret = twofa.generate_secret()
        self.client.put("/api/2fa/secret", json={"secret": secret})
        code = twofa.totp(secret)
        self.assertTrue(self.client.post("/api/2fa/verify", json={"code": code}).get_json()["ok"])
        cfg = self.client.put("/api/2fa/config", json={"enabled": True, "scope": {"webhook": True}})
        self.assertTrue(cfg.get_json()["ok"])
        status = self.client.get("/api/2fa/status").get_json()["data"]
        self.assertTrue(status["enabled"])
        self.assertTrue(status["scope"]["webhook"])

    def test_api_invalid_secret_rejected(self):
        resp = self.client.put("/api/2fa/secret", json={"secret": "不是base32"})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json().get("code"), "INVALID_SECRET")

    # -- 集成层
    def test_webhook_requires_code(self):
        secret = twofa.generate_secret()
        self.client.put("/api/2fa/secret", json={"secret": secret})
        self.client.put("/api/2fa/config", json={"enabled": True, "scope": {"webhook": True}})

        r1 = self.client.post("/webhook/ip-change", json={"ipv4": "203.0.113.77"})
        self.assertEqual(r1.status_code, 401)
        self.assertEqual(r1.get_json().get("code"), "TOTP_REQUIRED")

        r2 = self.client.post(
            "/webhook/ip-change",
            json={"ipv4": "203.0.113.78"},
            headers={"X-2FA-Code": twofa.totp(secret)},
        )
        self.assertEqual(r2.status_code, 200)

    def test_login_requires_code(self):
        secret = twofa.generate_secret()
        self.client.put("/api/2fa/secret", json={"secret": secret})
        self.client.put("/api/2fa/config", json={"enabled": True, "scope": {"web_login": True}})

        anon = self.app.test_client()
        r1 = anon.post("/api/login", json={"username": "admin", "password": TEST_PASSWORD})
        self.assertEqual(r1.status_code, 401)
        self.assertEqual(r1.get_json().get("code"), "TOTP_REQUIRED")

        r2 = anon.post(
            "/api/login",
            json={"username": "admin", "password": TEST_PASSWORD, "otp": twofa.totp(secret)},
        )
        self.assertEqual(r2.status_code, 200)

    def test_bot_requires_code(self):
        from app.dingtalk.bot import handle_message

        secret = twofa.generate_secret()
        self.client.put("/api/2fa/secret", json={"secret": secret})
        self.client.put("/api/2fa/config", json={"enabled": True, "scope": {"dingtalk": True}})

        base = {
            "sender_id": "u1",
            "sender_nick": "tester",
            "conversation_id": "c1",
            "is_at": True,
        }
        denied = handle_message({**base, "text": "203.0.113.90 22"})
        self.assertIn("二次验证", str(denied))

        allowed = handle_message({**base, "text": f"203.0.113.91 22 code={twofa.totp(secret)}"})
        self.assertNotIn("需要二次验证", str(allowed))


    def test_webhook_trusted_cidr_skips_2fa(self):
        secret = twofa.generate_secret()
        self.client.put("/api/2fa/secret", json={"secret": secret})
        resp = self.client.put(
            "/api/2fa/config",
            json={
                "enabled": True,
                "scope": {"webhook": True},
                "webhook_trusted_cidrs": "127.0.0.1/32, 10.0.0.0/8",
            },
        )
        self.assertTrue(resp.get_json()["ok"])
        # 命中白名单网段 → 免口令直接放行
        ok = self.client.post("/webhook/ip-change", json={"ipv4": "203.0.113.83"})
        self.assertEqual(ok.status_code, 200)
        # 白名单逻辑本身：内网命中、公网不命中
        self.assertTrue(twofa.webhook_client_trusted("10.1.2.3"))
        self.assertFalse(twofa.webhook_client_trusted("8.8.8.8"))

    def test_webhook_skip_private_skips_2fa(self):
        secret = twofa.generate_secret()
        self.client.put("/api/2fa/secret", json={"secret": secret})
        self.client.put(
            "/api/2fa/config",
            json={"enabled": True, "scope": {"webhook": True}, "webhook_skip_private": True},
        )
        # 来自回环（测试客户端默认 127.0.0.1）→ 免口令
        ok = self.client.post("/webhook/ip-change", json={"ipv4": "203.0.113.84"})
        self.assertEqual(ok.status_code, 200)
        self.assertTrue(twofa.is_internal_ip("192.168.1.10"))
        self.assertFalse(twofa.is_internal_ip("8.8.8.8"))

    def test_invalid_cidr_rejected(self):
        secret = twofa.generate_secret()
        self.client.put("/api/2fa/secret", json={"secret": secret})
        resp = self.client.put(
            "/api/2fa/config",
            json={"enabled": True, "webhook_trusted_cidrs": "not-a-cidr"},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json().get("code"), "INVALID_CIDR")

    def test_bot_strict_mode_requires_code_for_readonly(self):
        from app.dingtalk.bot import handle_message

        secret = twofa.generate_secret()
        self.client.put("/api/2fa/secret", json={"secret": secret})
        self.client.put(
            "/api/2fa/config",
            json={"enabled": True, "scope": {"dingtalk": True}, "dingtalk_all": True},
        )
        base = {
            "sender_id": "u1",
            "sender_nick": "tester",
            "conversation_id": "c1",
            "is_at": True,
        }
        # 严格模式下连「帮助」也要口令
        denied = handle_message({**base, "text": "帮助"})
        self.assertIn("二次验证", str(denied))
        # 带口令后正常返回帮助
        allowed = handle_message({**base, "text": f"帮助 code={twofa.totp(secret)}"})
        self.assertNotIn("需要二次验证", str(allowed))


def _reset_twofa() -> None:
    """把 twofa 配置复位到「关闭 + 无密钥」，供测试隔离（绕开 update 对空 secret 的保留逻辑）。"""
    data = dict(settings.data)
    data["twofa"] = {
        "enabled": False,
        "secret": "",
        "issuer": "动态云防火墙",
        "account": "admin",
        "window": 1,
        "scope": {"web_login": False, "webhook": False, "dingtalk": False},
    }
    store.set_setting("app_settings", data)
    settings.load()


# --------------------------------------------------------------------------- 入口
def main() -> int:
    argv = [sys.argv[0]]
    keyword = ""
    if "-k" in sys.argv:
        idx = sys.argv.index("-k")
        keyword = sys.argv[idx + 1]
        argv += ["-v"]
    if keyword:
        loader = unittest.TestLoader()
        suite = unittest.TestSuite()
        for name in dir(sys.modules[__name__]):
            obj = getattr(sys.modules[__name__], name)
            if isinstance(obj, type) and issubclass(obj, unittest.TestCase) and obj is not BaseCase:
                tests = loader.loadTestsFromTestCase(obj)
                for t in tests:
                    if keyword.lower() in t.id().lower():
                        suite.addTest(t)
    else:
        suite = unittest.TestLoader().loadTestsFromModule(sys.modules[__name__])

    result = unittest.TextTestRunner(verbosity=2).run(suite)
    print("\n" + "=" * 62)
    print(f"用例 {result.testsRun} 个｜失败 {len(result.failures)}｜错误 {len(result.errors)}")
    print("=" * 62)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
