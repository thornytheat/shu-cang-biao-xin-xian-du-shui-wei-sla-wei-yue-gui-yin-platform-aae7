"""探测服务：以"批次 + 断点"执行每日新鲜度巡检。

核心约定（对应值班诉求）：
1. 每个业务日至多一个 ProbeRun，重复触发不新增批次：
   running/failed → 复用同一批次从断点续跑；published → 幂等返回；
2. 单表采集逐条提交、状态落在 probe_run_items，单表超时/异常只挂起该表与整批，
   已完成的探测全部保留，再次触发从最后完成位置续跑，done 的表直接跳过（不重复写入）；
3. 整批校验通过后才统一判级、检测违约并把批次置为 published，
   看板 / 表详情 / 违约台账只读取已发布批次的数据，半成品不会提前上看板。
"""
from __future__ import annotations

import os
import time
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.config.database import session_scope
from src.config.settings import settings
from src.constants.enums import Grade, ProbeItemStatus, ProbeStatus, RunStatus
from src.constants.limits import RUN_HEARTBEAT_EVERY, RUN_HISTORY_LIMIT
from src.models.freshness_probe import FreshnessProbe
from src.models.probe_run import ProbeRun
from src.models.probe_run_item import ProbeRunItem
from src.models.table_asset import TableAsset
from src.services import fixture_source, run_core
from src.utils.errors import ProbeCollectionError
from src.utils.logger import get_logger
from src.utils.timeutil import business_now, business_today

logger = get_logger("probe")


# --------------------------------------------------------------------------- #
# 故障注入（仅排障/演练/测试用）：PROBE_FAIL_TABLES="dwd.a,dws.b" 让这些表采集失败，
# 模拟"单表采集超时中断整批"，验证断点续跑。生产默认空串、不生效。
# 直接读进程环境变量，便于测试用 monkeypatch 临时开关。
# --------------------------------------------------------------------------- #
def _fail_tables() -> set[str]:
    raw = os.getenv("PROBE_FAIL_TABLES", settings.probe_fail_tables)
    return {part.strip() for part in raw.split(",") if part.strip()}


# --------------------------------------------------------------------------- #
# 单表采集（幂等 upsert，天然不重复写入探测）
# --------------------------------------------------------------------------- #
def collect_one(db: Session, table: TableAsset, target: date, offset: int) -> dict:
    """采集一张表并 upsert 到 freshness_probes，返回状态计数所需的精简结果。"""
    if table.code in _fail_tables():
        raise ProbeCollectionError(
            table.code, f"采集超时（>{settings.probe_table_timeout_seconds}s）"
        )

    values = fixture_source.build_probe_values(fixture_source.spec_of(table.code), target, offset)
    existing = db.scalar(
        select(FreshnessProbe).where(
            FreshnessProbe.table_id == table.id,
            FreshnessProbe.probe_date == target,
        )
    )
    payload = {
        "expected_ready_at": values.expected_ready_at,
        "actual_ready_at": values.actual_ready_at,
        "delay_seconds": values.delay_seconds,
        "raw_offset_seconds": values.raw_offset_seconds,
        "is_early": values.is_early,
        "status": values.status,
        "note": values.note,
    }
    if existing is None:
        db.add(
            FreshnessProbe(
                table_id=table.id,
                probe_date=target,
                partition_dt=target.isoformat(),
                source="probe",
                **payload,
            )
        )
        created = 1
    else:
        for key, value in payload.items():
            setattr(existing, key, value)
        existing.source = "probe"
        created = 0
    return {"table_code": table.code, "status": values.status, "created": created}


def _active_tables(db: Session) -> list[TableAsset]:
    return list(
        db.scalars(select(TableAsset).where(TableAsset.is_active.is_(True)).order_by(TableAsset.code))
    )


# --------------------------------------------------------------------------- #
# 批次初始化 / 接管（幂等：同日不新增批次）
# --------------------------------------------------------------------------- #
def _get_or_init_run(db: Session, target: date, actor_email: str, trigger_source: str) -> tuple[ProbeRun, bool]:
    run_row = db.scalar(select(ProbeRun).where(ProbeRun.run_date == target))
    now = business_now()
    if run_row is not None:
        # 已发布 → 幂等返回，不新增批次、不重复采集
        if run_row.status == RunStatus.PUBLISHED.value:
            return run_row, False
        # running 但心跳陈旧（worker 崩溃）或 failed → 接管续跑
        if run_core.can_take_over(run_row.status, run_row.heartbeat_at, now, settings.run_stale_seconds):
            run_row.status = RunStatus.RUNNING.value
            run_row.triggered_by = actor_email
            run_row.trigger_source = trigger_source
            run_row.heartbeat_at = now
            run_row.fail_reason = None
            db.flush()
            return run_row, True
        # 健康的 running → 不允许并发再开一批
        from src.utils.errors import AppError

        raise AppError("PROBE_ALREADY_RUNNING", 409, detail={"run_date": target.isoformat()})

    tables = _active_tables(db)
    run_row = ProbeRun(
        run_date=target,
        status=RunStatus.RUNNING.value,
        triggered_by=actor_email,
        trigger_source=trigger_source,
        total_tables=len(tables),
        completed_tables=0,
        failed_tables=0,
        heartbeat_at=now,
        started_at=now,
    )
    db.add(run_row)
    db.flush()
    for ordinal, table in enumerate(tables):
        db.add(
            ProbeRunItem(
                run_id=run_row.id,
                table_id=table.id,
                table_code=table.code,
                ordinal=ordinal,
                status=ProbeItemStatus.PENDING.value,
            )
        )
    db.flush()
    return run_row, True


# --------------------------------------------------------------------------- #
# 断点续跑主体：只处理 pending/failed 明细，逐条提交断点
# --------------------------------------------------------------------------- #
def process_pending(db: Session, run_row: ProbeRun) -> dict:
    """推进一个批次：从断点继续采集，直到全部完成或某张表失败而挂起。"""
    target = run_row.run_date
    today = business_today()
    offset = (target - today).days
    tables = {table.id: table for table in _active_tables(db)}

    items = list(
        db.scalars(
            select(ProbeRunItem)
            .where(ProbeRunItem.run_id == run_row.id)
            .order_by(ProbeRunItem.ordinal)
        )
    )
    pending_ordinals = run_core.next_pending_ordinals(
        [
            {"ordinal": item.ordinal, "status": item.status, "table_code": item.table_code}
            for item in items
        ]
    )
    item_by_ordinal = {item.ordinal: item for item in items}

    processed = 0
    batch_created = 0
    failure: dict | None = None

    for ordinal in pending_ordinals:
        item = item_by_ordinal[ordinal]
        table = tables.get(item.table_id)
        if table is None:
            item.status = ProbeItemStatus.FAILED.value
            item.error_message = "表资产已停用或删除"
            failure = {"table_code": item.table_code, "reason": item.error_message}
            _checkpoint(db, run_row)  # 失败状态也要落盘
            break
        item.attempts += 1
        item.started_at = business_now()
        try:
            result = collect_one(db, table, target, offset)
            item.status = ProbeItemStatus.DONE.value
            item.error_message = None
            item.finished_at = business_now()
            batch_created += result["created"]
        except ProbeCollectionError as exc:
            item.status = ProbeItemStatus.FAILED.value
            item.error_message = exc.reason
            item.finished_at = business_now()
            failure = {"table_code": exc.table_code, "reason": exc.reason}
            logger.warning("[PROBE] table %s failed, run %s suspended: %s", exc.table_code, run_row.id, exc.reason)
            break
        except Exception as exc:  # noqa: BLE001 - 单表异常不能炸掉整批已完成成果
            item.status = ProbeItemStatus.FAILED.value
            item.error_message = str(exc)[:255]
            item.finished_at = business_now()
            failure = {"table_code": item.table_code, "reason": str(exc)[:255]}
            logger.exception("[PROBE] table %s unexpected error, run %s suspended", item.table_code, run_row.id)
            break
        finally:
            # 每张表都提交断点（探测 + 明细状态 + 心跳）：进程在此之后崩溃，已完成成果不丢，续跑从下一张继续
            _checkpoint(db, run_row)

        processed += 1
        if processed % RUN_HEARTBEAT_EVERY == 0:
            logger.info("[PROBE] run %s progress %s/%s", run_row.id, processed, len(pending_ordinals))

    progress = run_core.recompute_progress(
        [
            {
                "ordinal": item.ordinal,
                "status": item.status,
                "table_code": item.table_code,
            }
            for item in db.scalars(
                select(ProbeRunItem).where(ProbeRunItem.run_id == run_row.id)
            )
        ]
    )
    run_row.completed_tables = progress["completed"]
    run_row.failed_tables = progress["failed"]
    run_row.last_table_code = progress["last_table_code"]
    run_row.created_count = run_row.created_count + batch_created

    if failure is not None:
        run_row.status = RunStatus.FAILED.value
        run_row.fail_reason = f"{failure['table_code']}: {failure['reason']}"
        run_row.last_error = f"table={failure['table_code']} reason={failure['reason']} at {business_now():%Y-%m-%d %H:%M:%S}"
        db.commit()
        return {"advanced": processed, "failure": failure, "finished": False}
    db.commit()
    return {"advanced": processed, "failure": None, "finished": progress["pending"] == 0 and progress["failed"] == 0}


def _checkpoint(db: Session, run_row: ProbeRun) -> None:
    """提交当前断点：刷新心跳并把本张表的探测/明细状态持久化（抗进程崩溃）。"""
    run_row.heartbeat_at = business_now()
    db.commit()


# --------------------------------------------------------------------------- #
# 发布闸门：整批校验 → 判级 → 违约检测 → 一次性发布
# --------------------------------------------------------------------------- #
def _red_tables(db: Session, tables: list[TableAsset], target: date) -> list[str]:
    """发布前即时判级：只针对本次巡检日，取当日延迟与该表基线分位比较。"""
    from src.services import freshness_service

    tolerances = freshness_service.policy_map(db)
    reds: list[str] = []
    for table in tables:
        probes = freshness_service.probes_for_table(db, table.id)
        probes = [probe for probe in probes if probe.probe_date <= target]
        payload = freshness_service.evaluate(probes, tolerances.get(table.tier, 60))
        if payload["grade"] == Grade.RED.value:
            reds.append(table.code)
    return sorted(reds)


def finalize_and_publish(db: Session, run_row: ProbeRun) -> dict:
    """整批校验通过后：置为已发布 → 判级 → 清理计划值违约 → 用真实探测检测违约。

    顺序很关键：看板判级（probes_for_table）与违约扫描（scan）都带"仅已发布批次日可见"
    的闸门，必须先把本批置为 published，今日真实探测才会参与判级与违约计算。
    整个过程在同一事务内，任一步异常都会整体回滚，不会出现"半发布"。
    """
    from src.services import breach_service

    target = run_row.run_date
    tables = _active_tables(db)
    items = list(db.scalars(select(ProbeRunItem).where(ProbeRunItem.run_id == run_row.id)))
    item_dicts = [
        {"ordinal": item.ordinal, "status": item.status, "table_code": item.table_code} for item in items
    ]
    probes_today = list(
        db.scalars(select(FreshnessProbe).where(FreshnessProbe.probe_date == target))
    )
    ok, reason = run_core.validate_complete(
        item_dicts, probe_count=len(probes_today), total_tables=len(tables)
    )
    if not ok:
        run_row.status = RunStatus.FAILED.value
        run_row.fail_reason = reason
        db.flush()
        from src.utils.errors import AppError

        raise AppError("PROBE_TABLE_TIMEOUT", 409, detail={"reason": reason, "run_id": run_row.id})

    counters = run_core.aggregate_item_results(
        [{"status": probe.status} for probe in probes_today]
    )

    # 发布前清掉 seed 用 fixture 计划值生成的"今日计划违约"（probe_id IS NULL），
    # 避免探测真实结果落地后台账仍残留计划值，随后用真实探测统一重算。
    from src.models.sla_breach import SlaBreach

    planned = db.scalars(
        select(SlaBreach).where(SlaBreach.breach_date == target, SlaBreach.probe_id.is_(None))
    ).all()
    for stale in planned:
        db.delete(stale)

    # 先翻状态：此后今日成为"已发布批次日"，下方判级与违约扫描才能读到今日真实探测。
    # 与后续写入同一事务，失败整体回滚，不会留下已发布但数据不全的批次。
    now = business_now()
    run_row.status = RunStatus.PUBLISHED.value
    run_row.finished_at = now
    run_row.published_at = now
    run_row.fail_reason = None
    run_row.last_error = None
    db.flush()

    red_tables = _red_tables(db, tables, target)
    detection = breach_service.detect(db)

    run_row.missing_count = counters[ProbeStatus.MISSING.value]
    run_row.unknown_count = counters[ProbeStatus.MISSING.value]
    run_row.late_count = counters[ProbeStatus.LATE.value]
    run_row.red_tables = red_tables
    db.flush()

    logger.info(
        "[PROBE] run %s published date=%s tables=%s missing=%s late=%s red=%s",
        run_row.id, target, len(tables), counters["missing"], counters["late"], len(red_tables),
    )
    return {"counters": counters, "red_tables": red_tables, "detection": detection}


# --------------------------------------------------------------------------- #
# 对外编排：controller / celery 共用，内部自管事务
# --------------------------------------------------------------------------- #
def run_pipeline(*, actor_email: str, run_date: date | None = None, trigger_source: str = "api") -> dict:
    """一次触发的完整流程：初始化/接管 → 断点续跑 →（完成则）整批发布。

    每个阶段独立事务提交：单表失败时断点已落库，进程退出也能从最后完成位置续跑。
    并发触发同一业务日时，唯一约束会把后来者挡在 PROBE_ALREADY_RUNNING，绝不新增第二个批次。
    """
    from sqlalchemy.exc import IntegrityError

    from src.utils.errors import AppError

    target = run_date or business_today()
    started = time.time()

    try:
        with session_scope() as db:
            run_row, acquired = _get_or_init_run(db, target, actor_email, trigger_source)
            if not acquired and run_row.status == RunStatus.PUBLISHED.value:
                return serialize_run(run_row, detail=True, db=db) | {"idempotent": True, "breaches": None}
            run_id = run_row.id
    except IntegrityError as exc:  # 同业务日批次唯一约束冲突 = 已有另一个批次在跑
        raise AppError(
            "PROBE_ALREADY_RUNNING", 409, detail={"run_date": target.isoformat()}
        ) from exc

    failure = None
    finished = False
    with session_scope() as db:
        run_row = db.get(ProbeRun, run_id)
        progress = process_pending(db, run_row)
        failure = progress["failure"]
        finished = progress["finished"]

    if finished and failure is None:
        with session_scope() as db:
            run_row = db.get(ProbeRun, run_id)
            publish = finalize_and_publish(db, run_row)
            run_row.duration_ms = int((time.time() - started) * 1000)
            db.flush()
            from src.services import breach_service

            publish["detection"]["summary"] = breach_service.summary(db)
            run_payload = serialize_run(run_row, detail=True, db=db)
        return run_payload | {"idempotent": False, "breaches": publish["detection"]}

    with session_scope() as db:
        run_row = db.get(ProbeRun, run_id)
        run_row.duration_ms = int((time.time() - started) * 1000)
        db.flush()
        payload = serialize_run(run_row, detail=True, db=db)
    return payload | {"idempotent": False, "breaches": None}


# --------------------------------------------------------------------------- #
# 序列化 / 查询
# --------------------------------------------------------------------------- #
def _run_items(db: Session, run_id: int) -> list[ProbeRunItem]:
    return list(
        db.scalars(
            select(ProbeRunItem).where(ProbeRunItem.run_id == run_id).order_by(ProbeRunItem.ordinal)
        )
    )


def serialize_run(run_row: ProbeRun, *, detail: bool = False, db: Session | None = None) -> dict:
    total = run_row.total_tables or 0
    completed = run_row.completed_tables or 0
    failed = run_row.failed_tables or 0
    payload = {
        "id": run_row.id,
        "run_date": run_row.run_date.isoformat(),
        "status": run_row.status,
        "status_label": _run_status_label(run_row.status),
        "triggered_by": run_row.triggered_by,
        "trigger_source": run_row.trigger_source,
        "total_tables": total,
        "completed_tables": completed,
        "failed_tables": failed,
        "progress_pct": run_core.pct(completed, total),
        "last_table_code": run_row.last_table_code,
        "created_count": run_row.created_count,
        "missing_count": run_row.missing_count,
        "unknown_count": run_row.unknown_count,
        "late_count": run_row.late_count,
        "red_tables": run_row.red_tables or [],
        "red_count": len(run_row.red_tables or []),
        "duration_ms": run_row.duration_ms,
        "fail_reason": run_row.fail_reason,
        "heartbeat_at": run_row.heartbeat_at.isoformat(sep=" ") if run_row.heartbeat_at else None,
        "started_at": run_row.started_at.isoformat(sep=" ") if run_row.started_at else None,
        "finished_at": run_row.finished_at.isoformat(sep=" ") if run_row.finished_at else None,
        "published_at": run_row.published_at.isoformat(sep=" ") if run_row.published_at else None,
        "created_at": run_row.created_at.isoformat(sep=" ") if run_row.created_at else None,
        # 兼容旧前端字段
        "created": run_row.created_count,
        "updated": 0,
        "total": completed,
        "missing": run_row.missing_count,
        "unknown": run_row.unknown_count,
        "late": run_row.late_count,
        "early": 0,
        "ok": max(0, completed - run_row.missing_count - run_row.late_count),
        "waterline": run_row.run_date.isoformat() if run_row.status == RunStatus.PUBLISHED.value else None,
    }
    if detail and db is not None:
        items = _run_items(db, run_row.id)
        payload["items"] = [
            {
                "table_code": item.table_code,
                "ordinal": item.ordinal,
                "status": item.status,
                "attempts": item.attempts,
                "error_message": item.error_message,
            }
            for item in items
        ]
        payload["last_error"] = run_row.last_error
    return payload


def _run_status_label(status: str) -> str:
    from src.constants.enums import RUN_STATUS_LABELS

    return RUN_STATUS_LABELS.get(status, status)


def get_run(db: Session, run_id: int) -> ProbeRun:
    run_row = db.get(ProbeRun, run_id)
    if run_row is None:
        from src.utils.errors import AppError

        raise AppError("RUN_NOT_FOUND", 404, detail={"run_id": run_id})
    return run_row


def list_runs(db: Session, *, limit: int = RUN_HISTORY_LIMIT) -> list[ProbeRun]:
    limit = max(1, min(limit, RUN_HISTORY_LIMIT))
    return list(
        db.scalars(select(ProbeRun).order_by(ProbeRun.run_date.desc(), ProbeRun.id.desc()).limit(limit))
    )


def latest_run(db: Session) -> ProbeRun | None:
    return db.scalar(select(ProbeRun).order_by(ProbeRun.run_date.desc(), ProbeRun.id.desc()).limit(1))


def latest_published_date(db: Session) -> date | None:
    row = db.scalar(
        select(ProbeRun.run_date)
        .where(ProbeRun.status == RunStatus.PUBLISHED.value)
        .order_by(ProbeRun.run_date.desc())
        .limit(1)
    )
    return row


def status(db: Session) -> dict:
    today = business_today()
    run_row = db.scalar(select(ProbeRun).where(ProbeRun.run_date == today))
    latest = latest_run(db)
    published_waterline = latest_published_date(db)
    return {
        "today": today.isoformat(),
        # 只有整批发布才算"今日已巡检"，进行中/失败不会被值班当成已恢复
        "probed_today": bool(run_row and run_row.status == RunStatus.PUBLISHED.value),
        "run": serialize_run(run_row, detail=True, db=db) if run_row else None,
        "latest": serialize_run(latest) if latest else None,
        "published_waterline": published_waterline.isoformat() if published_waterline else None,
    }


def recent_probes(db: Session, *, table_code: str | None = None, days: int = 14) -> list[dict]:
    """探测记录列表：只返回已发布批次覆盖日期的数据，避免半成品提前可见。"""
    from src.services.freshness_service import visible_probe_dates

    stmt = (
        select(FreshnessProbe, TableAsset)
        .join(TableAsset, TableAsset.id == FreshnessProbe.table_id)
        .order_by(FreshnessProbe.probe_date.desc(), TableAsset.code)
    )
    if table_code:
        stmt = stmt.where(TableAsset.code == table_code)
    today = business_today()
    stmt = stmt.where(FreshnessProbe.probe_date >= today - timedelta(days=days))
    visible = visible_probe_dates(db)
    rows = []
    for probe, table in db.execute(stmt.limit(400)).all():
        # seed 历史（source=seed）始终可见；probe 来源要求其业务日批次已发布
        if probe.source != "seed" and probe.probe_date not in visible:
            continue
        rows.append(
            {
                "id": probe.id,
                "table_code": table.code,
                "table_name": table.name,
                "tier": table.tier,
                "probe_date": probe.probe_date.isoformat(),
                "partition_dt": probe.partition_dt,
                "expected_ready_at": probe.expected_ready_at.isoformat(sep=" "),
                "actual_ready_at": probe.actual_ready_at.isoformat(sep=" ") if probe.actual_ready_at else None,
                "delay_minutes": round(probe.delay_seconds / 60.0, 1) if probe.delay_seconds is not None else None,
                "status": probe.status,
                "is_early": probe.is_early,
            }
        )
    return rows[:200]
