"""巡检批次全链路集成测试（SQLite 内存库，不依赖 MySQL/Redis）。

覆盖值班诉求：
- 每个业务日独立批次与断点：单表失败只挂起，已完成探测保留；
- 失败后从最后完成位置续跑，done 的表跳过（不重复写探测），failed 的表重试；
- 重复运行不新增批次；已发布后再触发幂等返回；
- 整批校验通过才发布，发布前看板/台账看不到半成品。
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import src.config.database as database
from scripts.demo_config import POLICY_SPECS, TABLES
from src.constants.enums import ProbeItemStatus, RunStatus
from src.models import Base
from src.models.freshness_probe import FreshnessProbe
from src.models.probe_run import ProbeRun
from src.models.probe_run_item import ProbeRunItem
from src.models.sla_policy import SlaPolicy
from src.models.sla_breach import SlaBreach
from src.models.table_asset import TableAsset
from src.services import breach_service, freshness_service, probe_service
from src.utils.timeutil import business_today


@pytest.fixture()
def db_session(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    # service 内部通过 session_scope() 取连接，重定向到同一内存库
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(database, "SessionLocal", session)
    Base.metadata.create_all(engine)

    with session() as db:
        for spec in TABLES:
            db.add(
                TableAsset(
                    code=spec.code,
                    name=spec.name,
                    layer=spec.layer,
                    tier=spec.tier,
                    schedule_cron=spec.cron,
                    expected_ready_clock=spec.ready_clock,
                    upstream_code=spec.upstream,
                    report_count=spec.report_count,
                    owner_team=spec.owner_team,
                    size_gb=spec.size_gb,
                    is_active=True,
                )
            )
        for tier, tolerance, escalate_after, note in POLICY_SPECS:
            db.add(
                SlaPolicy(
                    tier=tier,
                    tolerance_minutes=tolerance,
                    escalate_after=escalate_after,
                    calibrate_days=30,
                    report_owner_note=note,
                )
            )
        db.commit()
    yield session
    monkeypatch.delenv("PROBE_FAIL_TABLES", raising=False)
    Base.metadata.drop_all(engine)


def _today_probe_count(session) -> int:
    with session() as db:
        return len(
            list(
                db.scalars(
                    select(FreshnessProbe).where(FreshnessProbe.probe_date == business_today())
                )
            )
        )


def _run_count(session) -> int:
    with session() as db:
        return len(list(db.scalars(select(ProbeRun))))


def test_full_publish_on_first_clean_run(db_session):
    result = probe_service.run_pipeline(actor_email="ops@freshsla.io", trigger_source="api")
    assert result["status"] == RunStatus.PUBLISHED.value
    assert result["completed_tables"] == len(TABLES)
    assert result["progress_pct"] == 100
    assert result["published_at"] is not None
    assert result["breaches"] is not None
    assert _today_probe_count(db_session) == len(TABLES)
    with db_session() as db:
        items = list(db.scalars(select(ProbeRunItem)))
        assert all(item.status == ProbeItemStatus.DONE.value for item in items)
        board = freshness_service.board(db)
        assert board["waterline"] == business_today().isoformat()
        assert probe_service.status(db)["probed_today"] is True
        # 发布后今日真实探测参与判级：批次红灯数与看板一致（修复"先检测后翻状态"的顺序问题）
        assert result["red_count"] == board["totals"]["red"]
        # 今日违约必须来自真实探测（probe_id 非空），seed 计划值违约（probe_id 空）已清理
        today_breaches = list(
            db.scalars(select(SlaBreach).where(SlaBreach.breach_date == business_today()))
        )
        assert all(breach.probe_id is not None for breach in today_breaches)
        # 台账统计与列表行数一致，且今日已发布不再被隐藏
        assert breach_service._hidden_breach_dates(db) == set()
        assert breach_service.summary(db)["total"] == len(breach_service.query(db))


def test_failure_suspends_and_half_batch_is_hidden(db_session, monkeypatch):
    with db_session() as db:
        ordered = probe_service._active_tables(db)
        fail_code = ordered[5].code
        expected_done = 5
    monkeypatch.setenv("PROBE_FAIL_TABLES", fail_code)

    result = probe_service.run_pipeline(actor_email="ops@freshsla.io", trigger_source="api")
    assert result["status"] == RunStatus.FAILED.value
    assert result["failed_tables"] == 1
    assert result["fail_reason"] and fail_code in result["fail_reason"]

    with db_session() as db:
        run = db.scalar(select(ProbeRun))
        items = list(db.scalars(select(ProbeRunItem).order_by(ProbeRunItem.ordinal)))
        assert run.status == RunStatus.FAILED.value
        assert run.completed_tables == expected_done
        assert sum(1 for i in items if i.status == ProbeItemStatus.DONE.value) == expected_done
        assert [i for i in items if i.status == ProbeItemStatus.FAILED.value][0].table_code == fail_code
        assert sum(1 for i in items if i.status == ProbeItemStatus.PENDING.value) == len(TABLES) - expected_done - 1
        # 断点：最后完成的表
        assert run.last_table_code == ordered[4].code
        # 半成品不上看板、不发布
        assert freshness_service.published_run_dates(db) == set()
        assert freshness_service.board(db)["latest_published"] is None
        assert business_today() in breach_service._hidden_breach_dates(db)
        assert probe_service.status(db)["probed_today"] is False
    # 已完成的探测已落库（保留成果），但只完成了 expected_done 张
    assert _today_probe_count(db_session) == expected_done


def test_resume_from_checkpoint_no_duplicate(db_session, monkeypatch):
    with db_session() as db:
        ordered = probe_service._active_tables(db)
        fail_code = ordered[5].code
    monkeypatch.setenv("PROBE_FAIL_TABLES", fail_code)
    first = probe_service.run_pipeline(actor_email="ops@freshsla.io", trigger_source="api")
    assert first["status"] == RunStatus.FAILED.value
    probes_after_first = _today_probe_count(db_session)
    run_count_after_first = _run_count(db_session)

    # 故障未恢复再触发：仍挂起，但不新增批次、不重复写探测，失败表尝试次数 +1
    second = probe_service.run_pipeline(actor_email="ops@freshsla.io", trigger_source="api")
    assert second["status"] == RunStatus.FAILED.value
    assert _run_count(db_session) == run_count_after_first == 1
    assert _today_probe_count(db_session) == probes_after_first
    with db_session() as db:
        failed_item = db.scalar(select(ProbeRunItem).where(ProbeRunItem.table_code == fail_code))
        assert failed_item.attempts == 2

    # 故障恢复后续跑：剩余表跑完并整批发布
    monkeypatch.delenv("PROBE_FAIL_TABLES")
    third = probe_service.run_pipeline(actor_email="ops@freshsla.io", trigger_source="api")
    assert third["status"] == RunStatus.PUBLISHED.value
    assert third["completed_tables"] == len(TABLES)
    assert _run_count(db_session) == 1  # 始终只有一个批次
    assert _today_probe_count(db_session) == len(TABLES)  # 每表恰好一条
    with db_session() as db:
        assert breach_service._hidden_breach_dates(db) == set()
        assert probe_service.status(db)["probed_today"] is True


def test_rerun_after_publish_is_idempotent(db_session):
    first = probe_service.run_pipeline(actor_email="ops@freshsla.io", trigger_source="api")
    assert first["status"] == RunStatus.PUBLISHED.value
    probe_ids_before = set()
    with db_session() as db:
        probe_ids_before = {row.id for row in db.scalars(select(FreshnessProbe))}

    again = probe_service.run_pipeline(actor_email="ops@freshsla.io", trigger_source="api")
    assert again["status"] == RunStatus.PUBLISHED.value
    assert again.get("idempotent") is True
    assert _run_count(db_session) == 1
    with db_session() as db:
        probe_ids_after = {row.id for row in db.scalars(select(FreshnessProbe))}
    assert probe_ids_after == probe_ids_before  # 没有重复写入探测


def test_publish_clears_stale_planned_breaches(db_session):
    """seed 风格的计划值违约（probe_id NULL）在真实批次发布时必须被真实结果取代。"""
    # 先手工插一条"今日计划违约"，模拟 seed 的 include_planned 产物
    with db_session() as db:
        table = db.scalar(select(TableAsset))
        db.add(
            SlaBreach(
                table_id=table.id,
                probe_id=None,
                breach_date=business_today(),
                delay_seconds=9999,
                tolerance_minutes=60,
                ratio=2.77,
                severity="severe",
                consecutive_count=1,
                primary_cause="undetermined",
                cause_contribution_pct=0.0,
                impacted_reports=table.report_count,
                impact_score=10.0,
                suggested_action="",
            )
        )
        db.commit()

    result = probe_service.run_pipeline(actor_email="ops@freshsla.io", trigger_source="api")
    assert result["status"] == RunStatus.PUBLISHED.value
    with db_session() as db:
        stale = list(
            db.scalars(
                select(SlaBreach).where(
                    SlaBreach.breach_date == business_today(),
                    SlaBreach.probe_id.is_(None),
                )
            )
        )
        assert stale == []  # 计划值违约已被清理，台账只留真实探测算出的结果


def test_run_history_and_item_detail(db_session):
    probe_service.run_pipeline(actor_email="ops@freshsla.io", trigger_source="beat")
    with db_session() as db:
        runs = probe_service.list_runs(db)
        assert len(runs) == 1
        detail = probe_service.serialize_run(runs[0], detail=True, db=db)
        assert detail["trigger_source"] == "beat"
        assert len(detail["items"]) == len(TABLES)
        assert all(item["status"] == ProbeItemStatus.DONE.value for item in detail["items"])
        # 控制器读取单批次
        fetched = probe_service.get_run(db, runs[0].id)
        assert fetched.id == runs[0].id


def test_checkpoint_survives_process_restart(tmp_path, monkeypatch):
    """断点逐表提交：模拟进程崩溃重开（全新引擎指向同一库）后，已完成成果不丢、可续跑。"""
    db_file = tmp_path / "restart.db"
    engine = create_engine(f"sqlite:///{db_file}")
    session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(database, "SessionLocal", session)
    Base.metadata.create_all(engine)

    with session() as db:
        for spec in TABLES:
            db.add(
                TableAsset(
                    code=spec.code, name=spec.name, layer=spec.layer, tier=spec.tier,
                    schedule_cron=spec.cron, expected_ready_clock=spec.ready_clock,
                    upstream_code=spec.upstream, report_count=spec.report_count,
                    owner_team=spec.owner_team, size_gb=spec.size_gb, is_active=True,
                )
            )
        for tier, tolerance, escalate_after, _note in POLICY_SPECS:
            db.add(SlaPolicy(tier=tier, tolerance_minutes=tolerance, escalate_after=escalate_after, calibrate_days=30))
        db.commit()

    with session() as db:
        fail_code = probe_service._active_tables(db)[8].code
    monkeypatch.setenv("PROBE_FAIL_TABLES", fail_code)
    first = probe_service.run_pipeline(actor_email="ops@freshsla.io", trigger_source="api")
    assert first["status"] == RunStatus.FAILED.value
    done_count = first["completed_tables"]

    # 进程崩溃：丢弃引擎/连接池，用全新引擎重连同一文件库
    engine.dispose()
    engine2 = create_engine(f"sqlite:///{db_file}")
    session2 = sessionmaker(bind=engine2, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(database, "engine", engine2)
    monkeypatch.setattr(database, "SessionLocal", session2)

    with session2() as db:
        run = db.scalar(select(ProbeRun))
        assert run.status == RunStatus.FAILED.value
        assert run.completed_tables == done_count
        probes = len(list(db.scalars(select(FreshnessProbe).where(FreshnessProbe.probe_date == business_today()))))
        done_items = len(list(db.scalars(select(ProbeRunItem).where(ProbeRunItem.status == ProbeItemStatus.DONE.value))))
        assert probes == done_items == done_count

    # 恢复后续跑：同一批次跑满并发布，无重复探测
    monkeypatch.delenv("PROBE_FAIL_TABLES")
    resumed = probe_service.run_pipeline(actor_email="ops@freshsla.io", trigger_source="api")
    assert resumed["status"] == RunStatus.PUBLISHED.value
    assert resumed["completed_tables"] == len(TABLES)
    with session2() as db:
        assert len(list(db.scalars(select(ProbeRun)))) == 1
        assert _today_probe_count(session2) == len(TABLES)
    engine2.dispose()
