#!/usr/bin/env python3
"""
Cron 表达式工具：校验、中文释义、未来执行时间预测。

供 Web 端「可视化 cron 配置器」使用——不懂 cron 的人也能看懂自己配了什么时候跑。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from apscheduler.triggers.cron import CronTrigger

DEFAULT_TZ = "Asia/Shanghai"

# 标准 cron 的数字：0/7=周日，1=周一 ... 6=周六
# ⚠️ APScheduler 的 day_of_week 是 0=周一，语义相反，必须转成英文名再传，否则 1-5 会变成 周二~周六
_DOW_NUM_CN = {0: "周日", 1: "周一", 2: "周二", 3: "周三", 4: "周四", 5: "周五", 6: "周六"}
_APS_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _tz(name: str) -> Any:
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name or DEFAULT_TZ)
    except Exception:  # noqa: BLE001
        return None


def _dow_name(token: str) -> str:
    """把标准 cron 的星期数字/英文名统一成 APScheduler 认识的英文名。"""
    token = token.strip().lower()
    if token in {"0", "7"}:
        return "sun"
    if token.isdigit() and 1 <= int(token) <= 6:
        return _APS_NAMES[int(token) - 1]
    return token


def _normalize_dow(field: str) -> str:
    """day_of_week 字段做数字→英文名转换，保证与标准 cron 语义一致。"""
    field = (field or "*").strip()
    if not field or field == "*":
        return "*"
    out: List[str] = []
    for token in field.split(","):
        token = token.strip().lower()
        if not token:
            continue
        step = ""
        if "/" in token:
            token, _, s = token.partition("/")
            step = f"/{s}"
        if "-" in token:
            a, _, b = token.partition("-")
            out.append(f"{_dow_name(a)}-{_dow_name(b)}{step}")
        else:
            out.append(f"{_dow_name(token)}{step}")
    return ",".join(out)


def make_trigger(expr: str, timezone: str = DEFAULT_TZ) -> CronTrigger:
    """构造 CronTrigger；表达式非法时抛 ValueError。"""
    expr = (expr or "").strip()
    if not expr:
        raise ValueError("表达式不能为空")
    parts = expr.split()
    if len(parts) != 5:
        raise ValueError("需要 5 段：分 时 日 月 周，例如 0 3 * * *")
    minute, hour, dom, month, dow = parts
    return CronTrigger(
        minute=minute,
        hour=hour,
        day=dom,
        month=month,
        day_of_week=_normalize_dow(dow),
        timezone=_tz(timezone),
    )


def validate(expr: str, timezone: str = DEFAULT_TZ) -> Tuple[bool, str]:
    """返回 (是否合法, 错误信息)。"""
    try:
        make_trigger(expr, timezone)
        return True, ""
    except ValueError as exc:
        return False, str(exc).split("\n")[0]
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def next_runs(expr: str, count: int = 5, timezone: str = DEFAULT_TZ) -> List[str]:
    """预测接下来 count 次执行时间。"""
    try:
        trigger = make_trigger(expr, timezone)
    except ValueError:
        return []
    out: List[str] = []
    now = datetime.now(_tz(timezone))
    prev: Optional[datetime] = None
    for _ in range(max(1, min(int(count or 5), 10))):
        nxt = trigger.get_next_fire_time(prev, now if prev is None else prev)
        if not nxt:
            break
        out.append(nxt.strftime("%Y-%m-%d %H:%M:%S"))
        prev = nxt
    return out


# --------------------------------------------------------------------------- 中文释义
def _is_star(field: str) -> bool:
    return field.strip() == "*"


def _step(field: str) -> Optional[int]:
    field = field.strip()
    if field.startswith("*/"):
        try:
            return int(field[2:])
        except ValueError:
            return None
    return None


def _num_list(field: str) -> List[int]:
    out: List[int] = []
    for token in field.split(","):
        token = token.strip()
        if not token:
            continue
        try:
            out.append(int(token))
        except ValueError:
            continue
    return out


def _dow_num(token: str) -> Optional[int]:
    token = token.strip().lower()
    if token in {"0", "7"}:
        return 0
    if token.isdigit():
        return int(token) if 0 <= int(token) <= 6 else None
    idx = {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}.get(token)
    return idx


def _dow_texts(field: str) -> List[str]:
    """把星期字段翻成中文列表（标准 cron 语义：0/7=周日）。"""
    out: List[str] = []
    for token in str(field or "").split(","):
        token = token.strip().lower()
        if not token:
            continue
        if "/" in token:
            token = token.split("/")[0]
        if "-" in token:
            a, _, b = token.partition("-")
            ia, ib = _dow_num(a), _dow_num(b)
            if ia is None or ib is None:
                continue
            day = ia
            for _ in range(7):
                out.append(_DOW_NUM_CN[day])
                if day == ib:
                    break
                day = (day + 1) % 7
        else:
            num = _dow_num(token)
            if num is not None:
                out.append(_DOW_NUM_CN[num])
    seen = set()
    return [d for d in out if not (d in seen or seen.add(d))]


def describe(expr: str, timezone: str = DEFAULT_TZ) -> str:
    """把 cron 表达式翻译成一句中文。非法表达式直接返回错误提示。"""
    expr = (expr or "").strip()
    ok, err = validate(expr, timezone)
    if not ok:
        return f"表达式不合法：{err}"

    minute, hour, dom, month, dow = expr.split()

    m_step = _step(minute)
    h_step = _step(hour)
    minutes = _num_list(minute)
    hours = _num_list(hour)

    # 1) 每分钟
    if _is_star(minute) and _is_star(hour) and _is_star(dom) and _is_star(month) and _is_star(dow):
        return "每分钟执行一次"

    # 2) 每 N 分钟
    if m_step and _is_star(hour):
        return f"每 {m_step} 分钟执行一次"

    # 3) 每小时第 M 分钟
    if minutes and _is_star(hour):
        return "每小时的第 " + "、".join(str(m) for m in minutes) + " 分钟执行"

    # 4) 每 N 小时
    if h_step and minutes:
        mm = "、".join(f"{m:02d}" for m in minutes)
        return f"每 {h_step} 小时执行一次（第 {mm} 分钟）"

    dow_text = "" if _is_star(dow) else "、".join(_dow_texts(dow))
    prefix = "每天 "
    if dow_text:
        prefix = f"每周 {dow_text} 的 "
    elif not _is_star(dom):
        prefix = f"每月 {dom} 日的 "

    # 5) 具体时刻
    if minutes and hours:
        clock = "、".join(f"{h:02d}:{m:02d}" for h in sorted(hours) for m in sorted(minutes))
        return f"{prefix}{clock} 执行"

    # 6) 小时区间 + 指定分钟，例如 15,45 9-18 * * *
    if minutes and "-" in hour:
        a, _, b = hour.partition("-")
        mm = "、".join(f"{m:02d}" for m in minutes)
        return f"{prefix}{a}:00 至 {b}:59 之间的第 {mm} 分钟执行"

    return f"自定义：{expr}"


def summarize(expr: str, count: int = 5, timezone: str = DEFAULT_TZ) -> Dict[str, Any]:
    """一次返回前端需要的全部信息。"""
    ok, err = validate(expr, timezone)
    return {
        "expr": (expr or "").strip(),
        "valid": ok,
        "error": err,
        "description": describe(expr, timezone) if ok else f"表达式不合法：{err}",
        "next_runs": next_runs(expr, count, timezone) if ok else [],
    }
