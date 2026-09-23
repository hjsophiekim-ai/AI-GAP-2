"""X1 SHADOW — X1 판정을 **주문 변경 없이** 기록만 하는 원장.

단계 1 의 안전장치다. ``config.X1_SHADOW_MODE`` 가 켜져 있어도 이 모듈은
would_* 판정을 CSV 한 줄로 남길 뿐, 주문/포지션/기존 원장에 손대지 않는다.
``record()`` 는 **어떤 예외도 밖으로 던지지 않는다** — shadow 기록 실패가
실거래를 막는 일은 없어야 한다(fail-open).

파일: ``<LOGS_DIR>/x1_shadow_ledger.csv``
기존 ``macd2_signal_ledger.csv`` / ``macd2_execution_ledger.csv`` 와 **완전히
분리된 파일**이라 flag-ledger hotfix / reconcile / manual_exit 경로에 영향이 없다.

사후 채움
---------
``future_mfe_pct`` / ``future_mae_pct`` 는 기록 시점에 알 수 없다. 장 종료 후
``backfill_future_excursion()`` 으로 채운다 — **기록 시점에 미래값을 쓰지 않는다.**
"""
from __future__ import annotations

import csv
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from app.trading.macd2 import config
from app.utils.data_paths import LOGS_DIR

logger = logging.getLogger("ai_gap")

SHADOW_LEDGER_PATH: Path = Path(LOGS_DIR) / config.X1_SHADOW_LEDGER_FILENAME

COLUMNS: list[str] = [
    # ── 식별 ──
    "date", "time", "recorded_at", "x1_version", "signal_id",
    # ── 신호/포지션 ──
    "signal", "direction", "current_position", "held_direction", "session",
    # ── base(기존 N1+C1) 판정 ──
    "base_action", "base_approved", "base_reject_reason", "slot_number",
    # ── X1 판정 ──
    "x1_action", "context_score",
    "would_block", "would_exit", "would_reentry", "would_late_entry",
    # ── X1-1 ──
    "premarket_trend", "session_trend", "morning_score", "morning_action",
    # ── X1-2/X1-4 ──
    "flip_count", "flag_count", "cluster_span_min", "flip_seq",
    "box_high", "box_low", "box_breakout",
    "flip_exit_armed", "flip_exit_score",
    # ── X1-3 ──
    "ar1_allowed", "ar1_stack_exempt", "ar1_reason",
    # ── X1-4 ──
    "late_entry_score", "late_entry_latency_min", "late_entry_latency_bucket",
    "late_entry_within_guard",
    # ── X1-5 ──
    "x1_afternoon_blue_bonus",
    # ── 진단 ──
    "x1_reasons", "data_ok",
    # ── 사후 채움 (기록 시점에는 항상 공란) ──
    "future_mfe_pct", "future_mae_pct", "backfilled_at",
]


def _ensure_columns(path: Path) -> None:
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        old = list(reader.fieldnames or [])
        if all(c in old for c in COLUMNS):
            return
        rows = list(reader)
    merged = list(old) + [c for c in COLUMNS if c not in old]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=merged)
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in merged})


def _read_header(path: Path) -> Optional[list[str]]:
    try:
        with open(path, newline="", encoding="utf-8") as fh:
            return next(csv.reader(fh), None)
    except Exception:
        return None


def _append(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    is_new = not path.exists() or path.stat().st_size == 0
    if not is_new:
        _ensure_columns(path)
    fieldnames = COLUMNS if is_new else (_read_header(path) or COLUMNS)
    with open(path, "a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames)
        if is_new:
            w.writeheader()
        w.writerow({c: row.get(c, "") for c in fieldnames})


def build_row(
    ctx: Any,
    *,
    now: datetime,
    signal_id: Optional[str] = None,
    direction: Optional[str] = None,
    current_position: Optional[str] = None,
    held_direction: Optional[str] = None,
    session: Optional[str] = None,
    slot_number: Optional[int] = None,
    base_approved: bool = False,
    base_reject_reason: Optional[str] = None,
    data_ok: bool = True,
) -> dict[str, Any]:
    """``X1Context`` 를 shadow 한 줄로 평면화한다. 부작용 없음."""
    from app.trading.macd2 import x1_context as X

    trace = ctx.as_trace() if hasattr(ctx, "as_trace") else {}
    action = getattr(ctx, "final_action", X.ACTION_PASS)
    base_action = "APPROVED" if base_approved else (base_reject_reason or "REJECTED")
    return {
        "date": now.strftime("%Y%m%d"),
        "time": now.strftime("%H:%M:%S"),
        "recorded_at": now.isoformat(),
        "x1_version": trace.get("x1_version", config.X1_FILTER_VERSION),
        "signal_id": signal_id or "",
        "signal": direction or "",
        "direction": direction or "",
        "current_position": current_position or "FLAT",
        "held_direction": held_direction or "",
        "session": session or "",
        "base_action": base_action,
        "base_approved": bool(base_approved),
        "base_reject_reason": base_reject_reason or "",
        "slot_number": "" if slot_number is None else slot_number,
        "x1_action": action,
        "context_score": trace.get("x1_context_score", 0),
        # would_* 는 "X1 이 켜져 있었다면 이렇게 했을 것"이다. 실제 주문 아님.
        "would_block": action == X.ACTION_BLOCK,
        "would_exit": action == X.ACTION_EXIT,
        "would_reentry": action == X.ACTION_REENTRY,
        "would_late_entry": action == X.ACTION_LATE_ENTRY,
        "premarket_trend": trace.get("premarket_trend") or "",
        "session_trend": trace.get("session_trend") or "",
        "morning_score": trace.get("morning_score", 0),
        "morning_action": trace.get("morning_action", ""),
        "flip_count": trace.get("flip_count", 0),
        "flag_count": trace.get("flag_count", 0),
        "cluster_span_min": trace.get("flip_span_min", 0),
        "flip_seq": trace.get("flip_seq", ""),
        "box_high": trace.get("box_high") if trace.get("box_high") is not None else "",
        "box_low": trace.get("box_low") if trace.get("box_low") is not None else "",
        "box_breakout": bool(trace.get("box_breakout")),
        "flip_exit_armed": bool(trace.get("flip_exit_armed")),
        "flip_exit_score": trace.get("flip_exit_score", 0),
        "ar1_allowed": bool(getattr(ctx.reentry, "allowed", False)) if hasattr(ctx, "reentry") else False,
        "ar1_stack_exempt": bool(getattr(ctx.reentry, "stack_exempt", False)) if hasattr(ctx, "reentry") else False,
        "ar1_reason": getattr(ctx.reentry, "reason", "") if hasattr(ctx, "reentry") else "",
        "late_entry_score": trace.get("late_entry_score", 0),
        "late_entry_latency_min": trace.get("late_entry_latency_min") if trace.get("late_entry_latency_min") is not None else "",
        "late_entry_latency_bucket": trace.get("late_entry_latency_bucket") or "",
        "late_entry_within_guard": bool(getattr(ctx.late_entry, "within_guard", False)) if hasattr(ctx, "late_entry") else False,
        "x1_afternoon_blue_bonus": bool(trace.get("x1_afternoon_blue_bonus")),
        "x1_reasons": trace.get("x1_reasons", ""),
        "data_ok": bool(data_ok),
        # 사후 채움 — 기록 시점에는 반드시 공란이다(미래정보 금지).
        "future_mfe_pct": "",
        "future_mae_pct": "",
        "backfilled_at": "",
    }


def record(ctx: Any, **kwargs: Any) -> bool:
    """shadow 한 줄 기록. ``X1_SHADOW_MODE`` 가 꺼져 있으면 아무것도 하지 않는다.

    **어떤 예외도 밖으로 던지지 않는다** — 기록 실패가 실거래를 막으면 안 된다.
    """
    if not config.X1_SHADOW_MODE:
        return False
    try:
        row = build_row(ctx, **kwargs)
        _append(SHADOW_LEDGER_PATH, row)
        return True
    except Exception as exc:                                   # pragma: no cover
        logger.warning("[X1] shadow 기록 실패(무시): %s", exc)
        return False


def load_rows(limit: int = 500) -> list[dict[str, Any]]:
    if not SHADOW_LEDGER_PATH.exists():
        return []
    try:
        with open(SHADOW_LEDGER_PATH, newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
    except Exception:
        return []
    return rows[-limit:] if limit else rows


def backfill_future_excursion(
    resolver: Any, *, limit: int = 2000, now: Optional[datetime] = None,
) -> int:
    """장 종료 후 ``future_mfe_pct`` / ``future_mae_pct`` 를 채운다.

    ``resolver(row) -> (mfe_pct, mae_pct) | None`` 은 호출부가 준다. 이 모듈은
    가격 데이터를 직접 읽지 않는다. 채운 행 수를 돌려준다.
    """
    if not SHADOW_LEDGER_PATH.exists():
        return 0
    try:
        with open(SHADOW_LEDGER_PATH, newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            fieldnames = list(reader.fieldnames or COLUMNS)
            rows = list(reader)
    except Exception:
        return 0
    stamp = (now or datetime.now()).isoformat()
    filled = 0
    for row in rows[-limit:]:
        if row.get("backfilled_at"):
            continue
        try:
            got = resolver(row)
        except Exception:
            got = None
        if not got:
            continue
        mfe, mae = got
        row["future_mfe_pct"] = "" if mfe is None else round(float(mfe), 6)
        row["future_mae_pct"] = "" if mae is None else round(float(mae), 6)
        row["backfilled_at"] = stamp
        filled += 1
    if not filled:
        return 0
    with open(SHADOW_LEDGER_PATH, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames)
        w.writeheader()
        for row in rows:
            w.writerow({c: row.get(c, "") for c in fieldnames})
    return filled
