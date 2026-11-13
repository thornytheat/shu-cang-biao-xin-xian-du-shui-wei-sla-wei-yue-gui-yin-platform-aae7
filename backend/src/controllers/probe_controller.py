"""控制器：探测批次运行（断点续跑 / 发布闸门）、批次状态与探测记录。"""
from __future__ import annotations

from fastapi import Depends, Query, Request
from sqlalchemy.orm import Session

from src.config.database import get_db
from src.constants.roles import PROBE_RUN_ROLES
from src.middlewares.rbac import principal_of, require_roles
from src.services import audit_service, probe_service


def run_probes(
    request: Request,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*PROBE_RUN_ROLES)),
):
    """触发/续跑今日巡检。

    幂等：同日重复调用不新增批次——进行中/失败从断点续跑，已发布直接返回原批次。
    批次内部自管事务，这里只在调用结束后补一条审计并提交。
    """
    result = probe_service.run_pipeline(
        actor_email=principal["email"], trigger_source="api"
    )
    status = result.get("status")
    if status == "published":
        message = (
            f"巡检 {result['run_date']} 已发布：完成 {result['completed_tables']}/{result['total_tables']}，"
            f"缺失 {result['missing_count']} 条，红灯 {result['red_count']} 张"
        )
    elif status == "failed":
        message = (
            f"巡检 {result['run_date']} 挂起于 {result.get('last_table_code') or '—'}："
            f"{result.get('fail_reason') or '采集失败'}，可再次触发从断点续跑"
        )
    else:
        message = f"巡检 {result['run_date']} 进行中：{result['completed_tables']}/{result['total_tables']}"
    audit_service.record(
        db,
        actor_email=principal["email"],
        actor_role=principal["role"],
        action="probe.run",
        entity="probe_run",
        entity_id=result["run_date"],
        detail={
            "run_id": result.get("id"),
            "status": status,
            "completed": result.get("completed_tables"),
            "total": result.get("total_tables"),
            "failed": result.get("failed_tables"),
            "idempotent": result.get("idempotent", False),
        },
        ip=request.client.host if request.client else None,
        message=message,
    )
    db.commit()
    return result


def probe_status(db: Session = Depends(get_db), principal: dict = Depends(principal_of)):
    return probe_service.status(db)


def list_runs(
    limit: int = Query(default=20, ge=1, le=30),
    db: Session = Depends(get_db),
    principal: dict = Depends(principal_of),
):
    rows = probe_service.list_runs(db, limit=limit)
    return {
        "items": [probe_service.serialize_run(row) for row in rows],
        "total": len(rows),
    }


def get_run(
    run_id: int,
    db: Session = Depends(get_db),
    principal: dict = Depends(principal_of),
):
    run_row = probe_service.get_run(db, run_id)
    return probe_service.serialize_run(run_row, detail=True, db=db)


def list_probes(
    table: str | None = Query(default=None, max_length=80),
    days: int = Query(default=14, ge=1, le=60),
    db: Session = Depends(get_db),
    principal: dict = Depends(principal_of),
):
    rows = probe_service.recent_probes(db, table_code=table, days=days)
    return {"items": rows, "total": len(rows)}
