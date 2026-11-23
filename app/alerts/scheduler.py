"""Runs the risk-change check on a schedule inside the API process (APScheduler).

A background thread in the web server is enough for one small deployment. With several
server replicas you'd move this to one external cron job (`python -m app.alerts.monitor`),
so the check doesn't run once per replica.
"""

from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.alerts.monitor import scheduled_check
from app.config import get_settings

log = logging.getLogger("wria.alerts")


def start_scheduler() -> BackgroundScheduler | None:
    cron = get_settings().alert_schedule_cron.strip()
    if not cron:
        log.info("alert scheduler disabled (ALERT_SCHEDULE_CRON is empty)")
        return None
    scheduler = BackgroundScheduler(daemon=True)
    # coalesce + max_instances=1: if the server was asleep and missed runs, do one, not many.
    scheduler.add_job(
        scheduled_check,
        CronTrigger.from_crontab(cron),
        id="risk_check",
        coalesce=True,
        max_instances=1,
    )
    scheduler.start()
    log.info("alert scheduler started: '%s'", cron)
    return scheduler
