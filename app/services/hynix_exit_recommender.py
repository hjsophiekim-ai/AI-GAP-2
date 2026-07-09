"""
hynix_exit_recommender.py — Dynamic Exit AI 학습(추천만, 자동변경 없음).

1) 최근 1000건의 청산(exit_engine_log.csv)을 분석해 실제 수익률이 가장 높았던
   TP/SL/Trailing/Profit Lock 조합을 추천한다 (`data/state/hynix_exit_recommendation.json`).
2) 매일 장 종료 후, 그날의 청산 건에 대해 "그때 TP/SL을 다르게 했다면 어땠을지"를
   분봉 캐시로 되짚어 조정안을 만든다 (`data/state/hynix_exit_daily_learning.json`).
어느 쪽도 실제 파라미터를 자동으로 바꾸지 않는다 — 추천값만 저장한다.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd

from app.logger import logger

ROOT = Path(__file__).resolve().parent.parent.parent
_EXIT_LOG_PATH = ROOT / "data" / "logs" / "exit_engine_log.csv"
_RECOMMENDATION_PATH = ROOT / "data" / "state" / "hynix_exit_recommendation.json"
_DAILY_LEARNING_PATH = ROOT / "data" / "state" / "hynix_exit_daily_learning.json"

MIN_SAMPLE_PER_GROUP = 5
LOOKBACK_TRADES = 1000
_LOOKAHEAD_MINUTES = 15


def _load_exit_log(limit: Optional[int] = None) -> pd.DataFrame:
    if not _EXIT_LOG_PATH.exists():
        return pd.DataFrame()
    try:
        df = pd.read_csv(_EXIT_LOG_PATH)
        if df.empty:
            return df
        df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
        df = df.dropna(subset=["timestamp"]).sort_values("timestamp")
        if limit:
            df = df.tail(limit)
        return df
    except Exception as exc:
        logger.debug("[ExitRecommender] exit_engine_log 읽기 실패: %s", exc)
        return pd.DataFrame()


def recommend_exit_parameters() -> dict:
    """최근 최대 1000건의 청산 결과를 조합별로 비교해 가장 성과 좋은 조합을 추천."""
    df = _load_exit_log(limit=LOOKBACK_TRADES)
    df = df[df["action"].isin(["SELL_ALL", "SELL_PARTIAL"])] if not df.empty else df

    if df.empty or len(df) < MIN_SAMPLE_PER_GROUP:
        result = {
            "skipped": True, "reason": f"청산 로그 샘플 부족({len(df)}건 < {MIN_SAMPLE_PER_GROUP}건)",
            "recommended": None, "sample_size": len(df), "created_at": datetime.now().isoformat(),
        }
        _save(_RECOMMENDATION_PATH, result)
        return result

    df["profit_pct"] = pd.to_numeric(df["profit_pct"], errors="coerce")
    df["trailing_used"] = df["trailing_stop"].astype(str).isin(["True", "true", "1"])
    df["profit_lock_used"] = pd.to_numeric(df["profit_lock"], errors="coerce").notna()
    df = df.dropna(subset=["profit_pct"])

    grouped = df.groupby(["tp", "sl", "trailing_used", "profit_lock_used"]).agg(
        avg_profit_pct=("profit_pct", "mean"), sample_size=("profit_pct", "count"),
    ).reset_index()
    grouped = grouped[grouped["sample_size"] >= MIN_SAMPLE_PER_GROUP]

    if grouped.empty:
        result = {
            "skipped": True, "reason": "조합별 최소 샘플(5건) 충족 그룹 없음",
            "recommended": None, "sample_size": len(df), "created_at": datetime.now().isoformat(),
        }
        _save(_RECOMMENDATION_PATH, result)
        return result

    best = grouped.loc[grouped["avg_profit_pct"].idxmax()]
    result = {
        "skipped": False,
        "recommended": {
            "tp_pct": float(best["tp"]), "sl_pct": float(best["sl"]),
            "trailing_used": bool(best["trailing_used"]), "profit_lock_used": bool(best["profit_lock_used"]),
        },
        "avg_profit_pct": round(float(best["avg_profit_pct"]), 4),
        "sample_size": int(best["sample_size"]),
        "total_trades_analyzed": len(df),
        "reason": (
            f"최근 {len(df)}건 중 TP {best['tp']}%/SL {best['sl']}%/Trailing "
            f"{'사용' if best['trailing_used'] else '미사용'}/ProfitLock {'사용' if best['profit_lock_used'] else '미사용'} "
            f"조합이 평균 수익률 {best['avg_profit_pct']:+.2f}%로 가장 우수(샘플 {int(best['sample_size'])}건)."
        ),
        "created_at": datetime.now().isoformat(),
    }
    _save(_RECOMMENDATION_PATH, result)
    return result


def generate_daily_exit_learning(date_str: Optional[str] = None) -> dict:
    """오늘 청산 건에 대해 '다른 TP/SL이었다면' 조정안을 만든다(분봉 캐시로 사후 검증, 자동반영 없음)."""
    date_str = date_str or datetime.now().strftime("%Y%m%d")
    df = _load_exit_log()
    if df.empty:
        result = {"date": date_str, "suggestions": [], "reason": "청산 로그 없음", "created_at": datetime.now().isoformat()}
        _save(_DAILY_LEARNING_PATH, result)
        return result

    today_rows = df[df["timestamp"].dt.strftime("%Y%m%d") == date_str]
    if today_rows.empty:
        result = {"date": date_str, "suggestions": [], "reason": "오늘 청산 건 없음", "created_at": datetime.now().isoformat()}
        _save(_DAILY_LEARNING_PATH, result)
        return result

    minute_df = _load_hynix_minute_history()
    suggestions = []
    for _, row in today_rows.iterrows():
        reason_text = str(row.get("reason", ""))
        is_tp_exit = "익절" in reason_text
        is_sl_exit = "손절" in reason_text
        if not (is_tp_exit or is_sl_exit) or minute_df is None:
            continue

        exit_time = row["timestamp"]
        window = minute_df[(minute_df["datetime"] > exit_time) & (minute_df["datetime"] <= exit_time + pd.Timedelta(minutes=_LOOKAHEAD_MINUTES))]
        if window.empty:
            continue
        exit_price = float(row.get("current_price") or 0)
        entry_price = float(row.get("entry_price") or exit_price)
        if entry_price <= 0:
            continue

        if is_tp_exit:
            peak_after = float(window["high"].max())
            peak_return_pct = round((peak_after / entry_price - 1.0) * 100, 2)
            current_tp = float(row.get("tp") or 0)
            if peak_return_pct > current_tp + 0.3:
                suggestions.append({
                    "market_type": row.get("market_type"), "type": "tp", "used": current_tp, "better": peak_return_pct,
                    "note": f"익절 {current_tp}% → 실제로는 {peak_return_pct:.1f}%가 더 좋았음 → 추천 익절 {peak_return_pct:.1f}%",
                })
        if is_sl_exit:
            recovery_after = float(window["high"].max())
            recovery_return_pct = round((recovery_after / entry_price - 1.0) * 100, 2)
            current_sl = float(row.get("sl") or 0)
            if recovery_return_pct > 0:
                suggestions.append({
                    "market_type": row.get("market_type"), "type": "sl", "used": current_sl,
                    "better": max(current_sl - 0.5, 0.3),
                    "note": f"손절 {current_sl}% → 청산 후 가격이 회복됨({recovery_return_pct:+.1f}%) → 추천 손절 {max(current_sl - 0.5, 0.3):.1f}%(완화)",
                })

    result = {
        "date": date_str, "suggestions": suggestions,
        "reason": f"오늘 청산 {len(today_rows)}건 중 개선 후보 {len(suggestions)}건",
        "created_at": datetime.now().isoformat(),
    }
    _save(_DAILY_LEARNING_PATH, result)
    return result


def _load_hynix_minute_history() -> Optional[pd.DataFrame]:
    try:
        from app.data_sources.auto_market_collector import _load_hynix_minute_cache

        df = _load_hynix_minute_cache()
        if df is None or df.empty:
            return None
        df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
        return df.dropna(subset=["datetime"])
    except Exception as exc:
        logger.debug("[ExitRecommender] 분봉 히스토리 로드 실패: %s", exc)
        return None


def _save(path: Path, result: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result, ensure_ascii=False, default=str, indent=2), encoding="utf-8")
    except Exception as exc:
        logger.debug("[ExitRecommender] 결과 저장 실패(%s): %s", path, exc)


def load_exit_recommendation() -> Optional[dict]:
    try:
        if _RECOMMENDATION_PATH.exists():
            return json.loads(_RECOMMENDATION_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.debug("[ExitRecommender] 추천 결과 로드 실패: %s", exc)
    return None


def load_daily_exit_learning() -> Optional[dict]:
    try:
        if _DAILY_LEARNING_PATH.exists():
            return json.loads(_DAILY_LEARNING_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.debug("[ExitRecommender] 일별 학습 결과 로드 실패: %s", exc)
    return None
