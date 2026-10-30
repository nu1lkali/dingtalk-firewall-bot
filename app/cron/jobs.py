#!/usr/bin/env python3
"""
定时任务具体实现。

每个任务返回 (status, message)，由调度器统一写入 task_runs 表，
Web 端即可看到每次执行的时间、耗时与结果。
"""

from __future__ import annotations

import glob
import os
from datetime import datetime
from typing import Any, Dict, Tuple

from app.core import rules_service
from app.settings import settings
from app.store import DB_PATH, store
from app.utils.logger import logger


def job_cleanup_expired() -> Tuple[str, str]:
    result = rules_service.cleanup_expired_rules()
    renewed = result.get("auto_renew") or {}
    extra = ""
    if renewed.get("total"):
        extra = (
            f"；自动续期 {renewed['renewed']} 条"
            f"，跟随新 IP {renewed['followed']} 条"
            f"，失败 {renewed['failed']} 条"
        )
    if result["total"] == 0:
        return "success", "没有需要清理的过期规则" + extra
    status = "success" if result["failed"] == 0 else "warning"
    return status, f"过期规则 {result['total']} 条：清理 {result['removed']}，失败 {result['failed']}{extra}"


def job_sync_public_ip() -> Tuple[str, str]:
    """每天把当前公网 IP 同步进防火墙（家庭宽带 / 动态 IP 场景）。"""
    result = rules_service.sync_public_ip_rule()
    if result.get("skipped"):
        return "skipped", result.get("reason", "未开启公网 IP 自动放行")
    if not result.get("ok"):
        return "failed", result.get("message", "同步失败")
    if result.get("created"):
        return "success", f"已按公网 IP {result['ip']} 新建规则，有效期至 {result['expires_at']}"
    return "success", (
        f"公网 IP {result['ip']} 未变，已续期 {result['refreshed']} 条至 {result['expires_at']}"
        f"（替换旧 IP 规则 {result['replaced']} 条）"
    )


def job_sync_cloud_rules() -> Tuple[str, str]:
    result = rules_service.sync_all_instances()
    if result["errors"]:
        return "warning", f"同步 {result['synced_rules']} 条；异常 {len(result['errors'])} 个实例"
    return "success", f"同步完成，云端共 {result['synced_rules']} 条规则"


def job_cleanup_stale() -> Tuple[str, str]:
    result = rules_service.cleanup_stale_ip_rules()
    if result.get("skipped"):
        return "skipped", result.get("reason", "策略未开启")
    return "success", f"清理 {result['removed']} 条超过 {result['days']} 天未更新的自动规则"


def job_prune_logs() -> Tuple[str, str]:
    days = int(settings.get("logging.keep_days", 30) or 30)
    removed = store.prune_logs(keep_days=days)
    return "success", f"清理 {days} 天前的日志/任务记录共 {removed} 条"


def job_health_probe() -> Tuple[str, str]:
    result = rules_service.health_probe()
    if result["ok"]:
        count = result.get("detail", {}).get("instance_count", 0)
        return "success", f"阿里云凭证正常，可访问实例 {count} 台"
    return "failed", f"阿里云凭证异常：{result['detail']}"


def job_daily_report() -> Tuple[str, str]:
    from app.dingtalk.client import notify

    stats = rules_service.stats()
    sched = _scheduler_status()
    text = (
        "### 📅 每日巡检报告\n\n"
        f"- 生效规则：**{stats['active_rules']}** 条（{stats['unique_ips']} 个 IP）\n"
        f"- 即将过期：{stats['expiring_soon']} 条 · 已禁用：{stats['disabled_rules']} 条\n"
        f"- 纳管实例：{stats['instances']} 台\n"
        f"- 定时任务：{sched.get('job_count', 0)} 个，运行中 {sched.get('running_jobs', 0)} 个"
    )
    sent = notify(text, title="每日巡检报告")
    return "success", "报告已推送钉钉群" if sent else "报告已生成（未配置通知群，未推送）"


def _scheduler_status() -> Dict[str, Any]:
    try:
        from app.cron.scheduler import scheduler_service

        return scheduler_service.status()
    except Exception:  # noqa: BLE001
        return {}


def job_expiring_notice() -> Tuple[str, str]:
    result = rules_service.run_expiring_notice()
    if result.get("skipped"):
        return "skipped", result.get("reason", "未开启到期提醒")
    if not result.get("sent"):
        return "success", "没有即将到期的规则"
    return "success", f"已提醒 {result['sent']} 条 {result['minutes']} 分钟内到期的规则"


def job_backup_db() -> Tuple[str, str]:
    """SQLite 在线备份，按 backup_keep 滚动保留。"""
    keep = int(settings.get("logging.backup_keep", 7) or 7)
    backup_dir = os.path.join(os.path.dirname(os.path.abspath(DB_PATH)), "backups")
    path = os.path.join(backup_dir, f"app-{datetime.now().strftime('%Y%m%d-%H%M%S')}.db")
    try:
        store.backup_to(path)
    except Exception as exc:  # noqa: BLE001
        logger.error(f"数据库备份失败: {exc}")
        return "failed", f"备份失败：{exc}"

    removed = 0
    files = sorted(glob.glob(os.path.join(backup_dir, "app-*.db")))
    if keep > 0:
        for old in files[:-keep]:
            try:
                os.remove(old)
                removed += 1
            except OSError as exc:
                logger.warning(f"清理旧备份失败 {old}: {exc}")
    size_kb = os.path.getsize(path) // 1024
    kept = len(glob.glob(os.path.join(backup_dir, "app-*.db")))
    return "success", f"已备份 {os.path.basename(path)}（{size_kb} KB），清理旧备份 {removed} 份，当前保留 {kept} 份"


JOB_FUNCS = {
    "cleanup_expired": job_cleanup_expired,
    "sync_cloud_rules": job_sync_cloud_rules,
    "cleanup_stale_ip": job_cleanup_stale,
    "prune_logs": job_prune_logs,
    "health_probe": job_health_probe,
    "daily_report": job_daily_report,
    "expiring_notice": job_expiring_notice,
    "sync_public_ip": job_sync_public_ip,
    "backup_db": job_backup_db,
}

JOB_DESCRIPTIONS = {
    "cleanup_expired": "扫描本地记录中已到期的放行规则，并从云端删除",
    "sync_cloud_rules": "定期拉取云端防火墙规则，修正本地镜像与漂移",
    "cleanup_stale_ip": "清理长期未刷新的自动规则，防止规则无序堆积",
    "prune_logs": "按保留天数清理操作日志、任务记录与机器人消息",
    "health_probe": "验证阿里云 AccessKey 是否可用，提前发现凭证失效",
    "daily_report": "每天汇总规则数量与任务状态并推送钉钉群",
    "expiring_notice": "规则到期前推送提醒，避免业务被误断",
    "sync_public_ip": "每天把当前公网 IP 同步进防火墙，IP 变了自动替换",
    "backup_db": "在线备份 SQLite 数据库，并滚动清理旧备份",
}
