"""MU ASSIST — 토글 하나(MACD2_MU_ASSIST_ENABLED, 기본 OFF)로 켜는 두 기능 (2026-10-22 최종형).

CHOP(및 P3/E/N1)을 기본 전략으로 그대로 두고, MU 는 독립 매매신호가 아니라
  ① 진입 품질 shadow(보조정보) 와 ② H50 조기청산의 확인조건 으로만 쓴다.

① 진입 BAD veto (ON 이면 실제 적용)
   대기 중인 T+3 후보의 **T+2 시점**에 MU 패턴 상태(CONFIRM/STALL/DIVERGE)와 하이닉스 T+2 gap
   상태(EXPAND/FLAT/SHRINK)를 계산해 ``macd2_mu_assist_ledger.csv`` 에 ENTRY_SHADOW 로 기록하고,
   ENTRY_BAD(= (STALL ∨ DIVERGE) ∧ (FLAT ∨ SHRINK))이면 T+3 판정에서 신규진입을 막는다
   (worker._resolve_tw2_3slot_candidate_body, MC-DELAY 와 같은 자리 = 슬롯 미소비).
   GOOD/그 외/판정 불가(결측·stale·T+2 기록 없음)는 기존 CHOP 그대로(fail-open). MU 로 진입을
   허용하거나 앞당기는 일은 없다 — T+3 와 CHOP/TEG/VWAP/slot 게이트는 전부 그대로다.

② H50 조기청산 (실제 기능)
   보유 중 반대 하이닉스 플래그에 대해 아래 셋을 **모두** 만족할 때만 T+2 에 H50 HOLD 를 해제하고 청산한다.
     1) H50 이 그 플래그를 HOLD 할 상황 (small_whipsaw_hold.evaluate_hold)
     2) T+2 형성봉에서 반대방향 하이닉스 gap 이 0 보다 크고 플래그봉보다 확대
     3) MU 가 그 반대방향으로 CONFIRM (아래 정의, stale/결측이면 불가 = 기존 H50 HOLD)
   gap 확대만으로도, MU 혼자 반대여도 청산하지 않는다. 그 플래그의 T+3 반대 신규진입은 MU_ASSIST_NO_REENTRY.

정의 — research_20261022_mu_confirm_exit_ext/PREREG_mu_pattern.md 그대로(새 임계값 없음)
   F = 플래그봉 시작, D = F+3분(플래그봉 종료), T+2 = D+2분. d = 플래그 방향부호.
   p(t) = t 에 끝나는 MU 1분봉 종가. r(a,b) = d·(p(D+b)/p(D+a) − 1)·1e4 (bp).
   MU_POST2 = r(0,+2), MU_ACC2 = 1.5·r(0,+2) − r(−3,0).
   h(t) = d·(MU 1분봉 MACD(12,26,9) − signal), t 에 끝난 완성봉 기준.
   DIVERGE = h(D), h(D+1), h(D+2) 중 하나라도 ≤ 0  또는  같은방향 전환(부호 전환 후 2봉 유지,
             시각 = 2번째 봉 종료)이 D−30분 ~ D+3분 사이에 없음
   CONFIRM = DIVERGE 아님 ∧ MU_POST2 > 0 ∧ MU_ACC2 ≥ −10
   STALL   = DIVERGE 아님 ∧ CONFIRM 아님
   HY gap(방향부호, 3분 MACD gap): g_prev 직전봉, g_flag 플래그봉, g_t2 = T+2 부분 확인봉(완성 1분봉 2개)
     EXPAND = (g_t2 − g_flag) ≥ (2/3)·(g_flag − g_prev) / FLAT = 그 외 g_t2 ≥ g_flag / SHRINK = g_t2 < g_flag
   진입 보조 상태: CONFIRM∧EXPAND = ENTRY_CONFIRM / STALL∧비EXPAND = ENTRY_BAD / DIVERGE = ENTRY_DIVERGE /
                  그 외 = ENTRY_WAIT / MU 판정 불가 = MU_UNAVAILABLE

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

PATTERN_CONFIRM = "CONFIRM"
PATTERN_STALL = "STALL"
PATTERN_DIVERGE = "DIVERGE"
PATTERN_UNAVAILABLE = "UNAVAILABLE"

HY_EXPAND = "EXPAND"
HY_FLAT = "FLAT"
HY_SHRINK = "SHRINK"

ENTRY_CONFIRM = "ENTRY_CONFIRM"
ENTRY_BAD = "ENTRY_BAD"
ENTRY_DIVERGE = "ENTRY_DIVERGE"
ENTRY_WAIT = "ENTRY_WAIT"
ENTRY_MU_UNAVAILABLE = "MU_UNAVAILABLE"

#: 사전등록 상수 (PREREG_mu_pattern.md) — 튜닝 금지.
CONFIRM_ACC2_MIN = -10.0
LEAD_WINDOW_BEFORE_MIN = 30
LEAD_WINDOW_AFTER_MIN = 3
HY_EXPAND_PACE = 2.0 / 3.0

LEDGER_COLUMNS = [
    "trading_date", "kind", "signal_id", "flag_bar_at", "direction", "evaluated_at",
    "position_symbol", "mu_t2_status", "mu_same_direction", "mu_post2_return_bp", "mu_acc2",
    "mu_pre3_return_bp", "mu_hist", "mu_momentum", "mu_entry_warning",
    "mu_opposite", "mu_first", "mu_cross_at",
    "hy_gap_flag", "hy_gap_t2", "hy_gap_expanding", "h50_would_hold", "h50_reason",
    "exit_result", "exit_signal_id",
    # 2026-10-22 최종형 (PREREG_mu_pattern)
    "mu_post2_bp", "mu_pattern_state", "hy_gap_t2_state", "mu_entry_assist_status",
    "hy_gap_prev", "hy_gap_t2_bar", "mu_hold_ok", "mu_lead_ok", "exit_decision",
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


def evaluate_pattern(series: Optional[pd.Series], direction: Direction, flag_bar_dt: datetime) -> dict[str, Any]:
    """T+2 MU 패턴 상태 (PREREG_mu_pattern.md). T+2 까지 끝난 완성 1분봉만 쓴다.
    판정 불가(결측/stale/warm-up)면 mu_pattern_state = UNAVAILABLE."""
    f = _naive(flag_bar_dt)
    d0 = f + timedelta(minutes=3)
    t2 = d0 + timedelta(minutes=2)
    status = _status(series, t2 - timedelta(minutes=1))
    out: dict[str, Any] = {"mu_t2_status": status, "mu_pattern_state": PATTERN_UNAVAILABLE,
                           "mu_post2_bp": "", "mu_acc2": "", "mu_pre3_return_bp": "",
                           "mu_hold_ok": "", "mu_lead_ok": "", "mu_cross_at": ""}
    if status != STATUS_OK:
        return out
    s = _sign(direction)
    p = lambda t: float(_upto(series, t).iloc[-1])
    r = lambda a, b: s * (p(d0 + timedelta(minutes=b)) / p(d0 + timedelta(minutes=a)) - 1.0) * 1e4
    post2, pre3 = r(0, 2), r(-3, 0)
    acc2 = 1.5 * post2 - pre3
    h = s * _macd_hist(_upto(series, t2))
    h_at = lambda t: float(h[h.index + pd.Timedelta(minutes=1) <= t].iloc[-1])
    hold_ok = all(h_at(d0 + timedelta(minutes=k)) > 0 for k in (0, 1, 2))
    sg = np.sign(h.values)
    cross = None
    for i in range(2, len(sg)):
        if sg[i] > 0 and sg[i - 1] > 0 and sg[i - 2] <= 0:
            cross = h.index[i] + pd.Timedelta(minutes=1)
    lead_ok = bool(cross is not None and d0 - timedelta(minutes=LEAD_WINDOW_BEFORE_MIN) <= cross
                   <= d0 + timedelta(minutes=LEAD_WINDOW_AFTER_MIN))
    if not (hold_ok and lead_ok):
        state = PATTERN_DIVERGE
    elif post2 > 0 and acc2 >= CONFIRM_ACC2_MIN:
        state = PATTERN_CONFIRM
    else:
        state = PATTERN_STALL
    out.update({"mu_pattern_state": state, "mu_post2_bp": round(post2, 3), "mu_acc2": round(acc2, 3),
                "mu_pre3_return_bp": round(pre3, 3), "mu_hold_ok": hold_ok, "mu_lead_ok": lead_ok,
                "mu_cross_at": cross.isoformat() if cross is not None else ""})
    return out


def hy_gap_state(g_prev: Optional[float], g_flag: Optional[float], g_t2: Optional[float]) -> str:
    """방향부호 하이닉스 gap 의 T+2 상태 (PREREG_mu_pattern.md). 값이 없으면 ''."""
    if g_prev is None or g_flag is None or g_t2 is None:
        return ""
    if (g_t2 - g_flag) >= HY_EXPAND_PACE * (g_flag - g_prev):
        return HY_EXPAND
    return HY_FLAT if g_t2 >= g_flag else HY_SHRINK


def entry_assist_status(pattern_state: str, hy_state: str) -> str:
    """GOOD = CONFIRM ∧ EXPAND / BAD = (STALL ∨ DIVERGE) ∧ (FLAT ∨ SHRINK) / 그 외 = 기존 CHOP 그대로."""
    if pattern_state == PATTERN_UNAVAILABLE or not hy_state:
        return ENTRY_MU_UNAVAILABLE
    if pattern_state == PATTERN_CONFIRM and hy_state == HY_EXPAND:
        return ENTRY_CONFIRM
    if pattern_state in (PATTERN_STALL, PATTERN_DIVERGE) and hy_state in (HY_FLAT, HY_SHRINK):
        return ENTRY_BAD
    return ENTRY_WAIT


# ── 진입 BAD veto (ON 일 때 실제 적용) ─────────────────────────────────────
_ENTRY_STATUS: dict[tuple[str, str], str] = {}     # (원장 경로, T+3 signal_id) -> entry status


def remember_entry_status(signal_id: str, status: str) -> None:
    with _LOCK:
        _ENTRY_STATUS[(str(ledger_path()), signal_id)] = status


def entry_status_for(signal_id: str) -> str:
    """T+2 에 기록한 진입 보조 상태. 기록이 없으면 ''(= fail-open). 재시작 대비 원장도 본다."""
    with _LOCK:
        key = (str(ledger_path()), signal_id)
        if key in _ENTRY_STATUS:
            return _ENTRY_STATUS[key]
        for row in _load_rows():
            if row.get("kind") == KIND_ENTRY and row.get("signal_id") == signal_id:
                _ENTRY_STATUS[key] = row.get("mu_entry_assist_status") or ""
                return _ENTRY_STATUS[key]
    return ""


def entry_veto_reason(signal_id: str) -> Optional[str]:
    """T+3 진입 판정에서 막을 사유. ENTRY_BAD 일 때만 막는다 — GOOD/WAIT/결측은 기존 CHOP 그대로."""
    return config.MU_ASSIST_ENTRY_BAD_BLOCK_REASON if entry_status_for(signal_id) == ENTRY_BAD else None


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
        _ENTRY_STATUS.clear()
