"""
swing_flag_logger.py — 스윙 플래그 로그 저장 모듈.

스윙 판단 결과를 CSV에 기록하고
장 이후 실제 수익률·적중 여부를 업데이트합니다.
"""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path
from typing import Optional

_ROOT = Path(__file__).resolve().parent.parent.parent
_PRED_DIR = _ROOT / "data" / "predictions"
SWING_FLAGS_CSV = _PRED_DIR / "hynix_swing_flags.csv"

_CSV_FIELDS = [
    "evaluated_at", "swing_score", "swing_flag", "flag_label",
    "bottom_probability", "top_probability", "confidence_score",
    "composite_signal",
    # 가격 구간
    "buy_zone_low", "buy_zone_high", "sell_zone_low", "sell_zone_high",
    "target_price", "stop_loss_price", "expected_holding_days",
    # 당시 주요 입력
    "hynix_prev_close", "micron_pm_return", "kospilab_return",
    "rsi_14", "from_20d_high_pct", "bollinger_pct",
    # 실제 결과 (장 후 업데이트)
    "actual_return_1d", "actual_return_3d", "actual_return_5d",
    "flag_hit",   # 1=적중, 0=빗나감, ""=미확인
]


def log_swing_flag(
    swing_result: dict,
    hynix_prev_close: Optional[float] = None,
    micron_features: Optional[dict] = None,
    kospilab_return: Optional[float] = None,
    tech_indicators: Optional[dict] = None,
) -> None:
    """
    스윙 플래그를 CSV에 저장.

    Parameters
    ----------
    swing_result    : evaluate_swing_flag() 반환 dict
    hynix_prev_close: SK하이닉스 전일 종가 (원)
    micron_features : compute_micron_features() 반환 dict (선택)
    kospilab_return : 코스피랩 예상등락률 (선택)
    tech_indicators : 기술적 지표 dict (선택)
    """
    _PRED_DIR.mkdir(parents=True, exist_ok=True)
    mf = micron_features or {}
    ti = tech_indicators or {}

    row = {
        "evaluated_at":       datetime.now().isoformat(),
        "swing_score":        swing_result.get("swing_score", ""),
        "swing_flag":         swing_result.get("swing_flag", ""),
        "flag_label":         swing_result.get("flag_label", ""),
        "bottom_probability": swing_result.get("bottom_probability", ""),
        "top_probability":    swing_result.get("top_probability", ""),
        "confidence_score":   swing_result.get("confidence_score", ""),
        "composite_signal":   swing_result.get("composite_signal", ""),
        "buy_zone_low":       swing_result.get("buy_zone_low", ""),
        "buy_zone_high":      swing_result.get("buy_zone_high", ""),
        "sell_zone_low":      swing_result.get("sell_zone_low", ""),
        "sell_zone_high":     swing_result.get("sell_zone_high", ""),
        "target_price":       swing_result.get("target_price", ""),
        "stop_loss_price":    swing_result.get("stop_loss_price", ""),
        "expected_holding_days": swing_result.get("expected_holding_days", ""),
        # 당시 입력값
        "hynix_prev_close":   hynix_prev_close or "",
        "micron_pm_return":   mf.get("micron_premarket_return", ""),
        "kospilab_return":    kospilab_return or "",
        "rsi_14":             ti.get("rsi_14", ""),
        "from_20d_high_pct":  ti.get("from_20d_high_pct", ""),
        "bollinger_pct":      ti.get("bollinger_pct", ""),
        # 실제 결과 (빈칸, 나중에 update_actual)
        "actual_return_1d":   "",
        "actual_return_3d":   "",
        "actual_return_5d":   "",
        "flag_hit":           "",
    }

    file_exists = SWING_FLAGS_CSV.exists()
    with open(SWING_FLAGS_CSV, "a", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=_CSV_FIELDS)
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)


def load_swing_flags() -> list[dict]:
    """저장된 스윙 플래그 전체 로드."""
    if not SWING_FLAGS_CSV.exists():
        return []
    with open(SWING_FLAGS_CSV, "r", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def update_actual_returns(
    evaluated_at_prefix: str,
    actual_return_1d: Optional[float] = None,
    actual_return_3d: Optional[float] = None,
    actual_return_5d: Optional[float] = None,
) -> bool:
    """
    실제 수익률과 플래그 적중 여부를 CSV에 업데이트.

    Parameters
    ----------
    evaluated_at_prefix : evaluated_at 앞 16자리 (분 단위 매칭)
    actual_return_1d    : 실제 1일 후 수익률 (%)
    actual_return_3d    : 실제 3일 후 수익률 (%)
    actual_return_5d    : 실제 5일 후 수익률 (%)

    Returns
    -------
    bool : 업데이트 성공 여부
    """
    if not SWING_FLAGS_CSV.exists():
        return False

    rows = []
    updated = False
    with open(SWING_FLAGS_CSV, "r", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            if row.get("evaluated_at", "")[:16] == evaluated_at_prefix[:16]:
                if actual_return_1d is not None:
                    row["actual_return_1d"] = actual_return_1d
                if actual_return_3d is not None:
                    row["actual_return_3d"] = actual_return_3d
                if actual_return_5d is not None:
                    row["actual_return_5d"] = actual_return_5d
                # 플래그 적중 판정: BUY/STRONG_BUY/WAIT_BUY → 3d 양수면 적중
                flag = row.get("swing_flag", "")
                if actual_return_3d is not None:
                    buy_flags  = {"STRONG_BUY", "BUY", "WAIT_BUY"}
                    sell_flags = {"TAKE_PROFIT", "SELL", "STRONG_SELL"}
                    if flag in buy_flags:
                        row["flag_hit"] = 1 if actual_return_3d > 0 else 0
                    elif flag in sell_flags:
                        row["flag_hit"] = 1 if actual_return_3d < 0 else 0
                updated = True
            rows.append(row)

    if updated:
        with open(SWING_FLAGS_CSV, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=_CSV_FIELDS)
            writer.writeheader()
            writer.writerows(rows)
    return updated
