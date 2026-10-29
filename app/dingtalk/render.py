#!/usr/bin/env python3
"""
钉钉回执卡片渲染。

⚠️ 钉钉 markdown 的排版坑：
    连续的单行（含 `>` 引用块里的多行）会被**合并成一个段落**，换行符被吃掉。
    因此所有「一行一条」的信息一律用无序列表 `- ` 承载，渲染时才不会挤成一坨。

版式约定：
    ### 标题
    - 关键信息（每行一个列表项）
    **分区标题**
    - 明细
    ━━━━━━━ 分隔条
    页脚（灰色小字）
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List

GREEN = "#00B578"
BLUE = "#1677FF"
ORANGE = "#FF8F1F"
RED = "#F5222D"
GRAY = "#8C8C8C"

BAR = "━━━━━━━━━━━━━━━"

# 示例 IP：用看起来像真实公网地址的值，比 1.2.3.4 更直观（勿改成线上真实资产）
DEMO_IP = "203.0.113.42"
DEMO_IP2 = "203.0.113.43"


def _c(text: str, color: str) -> str:
    return f"<font color={color}>{text}</font>"


def _footer(actor: str = "") -> str:
    who = f" · 操作人 {actor}" if actor else ""
    return _c(f"🤖 动态云防火墙{who} · {datetime.now().strftime('%m-%d %H:%M:%S')}", GRAY)


def _item(text: str) -> str:
    """一行一个列表项，避免钉钉把多行合并。"""
    return f"- {text}"


def _section(title: str) -> str:
    return f"**{title}**"


# --------------------------------------------------------------------------- 纯文本
class PlainText(str):
    """
    标记为「纯文本回复」。

    继承 str，所以所有把它当字符串用的地方都不受影响；
    上层通过 isinstance 判断后改用 text 消息发送，
    钉钉就不会把普通内容按 markdown 规范渲染（行内反引号变成蓝色代码框）。
    """


def plain(text: str) -> PlainText:
    return PlainText(text)


def _port_text(port: str) -> str:
    raw = str(port or "").strip()
    if not raw or raw.upper() == "ALL" or raw == "1/65535":
        return "全部端口"
    return raw.replace("/", "-")


def allow_reply(result: Dict[str, Any], ttl: int = 0, actor: str = "") -> Any:
    """
    放行结果：简单场景（单实例单 IP 且无失败）用纯文本，
    需要看明细时才退回 markdown 卡片。
    """
    summary = result.get("summary") or {}
    rows = result.get("results") or []
    if result.get("ok") and len(rows) == 1 and not summary.get("failed"):
        row = rows[0]
        proto = (result.get("protocol") or "TCP").upper()
        ttl_text = f"{ttl} 分钟后自动回收" if ttl and ttl > 0 else "长期有效"
        head = "✅ 已放行" if row.get("status") == "success" else "ℹ️ 规则已存在，无需重复放行"
        return plain(
            f"{head} {row.get('ip')}\n"
            f"端口：{_port_text(row.get('port'))}（{proto}）\n"
            f"有效期：{ttl_text}"
        )
    if not rows:
        return plain(f"❌ 放行失败：{result.get('message') or '没有可用的阿里云实例'}")
    return allow_card(result, ttl=ttl, actor=actor)


def delete_reply(ips: List[str], done: int, failed: int, rows: List[Dict[str, Any]]) -> Any:
    """删除结果：单条且无失败时用纯文本。"""
    ip = (ips or [""])[0]
    if failed == 0 and done == 0:
        return plain(f"🤔 {ip} 当前没有生效中的放行规则")
    if failed == 0 and done == 1:
        return plain(f"🗑️ 已删除 {ip} 的放行规则")
    return delete_card(ips, done, failed, rows)


def renew_reply(
    ips: List[str], done: int, failed: int, rows: List[Dict[str, Any]], minutes: int = 1440
) -> Any:
    """续期结果：单条成功用纯文本，其余走卡片。"""
    ip = (ips or [""])[0]
    if done == 0 and failed == 0:
        return plain(f"🤔 {ip} 当前没有生效中的放行规则，无法续期")
    if failed == 0 and done == 1 and rows:
        row = rows[0]
        return plain(
            f"⏱ 已续期 {row.get('ip')}\n"
            f"端口：{_port_text(row.get('port'))}\n"
            f"新的到期时间：{row.get('expires_at') or '-'}"
        )
    return renew_card(ips, done, failed, rows, minutes)


def myip_reply(ip: str, actor: str = "") -> Any:
    if ip:
        return plain(
            f"🌐 你的出口 IP：{ip}\n"
            f"直接把这个 IP 发给我即可放行全部端口；\n"
            f"想只开某个端口，发送：{ip} 22"
        )
    return myip_card(ip, actor)


def error_reply(title: str, detail: str, tip: str = "") -> Any:
    lines = [f"❌ {title}", detail]
    if tip:
        lines.append(tip)
    return plain("\n".join(lines))


def unknown_reply(text: str) -> Any:
    return plain(
        f"❓ 没看懂「{text}」\n"
        f"直接发 IP 即可放行，例如：{DEMO_IP}\n"
        f"想只开某个端口就发：{DEMO_IP} 22\n"
        f"发送「帮助」查看全部指令"
    )


# --------------------------------------------------------------------------- 放行
def allow_card(result: Dict[str, Any], ttl: int = 0, actor: str = "") -> str:
    summary = result.get("summary", {}) or {}
    ok = bool(result.get("ok"))
    failed = summary.get("failed", 0)
    created = summary.get("created", 0)
    skipped = summary.get("skipped", 0)

    if ok:
        title = f"### {_c('✅ 放行成功', GREEN)}"
    elif failed and (created or skipped):
        title = f"### {_c('⚠️ 部分成功', ORANGE)}"
    else:
        title = f"### {_c('❌ 放行失败', RED)}"

    ports = result.get("ports") or ["ALL"]
    port_text = "全部端口" if ports == ["ALL"] else "、".join(
        p.replace("/", "-") for p in ports
    )
    ips = result.get("ips") or []
    ip_text = "、".join(f"`{ip}`" for ip in ips)

    lines = [title, ""]
    lines.append(_item(f"🌐 **IP**　{ip_text}"))
    lines.append(_item(f"🔌 **端口**　{port_text}（{result.get('protocol', 'TCP').upper()}）"))
    lines.append(
        _item(
            "⏳ **有效期**　"
            + (f"{ttl} 分钟后自动回收" if ttl and ttl > 0 else "长期有效")
        )
    )
    stat = f"新增 {_c(str(created), GREEN)} · 已存在 {_c(str(skipped), GRAY)}"
    if failed:
        stat += f" · 失败 {_c(str(failed), RED)}"
    lines.append(_item(f"📦 **结果**　{stat}"))

    rows = result.get("results", []) or []
    if rows:
        lines.append("")
        lines.append(_section("生效明细"))
        for item in rows[:10]:
            status = item.get("status")
            icon = {"success": "🟢", "skipped": "⚪️", "failed": "🔴"}.get(status, "•")
            name = item.get("instance_name") or item.get("instance_id") or "-"
            port = item.get("port_display") or item.get("port") or ""
            target = f"`{item['ip']}`" if item.get("ip") else ""
            if port:
                target += f" : {port}"
            detail = item.get("message") or ""
            color = {"success": GREEN, "skipped": GRAY, "failed": RED}.get(status, GRAY)
            lines.append(_item(f"{icon} **{name}**　{target}　{_c(detail, color)}"))
        if len(rows) > 10:
            lines.append(_item(_c(f"其余 {len(rows) - 10} 条请在 Web 端查看", GRAY)))

    lines.extend(["", BAR, _footer(actor)])
    return "\n".join(lines)


# --------------------------------------------------------------------------- 列表
def list_card(rules: List[Dict[str, Any]], total: int = 0) -> str:
    if not rules:
        return "\n".join(
            [
                f"### {_c('📋 放行规则', BLUE)}",
                "",
                "当前没有任何生效中的规则。",
                "",
                _item(f"发送 `{DEMO_IP}` 放行全部端口"),
                _item(f"发送 `{DEMO_IP} 22` 只放行 22 端口"),
                "",
                BAR,
                _footer(),
            ]
        )

    lines = [f"### {_c('📋 放行规则', BLUE)}", ""]
    lines.append(_c(f"共 {max(total, len(rules))} 条生效中，展示最近 {len(rules)} 条", GRAY))
    lines.append("")
    for idx, rule in enumerate(rules, 1):
        port = (rule.get("port") or "1/65535").replace("/", "-")
        port_text = "全部端口" if port in {"1-65535", "ALL"} else port
        expire = rule.get("expires_at") or "长期"
        instance = rule.get("instance_name") or rule.get("instance_id") or ""
        proto = (rule.get("protocol") or "tcp").upper()
        lines.append(
            _item(
                f"**{idx}. `{rule.get('ip')}`**　{port_text}　"
                f"{_c(f'{instance} · {proto} · 到期 {expire}', GRAY)}"
            )
        )
    lines.extend(["", BAR, _footer()])
    return "\n".join(lines)


# --------------------------------------------------------------------------- 我的IP
def myip_card(ip: str, actor: str = "") -> str:
    lines = [f"### {_c('🌐 你的出口 IP', BLUE)}", ""]
    if ip:
        lines.append(_item(f"当前出口 IP：`{ip}`"))
        lines.append("")
        lines.append(_section("直接复制发送即可"))
        lines.append(_item(f"`{ip}`　放行全部端口"))
        lines.append(_item(f"`{ip} 22`　只放行 22 端口"))
    else:
        lines.append(_item(_c("钉钉服务端拿不到你的真实出口 IP", ORANGE)))
        lines.append("")
        lines.append(f"可以直接把 IP 发给我，例如 `{DEMO_IP}`。")
    lines.extend(["", BAR, _footer(actor)])
    return "\n".join(lines)


# --------------------------------------------------------------------------- 状态
def status_card(stats: Dict[str, Any], sched: Dict[str, Any], dingtalk: Dict[str, Any]) -> str:
    running = sched.get("running")
    sched_text = _c("运行中", GREEN) if running else _c("未运行", RED)
    dt_text = _c("已连接", GREEN) if dingtalk.get("connected") else _c("未连接", ORANGE)

    lines = [f"### {_c('📊 系统状态', BLUE)}", ""]
    lines.append(_item(f"🛡 **生效规则**　{stats.get('active_rules', 0)} 条（{stats.get('unique_ips', 0)} 个 IP）"))
    lines.append(_item(f"⏳ **即将过期**　{stats.get('expiring_soon', 0)} 条"))
    lines.append(_item(f"🚫 **已禁用**　{stats.get('disabled_rules', 0)} 条"))
    lines.append(_item(f"🖥 **纳管实例**　{stats.get('instances', 0)} 台"))
    lines.append("")
    lines.append(_section("运行组件"))
    lines.append(_item(f"⏱ **定时任务**　{sched_text} · {sched.get('running_jobs', 0)} 个在调度"))
    lines.append(_item(f"🤖 **机器人**　{dt_text} · {dingtalk.get('mode', 'stream')} 模式"))
    lines.extend(["", BAR, _footer()])
    return "\n".join(lines)


# --------------------------------------------------------------------------- 删除
def delete_card(ips: List[str], done: int, failed: int, rows: List[Dict[str, Any]]) -> str:
    ip_text = "、".join(f"`{ip}`" for ip in ips)
    if done == 0 and failed == 0:
        return "\n".join(
            [
                f"### {_c('🤔 没有找到匹配规则', ORANGE)}",
                "",
                _item(f"🌐 **目标**　{ip_text}"),
                _item("该 IP 当前没有生效中的放行规则"),
                "",
                BAR,
                _footer(),
            ]
        )

    title = f"### {_c('🗑️ 删除完成', GREEN)}" if failed == 0 else f"### {_c('⚠️ 部分删除失败', ORANGE)}"
    lines = [title, ""]
    lines.append(_item(f"🌐 **目标**　{ip_text}"))
    stat = f"成功 {_c(str(done), GREEN)}"
    if failed:
        stat += f" · 失败 {_c(str(failed), RED)}"
    lines.append(_item(f"📦 **结果**　{stat}"))
    if rows:
        lines.append("")
        lines.append(_section("已移除规则"))
        for rule in rows[:10]:
            port = (rule.get("port") or "1/65535").replace("/", "-")
            if port in {"1-65535", "ALL"}:
                port = "全部端口"
            instance = rule.get("instance_name") or rule.get("instance_id")
            lines.append(_item(f"`{rule.get('ip')}` : {port}　{_c(instance or '', GRAY)}"))
    lines.extend(["", BAR, _footer()])
    return "\n".join(lines)


# --------------------------------------------------------------------------- 帮助
def renew_card(
    ips: List[str], done: int, failed: int, rows: List[Dict[str, Any]], minutes: int = 1440
) -> str:
    ip_text = "、".join(f"`{ip}`" for ip in ips)
    if done == 0 and failed == 0:
        return "\n".join(
            [
                f"### {_c('🤔 没有可续期的规则', ORANGE)}",
                "",
                _item(f"🌐 **目标**　{ip_text}"),
                _item("该 IP 当前没有生效中的放行规则"),
                "",
                _c("需要重新放行的话，直接把 IP 发给机器人即可。", GRAY),
                BAR,
                _footer(),
            ]
        )

    title = f"### {_c('⏱ 续期完成', GREEN)}" if failed == 0 else f"### {_c('⚠️ 部分续期失败', ORANGE)}"
    lines = [title, ""]
    lines.append(_item(f"🌐 **目标**　{ip_text}"))
    lines.append(_item(f"⏳ **延长时间**　{_duration_text(minutes)}（从当前时间起算）"))
    stat = f"成功 {_c(str(done), GREEN)}"
    if failed:
        stat += f" · 失败 {_c(str(failed), RED)}"
    lines.append(_item(f"📦 **结果**　{stat}"))
    if rows:
        lines.append("")
        lines.append(_section("明细"))
        for row in rows[:10]:
            icon = "🟢" if row.get("ok") else "🔴"
            port = _port_text(row.get("port"))
            tail = row.get("expires_at") if row.get("ok") else (row.get("message") or "失败")
            color = GREEN if row.get("ok") else RED
            lines.append(_item(f"{icon} `{row.get('ip')}` : {port}　{_c(str(tail), color)}"))
    lines.extend(["", BAR, _footer()])
    return "\n".join(lines)


def _duration_text(minutes: int) -> str:
    if minutes % 1440 == 0 and minutes >= 1440:
        return f"{minutes // 1440} 天"
    if minutes % 60 == 0 and minutes >= 60:
        return f"{minutes // 60} 小时"
    return f"{minutes} 分钟"


def help_card() -> str:
    lines = [
        f"### {_c('🤖 IP 放行助手', BLUE)}",
        "",
        _c("直接发 IP 即可放行，不写端口默认放行全部端口", GRAY),
        "",
        _section(f"{_c('放行指令', GREEN)}"),
        _item(f"`{DEMO_IP}`　→　放行全部端口"),
        _item(f"`{DEMO_IP} 22`　→　仅放行 TCP 22"),
        _item(f"`{DEMO_IP} 22,80,443`　→　多个端口"),
        _item(f"`{DEMO_IP} 8000-9000`　→　端口段"),
        _item(f"`{DEMO_IP} 53 udp`　→　指定 UDP 协议"),
        _item(f"`{DEMO_IP} {DEMO_IP2} 22`　→　多 IP 同端口"),
        "",
        _section(f"{_c('管理指令', BLUE)}"),
        _item("`列表`　→　查看当前放行规则"),
        _item("`状态`　→　查看系统运行概览"),
        _item(f"`续期 {DEMO_IP}`　→　延长 24 小时（可写 `24h` / `30m` / `7d`）"),
        _item(f"`删除 {DEMO_IP}`　→　撤销放行"),
        _item("`我的IP`　→　查询你的出口 IP"),
        "",
        BAR,
        _footer(),
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- 异常
def error_card(title: str, detail: str, tip: str = "") -> str:
    lines = [f"### {_c('❌ ' + title, RED)}", ""]
    lines.append(_item(detail))
    if tip:
        lines.append("")
        lines.append(_c(tip, GRAY))
    lines.extend(["", BAR, _footer()])
    return "\n".join(lines)


def unknown_card(text: str) -> str:
    return "\n".join(
        [
            f"### {_c('❓ 指令无法识别', ORANGE)}",
            "",
            _item(f"🗣 **你的输入**　`{text}`"),
            "",
            "直接发 IP 即可放行全端口，例如：",
            _item(f"`{DEMO_IP}`　→　全部端口"),
            _item(f"`{DEMO_IP} 22`　→　仅 22 端口"),
            "",
            _c("发送「帮助」查看全部指令", GRAY),
            BAR,
            _footer(),
        ]
    )
