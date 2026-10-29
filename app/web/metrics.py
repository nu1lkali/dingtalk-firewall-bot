#!/usr/bin/env python3
"""
Prometheus 指标导出（文本格式，零依赖）。

挂载在 `GET /metrics`，默认允许免登录抓取（可在设置里关掉 metrics_public）。
只暴露聚合计数，不含 IP、AccessKey、Token 等敏感内容。
"""

from __future__ import annotations

import time
from typing import List

from app.settings import settings
from app.store import store

START_TS = time.time()


def _line(name: str, value, labels: str = "") -> str:
    return f"{name}{labels} {value}"


def render() -> str:
    out: List[str] = []

    # ---- 规则
    out.append("# HELP dcf_rules 本地规则数量")
    out.append("# TYPE dcf_rules gauge")
    rows = store._query("SELECT status, COUNT(*) AS n FROM rules GROUP BY status")  # noqa: SLF001
    for row in rows:
        out.append(_line("dcf_rules", row["n"], f'{{status="{row["status"]}"}}'))
    out.append(_line("dcf_rules_total", sum(r["n"] for r in rows)))

    out.append("# HELP dcf_rule_ips 生效规则涉及的唯一 IP 数")
    out.append("# TYPE dcf_rule_ips gauge")
    ips = store._query(  # noqa: SLF001
        "SELECT COUNT(DISTINCT ip) AS n FROM rules WHERE status='active'"
    )
    out.append(_line("dcf_rule_ips", ips[0]["n"] if ips else 0))

    out.append("# HELP dcf_rules_expiring 1 小时内即将到期的规则数")
    out.append("# TYPE dcf_rules_expiring gauge")
    from datetime import datetime, timedelta

    horizon = (datetime.now() + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    exp = store._query(  # noqa: SLF001
        "SELECT COUNT(*) AS n FROM rules WHERE status='active' AND expires_at<>'' AND expires_at<=?",
        (horizon,),
    )
    out.append(_line("dcf_rules_expiring", exp[0]["n"] if exp else 0))

    # ---- 任务
    out.append("# HELP dcf_task_runs_total 定时任务执行次数（累计）")
    out.append("# TYPE dcf_task_runs_total counter")
    runs = store._query(  # noqa: SLF001
        "SELECT job_id, status, COUNT(*) AS n FROM task_runs GROUP BY job_id, status"
    )
    for row in runs:
        out.append(
            _line(
                "dcf_task_runs_total",
                row["n"],
                f'{{job_id="{row["job_id"]}",status="{row["status"]}"}}',
            )
        )

    out.append("# HELP dcf_task_duration_ms 任务最近一次耗时")
    out.append("# TYPE dcf_task_duration_ms gauge")
    last = store._query(  # noqa: SLF001
        """SELECT r.job_id, r.duration_ms FROM task_runs r
           JOIN (SELECT job_id, MAX(id) AS mid FROM task_runs GROUP BY job_id) m
             ON r.id = m.mid"""
    )
    for row in last:
        out.append(_line("dcf_task_duration_ms", row["duration_ms"] or 0, f'{{job_id="{row["job_id"]}"}}'))

    # ---- 组件
    out.append("# HELP dcf_cron_running 调度器是否运行")
    out.append("# TYPE dcf_cron_running gauge")
    try:
        from app.cron.scheduler import scheduler_service

        out.append(_line("dcf_cron_running", 1 if scheduler_service.status().get("running") else 0))
    except Exception:  # noqa: BLE001
        out.append(_line("dcf_cron_running", 0))

    out.append("# HELP dcf_dingtalk_connected 钉钉长连接是否在线")
    out.append("# TYPE dcf_dingtalk_connected gauge")
    try:
        from app.dingtalk.bot import stream_runner

        state = stream_runner.status()
        out.append(_line("dcf_dingtalk_connected", 1 if state.get("connected") else 0))
        out.append(_line("dcf_dingtalk_frames_total", state.get("frames", 0)))
        out.append(_line("dcf_dingtalk_messages_total", state.get("messages", 0)))
    except Exception:  # noqa: BLE001
        out.append(_line("dcf_dingtalk_connected", 0))

    out.append("# HELP dcf_instances 纳管实例数")
    out.append("# TYPE dcf_instances gauge")
    out.append(_line("dcf_instances", len(settings.enabled_instances())))

    out.append("# HELP dcf_logs_total 日志条数（保留期内）")
    out.append("# TYPE dcf_logs_total gauge")
    logs = store._query("SELECT COUNT(*) AS n FROM logs")  # noqa: SLF001
    out.append(_line("dcf_logs_total", logs[0]["n"] if logs else 0))

    out.append("# HELP dcf_uptime_seconds 进程运行时长")
    out.append("# TYPE dcf_uptime_seconds counter")
    out.append(_line("dcf_uptime_seconds", int(time.time() - START_TS)))

    return "\n".join(out) + "\n"
