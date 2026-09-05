"""READ-ONLY backtest (2026-09-04 사용자 요청): TW2 3-SLOT A/B/C 30영업일 비교.
production 코드 수정/commit/push 없음 — 이 스크립트는 신규 파일이고 기존
production 모듈은 순수 함수 호출로만 사용한다.

  A. 현행 TW2 3-SLOT (PRE15 승계 OFF는 TW2_3SLOT 경로 자체의 구조이므로 별도
     플래그 불필요, 하루 최대 3회 = config.TW2_3SLOT_DAILY_CAP)
  B. A + 조기익절 필터(Early Take-Profit) ON — Entry-CHOP 거래만 대상,
     MFE +1.5% 도달 시 arm, 완성봉 종가 +0.8% 이하로 내려오면 발동(전량청산).
     TP1/TP2/trailing/손절이 먼저 발동하면 그게 우선(production과 동일 순서).
  C. B + Slot1(그날 첫 신규진입)에만 Trend Quality >= 4/5 추가 요구.

공통조건은 전부 production 함수를 그대로 호출한다(신규 구현 없음):
  - MACD zero-cross:  signal_engine.calculate_macd / evaluate_macd_crossover
  - T+3/TW2 게이트:    time_window_filter.evaluate_time_window_entry /
                       evaluate_tw2_extra_vetoes
  - 슬롯/세션/일한도:   time_window_3slot.resolve_slot (하루 최대 3회)
  - Trend Quality:     time_window_3slot.evaluate_trend_quality(required=4)
  - TEGv2:             teg_gate.evaluate_teg
  - whipsaw:           time_window_filter.evaluate_whipsaw_watch
  - TP1/TP2/trailing/손절: time_window_position_manager.
                       evaluate_take_profit_immediate / evaluate_position
  - 조기익절 필터:      app.trading.macd2.early_take_profit.evaluate_entry_chop
                       / .evaluate (실제 production 모듈, 재구현 아님 — 어제
                       확정된 그대로: EARLY_TP_TRIGGER_PCT=1.5,
                       EARLY_TP_FLOOR_PCT=0.8, score_min=3/4)
  - 수수료/체결가/순수익: worker._net_return_pct (TradeCostEngine),
                       order_executor.target_symbol_for_direction

데이터: _tmp_20260903_chop_adaptive_exit_train_oos.build_ctx() 재사용 —
최근 30영업일(20260721~20260903, 하이닉스+LONG/INVERSE ETF 1분봉 실데이터)를
그대로 로드한다. 오케스트레이션 루프는 _tmp_20260903_tw2_3slot_ABCD_train_oos.
run()과 동일 구조이되, CHOP/lock 판정을 그 스크립트의 로컬 재구현 대신 실제
early_take_profit 모듈 호출로 교체했다(동일 로직 보장, 드리프트 방지).
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from datetime import timedelta
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
from app.trading.macd2.worker import _net_return_pct  # noqa: E402

import backtest_time_window_filter as bt  # noqa: E402
import _tmp_20260903_chop_adaptive_exit_train_oos as ce  # noqa: E402

KST = config.KST
OUTPUT_DIR = PROJECT_ROOT / "data" / "validation" / "abc_30day_compare"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

REJECT_SLOT1_TQ4 = "SLOT1_TREND_QUALITY_4OF5_REJECT"


@dataclass
class Trade:
    date: str
    slot_number: Optional[int]
    session: Optional[str]
    direction: str
    decision_idx: int
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
    slot1_tq_passed: Optional[int] = None
    hold_bars: int = 0
    prior_completed_today: int = 0
    prior_cum_pnl_today: float = 0.0


@dataclass
class Slot1Block:
    """Slot1 Trend Quality>=4/5 게이트 때문에 차단된 후보 (C에서만 발생)."""
    date: str
    direction: str
    decision_idx: int
    decision_at: str
    tq_passed: int


def run(
    ctx,
    *,
    slot1_tq4: bool,
    early_tp_enabled: bool,
    frozen: Optional[dict] = None,
    record_decisions: bool = False,
    blocks_out: Optional[list] = None,
) -> tuple[list, dict]:
    bars = ctx.hynix_bars_3m
    flags_by_idx = ctx.flags_by_idx
    etf_close = ctx.etf_close
    etf_1m_close = ctx.etf_1m_close
    date_set = set(ctx.dates)

    trades: list = []
    decisions: dict = {}
    blocks: list = []
    position: Optional[dict] = None
    pending = None

    completed_today: list = []
    slots_used_today = 0
    morning_count = 0
    afternoon_count = 0
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
        completed_today.append(rec.net_pct)

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
            last_afternoon_direction = None
            pending = None
            whipsaw_watch = None
            completed_today = []
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
                flag_bar_dt = pd.Timestamp(p_bar_ts).to_pydatetime()
                bars_slice = bars.iloc[: idx + 1]
                dec_key = f"{idx}|{p_direction.value}"
                slot1_tq = None

                if frozen is not None:
                    fr = frozen.get(dec_key)
                    if fr is None:
                        final_approved, final_reason = False, "FROZEN_NO_DECISION"
                        slot_number = session = None
                    else:
                        final_approved = fr["approved"]
                        final_reason = fr["reason"]
                        slot_number = fr["slot_number"]
                        session = fr["session"]
                else:
                    slot_number = None
                    session = None
                    base_decision = twf.evaluate_time_window_entry(
                        bars_slice, p_direction, flag_bar_dt, bar_close_at,
                        position_direction=position_direction(),
                        morning_entry_count=0, afternoon_entry_count=0, daily_entry_count=0,
                    )
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
                        slot_decision = tw3.resolve_slot(
                            now=bar_close_at, slots_used_today=slots_used_today,
                            morning_count=morning_count, afternoon_count=afternoon_count,
                            direction=p_direction, is_flat=(position is None),
                            last_afternoon_direction=last_afternoon_direction,
                        )
                        slot_number = slot_decision.slot_number
                        session = slot_decision.session
                        if not slot_decision.slot_allowed:
                            final_reason = slot_decision.reject_reason
                        else:
                            if slot_decision.requires_quality_gate:
                                q = tw3.evaluate_trend_quality(bars_slice, p_direction)
                                final_approved = q.approved
                                final_reason = (config.TW_APPROVED if q.approved
                                                else config.TW2_3SLOT_REJECT_QUALITY)
                            elif slot_decision.requires_teg_gate:
                                t = teg_gate.evaluate_teg(bars_slice, p_direction,
                                                          flag_bar_dt, bar_close_at)
                                final_approved = t.approved
                                final_reason = (config.TW_APPROVED if t.approved
                                                else config.TW2_3SLOT_REJECT_TEG)
                            else:
                                final_approved = True
                                final_reason = config.TW_APPROVED
                            # ── C 추가 AND-게이트: Slot1만 TQ >= 4/5 ─────
                            if slot1_tq4 and final_approved and slot_number == 1:
                                q4 = tw3.evaluate_trend_quality(bars_slice, p_direction, required=4)
                                slot1_tq = q4.passed_count
                                if not q4.approved:
                                    final_approved = False
                                    final_reason = REJECT_SLOT1_TQ4
                                    blocks.append(Slot1Block(
                                        date=current_day, direction=p_direction.value,
                                        decision_idx=idx, decision_at=bar_close_at.isoformat(),
                                        tq_passed=q4.passed_count))
                    if record_decisions:
                        decisions[dec_key] = {
                            "approved": bool(final_approved), "reason": str(final_reason),
                            "slot_number": slot_number, "session": session,
                            "date": current_day, "direction": p_direction.value,
                        }

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
                                entry_time=bar_close_at.isoformat(),
                                entry_symbol=target, entry_price=fill, entry_bar_idx=idx,
                                entry_chop=entry_chop, chop_score_entry=chop_score,
                                slot1_tq_passed=slot1_tq,
                                prior_completed_today=len(completed_today),
                                prior_cum_pnl_today=round(sum(completed_today), 6),
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

        # 5) 틱(1분 종가) — TP만 틱 판정. 조기익절 필터는 arm만(발동은 완성봉).
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
    return trades, decisions


def main() -> int:
    ctx = ce.build_ctx(use_cache=False)
    print(f"전체 {len(ctx.dates)}영업일 {ctx.dates[0]}~{ctx.dates[-1]}")

    dec_a: dict = {}
    dec_b: dict = {}
    slot1_blocks: list = []

    trades_a, dec_a = run(ctx, slot1_tq4=False, early_tp_enabled=False, record_decisions=True)
    trades_b, dec_b = run(ctx, slot1_tq4=False, early_tp_enabled=True, record_decisions=True)
    trades_c, dec_c = run(ctx, slot1_tq4=True, early_tp_enabled=True, record_decisions=True,
                          blocks_out=slot1_blocks)
    # B_frozen: A와 완전히 동일한 진입 집합에 조기익절 필터만 적용 (청산 단독효과 분리용)
    trades_b_frozen, _ = run(ctx, slot1_tq4=False, early_tp_enabled=True, frozen=dec_a)

    print(f"A 거래 {len(trades_a)}건 / B 거래 {len(trades_b)}건 / C 거래 {len(trades_c)}건 "
          f"/ B_frozen 거래 {len(trades_b_frozen)}건")
    print(f"C Slot1 TQ<4 차단 후보 {len(slot1_blocks)}건")

    out = {
        "dates": ctx.dates,
        "A": [vars(t) for t in trades_a],
        "B": [vars(t) for t in trades_b],
        "C": [vars(t) for t in trades_c],
        "B_frozen": [vars(t) for t in trades_b_frozen],
        "decisions_A": dec_a,
        "decisions_B": dec_b,
        "slot1_blocks_C": [vars(b) for b in slot1_blocks],
    }
    (OUTPUT_DIR / "raw.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"saved {OUTPUT_DIR / 'raw.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
