"""Celery 任务：心跳（供 worker healthcheck）+ 每日自动巡检 + 违约检测。"""
from __future__ import annotations

import time

from src.config.celery_app import (
    TASK_DAILY_PROBE,
    TASK_DETECT_BREACHES,
    TASK_HEARTBEAT,
    celery_app,
)
from src.config.settings import settings
from src.utils.logger import get_logger

logger = get_logger("tasks")
HEARTBEAT_KEY = "freshsla:worker:heartbeat"


@celery_app.task(name=TASK_HEARTBEAT)
def heartbeat() -> dict:
    import redis

    client = redis.Redis.from_url(settings.redis_url, decode_responses=True)
    client.set(HEARTBEAT_KEY, str(time.time()), ex=300)
    return {"heartbeat": time.time()}


@celery_app.task(
    name=TASK_DAILY_PROBE,
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=30,
    retry_backoff_max=300,
    retry_jitter=True,
    max_retries=3,
)
def daily_probe(self) -> dict:
    """每天 08:10 自动巡检：与 API 触发同一套批次编排。

    单表失败只挂起批次、不抛断任务；整批发布失败/未知异常才触发 Celery 重试，
    重试复用同一批次从断点续跑（不会新增批次、不会重复写探测）。
    """
    from src.constants.enums import RunStatus
    from src.services import probe_service

    result = probe_service.run_pipeline(
        actor_email="celery@freshsla.io", trigger_source="beat"
    )
    status = result.get("status")
    if status == RunStatus.PUBLISHED.value:
        logger.info(
            "[BEAT] probe %s published tables=%s red=%s",
            result["run_date"], result["completed_tables"], result["red_count"],
        )
        return {"probe": result}
    if status == RunStatus.FAILED.value:
        # 断点已落库：主动重试，续跑剩余表；超过重试上限后保持 failed 等人工触发
        logger.warning("[BEAT] probe %s suspended at %s: %s",
                       result["run_date"], result.get("last_table_code"), result.get("fail_reason"))
        raise self.retry(exc=RuntimeError(result.get("fail_reason") or "probe run failed"))
    logger.info("[BEAT] probe %s still running: %s/%s",
                result["run_date"], result["completed_tables"], result["total_tables"])
    return {"probe": result}


@celery_app.task(name=TASK_DETECT_BREACHES)
def detect_breaches(days: int = 30) -> dict:
    from src.config.database import session_scope
    from src.services import breach_service

    with session_scope() as db:
        return breach_service.detect(db, days=days)
