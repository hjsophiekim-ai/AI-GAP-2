"""MU ASSIST — 토글 하나(MACD2_MU_ASSIST_ENABLED, 기본 OFF)로 켜는 두 기능 (2026-10-22).

① 진입 보조 (계측 / soft-confirm)
   대기 중인 T+3 후보의 **T+2 시점**(플래그봉 시작 +5분)에 MU 지표를 계산해
   ``macd2_mu_assist_ledger.csv`` 에 ENTRY_SHADOW 행으로만 남긴다. 진입을 막지도,
   앞당기지도 않는다 — T+3 구조 그대로, T+1 진입 없음. 약하면 ENTRY_CONFIRM_WEAK 경고만.

② H50 gap 조기청산
   보유 중 반대 플래그가 떴고 H50 이 그 플래그를 HOLD 할 상황(small_whipsaw_hold.
   evaluate_hold)인데, T+2 형성봉에서 반대방향 MACD gap 이 플래그봉보다 더 벌어졌으면
   T+3 를 기다리지 않고 청산한다(worker._advance_mu_assist_h50_gap_exit). 반대플래그 하나만으로는
   청산하지 않는다. 그 플래그의 T+3 반대 신규진입은 MU_ASSIST_NO_REENTRY 로 막는다.
   MU 는 청산 조건이 아니다 — 반대인지 / 먼저 반대였는지 / momentum 만 로그로 남긴다.

정의 (research_20261022_mu_confirm_exit_ext/PREREG_mu_momentum.md 와 같다)
   F = 플래그봉 시작, D = F+3분(플래그봉 종료), T+2 = D+2분.
   p(t) = t 에 끝나는 MU 1분봉 종가. r(a,b) = d·(p(D+b)/p(D+a) − 1)·1e4 (bp).
   mu_post2_return_bp = r(0,+2), mu_acc2 = 1.5·r(0,+2) − r(−3,0).
   MU hist = 1분봉 MACD(12,26,9) − signal. same_direction = d·hist > 0.
   momentum = hist(방향부호) 가 직전 봉보다 커졌으면 EXPANDING, 아니면 DECELERATING.

MU 원천: ws_shadow_logger 의 ``mu/<YYYY-MM-DD>/bars_1m.csv`` (DNASMU 08:00~ + RBAQMU 09:00~).
KIS 를 추가로 부르지 않는다. 파일이 없거나 T+2 에 필요한 분봉이 아직 없으면 MISSING/STALE 로
기록만 하고 기존 로직 그대로다(fallback). 이 모듈의 어떤 예외도 tick 을 깨지 않게 worker 가 감싼다.
"""
from __future__ import annotations

import csv
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd

from app.trading.macd2 import config, ledger
from app.trading.macd2.models import Direction

KIND_ENTRY = "ENTRY_SHADOW"
KIND_H50_EXIT = "H50_GAP_EXIT"
KIND_H50_KEPT = "H50_GAP_HOLD_KEPT"

STATUS_OK = "OK"
STATUS_MISSING = "MISSING"
STATUS_STALE = "STALE"
STATUS_WARMUP = "WARMUP"

MOMENTUM_EXPANDING = "EXPANDING"
MOMENTUM_DECELERATING = "DECELERATING"

LEDGER_COLUMNS = [
    "trading_date", "kind", "signal_id", "flag_bar_at", "direction", "evaluated_at",
    "position_symbol", "mu_t2_status", "mu_same_direction", "mu_post2_return_bp", "mu_acc2",
    "mu_pre3_return_bp", "mu_hist", "mu_momentum", "mu_entry_warning",
    "mu_opposite", "mu_first", "mu_cross_at",
    "hy_gap_flag", "hy_gap_t2", "hy_gap_expanding", "h50_would_hold", "h50_reason",
    "exit_result", "exit_signal_id",
]

_LOCK = threading.RLock()
_SEEN: set[tuple[str, str, str]] = set()     # (원장 경로, kind, signal_id) — 이번 프로세스에서 이미 기록한 것


def is_enabled() -> bool:
    return bool(config.MU_ASSIST_ENABLED)


def ledger_path() -> Path:
    """신호원장과 같은 디렉터리(테스트는 conftest 가 tmp 로 돌린다)."""
    return Path(ledger.SIGNAL_LEDGER_PATH).parent / config.MU_ASSIST_LEDGER_FILENAME


def _naive(dt: datetime) -> datetime:
    if dt.tzinfo is not None:
        dt = dt.astimezone(config.KST).replace(tzinfo=None)
    return dt


def in_t2_window(flag_bar_dt: datetime, now: datetime) -> bool:
    """T+2 판정 창: F+5분+grace <= now < F+6분 (F+6분 = T+3 확인봉 종료)."""
    f, n = _naive(flag_bar_dt), _naive(now)
    start = f + timedelta(minutes=5, seconds=int(config.MU_ASSIST_T2_GRACE_SEC))
    return start <= n < f + timedelta(minutes=6)


# ── MU 1분봉 ──────────────────────────────────────────────────────────────
def load_mu_1m(now: datetime, data_dir: Optional[str] = None) -> Optional[pd.Series]:
    """오늘 MU 1분봉 종가 (index = 분 시작, naive KST). 09:00 전은 DNASMU, 이후 RBAQMU.
    파일이 없으면 None."""
    root = data_dir if data_dir is not None else config.MU_ASSIST_DATA_DIR
    if not root:
        return None
    day = _naive(now).strftime("%Y-%m-%d")
    path = Path(root) / "mu" / day / "bars_1m.csv"
    if not path.exists():
        return None
    open_at = datetime.strptime(day, "%Y-%m-%d").replace(hour=9)
    pre, main = {}, {}
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if str(row.get("complete", "1")) not in ("1", "1.0", "True"):
                continue
            try:
                minute = datetime.fromisoformat(str(row["minute_kst"])[:19])
                close = float(row["close"])
            except (KeyError, TypeError, ValueError):
                continue
            feed = row.get("feed")
            if feed == "RBAQMU" and minute >= open_at:
                main[minute] = close
            elif feed == "DNASMU" and minute < open_at:
                pre[minute] = close
    merged = {**pre, **main}
    if not merged:
        return None
    s = pd.Series(merged).sort_index()
    return s.asfreq("1min").ffill()


def _macd_hist(close: pd.Series) -> pd.Series:
    fast = close.ewm(span=config.EMA_FAST, adjust=False).mean()
    slow = close.ewm(span=config.EMA_SLOW, adjust=False).mean()
    macd = fast - slow
    return macd - macd.ewm(span=config.EMA_SIGNAL, adjust=False).mean()


def _sign(direction: Direction) -> int:
    return 1 if direction == Direction.UP_RED else -1


def _upto(series: pd.Series, t: datetime) -> pd.Series:
    """t 에 끝난(= 분 시작 + 1분 <= t) 완성 분봉만."""
    return series[series.index + pd.Timedelta(minutes=1) <= t]


def _status(series: Optional[pd.Series], need_minute: datetime) -> str:
    if series is None or series.empty:
        return STATUS_MISSING
    if series.index.max() < need_minute:
        return STATUS_STALE
    if len(series[series.index <= need_minute]) < int(config.MU_ASSIST_MIN_BARS):
        return STATUS_WARMUP
    return STATUS_OK


def evaluate_entry(series: Optional[pd.Series], direction: Direction, flag_bar_dt: datetime) -> dict[str, Any]:
    """T+2 진입 보조 지표. 판정이 아니라 계측이다 — 결과는 기록과 경고에만 쓴다."""
    f = _naive(flag_bar_dt)
    d0 = f + timedelta(minutes=3)               # D: 플래그봉 종료
    t2 = d0 + timedelta(minutes=2)
    status = _status(series, t2 - timedelta(minutes=1))
    out: dict[str, Any] = {"mu_t2_status": status, "mu_same_direction": "", "mu_post2_return_bp": "",
                           "mu_acc2": "", "mu_pre3_return_bp": "", "mu_hist": "", "mu_momentum": "",
                           "mu_entry_warning": ""}
    if status != STATUS_OK:
        return out                               # fallback: 경고도 하지 않는다(판정 불가)
    s = _sign(direction)
    upto = _upto(series, t2)
    p = lambda t: float(_upto(series, t).iloc[-1])
    r = lambda a, b: s * (p(d0 + timedelta(minutes=b)) / p(d0 + timedelta(minutes=a)) - 1.0) * 1e4
    post2, pre3 = r(0, 2), r(-3, 0)
    acc2 = 1.5 * post2 - pre3
    hist = s * _macd_hist(upto)
    same = bool(hist.iloc[-1] > 0)
    momentum = MOMENTUM_EXPANDING if hist.iloc[-1] > hist.iloc[-2] else MOMENTUM_DECELERATING
    weak = (not same) or post2 <= 0 or acc2 < 0
    out.update({
        "mu_same_direction": same, "mu_post2_return_bp": round(post2, 3), "mu_acc2": round(acc2, 3),
        "mu_pre3_return_bp": round(pre3, 3), "mu_hist": round(float(hist.iloc[-1]), 5),
        "mu_momentum": momentum, "mu_entry_warning": config.MU_ASSIST_ENTRY_WARNING if weak else "",
    })
    return out


def evaluate_exit_context(series: Optional[pd.Series], held_direction: Direction,
                          flag_bar_dt: datetime, now: datetime) -> dict[str, Any]:
    """H50 gap 청산의 보조로그 — MU 가 반대인지 / 먼저 반대였는지 / momentum. 조건이 아니다."""
    f = _naive(flag_bar_dt)
    d0 = f + timedelta(minutes=3)
    n = _naive(now)
    status = _status(series, n.replace(second=0, microsecond=0) - timedelta(minutes=1))
    out: dict[str, Any] = {"mu_t2_status": status, "mu_opposite": "", "mu_first": "", "mu_cross_at": "",
                           "mu_hist": "", "mu_momentum": ""}
    if status != STATUS_OK:
        return out
    held = _sign(held_direction)
    hist = held * _macd_hist(_upto(series, n))
    opposite = bool(hist.iloc[-1] < 0)
    # 반대 전환(2봉 유지)의 시각 = 2번째 봉 종료. 그 시각이 D 이하면 'MU 가 먼저'.
    sg = np.sign(hist.values)
    cross = None
    for i in range(2, len(sg)):
        if sg[i] < 0 and sg[i - 1] < 0 and sg[i - 2] >= 0:
            cross = hist.index[i] + pd.Timedelta(minutes=1)
    out.update({
        "mu_opposite": opposite,
        "mu_first": bool(opposite and cross is not None and cross <= d0),
        "mu_cross_at": cross.isoformat() if (opposite and cross is not None) else "",
        "mu_hist": round(float(-hist.iloc[-1]), 5),
        "mu_momentum": MOMENTUM_EXPANDING if (-hist.iloc[-1]) > (-hist.iloc[-2]) else MOMENTUM_DECELERATING,
    })
    return out


# ── 원장 ──────────────────────────────────────────────────────────────────
def _load_rows() -> list[dict[str, Any]]:
    p = ledger_path()
    if not p.exists():
        return []
    with open(p, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def already_recorded(kind: str, signal_id: str) -> bool:
    with _LOCK:
        key = (str(ledger_path()), kind, signal_id)
        if key in _SEEN:
            return True
        for row in _load_rows():
            if row.get("kind") == kind and row.get("signal_id") == signal_id:
                _SEEN.add(key)
                return True
        return False


def record(kind: str, signal_id: str, fields: dict[str, Any]) -> bool:
    """한 (kind, signal_id) 당 한 행. 이미 있으면 쓰지 않는다."""
    with _LOCK:
        if already_recorded(kind, signal_id):
            return False
        ledger._assert_safe_to_write_ledger()
        row = {"kind": kind, "signal_id": signal_id, **fields}
        ledger._append_row(ledger_path(), LEDGER_COLUMNS, row)
        _SEEN.add((str(ledger_path()), kind, signal_id))
        return True


def exit_signal_id(flag_bar_dt: datetime, direction: Direction) -> str:
    from app.trading.macd2.signal_engine import make_signal_id
    return f"{make_signal_id(flag_bar_dt, direction)}:MU_ASSIST_H50_GAP_EXIT"


def exited_on_flag(flag_bar_dt: datetime, direction: Direction) -> bool:
    """이 플래그로 H50 gap 조기청산이 실제 체결됐는가 — T+3 반대진입 차단 판정.
    프로세스 재시작에도 살아남도록 원장 파일을 근거로 한다."""
    sid = exit_signal_id(flag_bar_dt, direction)
    with _LOCK:
        for row in _load_rows():
            if row.get("kind") == KIND_H50_EXIT and row.get("signal_id") == sid and row.get("exit_result") == "EXECUTED":
                return True
    return False


def reset_for_tests() -> None:
    with _LOCK:
        _SEEN.clear()
