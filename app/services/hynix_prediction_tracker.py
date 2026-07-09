"""
hynix_prediction_tracker.py — 판단(decision) 로그 + 예측-실제 결과(outcome) 추적.

3분마다(실제 주문 여부와 무관하게) 판단을 `data/logs/trade_decision_log.csv`에
기록하고, 3/5/10/30분 후 및 당일 종가 시점의 실제 가격을 비교해
`data/logs/prediction_outcome_log.csv`에 적중 여부를 남긴다. 두 파일 모두
날짜로 로테이션하지 않는 단일 누적 파일이다(최근 N거래일 분석이 목적).
"""

from __future__ import annotations

import csv
import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd

from app.logger import logger

ROOT = Path(__file__).resolve().parent.parent.parent
_LOGS_DIR = ROOT / "data" / "logs"
_DECISION_LOG_PATH = _LOGS_DIR / "trade_decision_log.csv"
_OUTCOME_LOG_PATH = _LOGS_DIR / "prediction_outcome_log.csv"
_PENDING_PATH = ROOT / "data" / "state" / "hynix_pending_outcomes.json"

HORIZON_MINUTES = [3, 5, 10, 30]
_NEUTRAL_RETURN_THRESHOLD_PCT = 0.15
_RETURN_TO_SCORE_SPAN_PCT = 1.0

_DECISION_LOG_COLUMNS = [
    "timestamp", "hynix_price", "inverse_price",
    "base_prediction_score", "existing_micron_score", "micron_1min_score", "micron_3min_score",
    "hynix_technical_score", "intraday_momentum_score", "inverse_pressure_score", "enhanced_score",
    "final_action", "actual_trade_executed", "position_symbol",
    "reason_top1", "reason_top2", "reason_top3", "reason_top4", "reason_top5",
]

_OUTCOME_LOG_COLUMNS = [
    "decision_timestamp", "outcome_timestamp", "horizon_minutes", "predicted_action", "predicted_direction",
    "hynix_price_at_decision", "hynix_price_at_outcome", "inverse_price_at_decision", "inverse_price_at_outcome",
    "hynix_return_pct", "inverse_return_pct", "prediction_correct", "score_error", "realized_trade_pnl",
]


def _direction_from_action(final_action: str) -> str:
    if final_action in ("HYNIX_STRONG_BUY", "HYNIX_BUY"):
        return "up"
    if final_action in ("INVERSE_STRONG_BUY", "INVERSE_BUY"):
        return "down"
    return "neutral"


def _append_csv(path: Path, columns: list[str], record: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        is_new = not path.exists()
        with path.open("a", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=columns)
            if is_new:
                writer.writeheader()
            writer.writerow({col: record.get(col, "") for col in columns})
    except Exception as exc:
        logger.debug("[PredictionTracker] 로그 기록 실패(%s): %s", path, exc)


def log_trade_decision(
    now: datetime, hynix_price: Optional[float], inverse_price: Optional[float],
    enhanced_result: dict, decision: dict, actual_trade_executed: bool, position_symbol: Optional[str],
) -> None:
    """매 사이클(3분마다) 판단 로그 기록 — 실제 주문 여부와 무관하게 항상 기록."""
    reasons = enhanced_result.get("reason_top5") or []
    micron_detail = enhanced_result.get("micron_detail", {}) or {}
    row = {
        "timestamp": now.strftime("%Y-%m-%d %H:%M:%S"),
        "hynix_price": hynix_price, "inverse_price": inverse_price,
        "base_prediction_score": enhanced_result.get("base_prediction_score"),
        "existing_micron_score": enhanced_result.get("existing_micron_score"),
        "micron_1min_score": micron_detail.get("micron_1min_score"),
        "micron_3min_score": micron_detail.get("micron_3min_score"),
        "hynix_technical_score": enhanced_result.get("hynix_technical_score"),
        "intraday_momentum_score": enhanced_result.get("intraday_momentum_score"),
        "inverse_pressure_score": enhanced_result.get("inverse_pressure_score"),
        "enhanced_score": enhanced_result.get("enhanced_score"),
        "final_action": decision.get("final_action"),
        "actual_trade_executed": bool(actual_trade_executed),
        "position_symbol": position_symbol or "",
    }
    for i in range(5):
        row[f"reason_top{i + 1}"] = reasons[i] if i < len(reasons) else ""
    _append_csv(_DECISION_LOG_PATH, _DECISION_LOG_COLUMNS, row)

    try:
        enqueue_pending_outcomes(now, decision.get("final_action", "HOLD"), hynix_price, inverse_price, enhanced_result.get("enhanced_score", 50.0))
    except Exception as exc:
        logger.debug("[PredictionTracker] pending outcome 등록 실패: %s", exc)


def _load_pending() -> list[dict]:
    try:
        if not _PENDING_PATH.exists():
            return []
        data = json.loads(_PENDING_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception as exc:
        logger.debug("[PredictionTracker] pending 로드 실패: %s", exc)
        return []


def _save_pending(pending: list[dict]) -> None:
    try:
        _PENDING_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = _PENDING_PATH.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(pending, ensure_ascii=False, default=str), encoding="utf-8")
        os.replace(tmp_path, _PENDING_PATH)
    except Exception as exc:
        logger.debug("[PredictionTracker] pending 저장 실패: %s", exc)


def enqueue_pending_outcomes(
    decision_time: datetime, final_action: str, hynix_price: Optional[float], inverse_price: Optional[float], enhanced_score: float,
) -> None:
    """3/5/10/30분 후 결과 확인을 위한 대기열 등록(당일 종가는 별도로 일괄 처리)."""
    if hynix_price is None:
        return
    pending = _load_pending()
    predicted_direction = _direction_from_action(final_action)
    for horizon in HORIZON_MINUTES:
        pending.append({
            "decision_timestamp": decision_time.isoformat(),
            "horizon_minutes": horizon,
            "target_time": (decision_time + timedelta(minutes=horizon)).isoformat(),
            "predicted_action": final_action,
            "predicted_direction": predicted_direction,
            "hynix_price_at_decision": hynix_price,
            "inverse_price_at_decision": inverse_price,
            "enhanced_score_at_decision": enhanced_score,
        })
    _save_pending(pending)


def _prediction_correct(predicted_direction: str, hynix_return_pct: float) -> bool:
    if predicted_direction == "up":
        return hynix_return_pct > 0
    if predicted_direction == "down":
        return hynix_return_pct < 0
    return abs(hynix_return_pct) < _NEUTRAL_RETURN_THRESHOLD_PCT


def _score_error(enhanced_score_at_decision: float, hynix_return_pct: float) -> float:
    actual_direction_score = 50.0 + max(-50.0, min(50.0, hynix_return_pct / _RETURN_TO_SCORE_SPAN_PCT * 50.0))
    return round(abs(enhanced_score_at_decision - actual_direction_score), 2)


def check_and_resolve_pending_outcomes(
    now: datetime, hynix_price: Optional[float], inverse_price: Optional[float],
) -> list[dict]:
    """대기 중인 3/5/10/30분 outcome 중 목표시각 도달분을 현재가로 확정 기록."""
    if hynix_price is None:
        return []
    pending = _load_pending()
    if not pending:
        return []

    resolved_rows: list[dict] = []
    remaining: list[dict] = []
    for item in pending:
        try:
            target_time = datetime.fromisoformat(item["target_time"])
        except Exception:
            continue
        if now < target_time:
            remaining.append(item)
            continue

        decision_price = item.get("hynix_price_at_decision")
        decision_inv_price = item.get("inverse_price_at_decision")
        hynix_return_pct = round((hynix_price / decision_price - 1.0) * 100, 4) if decision_price else None
        inverse_return_pct = (
            round((inverse_price / decision_inv_price - 1.0) * 100, 4)
            if (inverse_price is not None and decision_inv_price) else None
        )
        row = {
            "decision_timestamp": item["decision_timestamp"],
            "outcome_timestamp": now.isoformat(),
            "horizon_minutes": item["horizon_minutes"],
            "predicted_action": item.get("predicted_action"),
            "predicted_direction": item.get("predicted_direction"),
            "hynix_price_at_decision": decision_price,
            "hynix_price_at_outcome": hynix_price,
            "inverse_price_at_decision": decision_inv_price,
            "inverse_price_at_outcome": inverse_price,
            "hynix_return_pct": hynix_return_pct,
            "inverse_return_pct": inverse_return_pct,
            "prediction_correct": (
                _prediction_correct(item.get("predicted_direction", "neutral"), hynix_return_pct)
                if hynix_return_pct is not None else ""
            ),
            "score_error": (
                _score_error(item.get("enhanced_score_at_decision", 50.0), hynix_return_pct)
                if hynix_return_pct is not None else ""
            ),
            "realized_trade_pnl": "",
        }
        _append_csv(_OUTCOME_LOG_PATH, _OUTCOME_LOG_COLUMNS, row)
        resolved_rows.append(row)

    if len(remaining) != len(pending):
        _save_pending(remaining)
    return resolved_rows


def resolve_close_outcomes(
    date_str: Optional[str] = None, hynix_close_price: Optional[float] = None,
    inverse_close_price: Optional[float] = None, realized_pnl_today_krw: float = 0.0,
) -> int:
    """당일 종가 기준 outcome을 일괄 기록(장 종료 후 1회 호출). 이미 기록된 decision은 건너뜀."""
    if hynix_close_price is None:
        return 0
    date_str = date_str or datetime.now().strftime("%Y%m%d")
    decisions = _read_decision_log_for_date(date_str)
    if decisions.empty:
        return 0

    already = set()
    if _OUTCOME_LOG_PATH.exists():
        try:
            existing = pd.read_csv(_OUTCOME_LOG_PATH)
            close_rows = existing[existing["horizon_minutes"] == "close"]
            already = set(close_rows["decision_timestamp"].astype(str))
        except Exception:
            pass

    count = 0
    for _, row in decisions.iterrows():
        ts = str(row["timestamp"])
        decision_ts_iso = pd.to_datetime(ts).isoformat()
        if decision_ts_iso in already or ts in already:
            continue
        decision_price = row.get("hynix_price")
        decision_inv_price = row.get("inverse_price")
        hynix_return_pct = round((hynix_close_price / decision_price - 1.0) * 100, 4) if decision_price else None
        inverse_return_pct = (
            round((inverse_close_price / decision_inv_price - 1.0) * 100, 4)
            if (inverse_close_price is not None and decision_inv_price) else None
        )
        predicted_direction = _direction_from_action(str(row.get("final_action", "HOLD")))
        out_row = {
            "decision_timestamp": decision_ts_iso,
            "outcome_timestamp": datetime.now().isoformat(),
            "horizon_minutes": "close",
            "predicted_action": row.get("final_action"),
            "predicted_direction": predicted_direction,
            "hynix_price_at_decision": decision_price,
            "hynix_price_at_outcome": hynix_close_price,
            "inverse_price_at_decision": decision_inv_price,
            "inverse_price_at_outcome": inverse_close_price,
            "hynix_return_pct": hynix_return_pct,
            "inverse_return_pct": inverse_return_pct,
            "prediction_correct": _prediction_correct(predicted_direction, hynix_return_pct) if hynix_return_pct is not None else "",
            "score_error": _score_error(float(row.get("enhanced_score", 50.0) or 50.0), hynix_return_pct) if hynix_return_pct is not None else "",
            "realized_trade_pnl": realized_pnl_today_krw,
        }
        _append_csv(_OUTCOME_LOG_PATH, _OUTCOME_LOG_COLUMNS, out_row)
        count += 1
    return count


def _read_decision_log_for_date(date_str: str) -> pd.DataFrame:
    if not _DECISION_LOG_PATH.exists():
        return pd.DataFrame()
    try:
        df = pd.read_csv(_DECISION_LOG_PATH)
        if df.empty:
            return df
        df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
        df = df.dropna(subset=["timestamp"])
        return df[df["timestamp"].dt.strftime("%Y%m%d") == date_str]
    except Exception as exc:
        logger.debug("[PredictionTracker] decision log 읽기 실패: %s", exc)
        return pd.DataFrame()


def _read_outcome_log_for_dates(date_strs: list[str]) -> pd.DataFrame:
    if not _OUTCOME_LOG_PATH.exists():
        return pd.DataFrame()
    try:
        df = pd.read_csv(_OUTCOME_LOG_PATH)
        if df.empty:
            return df
        df["decision_timestamp"] = pd.to_datetime(df["decision_timestamp"], errors="coerce")
        df = df.dropna(subset=["decision_timestamp"])
        return df[df["decision_timestamp"].dt.strftime("%Y%m%d").isin(date_strs)]
    except Exception as exc:
        logger.debug("[PredictionTracker] outcome log 읽기 실패: %s", exc)
        return pd.DataFrame()


def compute_accuracy(outcome_df: pd.DataFrame, horizon) -> Optional[float]:
    if outcome_df is None or outcome_df.empty:
        return None
    sub = outcome_df[outcome_df["horizon_minutes"].astype(str) == str(horizon)]
    sub = sub[sub["prediction_correct"].astype(str).isin(["True", "False"])]
    if sub.empty:
        return None
    correct = (sub["prediction_correct"].astype(str) == "True").sum()
    return round(correct / len(sub) * 100.0, 2)


SCORE_COLUMNS = [
    "base_prediction_score", "existing_micron_score", "hynix_technical_score",
    "intraday_momentum_score", "inverse_pressure_score",
]


def compute_score_outcome_correlations(decision_df: pd.DataFrame, outcome_df: pd.DataFrame, horizon_minutes: int = 30) -> dict:
    """각 점수 컴포넌트와 실제 하이닉스 수익률(지정 horizon) 간 상관계수."""
    correlations: dict = {col: None for col in SCORE_COLUMNS}
    if decision_df is None or decision_df.empty or outcome_df is None or outcome_df.empty:
        return correlations
    try:
        dec = decision_df.copy()
        dec["timestamp"] = pd.to_datetime(dec["timestamp"], errors="coerce")
        out = outcome_df[outcome_df["horizon_minutes"].astype(str) == str(horizon_minutes)].copy()
        out["decision_timestamp"] = pd.to_datetime(out["decision_timestamp"], errors="coerce")
        joined = dec.merge(out, left_on="timestamp", right_on="decision_timestamp", how="inner")
        joined = joined.dropna(subset=["hynix_return_pct"])
        if len(joined) < 5:
            return correlations
        for col in SCORE_COLUMNS:
            if col not in joined.columns:
                continue
            series = pd.to_numeric(joined[col], errors="coerce")
            returns = pd.to_numeric(joined["hynix_return_pct"], errors="coerce")
            valid = series.notna() & returns.notna()
            if valid.sum() < 5:
                continue
            corr = series[valid].corr(returns[valid])
            correlations[col] = round(float(corr), 4) if pd.notna(corr) else None
        return correlations
    except Exception as exc:
        logger.debug("[PredictionTracker] 상관계수 계산 실패: %s", exc)
        return correlations
