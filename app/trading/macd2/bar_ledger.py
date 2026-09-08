"""3분 확정봉 MACD 원장 — production 신호 재현 전용 (2026-09-08).

■ 무엇을 남기나
  worker 가 확정봉 하나를 판정할 때마다(``worker._advance_confirmed_primary``)
  **그 판정이 실제로 쓴 값 그대로** 1행을 남긴다. 크로스가 났든 안 났든 전부
  기록한다 — 어느 봉에서 백테스트와 갈리는지 짚으려면 HOLD 봉도 있어야 한다.

■ 재계산하지 않는다
  ``macd`` / ``signal`` / ``gap`` / ``prev_gap`` / ``direction`` /
  ``prev_direction_state`` 는 전부 **이미 계산된 값을 인자로 받아** 그대로 쓴다.
  이 모듈은 ``calculate_macd`` 도 ``evaluate_macd_crossover`` 도 호출하지 않는다.
  ``gap``/``prev_gap`` 만은 ``MacdSnapshot`` 이 둘 다 None 일 수 있어
  ``evaluate_macd_crossover`` 와 **동일한 fallback**(current_diff -> macd-signal,
  previous_diff -> hist_last3[-2])을 쓴다. 즉 크로스 판정이 실제로 본 숫자다.

■ 왜 signal ledger 를 쓰지 않나
  ``ledger.append_signal()`` 은 signal_id dedup 을 위해 **매 호출마다 CSV 전체를
  스캔**한다(ledger.py). 봉마다 쓰면 O(n²) 로 장중 tick 을 물고 늘어진다.
  게다가 signal ledger 는 "주문 후보 1건의 생애 기록"이라는 의미가 있어
  크로스 없는 봉을 넣으면 기존 UI/집계의 전제가 깨진다. 그래서 **별도 파일**이며
  기존 두 원장의 스키마는 한 글자도 건드리지 않는다.

■ 안전
  * append-only. 기존 행을 고치지 않는다.
  * 같은 봉 중복기록은 프로세스 내 마지막 bar_at + 파일 복구로 차단한다.
  * 절대 raise 하지 않는다. 호출부도 try/except 로 한 번 더 감싼다.
  * ad-hoc 스크립트가 실서버 원장에 쓰는 2026-08-19 사고를 막기 위해
    ``ledger._assert_safe_to_write_ledger`` 와 같은 형태의 가드를 둔다.
"""
from __future__ import annotations

import csv
import os
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

import pandas as pd

from app.trading.macd2 import config
from app.trading.macd2.ledger import LIVE_WORKER_MARKER_ENV
from app.utils.data_paths import LOGS_DIR

KST = config.KST

BAR_LEDGER_FILENAME = "macd2_bar_ledger.csv"

#: 테스트/격리 스크립트는 이 속성을 재지정한다(conftest 관례와 동일).
BAR_LEDGER_PATH: Path = LOGS_DIR / BAR_LEDGER_FILENAME
_DEFAULT_BAR_LEDGER_PATH: Path = BAR_LEDGER_PATH

BAR_LEDGER_COLUMNS = [
    "bar_at",
    "macd", "signal", "gap", "prev_gap",
    "direction", "confirmed_cross", "prev_direction_state",
    "bars_in_frame", "premarket_bars_in_frame", "oldest_bar_at",
    "dropped_incomplete_bars",
    "signal_id", "worker_instance_id",
]

_LOCK = threading.RLock()
_last_bar_at: Optional[str] = None
_seeded = False


def _assert_safe_to_write() -> None:
    """production 기본 경로에 쓰려는 것이 진짜 live Worker 인지 확인한다.

    ``ledger._assert_safe_to_write_ledger`` 와 같은 규칙: 경로가 이미 다른 곳으로
    재지정돼 있으면(pytest conftest / 격리 스크립트) 안전, 아니면 live worker
    마커가 이 프로세스 pid 일 때만 허용.
    """
    if BAR_LEDGER_PATH != _DEFAULT_BAR_LEDGER_PATH:
        return
    if os.environ.get(LIVE_WORKER_MARKER_ENV) == str(os.getpid()):
        return
    raise RuntimeError(
        f"REFUSING to write to the production MACD2 bar ledger ({_DEFAULT_BAR_LEDGER_PATH}). "
        "Redirect bar_ledger.BAR_LEDGER_PATH to a tmp directory first "
        "(mirror tests/macd2/conftest.py's _isolate_macd2_state fixture)."
    )


def _seed_last_bar_at() -> None:
    """재시작 후에도 같은 봉을 다시 쓰지 않도록 파일에서 마지막 bar_at 복구."""
    global _last_bar_at, _seeded
    _seeded = True
    try:
        if not BAR_LEDGER_PATH.exists():
            return
        last = None
        with open(BAR_LEDGER_PATH, "r", encoding="utf-8", newline="") as fh:
            for raw in csv.DictReader(fh):
                v = str(raw.get("bar_at") or "")
                if v:
                    last = v
        _last_bar_at = last
    except Exception:
        _last_bar_at = None


def _resolve_gap(macd_snap) -> tuple[Optional[float], Optional[float]]:
    """``evaluate_macd_crossover`` 가 실제로 쓰는 두 값 그대로 (재계산 아님)."""
    prev = getattr(macd_snap, "previous_diff", None)
    cur = getattr(macd_snap, "current_diff", None)
    if prev is None:
        h3 = getattr(macd_snap, "hist_last3", None)
        if h3 is not None and len(h3) >= 2:
            prev = h3[-2]
    if cur is None:
        try:
            cur = float(macd_snap.macd) - float(macd_snap.signal)
        except Exception:
            cur = None
    return (float(prev) if prev is not None else None,
            float(cur) if cur is not None else None)


def _frame_diagnostics(bars_3m, bar_dt) -> tuple[Optional[str], Optional[int]]:
    """(oldest_bar_at, premarket_bars_in_frame). 실패하면 (None, None)."""
    try:
        if bars_3m is None or len(bars_3m) == 0:
            return None, None
        dt = pd.DatetimeIndex(bars_3m["datetime"]).tz_convert(KST)
        oldest = dt.min().isoformat()
        day = pd.Timestamp(bar_dt).astimezone(KST).strftime("%Y%m%d")
        same_day = dt.strftime("%Y%m%d") == day
        pre = int(((dt.time < config.SESSION_OPEN) & same_day).sum())
        return oldest, pre
    except Exception:
        return None, None


def record_bar(
    *,
    macd_snap,
    direction,
    prev_direction_state,
    bars_3m=None,
    dropped_bar_starts=None,
    signal_id: Optional[str] = None,
    worker_instance_id: Optional[str] = None,
) -> bool:
    """확정봉 1행 기록. 이미 기록된 봉이면 False. 절대 raise 하지 않는다.

    모든 수치는 호출부가 **이미 계산해 둔** 것을 그대로 받는다.
    """
    global _last_bar_at
    try:
        bar_at = pd.Timestamp(macd_snap.bar_dt).isoformat()
        with _LOCK:
            if not _seeded:
                _seed_last_bar_at()
            if _last_bar_at == bar_at:
                return False
            _assert_safe_to_write()

            prev_gap, gap = _resolve_gap(macd_snap)
            oldest, premarket = _frame_diagnostics(bars_3m, macd_snap.bar_dt)
            dv = getattr(direction, "value", direction)
            pdv = getattr(prev_direction_state, "value", prev_direction_state)
            row = {
                "bar_at": bar_at,
                "macd": getattr(macd_snap, "macd", None),
                "signal": getattr(macd_snap, "signal", None),
                "gap": gap,
                "prev_gap": prev_gap,
                "direction": "" if dv is None else str(dv),
                "confirmed_cross": bool(dv in ("UP_RED", "DOWN_BLUE")),
                "prev_direction_state": "" if pdv is None else str(pdv),
                "bars_in_frame": getattr(macd_snap, "completed_3m_count", None),
                "premarket_bars_in_frame": premarket,
                "oldest_bar_at": oldest,
                "dropped_incomplete_bars": (
                    None if dropped_bar_starts is None else len(dropped_bar_starts)
                ),
                "signal_id": signal_id or "",
                "worker_instance_id": str(worker_instance_id or ""),
            }
            BAR_LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
            is_new = not BAR_LEDGER_PATH.exists() or BAR_LEDGER_PATH.stat().st_size == 0
            with open(BAR_LEDGER_PATH, "a", encoding="utf-8", newline="") as fh:
                writer = csv.DictWriter(fh, fieldnames=BAR_LEDGER_COLUMNS)
                if is_new:
                    writer.writeheader()
                writer.writerow({c: ("" if row.get(c) is None else row.get(c))
                                 for c in BAR_LEDGER_COLUMNS})
            _last_bar_at = bar_at
            return True
    except Exception:
        return False


def load(date_ymd: Optional[str] = None) -> pd.DataFrame:
    """원장 읽기. ``date_ymd`` 를 주면 그날 봉만. 없으면 빈 프레임."""
    if not BAR_LEDGER_PATH.exists():
        return pd.DataFrame(columns=BAR_LEDGER_COLUMNS)
    df = pd.read_csv(BAR_LEDGER_PATH)
    if "bar_at" in df.columns:
        df["bar_at"] = pd.to_datetime(df["bar_at"], errors="coerce", utc=True)
        try:
            df["bar_at"] = df["bar_at"].dt.tz_convert(KST)
        except Exception:
            pass
        df = df.drop_duplicates(subset=["bar_at"], keep="last")
        if date_ymd:
            df = df[df["bar_at"].dt.strftime("%Y%m%d") == date_ymd]
    return df.sort_values("bar_at").reset_index(drop=True)


def _reset_for_tests() -> None:
    """테스트가 BAR_LEDGER_PATH 를 옮긴 뒤 중복차단 상태를 비우기 위한 훅."""
    global _last_bar_at, _seeded
    with _LOCK:
        _last_bar_at = None
        _seeded = False
