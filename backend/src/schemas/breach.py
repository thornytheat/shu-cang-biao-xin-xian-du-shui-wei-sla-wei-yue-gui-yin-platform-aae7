"""违约与探测批次相关模型。"""
from __future__ import annotations

from pydantic import BaseModel, Field


class BreachOut(BaseModel):
    id: int
    table_code: str
    table_name: str
    tier: str
    layer: str
    breach_date: str
    delay_seconds: int
    delay_minutes: float
    tolerance_minutes: int
    ratio: float
    severity: str
    severity_label: str
    consecutive_count: int
    primary_cause: str
    cause_contribution_pct: float
    impacted_reports: int
    impact_score: float
    suggested_action: str
    attribution_detail: dict | None = None
    handling_status: str
    claimed_by: str | None = None
    claimed_at: str | None = None
    resolved_at: str | None = None
    root_note: str | None = None
    report_count: int
    expected_ready_clock: str


class BreachListOut(BaseModel):
    items: list[BreachOut]
    summary: dict


class BreachActionRequest(BaseModel):
    note: str | None = Field(default=None, max_length=600)


class DetectRequest(BaseModel):
    days: int = Field(default=30, ge=7, le=90)
    include_planned: bool = False


class DetectResponse(BaseModel):
    scanned: int
    created: int
    updated: int
    total: int
    budget: dict
    window_days: int
    summary: dict


class ProbeRunItemOut(BaseModel):
    table_code: str
    ordinal: int
    status: str
    attempts: int
    error_message: str | None = None


class ProbeRunResponse(BaseModel):
    """巡检批次（含状态机/断点/发布信息）。进行中与失败时 breaches 为 None。"""

    id: int
    run_date: str
    status: str
    status_label: str
    triggered_by: str
    trigger_source: str
    total_tables: int
    completed_tables: int
    failed_tables: int
    progress_pct: int
    last_table_code: str | None = None
    created_count: int
    missing_count: int
    unknown_count: int
    late_count: int
    red_tables: list[str]
    red_count: int
    duration_ms: int
    fail_reason: str | None = None
    last_error: str | None = None
    heartbeat_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    published_at: str | None = None
    created_at: str | None = None
    items: list[ProbeRunItemOut] | None = None
    # 兼容旧看板字段
    created: int = 0
    updated: int = 0
    total: int = 0
    missing: int = 0
    unknown: int = 0
    late: int = 0
    early: int = 0
    ok: int = 0
    waterline: str | None = None
    idempotent: bool = False
    breaches: dict | None = None


class ProbeRowOut(BaseModel):
    id: int
    table_code: str
    table_name: str
    tier: str
    probe_date: str
    partition_dt: str
    expected_ready_at: str
    actual_ready_at: str | None = None
    delay_minutes: float | None = None
    status: str
    is_early: bool


class ProbeStatusOut(BaseModel):
    today: str
    # 仅当今日批次已发布才为 True（进行中/失败不视为已巡检）
    probed_today: bool
    run: dict | None = None
    latest: dict | None = None
    published_waterline: str | None = None


class ProbeRunListOut(BaseModel):
    items: list[dict]
    total: int
