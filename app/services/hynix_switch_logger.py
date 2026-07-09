"""
hynix_switch_logger.py — 예측/거래 로그 저장 (기존 컨벤션: data/predictions/, data/logs/).

날짜별 파일로 로테이션(YYYYMMDD 접미사)하며, 항상 append(기존 내용 덮어쓰지 않음)한다.
디렉토리는 자동 생성한다.
"""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path

from app.logger import logger

ROOT = Path(__file__).resolve().parent.parent.parent
_PREDICTIONS_DIR = ROOT / "data" / "predictions"
_LOGS_DIR = ROOT / "data" / "logs"

_PREDICTION_LOG_COLUMNS = [
    "timestamp", "hynix_price", "inverse_price",
    "base_prediction_score", "existing_micron_score", "hynix_technical_score",
    "intraday_momentum_score", "inverse_pressure_score", "enhanced_score",
    "final_action", "reason_top1", "reason_top2", "reason_top3", "reason_top4", "reason_top5",
]

_TRADE_LOG_COLUMNS = [
    "timestamp", "mode", "action", "symbol", "name", "price", "quantity", "amount", "reason",
    "success", "message",
    "base_prediction_score", "existing_micron_score", "hynix_technical_score",
    "inverse_pressure_score", "enhanced_score", "realized_pnl", "unrealized_pnl", "daily_return",
]


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
        logger.debug("[HynixSwitchLogger] 로그 기록 실패(%s): %s", path, exc)


def log_enhanced_prediction(record: dict) -> None:
    """data/predictions/hynix_enhanced_prediction_log_{YYYYMMDD}.csv 에 append."""
    date_str = datetime.now().strftime("%Y%m%d")
    path = _PREDICTIONS_DIR / f"hynix_enhanced_prediction_log_{date_str}.csv"
    row = dict(record)
    row.setdefault("timestamp", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    reasons = record.get("reason_top5") or []
    for i in range(5):
        row[f"reason_top{i + 1}"] = reasons[i] if i < len(reasons) else ""
    _append_csv(path, _PREDICTION_LOG_COLUMNS, row)


def log_trade(record: dict) -> None:
    """data/logs/hynix_auto_trade_log_{YYYYMMDD}.csv 에 append."""
    date_str = datetime.now().strftime("%Y%m%d")
    path = _LOGS_DIR / f"hynix_auto_trade_log_{date_str}.csv"
    row = dict(record)
    row.setdefault("timestamp", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    _append_csv(path, _TRADE_LOG_COLUMNS, row)
