"""ProbeRun：巡检批次（每个业务日至多一条），承载独立运行状态、断点与发布闸门。

状态机（RunStatus）：
  running   批次进行中：按表逐条采集，断点落在 probe_run_items；采集失败/超时 → failed
  failed    批次挂起：已完成的探测保留，再次触发从最后完成位置续跑
  published 整批校验通过后一次性发布，看板/详情/台账只展示已发布批次的数据

重复触发同一业务日不会新增批次：running/failed 复用同一行续跑，published 直接幂等返回。
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import JSON, Date, DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from src.constants.enums import RunStatus
from src.models.base import Base


class ProbeRun(Base):
    __tablename__ = "probe_runs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    run_date: Mapped[date] = mapped_column(Date, nullable=False, unique=True, index=True)
    status: Mapped[str] = mapped_column(
        String(12),
        nullable=False,
        default=RunStatus.RUNNING.value,
        index=True,
        comment="running/published/failed",
    )
    triggered_by: Mapped[str] = mapped_column(String(160), nullable=False)
    trigger_source: Mapped[str] = mapped_column(String(16), nullable=False, default="api", comment="api/beat/seed")
    total_tables: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="本批应采集表数")
    completed_tables: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="断点：已完成（done）表数")
    failed_tables: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="失败表数，续跑时重试")
    last_table_code: Mapped[str | None] = mapped_column(String(80), nullable=True, comment="最后完成的表（断点位置）")
    created_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    missing_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    unknown_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    late_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    red_tables: Mapped[list | None] = mapped_column(JSON, nullable=True)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    fail_reason: Mapped[str | None] = mapped_column(String(255), nullable=True, comment="最近一次失败原因")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True, comment="失败明细（含表 code / 异常）")
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="最近一次断点心跳")
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="首次开始时间")
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="发布时间")
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="整批发布时间")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )
