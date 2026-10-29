#!/usr/bin/env python3
"""
内置调度器（APScheduler），随 Web 服务一起启动，无需外部 cron。

* 任务列表、cron 表达式全部来自 Web 端配置，可在页面中修改后热重载
* 每次执行都写入 task_runs 表，支持「立即执行」与手动暂停
"""

from __future__ import annotations

import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from apscheduler.schedulers.background import BackgroundScheduler

from app.cron.cronexpr import DEFAULT_TZ, describe, make_trigger
from app.cron.jobs import JOB_DESCRIPTIONS, JOB_FUNCS
from app.settings import settings
from app.store import now_str, store
from app.utils.logger import logger


class SchedulerService:
    def __init__(self) -> None:
        self._scheduler: Optional[BackgroundScheduler] = None
        self._lock = threading.RLock()
        self._last_runs: Dict[str, Dict[str, Any]] = {}
        self.started_at = ""

    # ------------------------------------------------------------------ 生命周期
    def start(self) -> None:
        with self._lock:
            if self._scheduler and self._scheduler.running:
                self._scheduler.shutdown(wait=False)
            cfg = settings.section("cron")
            if not cfg.get("enabled", True):
                logger.info("定时任务已禁用，跳过启动")
                self._scheduler = None
                return

            self._scheduler = BackgroundScheduler(
                timezone=cfg.get("timezone") or "Asia/Shanghai",
                job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 120},
            )
            self._register_jobs()
            self._scheduler.start()
            self.started_at = now_str()
            logger.info(f"调度器已启动，共 {len(self._scheduler.get_jobs())} 个任务")

    def _register_jobs(self) -> None:
        if not self._scheduler:
            return
        jobs_cfg = settings.get("cron.jobs", {}) or {}
        for job_id, func in JOB_FUNCS.items():
            cfg = jobs_cfg.get(job_id) or {}
            if not cfg.get("enabled", True):
                continue
            expr = cfg.get("cron") or "*/30 * * * *"
            try:
                trigger = make_trigger(expr, cfg.get("timezone") or settings.get("cron.timezone") or DEFAULT_TZ)
            except ValueError as exc:
                logger.error(f"任务 {job_id} 的 cron 表达式非法({expr}): {exc}")
                continue
            self._scheduler.add_job(
                self._run,
                trigger=trigger,
                id=job_id,
                name=cfg.get("name") or job_id,
                args=[job_id, "schedule"],
                replace_existing=True,
            )

    def reload(self) -> Dict[str, Any]:
        self.start()
        return self.status()

    def shutdown(self) -> None:
        with self._lock:
            if self._scheduler and self._scheduler.running:
                self._scheduler.shutdown(wait=False)
                logger.info("调度器已停止")
            self._scheduler = None

    # ------------------------------------------------------------------ 执行
    def _run(self, job_id: str, trigger_type: str = "schedule") -> None:
        func = JOB_FUNCS.get(job_id)
        if not func:
            return
        run_id = store.start_task_run(job_id, JOB_DESCRIPTIONS.get(job_id, job_id))
        started = time.time()
        status, message = "success", ""
        try:
            status, message = func()
        except Exception as exc:  # noqa: BLE001
            status = "failed"
            message = str(exc)
            logger.error(f"定时任务 {job_id} 执行失败: {exc}", exc_info=True)
        duration = int((time.time() - started) * 1000)
        store.finish_task_run(run_id, status, message, duration)
        self._last_runs[job_id] = {
            "at": now_str(),
            "status": status,
            "message": message,
            "duration_ms": duration,
            "trigger": trigger_type,
        }
        if trigger_type == "manual":
            store.log(
                "手动执行任务",
                target=job_id,
                status=status,
                message=message,
                detail={"duration_ms": duration},
            )

    def run_now(self, job_id: str) -> Dict[str, Any]:
        if job_id not in JOB_FUNCS:
            return {"ok": False, "message": "任务不存在"}
        self._run(job_id, trigger_type="manual")
        return {"ok": True, "message": "已执行", "last": self._last_runs.get(job_id, {})}

    # ------------------------------------------------------------------ 查询
    def status(self) -> Dict[str, Any]:
        scheduler = self._scheduler
        running = bool(scheduler and scheduler.running)
        jobs: List[Dict[str, Any]] = []
        jobs_cfg = settings.get("cron.jobs", {}) or {}

        scheduled = {}
        if running and scheduler:
            for job in scheduler.get_jobs():
                scheduled[job.id] = job

        recent = {}
        for row in store.list_task_runs(limit=200):
            recent.setdefault(row["job_id"], row)

        for job_id, func in JOB_FUNCS.items():
            cfg = jobs_cfg.get(job_id) or {}
            job = scheduled.get(job_id)
            last = recent.get(job_id) or self._last_runs.get(job_id)
            next_run = ""
            if job and job.next_run_time:
                next_run = job.next_run_time.strftime("%Y-%m-%d %H:%M:%S")
            jobs.append(
                {
                    "id": job_id,
                    "name": cfg.get("name") or job_id,
                    "description": JOB_DESCRIPTIONS.get(job_id, ""),
                    "enabled": bool(cfg.get("enabled", True)),
                    "cron": cfg.get("cron", ""),
                    "cron_desc": describe(cfg.get("cron", "")),
                    "scheduled": job is not None,
                    "next_run": next_run,
                    "last_run": (last or {}).get("finished_at")
                    or (last or {}).get("started_at", ""),
                    "last_status": (last or {}).get("status", ""),
                    "last_message": (last or {}).get("message", ""),
                    "last_duration_ms": (last or {}).get("duration_ms", 0),
                }
            )

        return {
            "enabled": bool(settings.get("cron.enabled", True)),
            "running": running,
            "started_at": self.started_at,
            "timezone": settings.get("cron.timezone", "Asia/Shanghai"),
            "job_count": len(jobs),
            "running_jobs": len([j for j in jobs if j["scheduled"]]),
            "jobs": jobs,
        }


scheduler_service = SchedulerService()
