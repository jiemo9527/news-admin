from __future__ import annotations

import importlib
from typing import Any

from .repository import list_schedules
from .service import execute_pipeline

_scheduler: Any | None = None


def get_scheduler() -> Any:
    global _scheduler
    if _scheduler is None:
        scheduler_module = importlib.import_module('apscheduler.schedulers.background')
        _scheduler = scheduler_module.BackgroundScheduler()
    return _scheduler


def sync_scheduler() -> None:
    scheduler = get_scheduler()
    cron_module = importlib.import_module('apscheduler.triggers.cron')
    CronTrigger = cron_module.CronTrigger
    existing = {job.id for job in scheduler.get_jobs()}
    wanted: set[str] = set()
    for schedule in list_schedules():
        job_id = f'schedule-{schedule["id"]}'
        wanted.add(job_id)
        if not schedule['enabled']:
            if job_id in existing:
                scheduler.remove_job(job_id)
            continue
        trigger = CronTrigger.from_crontab(schedule['cron_expr'], timezone=schedule['timezone'])
        scheduler.add_job(execute_pipeline, trigger=trigger, id=job_id, replace_existing=True, kwargs={'trigger_kind': f'schedule:{schedule["id"]}'}, max_instances=1, coalesce=True)
    for job_id in existing - wanted:
        scheduler.remove_job(job_id)


def start_scheduler() -> None:
    scheduler = get_scheduler()
    if not scheduler.running:
        scheduler.start()
    sync_scheduler()
