"""巡检批次编排内核（run_core）单测：断点推进、陈旧接管、整批校验、汇总口径。

纯函数，不连 MySQL / Redis。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from src.services import run_core

NOW = datetime(2026, 11, 13, 8, 30, 0)


def _items(spec: list[tuple[int, str, str]]) -> list[dict]:
    return [{"ordinal": ordinal, "status": status, "table_code": code} for ordinal, status, code in spec]


# ---------- 心跳陈旧 / 接管 ----------


def test_stale_heartbeat():
    assert run_core.is_stale(None, NOW, 300) is True
    assert run_core.is_stale(NOW - timedelta(seconds=400), NOW, 300) is True
    assert run_core.is_stale(NOW - timedelta(seconds=10), NOW, 300) is False
    # 恰好等于阈值不算陈旧
    assert run_core.is_stale(NOW - timedelta(seconds=300), NOW, 300) is False


def test_can_take_over_rules():
    # failed 随时可接管续跑
    assert run_core.can_take_over("failed", NOW, NOW, 300) is True
    # running 且心跳陈旧（worker 崩溃）才可接管
    assert run_core.can_take_over("running", NOW - timedelta(seconds=400), NOW, 300) is True
    assert run_core.can_take_over("running", NOW - timedelta(seconds=10), NOW, 300) is False
    # 已发布不可接管（幂等返回即可）
    assert run_core.can_take_over("published", NOW, NOW, 300) is False


# ---------- 断点推进 ----------


def test_next_pending_skips_done():
    items = _items(
        [
            (0, "done", "a"),
            (1, "done", "b"),
            (2, "failed", "c"),
            (3, "pending", "d"),
            (4, "done", "e"),
        ]
    )
    # done 跳过（不重复采集）；pending + failed（重试）按序号返回
    assert run_core.next_pending_ordinals(items) == [2, 3]


def test_recompute_progress():
    items = _items(
        [
            (0, "done", "a"),
            (1, "done", "b"),
            (2, "failed", "c"),
            (3, "pending", "d"),
        ]
    )
    progress = run_core.recompute_progress(items)
    assert progress == {
        "total": 4,
        "completed": 2,
        "failed": 1,
        "pending": 1,
        "last_table_code": "b",
    }


def test_recompute_progress_empty():
    assert run_core.recompute_progress([]) == {
        "total": 0,
        "completed": 0,
        "failed": 0,
        "pending": 0,
        "last_table_code": None,
    }


# ---------- 发布闸门 ----------


def test_validate_complete_rejects_incomplete():
    items = _items([(0, "done", "a"), (1, "failed", "b"), (2, "pending", "c")])
    ok, reason = run_core.validate_complete(items, probe_count=1, total_tables=3)
    assert ok is False
    assert reason and "失败" in reason


def test_validate_complete_rejects_probe_count_mismatch():
    items = _items([(i, "done", f"t{i}") for i in range(3)])
    # 明细全 done 但探测条数对不上 → 拒绝发布半成品
    ok, reason = run_core.validate_complete(items, probe_count=2, total_tables=3)
    assert ok is False
    assert reason and "探测条数" in reason


def test_validate_complete_accepts_full_batch():
    items = _items([(i, "done", f"t{i}") for i in range(3)])
    ok, reason = run_core.validate_complete(items, probe_count=3, total_tables=3)
    assert ok is True
    assert reason is None


# ---------- 汇总 ----------


def test_aggregate_item_results():
    results = [{"status": "ok"}, {"status": "late"}, {"status": "missing"}, {"status": "early"}, {"status": "late"}]
    assert run_core.aggregate_item_results(results) == {
        "ok": 1,
        "late": 2,
        "early": 1,
        "missing": 1,
    }


def test_pct():
    assert run_core.pct(1, 4) == 25
    assert run_core.pct(40, 40) == 100
    assert run_core.pct(0, 0) == 0
