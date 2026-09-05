"""READ-ONLY 연구 1/2 (2026-09-04 사용자 요청 #3): AFTERNOON RUNNER 후보
데이터셋 구축.

최근 60영업일에서 아래 조건 중 하나를 만족하는 **모든 확정 MACD zero-cross
플래그 후보**를 모아, production 과 동일한 T+3 시점에 가상 진입시키고 기존
청산 로직으로 결과를 계산한 뒤, 진입 시점 피처를 저장한다.

  (a) T+3 확정판정 시각이 11:00(config.TW2_3SLOT_MORNING_WINDOW_END) 이후, 또는
  (b) 그날 TW2 3-SLOT 3개가 이미 모두 소진된 이후

Production 코드는 수정하지 않는다. 모든 지표/게이트는 production 함수 재사용:
  signal_engine.calculate_macd / calculate_macd_series / evaluate_macd_crossover
  time_window_filter._gap_series / _confirmed_flag_indices /
                     evaluate_time_window_entry / evaluate_tw2_extra_vetoes /
                     evaluate_whipsaw_watch
  major_flag_filter._session_vwap
  time_window_3slot.evaluate_trend_quality
  teg_gate.evaluate_teg
  time_window_position_manager.evaluate_take_profit_immediate / evaluate_position
  early_take_profit.evaluate_entry_chop / .evaluate
  worker._net_return_pct  /  order_executor.target_symbol_for_direction
  config.MAJOR_EMA_FAST(10) / MAJOR_EMA_SLOW(20)

■ 미래정보 차단
  피처는 전부 인덱스 <= 확정판정봉(i) 의 값만 사용한다. EMA/MACD/VWAP 는
  ewm(adjust=False) 재귀식과 일자별 cumsum 으로 전부 인과적이라 전체 프레임
  일괄 계산과 prefix 계산 결과가 같다(main() 에서 표본 후보에 대해 production
  원함수 prefix 호출과 일치하는지 실제로 assert 한다).
  30분/45분 창, 당일 고저, 직전 반대플래그 경과시간도 모두 i 이전만 본다.
  "슬롯 소진 이후 몇 번째" 도 그 시점까지 확정된 A-체인 진입만으로 센다.

■ 슬롯 상태 산출
  A 체인(현행 TW2 3-SLOT + 조기익절)을 그대로 재생해 얻은 진입 목록에서,
  후보 시점 이전에 그날 이미 체결된 A 진입 수 = slots_used_before 로 센다
  (production 의 slots_used_today 는 실제 체결에서만 증가하므로 정확히 동치).

■ 섀도우 청산
  사용자 스펙대로 "기존 TP/SL/trailing/반대신호" 로 계산한다(net_pct).
  참고로 조기익절 필터까지 켠 값(net_pct_etp)도 함께 저장해 라벨 민감도를
  같이 볼 수 있게 한다. 청산 제어흐름은 검증된 메인 루프의 미러:
    15:00 강제청산 > 반대플래그 T+3(휩쏘면 whipsaw watch) > 틱 TP >
    완성봉 래더(TP1/TP2/trailing/SL) > (옵션)조기익절
"""
from __future__ import annotations

import json
import pickle
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any, Optional

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(PROJECT_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from app.trading.macd2 import config, order_executor, teg_gate  # noqa: E402
from app.trading.macd2 import early_take_profit as etp  # noqa: E402
from app.trading.macd2 import time_window_3slot as tw3  # noqa: E402
from app.trading.macd2 import time_window_filter as twf  # noqa: E402
from app.trading.macd2 import time_window_position_manager as twpm  # noqa: E402
from app.trading.macd2.major_flag_filter import _session_vwap  # noqa: E402
from app.trading.macd2.models import Direction  # noqa: E402
from app.trading.macd2.signal_engine import (  # noqa: E402
    calculate_macd, calculate_macd_series, evaluate_macd_crossover, resample_completed_3m,
)
from app.trading.macd2.worker import _net_return_pct  # noqa: E402

import backtest_time_window_filter as bt  # noqa: E402
import _tmp_20260903_chop_adaptive_exit_train_oos as ce  # noqa: E402
import _tmp_20260904_teg4th_fullchain as teg4  # noqa: E402

KST = config.KST
OUTPUT_DIR = PROJECT_ROOT / "data" / "validation" / "afternoon_runner"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
_CTX_CACHE = OUTPUT_DIR / "_ctx60.pkl"

N_DAYS = 60
N_TRAIN = 40
WIN30_BARS = 10          # 완성 3분봉 10개 == 30분
WIN45_BARS = 15          # 45분


@dataclass
class Ctx:
    dates: list
    hynix_bars_3m: pd.DataFrame
    flags_by_idx: dict
    etf_close: dict
    etf_1m_close: dict


def build_ctx(use_cache: bool = True) -> Ctx:
    if use_cache and _CTX_CACHE.exists():
        with open(_CTX_CACHE, "rb") as fh:
            return Ctx(**pickle.load(fh))

    all_dates = ce._common_dates()
    dates = all_dates[-N_DAYS:]
    warmup = all_dates[all_dates.index(dates[0]) - 1]

    hynix_all = ce._load_all("hynix", [warmup] + dates)
    long_all = ce._load_all("long", dates)
    inverse_all = ce._load_all("inverse", dates)
    end = datetime.combine(pd.Timestamp(dates[-1]).date(), dtime(20, 0), tzinfo=KST)
    hynix_bars_3m = resample_completed_3m(hynix_all, now=end)
    long_bars_3m = resample_completed_3m(long_all, now=end)
    inverse_bars_3m = resample_completed_3m(inverse_all, now=end)

    etf_close = {
        config.LONG_SYMBOL: bt._etf_close_lookup(long_bars_3m),
        config.INVERSE_SYMBOL: bt._etf_close_lookup(inverse_bars_3m),
    }
    etf_1m_close = {
        config.LONG_SYMBOL: dict(zip(long_all["datetime"], long_all["close"])),
        config.INVERSE_SYMBOL: dict(zip(inverse_all["datetime"], inverse_all["close"])),
    }

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

    ctx = Ctx(dates=dates, hynix_bars_3m=hynix_bars_3m, flags_by_idx=flags_by_idx,
              etf_close=etf_close, etf_1m_close=etf_1m_close)
    with open(_CTX_CACHE, "wb") as fh:
        pickle.dump(dict(vars(ctx)), fh)
    return ctx


# ── 전체 프레임 인과적 지표 precompute ──────────────────────────────────
@dataclass
class Pre:
    macd: list
    signal: list
    gap: list
    ema10: list
    ema20: list
    vwap: list
    close: list
    high: list
    low: list
    volume: list
    day: list
    tod: list
    in_session: list
    first_idx: list          # 당일 정규장 첫 봉 인덱스
    flag_confirm: list       # (confirm_datetime, Direction) 확정 플래그 목록


def precompute(bars: pd.DataFrame) -> Pre:
    n = len(bars)
    close = bars["close"].astype(float).reset_index(drop=True)
    ema10 = close.ewm(span=config.MAJOR_EMA_FAST, adjust=False).mean()
    ema20 = close.ewm(span=config.MAJOR_EMA_SLOW, adjust=False).mean()

    series = calculate_macd_series(bars)
    macd = [float("nan")] * n
    signal = [float("nan")] * n
    if series is not None:
        off = n - len(series)
        for j in range(len(series)):
            macd[off + j] = float(series["macd"].iloc[j])
            signal[off + j] = float(series["signal"].iloc[j])
    gap = [macd[i] - signal[i] for i in range(n)]

    vwap = _session_vwap(bars).reset_index(drop=True).tolist()
    kst = bars["datetime"].dt.tz_convert(KST)
    day = kst.dt.strftime("%Y%m%d").tolist()
    tod = [t.time() for t in kst]
    in_session = [tod[i] >= config.SESSION_OPEN for i in range(n)]

    first_idx = [0] * n
    cur_day = None
    cur_first = None
    for i in range(n):
        if day[i] != cur_day:
            cur_day = day[i]
            cur_first = None
        if cur_first is None and in_session[i]:
            cur_first = i
        first_idx[i] = cur_first if cur_first is not None else i

    gseries = twf._gap_series(bars)
    flag_confirm: list = []
    if gseries is not None:
        for i, d in twf._confirmed_flag_indices(gseries):
            bar_dt = pd.Timestamp(gseries["datetime"].iloc[i]).to_pydatetime()
            if bar_dt.astimezone(KST).time() < config.SESSION_OPEN:
                continue
            flag_confirm.append((bar_dt + timedelta(minutes=3), d))

    return Pre(macd=macd, signal=signal, gap=gap,
               ema10=ema10.tolist(), ema20=ema20.tolist(), vwap=vwap,
               close=close.tolist(),
               high=bars["high"].astype(float).tolist(),
               low=bars["low"].astype(float).tolist(),
               volume=bars["volume"].astype(float).tolist(),
               day=day, tod=tod, in_session=in_session, first_idx=first_idx,
               flag_confirm=flag_confirm)


def _safe(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if pd.isna(v) else round(v, 6)


def build_features(bars: pd.DataFrame, pre: Pre, i: int, direction: Direction,
                   decision_at: datetime, bars_slice: pd.DataFrame,
                   flag_bar_dt: datetime) -> dict:
    """i = T+3 확정판정봉 인덱스. i 이후 데이터는 절대 참조하지 않는다."""
    sign = 1.0 if direction == Direction.UP_RED else -1.0
    c = pre.close[i]
    f: dict = {}

    # ── MACD gap / zero-line ──
    g = pre.gap
    f["gap_abs"] = _safe(abs(g[i]))
    f["gap_signed"] = _safe(g[i] * sign)
    for k in (1, 2, 3):
        f[f"gap_exp{k}"] = _safe((g[i] - g[i - k]) * sign) if i - k >= 0 else None
    f["zero_dist_abs"] = _safe(abs(pre.macd[i]))
    f["zero_dist_signed"] = _safe(pre.macd[i] * sign)
    f["zero_dist_pct"] = _safe(abs(pre.macd[i]) / c * 100.0)

    # ── EMA ──
    sp = (pre.ema10[i] - pre.ema20[i]) * sign
    f["ema_spread"] = _safe(sp)
    f["ema_spread_pct"] = _safe(sp / c * 100.0)
    f["ema_spread_exp2"] = _safe(sp - (pre.ema10[i - 2] - pre.ema20[i - 2]) * sign) if i >= 2 else None
    f["ema_spread_exp3"] = _safe(sp - (pre.ema10[i - 3] - pre.ema20[i - 3]) * sign) if i >= 3 else None
    f["ema20_slope_pct"] = _safe((pre.ema20[i] - pre.ema20[i - 2]) * sign / c * 100.0) if i >= 2 else None
    f["price_vs_ema20_pct"] = _safe((c - pre.ema20[i]) * sign / c * 100.0)
    v = pre.vwap[i]
    f["price_vs_vwap_pct"] = (_safe((c - v) * sign / c * 100.0)
                              if v and not pd.isna(v) and v > 0 else None)

    # ── 최근 30분 고저 돌파 (현재봉 제외한 직전 10봉 창) ──
    lo_i = max(i - WIN30_BARS, pre.first_idx[i])
    if i - lo_i >= 3:
        hi30 = max(pre.high[lo_i:i])
        lo30 = min(pre.low[lo_i:i])
        rng = hi30 - lo30
        brk = (c - hi30) if sign > 0 else (lo30 - c)
        f["break30"] = 1 if brk > 0 else 0
        f["break30_pct"] = _safe(brk / c * 100.0)
        f["range30_pct"] = _safe(rng / c * 100.0)
        f["break30_ratio"] = _safe(brk / rng) if rng > 0 else None
    else:
        f["break30"] = None
        f["break30_pct"] = None
        f["range30_pct"] = None
        f["break30_ratio"] = None

    # ── 거래량 ──
    for k in (5, 10):
        lo_v = max(i - k, pre.first_idx[i])
        if i - lo_v >= 2:
            avg = sum(pre.volume[lo_v:i]) / (i - lo_v)
            f[f"vol_ratio{k}"] = _safe(pre.volume[i] / avg) if avg > 0 else None
        else:
            f[f"vol_ratio{k}"] = None

    # ── 최근 30/45분 확정 cross 횟수 (production 과 동일한 장전 제외) ──
    for label, mins in (("cross30", 30), ("cross45", 45)):
        start = decision_at - timedelta(minutes=mins)
        f[label] = sum(1 for ct, _d in pre.flag_confirm if start <= ct < decision_at)

    # ── 직전 반대플래그 이후 경과시간(분) ──
    opp = Direction.DOWN_BLUE if direction == Direction.UP_RED else Direction.UP_RED
    prev_opp = [ct for ct, d in pre.flag_confirm if d == opp and ct < decision_at
                and ct.strftime("%Y%m%d") == pre.day[i]]
    if prev_opp:
        f["mins_since_opposite"] = _safe((decision_at - max(prev_opp)).total_seconds() / 60.0)
    else:
        f["mins_since_opposite"] = None

    # ── 당일 고저 갱신 ──
    s0 = pre.first_idx[i]
    if i > s0:
        if sign > 0:
            f["day_extreme"] = 1 if pre.high[i] >= max(pre.high[s0:i + 1]) else 0
            f["day_extreme_margin_pct"] = _safe(
                (pre.high[i] - max(pre.high[s0:i])) / c * 100.0)
        else:
            f["day_extreme"] = 1 if pre.low[i] <= min(pre.low[s0:i + 1]) else 0
            f["day_extreme_margin_pct"] = _safe(
                (min(pre.low[s0:i]) - pre.low[i]) / c * 100.0)
    else:
        f["day_extreme"] = None
        f["day_extreme_margin_pct"] = None

    # ── TEGv2 / Trend Quality (production 함수 그대로) ──
    t = teg_gate.evaluate_teg(bars_slice, direction, flag_bar_dt, decision_at)
    f["teg_passed"] = int(sum(1 for cnd in teg_gate.ALL_CONDITIONS
                              if t.conditions.get(cnd, False)))
    f["teg_total"] = len(teg_gate.ALL_CONDITIONS)
    f["teg_approved"] = 1 if t.approved else 0
    q = tw3.evaluate_trend_quality(bars_slice, direction)
    f["tq"] = int(q.passed_count)

    # ── 시각 ──
    tt = decision_at.astimezone(KST)
    f["decision_minutes"] = tt.hour * 60 + tt.minute - (
        config.SESSION_OPEN.hour * 60 + config.SESSION_OPEN.minute)
    f["is_afternoon"] = 1 if tt.time() >= config.TW2_3SLOT_MORNING_WINDOW_END else 0
    return f


# ── 섀도우 청산 시뮬레이터 (검증된 메인 루프의 청산부 미러) ─────────────
def simulate_shadow(ctx: Ctx, pre: Pre, i0: int, direction: Direction,
                    decision_at: datetime, *, early_tp: bool) -> Optional[dict]:
    bars = ctx.hynix_bars_3m
    etf_close = ctx.etf_close
    etf_1m_close = ctx.etf_1m_close
    flags = ctx.flags_by_idx

    target = order_executor.target_symbol_for_direction(direction)
    entry = etf_close[target].get(bars["datetime"].iloc[i0])
    if entry is None:
        return None
    session = (tw3.SESSION_MORNING
               if decision_at.astimezone(KST).time() < config.TW2_3SLOT_MORNING_WINDOW_END
               else tw3.SESSION_AFTERNOON)
    day = pre.day[i0]

    def net_at(p):
        return float(_net_return_pct(target, entry, p, 1))

    entry_chop = False
    if early_tp:
        cd = etp.evaluate_entry_chop(bars.iloc[: i0 + 1], direction, decision_at)
        if not cd.insufficient_data:
            entry_chop = bool(cd.is_chop)

    tp1_done = False
    qty = 1.0
    realized = 0.0
    peak_ladder = 0.0
    peak = 0.0
    trough = 0.0
    whipsaw = None

    for i in range(i0 + 1, len(bars)):
        if pre.day[i] != day:
            break
        bar_ts = bars["datetime"].iloc[i]
        bar_start = pd.Timestamp(bar_ts).to_pydatetime()
        bar_close_at = bar_start + timedelta(minutes=3)

        # 15:00 강제청산
        if bar_close_at.astimezone(KST).time() >= config.FORCE_LIQUIDATE_AT:
            px = etf_close[target].get(bar_ts)
            if px is not None:
                leg = net_at(px)
                return dict(exit_reason=config.EXIT_FORCED_LIQUIDATION,
                            exit_time=ce._fmt(bar_close_at), exit_price=px,
                            net_pct=round(realized + qty * leg, 6),
                            peak_net_pct=round(max(peak, leg), 6),
                            trough_net_pct=round(min(trough, leg), 6),
                            hold_bars=i - i0, tp1_hit=tp1_done,
                            entry_price=entry, entry_symbol=target, session=session)
            break

        # 반대 플래그 T+3 판정 (메인 루프와 동일: 플래그봉 다음 봉에서 판정)
        pidx = i - 1
        if pidx in flags and flags[pidx] != direction:
            opp_dir = flags[pidx]
            flag_dt = pd.Timestamp(bars["datetime"].iloc[pidx]).to_pydatetime()
            if config.SESSION_OPEN <= flag_dt.astimezone(KST).time() < config.NEW_ENTRY_CUTOFF:
                sl = bars.iloc[: i + 1]
                dec = twf.evaluate_time_window_entry(
                    sl, opp_dir, flag_dt, bar_close_at,
                    position_direction=direction,
                    morning_entry_count=0, afternoon_entry_count=0, daily_entry_count=0)
                reason = dec.block_reason
                if dec.approved:
                    vetoed, vr = twf.evaluate_tw2_extra_vetoes(sl, opp_dir, flag_dt, bar_close_at)
                    reason = vr if vetoed else None
                is_whip = (not dec.approved) and reason in config.TW_WHIPSAW_REJECT_REASONS
                if is_whip:
                    whipsaw = {"direction": opp_dir, "last_gap": float("-inf"),
                               "last_ema_spread": float("-inf")}
                else:
                    px = etf_close[target].get(bar_ts, entry)
                    leg = net_at(px)
                    return dict(exit_reason=config.EXIT_OPPOSITE_SIGNAL,
                                exit_time=ce._fmt(bar_close_at), exit_price=px,
                                net_pct=round(realized + qty * leg, 6),
                                peak_net_pct=round(max(peak, leg), 6),
                                trough_net_pct=round(min(trough, leg), 6),
                                hold_bars=i - i0, tp1_hit=tp1_done,
                                entry_price=entry, entry_symbol=target, session=session)

        # whipsaw 추적
        if whipsaw is not None:
            wd = twf.evaluate_whipsaw_watch(bars.iloc[: i + 1], whipsaw["direction"],
                                            whipsaw["last_gap"], whipsaw["last_ema_spread"])
            if not wd.insufficient_data:
                if wd.should_release:
                    whipsaw = None
                elif wd.should_sell:
                    px = etf_close[target].get(bar_ts)
                    if px is not None:
                        leg = net_at(px)
                        return dict(exit_reason="WHIPSAW_WATCH_DETERIORATION_EXIT",
                                    exit_time=ce._fmt(bar_close_at), exit_price=px,
                                    net_pct=round(realized + qty * leg, 6),
                                    peak_net_pct=round(max(peak, leg), 6),
                                    trough_net_pct=round(min(trough, leg), 6),
                                    hold_bars=i - i0, tp1_hit=tp1_done,
                                    entry_price=entry, entry_symbol=target, session=session)
                    whipsaw = None
                else:
                    whipsaw["last_gap"] = wd.current_gap
                    whipsaw["last_ema_spread"] = wd.current_ema_spread

        # 틱 TP
        for mo in range(3):
            tick = bar_start + timedelta(minutes=mo)
            if tick <= decision_at or tick > bar_close_at:
                continue
            px = etf_1m_close[target].get(pd.Timestamp(tick))
            if px is None:
                continue
            leg = net_at(px)
            peak = max(peak, leg)
            trough = min(trough, leg)
            tp = twpm.evaluate_take_profit_immediate(
                session=session, net_return_pct=leg, tp1_done=tp1_done,
                tp2_pct_override=config.TW2_MORNING_TP2 * 100.0)
            peak_ladder = max(peak_ladder, tp.peak_net_return)
            if tp.exit_reason == config.EXIT_TW_TP1_PARTIAL:
                tp1_done = tp.tp1_done
                realized += qty * tp.sell_fraction * leg
                qty *= (1.0 - tp.sell_fraction)
            elif tp.exit_reason is not None:
                return dict(exit_reason=tp.exit_reason, exit_time=ce._fmt(tick),
                            exit_price=px, net_pct=round(realized + qty * leg, 6),
                            peak_net_pct=round(peak, 6), trough_net_pct=round(trough, 6),
                            hold_bars=i - i0, tp1_hit=tp1_done,
                            entry_price=entry, entry_symbol=target, session=session)
            else:
                tp1_done = tp.tp1_done

        # 완성봉 래더
        px = etf_close[target].get(bar_ts)
        if px is None:
            continue
        leg = net_at(px)
        peak = max(peak, leg)
        trough = min(trough, leg)
        pm = twpm.evaluate_position(
            session=session, net_return_pct=leg, tp1_done=tp1_done,
            peak_net_return=peak_ladder, tp2_pct_override=config.TW2_MORNING_TP2 * 100.0)
        peak_ladder = pm.peak_net_return
        if pm.exit_reason == config.EXIT_TW_TP1_PARTIAL:
            tp1_done = pm.tp1_done
            realized += qty * pm.sell_fraction * leg
            qty *= (1.0 - pm.sell_fraction)
        elif pm.exit_reason is not None:
            return dict(exit_reason=pm.exit_reason, exit_time=ce._fmt(bar_close_at),
                        exit_price=px, net_pct=round(realized + qty * leg, 6),
                        peak_net_pct=round(peak, 6), trough_net_pct=round(trough, 6),
                        hold_bars=i - i0, tp1_hit=tp1_done,
                        entry_price=entry, entry_symbol=target, session=session)
        else:
            tp1_done = pm.tp1_done
            if early_tp and entry_chop:
                ed = etp.evaluate(entry_chop=True, peak_net_return_pct=peak_ladder,
                                  net_return_pct=leg)
                if ed.exit_reason == config.EXIT_EARLY_TAKE_PROFIT:
                    return dict(exit_reason=config.EXIT_EARLY_TAKE_PROFIT,
                                exit_time=ce._fmt(bar_close_at), exit_price=px,
                                net_pct=round(realized + qty * leg, 6),
                                peak_net_pct=round(peak, 6), trough_net_pct=round(trough, 6),
                                hold_bars=i - i0, tp1_hit=tp1_done,
                                entry_price=entry, entry_symbol=target, session=session)

    # 당일 마지막 봉까지 보유 -> 그 종가로 마감
    last = i0
    for i in range(i0 + 1, len(bars)):
        if pre.day[i] != day:
            break
        last = i
    px = etf_close[target].get(bars["datetime"].iloc[last], entry)
    leg = net_at(px)
    return dict(exit_reason="END_OF_DAY", exit_time=ce._fmt(
        pd.Timestamp(bars["datetime"].iloc[last]).to_pydatetime() + timedelta(minutes=3)),
        exit_price=px, net_pct=round(realized + qty * leg, 6),
        peak_net_pct=round(max(peak, leg), 6), trough_net_pct=round(min(trough, leg), 6),
        hold_bars=last - i0, tp1_hit=tp1_done,
        entry_price=entry, entry_symbol=target, session=session)


def label_of(net: float) -> str:
    if net >= 5.0:
        return "SUPER_WIN"
    if net >= 3.0:
        return "BIG_WIN"
    if net > 0.0:
        return "MID"
    return "LOSER"


def main() -> int:
    ctx = build_ctx(use_cache=True)
    bars = ctx.hynix_bars_3m
    pre = precompute(bars)
    dates = ctx.dates
    print(f"기간 {dates[0]}~{dates[-1]} ({len(dates)}영업일) 3분봉 {len(bars)} "
          f"확정플래그 {len(ctx.flags_by_idx)}")

    # A 체인(현행) 재생 -> 슬롯 소진 타임라인
    trades_a = teg4.run(ctx, extra_teg_entry=False)
    a_by_day: dict = {}
    for t in trades_a:
        a_by_day.setdefault(t.date, []).append(t)
    print(f"A 체인 진입 {len(trades_a)}건")

    # ── 미래정보 미사용 검증: 전체프레임 precompute == prefix 계산 ──
    checked = 0
    for i in range(200, len(bars), 411):
        snap = calculate_macd(bars.iloc[: i + 1])
        if snap is None:
            continue
        assert abs(snap.macd - pre.macd[i]) < 1e-6, (i, snap.macd, pre.macd[i])
        assert abs(snap.signal - pre.signal[i]) < 1e-6, (i, snap.signal, pre.signal[i])
        pv = _session_vwap(bars.iloc[: i + 1]).iloc[-1]
        if not pd.isna(pv):
            assert abs(float(pv) - pre.vwap[i]) < 1e-6, (i, pv, pre.vwap[i])
        checked += 1
    print(f"인과성 검증(전체프레임 precompute == prefix 원함수) 표본 {checked}개 통과")

    rows: list = []
    date_set = set(dates)
    for fidx in sorted(ctx.flags_by_idx):
        i = fidx + 1                       # T+3 확정판정봉
        if i >= len(bars):
            continue
        direction = ctx.flags_by_idx[fidx]
        flag_bar_dt = pd.Timestamp(bars["datetime"].iloc[fidx]).to_pydatetime()
        day = pre.day[fidx]
        if day not in date_set or pre.day[i] != day:
            continue
        if not (config.SESSION_OPEN <= flag_bar_dt.astimezone(KST).time()
                < config.NEW_ENTRY_CUTOFF):
            continue
        bar_start = pd.Timestamp(bars["datetime"].iloc[i]).to_pydatetime()
        decision_at = bar_start + timedelta(minutes=3)
        tt = decision_at.astimezone(KST).time()
        if tt >= config.TW2_3SLOT_AFTERNOON_WINDOW_END:
            continue                        # production 진입창 밖

        slots_before = sum(1 for t in a_by_day.get(day, []) if t.decision_idx < i)
        is_afternoon = tt >= config.TW2_3SLOT_MORNING_WINDOW_END
        exhausted = slots_before >= config.TW2_3SLOT_DAILY_CAP
        if not (is_afternoon or exhausted):
            continue

        bars_slice = bars.iloc[: i + 1]
        feats = build_features(bars, pre, i, direction, decision_at, bars_slice, flag_bar_dt)

        # 슬롯 소진 후 몇 번째 후보인지
        after_ord = 0
        if exhausted:
            after_ord = 1 + sum(
                1 for r in rows
                if r["date"] == day and r["exhausted"] and r["decision_idx"] < i)

        sim = simulate_shadow(ctx, pre, i, direction, decision_at, early_tp=False)
        if sim is None:
            continue
        sim_etp = simulate_shadow(ctx, pre, i, direction, decision_at, early_tp=True)

        a_entered = any(t.decision_idx == i and t.direction == direction.value
                        for t in a_by_day.get(day, []))

        rows.append({
            "date": day,
            "split": "TRAIN" if day in set(dates[:N_TRAIN]) else "OOS",
            "decision_idx": i,
            "flag_idx": fidx,
            "direction": direction.value,
            "flag_bar_at": flag_bar_dt.isoformat(),
            "decision_at": decision_at.isoformat(),
            "slots_used_before": slots_before,
            "exhausted": exhausted,
            "after_exhaust_ordinal": after_ord,
            "a_entered": a_entered,
            **feats,
            "net_pct": sim["net_pct"],
            "net_pct_etp": sim_etp["net_pct"] if sim_etp else None,
            "peak_net_pct": sim["peak_net_pct"],
            "trough_net_pct": sim["trough_net_pct"],
            "exit_reason": sim["exit_reason"],
            "exit_time": sim["exit_time"],
            "entry_price": sim["entry_price"],
            "exit_price": sim["exit_price"],
            "hold_bars": sim["hold_bars"],
            "label": label_of(sim["net_pct"]),
            "label_etp": label_of(sim_etp["net_pct"]) if sim_etp else None,
        })

    df = pd.DataFrame(rows)
    print(f"\n후보 총 {len(df)}건  (TRAIN {int((df['split']=='TRAIN').sum())} / "
          f"OOS {int((df['split']=='OOS').sum())})")
    print("라벨 분포:", df["label"].value_counts().to_dict())
    print("  11:00 이후:", int(df["is_afternoon"].sum()),
          " / 3슬롯 소진 후:", int(df["exhausted"].sum()),
          " / 둘 다:", int((df["is_afternoon"] & df["exhausted"]).sum()))

    df.to_csv(OUTPUT_DIR / "candidates.csv", index=False, encoding="utf-8-sig")
    (OUTPUT_DIR / "meta.json").write_text(json.dumps({
        "dates": dates, "train_dates": dates[:N_TRAIN], "oos_dates": dates[N_TRAIN:],
        "n_candidates": len(df),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"saved {OUTPUT_DIR / 'candidates.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
