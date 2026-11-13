"""ProbeRunItem：批次内单表采集明细，是断点续跑与"不重复写入探测"的落点。

自然键 (run_id, table_id)：
  pending 未采集（续跑从这里继续）
  done    已采集且 FreshnessProbe 已 upsert，续跑直接跳过 → 不重复写入
  failed  采集超时/异常，续跑重试
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from src.constants.enums import ProbeItemStatus
from src.models.base import Base


class ProbeRunItem(Base):
    __tablename__ = "probe_run_items"
    __table_args__ = (
        UniqueConstraint("run_id", "table_id", name="uq_run_item_table"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("probe_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    table_id: Mapped[int] = mapped_column(
        ForeignKey("table_assets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    table_code: Mapped[str] = mapped_column(String(80), nullable=False, comment="冗余 code，便于排障展示")
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="批次内顺序，从 0 开始")
    status: Mapped[str] = mapped_column(
        String(12),
        nullable=False,
        default=ProbeItemStatus.PENDING.value,
        index=True,
        comment="pending/done/failed",
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="采集尝试次数")
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )
