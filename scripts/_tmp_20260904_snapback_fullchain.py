"""READ-ONLY full-chain backtest (2026-09-04 사용자 요청 #4): 현행(A) 대비
SNAPBACK 오후 추가 1회(B).

  A. 현행 — TW2 3-SLOT + 조기익절 필터, PRE15 OFF, 하루 최대 3회
  B. A + SNAPBACK 추가 1회
       - 13:00 <= T+3 판정시각 < 14:50 에서만
       - 그날 3슬롯(config.TW2_3SLOT_DAILY_CAP)을 모두 소진한 뒤, 하루 1회만
       - 방향은 확정 MACD zero-cross 방향 = 직전 과도한 움직임의 반대(스냅백)
       - 아래 4개 중 3개 이상 만족 시 진입 (임계값 고정, 재튜닝 없음):
           C1  day_extreme_margin_pct <= -4.0
           C2  price_vs_vwap_pct      <= -1.0
           C3  ema20_slope_pct        <= 0
           C4  반대방향 MACD crossover 가 T+3 까지 유지
       - 기존 TW2/TEGv2 의 afternoon time-window 제한은 SNAPBACK 에 적용하지
         않는다. SNAPBACK 후보에는 위 조건 + 신규진입 마감 14:50 만 적용한다.
       - 진입 후 청산은 기존 TP1/TP2/trailing/SL/반대신호/whipsaw/조기익절 그대로
       - 하루 실제 총 신규진입 최대 4회 (3슬롯 + SNAPBACK 1)

■ 피처 부호 규약 (전부 후보의 플래그 방향 = 스냅백 방향 기준)
  day_extreme_margin_pct : UP_RED  -> (high[i] - max(high[장중..i-1])) / close * 100
                           DOWN_BLUE-> (min(low[장중..i-1]) - low[i]) / close * 100
                           음수 = 당일 극값에서 그만큼 떨어져 있음
  price_vs_vwap_pct      : (close - VWAP) * sign / close * 100
                           음수 = 신호 방향의 반대쪽 VWAP 건너편에 있음
  ema20_slope_pct        : (EMA20[i] - EMA20[i-2]) * sign / close * 100
                           <=0 = EMA20 기울기가 스냅백 방향과 반대
  C4                     : (MACD - Signal) * sign > 0  at T+3 확정봉
                           = production evaluate_time_window_entry 의
                             TW_REJECT_NOT_CONFIRMED 판정( gap_now > 0 )과 동일식.
                             gap 확대(REJECT_MACD_GAP_NOT_EXPANDING)는 요구하지 않음
                             — 사용자 조건은 "유지"이지 "확대"가 아니다.
  피처는 전부 인덱스 <= T+3 확정봉만 참조한다(미래정보 없음). 계산은 연구
  1단계 _tmp_20260904_runner_dataset.build_features 를 그대로 재사용하며,
  그 precompute 가 production 원함수 prefix 호출과 일치함은 이미 assert 검증됨.

■ 민감도 참고 (사용자 스펙 아님, 별도 표기)
  B_TW2KEEP — SNAPBACK 후보에도 TW2 의 시간창 '이전' 단계(T+3 재확인 /
  gap 확대 / 최소 플래그간격)까지는 그대로 요구하고, 시간창 거절만 면제한 변형.

Production 코드는 수정하지 않는다. 진입/청산 판단은 전부 production 순수함수
호출이며, 신규 로직은 위 SNAPBACK 게이트뿐이다.
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import time as dtime, timedelta
from pathlib import Path
from typing import Optional

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
from app.trading.macd2.models import Direction  # noqa: E402
from app.trading.macd2.worker import _net_return_pct  # noqa: E402

import backtest_time_window_filter as bt  # noqa: E402
import _tmp_20260903_chop_adaptive_exit_train_oos as ce  # noqa: E402
import _tmp_20260904_runner_dataset as ds  # noqa: E402

KST = config.KST
OUTPUT_DIR = PROJECT_ROOT / "data" / "validation" / "snapback"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# ── 고정 임계값 (이번 테스트에서 재튜닝 금지) ──────────────────────────
SNAPBACK_START = dtime(13, 0)
SNAPBACK_END = dtime(14, 50)
THR_EXTREME = -4.0
THR_VWAP = -1.0
THR_SLOPE = 0.0
MIN_CONDITIONS = 3
SYMBOL_NAME = {"0193T0": "LONG", "0197X0": "INVERSE"}


def snapback_conditions(f: dict) -> dict:
    """4개 조건 개별 판정. None(계산불가)은 미충족으로 센다."""
    e = f.get("day_extreme_margin_pct")
    v = f.get("price_vs_vwap_pct")
    s = f.get("ema20_slope_pct")
    g = f.get("gap_signed")
    return {
        "C1_day_extreme": bool(e is not None and e <= THR_EXTREME),
        "C2_vwap": bool(v is not None and v <= THR_VWAP),
        "C3_ema20_slope": bool(s is not None and s <= THR_SLOPE),
        "C4_t3_hold": bool(g is not None and g > 0),
    }


@dataclass
class Trade:
    date: str
    slot_number: Optional[int]
    session: Optional[str]
    direction: str
    decision_idx: int
    is_snapback: bool = False
    entry_time: Optional[str] = None
    entry_symbol: Optional[str] = None
    entry_price: Optional[float] = None
    entry_bar_idx: Optional[int] = None
    exit_time: Optional[str] = None
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None
    exit_bar_idx: Optional[int] = None
    net_pct: Optional[float] = None
    peak_net_pct: float = 0.0
    trough_net_pct: float = 0.0
    entry_chop: bool = False
    lock_fired: bool = False
    hold_bars: int = 0
    switched_from: Optional[str] = None
    snap_conditions: Optional[dict] = None
    snap_passed: Optional[int] = None


@dataclass
class SnapCandidate:
    date: str
    direction: str
    decision_idx: int
    decision_at: str
    slots_used_before: int
    conditions: dict
    passed: int
    approved: bool
    reject: Optional[str]
    position_before: Optional[str]
    day_extreme_margin_pct: Optional[float] = None
    price_vs_vwap_pct: Optional[float] = None
    ema20_slope_pct: Optional[float] = None
    gap_signed: Optional[float] = None
    tw2_block_reason: Optional[str] = None


def run_chain(ctx, pre, *, snapback: bool, tw2_keep: bool = False,
              snaps_out: Optional[list] = None) -> list:
    bars = ctx.hynix_bars_3m
    flags_by_idx = ctx.flags_by_idx
    etf_close = ctx.etf_close
    etf_1m_close = ctx.etf_1m_close
    date_set = set(ctx.dates)

    trades: list = []
    snaps: list = []
    position = None
    pending = None
    slots_used_today = 0
    morning_count = afternoon_count = 0
    snap_used_today = False
    last_afternoon_direction = None
    current_day = None
    whipsaw_watch = None

    def position_direction():
        return bt._direction_for_symbol(position["symbol"]) if position is not None else None

    def net_at(price):
        return float(_net_return_pct(position["symbol"], position["rec"].entry_price, price, 1))

    def close_trade(exit_time, exit_price, reason, idx):
        rec = position["rec"]
        rec.exit_time = ce._fmt(exit_time)
        rec.exit_price = exit_price
        rec.exit_reason = reason
        rec.exit_bar_idx = idx
        leg = net_at(exit_price)
        rec.net_pct = round(position["realized"] + position["qty_frac"] * leg, 6)
        rec.hold_bars = int(idx - rec.entry_bar_idx)
        trades.append(rec)

    for idx in range(len(bars)):
        bar_ts = bars["datetime"].iloc[idx]
        bar_start = pd.Timestamp(bar_ts).to_pydatetime()
        day_key = bar_start.strftime("%Y%m%d")
        if day_key not in date_set:
            continue
        if day_key != current_day:
            current_day = day_key
            slots_used_today = morning_count = afternoon_count = 0
            snap_used_today = False
            last_afternoon_direction = None
            pending = None
            whipsaw_watch = None
        bar_close_at = bar_start + timedelta(minutes=3)

        if position is not None and bar_close_at.astimezone(KST).time() >= config.FORCE_LIQUIDATE_AT:
            close = etf_close[position["symbol"]].get(bar_ts)
            if close is not None:
                close_trade(bar_close_at, close, config.EXIT_FORCED_LIQUIDATION, idx)
                position = None
            pending = None
            whipsaw_watch = None

        if pending is not None:
            p_direction, p_idx, p_bar_ts = pending
            if idx == p_idx + 1:
                pending = None
                flag_bar_dt = pd.Timestamp(p_bar_ts).to_pydatetime()
                bars_slice = bars.iloc[: idx + 1]
                slot_number = session = None
                is_snap = False
                snap_conds = None
                snap_passed = None

                base_decision = twf.evaluate_time_window_entry(
                    bars_slice, p_direction, flag_bar_dt, bar_close_at,
                    position_direction=position_direction(),
                    morning_entry_count=0, afternoon_entry_count=0, daily_entry_count=0)
                tw2_cleared = bool(base_decision.approved)
                base_reason = base_decision.block_reason
                if tw2_cleared:
                    vetoed, veto_reason = twf.evaluate_tw2_extra_vetoes(
                        bars_slice, p_direction, flag_bar_dt, bar_close_at)
                    if vetoed:
                        tw2_cleared = False
                        base_reason = veto_reason

                final_approved = False
                final_reason = base_reason
                if tw2_cleared:
                    sd = tw3.resolve_slot(
                        now=bar_close_at, slots_used_today=slots_used_today,
                        morning_count=morning_count, afternoon_count=afternoon_count,
                        direction=p_direction, is_flat=(position is None),
                        last_afternoon_direction=last_afternoon_direction)
                    slot_number, session = sd.slot_number, sd.session
                    if not sd.slot_allowed:
                        final_reason = sd.reject_reason
                    elif sd.requires_quality_gate:
                        q = tw3.evaluate_trend_quality(bars_slice, p_direction)
                        final_approved = q.approved
                        final_reason = (config.TW_APPROVED if q.approved
                                        else config.TW2_3SLOT_REJECT_QUALITY)
                    elif sd.requires_teg_gate:
                        t = teg_gate.evaluate_teg(bars_slice, p_direction,
                                                  flag_bar_dt, bar_close_at)
                        final_approved = t.approved
                        final_reason = (config.TW_APPROVED if t.approved
                                        else config.TW2_3SLOT_REJECT_TEG)
                    else:
                        final_approved = True
                        final_reason = config.TW_APPROVED

                # ── SNAPBACK 추가 1회 게이트 (TW2 시간창 제한 미적용) ──
                moment = bar_close_at.astimezone(KST).time()
                if (snapback and not final_approved and not snap_used_today
                        and slots_used_today >= config.TW2_3SLOT_DAILY_CAP
                        and SNAPBACK_START <= moment < SNAPBACK_END):
                    feats = ds.build_features(bars, pre, idx, p_direction,
                                              bar_close_at, bars_slice, flag_bar_dt)
                    conds = snapback_conditions(feats)
                    npass = sum(1 for v in conds.values() if v)
                    ok = npass >= MIN_CONDITIONS
                    reject = None if ok else f"SNAPBACK_ONLY_{npass}_OF_4"
                    if ok and tw2_keep:
                        # 시간창 '이전' 단계까지는 그대로 요구하는 민감도 변형
                        pre_window_ok = (base_decision.approved
                                         or base_reason == config.TW_REJECT_TIME_WINDOW)
                        if not pre_window_ok:
                            ok = False
                            reject = f"TW2KEEP_{base_reason}"
                    if snaps_out is not None:
                        snaps.append(SnapCandidate(
                            date=current_day, direction=p_direction.value,
                            decision_idx=idx, decision_at=bar_close_at.isoformat(),
                            slots_used_before=slots_used_today,
                            conditions=conds, passed=npass, approved=bool(ok),
                            reject=reject,
                            position_before=position["symbol"] if position else None,
                            day_extreme_margin_pct=feats.get("day_extreme_margin_pct"),
                            price_vs_vwap_pct=feats.get("price_vs_vwap_pct"),
                            ema20_slope_pct=feats.get("ema20_slope_pct"),
                            gap_signed=feats.get("gap_signed"),
                            tw2_block_reason=str(base_reason)))
                    if ok:
                        is_snap = True
                        final_approved = True
                        final_reason = "SNAPBACK_APPROVED"
                        slot_number = 4
                        session = tw3.SESSION_AFTERNOON
                        snap_conds = conds
                        snap_passed = npass

                target = order_executor.target_symbol_for_direction(p_direction)
                if not final_approved:
                    if position is not None and position["symbol"] != target:
                        if final_reason in config.TW_WHIPSAW_REJECT_REASONS:
                            whipsaw_watch = {"direction": p_direction,
                                             "last_gap": float("-inf"),
                                             "last_ema_spread": float("-inf")}
                        else:
                            close_now = etf_close[position["symbol"]].get(
                                bar_ts, position["rec"].entry_price)
                            close_trade(bar_close_at, close_now, config.EXIT_OPPOSITE_SIGNAL, idx)
                            position = None
                else:
                    fill = etf_close[target].get(bar_ts)
                    if fill is not None:
                        switched_from = None
                        if position is not None and position["symbol"] != target:
                            switched_from = position["symbol"]
                            close_now = etf_close[position["symbol"]].get(
                                bar_ts, position["rec"].entry_price)
                            close_trade(bar_close_at, close_now, config.EXIT_OPPOSITE_SIGNAL, idx)
                            position = None
                        if position is None:
                            slots_used_today += 1
                            if is_snap:
                                snap_used_today = True
                                afternoon_count += 1
                                last_afternoon_direction = p_direction.value
                            elif session == tw3.SESSION_MORNING:
                                morning_count += 1
                            else:
                                afternoon_count += 1
                                last_afternoon_direction = p_direction.value
                            entry_chop = False
                            cd = etp.evaluate_entry_chop(bars_slice, p_direction, bar_close_at)
                            if not cd.insufficient_data:
                                entry_chop = bool(cd.is_chop)
                            rec = Trade(date=current_day, slot_number=slot_number,
                                        session=session, direction=p_direction.value,
                                        decision_idx=idx, is_snapback=is_snap,
                                        entry_time=bar_close_at.isoformat(),
                                        entry_symbol=target, entry_price=fill,
                                        entry_bar_idx=idx, entry_chop=entry_chop,
                                        switched_from=switched_from,
                                        snap_conditions=snap_conds, snap_passed=snap_passed)
                            position = {"symbol": target, "entry_idx": idx,
                                        "entry_time": bar_close_at, "tp1_done": False,
                                        "peak": 0.0, "session": session, "rec": rec,
                                        "qty_frac": 1.0, "realized": 0.0}
                            whipsaw_watch = None

        if idx in flags_by_idx:
            ft = bar_start.astimezone(KST).time()
            if config.SESSION_OPEN <= ft < config.NEW_ENTRY_CUTOFF:
                pending = (flags_by_idx[idx], idx, bar_ts)

        if whipsaw_watch is not None and position is not None:
            d = twf.evaluate_whipsaw_watch(bars.iloc[: idx + 1], whipsaw_watch["direction"],
                                           whipsaw_watch["last_gap"],
                                           whipsaw_watch["last_ema_spread"])
            if not d.insufficient_data:
                if d.should_release:
                    whipsaw_watch = None
                elif d.should_sell:
                    close = etf_close[position["symbol"]].get(bar_ts)
                    if close is not None:
                        close_trade(bar_close_at, close,
                                    "WHIPSAW_WATCH_DETERIORATION_EXIT", idx)
                        position = None
                    whipsaw_watch = None
                else:
                    whipsaw_watch["last_gap"] = d.current_gap
                    whipsaw_watch["last_ema_spread"] = d.current_ema_spread

        if position is not None:
            for mo in range(3):
                tick = bar_start + timedelta(minutes=mo)
                if tick <= position["entry_time"] or tick > bar_close_at:
                    continue
                price = etf_1m_close[position["symbol"]].get(pd.Timestamp(tick))
                if price is None:
                    continue
                net = net_at(price)
                position["rec"].peak_net_pct = max(position["rec"].peak_net_pct, net)
                position["rec"].trough_net_pct = min(position["rec"].trough_net_pct, net)
                tp = twpm.evaluate_take_profit_immediate(
                    session=position["session"], net_return_pct=net,
                    tp1_done=position["tp1_done"],
                    tp2_pct_override=config.TW2_MORNING_TP2 * 100.0)
                position["peak"] = max(position["peak"], tp.peak_net_return)
                if tp.exit_reason == config.EXIT_TW_TP1_PARTIAL:
                    position["realized"] += position["qty_frac"] * tp.sell_fraction * net
                    position["qty_frac"] *= (1.0 - tp.sell_fraction)
                    position["tp1_done"] = tp.tp1_done
                elif tp.exit_reason is not None:
                    close_trade(tick, price, tp.exit_reason, idx)
                    position = None
                    whipsaw_watch = None
                    break
                else:
                    position["tp1_done"] = tp.tp1_done

        if position is not None and idx > position["entry_idx"]:
            close = etf_close[position["symbol"]].get(bar_ts)
            if close is not None:
                net = net_at(close)
                position["rec"].peak_net_pct = max(position["rec"].peak_net_pct, net)
                position["rec"].trough_net_pct = min(position["rec"].trough_net_pct, net)
                pm = twpm.evaluate_position(
                    session=position["session"], net_return_pct=net,
                    tp1_done=position["tp1_done"], peak_net_return=position["peak"],
                    tp2_pct_override=config.TW2_MORNING_TP2 * 100.0)
                position["peak"] = pm.peak_net_return
                if pm.exit_reason == config.EXIT_TW_TP1_PARTIAL:
                    position["realized"] += position["qty_frac"] * pm.sell_fraction * net
                    position["qty_frac"] *= (1.0 - pm.sell_fraction)
                    position["tp1_done"] = pm.tp1_done
                elif pm.exit_reason is not None:
                    close_trade(bar_close_at, close, pm.exit_reason, idx)
                    position = None
                    whipsaw_watch = None
                else:
                    position["tp1_done"] = pm.tp1_done
                    if position["rec"].entry_chop:
                        ed = etp.evaluate(entry_chop=True,
                                          peak_net_return_pct=position["peak"],
                                          net_return_pct=net)
                        if ed.exit_reason == config.EXIT_EARLY_TAKE_PROFIT:
                            position["rec"].lock_fired = True
                            close_trade(bar_close_at, close,
                                        config.EXIT_EARLY_TAKE_PROFIT, idx)
                            position = None
                            whipsaw_watch = None

    if position is not None:
        last_idx = len(bars) - 1
        last_dt = pd.Timestamp(bars["datetime"].iloc[last_idx]).to_pydatetime() + timedelta(minutes=3)
        close = etf_close[position["symbol"]].get(bars["datetime"].iloc[last_idx],
                                                  position["rec"].entry_price)
        close_trade(last_dt, close, "END_OF_DATA", last_idx)

    if snaps_out is not None:
        snaps_out.extend(snaps)
    return trades


def main() -> int:
    ctx = ds.build_ctx(use_cache=True)
    pre = ds.precompute(ctx.hynix_bars_3m)
    dates60 = ctx.dates
    dates30 = dates60[-30:]
    print(f"ctx {dates60[0]}~{dates60[-1]} (60일), 평가창 30일 {dates30[0]}~{dates30[-1]}")

    # C4 식이 production TW_REJECT_NOT_CONFIRMED 판정과 같은지 표본 검증
    bars = ctx.hynix_bars_3m
    checked = 0
    for fidx in sorted(ctx.flags_by_idx)[::37]:
        i = fidx + 1
        if i >= len(bars):
            continue
        d = ctx.flags_by_idx[fidx]
        sl = bars.iloc[: i + 1]
        series = twf._gap_series(sl)
        if series is None:
            continue
        sign = twf._direction_sign(d)
        gap_now = float(series["gap"].iloc[-1]) * sign
        mine = pre.gap[i] * (1.0 if d == Direction.UP_RED else -1.0)
        assert abs(gap_now - mine) < 1e-6, (fidx, gap_now, mine)
        checked += 1
    print(f"C4 식 검증(production _gap_series 대비) 표본 {checked}개 통과")

    snaps_b: list = []
    snaps_k: list = []
    trades_a = run_chain(ctx, pre, snapback=False)
    trades_b = run_chain(ctx, pre, snapback=True, snaps_out=snaps_b)
    trades_k = run_chain(ctx, pre, snapback=True, tw2_keep=True, snaps_out=snaps_k)

    # A 재현 검증 (직전 검증된 30일 엔진과 동일한지)
    prev = PROJECT_ROOT / "data" / "validation" / "slot23_tq_fullchain" / "raw.json"
    if prev.exists():
        old = json.loads(prev.read_text(encoding="utf-8"))["A"]
        def k(t):
            return (t["date"], t["entry_time"], t["direction"],
                    round(t["net_pct"], 6), t["exit_reason"])
        o = {k(t) for t in old}
        n = {k(vars(t)) for t in trades_a if t.date in set(dates30)}
        print(f"A 재현 검증(30일 구간, 직전 검증엔진과 동일): {o == n}  "
              f"[old {len(o)} / new {len(n)}]")

    n30 = lambda ts: [t for t in ts if t.date in set(dates30)]
    print(f"30일: A {len(n30(trades_a))}건 / B {len(n30(trades_b))}건 "
          f"(SNAPBACK {sum(1 for t in n30(trades_b) if t.is_snapback)}건) / "
          f"B_TW2KEEP {len(n30(trades_k))}건 "
          f"(SNAPBACK {sum(1 for t in n30(trades_k) if t.is_snapback)}건)")
    print(f"60일: A {len(trades_a)} / B {len(trades_b)} "
          f"(SNAP {sum(1 for t in trades_b if t.is_snapback)})")
    print(f"SNAPBACK 후보 도달 {len(snaps_b)}건, 승인 {sum(1 for s in snaps_b if s.approved)}건")

    out = {
        "dates60": dates60, "dates30": dates30,
        "thresholds": {"window": [str(SNAPBACK_START), str(SNAPBACK_END)],
                       "THR_EXTREME": THR_EXTREME, "THR_VWAP": THR_VWAP,
                       "THR_SLOPE": THR_SLOPE, "MIN_CONDITIONS": MIN_CONDITIONS},
        "A": [vars(t) for t in trades_a],
        "B": [vars(t) for t in trades_b],
        "B_TW2KEEP": [vars(t) for t in trades_k],
        "snap_candidates_B": [vars(s) for s in snaps_b],
        "snap_candidates_K": [vars(s) for s in snaps_k],
    }
    (OUTPUT_DIR / "raw.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"saved {OUTPUT_DIR / 'raw.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
