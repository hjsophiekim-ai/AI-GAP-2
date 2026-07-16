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
from app.utils.time_utils import kst_now
from app.utils.data_paths import PREDICTIONS_DIR as _PREDICTIONS_DIR, LOGS_DIR as _LOGS_DIR

ROOT = Path(__file__).resolve().parent.parent.parent

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


def _header_matches(path: Path, columns: list[str]) -> bool:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            first_line = f.readline().strip()
        existing = first_line.split(",") if first_line else []
        return existing == columns
    except Exception:
        return True  # 읽기 실패 시 굳이 아카이빙하지 않고 그냥 append 시도(무해)


def _archive_if_schema_drifted(path: Path, columns: list[str]) -> None:
    """스키마(컬럼 목록)가 바뀌었는데 그날 파일이 이미 존재하면, 예전 형식 데이터를 조용히
    깨뜨리지 않도록 옛 파일을 타임스탬프를 붙여 보존하고 새 스키마로 다시 시작한다."""
    if not path.exists() or _header_matches(path, columns):
        return
    backup = path.with_name(f"{path.stem}.schema_{kst_now().strftime('%H%M%S')}{path.suffix}")
    try:
        path.rename(backup)
        logger.warning("[HynixSwitchLogger] %s 스키마 변경 감지, 기존 파일을 %s로 보존하고 새로 시작", path.name, backup.name)
    except Exception as exc:
        logger.debug("[HynixSwitchLogger] 스키마 드리프트 아카이빙 실패(%s): %s", path, exc)


def _append_csv(path: Path, columns: list[str], record: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _archive_if_schema_drifted(path, columns)
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
    date_str = kst_now().strftime("%Y%m%d")
    path = _PREDICTIONS_DIR / f"hynix_enhanced_prediction_log_{date_str}.csv"
    row = dict(record)
    row.setdefault("timestamp", kst_now().strftime("%Y-%m-%d %H:%M:%S"))
    reasons = record.get("reason_top5") or []
    for i in range(5):
        row[f"reason_top{i + 1}"] = reasons[i] if i < len(reasons) else ""
    _append_csv(path, _PREDICTION_LOG_COLUMNS, row)


def log_trade(record: dict) -> None:
    """data/logs/hynix_auto_trade_log_{YYYYMMDD}.csv 에 append."""
    date_str = kst_now().strftime("%Y%m%d")
    path = _LOGS_DIR / f"hynix_auto_trade_log_{date_str}.csv"
    row = dict(record)
    row.setdefault("timestamp", kst_now().strftime("%Y-%m-%d %H:%M:%S"))
    _append_csv(path, _TRADE_LOG_COLUMNS, row)
