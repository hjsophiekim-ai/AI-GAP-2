"""H50 + whipsaw-watch 연구 엔진 (2026-09-17, READ-ONLY).

`scripts/_tmp_20260907_faithful.py::run_chain` 의 복사본에 아래 3가지만 더했다.

  1. **X2-lite 청산 파라미터 + W1a 사이징** — production
     `time_window_3slot.exit_overrides(MODE_X2LITE_3SLOT)` /
     `position_sizing` 규칙을 그대로 옮긴 것(새 값 없음).
  2. **H50** — production `small_whipsaw_hold.evaluate_hold/evaluate_release`
     를 **직접 import 해서** 호출한다. 연구용 재구현이 아니다.
  3. **H50 + whipsaw-watch 연결(변형 C)** — H50 HOLD 가 처음 발동할 때
     production 과 동일하게 whipsaw-watch 를 seed 한다.

또 하나, 원본 엔진의 whipsaw-watch seeding 이 production 과 달랐던 부분을
선택적으로 고칠 수 있게 했다(`watch_seed_fix`):

  원본: seed 시 last_gap/last_ema_spread = -inf 로 두고 **같은 봉에서 바로**
        재평가 -> gap>0 이면 그 자리에서 즉시 매도(= 반대신호 즉시청산과 동일).
  production: `_start_whipsaw_watch` 가 seed 값으로 **현재 gap/spread** 를
        기록하고 `whipsaw_watch_last_checked_bar_ts = 현재봉` 이므로 **다음
        완성봉부터** 비교한다.

`watch_seed_fix=False` 가 공표 앵커(2026-09-15 연구) 재현용, `True` 가
production 충실판이다. 두 값 모두 보고한다.
"""
from __future__ import annotations

import pickle
import sys
from dataclasses import dataclass, field, replace
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from typing import Callable, Optional

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent / "proj"
for _p in (PROJECT_ROOT, PROJECT_ROOT / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from app.trading.macd2 import config, order_executor, teg_gate  # noqa: E402
from app.trading.macd2 import early_take_profit as etp  # noqa: E402
from app.trading.macd2 import small_whipsaw_hold as swh  # noqa: E402
from app.trading.macd2 import time_window_3slot as tw3  # noqa: E402
from app.trading.macd2 import time_window_filter as twf  # noqa: E402
from app.trading.macd2 import time_window_position_manager as twpm  # noqa: E402
from app.trading.macd2.market_data import filter_complete_3m_bars  # noqa: E402
from app.trading.macd2.models import Direction  # noqa: E402
from app.trading.macd2.signal_engine import (  # noqa: E402
    calculate_macd, evaluate_macd_crossover, resample_completed_3m,
)
from app.trading.macd2.worker import _net_return_pct  # noqa: E402

import backtest_time_window_filter as bt  # noqa: E402
import _tmp_20260903_chop_adaptive_exit_train_oos as ce  # noqa: E402
import tregime as tr  # noqa: E402
import adaptive as ad  # noqa: E402
from _tmp_20260907_exitlab import ExitParams, BASE, _Patched  # noqa: E402

KST = config.KST
HERE = Path(__file__).resolve().parent
_CTX_CACHE = HERE / "_ctx_h50.pkl"
_MEMO_PATH = HERE / "_memo_h50.pkl"

_MEMO: dict = {"base": {}, "veto": {}, "tq": {}, "teg": {}, "chop": {}, "whip": {},
               "h50h": {}, "h50r": {}, "base_q3": {}}
_MEMO_DIRTY = False


def load_memo() -> None:
    global _MEMO
    if _MEMO_PATH.exists():
        try:
            with open(_MEMO_PATH, "rb") as fh:
                loaded = pickle.load(fh)
            for k in _MEMO:
                _MEMO[k].update(loaded.get(k, {}))
        except Exception as exc:
            print(f"  (memo load skipped: {exc})")


def save_memo() -> None:
    if _MEMO_DIRTY:
        with open(_MEMO_PATH, "wb") as fh:
            pickle.dump(_MEMO, fh)


def _memo(bucket, key, fn):
    global _MEMO_DIRTY
    d = _MEMO[bucket]
    if key not in d:
        d[key] = fn()
        _MEMO_DIRTY = True
    return d[key]


# ── X2-lite 청산 파라미터 (production exit_overrides 에서 그대로) ───────────
def x2lite_params() -> ExitParams:
    ov = tw3.exit_overrides(tw3.MODE_X2LITE_3SLOT)
    return replace(
        BASE,
        name="X2-lite",
        stop_loss_pct=float(ov["stop_loss_pct_override"]),
        after_tp1_stop_pct=float(ov["after_tp1_stop_pct_override"]),
        aft_tp_pct=float(ov["afternoon_tp_pct_override"]),
        tp1_sell_ratio=float(ov["tp1_sell_ratio_override"]),
        trail_stop_pct=float(ov["trailing_stop_pct_override"]),
        tp2_pct=float(tw3.morning_tp2_pct_override(tw3.MODE_X2LITE_3SLOT)),
    )


X2LITE = x2lite_params()


# ── D: Trend TP/Runner 변형 (2026-09-17 연구) ──────────────────────────────
# H50 위에 얹는다. **상위추세가 살아 있는 순간에만** 청산 래더 파라미터를
# 바꾸고, 추세가 아니면 X2-lite 값 그대로다. 새 숫자를 만들지 않기 위해
# 대체값은 전부 저장소에 이미 존재하는 상수에서 가져왔다.
#
#   after_tp1  X2-lite 2.0  -> config.MORNING_AFTER_TP1_STOP*100 = 0.3 (모듈 기본값)
#   tp2        X2-lite 5.0  -> config.TW2_MORNING_TP2*100        = 6.0 (TW2 값)
#   tp1        3.0          -> twpm.MORNING_TRAILING_TRIGGER_PCT = 3.5 (기존 상수)
#   tp1_ratio  0.2          -> 0.10 / 0.0 (사용자가 지정한 두 값)
D_VARIANTS = {
    # D1: TP1 은 그대로, TP1 이후 잔량 스탑만 완화
    "D1": {"after_tp1": 0.3},
    # D2: TP1 이후 잔량을 EMA20/EMA50 구조 붕괴까지 보유
    #     (추세 동안 after-TP1/트레일링 스탑을 하드스톱 수준까지 낮추고,
    #      구조가 반전되면 잔량 청산. 하드스톱/강제청산은 그대로 우선.)
    "D2": {"runner_until_structure": True},
    # D3a: TP2 만 더 멀리 (5.0 -> 6.0)
    "D3a": {"tp2": 6.0},
    # D3b: TP1/TP2 둘 다 더 멀리 (3.0 -> 3.5, 5.0 -> 6.0)
    "D3b": {"tp1": 3.5, "tp2": 6.0},
    # D4a: TP1 익절비중 20% -> 10%
    "D4a": {"tp1_ratio": 0.10},
    # D4b: TP1 부분익절 자체를 하지 않음 (0%)
    "D4b": {"tp1_ratio": 0.0},
}


RUNNER_TREND_BREAK = "RUNNER_TREND_BREAK"

# ── tregime.snapshot 벡터화 (ewm adjust=False 는 재귀라 prefix==전체계산) ──
_SNAP_CACHE: dict = {}


def snap_table(bars):
    """(close, ema20, ema50, slope50, slope20) 전체 프레임 1회 계산.
    tr.snapshot(bars[:i+1], d) 와 값이 같은지 무작위 인덱스에서 검증한다."""
    key = id(bars)
    if key in _SNAP_CACHE:
        return _SNAP_CACHE[key]
    from app.trading.macd2.major_flag_filter import _ema, _prepare_bars
    work = _prepare_bars(bars)
    if work is None or len(work) != len(bars):
        raise RuntimeError("snap_table: _prepare_bars 가 행을 바꿨다")
    cl = work["close"].astype(float).to_numpy()
    e20 = _ema(work["close"].astype(float), int(config.H50_TREND_EMA_FAST)).to_numpy()
    e50 = _ema(work["close"].astype(float), int(config.H50_TREND_EMA_SLOW)).to_numpy()
    s50 = np.concatenate([[np.nan], np.diff(e50)])
    s20 = np.concatenate([[np.nan], np.diff(e20)])
    n50 = int(config.H50_TREND_EMA_SLOW)
    tbl = {"close": cl, "e20": e20, "e50": e50, "s50": s50, "s20": s20, "min": n50 + 1}
    # 검증 — production tr.snapshot 과 일치
    for i in (n50 + 5, len(bars) // 3, len(bars) // 2, len(bars) - 2):
        if i < n50 + 1 or i >= len(bars):
            continue
        for d in (Direction.UP_RED, Direction.DOWN_BLUE):
            ref = tr.snapshot(bars.iloc[: i + 1], d)
            assert abs(ref.ema_fast - e20[i]) < 1e-8 and abs(ref.ema_slow - e50[i]) < 1e-8, i
            assert abs(ref.ema_slow_slope - s50[i]) < 1e-8, i
            assert ref.ok == _snap_ok(tbl, i, d), i
    _SNAP_CACHE[key] = tbl
    return tbl


_FEAT_CACHE: dict = {}
_PL = [0, 0.35]   # (seed, p) — pp 위약용


def gapneg_rate(bars, held):
    """보유방향 MACD gap<=0 인 완성봉 비율 — 위약의 p 를 실측에 맞춘다."""
    ft = feat_table(bars)
    sgn = 1.0 if held == Direction.UP_RED else -1.0
    g = ft["gap"] * sgn
    return float((g <= 0).mean())


def feat_table(bars):
    """MACD gap(부호 없음) + 세션 VWAP 전체프레임 1회 계산.
    production twf._gap_series(= signal_engine.calculate_macd_series) 및
    major_flag_filter._session_vwap 과 같은 식이며, prefix 계산과 수치동일함을
    검증한다(ewm adjust=False 재귀 / 일자내 누적)."""
    key = id(bars)
    if key in _FEAT_CACHE:
        return _FEAT_CACHE[key]
    from app.trading.macd2.major_flag_filter import _prepare_bars, _session_vwap
    from app.trading.macd2 import signal_engine as _se
    work = _prepare_bars(bars)
    if work is None or len(work) != len(bars):
        raise RuntimeError("feat_table: _prepare_bars 가 행을 바꿨다")
    ser = _se.calculate_macd_series(work)
    gap = (ser["macd"] - ser["signal"]).astype(float).to_numpy()
    vw = _session_vwap(work).astype(float).to_numpy()
    cl = work["close"].astype(float).to_numpy()
    tbl = {"gap": gap, "vwap": vw, "close": cl}
    # 검증 — prefix 계산과 일치
    for i in (200, len(bars) // 2, len(bars) - 3):
        w2 = _prepare_bars(bars.iloc[: i + 1])
        s2 = _se.calculate_macd_series(w2)
        assert abs(float((s2["macd"] - s2["signal"]).iloc[-1]) - gap[i]) < 1e-8, i
        assert abs(float(_session_vwap(w2).iloc[-1]) - vw[i]) < 1e-8, i
    _FEAT_CACHE[key] = tbl
    return tbl


def pp_cond_ok(bars, i, held, cond, watch_on):
    """peak protection 결합조건. 전부 완성봉 정보. None 이면 무조건 True."""
    if not cond:
        return True
    if cond == "regime_off":
        return not _snap_ok(snap_table(bars), i, held)
    if cond == "placebo":
        # 위약 — gap 부호를 보지 않고 같은 비율(p)로 참을 찍는다.
        import hashlib
        _h = hashlib.md5(f"{_PL[0]}|{i}|{held.value}".encode()).digest()
        return (int.from_bytes(_h[:4], "big") / 2**32) < float(_PL[1])
    if cond == "watch":
        return bool(watch_on)
    ft = feat_table(bars)
    sgn = 1.0 if held == Direction.UP_RED else -1.0
    if cond == "gap_cross":
        # 확정 crossover 그 봉만 (gap_prev>0 -> gap<=0). signal_engine 의
        # evaluate_macd_crossover 온셋 규칙과 같은 정의, 보유방향 기준.
        return bool(ft["gap"][i - 1] * sgn > 0 and ft["gap"][i] * sgn <= 0)
    if cond == "gap_shrink":
        return bool(ft["gap"][i] * sgn < ft["gap"][i - 1] * sgn)
    if cond == "gap_neg":
        return bool(ft["gap"][i] * sgn <= 0)
    if cond == "vwap_off":
        v = ft["vwap"][i]
        if not np.isfinite(v) or v <= 0:
            return False
        return bool((ft["close"][i] < v) if sgn > 0 else (ft["close"][i] > v))
    raise ValueError(cond)


def _snap_ok(tbl, i, held):
    if i < tbl["min"] - 1:
        return False
    c_, f_, sl_, sp_ = tbl["close"][i], tbl["e20"][i], tbl["e50"][i], tbl["s50"][i]
    if held == Direction.UP_RED:
        return bool(c_ > sl_ and f_ > sl_ and sp_ > 0)
    return bool(c_ < sl_ and f_ < sl_ and sp_ < 0)


def fast_release(tbl, i, held, variant, count):
    """tregime.should_release 의 동치 구현 (같은 값을 캐시에서 읽을 뿐)."""
    if i < tbl["min"] - 1:
        return False, count, "INSUFFICIENT_BARS"
    long_ = held == Direction.UP_RED
    c_, f_, sl_ = tbl["close"][i], tbl["e20"][i], tbl["e50"][i]
    fs_ = tbl["s20"][i]
    below_slow = (c_ < sl_) if long_ else (c_ > sl_)
    below_fast = (c_ < f_) if long_ else (c_ > f_)
    fast_bad = (fs_ < 0) if long_ else (fs_ > 0)
    flipped = (f_ < sl_) if long_ else (f_ > sl_)
    cnt = count + 1 if below_slow else 0
    if variant in ("C1", "C5"):
        return bool(below_slow), cnt, "EMA50_BREAK" if below_slow else "HOLDING"
    if variant == "C2":
        return bool(cnt >= 2), cnt, "EMA50_BREAK_2BAR" if cnt >= 2 else "HOLDING"
    if variant == "C3":
        return bool(flipped), cnt, "EMA_STRUCTURE_FLIP" if flipped else "HOLDING"
    if variant == "C4":
        hits = int(below_fast) + int(fast_bad) + int(below_slow)
        return bool(hits >= 2), cnt, f"TWO_OF_THREE({hits})" if hits >= 2 else "HOLDING"
    raise ValueError(variant)

# ── faithful fill primitive (원본 그대로) ──────────────────────────────────
class Quotes:
    def __init__(self, df_1m: pd.DataFrame):
        w = df_1m.sort_values("datetime").reset_index(drop=True)
        self.ts = pd.DatetimeIndex(w["datetime"]).as_unit("ns").asi8
        self.px = w["close"].astype(float).to_numpy()
        self.exact = dict(zip(w["datetime"], self.px))

    def at(self, when) -> Optional[float]:
        i = int(np.searchsorted(self.ts, pd.Timestamp(when).as_unit("ns").value, side="right"))
        if i == 0:
            return None
        return float(self.px[i - 1])


@dataclass
class Ctx:
    dates: list
    hynix_bars_3m: pd.DataFrame
    flags_by_idx: dict
    quotes: dict
    complete_bar_starts: dict
    dropped: dict


def build_ctx(n_days: int = 76, use_cache: bool = True) -> Ctx:
    if use_cache and _CTX_CACHE.exists():
        with open(_CTX_CACHE, "rb") as fh:
            raw = pickle.load(fh)
        if len(raw["dates"]) >= n_days:
            raw["quotes"] = {k: Quotes(v) for k, v in raw.pop("quotes_1m").items()}
            return Ctx(**raw)

    all_dates = ce._common_dates()
    dates = all_dates[-n_days:]
    warmup = all_dates[all_dates.index(dates[0]) - 1]

    hynix_all = ce._load_all("hynix", [warmup] + dates)
    long_all = ce._load_all("long", dates)
    inverse_all = ce._load_all("inverse", dates)
    end = datetime.combine(pd.Timestamp(dates[-1]).date(), dtime(20, 0), tzinfo=KST)

    hynix_raw = resample_completed_3m(hynix_all, now=end)
    hynix_bars_3m, hynix_dropped = filter_complete_3m_bars(hynix_raw, hynix_all)
    long_raw = resample_completed_3m(long_all, now=end)
    long_bars_3m, long_dropped = filter_complete_3m_bars(long_raw, long_all)
    inv_raw = resample_completed_3m(inverse_all, now=end)
    inv_bars_3m, inv_dropped = filter_complete_3m_bars(inv_raw, inverse_all)

    dropped = {"hynix": len(hynix_dropped), config.LONG_SYMBOL: len(long_dropped),
               config.INVERSE_SYMBOL: len(inv_dropped),
               "hynix_total_bars": len(hynix_raw)}

    quotes_1m = {config.LONG_SYMBOL: long_all[["datetime", "close"]],
                 config.INVERSE_SYMBOL: inverse_all[["datetime", "close"]]}
    complete_bar_starts = {config.LONG_SYMBOL: set(long_bars_3m["datetime"]),
                           config.INVERSE_SYMBOL: set(inv_bars_3m["datetime"])}

    flags_by_idx: dict = {}
    prev_direction = None
    last_bar_date = None
    date_set = set(dates)
    for i in range(len(hynix_bars_3m)):
        snap = calculate_macd(hynix_bars_3m.iloc[: i + 1])
        if snap is None:
            continue
        bar_date = pd.Timestamp(hynix_bars_3m["datetime"].iloc[i]).astimezone(KST).strftime("%Y%m%d")
        if last_bar_date is None or bar_date != last_bar_date:
            prev_direction = None
        last_bar_date = bar_date
        direction = evaluate_macd_crossover(snap, prev_direction)
        if direction in (Direction.UP_RED, Direction.DOWN_BLUE):
            prev_direction = direction
            if bar_date in date_set:
                flags_by_idx[i] = direction

    payload = {"dates": dates, "hynix_bars_3m": hynix_bars_3m,
               "flags_by_idx": flags_by_idx, "quotes_1m": quotes_1m,
               "complete_bar_starts": complete_bar_starts, "dropped": dropped}
    with open(_CTX_CACHE, "wb") as fh:
        pickle.dump(payload, fh)
    payload["quotes"] = {k: Quotes(v) for k, v in payload.pop("quotes_1m").items()}
    return Ctx(**payload)


@dataclass
class Trade:
    date: str
    slot_number: Optional[int]
    session: Optional[str]
    direction: str
    decision_idx: int
    flag_ordinal: int = 0
    entry_time: Optional[str] = None
    entry_symbol: Optional[str] = None
    entry_price: Optional[float] = None
    entry_bar_idx: Optional[int] = None
    exit_time: Optional[str] = None
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None
    net_pct: Optional[float] = None
    peak_net_pct: float = 0.0
    entry_chop: bool = False
    chop_score: int = 0
    tq: Optional[int] = None
    lock_fired: bool = False
    hold_bars: int = 0
    w1a: float = 1.0
    h50_held: bool = False      # 이 거래가 H50 HOLD 를 한 번이라도 겪었는가
    ignored_flags: int = 0      # Trend Regime Hold 로 무시한 반대 플래그 수
    regime_hold: bool = False   # regime hold 가 한 번이라도 걸렸는가
    regime_eligible: bool = False
    hold_minutes: float = 0.0
    mode: str = ""            # V4 가 이 거래에 고른 전략 (V1/V2/V3)
    mode_reason: str = ""
    h50_hold_at: Optional[str] = None
    relax: str = ""          # 이 진입이 완화규칙으로 열렸는가 (C/D/TEG)
    legs: list = field(default_factory=list)   # (시각, 가격, 매도비중, 사유, net)
    runner_used: bool = False
    mae_net_pct: float = 0.0        # 보유 중 최저 net (MAE)
    tp1_hit: bool = False
    trend_at_entry: bool = False
    e_close: Optional[float] = None
    e_ema20: Optional[float] = None
    e_ema50: Optional[float] = None
    e_slope: Optional[float] = None
    ax_mode: str = ""            # position-level adaptive exit 판정
    ax_at: Optional[str] = None
    ax_net: Optional[float] = None
    pp_armed: bool = False      # peak protection 무장(peak>=arm) 여부
    pp_fired: bool = False
    pp_arm_at: Optional[str] = None
    pp_arm_idx: Optional[int] = None
    pp_flip_at: Optional[str] = None
    pp_fire_at: Optional[str] = None
    pp_fire_idx: Optional[int] = None
    pp_peak: Optional[float] = None
    pp_give: Optional[float] = None
    peak_at: Optional[str] = None
    # ── RX: regime 방어 오버레이 (연구 전용, rx=None 이면 전부 기본값) ──
    rx_on: bool = False
    rx_h50r: Optional[float] = None
    rx_tp1r: Optional[float] = None
    rx_size_mult: float = 1.0
    rx_lock_arm_at: Optional[str] = None
    rx_lock_fire_at: Optional[str] = None
    rx_partial_at: Optional[str] = None
    rx_partial_px: Optional[float] = None
    rx_reason: str = ""
    # -- CX: CHOP MODE (연구 전용) --
    cx_strategy: str = ""
    cx_on: bool = False
    cx_part_done: bool = False
    # -- GX: CHOP RESPONSE (연구 전용) --
    gx_chop: bool = False
    gx_ar1: bool = False
    gx_part_done: bool = False


# ── W1a (production position_sizing 규칙 그대로) ───────────────────────────
def _w1a_multiplier(*, entry_chop: bool, first_stop: bool, exposure: float,
                    extra: float = 1.0) -> float:
    raw = 1.0
    if entry_chop:
        raw *= float(config.X2LITE_SIZING_CHOP_MULT)
    if first_stop:
        raw *= float(config.X2LITE_SIZING_POST_STOP_MULT)
    raw *= float(extra)
    clipped = max(float(config.X2LITE_SIZING_MIN_MULT),
                  min(float(config.X2LITE_SIZING_MAX_MULT), raw))
    room = float(config.X2LITE_SIZING_DAILY_EXPOSURE_CAP) - exposure
    if room <= 0:
        return 0.0
    return min(clipped, room)


def run_chain(ctx: Ctx, p: ExitParams = X2LITE, dates: Optional[list] = None,
              gate: Optional[Callable] = None,
              morning_only_bypass: bool = True,
              h50: bool = False,
              h50_watch_link=False,
              stale_hold_bug: bool = False,
              d_variant: Optional[str] = None,
              day_cfg: Optional[dict] = None,
              relax_mode: Optional[str] = None,   # None|"B"|"C"|"D"
              teg_soft: bool = False,             # C/D 조건에서 TEG soft-pass
              quality_relax: bool = True,        # 완화조건에서 quality 4->3 도 풀지
              runner: Optional[str] = None,      # None|"R1"|"R2"|"R3" — Trend Runner
              adaptive_default: Optional[str] = None,
              regime_release: Optional[str] = None,
              regime_scope: str = "R1",
              watch_seed_fix: bool = True,
              ax: Optional[dict] = None,
              day_loss_stop: Optional[float] = None,   # 그날 실현손익 이하면 신규진입 금지
              slot_mult: Optional[dict] = None,        # {slot: 배수}
              pp_delay_bars: int = 0,
              pp_slip_pct: float = 0.0,
              paths: Optional[list] = None,
              milestones: Optional[list] = None,
              ms_levels: tuple = (3.0, 4.0, 5.0),
              rx: Optional[dict] = None,
              cx: Optional[dict] = None,
              gx: Optional[dict] = None,
              events: Optional[list] = None) -> list:
    bars = ctx.hynix_bars_3m
    flags_by_idx = ctx.flags_by_idx
    quotes = ctx.quotes
    complete = ctx.complete_bar_starts
    date_set = set(dates if dates is not None else ctx.dates)

    trades: list = []
    # ── RX regime: 진입시점 판정. **이미 청산된 거래만** 본다(미래정보 없음).
    _gx = dict(gx or {})
    _gx_on = _gx.get("on")
    _gx_log = _gx.get("log") if isinstance(_gx.get("log"), list) else []
    _gx_day = {"d": None, "n_ord": 0, "first_loss": None, "streak": 0}

    def _gx_is_chop(i):
        return bool(_gx_on is not None and 0 <= i < len(_gx_on) and _gx_on[i])

    _cx = dict(cx or {})
    _cx_log = _cx.get("log") if isinstance(_cx.get("log"), list) else []
    _cx_state = {"on": False}
    import cxlib as _CX

    _rx = dict(rx or {})
    _rx_reg = _rx.get("regime")

    def _rx_state():
        if not _rx:
            return False, None, None
        if not _rx_reg:
            return True, None, None
        K = int(_rx_reg.get("K", 10))
        prior = trades[-K:]
        if len(prior) < K:
            return False, None, None
        h = sum(1.0 for t in prior if getattr(t, "h50_held", False)) / len(prior)
        q = sum(1.0 for t in prior if getattr(t, "tp1_hit", False)) / len(prior)
        on = (h >= float(_rx_reg.get("h50", 0.40))
              and q <= float(_rx_reg.get("tp1", 0.20)))
        return bool(on), round(h, 4), round(q, 4)

    def _cx_regime():
        """CHOP regime 판정 - 이미 청산된 거래만(지연계산). 히스테리시스 선택."""
        if not _cx:
            return False
        r = _cx.get("regime") or {"K": 10, "h50": 0.40, "tp1": 0.20}
        K = int(r.get("K", 10))
        prior = trades[-K:]
        if len(prior) < K:
            return False
        h = sum(1.0 for t in prior if getattr(t, "h50_held", False)) / len(prior)
        q = sum(1.0 for t in prior if getattr(t, "tp1_hit", False)) / len(prior)
        if not _cx.get("hyst"):
            return bool(h >= float(r.get("h50", 0.40)) and q <= float(r.get("tp1", 0.20)))
        if _cx_state["on"]:
            if h < float(r.get("off_h50", 0.30)) or q > float(r.get("off_tp1", 0.30)):
                _cx_state["on"] = False
        else:
            if h >= float(r.get("h50", 0.40)) and q <= float(r.get("tp1", 0.20)):
                _cx_state["on"] = True
        return bool(_cx_state["on"])

    def _gx_skip(i, rec_at, day, direction, slot_no, sess):
        """CHOP ON 에서 ordinary N1 진입을 막을지. AR1 후보는 절대 막지 않는다."""
        sk = _gx.get("skip")
        if not sk:
            return False
        isar1 = False
        fn = _gx.get("is_ar1")
        if fn is not None:
            try:
                isar1 = bool(fn(day, rec_at))
            except Exception:
                isar1 = False
        if isar1:
            return False
        rule = sk.get("rule", "")
        F = _gx.get("feat")
        pr = sk.get("params", {})
        if rule == "T1":
            t = rec_at.astimezone(KST).time()
            return bool(dtime(10, 0) <= t < dtime(11, 30))
        if rule == "D1":
            return bool((slot_no or 1) >= 2)
        if rule == "D2":
            return bool(_gx_day["first_loss"] is True)
        if rule == "D3":
            return bool(_gx_day["n_ord"] >= int(pr.get("max", 2)))
        if rule == "D4":
            # 무기한 block 금지 — 연속손실이 임계에 닿으면 **한 건만** 건너뛰고
            # 그 즉시 streak 을 리셋한다.
            if _gx_day["streak"] >= int(pr.get("n", 2)):
                _gx_day["streak"] = 0
                return True
            return False
        if F is None:
            return False
        if rule in ("A1", "A2", "A3", "C1", "C2", "C3"):
            sc = F.get("score")
            if rule == "C3":
                return not bool(F["teg_ok"][i])
            if sc is None or i >= len(sc):
                return False
            th = float(pr.get("th", 0.0))
            return bool(sc[i] < th)
        return False

    position = None
    pending = None
    slots_used_today = morning_count = afternoon_count = 0
    flag_ordinal_today = 0
    last_afternoon_direction = None
    current_day = None
    whipsaw_watch = None
    hold = None                       # H50 HOLD 상태
    # stale_hold_bug=True 면 포지션이 닫혀도 HOLD 상태를 버리지 않는다
    # (2026-09-17 이전 production 동작 재현).
    _clr = (lambda h: h) if stale_hold_bug else (lambda h: None)
    rhold = None                      # Trend Regime Hold 상태
    day_entry_seq = 0                 # 그날 몇 번째 진입인가 (R2 판정용)
    d_relax_used = False              # D: 하루 1회 한정
    day_chop_seen = False             # D: 오전 방향성 불명확(CHOP) 이력
    w1a_seq = 0
    w1a_first_stop = False
    w1a_exposure = 0.0

    _CXF = _cx.get("feat") if _cx else None
    _dcfg_fixed = dict(D_VARIANTS[d_variant]) if d_variant else {}
    _trend_cache: dict = {}

    def _mode_of(pos):
        """이 포지션을 관리하는 전략. 고정 실행이면 항상 빈 문자열."""
        return (pos or {}).get("mode", "") if adaptive_default else ""

    def h50_on(pos):
        """H50 HOLD 가 이 포지션에 적용되는가."""
        if adaptive_default:
            return _mode_of(pos) in ("V2", "V3")
        return bool(h50)

    def dcfg_of(pos):
        """이 포지션에 적용할 D 변형 설정."""
        _c = dict(_dcfg_of_inner(pos))
        # E7: 오전 진입 포지션에만 다른 TP2 (오후는 기본값 유지)
        if "tp2_morning" in _c:
            if (pos or {}).get("session") == tw3.SESSION_MORNING:
                _c["tp2"] = _c["tp2_morning"]
            _c.pop("tp2_morning")
        return _c

    def _dcfg_of_inner(pos):
        if day_cfg is not None:
            return dict(day_cfg.get(current_day, {}))
        if adaptive_default:
            return dict(D_VARIANTS["D3a"]) if _mode_of(pos) == "V3" else {}
        return _dcfg_fixed

    def trend_ok_at(idx_, symbol_, active_):
        """보유방향 기준 상위추세(close>EMA50 & EMA20>EMA50 & slope>0).
        판정시점 이전 완성봉만 본다. (idx, 방향) 캐시."""
        if not active_:
            return False
        held_ = bt._direction_for_symbol(symbol_)
        key = (idx_, held_.value)
        if key not in _trend_cache:
            _trend_cache[key] = bool(tr.snapshot(bars.iloc[: idx_ + 1], held_).ok)
        return _trend_cache[key]

    def eff_cfg(trend_, cfg_):
        """이 순간 실제로 적용할 래더 override 묶음.

        - `always=True`  : 추세 여부와 무관하게 적용 (일자단위 regime 용)
        - `off_*` 키     : **비추세** 구간에 적용 (추세땐 큰 스윙 / 휩쏘땐 작은
                           스윙 같은 양방향 변형)
        - 그 외 키       : 추세 구간에 적용 (기존 D 계열과 동일)
        """
        if not cfg_:
            return {}
        if cfg_.get("always"):
            return {k: v for k, v in cfg_.items()
                    if not k.startswith("off_") and k != "always"}
        if trend_:
            return {k: v for k, v in cfg_.items() if not k.startswith("off_")}
        return {k[4:]: v for k, v in cfg_.items() if k.startswith("off_")}

    def ladder_kw(trend_, cfg_):
        """추세일 때만 바뀌는 override 묶음. 추세가 아니면 X2-lite 그대로."""
        _dcfg = eff_cfg(trend_, cfg_)
        if not _dcfg:
            return {}, p.tp1_sell_ratio, p.tp2_pct
        tp2_ = float(_dcfg.get("tp2", p.tp2_pct))
        ratio_ = float(_dcfg.get("tp1_ratio", p.tp1_sell_ratio))
        kw = {}
        if "after_tp1" in _dcfg:
            kw["after_tp1_stop_pct_override"] = float(_dcfg["after_tp1"])
        if "trail" in _dcfg:
            kw["trailing_stop_pct_override"] = float(_dcfg["trail"])
        if _dcfg.get("trail_off"):
            # 추세 유지 중에는 트레일링을 하드스톱 수준까지 낮춘다(= 사실상 해제).
            # 새 숫자를 만들지 않기 위해 p.stop_loss_pct 를 그대로 쓴다.
            kw["trailing_stop_pct_override"] = float(p.stop_loss_pct)
        if _dcfg.get("runner_until_structure"):
            # 잔량 스탑을 하드스톱까지 낮춘다 = 사실상 after-TP1/트레일링 해제.
            kw["after_tp1_stop_pct_override"] = float(p.stop_loss_pct)
            kw["trailing_stop_pct_override"] = float(p.stop_loss_pct)
        return kw, ratio_, tp2_

    def ax_step(net, idx_, when):
        """position-level adaptive exit 상태 전이. **완성봉에서만** 호출한다
        (bars.iloc[:idx_+1] 전부 완성봉). 미래값 없음."""
        if ax is None or position is None:
            return
        st = position["ax"]
        if st["mode"] is None and net >= float(ax["decide"]):
            held_ = bt._direction_for_symbol(position["symbol"])
            if ax.get("cond") == "placebo":
                # 위약 판정 — 구조를 보지 않고 (진입키, seed) 해시로 같은 비율만큼
                # WEAK 을 찍는다. 규칙의 우위가 판정력에서 오는지 분포에서 오는지 가른다.
                import hashlib
                _h = hashlib.md5(f"{ax.get('seed')}|{position['rec'].date}|"
                                 f"{position['rec'].entry_time}".encode()).digest()
                ok = (int.from_bytes(_h[:4], "big") / 2**32) >= float(ax.get("p_weak", 0.35))
            else:
                ok = _snap_ok(snap_table(bars), idx_, held_)
            st["mode"] = "STRONG" if ok else "WEAK"
            ov = dict(ax.get("strong" if ok else "weak") or {})
            st["tp2"] = ov.get("tp2")
            st["floor"] = ov.get("floor")
            st["close"] = bool(ov.get("close"))
            rec_ = position["rec"]
            rec_.ax_mode = st["mode"]
            rec_.ax_at = pd.Timestamp(when).isoformat()
            rec_.ax_net = round(float(net), 4)
        rm = ax.get("ratchet_mode")
        if ax.get("ratchet") and (rm is None or st["mode"] == rm):
            pk = max(float(position["peak"]), float(net))
            for lv, fl in ax["ratchet"]:
                if pk >= float(lv):
                    st["floor"] = fl if st["floor"] is None else max(st["floor"], fl)

    def ax_apply(tp2_, kw_):
        """판정 결과를 래더 인자에 반영. 판정 전이면 N1 그대로."""
        if ax is None or position is None:
            return tp2_, kw_
        st = position["ax"]
        if st["tp2"] is not None:
            tp2_ = float(st["tp2"])
        if st["floor"] is not None:
            kw_ = dict(kw_)
            kw_["trailing_stop_pct_override"] = float(st["floor"])
            kw_["after_tp1_stop_pct_override"] = float(st["floor"])
        return tp2_, kw_

    def note_ms(net, when, idx_):
        """+3/+4/+5% 최초 도달 기록. 관측 전용 — 체결/판정에 관여하지 않는다."""
        if milestones is None or position is None:
            return
        rec_ = position["rec"]
        for lv in ms_levels:
            if net >= lv and lv not in position["ms"]:
                position["ms"].add(lv)
                milestones.append({
                    "date": rec_.date, "entry_time": rec_.entry_time,
                    "session": rec_.session, "direction": rec_.direction,
                    "symbol": rec_.entry_symbol, "level": lv, "idx": int(idx_),
                    "at": pd.Timestamp(when).isoformat(),
                    "net": float(net), "mae_so_far": float(rec_.mae_net_pct),
                    "mins": round((pd.Timestamp(when) - pd.Timestamp(rec_.entry_time)).total_seconds() / 60.0, 2),
                    "bars": int(idx_ - rec_.entry_bar_idx),
                    "tp1_done": bool(position["tp1_done"]),
                    "entry_idx": int(rec_.entry_bar_idx),
                })

    def position_direction():
        return bt._direction_for_symbol(position["symbol"]) if position is not None else None

    def fill_at(symbol, recognition_time):
        return quotes[symbol].at(recognition_time)

    def net_at(price):
        return float(_net_return_pct(position["symbol"], position["rec"].entry_price, price, 1))

    nonlocal_day = [0.0]

    def close_trade(exit_time, exit_price, reason, idx):
        nonlocal w1a_first_stop
        rec = position["rec"]
        rec.exit_time = ce._fmt(exit_time)
        rec.exit_price = exit_price
        rec.exit_reason = reason
        leg = net_at(exit_price)
        rec.net_pct = round(position["realized"] + position["qty_frac"] * leg, 6)
        rec.hold_bars = int(idx - rec.entry_bar_idx)
        try:
            rec.hold_minutes = round(
                (pd.Timestamp(exit_time) - pd.Timestamp(rec.entry_time)).total_seconds() / 60.0, 2)
        except Exception:
            rec.hold_minutes = 0.0
        position["legs"].append((ce._fmt(exit_time), exit_price,
                                 round(position["qty_frac"], 6), reason, round(leg, 4)))
        rec.legs = list(position["legs"])
        trades.append(rec)
        nonlocal_day[0] += float(rec.net_pct or 0.0) * float(rec.w1a or 0.0)
        if w1a_seq == 1 and str(reason or "") == config.EXIT_TW_STOP_LOSS:
            w1a_first_stop = True
        if _gx:
            if _gx_day["first_loss"] is None:
                _gx_day["first_loss"] = bool((rec.net_pct or 0.0) <= 0)
            if (rec.net_pct or 0.0) <= 0:
                _gx_day["streak"] += 1
            else:
                _gx_day["streak"] = 0

    # D3b 의 TP1 이동만 모듈 상수라 런 단위로 잡아 둔다 (adaptive 는 D3a 만
    # 쓰므로 tp1 이동이 없어 항상 None 이다).
    _tp1_trend = float(_dcfg_fixed.get("tp1")) if _dcfg_fixed.get("tp1") else None
    with _Patched(p):
        _tp1_base = twpm.MORNING_TP1_PCT
        for idx in range(len(bars)):
            bar_ts = bars["datetime"].iloc[idx]
            bar_start = pd.Timestamp(bar_ts).to_pydatetime()
            day_key = bar_start.strftime("%Y%m%d")
            if day_key not in date_set:
                continue
            if day_key != current_day:
                current_day = day_key
                slots_used_today = morning_count = afternoon_count = 0
                flag_ordinal_today = 0
                last_afternoon_direction = None
                pending = None
                whipsaw_watch = None
                hold = None
                rhold = None
                w1a_seq = 0
                day_entry_seq = 0
                d_relax_used = False
                day_chop_seen = False
                rhold = None
                w1a_first_stop = False
                w1a_exposure = 0.0
                nonlocal_day[0] = 0.0
                _gx_day["d"] = day_key
                _gx_day["n_ord"] = 0
                _gx_day["first_loss"] = None
            if position is None:
                hold = _clr(hold)
                rhold = None
                whipsaw_watch = None
            recognition_at = bar_start + timedelta(minutes=3)

            if position is not None and recognition_at.astimezone(KST).time() >= config.FORCE_LIQUIDATE_AT:
                px = fill_at(position["symbol"], recognition_at)
                if px is not None:
                    close_trade(recognition_at, px, config.EXIT_FORCED_LIQUIDATION, idx)
                    position = None
                pending = None
                whipsaw_watch = None
                hold = _clr(hold)
                rhold = None

            # -- CX: CHOP MODE 진입 (연구 전용) --------------------------
            # 슬롯/일예산/세션/동일방향 규칙은 tw3.resolve_slot 으로 그대로 탄다.
            # M0: 같은 봉에 N1 진입이 예정돼 있으면 양보 / M1: N1 신규진입 차단
            # 상태에서 CHOP 만 / M2: CHOP 우선.
            if _cx and _CXF is not None and position is None:
                _n1_due = (pending is not None and idx == pending[1] + 1)
                _mode = _cx.get("interact", "M0")
                _may = (_mode == "M2") or (_mode == "M1") or (not _n1_due)
                if _may and _cx_regime():
                    _t = recognition_at.astimezone(KST).time()
                    if config.SESSION_OPEN <= _t < config.NEW_ENTRY_CUTOFF:
                        _d = _CX.evaluate(_cx["strategy"], _CXF, idx, _cx.get("params", {}))
                        if _d is not None:
                            _sd = tw3.resolve_slot(
                                now=recognition_at, slots_used_today=slots_used_today,
                                morning_count=morning_count, afternoon_count=afternoon_count,
                                direction=_d, is_flat=True,
                                last_afternoon_direction=last_afternoon_direction)
                            if _sd.slot_allowed:
                                _tg = order_executor.target_symbol_for_direction(_d)
                                _fl = fill_at(_tg, recognition_at)
                                if _fl is not None:
                                    slots_used_today += 1
                                    if _sd.session == tw3.SESSION_MORNING:
                                        morning_count += 1
                                    else:
                                        afternoon_count += 1
                                        last_afternoon_direction = _d.value
                                    _m = _w1a_multiplier(entry_chop=True,
                                                         first_stop=w1a_first_stop,
                                                         exposure=w1a_exposure, extra=1.0)
                                    w1a_exposure += _m
                                    w1a_seq += 1
                                    _rec = Trade(date=current_day, slot_number=_sd.slot_number,
                                                 session=_sd.session, direction=_d.value,
                                                 decision_idx=idx, flag_ordinal=0,
                                                 entry_time=recognition_at.isoformat(),
                                                 entry_symbol=_tg, entry_price=_fl,
                                                 entry_bar_idx=idx + 1, entry_chop=True,
                                                 chop_score=0, tq=None, w1a=_m)
                                    _rec.cx_strategy = str(_cx["strategy"])
                                    _rec.cx_on = True
                                    position = {"symbol": _tg, "entry_idx": idx + 1, "mode": "",
                                                "entry_time": recognition_at, "tp1_done": False,
                                                "peak": 0.0, "session": _sd.session, "rec": _rec,
                                                "qty_frac": 1.0, "realized": 0.0,
                                                "runner_on": False, "tp2_part": False,
                                                "legs": [], "ms": set(),
                                                "ax": {"mode": None, "tp2": None, "floor": None,
                                                       "brk": 0, "brk_idx": None, "pend": None,
                                                       "flip": None},
                                                "regime_eligible": True, "cx": True}
                                    whipsaw_watch = None
                                    hold = _clr(hold)
                                    rhold = None
                                    _cx_log.append({"kind": "CX_ENTRY", "date": current_day,
                                                    "at": recognition_at.isoformat(),
                                                    "strategy": _cx["strategy"],
                                                    "direction": _d.value,
                                                    "slot": _sd.slot_number,
                                                    "slots_used": slots_used_today,
                                                    "n1_due_same_bar": bool(_n1_due)})

            if pending is not None:
                p_direction, p_idx, p_bar_ts = pending
                if idx == p_idx + 1:
                    pending = None
                    flag_ordinal_today += 1
                    flag_ord = flag_ordinal_today
                    flag_bar_dt = pd.Timestamp(p_bar_ts).to_pydatetime()
                    bars_slice = bars.iloc[: idx + 1]
                    slot_number = session = None
                    pos_dir = position_direction()
                    pos_key = None if pos_dir is None else pos_dir.value

                    base_decision = _memo(
                        "base", (p_idx, pos_key),
                        lambda: twf.evaluate_time_window_entry(
                            bars_slice, p_direction, flag_bar_dt, recognition_at,
                            position_direction=pos_dir,
                            morning_entry_count=0, afternoon_entry_count=0,
                            daily_entry_count=0))
                    # ── 조건부 진입완화 (2026-09-18 연구) ────────────────
                    #   C: 상위추세 정렬(tregime.snapshot.ok)일 때만
                    #   D: 방향성 불명확한 장 이후 "첫 정렬 플래그" 1회만
                    #   완화 범위는 quality 4->3 뿐. VWAP/hard veto/세션/T+3 불변.
                    _relax_tag = ""
                    if relax_mode:
                        _cd0 = _memo("chop", p_idx,
                                     lambda: etp.evaluate_entry_chop(bars_slice, p_direction, recognition_at))
                        if bool(_cd0.is_chop) and not _cd0.insufficient_data:
                            day_chop_seen = True
                        if relax_mode == "B":
                            _relax_tag = "B"
                        else:
                            _aligned = bool(tr.snapshot(bars_slice, p_direction).ok)
                            _firstdir = (_aligned and not d_relax_used
                                         and (flag_ord >= 3 or day_chop_seen))
                            if relax_mode in ("C", "CD") and _aligned:
                                _relax_tag = "C"
                            if relax_mode in ("D", "CD") and _firstdir and not _relax_tag:
                                _relax_tag = "D"
                            if relax_mode == "CD" and _firstdir and _relax_tag == "C":
                                _relax_tag = "CD"

                    if (_relax_tag and quality_relax
                            and not base_decision.approved
                            and base_decision.block_reason == config.TW_REJECT_LOW_QUALITY_SCORE):
                        _old_thr = config.QUALITY_SCORE_THRESHOLD
                        try:
                            config.QUALITY_SCORE_THRESHOLD = 3
                            _bd3 = _memo(
                                "base_q3", (p_idx, pos_key),
                                lambda: twf.evaluate_time_window_entry(
                                    bars_slice, p_direction, flag_bar_dt, recognition_at,
                                    position_direction=pos_dir,
                                    morning_entry_count=0, afternoon_entry_count=0,
                                    daily_entry_count=0))
                        finally:
                            config.QUALITY_SCORE_THRESHOLD = _old_thr
                        if _bd3.approved:
                            base_decision = _bd3
                            _relax_hit = "Q:" + _relax_tag
                        else:
                            _relax_hit = ""
                    else:
                        _relax_hit = ""

                    bypass = False
                    if morning_only_bypass:
                        bypass = (
                            base_decision.block_reason == config.TW_REJECT_TIME_WINDOW
                            and (base_decision.metrics or {}).get("window") in (
                                twf.WINDOW_AFTERNOON_1, twf.WINDOW_AFTERNOON_2)
                            and recognition_at.astimezone(KST).time()
                            < config.TW_AFTERNOON_ENTRY_HARD_CUTOFF)
                    tw2_cleared = bool(base_decision.approved or bypass)
                    base_reason = base_decision.block_reason
                    if tw2_cleared:
                        vetoed_x, vr = _memo(
                            "veto", p_idx,
                            lambda: twf.evaluate_tw2_extra_vetoes(
                                bars_slice, p_direction, flag_bar_dt, recognition_at))
                        if vetoed_x:
                            tw2_cleared, base_reason = False, vr

                    final_approved = False
                    final_reason = base_reason
                    tq_score = None
                    if tw2_cleared:
                        sd = tw3.resolve_slot(
                            now=recognition_at, slots_used_today=slots_used_today,
                            morning_count=morning_count, afternoon_count=afternoon_count,
                            direction=p_direction, is_flat=(position is None),
                            last_afternoon_direction=last_afternoon_direction)
                        slot_number, session = sd.slot_number, sd.session
                        if not sd.slot_allowed:
                            final_reason = sd.reject_reason
                        elif sd.requires_quality_gate:
                            q = _memo("tq", p_idx,
                                      lambda: tw3.evaluate_trend_quality(bars_slice, p_direction))
                            tq_score = int(getattr(q, "passed_count", 0))
                            final_approved = q.approved
                            final_reason = (config.TW_APPROVED if q.approved
                                            else config.TW2_3SLOT_REJECT_QUALITY)
                        elif sd.requires_teg_gate:
                            t = _memo("teg", p_idx,
                                      lambda: teg_gate.evaluate_teg(bars_slice, p_direction,
                                                                    flag_bar_dt, recognition_at))
                            final_approved = t.approved
                            final_reason = (config.TW_APPROVED if t.approved
                                            else config.TW2_3SLOT_REJECT_TEG)
                            # E: C/D 조건이 참일 때 TEG 거절만 soft-pass
                            if (not t.approved) and teg_soft and _relax_tag:
                                final_approved = True
                                final_reason = config.TW_APPROVED
                                _relax_hit = ("TEG:" + _relax_tag) if not _relax_hit else _relax_hit + "+TEG"
                                if _relax_tag in ("D", "CD"):
                                    d_relax_used = True
                        else:
                            final_approved = True
                            final_reason = config.TW_APPROVED

                    cd = _memo("chop", p_idx,
                               lambda: etp.evaluate_entry_chop(bars_slice, p_direction, recognition_at))
                    entry_chop = bool(cd.is_chop) and not cd.insufficient_data
                    chop_score = 0 if cd.insufficient_data else int(cd.score)

                    if gate is not None and final_approved:
                        g = gate({"date": current_day, "decision_at": recognition_at,
                                  "direction": p_direction, "session": session,
                                  "slot_number": slot_number, "idx": idx, "flag_idx": p_idx,
                                  "bars_slice": bars_slice, "flag_bar_dt": flag_bar_dt,
                                  "entry_chop": entry_chop, "chop_score": chop_score})
                        if g is not None:
                            final_approved = False
                            final_reason = g

                    target = order_executor.target_symbol_for_direction(p_direction)
                    if events is not None:
                        events.append({"date": current_day, "kind": "DECISION",
                                       "at": recognition_at.isoformat(),
                                       "direction": p_direction.value,
                                       "approved": bool(final_approved),
                                       "reason": final_reason,
                                       "slot": slot_number, "session": session,
                                       "flat": position is None})

                    # ── H50: production worker.py 의 위치와 동일 (승인/거절
                    #    분기보다 앞, 보유 반대방향일 때만) ──────────────────
                    # ── Trend Regime Hold (C, 2026-09-17 연구) ─────────────
                    # 상위추세가 살아 있는 동안에는 중간 반대 플래그를 청산
                    # 신호로 쓰지 않는다. H50 과 같은 자리(승인/거절 분기 앞)라
                    # 승인이면 switch, 거절이면 exit-only 인 두 경우를 한 번에
                    # 막는다. 반대방향 신규진입도 여기서 같이 막힌다.
                    regime_handled = False
                    if (regime_release and position is not None
                            and position["symbol"] != target
                            and position.get("regime_eligible")):
                        _held = bt._direction_for_symbol(position["symbol"])
                        _rs = tr.snapshot(bars_slice, _held)
                        if _rs.ok:
                            regime_handled = True
                            position["rec"].ignored_flags += 1
                            if rhold is None:
                                rhold = {"dir": _held, "started_at": recognition_at,
                                         "breaks": 0, "last_idx": None}
                                position["rec"].regime_hold = True
                                if events is not None:
                                    events.append({
                                        "date": current_day, "kind": "REGIME_HOLD",
                                        "at": recognition_at.isoformat(),
                                        "opposite": p_direction.value, "held": _held.value,
                                        "close": _rs.close, "ema20": _rs.ema_fast,
                                        "ema50": _rs.ema_slow, "ema50_slope": _rs.ema_slow_slope})

                    h50_handled = False
                    if h50_on(position) and position is not None and position["symbol"] != target:
                        held_dir = bt._direction_for_symbol(position["symbol"])
                        hd = _memo("h50h", (idx, held_dir.value),
                                   lambda: swh.evaluate_hold(bars_slice, held_dir, recognition_at))
                        if hd.should_hold:
                            h50_handled = True
                            position["rec"].h50_held = True
                            # production 은 이 분기에서 매 반대 플래그마다
                            # re-seed 한다("every"). "first" 는 최초 HOLD 때
                            # 한 번만 arm 하는 대안이다.
                            if h50_watch_link == "every":
                                seed = twf.evaluate_whipsaw_watch(
                                    bars_slice, p_direction, float("-inf"), float("-inf"))
                                whipsaw_watch = {
                                    "direction": p_direction,
                                    "last_gap": (seed.current_gap if not seed.insufficient_data else 0.0),
                                    "last_ema_spread": (seed.current_ema_spread
                                                        if not seed.insufficient_data else 0.0),
                                    "seeded_idx": idx, "origin": "H50"}
                            if hold is None:
                                hold = {"dir": held_dir, "started_at": recognition_at,
                                        "breaks": 0, "last_idx": None}
                                position["rec"].h50_hold_at = recognition_at.isoformat()
                                if events is not None:
                                    events.append({"date": current_day, "kind": "H50_HOLD",
                                                   "at": recognition_at.isoformat(),
                                                   "opposite": p_direction.value,
                                                   "held": held_dir.value,
                                                   "trend": hd.trend,
                                                   "range_pct": hd.range_pct})
                                if h50_watch_link == "first":
                                    # production _start_whipsaw_watch 와 동일한 seed
                                    seed = twf.evaluate_whipsaw_watch(
                                        bars_slice, p_direction, float("-inf"), float("-inf"))
                                    whipsaw_watch = {
                                        "direction": p_direction,
                                        "last_gap": (seed.current_gap if not seed.insufficient_data else 0.0),
                                        "last_ema_spread": (seed.current_ema_spread
                                                            if not seed.insufficient_data else 0.0),
                                        "seeded_idx": idx,
                                        "origin": "H50",
                                    }
                                    if events is not None:
                                        events.append({"date": current_day, "kind": "H50_WATCH_ARMED",
                                                       "at": recognition_at.isoformat(),
                                                       "gap": whipsaw_watch["last_gap"],
                                                       "spread": whipsaw_watch["last_ema_spread"]})

                    if regime_handled or h50_handled:
                        pass
                    elif not final_approved:
                        if position is not None and position["symbol"] != target:
                            if final_reason in config.TW_WHIPSAW_REJECT_REASONS:
                                if watch_seed_fix:
                                    seed = twf.evaluate_whipsaw_watch(
                                        bars_slice, p_direction, float("-inf"), float("-inf"))
                                    whipsaw_watch = {
                                        "direction": p_direction,
                                        "last_gap": (seed.current_gap if not seed.insufficient_data else 0.0),
                                        "last_ema_spread": (seed.current_ema_spread
                                                            if not seed.insufficient_data else 0.0),
                                        "seeded_idx": idx, "origin": "TW"}
                                else:
                                    whipsaw_watch = {"direction": p_direction,
                                                     "last_gap": float("-inf"),
                                                     "last_ema_spread": float("-inf"),
                                                     "seeded_idx": None, "origin": "TW"}
                            else:
                                cn = fill_at(position["symbol"], recognition_at)
                                if cn is None:
                                    cn = position["rec"].entry_price
                                close_trade(recognition_at, cn, config.EXIT_OPPOSITE_SIGNAL, idx)
                                position = None
                                hold = _clr(hold)
                                rhold = None
                    elif (day_loss_stop is not None and position is None
                          and nonlocal_day[0] <= float(day_loss_stop)):
                        # 일손실 한도 — 그날 실현손익이 한도 이하면 신규진입만 금지.
                        # 슬롯도 소비하지 않고, 보유 중이면 개입하지 않는다.
                        if events is not None:
                            events.append({"date": current_day, "kind": "DAY_LOSS_BLOCK",
                                           "at": recognition_at.isoformat(),
                                           "realized": nonlocal_day[0]})
                    elif (_cx and _cx.get("interact") == "M1" and _cx_regime()
                          and position is None):
                        _cx_log.append({"kind": "N1_SUPPRESSED_M1", "date": current_day,
                                        "at": recognition_at.isoformat(),
                                        "direction": p_direction.value})
                    elif (_gx and _gx_is_chop(idx) and position is None
                          and _gx_skip(idx, recognition_at, current_day, p_direction,
                                       slot_number, session)):
                        _gx_log.append({"kind": "GX_SKIP", "date": current_day,
                                        "at": recognition_at.isoformat(),
                                        "direction": p_direction.value,
                                        "slot": slot_number, "session": session,
                                        "rule": _gx.get("skip", {}).get("rule", "")})
                    else:
                        if (_cx and position is not None and position.get("cx")):
                            _cx_log.append({"kind": "N1_BLOCKED_BY_CX", "date": current_day,
                                            "at": recognition_at.isoformat(),
                                            "direction": p_direction.value,
                                            "cx_symbol": position["symbol"],
                                            "cx_strategy": position["rec"].cx_strategy})
                        fill = fill_at(target, recognition_at)
                        if fill is not None:
                            if position is not None and position["symbol"] != target:
                                cn = fill_at(position["symbol"], recognition_at)
                                if cn is None:
                                    cn = position["rec"].entry_price
                                close_trade(recognition_at, cn, config.EXIT_OPPOSITE_SIGNAL, idx)
                                position = None
                                hold = _clr(hold)
                                rhold = None
                            if position is None:
                                slots_used_today += 1
                                if session == tw3.SESSION_MORNING:
                                    morning_count += 1
                                else:
                                    afternoon_count += 1
                                    last_afternoon_direction = p_direction.value
                                _ex = 1.0
                                if slot_mult:
                                    _ex = float(slot_mult.get(slot_number, 1.0))
                                mult = _w1a_multiplier(entry_chop=entry_chop,
                                                       first_stop=w1a_first_stop,
                                                       exposure=w1a_exposure, extra=_ex)
                                _rx_on, _rx_h, _rx_q = _rx_state()
                                _rx_sz = float(_rx.get("size", 1.0)) if _rx_on else 1.0
                                if _rx.get("size_mode") == "redist":
                                    mult *= _rx_sz
                                    w1a_exposure += mult
                                else:
                                    w1a_exposure += mult
                                    mult *= _rx_sz
                                w1a_seq += 1
                                rec = Trade(date=current_day, slot_number=slot_number,
                                            session=session, direction=p_direction.value,
                                            decision_idx=idx, flag_ordinal=flag_ord,
                                            entry_time=recognition_at.isoformat(),
                                            entry_symbol=target, entry_price=fill,
                                            entry_bar_idx=idx + 1, entry_chop=entry_chop,
                                            chop_score=chop_score, tq=tq_score, w1a=mult,
                                            relax=_relax_hit)
                                if _gx:
                                    rec.gx_chop = bool(_gx_is_chop(idx))
                                    _fn = _gx.get("is_ar1")
                                    try:
                                        rec.gx_ar1 = bool(_fn(current_day, recognition_at)) if _fn else False
                                    except Exception:
                                        rec.gx_ar1 = False
                                    if not rec.gx_ar1:
                                        _gx_day["n_ord"] += 1
                                rec.rx_on = bool(_rx_on)
                                rec.rx_h50r = _rx_h
                                rec.rx_tp1r = _rx_q
                                rec.rx_size_mult = _rx_sz
                                _rs = tr.snapshot(bars_slice, p_direction)
                                rec.trend_at_entry = bool(_rs.ok)
                                rec.e_close = _rs.close
                                rec.e_ema20 = _rs.ema_fast
                                rec.e_ema50 = _rs.ema_slow
                                rec.e_slope = _rs.ema_slow_slope
                                if _relax_hit and _relax_tag in ("D", "CD"):
                                    d_relax_used = True
                                day_entry_seq += 1
                                # R1 = 그날 첫 진입 이후 모든 포지션에 적용
                                # R2 = 그날 **첫 진입**이 09:00~11:00 에 있었던
                                #      경우 그 포지션에만 적용
                                _t = recognition_at.astimezone(KST).time()
                                if regime_scope == "R2":
                                    _elig = (day_entry_seq == 1
                                             and dtime(9, 0) <= _t < dtime(11, 0))
                                else:
                                    _elig = True
                                rec.regime_eligible = bool(_elig)
                                # ── V4 Adaptive: **flat 에서 새 포지션을 여는 이
                                # 순간에만** regime 을 보고 전략을 고른다. 고른
                                # 전략은 이 포지션이 닫힐 때까지 고정된다(장중
                                # 전환 금지). 미래정보 없음 -- bars_slice 는 판정
                                # 시점까지의 완성봉뿐이다.
                                _mode = ""
                                if adaptive_default:
                                    _pick = ad.pick_mode(bars_slice, p_direction,
                                                         recognition_at,
                                                         default=adaptive_default)
                                    _mode = _pick.mode
                                    rec.mode = _pick.mode
                                    rec.mode_reason = _pick.reason
                                    if events is not None:
                                        events.append({"date": current_day, "kind": "PICK",
                                                       "at": recognition_at.isoformat(),
                                                       "mode": _pick.mode, "reason": _pick.reason,
                                                       "direction": p_direction.value,
                                                       "range_pct": _pick.range_pct,
                                                       "aligned": _pick.aligned_bars,
                                                       "close_side": _pick.close_side_bars})
                                position = {"symbol": target, "entry_idx": idx + 1,
                                            "mode": _mode,
                                            "entry_time": recognition_at, "tp1_done": False,
                                            "peak": 0.0, "session": session, "rec": rec,
                                            "qty_frac": 1.0, "realized": 0.0,
                                            "runner_on": False, "tp2_part": False,
                                            "legs": [], "ms": set(),
                                            "ax": {"mode": None, "tp2": None, "floor": None, "brk": 0,
                                                   "brk_idx": None, "pend": None, "flip": None},
                                            "regime_eligible": bool(_elig)}
                                whipsaw_watch = None
                                hold = _clr(hold)
                                rhold = None

            if idx in flags_by_idx:
                ft = bar_start.astimezone(KST).time()
                if config.SESSION_OPEN <= ft < config.NEW_ENTRY_CUTOFF:
                    pending = (flags_by_idx[idx], idx, bar_ts)

            # ── whipsaw-watch 추적 (production _advance_whipsaw_watch) ──────
            if whipsaw_watch is not None and position is not None and not position.get("cx"):
                if whipsaw_watch.get("seeded_idx") == idx:
                    pass   # production: seed 한 봉은 다시 평가하지 않는다
                else:
                    d = twf.evaluate_whipsaw_watch(
                        bars.iloc[: idx + 1], whipsaw_watch["direction"],
                        whipsaw_watch["last_gap"], whipsaw_watch["last_ema_spread"])
                    if not d.insufficient_data:
                        if d.should_release:
                            whipsaw_watch = None
                        elif d.should_sell:
                            px = fill_at(position["symbol"], recognition_at)
                            if px is not None:
                                close_trade(recognition_at, px,
                                            "WHIPSAW_WATCH_DETERIORATION_EXIT", idx)
                                if events is not None:
                                    events.append({"date": current_day, "kind": "WATCH_EXIT",
                                                   "at": recognition_at.isoformat(),
                                                   "gap": d.current_gap,
                                                   "origin": whipsaw_watch.get("origin")})
                                position = None
                                hold = _clr(hold)
                                rhold = None
                            whipsaw_watch = None
                        else:
                            whipsaw_watch["last_gap"] = d.current_gap
                            whipsaw_watch["last_ema_spread"] = d.current_ema_spread

            # ── H50 HOLD 해제 (production _advance_h50_hold) ────────────────
            if h50_on(position) and hold is not None and position is not None and not position.get("cx"):
                if hold["last_idx"] is None or idx > hold["last_idx"]:
                    hold["last_idx"] = idx
                    rd = swh.evaluate_release(bars.iloc[: idx + 1], hold["dir"],
                                              hold["started_at"], recognition_at,
                                              trend_break_count=hold["breaks"])
                    hold["breaks"] = rd.trend_break_count
                    if rd.should_release:
                        px = fill_at(position["symbol"], recognition_at)
                        if px is not None:
                            close_trade(recognition_at, px,
                                        swh.EXIT_SMALL_WHIPSAW_HOLD, idx)
                            if events is not None:
                                events.append({"date": current_day, "kind": "H50_EXIT",
                                               "at": recognition_at.isoformat(),
                                               "reason": rd.reason})
                            position = None
                            whipsaw_watch = None
                        hold = None
                        rhold = None

            # ── Trend Regime Hold 해제 (완성봉마다) ────────────────────────
            # 하드스톱/TP/트레일링/ETP/강제청산은 이 블록 뒤(틱 익절 + 완성봉
            # 래더)에서 평가되지만, 엔진의 기존 관례상 청산 우선순위는 별도로
            # 감사한다(report 의 exit_reason 분포 참조).
            if regime_release and rhold is not None and position is not None and not position.get("cx"):
                if rhold["last_idx"] is None or idx > rhold["last_idx"]:
                    rhold["last_idx"] = idx
                    _rel, _cnt, _why = tr.should_release(
                        bars.iloc[: idx + 1], rhold["dir"], regime_release, rhold["breaks"])
                    rhold["breaks"] = _cnt
                    if _rel:
                        px = fill_at(position["symbol"], recognition_at)
                        if px is not None:
                            close_trade(recognition_at, px, "TREND_REGIME_HOLD_EXIT", idx)
                            if events is not None:
                                events.append({"date": current_day, "kind": "REGIME_EXIT",
                                               "at": recognition_at.isoformat(), "reason": _why})
                            position = None
                            whipsaw_watch = None
                            hold = None
                        rhold = None

            # -- CX: CHOP 포지션 전용 청산 (자체 TP/SL/maxhold). N1 래더 미사용.
            if _cx and position is not None and position.get("cx"):
                for mo in range(3):
                    tick = bar_start + timedelta(minutes=mo)
                    if tick <= position["entry_time"] or tick > recognition_at:
                        continue
                    price = quotes[position["symbol"]].exact.get(pd.Timestamp(tick))
                    if price is None:
                        continue
                    net = net_at(price)
                    if net > position["rec"].peak_net_pct:
                        position["rec"].peak_at = pd.Timestamp(tick).isoformat()
                    position["rec"].peak_net_pct = max(position["rec"].peak_net_pct, net)
                    position["rec"].mae_net_pct = min(position["rec"].mae_net_pct, net)
                    position["peak"] = max(position["peak"], net)
                    _q = _cx.get("partial")
                    if _q and not position["rec"].cx_part_done and net >= float(_q["at"]):
                        _r = float(_q.get("ratio", 0.5))
                        position["realized"] += position["qty_frac"] * _r * net
                        position["legs"].append((ce._fmt(tick), price,
                                                 round(position["qty_frac"] * _r, 6),
                                                 "CX_PARTIAL", round(net, 4)))
                        position["qty_frac"] *= (1.0 - _r)
                        position["rec"].cx_part_done = True
                    _hm = (tick - position["entry_time"]).total_seconds() / 60.0
                    _why = None
                    if net >= float(_cx["tp"]):
                        _why = "CX_TP"
                    elif net <= -float(_cx["sl"]):
                        _why = "CX_SL"
                    elif _hm >= float(_cx["maxmin"]):
                        _why = "CX_MAXHOLD"
                    elif _cx.get("vwap_exit") and _CXF is not None:
                        _vd = _CXF["vwap_dist"][idx]
                        if (position["rec"].direction == "UP_RED" and _vd >= 0) or (position["rec"].direction == "DOWN_BLUE" and _vd <= 0):
                            _why = "CX_VWAP"
                    if _why:
                        close_trade(tick, price, _why, idx)
                        position = None
                        whipsaw_watch = None
                        hold = _clr(hold)
                        rhold = None
                        break

            # -- GX: CHOP 진입 거래 전용 청산 (축 B) --
            _gxe = _gx.get("exit") if _gx else None
            if (_gxe and position is not None and not position.get("cx")
                    and position["rec"].gx_chop):
                for mo in range(3):
                    tick = bar_start + timedelta(minutes=mo)
                    if tick <= position["entry_time"] or tick > recognition_at:
                        continue
                    price = quotes[position["symbol"]].exact.get(pd.Timestamp(tick))
                    if price is None:
                        continue
                    net = net_at(price)
                    if net > position["rec"].peak_net_pct:
                        position["rec"].peak_at = pd.Timestamp(tick).isoformat()
                    position["rec"].peak_net_pct = max(position["rec"].peak_net_pct, net)
                    position["rec"].mae_net_pct = min(position["rec"].mae_net_pct, net)
                    position["peak"] = max(position["peak"], net)
                    _pk = position["peak"]
                    _q = _gxe.get("partial")
                    if _q and not position["rec"].gx_part_done and net >= float(_q["at"]):
                        _r = float(_q.get("ratio", 0.5))
                        position["realized"] += position["qty_frac"] * _r * net
                        position["legs"].append((ce._fmt(tick), price,
                                                 round(position["qty_frac"] * _r, 6),
                                                 "GX_PARTIAL", round(net, 4)))
                        position["qty_frac"] *= (1.0 - _r)
                        position["rec"].gx_part_done = True
                    _hm = (tick - position["entry_time"]).total_seconds() / 60.0
                    _why = None
                    if _gxe.get("tp") is not None and net >= float(_gxe["tp"]):
                        _why = "GX_TP"
                    elif _gxe.get("sl") is not None and net <= -float(_gxe["sl"]):
                        _why = "GX_SL"
                    elif (_gxe.get("be_after") is not None
                          and _pk >= float(_gxe["be_after"]) and net <= 0.0):
                        _why = "GX_BREAKEVEN"
                    elif _gxe.get("maxmin") is not None and _hm >= float(_gxe["maxmin"]):
                        _why = "GX_MAXHOLD"
                    if _why:
                        close_trade(tick, price, _why, idx)
                        position = None
                        whipsaw_watch = None
                        hold = _clr(hold)
                        rhold = None
                        break

            # 틱 익절
            if position is not None and not position.get("cx") and not (
                    _gx and _gx.get("exit", {}).get("mode") == "replace"
                    and position["rec"].gx_chop):
                for mo in range(3):
                    tick = bar_start + timedelta(minutes=mo)
                    if tick <= position["entry_time"] or tick > recognition_at:
                        continue
                    price = quotes[position["symbol"]].exact.get(pd.Timestamp(tick))
                    if price is None:
                        continue
                    net = net_at(price)
                    if net > position["rec"].peak_net_pct:
                        position["rec"].peak_at = pd.Timestamp(tick).isoformat()
                    position["rec"].peak_net_pct = max(position["rec"].peak_net_pct, net)
                    position["rec"].mae_net_pct = min(position["rec"].mae_net_pct, net)
                    note_ms(net, tick, idx)
                    _cfg = dcfg_of(position)
                    _tr = trend_ok_at(idx, position["symbol"], bool(_cfg))
                    _kw, _ratio, _tp2 = ladder_kw(_tr, _cfg)
                    _tp2, _kw = ax_apply(_tp2, _kw)
                    _tr_run = trend_ok_at(idx, position["symbol"], True) if runner else False
                    _e = eff_cfg(_tr, _cfg)
                    twpm.MORNING_TP1_PCT = (float(_e["tp1"]) if _e.get("tp1") else _tp1_base)
                    tp = twpm.evaluate_take_profit_immediate(
                        session=position["session"], net_return_pct=net,
                        tp1_done=position["tp1_done"], tp2_pct_override=_tp2,
                        tp1_sell_ratio_override=_ratio)
                    twpm.MORNING_TP1_PCT = _tp1_base
                    position["peak"] = max(position["peak"], tp.peak_net_return)
                    if tp.exit_reason == config.EXIT_TW_TP1_PARTIAL:
                        if tp.sell_fraction >= 1.0:
                            close_trade(tick, price, config.EXIT_TW_TP1_PARTIAL, idx)
                            position = None
                            whipsaw_watch = None
                            hold = _clr(hold)
                            rhold = None
                            break
                        position["realized"] += position["qty_frac"] * tp.sell_fraction * net
                        position["qty_frac"] *= (1.0 - tp.sell_fraction)
                        position["tp1_done"] = tp.tp1_done
                        position["rec"].tp1_hit = True
                    elif (runner and _tr_run
                          and tp.exit_reason == config.EXIT_TW_TP2_FULL):
                        # Trend Runner: 추세가 살아 있는 동안 TP2 전량청산을 보류한다.
                        # 하드스톱/강제청산/반대신호/트레일링은 그대로 살아 있다.
                        position["runner_on"] = True
                        position["rec"].runner_used = True
                        if runner == "R3" and not position["tp2_part"]:
                            position["realized"] += position["qty_frac"] * 0.5 * net
                            position["legs"].append((ce._fmt(tick), price, 0.5,
                                                     "RUNNER_TP2_HALF", round(net, 4)))
                            position["qty_frac"] *= 0.5
                            position["tp2_part"] = True
                    elif tp.exit_reason is not None:
                        close_trade(tick, price, tp.exit_reason, idx)
                        position = None
                        whipsaw_watch = None
                        hold = _clr(hold)
                        rhold = None
                        break
                    else:
                        position["tp1_done"] = tp.tp1_done

            # 완성봉 래더
            if (position is not None and idx > position["entry_idx"]
                    and bar_ts in complete.get(position["symbol"], set())):
                px = fill_at(position["symbol"], recognition_at)
                if px is not None:
                    net = net_at(px)
                    if net > position["rec"].peak_net_pct:
                        position["rec"].peak_at = pd.Timestamp(recognition_at).isoformat()
                    position["rec"].peak_net_pct = max(position["rec"].peak_net_pct, net)
                    position["rec"].mae_net_pct = min(position["rec"].mae_net_pct, net)
                    note_ms(net, recognition_at, idx)
                    if paths is not None:
                        _ftb = feat_table(bars)
                        _sg = 1.0 if position["rec"].direction == "UP_RED" else -1.0
                        paths.append({
                            "date": position["rec"].date, "entry_time": position["rec"].entry_time,
                            "idx": int(idx), "at": pd.Timestamp(recognition_at).isoformat(),
                            "net": float(net), "px": float(px),
                            "peak": float(max(position["peak"], net)),
                            "gap": float(_ftb["gap"][idx]) * _sg,
                            "gap_prev": float(_ftb["gap"][idx - 1]) * _sg,
                            "realized": float(position["realized"]),
                            "qty": float(position["qty_frac"])})
                    ax_step(net, idx, recognition_at)
                    if position is not None and position["ax"].get("close"):
                        close_trade(recognition_at, px, "AX_LOCK_EXIT", idx)
                        position = None
                        whipsaw_watch = None
                        hold = _clr(hold)
                        rhold = None
                        continue
                    _cfg = dcfg_of(position)
                    _tr = trend_ok_at(idx, position["symbol"], bool(_cfg))
                    _kw, _ratio, _tp2 = ladder_kw(_tr, _cfg)
                    _tp2, _kw = ax_apply(_tp2, _kw)
                    _tr_run = trend_ok_at(idx, position["symbol"], True) if runner else False
                    # R1/R3: 추세 구조가 깨지면 러너 잔량을 즉시 전량청산한다.
                    if (runner in ("R1", "R3") and position["runner_on"] and not _tr_run):
                        close_trade(recognition_at, px, RUNNER_TREND_BREAK, idx)
                        position = None
                        whipsaw_watch = None
                        hold = _clr(hold)
                        rhold = None
                        continue
                    _e = eff_cfg(_tr, _cfg)
                    twpm.MORNING_TP1_PCT = (float(_e["tp1"]) if _e.get("tp1") else _tp1_base)
                    pm = twpm.evaluate_position(
                        session=position["session"], net_return_pct=net,
                        tp1_done=position["tp1_done"], peak_net_return=position["peak"],
                        tp2_pct_override=_tp2, tp1_sell_ratio_override=_ratio, **_kw)
                    twpm.MORNING_TP1_PCT = _tp1_base
                    position["peak"] = pm.peak_net_return
                    if pm.exit_reason == config.EXIT_TW_TP1_PARTIAL:
                        if pm.sell_fraction >= 1.0:
                            close_trade(recognition_at, px, config.EXIT_TW_TP1_PARTIAL, idx)
                            position = None
                            whipsaw_watch = None
                            hold = _clr(hold)
                            rhold = None
                            continue
                        position["realized"] += position["qty_frac"] * pm.sell_fraction * net
                        position["qty_frac"] *= (1.0 - pm.sell_fraction)
                        position["tp1_done"] = pm.tp1_done
                        position["rec"].tp1_hit = True
                    elif (runner and _tr_run
                          and pm.exit_reason == config.EXIT_TW_TP2_FULL):
                        position["runner_on"] = True
                        position["rec"].runner_used = True
                        if runner == "R3" and not position["tp2_part"]:
                            position["realized"] += position["qty_frac"] * 0.5 * net
                            position["legs"].append((ce._fmt(recognition_at), px, 0.5,
                                                     "RUNNER_TP2_HALF", round(net, 4)))
                            position["qty_frac"] *= 0.5
                            position["tp2_part"] = True
                    elif pm.exit_reason is not None:
                        close_trade(recognition_at, px, pm.exit_reason, idx)
                        position = None
                        whipsaw_watch = None
                        hold = _clr(hold)
                        rhold = None
                    else:
                        position["tp1_done"] = pm.tp1_done
                        # D2 -- TP1 이후 잔량을 EMA20/EMA50 구조가 무너질 때 정리.
                        # 하드스톱/TP2/강제청산이 먼저 걸리면 여기까지 오지 않는다.
                        if (dcfg_of(position).get("runner_until_structure") and position is not None
                                and position["tp1_done"]):
                            _held = bt._direction_for_symbol(position["symbol"])
                            _rel, _c, _why = tr.should_release(
                                bars.iloc[: idx + 1], _held, "C3", 0)
                            if _rel:
                                close_trade(recognition_at, px, "D2_STRUCTURE_EXIT", idx)
                                position = None
                                whipsaw_watch = None
                                hold = None
                                rhold = None
                                continue
                        # ── AX 구조붕괴 청산 (production ETP 와 같은 자리 =
                        #    하드스톱/TP/트레일링이 먼저 걸리면 여기 오지 않는다)
                        if ax is not None and ax.get("brk") and position is not None:
                            _b = ax["brk"]
                            _st = position["ax"]
                            _armed = max(float(position["peak"]), float(net)) >= float(_b["arm"])
                            _mode_ok = (_b.get("mode") is None or _st["mode"] == _b["mode"])
                            if _armed and _mode_ok and (_st["brk_idx"] is None or idx > _st["brk_idx"]):
                                _st["brk_idx"] = idx
                                _held = bt._direction_for_symbol(position["symbol"])
                                _rel, _cnt, _why = fast_release(
                                    snap_table(bars), idx, _held, _b["variant"], _st["brk"])
                                _st["brk"] = _cnt
                                if _rel:
                                    close_trade(recognition_at, px, "AX_STRUCTURE_EXIT", idx)
                                    position = None
                                    whipsaw_watch = None
                                    hold = _clr(hold)
                                    rhold = None
                                    continue
                        use_etp = (p.etp_scope == "all" or
                                   (p.etp_scope == "chop" and position["rec"].entry_chop))
                        if use_etp:
                            ed = etp.evaluate(entry_chop=True,
                                              peak_net_return_pct=position["peak"],
                                              net_return_pct=net)
                            if ed.exit_reason == config.EXIT_EARLY_TAKE_PROFIT:
                                position["rec"].lock_fired = True
                                close_trade(recognition_at, px,
                                            config.EXIT_EARLY_TAKE_PROFIT, idx)
                                position = None
                                whipsaw_watch = None
                                hold = _clr(hold)
                                rhold = None
                        # ── Peak-relative profit protection ────────────────
                        # production early_take_profit.evaluate 를 **그대로**
                        # 호출한다(arm=trigger, 바닥=floor). 새 청산식 없음.
                        # 래더(TP/SL/트레일링)·강제청산이 먼저 걸리면 여기 오지
                        # 않으므로 기존 구조는 그대로다.
                        if (ax is not None and ax.get("pp") and position is not None
                                and not position.get("cx")):
                            _pp = ax["pp"]
                            _st = position["ax"]
                            _peak = max(float(position["peak"]), float(net))
                            _arm = float(_pp["arm"])
                            _held = bt._direction_for_symbol(position["symbol"])
                            if _st["pend"] is not None and idx >= _st["pend"]:
                                _fx = px * (1.0 - float(pp_slip_pct) / 100.0)
                                position["rec"].pp_fired = True
                                close_trade(recognition_at, _fx, "PP_EXIT", idx)
                                position = None
                                whipsaw_watch = None
                                hold = _clr(hold)
                                rhold = None
                            elif _peak >= _arm and _st["pend"] is None:
                                if not position["rec"].pp_armed:
                                    position["rec"].pp_armed = True
                                    position["rec"].pp_arm_at = pd.Timestamp(recognition_at).isoformat()
                                    position["rec"].pp_arm_idx = int(idx)
                                if (_st["flip"] is None
                                        and pp_cond_ok(bars, idx, _held, "gap_neg", False)):
                                    _st["flip"] = int(idx)
                                    position["rec"].pp_flip_at = pd.Timestamp(recognition_at).isoformat()
                                _flr = (float(_pp["floor"]) if _pp.get("floor") is not None
                                        else _peak - float(_pp["give"]))
                                _ok = pp_cond_ok(bars, idx, _held, _pp.get("cond"),
                                                 whipsaw_watch is not None or hold is not None)
                                if _ok:
                                    _pd = etp.evaluate(entry_chop=True,
                                                       peak_net_return_pct=_peak,
                                                       net_return_pct=net,
                                                       trigger_pct=_arm, floor_pct=_flr)
                                    if _pd.exit_reason == config.EXIT_EARLY_TAKE_PROFIT:
                                        position["rec"].pp_fire_at = pd.Timestamp(recognition_at).isoformat()
                                        position["rec"].pp_fire_idx = int(idx)
                                        position["rec"].pp_peak = round(_peak, 4)
                                        position["rec"].pp_give = round(_peak - net, 4)
                                        if int(pp_delay_bars) > 0:
                                            _st["pend"] = idx + int(pp_delay_bars)
                                        else:
                                            _fx = px * (1.0 - float(pp_slip_pct) / 100.0)
                                            position["rec"].pp_fired = True
                                            close_trade(recognition_at, _fx, "PP_EXIT", idx)
                                            position = None
                                            whipsaw_watch = None
                                            hold = _clr(hold)
                                            rhold = None

                        # ── RX 방어 오버레이 (연구 전용) ──────────────────
                        # 기존 청산(래더/STOP/OPPOSITE/H50/C1)이 먼저 걸리면
                        # 이 지점에 오지 않는다 -> 기존 청산이 항상 우선이다.
                        if _rx and position is not None and position["rec"].rx_on:
                            _pt = _rx.get("partial")
                            if _pt and not position.get("rx_part_done"):
                                if net >= float(_pt["at"]):
                                    _r = float(_pt.get("ratio", 0.5))
                                    position["realized"] += position["qty_frac"] * _r * net
                                    position["legs"].append(
                                        (ce._fmt(recognition_at), px,
                                         round(position["qty_frac"] * _r, 6),
                                         "RX_PARTIAL", round(net, 4)))
                                    position["qty_frac"] *= (1.0 - _r)
                                    position["rx_part_done"] = True
                                    position["rec"].rx_partial_at = pd.Timestamp(recognition_at).isoformat()
                                    position["rec"].rx_partial_px = float(px)
                            _lk = _rx.get("lock")
                            if _lk and position is not None:
                                _pk2 = max(float(position["peak"]), float(net))
                                if _pk2 >= float(_lk["arm"]):
                                    if position["rec"].rx_lock_arm_at is None:
                                        position["rec"].rx_lock_arm_at = pd.Timestamp(recognition_at).isoformat()
                                    if net <= float(_lk["keep"]):
                                        position["rec"].rx_lock_fire_at = pd.Timestamp(recognition_at).isoformat()
                                        position["rec"].rx_reason = "RX_LOCK"
                                        close_trade(recognition_at, px, "RX_LOCK", idx)
                                        position = None
                                        whipsaw_watch = None
                                        hold = _clr(hold)
                                        rhold = None
                            if position is not None and _rx.get("hold_min"):
                                try:
                                    _hm = (pd.Timestamp(recognition_at)
                                           - pd.Timestamp(position["rec"].entry_time)).total_seconds() / 60.0
                                except Exception:
                                    _hm = 0.0
                                if _hm >= float(_rx["hold_min"]):
                                    position["rec"].rx_reason = "RX_MAXHOLD"
                                    close_trade(recognition_at, px, "RX_MAXHOLD", idx)
                                    position = None
                                    whipsaw_watch = None
                                    hold = _clr(hold)
                                    rhold = None

        if position is not None:
            li = len(bars) - 1
            ldt = pd.Timestamp(bars["datetime"].iloc[li]).to_pydatetime() + timedelta(minutes=3)
            px = fill_at(position["symbol"], ldt) or position["rec"].entry_price
            close_trade(ldt, px, "END_OF_DATA", li)

    return [vars(t) for t in trades]


# ── 지표 ───────────────────────────────────────────────────────────────────
def metrics(trades, dates, *, w1a: bool = True) -> dict:
    ds = set(dates)
    ts = [t for t in trades if t["date"] in ds]
    if not ts:
        return {"trades": 0}
    df = pd.DataFrame(ts).sort_values("exit_time").reset_index(drop=True)
    df["pnl"] = df["net_pct"] * (df["w1a"] if w1a else 1.0)
    n = len(df)
    w, l = df[df.pnl > 0], df[df.pnl < 0]
    eq = (1 + df.pnl / 100).cumprod()
    gp, gl = w.pnl.sum(), -l.pnl.sum()
    dd = (eq / eq.cummax() - 1) * 100
    daily = df.groupby("date")["pnl"].sum()
    # 최대 연속 손실일
    streak = best = 0
    for d in sorted(daily.index):
        if daily[d] < 0:
            streak += 1
            best = max(best, streak)
        else:
            streak = 0

    def _excl(k):
        top = df.nlargest(min(k, n), "pnl")
        rest = df.drop(top.index)
        if not len(rest):
            return 0.0
        return float(((1 + rest.sort_values("exit_time").pnl / 100).cumprod().iloc[-1] - 1) * 100)

    return {
        "trades": n,
        "win_rate_pct": round(len(w) / n * 100, 2),
        "day_win_rate_pct": round(float((daily > 0).sum()) / max(1, len(daily)) * 100, 2),
        "simple_pct": round(float(df.pnl.sum()), 4),
        "compound_pct": round(float((eq.iloc[-1] - 1) * 100), 4),
        "pf": round(float(gp / gl), 4) if gl > 0 else None,
        "mdd_pct": round(float(dd.min()), 4),
        "profit_days": int((daily > 0).sum()), "loss_days": int((daily < 0).sum()),
        "max_loss_streak": int(best),
        "top5_excl_pct": round(_excl(5), 4),
        "top10_excl_pct": round(_excl(10), 4),
        "runners_3pct": int((df.net_pct >= 3.0).sum()),
        "stops": int((df.exit_reason == config.EXIT_TW_STOP_LOSS).sum()),
        "opposite_exits": int((df.exit_reason == config.EXIT_OPPOSITE_SIGNAL).sum()),
        "watch_exits": int((df.exit_reason == "WHIPSAW_WATCH_DETERIORATION_EXIT").sum()),
        "h50_exits": int((df.exit_reason == swh.EXIT_SMALL_WHIPSAW_HOLD).sum()),
    }
