"""1분봉 입력 계약 검사 -- 이상을 즉시 드러내고 신규 진입만 막는다 (2026-11-20).

11/19 실사고: KIS 1분봉 라벨이 하루 종일 HH:MM:30 이었고(봉 구간 자체가 30초 밀린
것으로 판단), MACD2 는 아무 경보 없이 하루를 무거래로 보냈다. 정규화
(app.utils.minute_bars)로 데이터는 받아 이어 붙이되, 신호가 KIS 차트와 30초 어긋날
수 있는 동안에는 **자동 신규진입을 막고** 사람에게 알린다.

상태 (우선순위 순)
  * BAR_ALIGNMENT_ANOMALY  -- 당일 1분봉 원래 라벨의 초가 00 이 아님
        발동: 최근 연속 ALIGN_TRIGGER_RUN 개 이상, 또는 (해제 이후) 행의 ALIGN_TRIGGER_RATIO 이상
        해제: 최근 연속 ALIGN_CLEAR_RUN 개가 다시 :00
  * DATA_CONTRACT_VIOLATION -- 1분봉은 새로 들어오는데 완성 3분봉이 STALE_3M_MIN 분 이상 정체,
        또는 당일 1분봉 커버리지(받은 분 / 경과 분)가 COVERAGE_MIN 미만
  * OK

차단 범위: ``state.data_contract_block_reason`` 이 진입창을 닫는다(worker 의
entry_window_open, order_executor.execute_signal 의 BUY 직전 최종 방어선). 손절·익절·강제청산
등 보유 포지션 청산 로직과 플래그 계산·원장 기록은 그대로 동작한다 -- 플래그 원장에는
차단 사유로 이 상태가 남아 사후 비교가 가능하다.

이 모듈은 MACD/플래그 값을 만들거나 바꾸지 않는다.
"""
from __future__ import annotations

import csv
import threading
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any, Optional

import pandas as pd

from app.logger import logger
from app.trading.macd2 import config, ledger
from app.utils.minute_bars import RAW_TS_COL

KST = config.KST

STATUS_OK = "OK"
STATUS_ALIGN = "BAR_ALIGNMENT_ANOMALY"
STATUS_VIOLATION = "DATA_CONTRACT_VIOLATION"

ALIGN_TRIGGER_RUN = 3
ALIGN_TRIGGER_RATIO = 0.05
ALIGN_TRIGGER_MIN_ROWS = 20
ALIGN_CLEAR_RUN = 10
STALE_3M_MIN = 6.0
FRESH_1M_MAX_SEC = 180.0
COVERAGE_MIN = 0.8
COVERAGE_MIN_SPAN = 30

#: 검사 창 -- 매일 구조적으로 1분봉/3분봉이 비는 구간은 정체로 보지 않는다.
CHECK_START = time(8, 5)
CHECK_END = time(15, 20)
PADDING_START = time(8, 50)
PADDING_END = time(9, 0)
_STRUCTURAL = ((time(8, 50), time(9, 0)), (time(15, 20), time(15, 40)))

LEDGER_NAME = "macd2_data_contract_ledger.csv"
LEDGER_COLUMNS = [
    "timestamp", "trading_date", "event", "status", "prev_status", "reasons",
    "today_1m_count", "covered_minutes", "span_minutes", "coverage",
    "nonzero_second_count", "nonzero_second_ratio", "trailing_nonzero_run", "trailing_zero_run",
    "offset_examples", "actionable_3m_drops", "last_completed_3m_bar_at", "stale_3m_min",
    "newest_1m_age_sec", "worker_instance_id",
]
_io_lock = threading.Lock()


def ledger_path() -> Path:
    return Path(ledger.LOGS_DIR_PATH) / LEDGER_NAME


def _append(row: dict) -> None:
    try:
        path = ledger_path()
        with _io_lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            new = not path.exists()
            with path.open("a", encoding="utf-8", newline="") as fh:
                w = csv.DictWriter(fh, fieldnames=LEDGER_COLUMNS, extrasaction="ignore")
                if new:
                    w.writeheader()
                w.writerow(row)
    except Exception:
        logger.exception("[MACD2][DATA_CONTRACT] 원장 기록 실패")


def _in_structural(t: time) -> bool:
    return any(lo <= t < hi for lo, hi in _STRUCTURAL)


def _fresh_today_block(trading_date: str) -> dict:
    return {
        "trading_date": trading_date, "status": STATUS_OK, "since": None, "reasons": [],
        "align_active": False, "align_since": None, "align_cleared_at": None,
        "violation_since": None, "history_gap_since": None,
        "violation_events": 0, "align_events": 0, "last_minute_logged": None,
        "metrics": {},
    }


def _today_rows(df_1m: Optional[pd.DataFrame], today: str) -> pd.DataFrame:
    if df_1m is None or df_1m.empty or "datetime" not in df_1m.columns:
        return pd.DataFrame(columns=["datetime", RAW_TS_COL])
    work = df_1m[["datetime"] + ([RAW_TS_COL] if RAW_TS_COL in df_1m.columns else [])].copy()
    work["datetime"] = pd.to_datetime(work["datetime"], errors="coerce")
    work = work.dropna(subset=["datetime"])
    work = work[work["datetime"].dt.tz_convert(KST).dt.strftime("%Y%m%d") == today]
    if RAW_TS_COL not in work.columns:
        work[RAW_TS_COL] = work["datetime"]
    work[RAW_TS_COL] = pd.to_datetime(work[RAW_TS_COL], errors="coerce")
    work[RAW_TS_COL] = work[RAW_TS_COL].where(work[RAW_TS_COL].notna(), work["datetime"])
    return work.sort_values("datetime").reset_index(drop=True)


def compute_metrics(
    df_1m: Optional[pd.DataFrame], *, now: datetime, last_completed_3m_bar_at: Optional[str],
    actionable_3m_drops: int, since_ts: Optional[str] = None,
) -> dict[str, Any]:
    """순수 계산. 상태를 바꾸지 않는다."""
    today = now.astimezone(KST).strftime("%Y%m%d")
    rows = _today_rows(df_1m, today)
    off = (rows[RAW_TS_COL] - rows["datetime"]) if not rows.empty else pd.Series(dtype="timedelta64[ns]")
    nonzero = (off != pd.Timedelta(0)) if not rows.empty else pd.Series(dtype=bool)
    flags = list(nonzero.astype(bool)) if not rows.empty else []
    trailing_nonzero = trailing_zero = 0
    for f in reversed(flags):
        if f:
            if trailing_zero:
                break
            trailing_nonzero += 1
        else:
            if trailing_nonzero:
                break
            trailing_zero += 1
    # 해제 이후 구간 비율
    if since_ts and not rows.empty:
        cut = pd.Timestamp(since_ts)
        win = nonzero[rows["datetime"] > cut]
    else:
        win = nonzero
    win_n = int(len(win))
    win_ratio = float(win.mean()) if win_n else 0.0
    # 커버리지 (구조적 창 제외)
    covered = span = 0
    if not rows.empty:
        first = rows["datetime"].iloc[0]
        newest = rows["datetime"].iloc[-1]
        minutes = pd.date_range(first, newest, freq="1min")
        expected = [m for m in minutes if not _in_structural(m.tz_convert(KST).time())]
        have = set(rows["datetime"])
        span = len(expected)
        covered = sum(1 for m in expected if m in have)
    newest_1m_age = None
    if not rows.empty:
        newest_1m_age = (now - rows["datetime"].iloc[-1].to_pydatetime()).total_seconds() - 60.0
        newest_1m_age = max(0.0, newest_1m_age)
    # 완성 3분봉 정체(분) -- 프리오픈 padding 이후 09:00 을 기준점으로 보정
    stale_min = None
    try:
        last_end = (pd.Timestamp(last_completed_3m_bar_at) + timedelta(minutes=3)).to_pydatetime() \
            if last_completed_3m_bar_at else None
    except Exception:
        last_end = None
    if last_end is not None:
        ref = last_end
        open_dt = now.astimezone(KST).replace(hour=PADDING_END.hour, minute=PADDING_END.minute,
                                              second=0, microsecond=0)
        if ref.astimezone(KST).date() == now.astimezone(KST).date() and ref < open_dt <= now:
            ref = open_dt
        stale_min = round((now - ref).total_seconds() / 60.0, 2)
    return {
        "today_1m_count": int(len(rows)),
        "nonzero_second_count": int(nonzero.sum()) if not rows.empty else 0,
        "nonzero_second_ratio": round(float(nonzero.mean()), 4) if not rows.empty else 0.0,
        "window_rows": win_n, "window_nonzero_ratio": round(win_ratio, 4),
        "trailing_nonzero_run": trailing_nonzero, "trailing_zero_run": trailing_zero,
        "offset_examples": [pd.Timestamp(x).isoformat() for x in rows.loc[nonzero, RAW_TS_COL].tail(3)]
        if not rows.empty else [],
        "covered_minutes": covered, "span_minutes": span,
        "coverage": round(covered / span, 4) if span else None,
        "actionable_3m_drops": int(actionable_3m_drops),
        "last_completed_3m_bar_at": last_completed_3m_bar_at,
        "last_completed_3m_today": bool(last_end is not None
                                        and last_end.astimezone(KST).date() == now.astimezone(KST).date()),
        "stale_3m_min": stale_min,
        "newest_1m_age_sec": None if newest_1m_age is None else round(newest_1m_age, 1),
    }


def evaluate(
    state: Any, *, df_1m: Optional[pd.DataFrame], now: datetime,
    last_completed_3m_bar_at: Optional[str], actionable_3m_drops: int,
    history_gap_active: bool, worker_instance_id: str = "",
) -> dict[str, Any]:
    """매 history 반영 tick 마다 호출. state.data_contract / data_contract_block_reason 갱신.

    절대 raise 하지 않는다(실패 시 기존 상태 유지 + 로그)."""
    try:
        return _evaluate(state, df_1m=df_1m, now=now, last_completed_3m_bar_at=last_completed_3m_bar_at,
                         actionable_3m_drops=actionable_3m_drops, history_gap_active=history_gap_active,
                         worker_instance_id=worker_instance_id)
    except Exception:
        logger.exception("[MACD2][DATA_CONTRACT] 평가 실패 -- 이전 상태 유지")
        return dict(getattr(state, "data_contract", None) or {})


def _evaluate(state, *, df_1m, now, last_completed_3m_bar_at, actionable_3m_drops,
              history_gap_active, worker_instance_id):
    today = now.astimezone(KST).strftime("%Y%m%d")
    dc = dict(getattr(state, "data_contract", None) or {})
    if dc.get("trading_date") != today:
        dc = _fresh_today_block(today)
    m = compute_metrics(df_1m, now=now, last_completed_3m_bar_at=last_completed_3m_bar_at,
                        actionable_3m_drops=actionable_3m_drops, since_ts=dc.get("align_cleared_at"))
    now_iso = now.isoformat()

    # ── 정렬 이상 (히스테리시스) ─────────────────────────────────────────
    align_events: list[str] = []
    if dc.get("align_active"):
        if m["trailing_zero_run"] >= ALIGN_CLEAR_RUN:
            dc["align_active"] = False
            dc["align_cleared_at"] = now_iso
            align_events.append("ALIGN_CLEARED")
    else:
        trig_run = m["trailing_nonzero_run"] >= ALIGN_TRIGGER_RUN
        trig_ratio = (m["window_rows"] >= ALIGN_TRIGGER_MIN_ROWS
                      and m["window_nonzero_ratio"] >= ALIGN_TRIGGER_RATIO
                      and m["trailing_zero_run"] < ALIGN_CLEAR_RUN)
        if trig_run or trig_ratio:
            dc["align_active"] = True
            dc["align_since"] = now_iso
            dc["align_events"] = int(dc.get("align_events") or 0) + 1
            align_events.append("ALIGN_TRIGGERED:" + ("RUN" if trig_run else "RATIO"))

    # ── 계약 위반 ────────────────────────────────────────────────────────
    reasons: list[str] = []
    t = now.astimezone(KST).time()
    in_window = CHECK_START <= t < CHECK_END and not _in_structural(t)
    fresh_1m = m["newest_1m_age_sec"] is not None and m["newest_1m_age_sec"] <= FRESH_1M_MAX_SEC
    if in_window and fresh_1m and m["stale_3m_min"] is not None and m["stale_3m_min"] >= STALE_3M_MIN:
        reasons.append(f"STALE_3M_WITH_FRESH_1M:{m['stale_3m_min']}min")
    if in_window and m["span_minutes"] >= COVERAGE_MIN_SPAN and (m["coverage"] or 0) < COVERAGE_MIN:
        reasons.append(f"LOW_1M_COVERAGE:{m['covered_minutes']}/{m['span_minutes']}")
    violation = bool(reasons)
    if violation and not dc.get("violation_since"):
        dc["violation_since"] = now_iso
        dc["violation_events"] = int(dc.get("violation_events") or 0) + 1
    elif not violation:
        dc["violation_since"] = None

    if history_gap_active:
        dc["history_gap_since"] = dc.get("history_gap_since") or now_iso
    else:
        dc["history_gap_since"] = None

    if dc.get("align_active"):
        reasons.insert(0, f"NONZERO_SECOND_LABELS:{m['nonzero_second_count']}"
                          f"(trailing {m['trailing_nonzero_run']}, ex {m['offset_examples'][-1:] or ''})")
        status = STATUS_ALIGN
    elif violation:
        status = STATUS_VIOLATION
    else:
        status = STATUS_OK
    prev = dc.get("status") or STATUS_OK
    if status != prev:
        dc["since"] = now_iso if status != STATUS_OK else None
    dc["status"] = status
    dc["reasons"] = reasons
    dc["metrics"] = m
    dc["updated_at"] = now_iso
    state.data_contract = dc
    state.data_contract_block_reason = None if status == STATUS_OK else status

    minute_key = now.astimezone(KST).strftime("%H:%M")
    event = None
    if status != prev or align_events:
        event = "TRANSITION:" + ",".join(align_events or [f"{prev}->{status}"])
        log = logger.error if status != STATUS_OK else logger.warning
        log("[MACD2][DATA_CONTRACT] %s -> %s reasons=%s metrics=%s", prev, status, reasons,
            {k: m[k] for k in ("today_1m_count", "nonzero_second_count", "trailing_nonzero_run",
                               "trailing_zero_run", "stale_3m_min", "coverage")})
    elif dc.get("last_minute_logged") != minute_key:
        event = "MINUTE"
    if event:
        dc["last_minute_logged"] = minute_key
        _append({
            "timestamp": now_iso, "trading_date": today, "event": event, "status": status,
            "prev_status": prev, "reasons": ";".join(reasons),
            **{k: m.get(k) for k in LEDGER_COLUMNS if k in m and k != "offset_examples"},
            "offset_examples": "|".join(m.get("offset_examples") or []),
            "worker_instance_id": worker_instance_id,
        })
    return dc
