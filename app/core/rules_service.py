#!/usr/bin/env python3
"""
放行规则业务层。

所有对云端的写操作都收敛在这里，Web / 钉钉 / Webhook / 定时任务
共用同一套逻辑，保证行为一致、日志齐全。
"""

from __future__ import annotations

import ipaddress
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from app.cloud.factory import build_client
from app.cloud.swas import (
    ALL_PORTS,
    CloudError,
    SwasFirewall,
    normalize_cidr,
    normalize_port_value,
    port_to_display,
    protocol_normalize,
    split_ports,
)
from app.core import notifier
from app.settings import settings
from app.store import now_str, store
from app.utils.logger import logger

# 机器人 / 自动放行写入的规则统一带这个前缀，便于自动化清理时精准识别
AUTO_TAG = "AUTO-ALLOW"

# 规则终态：这些状态的记录只作为历史保留，不再代表云端有该规则
GONE_STATUSES = {"deleted", "expired", "stale"}
# 「在用」= 生效中 + 已禁用（默认视图）
LIVE_STATUSES = ["active", "disabled"]

# 云端「规则已不存在」的特征串，命中即视为删除成功
_NOT_FOUND_HINTS = (
    "not found",
    "notfound",
    "notexist",
    "invalidruleid",
    "invalidrule",
    "不存在",
)


def _is_not_found(message: str) -> bool:
    text = (message or "").lower().replace(" ", "")
    return any(hint.replace(" ", "") in text for hint in _NOT_FOUND_HINTS)


def _ts_after_minutes(minutes: int) -> str:
    if not minutes or minutes <= 0:
        return ""
    return (datetime.now() + timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")


def _validate_ip(text: str) -> str:
    try:
        ipaddress.ip_address(text.strip())
    except ValueError as exc:
        raise CloudError(f"非法 IP 地址: {text}") from exc
    return text.strip()


def _parse_ip_input(raw: str) -> List[str]:
    parts = [p for p in str(raw or "").replace("，", ",").replace("\n", ",").split(",")]
    ips = [_validate_ip(p) for p in (x.strip() for x in parts) if p]
    if not ips:
        raise CloudError("未提供有效 IP")
    return ips


# --------------------------------------------------------------------------- 查询
def list_cloud_rules(instance_id: str, refresh_local: bool = True) -> List[Dict[str, Any]]:
    client = build_client()
    rules = client.list_rules(instance_id)
    name = settings.instance_name(instance_id)
    for r in rules:
        r["instance_name"] = name
        r["port_display"] = port_to_display(r["port"])
    if refresh_local:
        try:
            store.replace_instance_rules(instance_id, rules)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"刷新本地规则镜像失败: {exc}")
    return rules


def list_all_cloud_rules() -> Dict[str, Any]:
    """拉取全部已配置实例的防火墙规则。"""
    out: Dict[str, Any] = {"instances": [], "errors": []}
    for inst in settings.enabled_instances():
        instance_id = inst["instance_id"]
        try:
            rules = list_cloud_rules(instance_id)
            out["instances"].append(
                {
                    "instance_id": instance_id,
                    "instance_name": inst.get("name") or instance_id,
                    "rules": rules,
                    "count": len(rules),
                }
            )
        except CloudError as exc:
            out["errors"].append({"instance_id": instance_id, "message": str(exc)})
    return out


# --------------------------------------------------------------------------- 放行
def allow_ip(
    ip_input: str,
    ports: str = "ALL",
    protocol: str = "tcp",
    description: str = "",
    source: str = "web",
    actor: str = "web",
    ttl_minutes: Optional[int] = None,
    instance_ids: Optional[List[str]] = None,
    cleanup_old: Optional[bool] = None,
    tag: str = "",
) -> Dict[str, Any]:
    """
    放行一个或多个 IP。

    Args:
        ip_input: 单个 IP，或逗号分隔的多个 IP
        ports: "ALL" / "22" / "80,443" / "8000-9000"
        ttl_minutes: 有效期，0 或 None 时取全局默认
        instance_ids: 指定实例，默认全部启用实例
        tag: 规则标签（用于分组筛选）
    """
    ips = _parse_ip_input(ip_input)
    port_list = split_ports(ports)
    proto = protocol_normalize(protocol)
    # 「全部端口」且未指定协议时，用 TCP+UDP，否则只放 TCP 会漏掉 UDP 业务
    if port_list == [ALL_PORTS] and proto == "TCP":
        proto = "TCP+UDP"
    ttl = settings.get("rules_policy.default_ttl_minutes", 1440) if ttl_minutes is None else ttl_minutes
    policy = settings.section("rules_policy")
    do_cleanup = policy.get("auto_cleanup_old_ip", True) if cleanup_old is None else cleanup_old

    # 本次批量放行的全部 cidr：清理旧规则时必须整体排除，
    # 否则「1.1.1.1 2.2.2.2 22」里后一个 IP 会把前一个 IP 刚建的规则删掉。
    keep_cidrs = {normalize_cidr(ip)[0] for ip in ips}

    targets = settings.enabled_instances()
    if instance_ids:
        targets = [t for t in targets if t["instance_id"] in instance_ids]
    if not targets:
        return {
            "ok": False,
            "message": "没有可用的阿里云实例，请先在「云账户」中配置实例",
            "results": [],
            "summary": {"created": 0, "skipped": 0, "failed": 0},
        }

    client = build_client()
    results: List[Dict[str, Any]] = []

    for inst in targets:
        instance_id = inst["instance_id"]
        instance_name = inst.get("name") or instance_id
        try:
            cloud_rules = client.list_rules(instance_id)
        except CloudError as exc:
            results.append(
                {
                    "instance_id": instance_id,
                    "instance_name": instance_name,
                    "status": "failed",
                    "message": f"读取云端规则失败: {exc}",
                    "action": "list",
                }
            )
            continue

        for ip in ips:
            cidr, plain_ip = normalize_cidr(ip)
            for port in port_list:
                existing = next(
                    (
                        r
                        for r in cloud_rules
                        if r["cidr"] == cidr and r["port"] == port and r["protocol"] == proto.lower()
                    ),
                    None,
                )
                if existing:
                    results.append(
                        {
                            "instance_id": instance_id,
                            "instance_name": instance_name,
                            "ip": plain_ip,
                            "port": port,
                            "port_display": port_to_display(port),
                            "protocol": proto.lower(),
                            "status": "skipped",
                            "action": "exists",
                            "rule_id": existing["rule_id"],
                            "message": "规则已存在，无需重复添加",
                        }
                    )
                    _upsert_local(
                        instance_id, instance_name, existing, source, actor, ttl, plain_ip, tag
                    )
                    continue

                # 清理同端口的旧自动放行规则（动态 IP 场景）
                # 只清理「同一操作人/同一来源」名下、端口相同的旧 IP 规则，
                # 避免群里 A 用户放行 22 端口时把 B 用户的 22 端口规则删掉。
                if do_cleanup:
                    removed = _cleanup_old_auto_rules(
                        client, instance_id, cloud_rules, port, proto, keep_cidrs, actor
                    )
                    if removed:
                        logger.info(f"{instance_name}: 清理旧自动规则 {removed} 条")

                remark = f"{AUTO_TAG} {plain_ip} {port_to_display(port)}"
                if description:
                    remark = f"{AUTO_TAG} {description}"[:64]
                try:
                    created = client.create_rule(instance_id, cidr, proto, port, remark)
                    new_rule = {
                        "rule_id": created.get("rule_id", ""),
                        "cidr": cidr,
                        "port": port,
                        "protocol": proto.lower(),
                        "description": remark,
                    }
                    cloud_rules.append(new_rule)
                    _upsert_local(
                        instance_id, instance_name, new_rule, source, actor, ttl, plain_ip, tag
                    )
                    results.append(
                        {
                            "instance_id": instance_id,
                            "instance_name": instance_name,
                            "ip": plain_ip,
                            "port": port,
                            "port_display": port_to_display(port),
                            "protocol": proto.lower(),
                            "status": "success",
                            "action": "created",
                            "rule_id": created.get("rule_id", ""),
                            "message": "放行成功",
                        }
                    )
                except CloudError as exc:
                    results.append(
                        {
                            "instance_id": instance_id,
                            "instance_name": instance_name,
                            "ip": plain_ip,
                            "port": port,
                            "port_display": port_to_display(port),
                            "protocol": proto.lower(),
                            "status": "failed",
                            "action": "create",
                            "message": str(exc),
                        }
                    )

    summary = {
        "created": len([r for r in results if r.get("action") == "created"]),
        "skipped": len([r for r in results if r.get("action") == "exists"]),
        "failed": len([r for r in results if r.get("status") == "failed"]),
    }
    ok = summary["failed"] == 0 and (summary["created"] + summary["skipped"]) > 0
    message = f"共 {summary['created']} 条新增，{summary['skipped']} 条已存在，{summary['failed']} 条失败"

    store.log(
        action="放行规则",
        target=",".join(ips),
        status="success" if ok else "failed",
        level="INFO" if ok else "ERROR",
        message=f"{message}（端口 {ports} / {proto.upper()} / 来源 {source}）",
        detail=results,
    )
    created_rows = [r for r in results if r.get("action") == "created"]
    if created_rows:
        notifier.notify_change(
            "allow",
            [
                {
                    **r,
                    "actor": actor,
                    "source": source,
                    "tag": tag,
                    "expires_at": _ts_after_minutes(ttl),
                }
                for r in created_rows
            ],
        )
    return {
        "ok": ok,
        "message": message,
        "ips": ips,
        "ports": port_list,
        "protocol": proto,
        "ttl_minutes": ttl,
        "results": results,
        "summary": summary,
    }


def _upsert_local(
    instance_id: str,
    instance_name: str,
    rule: Dict[str, Any],
    source: str,
    actor: str,
    ttl: int,
    plain_ip: str,
    tag: str = "",
) -> None:
    existing = [
        row
        for row in store.find_rules(
            instance_id=instance_id,
            ip=plain_ip,
            port=rule.get("port", "ALL"),
            # 已禁用但云端仍在的行也要命中，否则重复放行会插入重复记录
            status_in=["active", "disabled"],
        )
        # 同一 IP+端口可能同时存在 TCP 与 UDP 规则，必须按协议区分，否则会互相覆盖
        if (row.get("protocol") or "").lower() == (rule.get("protocol") or "").lower()
    ]
    expires_at = _ts_after_minutes(ttl)
    if existing:
        hit = existing[0]
        store.update_rule(
            hit["id"],
            rule_id=rule.get("rule_id", "") or hit["rule_id"],
            expires_at=expires_at or hit["expires_at"],
            status="active",
            # 到期时间被刷新，提醒标记也要重置，否则下次到期不再提醒
            notified_at="",
            tag=tag or hit.get("tag", ""),
        )
        return
    store.add_rule(
        {
            "instance_id": instance_id,
            "instance_name": instance_name,
            "rule_id": rule.get("rule_id", ""),
            "ip": plain_ip,
            "cidr": rule.get("cidr", ""),
            "protocol": rule.get("protocol", "tcp"),
            "port": rule.get("port", "ALL"),
            "description": rule.get("description", ""),
            "status": "active",
            "source": source,
            "actor": actor,
            "expires_at": expires_at,
            "tag": tag or "",
        }
    )


def _cleanup_old_auto_rules(
    client: SwasFirewall,
    instance_id: str,
    cloud_rules: List[Dict[str, Any]],
    port: str,
    protocol: str,
    keep_cidrs,
    actor: str = "",
) -> int:
    """
    删除同一操作人（或同一 DDNS 域名）名下、同端口的旧 IP 自动规则。

    仅处理带 AUTO_TAG 且本地记录 actor 完全一致的规则，避免误删他人放行。
    ``keep_cidrs`` 是本次批量放行的全部 IP，必须整体排除——
    否则一次放行多个 IP 时，后面的 IP 会把前面刚建的规则当成「旧规则」删掉。
    """
    keep = {keep_cidrs} if isinstance(keep_cidrs, str) else set(keep_cidrs or ())
    active_rows = store.find_rules(instance_id=instance_id, status_in=["active"])
    own_rule_ids = {
        row["rule_id"]
        for row in active_rows
        if row.get("rule_id") and (row.get("actor") or "") == (actor or "")
    }
    victims = [
        r
        for r in cloud_rules
        if r["port"] == port
        and r["protocol"] == protocol.lower()
        and r["cidr"] not in keep
        and AUTO_TAG in (r.get("description") or "")
        and r.get("rule_id") in own_rule_ids
    ]
    removed = 0
    for rule in victims:
        try:
            client.delete_rules(instance_id, [rule["rule_id"]])
            cloud_rules.remove(rule)
            removed += 1
            for row in active_rows:
                if row["rule_id"] == rule["rule_id"]:
                    store.update_rule(row["id"], status="deleted", description=(row["description"] or "") + " [被新IP替换]")
        except CloudError as exc:
            logger.warning(f"清理旧规则失败 {rule['rule_id']}: {exc}")
    return removed


# --------------------------------------------------------------------------- 单条操作
def create_rule_manual(
    instance_id: str,
    ip: str,
    ports: str,
    protocol: str = "tcp",
    description: str = "",
    ttl_minutes: Optional[int] = None,
    actor: str = "web",
    instance_ids: Optional[List[str]] = None,
    tag: str = "",
) -> Dict[str, Any]:
    """手动新增。instance_id 为空时按 instance_ids 批量下发到多台实例。"""
    targets = instance_ids or ([instance_id] if instance_id else [])
    result = allow_ip(
        ip,
        ports=ports,
        protocol=protocol,
        description=description or "手动添加",
        source="web",
        actor=actor,
        ttl_minutes=ttl_minutes,
        instance_ids=targets or None,
        cleanup_old=False,
        tag=tag,
    )
    return result


def delete_rule(rule_pk: int, actor: str = "web") -> Dict[str, Any]:
    """
    删除规则（云端 + 本地软删除）。

    * 已经是终态（已删除/已过期/陈旧）的规则直接返回成功，重复点击不再报错；
    * 云端返回 NotFound 说明规则早已不在，按「已删除」处理，不同步报错。
    """
    row = store.get_rule(rule_pk)
    if not row:
        return {"ok": True, "message": "规则不存在（可能已被清理）", "already": True}
    if row["status"] in GONE_STATUSES:
        return {"ok": True, "message": "该规则已是删除状态，无需重复删除", "already": True}

    client = build_client()
    cloud_gone = False
    try:
        if row["rule_id"]:
            client.delete_rules(row["instance_id"], [row["rule_id"]])
    except CloudError as exc:
        message = str(exc)
        if _is_not_found(message):
            cloud_gone = True
            logger.info(f"规则 {row['ip']}:{row['port']} 云端已不存在，按删除成功处理")
        else:
            store.log("删除规则", target=row["ip"], status="failed", level="ERROR", message=message)
            return {"ok": False, "message": f"云端删除失败: {message}"}

    store.update_rule(rule_pk, status="deleted", rule_id="")
    store.log(
        "删除规则",
        target=f"{row['instance_name']} {row['ip']}:{row['port']}",
        message=f"{row['protocol'].upper()} {row['port']} 已删除"
        + ("（云端已不存在，仅清理本地记录）" if cloud_gone else ""),
        detail={"actor": actor},
    )
    notifier.notify_change(
        "delete",
        [
            {
                **row,
                "port_display": port_to_display(row["port"]),
                "actor": actor,
                "detail": "云端已不存在，仅清理本地记录" if cloud_gone else "",
            }
        ],
    )
    return {"ok": True, "message": "已删除"}


def restore_rule(rule_pk: int, actor: str = "web") -> Dict[str, Any]:
    """
    把已删除 / 已过期 / 已清理的记录重新下发到云端。

    * 云端已存在同 IP+端口+协议的规则时，直接复用并重新关联 rule_id；
    * 原到期时间已过的，恢复为长期有效，避免刚恢复就被过期清理任务回收。
    """
    row = store.get_rule(rule_pk)
    if not row:
        return {"ok": False, "message": "规则记录不存在"}
    if row["status"] == "active":
        return {"ok": True, "message": "规则已在生效中，无需恢复", "already": True}

    cidr, plain_ip = normalize_cidr(row["cidr"] or row["ip"])
    port = normalize_port_value(row["port"])
    proto = protocol_normalize(row["protocol"])
    remark = (row["description"] or f"{AUTO_TAG} restored")[:64]
    client = build_client()

    try:
        cloud_rules = client.list_rules(row["instance_id"])
    except CloudError as exc:
        return {"ok": False, "message": f"读取云端规则失败: {exc}"}

    match = next(
        (
            r
            for r in cloud_rules
            if r["cidr"] == cidr and r["port"] == port and r["protocol"] == proto.lower()
        ),
        None,
    )
    if match:
        rule_id = match["rule_id"]
        recreated = False
    else:
        try:
            created = client.create_rule(row["instance_id"], cidr, proto, port, remark)
        except CloudError as exc:
            store.log(
                "恢复规则", target=f"{plain_ip}:{port}", status="failed", level="ERROR", message=str(exc)
            )
            return {"ok": False, "message": f"云端恢复失败: {exc}"}
        rule_id = created.get("rule_id", "")
        recreated = True

    expires_at = row["expires_at"] or ""
    if expires_at and expires_at <= now_str():
        expires_at = ""
    store.update_rule(
        rule_pk,
        status="active",
        rule_id=rule_id,
        cidr=cidr,
        port=port,
        protocol=proto.lower(),
        expires_at=expires_at,
    )
    store.log(
        "恢复规则",
        target=f"{plain_ip}:{port}",
        message=("已重新下发到云端" if recreated else "云端已存在该规则，已重新关联"),
        detail={"actor": actor, "rule_id": rule_id},
    )
    notifier.notify_change(
        "restore",
        [{**row, "ip": plain_ip, "port": port, "port_display": port_to_display(port), "actor": actor}],
    )
    return {
        "ok": True,
        "message": "已恢复到云端" if recreated else "云端已存在该规则，已重新关联",
    }


def purge_rule(rule_pk: int, actor: str = "web") -> Dict[str, Any]:
    """从本地数据库彻底移除一条历史记录（不触碰云端）。"""
    row = store.get_rule(rule_pk)
    if not row:
        return {"ok": True, "message": "记录已不存在", "already": True}
    if row["status"] not in GONE_STATUSES:
        return {
            "ok": False,
            "message": "只能清理已失效的记录；在用规则请先删除（删除后仍可恢复）",
        }
    store.delete_rule_row(rule_pk)
    store.log(
        "清理规则记录",
        target=f"{row['ip']}:{row['port']}",
        message="已从本地数据库移除（云端无此规则）",
        detail={"actor": actor},
    )
    return {"ok": True, "message": "已从本地记录中删除"}


def toggle_rule(rule_pk: int, enable: bool, actor: str = "web") -> Dict[str, Any]:
    """
    启用 / 禁用规则。

    优先调用 SDK 原生 EnableFirewallRule / DisableFirewallRule；
    若云端接口不可用（旧版本实例或权限不足），退化为「删除 + 本地快照」。
    """
    row = store.get_rule(rule_pk)
    if not row:
        return {"ok": False, "message": "规则不存在"}
    client = build_client()
    target = f"{row['instance_name'] or row['instance_id']} {row['ip']}:{row['port']}"

    if not enable:
        if row["status"] != "active":
            return {"ok": False, "message": "只有生效中的规则才能禁用"}
        try:
            client.disable_rule(row["instance_id"], row["rule_id"], row["description"] or "")
            store.update_rule(rule_pk, status="disabled")
            store.log("禁用规则", target=target, message="已通过云端接口禁用", detail={"actor": actor})
            return {"ok": True, "message": "已禁用"}
        except CloudError as exc:
            snapshot = {
                "ip": row["ip"],
                "cidr": row["cidr"] or row["ip"],
                "port": row["port"],
                "protocol": row["protocol"],
                "description": row["description"],
                "rule_id": row["rule_id"],
            }
            try:
                if row["rule_id"]:
                    client.delete_rules(row["instance_id"], [row["rule_id"]])
            except CloudError as exc2:
                store.log("禁用规则", target=target, status="failed", level="ERROR", message=f"{exc} / {exc2}")
                return {"ok": False, "message": f"禁用失败: {exc2}"}
            store.update_rule(
                rule_pk,
                status="disabled",
                snapshot=__import__("json").dumps(snapshot, ensure_ascii=False),
                rule_id="",
            )
            store.log(
                "禁用规则",
                target=target,
                message="云端不支持禁用，已移除规则并保留快照，可随时启用",
                detail={"actor": actor, "fallback": str(exc)},
            )
            return {"ok": True, "message": "已禁用（规则已从云端移除，可一键恢复）"}

    if row["status"] == "active":
        return {"ok": False, "message": "规则已处于启用状态"}

    if row["rule_id"]:
        try:
            client.enable_rule(
                row["instance_id"], row["rule_id"], row["cidr"] or row["ip"], row["description"] or ""
            )
            store.update_rule(rule_pk, status="active")
            store.log("启用规则", target=target, message="已通过云端接口启用", detail={"actor": actor})
            return {"ok": True, "message": "已启用"}
        except CloudError:
            pass  # 回退到重建

    try:
        # 历史行可能是 port="ALL" / protocol="ALL"，直接下发会再次触发
        # InvalidPort.ValueNotSupported，这里必须重新归一化。
        cidr, plain_ip = normalize_cidr(row["cidr"] or row["ip"])
        port_value = normalize_port_value(row["port"])
        proto_value = protocol_normalize(row["protocol"])
        created = client.create_rule(
            row["instance_id"],
            cidr,
            proto_value,
            port_value,
            row["description"] or f"{AUTO_TAG} restored",
        )
    except CloudError as exc:
        store.log("启用规则", target=target, status="failed", level="ERROR", message=str(exc))
        return {"ok": False, "message": f"云端创建失败: {exc}"}
    store.update_rule(
        rule_pk,
        status="active",
        rule_id=created.get("rule_id", ""),
        ip=plain_ip,
        cidr=cidr,
        port=port_value,
        protocol=proto_value.lower(),
    )
    store.log("启用规则", target=target, message="已按快照重新下发到云端", detail={"actor": actor})
    return {"ok": True, "message": "已启用"}


def renew_rule(rule_pk: int, minutes: int = 1440, actor: str = "web") -> Dict[str, Any]:
    """
    续期：把到期时间从**现在**起往后推。

    只动本地有效期，不碰云端（云端本来就没有 TTL 概念）。
    ``expired`` 状态的记录不允许续期——它已经被删除任务回收过，应走「恢复到云端」。
    """
    row = store.get_rule(rule_pk)
    if not row:
        return {"ok": False, "message": "规则不存在"}
    if row["status"] in GONE_STATUSES:
        return {"ok": False, "message": "该规则已失效，请先恢复到云端"}
    if row["status"] != "active":
        return {"ok": False, "message": "只有生效中的规则可以续期"}
    if minutes <= 0:
        return {"ok": False, "message": "续期时长必须大于 0 分钟"}

    expires_at = _ts_after_minutes(minutes)
    store.update_rule(rule_pk, expires_at=expires_at, notified_at="")
    store.log(
        "续期规则",
        target=f"{row['instance_name']} {row['ip']}:{row['port']}",
        message=f"延长 {minutes} 分钟，新到期时间 {expires_at}",
        detail={"actor": actor},
    )
    notifier.notify_change(
        "renew",
        [
            {
                **row,
                "port_display": port_to_display(row["port"]),
                "actor": actor,
                "expires_at": expires_at,
            }
        ],
    )
    return {"ok": True, "message": f"已续期到 {expires_at}", "expires_at": expires_at}


def notify_expiring(rows: List[Dict[str, Any]], minutes: int) -> int:
    """把「即将到期」的规则汇总推一条提醒，并打上 notified_at 防止重复。"""
    rows = [r for r in (rows or []) if r]
    if not rows:
        return 0
    from datetime import datetime

    extra_lines = []
    for row in rows[:15]:
        try:
            left = datetime.strptime(row["expires_at"], "%Y-%m-%d %H:%M:%S") - datetime.now()
            left_min = max(0, int(left.total_seconds() // 60))
        except Exception:  # noqa: BLE001
            left_min = 0
        extra_lines.append(
            f"- `{row['ip']}` {port_to_display(row['port'])} 还剩 {left_min} 分钟"
            f"（{row.get('tag') or row.get('actor') or '-'}）"
        )
    extra = "\n".join(["**即将到期**", *extra_lines, "", "在 Web 端「放行规则」里点「续期」，或发「续期 {IP}」。"])
    notifier.notify_change(
        "expire",
        [{**r, "port_display": port_to_display(r["port"]), "detail": f"{minutes} 分钟内到期"} for r in rows],
        extra=extra,
    )
    for row in rows:
        store.update_rule(row["id"], notified_at=now_str())
    return len(rows)


def cloud_diff(instance_id: str) -> Dict[str, Any]:
    """
    本地镜像 vs 云端的差异。

    三类：
      * only_local  —— 本地认为生效，云端没有（漂移，应「恢复」）
      * only_cloud  —— 云端有、本地没记录（控制台手工创建，应「纳入纳管」）
      * changed     —— 同一 rule_id 但 IP/端口/协议/备注不一致
    """
    client = build_client()
    cloud_rules = client.list_rules(instance_id)
    local_rows = store.find_rules(instance_id=instance_id, status_in=["active"])

    def key_of(item):
        return (item.get("cidr") or item.get("ip") or "", str(item.get("port") or ""), (item.get("protocol") or "").lower())

    local_by_rule_id = {r["rule_id"]: r for r in local_rows if r.get("rule_id")}
    local_keys = {key_of(r) for r in local_rows}
    cloud_keys = set()

    only_local, only_cloud, changed = [], [], []
    for cr in cloud_rules:
        cloud_keys.add(key_of(cr))
        hit = local_by_rule_id.get(cr.get("rule_id", ""))
        if not hit:
            if key_of(cr) in local_keys:
                continue  # rule_id 变了但内容一致（删除重建过），不算差异
            only_cloud.append({**cr, "port_display": port_to_display(cr.get("port", ""))})
            continue
        if (
            (hit["ip"] or "") != (cr.get("ip") or "")
            or str(hit["port"] or "") != str(cr.get("port") or "")
            or (hit["protocol"] or "").lower() != (cr.get("protocol") or "").lower()
            or (hit["description"] or "") != (cr.get("description") or "")
        ):
            changed.append(
                {
                    "rule_id": cr.get("rule_id") or hit["rule_id"],
                    "local": {
                        "ip": hit["ip"],
                        "port": hit["port"],
                        "protocol": hit["protocol"],
                        "description": hit["description"],
                    },
                    "cloud": {
                        "ip": cr.get("ip", ""),
                        "port": cr.get("port", ""),
                        "protocol": cr.get("protocol", ""),
                        "description": cr.get("description", ""),
                    },
                }
            )

    for row in local_rows:
        if key_of(row) not in cloud_keys:
            only_local.append({**row, "port_display": port_to_display(row["port"])})

    return {
        "instance_id": instance_id,
        "instance_name": settings.instance_name(instance_id),
        "cloud_total": len(cloud_rules),
        "local_total": len(local_rows),
        "only_local": only_local,
        "only_cloud": only_cloud,
        "changed": changed,
        "healthy": not (only_local or only_cloud or changed),
    }


def diff_all_instances() -> Dict[str, Any]:
    out: Dict[str, Any] = {"instances": [], "errors": []}
    for inst in settings.enabled_instances():
        try:
            out["instances"].append(cloud_diff(inst["instance_id"]))
        except CloudError as exc:
            out["errors"].append({"instance_id": inst["instance_id"], "message": str(exc)})
    return out


def update_rule_manual(
    rule_pk: int,
    ip: Optional[str] = None,
    ports: Optional[str] = None,
    protocol: Optional[str] = None,
    description: Optional[str] = None,
    ttl_minutes: Optional[int] = None,
    actor: str = "web",
) -> Dict[str, Any]:
    row = store.get_rule(rule_pk)
    if not row:
        return {"ok": False, "message": "规则不存在"}
    if row["status"] != "active":
        return {"ok": False, "message": "请先启用规则再编辑"}

    new_ip = ip or row["ip"]
    new_ports = split_ports(ports if ports is not None else row["port"])
    new_proto = protocol_normalize(protocol or row["protocol"])
    new_desc = description if description is not None else row["description"]
    if len(new_ports) > 1:
        # 单条规则改多端口 = 拆成多条（云端一条规则只能有一个端口段）
        return _split_rule_into_ports(row, new_ip, new_ports, new_proto, new_desc, ttl_minutes, actor)
    cidr, plain_ip = normalize_cidr(new_ip)
    port = new_ports[0]
    client = build_client()

    try:
        client.modify_rule(
            row["instance_id"],
            row["rule_id"],
            cidr,
            new_proto,
            port,
            new_desc or f"{AUTO_TAG} edited",
        )
    except CloudError as exc:
        store.log("编辑规则", target=row["ip"], status="failed", level="ERROR", message=str(exc))
        return {"ok": False, "message": f"云端修改失败: {exc}"}

    # 删除重建路径下 rule_id 会变化，重新定位
    new_rule_id = ""
    try:
        for cr in client.list_rules(row["instance_id"]):
            if cr["cidr"] == cidr and cr["port"] == port and cr["protocol"] == new_proto.lower():
                new_rule_id = cr["rule_id"]
                break
    except CloudError:
        pass

    expires_at = row["expires_at"]
    if ttl_minutes is not None:
        expires_at = _ts_after_minutes(ttl_minutes)
    store.update_rule(
        rule_pk,
        ip=plain_ip,
        cidr=cidr,
        port=port,
        protocol=new_proto.lower(),
        description=new_desc or "",
        rule_id=new_rule_id or row["rule_id"],
        expires_at=expires_at or "",
    )
    store.log("编辑规则", target=f"{plain_ip}:{port}", message=f"已更新为 {new_proto.upper()} {port}")
    return {"ok": True, "message": "已更新"}


def _split_rule_into_ports(
    row: Dict[str, Any],
    ip: str,
    ports: List[str],
    proto: str,
    description: str,
    ttl_minutes: Optional[int],
    actor: str,
) -> Dict[str, Any]:
    """
    把一条规则按多端口展开成多条（云端一条规则只能承载一个端口段）。

    旧行标记为 deleted（保留可追溯历史），新行按端口各建一条。
    """
    cidr, plain_ip = normalize_cidr(ip)
    client = build_client()

    if row.get("rule_id"):
        try:
            client.delete_rules(row["instance_id"], [row["rule_id"]])
        except CloudError as exc:
            if not _is_not_found(str(exc)):
                return {"ok": False, "message": f"云端删除旧规则失败: {exc}"}

    expires_at = _ts_after_minutes(ttl_minutes) if ttl_minutes is not None else (row["expires_at"] or "")
    created_ports, failures = [], []
    for port in ports:
        remark = (description or row["description"] or f"{AUTO_TAG} split")[:64]
        try:
            res = client.create_rule(row["instance_id"], cidr, proto, port, remark)
        except CloudError as exc:
            failures.append(f"{port_to_display(port)}：{exc}")
            continue
        store.add_rule(
            {
                "instance_id": row["instance_id"],
                "instance_name": row["instance_name"],
                "rule_id": res.get("rule_id", ""),
                "ip": plain_ip,
                "cidr": cidr,
                "protocol": proto.lower(),
                "port": port,
                "description": remark,
                "status": "active",
                "source": row.get("source") or "web",
                "actor": actor,
                "expires_at": expires_at,
                "tag": row.get("tag", ""),
            }
        )
        created_ports.append(port_to_display(port))

    store.update_rule(
        row["id"],
        status="deleted",
        rule_id="",
        description=(row["description"] or "") + " [已拆分为多端口]",
    )
    store.log(
        "编辑规则",
        target=f"{plain_ip}",
        status="success" if not failures else "failed",
        message=f"拆分为 {len(created_ports)} 条：{'、'.join(created_ports)}"
        + (f"；失败 {len(failures)}" if failures else ""),
        detail={"actor": actor, "failures": failures},
    )
    if not created_ports:
        return {"ok": False, "message": "没有任何端口创建成功：" + "；".join(failures)}
    return {
        "ok": True,
        "message": f"已拆分为 {len(created_ports)} 条规则：{'、'.join(created_ports)}"
        + (f"（{len(failures)} 条失败）" if failures else ""),
        "created": created_ports,
        "failures": failures,
    }


# --------------------------------------------------------------------------- 自动任务
def cleanup_expired_rules() -> Dict[str, Any]:
    """删除已过期的自动放行规则。"""
    expired = store.expired_active_rules()
    removed, failed = 0, 0
    client = build_client()
    for row in expired:
        try:
            if row["rule_id"]:
                client.delete_rules(row["instance_id"], [row["rule_id"]])
            store.update_rule(row["id"], status="expired")
            removed += 1
        except CloudError as exc:
            # 云端早就没有这条规则时按成功处理，否则它永远清不掉、每次任务都报失败
            if _is_not_found(str(exc)):
                store.update_rule(row["id"], status="expired")
                removed += 1
                logger.info(f"过期规则 {row['ip']}:{row['port']} 云端已不存在，仅更新本地状态")
                continue
            failed += 1
            logger.warning(f"过期规则清理失败 {row['ip']}:{row['port']} - {exc}")
    if removed or failed:
        store.log(
            "定时任务-过期清理",
            message=f"清理 {removed} 条，失败 {failed} 条",
            status="success" if failed == 0 else "failed",
            detail=[{"ip": r["ip"], "port": r["port"], "instance": r["instance_id"]} for r in expired],
        )
    if removed:
        notifier.notify_change(
            "expire",
            [
                {**r, "port_display": port_to_display(r["port"]), "actor": r.get("actor") or "定时任务"}
                for r in expired
            ],
        )
    return {"removed": removed, "failed": failed, "total": len(expired)}


def run_expiring_notice() -> Dict[str, Any]:
    """到期前提醒：扫出 N 分钟内到期、且还没提醒过的规则，汇总推一条钉钉。"""
    minutes = int(settings.get("rules_policy.expire_notice_minutes") or 0)
    if minutes <= 0:
        return {"skipped": True, "reason": "未开启到期提醒（expire_notice_minutes=0）"}
    rows = store.expiring_soon_rules(minutes=minutes)
    if not rows:
        return {"sent": 0, "minutes": minutes}
    sent = notify_expiring(rows, minutes)
    store.log(
        "定时任务-到期提醒",
        message=f"提醒 {sent} 条 {minutes} 分钟内到期的规则",
        detail=[{"ip": r["ip"], "port": r["port"], "expires_at": r["expires_at"]} for r in rows],
    )
    return {"sent": sent, "minutes": minutes}


def cleanup_stale_ip_rules() -> Dict[str, Any]:
    """按天清理长期未刷新的自动放行规则（安全兜底）。"""
    policy = settings.section("rules_policy")
    days = int(policy.get("cleanup_days") or 7)
    if not policy.get("auto_cleanup_old_ip", True):
        return {"skipped": True, "reason": "策略已关闭 auto_cleanup_old_ip"}

    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    candidates = [
        r
        for r in store.find_rules(status_in=["active"])
        if (r["updated_at"] or "") < cutoff and AUTO_TAG in (r["description"] or "")
    ]
    removed, failed = 0, 0
    client = build_client()
    for row in candidates:
        try:
            if row["rule_id"]:
                client.delete_rules(row["instance_id"], [row["rule_id"]])
            store.update_rule(row["id"], status="stale")
            removed += 1
        except CloudError as exc:
            if _is_not_found(str(exc)):
                store.update_rule(row["id"], status="stale")
                removed += 1
                continue
            failed += 1
            logger.warning(f"陈旧规则清理失败 {row['ip']}:{row['port']} - {exc}")
    store.log(
        "定时任务-陈旧规则清理",
        message=f"超过 {days} 天未更新的自动规则：清理 {removed} 条，失败 {failed} 条",
        status="success" if failed == 0 else "failed",
    )
    return {"removed": removed, "failed": failed, "days": days}


def sync_all_instances() -> Dict[str, Any]:
    synced, errors = 0, []
    for inst in settings.enabled_instances():
        try:
            rules = list_cloud_rules(inst["instance_id"])
            synced += len(rules)
        except CloudError as exc:
            errors.append(f"{inst['instance_id']}: {exc}")
    if errors:
        store.log("定时任务-云端同步", message="; ".join(errors), status="failed", level="WARNING")
    return {"synced_rules": synced, "errors": errors}


def health_probe() -> Dict[str, Any]:
    try:
        client = build_client()
        info = client.ping()
        return {"ok": True, "detail": info}
    except CloudError as exc:
        store.log("定时任务-云凭证检查", message=str(exc), status="failed", level="ERROR")
        return {"ok": False, "detail": str(exc)}


def stats() -> Dict[str, Any]:
    all_rules = store.find_rules()
    active = [r for r in all_rules if r["status"] == "active"]
    unique_ips = {r["ip"] for r in active}
    expiring = [
        r
        for r in active
        if r["expires_at"]
        and r["expires_at"] <= (datetime.now() + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    ]
    return {
        "active_rules": len(active),
        "disabled_rules": len([r for r in all_rules if r["status"] == "disabled"]),
        "total_rules": len(all_rules),
        "unique_ips": len(unique_ips),
        "instances": len(settings.enabled_instances()),
        "expiring_soon": len(expiring),
    }
