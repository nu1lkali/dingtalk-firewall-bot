#!/usr/bin/env python3
"""
规则变更通知（钉钉）。

设计原则：
  * **模板化** —— 文案在设置里改，不用动代码；
  * **绝不影响主流程** —— 通知发不出去只是少条消息，不能让「放行」失败；
  * **默认异步** —— 网络调用放到后台线程，Web 接口不被钉钉的响应时间拖慢。

可用占位符（settings.notify.template）：
    {action} {ip} {port} {protocol} {instance} {actor} {source}
    {expire} {tag} {time} {detail}

未知占位符会被替换成空串，不会抛 KeyError。
"""

from __future__ import annotations

import threading
from datetime import datetime
from typing import Any, Dict, Iterable, List

from app.settings import settings
from app.utils.logger import logger

DEFAULT_TEMPLATE = "{action} {ip} {port}（{protocol}）· 实例 {instance} · 操作人 {actor}"

ACTION_TEXT = {
    "allow": "🟢 放行",
    "delete": "🗑️ 删除",
    "renew": "🔁 续期",
    "expire": "⏰ 到期回收",
    "restore": "♻️ 恢复",
    "toggle_off": "⏸ 禁用",
    "toggle_on": "▶️ 启用",
}

# kind -> 对应的开关字段
SWITCHES = {
    "allow": "on_allow",
    "delete": "on_delete",
    "renew": "on_renew",
    "expire": "on_expire",
}


class _SafeDict(dict):
    def __missing__(self, key):  # noqa: D105
        return ""


def render_line(kind: str, ctx: Dict[str, Any], template: str = "") -> str:
    """渲染单行文案；模板里的未知占位符一律变空串。"""
    data = _SafeDict(
        action=ACTION_TEXT.get(kind, kind),
        ip=ctx.get("ip", ""),
        port=ctx.get("port_display") or ctx.get("port", ""),
        protocol=str(ctx.get("protocol", "")).upper(),
        instance=ctx.get("instance_name") or ctx.get("instance_id", ""),
        actor=ctx.get("actor", ""),
        source=ctx.get("source", ""),
        expire=ctx.get("expires_at") or "长期",
        tag=ctx.get("tag", ""),
        detail=ctx.get("detail", ""),
        time=datetime.now().strftime("%m-%d %H:%M:%S"),
    )
    text = (template or settings.get("notify.template") or DEFAULT_TEMPLATE).strip()
    try:
        return text.format_map(data)
    except Exception:  # noqa: BLE001 - 模板写坏了也不能炸
        return DEFAULT_TEMPLATE.format_map(data)


def build_message(kind: str, rows: Iterable[Dict[str, Any]], extra: str = "") -> str:
    rows = list(rows or [])
    title = settings.get("notify.title") or "防火墙规则变更"
    template = settings.get("notify.template") or DEFAULT_TEMPLATE
    lines: List[str] = [f"### 🔔 {title}", ""]
    for row in rows[:20]:
        lines.append(f"- {render_line(kind, row, template)}")
    if len(rows) > 20:
        lines.append(f"- 其余 {len(rows) - 20} 条请在管理后台查看")
    if extra:
        lines.extend(["", extra])
    lines.extend(["", f"<font color=#8C8C8C>动态云防火墙 · {datetime.now().strftime('%m-%d %H:%M:%S')}</font>"])
    return "\n".join(lines)


def notify_change(
    kind: str,
    rows: Iterable[Dict[str, Any]],
    extra: str = "",
    background: bool = True,
) -> bool:
    """
    推送一条规则变更通知。

    返回是否**已发起**推送（异步模式下不代表送达）。任何异常都吞掉并记日志。
    """
    try:
        if not settings.get("notify.enabled", False):
            return False
        switch = SWITCHES.get(kind)
        if switch and not settings.get(f"notify.{switch}", True):
            return False
        rows = [r for r in (rows or []) if r]
        if not rows:
            return False
        text = build_message(kind, rows, extra)

        from app.dingtalk.client import notify

        def _send() -> None:
            try:
                notify(text, title=settings.get("notify.title") or "防火墙规则变更")
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"规则变更通知发送失败: {exc}")

        if background:
            threading.Thread(target=_send, name="notify-rules", daemon=True).start()
        else:
            _send()
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"规则变更通知构造失败: {exc}")
        return False
