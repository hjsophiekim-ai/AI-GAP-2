"""하이닉스 체결강도(KIS cttr) 방향일치 판정 — MU Assist 보조 (2026-10-26, feature/macd2-mu-cttr-assist).

정의(사용자 고정, 임계값 탐색 금지):
  판정 시각 직전 60초 동안의 하이닉스(000660) 1초 스냅샷 cttr 평균.
  RED(UP) 후보는 cttr > 100, BLUE(DOWN) 후보는 cttr < 100 이면 MATCH, 아니면 MISMATCH.
  60초 안에 스냅샷이 하나도 없거나 파일이 없으면 UNAVAILABLE -- 이때 호출자는 기존 CHOP 그대로 간다.

KIS cttr 는 당일 누적 체결강도라 하루 안에서 거의 바뀌지 않는다(하루 단위 regime 에 가깝다).
원천은 ws_shadow_logger 가 쓰는 KIS_LOGGER_DATA_DIR/snapshots/YYYY-MM-DD/000660.csv (MU Assist 와 같은 폴더).
판정만 한다 -- 주문도 state 변경도 하지 않는다.
"""
from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from app.trading.macd2 import config
from app.trading.macd2.models import Direction

KST = timezone(timedelta(hours=9))

MATCH = "MATCH"
MISMATCH = "MISMATCH"
UNAVAILABLE = "UNAVAILABLE"

CTTR_NEUTRAL = 100.0          # 사용자 고정 기준 -- 바꾸지 않는다
WINDOW_SEC = 60
SYMBOL = "000660"
_TAIL_BYTES = 262144          # 1초 스냅샷 ~60행 이상을 넉넉히 덮는다(행당 ~600B)


def _naive_kst(dt: datetime) -> datetime:
    return dt.astimezone(KST).replace(tzinfo=None) if dt.tzinfo is not None else dt


def snapshot_path(now: datetime, data_dir: Optional[str] = None) -> Optional[Path]:
    root = data_dir if data_dir is not None else config.EXEC_STRENGTH_DATA_DIR
    if not root:
        return None
    return Path(root) / "snapshots" / _naive_kst(now).strftime("%Y-%m-%d") / f"{SYMBOL}.csv"


def mean_cttr(now: datetime, data_dir: Optional[str] = None) -> Optional[float]:
    """[now-60s, now) 의 cttr 평균. 데이터가 없으면 None. 파일 끝만 읽는다(append 중인 파일)."""
    path = snapshot_path(now, data_dir)
    if path is None or not path.exists():
        return None
    end = _naive_kst(now)
    start = end - timedelta(seconds=WINDOW_SEC)
    try:
        with open(path, "rb") as fh:
            header = fh.readline().decode("utf-8", "replace").strip()
            size = fh.seek(0, 2)
            fh.seek(max(0, size - _TAIL_BYTES))
            chunk = fh.read().decode("utf-8", "replace")
    except OSError:
        return None
    cols = next(csv.reader([header])) if header else []
    if "ts" not in cols or "cttr" not in cols:
        return None
    i_ts, i_c = cols.index("ts"), cols.index("cttr")
    lines = chunk.splitlines()
    if size > _TAIL_BYTES:
        lines = lines[1:]                       # 잘린 첫 줄
    vals = []
    for ln in lines:
        if not ln.strip():
            continue
        row = next(csv.reader([ln]))
        if len(row) != len(cols) or row == cols:
            continue
        try:
            ts = datetime.fromisoformat(row[i_ts][:26])
            c = float(row[i_c])
        except (TypeError, ValueError):
            continue
        if start <= ts < end:
            vals.append(c)
    return sum(vals) / len(vals) if vals else None


def state_for(direction: Direction, cttr: Optional[float]) -> str:
    if cttr is None:
        return UNAVAILABLE
    sign = 1.0 if direction == Direction.UP_RED else -1.0
    return MATCH if sign * (cttr - CTTR_NEUTRAL) > 0 else MISMATCH


def evaluate(direction: Direction, now: datetime, data_dir: Optional[str] = None) -> dict[str, Any]:
    c = mean_cttr(now, data_dir)
    return {"cttr": c, "state": state_for(direction, c)}
