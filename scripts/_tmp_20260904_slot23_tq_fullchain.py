"""READ-ONLY full-chain backtest (2026-09-04 사용자 요청):
TW2 3-SLOT + 조기익절필터 위에 Slot2/Slot3 품질(Trend Quality) 선별을 얹는
네 가지 안을 최근 30영업일 실데이터로 비교한다.

  A. 현행        — TW2 3-SLOT + 조기익절 필터, PRE15 OFF, 하루 최대 3회
  B. Slot2/3 TQ>=3/5 — Slot1 현행. Slot2/Slot3 후보는 기존 production 승인
                       (TW2 + veto + slot + morning-3rd TQ / afternoon TEG)을
                       모두 통과한 뒤 Trend Quality >= 3/5 를 AND-게이트로 추가
                       요구. 미달이면 진입하지 않고 슬롯을 소비하지 않는다.
  C. Slot2 TQ>=3/5 + Slot3 TQ>=4/5 — 나머지는 B와 동일.
  D. B + 슬롯 보존 — 차단된 Slot2/3 슬롯을 소진하지 않고 이후 모든 후속
                     플래그에 재사용.

Production 코드는 전혀 수정하지 않는다. 판단은 전부 production 순수함수 호출:
  - MACD zero-cross:      signal_engine.calculate_macd / evaluate_macd_crossover
  - T+3 재확인/TW2:        time_window_filter.evaluate_time_window_entry
  - TW2 추가 veto:         time_window_filter.evaluate_tw2_extra_vetoes
  - 슬롯/세션/일한도(3회):  time_window_3slot.resolve_slot
  - Trend Quality:         time_window_3slot.evaluate_trend_quality
  - TEGv2(오후 필수):       teg_gate.evaluate_teg
  - whipsaw:               time_window_filter.evaluate_whipsaw_watch
  - 조기익절 필터:          early_take_profit.evaluate_entry_chop / .evaluate
  - TP1/TP2/trailing/SL:   time_window_position_manager.
                           evaluate_take_profit_immediate / evaluate_position
  - 수수료/순수익:          worker._net_return_pct (TradeCostEngine)
  - 방향→종목:              order_executor.target_symbol_for_direction

오케스트레이션 루프는 scripts/_tmp_20260904_abc_30day_compare.run()과 동일
구조(= worker._resolve_tw2_3slot_candidate_body 제어흐름의 미러)이며,
새로 추가된 것은 Slot2/Slot3 TQ AND-게이트와 진단 기록뿐이다.

■ full-chain 규칙 (사용자 요구사항 그대로)
  - "A에서 체결된 거래 중 차단된 것만 빼는" 정적 계산을 하지 않는다. 각 안은
    독립적으로 처음부터 끝까지 시간순 재생된다.
  - 차단된 후보는 slots_used_today 를 증가시키지 않으므로, 그 뒤에 오는 모든
    플래그가 실제 후보로 다시 평가된다 — A에서 daily cap(3회) 때문에 못 들어갔던
    4·5번째 플래그도 슬롯이 남아 있으면 새로 진입 후보가 된다.
  - 후속 후보의 게이트는 "플래그 순번"이 아니라 resolve_slot 이 돌려주는
    현재 slot_number(= slots_used_today + 1) 기준으로 적용된다. 즉 Slot2가
    미체결로 남아 있으면 4번째·5번째 플래그도 계속 Slot2 조건을 받고,
    Slot2가 실제 체결된 뒤에야 다음 후보부터 Slot3 조건이 적용된다.
  - 오후 후보에는 production 오후 게이트(TEGv2 + 2번째 오후후보 flat/역방향
    조건)가 그대로 먼저 적용되고, 그 위에 TQ 게이트가 얹힌다.
  - 하루 실제 신규진입은 config.TW2_3SLOT_DAILY_CAP(=3)회를 유지한다.

주의: B 와 D 는 위 resolve_slot 정의상 구조적으로 동일한 시뮬레이션이다
(차단은 애초에 슬롯을 소비하지 않는다). 그래도 사용자가 별도 안으로 요청했으므로
독립 실행하여 산출물이 실제로 일치하는지 수치로 확인한다.
"""
from __future__ import annotations

import json
import pickle
import sys
from dataclasses import dataclass
from datetime import datetime, time as dtime, timedelta
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
from app.trading.macd2.signal_engine import (  # noqa: E402
    calculate_macd, evaluate_macd_crossover, resample_completed_3m,
)
from app.trading.macd2.worker import _net_return_pct  # noqa: E402

import backtest_time_window_filter as bt  # noqa: E402
import _tmp_20260903_chop_adaptive_exit_train_oos as ce  # noqa: E402

KST = config.KST
CACHE_DIR = PROJECT_ROOT / "data" / "cache"
OUTPUT_DIR = PROJECT_ROOT / "data" / "validation" / "slot23_tq_fullchain"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
_CTX_CACHE = OUTPUT_DIR / "_ctx_cache.pkl"

N_DAYS = 30
TRACE_DATES = {"20260904"}

REJECT_SLOT_TQ = "SLOTN_TREND_QUALITY_REJECT"


# ── ctx: _tmp_20260903_chop_adaptive_exit_train_oos.build_ctx 와 동일하되
#         "오늘"을 제외하지 않는다(장 종료 후 실행, 당일 데이터 완결).
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


@dataclass
class Trade:
    date: str
    slot_number: Optional[int]
    session: Optional[str]
    direction: str
    decision_idx: int
    flag_ordinal: int = 0          # 그날 T+3 판정에 도달한 후보 중 몇 번째인가
    entry_time: Optional[str] = None
    entry_symbol: Optional[str] = None
    entry_price: Optional[float] = None
    entry_bar_idx: Optional[int] = None
    exit_time: Optional[str] = None
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None
    exit_bar_idx: Optional[int] = None
    net_pct: Optional[float] = None
    net_pct_fullqty: Optional[float] = None
    tp1_hit: bool = False
    tp2_hit: bool = False
    peak_net_pct: float = 0.0
    trough_net_pct: float = 0.0
    entry_chop: bool = False
    chop_score_entry: Optional[int] = None
    lock_armed: bool = False
    lock_fired: bool = False
    tq_passed: Optional[int] = None       # 이 진입에 적용된 TQ 게이트 통과 개수
    tq_required: Optional[int] = None
    hold_bars: int = 0


@dataclass
class Block:
    """Slot2/Slot3 TQ AND-게이트에서 차단된 후보."""
    date: str
    direction: str
    decision_idx: int
    decision_at: str
    slot_number: int
    session: str
    flag_ordinal: int
    tq_passed: int
    tq_required: int


def run(
    ctx: Ctx,
    *,
    slot2_tq: Optional[int],
    slot3_tq: Optional[int],
    early_tp_enabled: bool = True,
    blocks_out: Optional[list] = None,
    trace_out: Optional[list] = None,
) -> list:
    bars = ctx.hynix_bars_3m
    flags_by_idx = ctx.flags_by_idx
    etf_close = ctx.etf_close
    etf_1m_close = ctx.etf_1m_close
    date_set = set(ctx.dates)

    trades: list = []
    blocks: list = []
    position: Optional[dict] = None
    pending = None

    slots_used_today = 0
    morning_count = 0
    afternoon_count = 0
    flag_ordinal_today = 0
    last_afternoon_direction: Optional[str] = None
    current_day: Optional[str] = None
    whipsaw_watch: Optional[dict] = None

    def position_direction():
        return bt._direction_for_symbol(position["symbol"]) if position is not None else None

    def net_at(price: float) -> float:
        return float(_net_return_pct(position["symbol"], position["rec"].entry_price, price, 1))

    def close_trade(exit_time, exit_price, reason, idx):
        rec = position["rec"]
        rec.exit_time = ce._fmt(exit_time)
        rec.exit_price = exit_price
        rec.exit_reason = reason
        rec.exit_bar_idx = idx
        leg = net_at(exit_price)
        rec.net_pct = round(position["realized"] + position["qty_frac"] * leg, 6)
        rec.net_pct_fullqty = round(leg, 6)
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
            slots_used_today = 0
            morning_count = 0
            afternoon_count = 0
            flag_ordinal_today = 0
            last_afternoon_direction = None
            pending = None
            whipsaw_watch = None
        bar_close_at = bar_start + timedelta(minutes=3)

        # 1) 15:00 강제청산
        if position is not None and bar_close_at.astimezone(KST).time() >= config.FORCE_LIQUIDATE_AT:
            close = etf_close[position["symbol"]].get(bar_ts)
            if close is not None:
                close_trade(bar_close_at, close, config.EXIT_FORCED_LIQUIDATION, idx)
                position = None
            pending = None
            whipsaw_watch = None

        # 2) 직전 봉 후보를 T+3에서 확정 판정
        if pending is not None:
            p_direction, p_idx, p_bar_ts = pending
            if idx == p_idx + 1:
                pending = None
                flag_ordinal_today += 1
                flag_ord = flag_ordinal_today
                flag_bar_dt = pd.Timestamp(p_bar_ts).to_pydatetime()
                bars_slice = bars.iloc[: idx + 1]
                slot_number = None
                session = None
                applied_tq_passed = None
                applied_tq_required = None
                tr: Optional[dict] = None
                if trace_out is not None and current_day in TRACE_DATES:
                    tr = {
                        "date": current_day, "flag_ordinal": flag_ord,
                        "direction": p_direction.value,
                        "flag_bar_at": flag_bar_dt.isoformat(),
                        "decision_at": bar_close_at.isoformat(),
                        "slots_used_before": slots_used_today,
                        "morning_count_before": morning_count,
                        "afternoon_count_before": afternoon_count,
                        "position_before": position["symbol"] if position else None,
                    }

                base_decision = twf.evaluate_time_window_entry(
                    bars_slice, p_direction, flag_bar_dt, bar_close_at,
                    position_direction=position_direction(),
                    morning_entry_count=0, afternoon_entry_count=0, daily_entry_count=0,
                )
                tw2_cleared = bool(base_decision.approved)
                base_reason = base_decision.block_reason
                if tr is not None:
                    tr["tw2_approved"] = tw2_cleared
                    tr["tw2_block_reason"] = str(base_reason)
                if tw2_cleared:
                    vetoed, veto_reason = twf.evaluate_tw2_extra_vetoes(
                        bars_slice, p_direction, flag_bar_dt, bar_close_at)
                    if tr is not None:
                        tr["tw2_extra_veto"] = bool(vetoed)
                        tr["tw2_extra_veto_reason"] = str(veto_reason) if vetoed else None
                    if vetoed:
                        tw2_cleared = False
                        base_reason = veto_reason

                final_approved = False
                final_reason = base_reason
                if tw2_cleared:
                    slot_decision = tw3.resolve_slot(
                        now=bar_close_at, slots_used_today=slots_used_today,
                        morning_count=morning_count, afternoon_count=afternoon_count,
                        direction=p_direction, is_flat=(position is None),
                        last_afternoon_direction=last_afternoon_direction,
                    )
                    slot_number = slot_decision.slot_number
                    session = slot_decision.session
                    if tr is not None:
                        tr["slot_allowed"] = slot_decision.slot_allowed
                        tr["slot_number"] = slot_number
                        tr["session"] = session
                        tr["requires_quality_gate"] = slot_decision.requires_quality_gate
                        tr["requires_teg_gate"] = slot_decision.requires_teg_gate
                        tr["slot_reject_reason"] = slot_decision.reject_reason
                    if not slot_decision.slot_allowed:
                        final_reason = slot_decision.reject_reason
                    else:
                        # ── production 기본 게이트 (변경 없음) ──
                        if slot_decision.requires_quality_gate:
                            q = tw3.evaluate_trend_quality(bars_slice, p_direction)
                            final_approved = q.approved
                            final_reason = (config.TW_APPROVED if q.approved
                                            else config.TW2_3SLOT_REJECT_QUALITY)
                            if tr is not None:
                                tr["prod_quality_passed"] = q.passed_count
                                tr["prod_quality_required"] = q.required
                                tr["prod_quality_approved"] = q.approved
                        elif slot_decision.requires_teg_gate:
                            t = teg_gate.evaluate_teg(bars_slice, p_direction,
                                                      flag_bar_dt, bar_close_at)
                            final_approved = t.approved
                            final_reason = (config.TW_APPROVED if t.approved
                                            else config.TW2_3SLOT_REJECT_TEG)
                            if tr is not None:
                                tr["teg_approved"] = t.approved
                                tr["teg_block_reason"] = getattr(t, "block_reason", None)
                        else:
                            final_approved = True
                            final_reason = config.TW_APPROVED

                        # ── 신규 AND-게이트: 현재 slot_number 기준 TQ 요구 ──
                        need = None
                        if slot_number == 2:
                            need = slot2_tq
                        elif slot_number == 3:
                            need = slot3_tq
                        if tr is not None:
                            tr["extra_tq_required"] = need
                        if need is not None and final_approved:
                            q = tw3.evaluate_trend_quality(bars_slice, p_direction, required=need)
                            applied_tq_passed = q.passed_count
                            applied_tq_required = need
                            if tr is not None:
                                tr["extra_tq_passed"] = q.passed_count
                                tr["extra_tq_approved"] = q.approved
                                tr["extra_tq_conditions"] = dict(q.conditions)
                            if not q.approved:
                                final_approved = False
                                final_reason = f"{REJECT_SLOT_TQ}_SLOT{slot_number}_{need}OF5"
                                blocks.append(Block(
                                    date=current_day, direction=p_direction.value,
                                    decision_idx=idx, decision_at=bar_close_at.isoformat(),
                                    slot_number=slot_number, session=session,
                                    flag_ordinal=flag_ord,
                                    tq_passed=q.passed_count, tq_required=need))
                if tr is not None:
                    tr["final_approved"] = bool(final_approved)
                    tr["final_reason"] = str(final_reason)
                    trace_out.append(tr)

                target = order_executor.target_symbol_for_direction(p_direction)
                if not final_approved:
                    is_whipsaw = final_reason in config.TW_WHIPSAW_REJECT_REASONS
                    if position is not None and position["symbol"] != target:
                        if is_whipsaw:
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
                        if position is not None and position["symbol"] != target:
                            close_now = etf_close[position["symbol"]].get(
                                bar_ts, position["rec"].entry_price)
                            close_trade(bar_close_at, close_now, config.EXIT_OPPOSITE_SIGNAL, idx)
                            position = None
                        if position is None:
                            slots_used_today += 1
                            if session == tw3.SESSION_MORNING:
                                morning_count += 1
                            else:
                                afternoon_count += 1
                                last_afternoon_direction = p_direction.value
                            entry_chop = False
                            chop_score = None
                            chop_dec = etp.evaluate_entry_chop(bars_slice, p_direction, bar_close_at)
                            if not chop_dec.insufficient_data:
                                entry_chop = bool(chop_dec.is_chop)
                                chop_score = int(chop_dec.score)
                            rec = Trade(
                                date=current_day, slot_number=slot_number, session=session,
                                direction=p_direction.value, decision_idx=idx,
                                flag_ordinal=flag_ord,
                                entry_time=bar_close_at.isoformat(),
                                entry_symbol=target, entry_price=fill, entry_bar_idx=idx,
                                entry_chop=entry_chop, chop_score_entry=chop_score,
                                tq_passed=applied_tq_passed, tq_required=applied_tq_required,
                            )
                            position = {"symbol": target, "entry_idx": idx,
                                        "entry_time": bar_close_at, "tp1_done": False,
                                        "peak": 0.0, "session": session, "rec": rec,
                                        "qty_frac": 1.0, "realized": 0.0}
                            whipsaw_watch = None

        # 3) 새 플래그 등록
        if idx in flags_by_idx:
            flag_time = bar_start.astimezone(KST).time()
            if config.SESSION_OPEN <= flag_time < config.NEW_ENTRY_CUTOFF:
                pending = (flags_by_idx[idx], idx, bar_ts)

        # 4) whipsaw 추적
        if whipsaw_watch is not None and position is not None:
            decision = twf.evaluate_whipsaw_watch(
                bars.iloc[: idx + 1], whipsaw_watch["direction"],
                whipsaw_watch["last_gap"], whipsaw_watch["last_ema_spread"],
            )
            if not decision.insufficient_data:
                if decision.should_release:
                    whipsaw_watch = None
                elif decision.should_sell:
                    close = etf_close[position["symbol"]].get(bar_ts)
                    if close is not None:
                        close_trade(bar_close_at, close, "WHIPSAW_WATCH_DETERIORATION_EXIT", idx)
                        position = None
                    whipsaw_watch = None
                else:
                    whipsaw_watch["last_gap"] = decision.current_gap
                    whipsaw_watch["last_ema_spread"] = decision.current_ema_spread

        # 5) 틱(1분 종가) — TP만 틱 판정. 조기익절 필터는 arm만.
        if position is not None:
            for minute_offset in range(3):
                tick_time = bar_start + timedelta(minutes=minute_offset)
                if tick_time <= position["entry_time"] or tick_time > bar_close_at:
                    continue
                price = etf_1m_close[position["symbol"]].get(pd.Timestamp(tick_time))
                if price is None:
                    continue
                net = net_at(price)
                position["rec"].peak_net_pct = max(position["rec"].peak_net_pct, net)
                position["rec"].trough_net_pct = min(position["rec"].trough_net_pct, net)
                tp = twpm.evaluate_take_profit_immediate(
                    session=position["session"], net_return_pct=net,
                    tp1_done=position["tp1_done"],
                    tp2_pct_override=config.TW2_MORNING_TP2 * 100.0,
                )
                position["peak"] = max(position["peak"], tp.peak_net_return)
                if early_tp_enabled and position["rec"].entry_chop:
                    etp_dec = etp.evaluate(entry_chop=True, peak_net_return_pct=position["peak"],
                                           net_return_pct=net)
                    if etp_dec.armed:
                        position["rec"].lock_armed = True
                if tp.exit_reason == config.EXIT_TW_TP1_PARTIAL:
                    position["rec"].tp1_hit = True
                    position["realized"] += position["qty_frac"] * tp.sell_fraction * net
                    position["qty_frac"] *= (1.0 - tp.sell_fraction)
                    position["tp1_done"] = tp.tp1_done
                elif tp.exit_reason is not None:
                    if tp.exit_reason == config.EXIT_TW_TP2_FULL:
                        position["rec"].tp2_hit = True
                    close_trade(tick_time, price, tp.exit_reason, idx)
                    position = None
                    whipsaw_watch = None
                    break
                else:
                    position["tp1_done"] = tp.tp1_done

        # 6) 완성봉 종가 래더 (+ 조기익절 필터 발동은 여기서만)
        if position is not None and idx > position["entry_idx"]:
            close = etf_close[position["symbol"]].get(bar_ts)
            if close is not None:
                net = net_at(close)
                position["rec"].peak_net_pct = max(position["rec"].peak_net_pct, net)
                position["rec"].trough_net_pct = min(position["rec"].trough_net_pct, net)
                pm = twpm.evaluate_position(
                    session=position["session"], net_return_pct=net,
                    tp1_done=position["tp1_done"], peak_net_return=position["peak"],
                    tp2_pct_override=config.TW2_MORNING_TP2 * 100.0,
                )
                position["peak"] = pm.peak_net_return
                if pm.exit_reason == config.EXIT_TW_TP1_PARTIAL:
                    position["rec"].tp1_hit = True
                    position["realized"] += position["qty_frac"] * pm.sell_fraction * net
                    position["qty_frac"] *= (1.0 - pm.sell_fraction)
                    position["tp1_done"] = pm.tp1_done
                elif pm.exit_reason is not None:
                    if pm.exit_reason == config.EXIT_TW_TP2_FULL:
                        position["rec"].tp2_hit = True
                    close_trade(bar_close_at, close, pm.exit_reason, idx)
                    position = None
                    whipsaw_watch = None
                else:
                    position["tp1_done"] = pm.tp1_done
                    if early_tp_enabled and position["rec"].entry_chop:
                        etp_dec = etp.evaluate(entry_chop=True, peak_net_return_pct=position["peak"],
                                               net_return_pct=net)
                        if etp_dec.armed:
                            position["rec"].lock_armed = True
                        if etp_dec.exit_reason == config.EXIT_EARLY_TAKE_PROFIT:
                            position["rec"].lock_fired = True
                            close_trade(bar_close_at, close, config.EXIT_EARLY_TAKE_PROFIT, idx)
                            position = None
                            whipsaw_watch = None

    if position is not None:
        last_idx = len(bars) - 1
        last_dt = pd.Timestamp(bars["datetime"].iloc[last_idx]).to_pydatetime() + timedelta(minutes=3)
        close = etf_close[position["symbol"]].get(bars["datetime"].iloc[last_idx],
                                                  position["rec"].entry_price)
        close_trade(last_dt, close, "END_OF_DATA", last_idx)

    if blocks_out is not None:
        blocks_out.extend(blocks)
    return trades


def main() -> int:
    ctx = build_ctx(use_cache=True)
    print(f"전체 {len(ctx.dates)}영업일 {ctx.dates[0]}~{ctx.dates[-1]}  "
          f"3분봉 {len(ctx.hynix_bars_3m)}개  확정플래그 {len(ctx.flags_by_idx)}개")

    blocks_b: list = []
    blocks_c: list = []
    blocks_d: list = []
    trace_a: list = []
    trace_b: list = []
    trace_c: list = []
    trace_d: list = []

    trades_a = run(ctx, slot2_tq=None, slot3_tq=None, trace_out=trace_a)
    trades_b = run(ctx, slot2_tq=3, slot3_tq=3, blocks_out=blocks_b, trace_out=trace_b)
    trades_c = run(ctx, slot2_tq=3, slot3_tq=4, blocks_out=blocks_c, trace_out=trace_c)
    trades_d = run(ctx, slot2_tq=3, slot3_tq=3, blocks_out=blocks_d, trace_out=trace_d)

    print(f"A {len(trades_a)}건 / B {len(trades_b)}건 / C {len(trades_c)}건 / D {len(trades_d)}건")
    print(f"차단: B {len(blocks_b)}건 / C {len(blocks_c)}건 / D {len(blocks_d)}건")
    same_bd = ([vars(t) for t in trades_b] == [vars(t) for t in trades_d])
    print(f"B == D (거래 전체 동일): {same_bd}")

    # 09-04 플래그 목록 (트레이스 대조용)
    day_flags = []
    for i, d in sorted(ctx.flags_by_idx.items()):
        bar_start = pd.Timestamp(ctx.hynix_bars_3m["datetime"].iloc[i]).to_pydatetime()
        if bar_start.strftime("%Y%m%d") in TRACE_DATES:
            day_flags.append({
                "idx": i, "direction": d.value,
                "flag_bar_at": bar_start.isoformat(),
                "confirm_at": (bar_start + timedelta(minutes=3)).isoformat(),
            })

    out = {
        "dates": ctx.dates,
        "A": [vars(t) for t in trades_a],
        "B": [vars(t) for t in trades_b],
        "C": [vars(t) for t in trades_c],
        "D": [vars(t) for t in trades_d],
        "blocks_B": [vars(b) for b in blocks_b],
        "blocks_C": [vars(b) for b in blocks_c],
        "blocks_D": [vars(b) for b in blocks_d],
        "trace": {"A": trace_a, "B": trace_b, "C": trace_c, "D": trace_d},
        "trace_day_flags": day_flags,
        "B_equals_D": same_bd,
    }
    (OUTPUT_DIR / "raw.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"saved {OUTPUT_DIR / 'raw.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
