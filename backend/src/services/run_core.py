"""巡检批次编排的纯函数内核（断点续跑 / 发布闸门）。

与 freshness_core / breach_core 同一约束：
- 只吃 dict / 基本类型，不 import ORM、Session、数据库配置；
- 批次进度推进、陈旧判定、整批校验、汇总口径集中在此，便于单测复核；
- probe_service 只负责取数、落库与事务，把状态推进交给这里。
"""
from __future__ import annotations

from datetime import datetime

from src.constants.enums import ProbeItemStatus, RunStatus


def is_stale(heartbeat_at: datetime | None, now: datetime, stale_seconds: int) -> bool:
    """running 批次超过 stale_seconds 没有心跳 → worker 已崩溃，可被安全接管。"""
    if heartbeat_at is None:
        return True
    return (now - heartbeat_at).total_seconds() > stale_seconds


def can_take_over(status: str, heartbeat_at: datetime | None, now: datetime, stale_seconds: int) -> bool:
    """只有 running 且心跳陈旧、或 failed 的批次才能被新一次触发接管。"""
    if status == RunStatus.FAILED.value:
        return True
    if status == RunStatus.RUNNING.value:
        return is_stale(heartbeat_at, now, stale_seconds)
    return False


def next_pending_ordinals(items: list[dict]) -> list[int]:
    """返回需要采集的明细序号：pending 与 failed（重试）；done 跳过 → 不重复写入探测。"""
    return sorted(
        item["ordinal"]
        for item in items
        if item["status"] in (ProbeItemStatus.PENDING.value, ProbeItemStatus.FAILED.value)
    )


def recompute_progress(items: list[dict]) -> dict:
    """由明细重算批次断点计数（单一事实来源，避免计数器与明细漂移）。"""
    done = sum(1 for item in items if item["status"] == ProbeItemStatus.DONE.value)
    failed = sum(1 for item in items if item["status"] == ProbeItemStatus.FAILED.value)
    pending = sum(1 for item in items if item["status"] == ProbeItemStatus.PENDING.value)
    done_items = sorted(
        (item for item in items if item["status"] == ProbeItemStatus.DONE.value),
        key=lambda item: item["ordinal"],
    )
    return {
        "total": len(items),
        "completed": done,
        "failed": failed,
        "pending": pending,
        "last_table_code": done_items[-1]["table_code"] if done_items else None,
    }


def validate_complete(items: list[dict], probe_count: int, total_tables: int) -> tuple[bool, str | None]:
    """整批校验（发布闸门）：全部明细 done、无失败/待采集，且探测条数与表数一致。"""
    progress = recompute_progress(items)
    if progress["failed"]:
        return False, f"仍有 {progress['failed']} 张表采集失败，需续跑后才能发布"
    if progress["pending"]:
        return False, f"仍有 {progress['pending']} 张表未采集，需续跑后才能发布"
    if progress["completed"] != total_tables:
        return False, f"完成表数 {progress['completed']} 与应采集表数 {total_tables} 不一致"
    if probe_count != total_tables:
        return False, f"探测条数 {probe_count} 与应采集表数 {total_tables} 不一致，拒绝发布半成品"
    return True, None


def aggregate_item_results(results: list[dict]) -> dict:
    """汇总本批探测的状态计数（results 元素含 status: ok/late/early/missing）。"""
    counters = {"ok": 0, "late": 0, "early": 0, "missing": 0}
    for result in results:
        status = result.get("status")
        if status in counters:
            counters[status] += 1
    return counters


def pct(part: int, whole: int) -> int:
    """完成百分比（0-100），整批为 0 张表时记 0。"""
    if whole <= 0:
        return 0
    return round(part * 100.0 / whole)
