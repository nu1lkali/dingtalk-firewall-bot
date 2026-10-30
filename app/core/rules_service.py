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
    auto_renew: bool = False,
    renew_mode: Optional[str] = None,
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
    max_rules = int(policy.get("max_rules_per_instance") or 0)
    # 公网 IP 跟随生成的规则默认开启自动续期
    renew_flag = 1 if (auto_renew or source == "public_ip") else 0
    # renew_mode 决定到期续期时的行为：same=原地顺延(原IP)，follow=探测并跟随新IP
    # （public_ip 来源必须跟随当前公网 IP，强制 follow）
    renew_mode_value = "follow" if source == "public_ip" else (renew_mode or "follow")

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
                        instance_id, instance_name, existing, source, actor, ttl, plain_ip, tag, renew_flag, renew_mode_value
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
                # 单实例规则上限（0 = 不限制）：防止脚本刷爆云端配额
                live_count = store.count_rules(instance_id=instance_id, status_in=["active"])
                if max_rules > 0 and live_count >= max_rules:
                    results.append(
                        {
                            "instance_id": instance_id,
                            "instance_name": instance_name,
                            "ip": plain_ip,
                            "port": port,
                            "port_display": port_to_display(port),
                            "protocol": proto.lower(),
                            "status": "failed",
                            "action": "limit",
                            "message": f"已达单实例规则上限 {max_rules} 条，未下发",
                        }
                    )
                    continue
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
                        instance_id, instance_name, new_rule, source, actor, ttl, plain_ip, tag, renew_flag, renew_mode_value
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
    auto_renew: int = 0,
    renew_mode: str = "follow",
) -> None:
    rule_port = store.canonical_port(rule.get("port", "ALL"))
    existing = [
        row
        for row in store.find_rules(
            instance_id=instance_id,
            ip=plain_ip,
            port=rule_port,
            # 已禁用但云端仍在的行也要命中，否则重复放行会插入重复记录
            status_in=["active", "disabled"],
        )
        # 同一 IP+端口可能同时存在 TCP 与 UDP 规则，必须按协议区分，否则会互相覆盖
        if (row.get("protocol") or "").lower() == (rule.get("protocol") or "").lower()
    ]
    expires_at = _ts_after_minutes(ttl)
    # ttl<=0 是显式「永久放行」：必须把旧的到期时间清空，不能回填
    permanent = ttl is not None and ttl <= 0
    if existing:
        hit = existing[0]
        store.update_rule(
            hit["id"],
            rule_id=rule.get("rule_id", "") or hit["rule_id"],
            # 命中行可能来自云端同步且字段有偏差（如 ip 存了 /32），一并纠正，
            # 保证「一行 = 云端一条规则」，后续按业务键匹配才不会错位
            ip=plain_ip or hit["ip"],
            cidr=rule.get("cidr", "") or hit.get("cidr", ""),
            port=rule_port or hit["port"],
            expires_at=expires_at if permanent else (expires_at or hit["expires_at"]),
            status="active",
            # 到期时间被刷新，提醒标记也要重置，否则下次到期不再提醒
            notified_at="",
            tag=tag or hit.get("tag", ""),
            auto_renew=auto_renew or int(hit.get("auto_renew") or 0),
            # 已有规则重新放行时，保留其原有续期方式，不擅自翻转
            renew_mode=hit.get("renew_mode") or "follow",
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
            "port": rule_port,
            "description": rule.get("description", ""),
            "status": "active",
            "source": source,
            "actor": actor,
            "expires_at": expires_at,
            "tag": tag or "",
            "auto_renew": auto_renew,
            "renew_mode": renew_mode,
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
    auto_renew: bool = False,
    renew_mode: Optional[str] = None,
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
        auto_renew=auto_renew,
        renew_mode=renew_mode,
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
    auto_renew: Optional[bool] = None,
    renew_mode: Optional[str] = None,
) -> Dict[str, Any]:
    row = store.get_rule(rule_pk)
    if not row:
        return {"ok": False, "message": "规则不存在"}
    if row["status"] != "active":
        return {"ok": False, "message": "请先启用规则再编辑"}

    # 仅切换本地标记（自动续期 / 续期方式 / 有效期）时，不必碰云端
    if ip is None and ports is None and protocol is None and description is None:
        fields = {}
        if auto_renew is not None:
            fields["auto_renew"] = 1 if auto_renew else 0
        if renew_mode is not None:
            fields["renew_mode"] = renew_mode
        if ttl_minutes is not None:
            fields["expires_at"] = _ts_after_minutes(ttl_minutes)
            fields["notified_at"] = ""
        if fields:
            store.update_rule(rule_pk, **fields)
        store.log(
            "更新标记",
            target=f"{row['ip']}:{row['port']}",
            message="已更新自动续期 / 续期方式 / 有效期等本地标记",
            detail={"actor": actor},
        )
        return {"ok": True, "message": "已更新本地标记"}

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
        **({"auto_renew": 1 if auto_renew else 0} if auto_renew is not None else {}),
        **({"renew_mode": renew_mode} if renew_mode is not None else {}),
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
    """
    清理已过期的自动放行规则。

    先处理勾选了「自动续期」的：探测公网 IP 后续期/跟随；剩下的才真正删除。
    这样续期和清理在同一轮里完成，不会出现「先删后建」的断流窗口。
    """
    renewed = auto_renew_due_rules()
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
    return {"removed": removed, "failed": failed, "total": len(expired), "auto_renew": renewed}


# --------------------------------------------------------------------------- 公网 IP 跟随
def _probe_public_ip(retries: int = 3) -> str:
    """探测本机公网 IPv4；多试几次，避免单次接口抖动导致误判为「IP 变了」。"""
    from app.core import iputil

    for _ in range(max(1, retries)):
        info = iputil.get_public_ip()
        if info.get("ip"):
            return str(info["ip"])
    return ""


def _notify_ip_switch(old_ips: List[str], new_ip: str, reason: str) -> bool:
    """
    公网 IP 变化并已自动跟随成功时，推一条钉钉通知。

    只在 public_ip.notify_on_change 开启时发送；未配置通知群时静默跳过，
    绝不影响主流程（发通知失败不能导致放行失败）。
    """
    try:
        if not settings.get("public_ip.notify_on_change", True):
            return False
        old_text = "、".join(f"`{ip}`" for ip in sorted(set(old_ips))) or "`无`"
        text = (
            "### 🌐 公网 IP 已自动跟随\n\n"
            f"- **旧 IP**：{old_text}\n"
            f"- **新 IP**：`{new_ip}`\n"
            f"- **触发**：{reason}\n"
            f"- **时间**：{now_str()}\n\n"
            "> 云端防火墙已切换到新 IP，原 IP 的放行规则已删除。"
        )
        from app.dingtalk.client import notify

        return bool(notify(text, title="公网 IP 变化通知"))
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"公网 IP 变化通知发送失败: {exc}")
        return False


def _follow_ip(row: Dict[str, Any], new_ip: str, ttl: int, actor: str = "cron") -> Dict[str, Any]:
    """
    把一条已有规则从旧 IP 换成新 IP：云端删旧 + 建新，本地改写记录。

    用于「自动续期」时发现 IP 变了（家庭宽带重播号），以及每日公网 IP 同步。
    """
    cidr, plain_ip = normalize_cidr(new_ip)
    instance_id = row["instance_id"]
    client = build_client()
    old_rule_id = row.get("rule_id") or ""
    if old_rule_id:
        try:
            client.delete_rules(instance_id, [old_rule_id])
        except CloudError as exc:
            # 云端本来就没有这条（换过 IP / 手工删过）不算失败
            if not _is_not_found(str(exc)):
                return {"ok": False, "message": f"删除旧规则失败: {exc}"}
    remark = f"{AUTO_TAG} 自动跟随 {plain_ip}"
    try:
        created = client.create_rule(
            instance_id, cidr, (row.get("protocol") or "tcp"), (row.get("port") or ALL_PORTS), remark
        )
    except CloudError as exc:
        return {"ok": False, "message": f"创建新规则失败: {exc}"}

    store.update_rule(
        row["id"],
        ip=plain_ip,
        cidr=cidr,
        rule_id=created.get("rule_id", ""),
        expires_at=_ts_after_minutes(ttl),
        notified_at="",
        description=remark,
        status="active",
    )
    store.log(
        "自动跟随 IP",
        target=f"{row.get('instance_name') or instance_id} {row['ip']}:{row['port']}",
        message=f"{row['ip']} → {plain_ip}，新有效期 {_ts_after_minutes(ttl)}",
        detail={"actor": actor},
    )
    return {"ok": True, "ip": plain_ip, "rule_id": created.get("rule_id", "")}


def auto_renew_due_rules(now: Optional[str] = None) -> Dict[str, Any]:
    """
    处理已到期且勾选了「自动续期」的规则，按每条规则的 renew_mode 分流：

    * renew_mode="follow"（默认）：探测公网 IP。没变就顺延有效期（云端不动）；
      变了就删旧规则、按新 IP 重建，实现家庭宽带重播号后自动跟随；
    * renew_mode="same"：原地续期。无论公网 IP 是否变化，都保持规则原有 IP、
      仅把有效期顺延到「现在 + 默认时长」，不探测、不碰云端（适合固定 / 静态 IP）；
    * 探测失败（仅 follow 模式）：这一轮不续期，规则仍会被过期清理回收（宁可断开，也不盲目放开）。
    """
    now = now or now_str()
    rows = [
        r
        for r in store.find_rules(status_in=["active"], auto_renew=True)
        if (r.get("expires_at") or "") and r["expires_at"] <= now
    ]
    if not rows:
        return {"total": 0, "renewed": 0, "followed": 0, "failed": 0}

    ttl = int(settings.get("rules_policy.default_ttl_minutes") or 0) or 1440
    renewed = followed = failed = renewed_same = 0
    followed_from: List[str] = []
    public_ip = None  # 仅 follow 模式需要探测，惰性探测一次即可
    for row in rows:
        mode = (row.get("renew_mode") or "follow")
        if mode == "same":
            # 「原 IP 续期」：保持当前规则 IP，仅把有效期顺延到「现在 + TTL」，
            # 不探测公网 IP、不碰云端（适合固定 IP / 静态办公网段）。
            store.update_rule(row["id"], expires_at=_ts_after_minutes(ttl), notified_at="")
            renewed += 1
            renewed_same += 1
            continue
        # follow 模式：探测公网 IP，没变顺延、变了跟随
        if public_ip is None:
            public_ip = _probe_public_ip()
        if not public_ip:
            failed += 1
            logger.warning(f"自动续期跳过 {row['ip']}:{row['port']} —— 公网 IP 探测失败")
            continue
        if row["ip"] == public_ip:
            store.update_rule(row["id"], expires_at=_ts_after_minutes(ttl), notified_at="")
            renewed += 1
            continue
        result = _follow_ip(row, public_ip, ttl)
        if result.get("ok"):
            followed += 1
            followed_from.append(row["ip"])
        else:
            failed += 1
            logger.warning(f"自动续期跟随失败 {row['ip']}:{row['port']} - {result.get('message')}")

    store.log(
        "定时任务-自动续期",
        message=f"到期 {len(rows)} 条：续期 {renewed}，跟随新 IP {followed}，失败 {failed}",
        status="success" if failed == 0 else "warning",
    )
    if followed:
        # 多条端口规则一起跟随，只推一条通知
        _notify_ip_switch(followed_from, public_ip, "规则到期自动续期时发现公网 IP 变化")
    return {"total": len(rows), "renewed": renewed, "renewed_same": renewed_same,
            "followed": followed, "failed": failed, "public_ip": public_ip}


def _public_ip_ttl(cfg: Dict[str, Any]) -> int:
    """
    解析「公网 IP 自动放行」的有效期：

    * 负数（通常是 -1）→ 0，表示永久放行，不写 expires_at，永不参与过期清理；
    * 0                → 跟随 rules_policy.default_ttl_minutes；
    * 正数              → 直接使用该分钟数。
    """
    try:
        value = int(cfg.get("ttl_minutes") or 0)
    except (TypeError, ValueError):
        value = 0
    if value < 0:
        return 0
    if value == 0:
        return int(settings.get("rules_policy.default_ttl_minutes") or 0) or 1440
    return value


def sync_public_ip_rule() -> Dict[str, Any]:
    """
    每天把「当前公网 IP」同步进防火墙（settings.public_ip）。

    行为：没有对应规则就创建；已有同 IP 规则就刷新有效期；IP 变了就把旧的换掉。
    生成的规则自动带「自动续期」，避免两次任务之间到期被清理。
    """
    cfg = settings.section("public_ip")
    if not cfg.get("enabled"):
        return {"skipped": True, "reason": "未开启「公网 IP 自动放行」"}

    public_ip = _probe_public_ip()
    if not public_ip:
        return {"ok": False, "message": "探测公网 IP 失败（多个来源均不可用）"}

    ttl = _public_ip_ttl(cfg)
    tag_name = cfg.get("tag") or "公网IP"
    # 注意：不能只按 source='public_ip' 找。规则被「云端同步」收编后 source 会变成
    # cloud-sync，只按 source 匹配会让任务认为“本地没有该 IP 的规则”而反复新建，
    # 所以这里同时认「来源=public_ip」或「标签=公网IP」的行。
    rows = [
        r
        for r in store.find_rules(status_in=["active"])
        if r["source"] == "public_ip" or (tag_name and r["tag"] == tag_name)
    ]
    same = [r for r in rows if r["ip"] == public_ip]
    stale = [r for r in rows if r["ip"] != public_ip]
    stale_ips = [r["ip"] for r in stale]

    changed = 0
    for row in stale:
        if delete_rule(row["id"], actor="cron").get("ok"):
            changed += 1

    if same:
        expires_at = _ts_after_minutes(ttl)
        for row in same:
            store.update_rule(row["id"], expires_at=expires_at, notified_at="")
        # 历史遗留的重复行（同一 IP+协议+端口有多条）在这里顺手合并，
        # 这样即使还没跑到「云端同步」，也能自己把脏数据收敛掉。
        merged = 0
        for inst_id in {r["instance_id"] for r in same}:
            merged += store.dedupe_duplicate_rules(instance_id=inst_id)
        groups = {
            (r["instance_id"], store.natural_key(r["ip"], r["protocol"], r["port"]))
            for r in same
        }
        return {
            "ok": True,
            "ip": public_ip,
            "created": 0,
            "refreshed": len(groups),
            "merged": merged,
            "replaced": changed,
            "ttl_minutes": ttl,
            "expires_at": expires_at,
        }

    result = allow_ip(
        public_ip,
        ports=cfg.get("ports") or "ALL",
        protocol=cfg.get("protocol") or "tcp",
        description="公网 IP 自动跟随",
        source="public_ip",
        actor="cron",
        ttl_minutes=ttl,
        tag=cfg.get("tag") or "公网IP",
        cleanup_old=False,
        auto_renew=True,
    )
    if not result.get("ok"):
        return {"ok": False, "message": result.get("message") or "放行失败"}

    # 只有「真的换了 IP」才通知；首次创建（stale 为空）不打扰
    if stale_ips:
        _notify_ip_switch(stale_ips, public_ip, "定时同步发现公网 IP 变化")

    return {
        "ok": True,
        "ip": public_ip,
        "created": result.get("summary", {}).get("created", 0),
        "refreshed": 0,
        "replaced": changed,
        "ttl_minutes": ttl,
        "expires_at": _ts_after_minutes(ttl),
    }


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
