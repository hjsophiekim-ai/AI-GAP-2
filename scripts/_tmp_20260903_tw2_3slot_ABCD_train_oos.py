"""READ-ONLY backtest (2026-09-03 사용자 요청 2차): TW2 3-SLOT 개선안 A/B/C/D
비교. production 수정/commit/push 없음.

  A. 현행 TW2 3-SLOT (PRE15 승계 OFF, 하루 최대 3회 = config.TW2_3SLOT_DAILY_CAP)
  B. A + Slot1(그날 첫 신규진입)에만 Trend Quality >= 4/5 추가 요구
     (Slot2/3 게이트는 현행 그대로)
  C. A + Entry-CHOP Profit Lock — 진입 확정 시점에 CHOP으로 판정된 거래에만,
     MFE(peak 순수익) +1.5% 도달 시 profit lock 활성화(전량익절 아님).
     TP1/TP2/trailing/손절은 전부 그대로 살아 있고, lock은 "최소 보호선"으로
     production 활성 스탑 위에 얹힌다(effective stop = max(production stop,
     lock floor) — 아래 구현 주석 참조).
  D. B + C 결합

공통조건은 전부 production 함수를 그대로 호출해서 얻는다(신규 구현 없음):
  - MACD zero-cross:  signal_engine.calculate_macd / evaluate_macd_crossover
  - T+3 기본 게이트:   time_window_filter.evaluate_time_window_entry
  - TW2 추가 veto:     time_window_filter.evaluate_tw2_extra_vetoes
  - 슬롯/세션/일한도:   time_window_3slot.resolve_slot (하루 최대 3회)
  - Trend Quality:     time_window_3slot.evaluate_trend_quality (B/D는 이 함수의
                       기존 `required=` 파라미터로 4/5를 요구 — 새 점수식 아님)
  - TEGv2:             teg_gate.evaluate_teg
  - whipsaw:           config.TW_WHIPSAW_REJECT_REASONS +
                       time_window_filter.evaluate_whipsaw_watch
  - TP1/TP2/trailing/손절: time_window_position_manager.
                       evaluate_take_profit_immediate / evaluate_position
                       (production과 동일한 TW2_MORNING_TP2 override)
  - 수수료/체결가/순수익: worker._net_return_pct (TradeCostEngine),
                       order_executor.target_symbol_for_direction, 실제 ETF
                       1분/3분 종가
  - CHOP 판정:          1차 요청 TRAIN에서 확정한 정의를 그대로 재사용
                       (_tmp_20260903_chop_adaptive_exit_train_oos.chop_conditions,
                        cross>=1 / flip>=3 / score>=3) — 재조정 없음

새로 쓴 코드는 3분봉 오케스트레이션 루프(1차 요청 스크립트와 동일 구조)와
B의 Slot1 TQ>=4 AND-게이트, C의 profit lock 규칙뿐이다.

진입 동결에 대하여
------------------
B/D는 진입 게이트 자체를 바꾸므로 진입 집합이 A와 달라지는 것이 정상이고,
그 연쇄효과(슬롯 소비/포지션 상태 변화까지)를 전부 재현한 full-chain 재생이
유일하게 옳은 비교다. C는 청산만 바꾸므로 원리상 A와 진입이 같아야 하지만,
청산이 빨라져 포지션이 먼저 비면 evaluate_time_window_entry의 피라미딩 금지
분기와 resolve_slot의 is_flat 분기를 통해 진입이 달라질 수 있다. 따라서 C는
(1) full-chain(표 본문) 과 (2) A의 후보 승인/거절을 그대로 재생한 진입 동결본
(pair table 용) 두 가지를 모두 돌리고, 둘이 일치하는지 검증해 보고한다.
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass
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
from app.trading.macd2 import time_window_3slot as tw3  # noqa: E402
from app.trading.macd2 import time_window_filter as twf  # noqa: E402
from app.trading.macd2 import time_window_position_manager as twpm  # noqa: E402
from app.trading.macd2.worker import _net_return_pct  # noqa: E402

import backtest_time_window_filter as bt  # noqa: E402
import _tmp_20260903_chop_adaptive_exit_train_oos as ce  # noqa: E402

KST = config.KST
OUTPUT_DIR = PROJECT_ROOT / "data" / "validation" / "tw2_3slot_ABCD"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# 1차 요청 TRAIN에서 확정된 CHOP 정의 — 재조정 금지, 그대로 재사용
FROZEN_CHOP_CFG = ce.ChopConfig(cross_min=1, flip_min=3, score_min=3)
LOCK_TRIGGER_PCT = 1.5
EXIT_PROFIT_LOCK = "ENTRY_CHOP_PROFIT_LOCK"
REJECT_SLOT1_TQ4 = "SLOT1_TREND_QUALITY_4OF5_REJECT"
REJECT_SLOT3_TWO_LOSS_TEG = "SLOT3_TWO_LOSS_PROTECT_TEG_REJECT"
REJECT_SLOT3_NEG_PNL_TQ4 = "SLOT3_NEG_PNL_PROTECT_TQ4_REJECT"


@dataclass
class Trade:
    date: str
    slot_number: Optional[int]
    session: Optional[str]
    direction: str
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
    # Slot3 보호조건 진단용 (진입 성사된 거래에만 기록)
    prior_completed_today: int = 0
    prior_cum_pnl_today: float = 0.0


@dataclass
class Slot3Block:
    """보호조건 때문에 차단된 Slot3 후보 (진입 안 됨)."""
    date: str
    direction: str
    decision_at: str
    reason: str
    prior_completed_today: int
    prior_cum_pnl_today: float
    first2_losses: bool
    teg_approved: Optional[bool] = None
    tq_passed: Optional[int] = None


def run(
    ctx,
    *,
    slot1_tq4: bool,
    lock_floor_pct: Optional[float],
    frozen: Optional[dict] = None,
    record_decisions: bool = False,
    slot3_two_loss_teg: bool = False,
    slot3_negative_pnl_tq4: bool = False,
    blocks_out: Optional[list] = None,
) -> tuple[list, dict]:
    """`slot1_tq4`: B/D의 Slot1 Trend Quality >= 4/5 AND-게이트.
    `lock_floor_pct`: C/D의 Entry-CHOP profit lock 최소 보호선(None이면 미적용).

    2026-09-03 3차 요청으로 추가된 Slot3 보호조건 두 개(기본 OFF이므로 기존
    호출자의 결과는 바이트 단위로 불변 -- 60일 A/C 재현 검증으로 확인):
      `slot3_two_loss_teg`      : 당일 완료된 첫 2거래가 모두 손실이면 Slot3에
                                  TEGv2 PASS를 추가로 요구
      `slot3_negative_pnl_tq4`  : Slot3 후보 시점 당일 완료거래 누적 순손익이
                                  음수이면 Slot3에 Trend Quality >= 4/5를 추가 요구
    두 조건 모두 production이 이미 승인한 후보에만 AND로 얹는다(순서상 기존
    게이트를 절대 느슨하게 만들 수 없다). "기존보다 엄격하게"라는 요구를 그대로
    지키려면 이 방식이어야 한다 -- 예컨대 오전 Slot3의 production 게이트는
    Trend Quality >= 3/5인데, 그걸 "TW2 AND TEG"로 치환해버리면 TQ 요구가
    사라져 오히려 느슨해진다(그 치환 해석도 별도 민감도로 함께 계산한다)."""
    bars = ctx.hynix_bars_3m
    flags_by_idx = ctx.flags_by_idx
    etf_close = ctx.etf_close
    etf_1m_close = ctx.etf_1m_close
    feat = ctx.feat
    date_set = set(ctx.dates)

    trades: list = []
    decisions: dict = {}
    blocks: list = []
    position: Optional[dict] = None
    pending = None

    completed_today: list = []   # 당일 완료거래 net_pct (청산 시각 순)
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
                            # ── production 게이트 (완전 무수정) ──────────────
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
                            # ── B/D 추가 AND-게이트: Slot1만 TQ >= 4/5 ─────
                            # production이 이미 승인한 후보에만 얹는다(순서상
                            # 기존 게이트를 절대 느슨하게 만들 수 없음).
                            if slot1_tq4 and final_approved and slot_number == 1:
                                q4 = tw3.evaluate_trend_quality(bars_slice, p_direction, required=4)
                                slot1_tq = q4.passed_count
                                if not q4.approved:
                                    final_approved = False
                                    final_reason = REJECT_SLOT1_TQ4
                            # ── Slot3 보호조건 (3차 요청) — 역시 production이
                            #    승인한 후보에만 AND로 얹는다.
                            if final_approved and slot_number == 3:
                                first2_losses = (len(completed_today) >= 2
                                                 and completed_today[0] <= 0
                                                 and completed_today[1] <= 0)
                                cum = sum(completed_today)
                                if slot3_two_loss_teg and first2_losses:
                                    t3 = teg_gate.evaluate_teg(bars_slice, p_direction,
                                                               flag_bar_dt, bar_close_at)
                                    if not t3.approved:
                                        final_approved = False
                                        final_reason = REJECT_SLOT3_TWO_LOSS_TEG
                                        blocks.append(Slot3Block(
                                            date=current_day, direction=p_direction.value,
                                            decision_at=bar_close_at.isoformat(),
                                            reason=final_reason,
                                            prior_completed_today=len(completed_today),
                                            prior_cum_pnl_today=round(cum, 6),
                                            first2_losses=first2_losses,
                                            teg_approved=bool(t3.approved)))
                                if slot3_negative_pnl_tq4 and final_approved and cum < 0:
                                    q3 = tw3.evaluate_trend_quality(bars_slice, p_direction,
                                                                    required=4)
                                    if not q3.approved:
                                        final_approved = False
                                        final_reason = REJECT_SLOT3_NEG_PNL_TQ4
                                        blocks.append(Slot3Block(
                                            date=current_day, direction=p_direction.value,
                                            decision_at=bar_close_at.isoformat(),
                                            reason=final_reason,
                                            prior_completed_today=len(completed_today),
                                            prior_cum_pnl_today=round(cum, 6),
                                            first2_losses=first2_losses,
                                            tq_passed=q3.passed_count))
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
                            cc = ce.chop_conditions(feat, idx, p_direction, FROZEN_CHOP_CFG)
                            rec = Trade(
                                date=current_day, slot_number=slot_number, session=session,
                                direction=p_direction.value, entry_time=bar_close_at.isoformat(),
                                entry_symbol=target, entry_price=fill, entry_bar_idx=idx,
                                entry_chop=bool(cc["is_chop"]) if cc is not None else False,
                                chop_score_entry=cc["score"] if cc is not None else None,
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

        # 5) 틱(1분 종가) 익절 체크 — production과 동일하게 TP만 틱 판정.
        #    profit lock은 STOP 성격이므로 여기서 발동시키지 않고 arming만 한다
        #    (production 주석: 하방 래더는 완성봉 종가 게이트 유지, 노이즈 틱
        #     하나로 스탑을 때리는 건 실제 사고였음).
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
                if (lock_floor_pct is not None and position["rec"].entry_chop
                        and position["peak"] >= LOCK_TRIGGER_PCT):
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

        # 6) 완성봉 종가 래더 (+ profit lock)
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
                if (lock_floor_pct is not None and position["rec"].entry_chop
                        and position["peak"] >= LOCK_TRIGGER_PCT):
                    position["rec"].lock_armed = True
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
                    # production이 HOLD라고 답한 경우에만 lock floor를 추가로 본다
                    # => effective stop = max(production 활성 스탑, lock floor).
                    #    production이 HOLD면 net > production 스탑이므로, 여기서
                    #    net <= floor로 끊는 것이 정확히 두 스탑의 max와 같다.
                    #    "최소 +X% 보호"의 문자 그대로이며 TP1/TP2/trailing은
                    #    위에서 이미 전부 그대로 평가된 뒤다.
                    if position["rec"].lock_armed and net <= lock_floor_pct:
                        position["rec"].lock_fired = True
                        close_trade(bar_close_at, close, EXIT_PROFIT_LOCK, idx)
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
    ctx = ce.build_ctx()
    print(f"전체 {len(ctx.dates)}영업일 {ctx.dates[0]}~{ctx.dates[-1]} "
          f"(TRAIN {ctx.train_dates[0]}~{ctx.train_dates[-1]} / "
          f"OOS {ctx.oos_dates[0]}~{ctx.oos_dates[-1]})")

    a, dec_a = run(ctx, slot1_tq4=False, lock_floor_pct=None, record_decisions=True)
    b, dec_b = run(ctx, slot1_tq4=True, lock_floor_pct=None, record_decisions=True)
    print(f"A 거래 {len(a)}건 / B 거래 {len(b)}건")

    out = {
        "period": {"all": ctx.dates, "train": ctx.train_dates, "oos": ctx.oos_dates},
        "chop_cfg": FROZEN_CHOP_CFG.key(),
        "A": [vars(t) for t in a],
        "B": [vars(t) for t in b],
        "decisions_A": dec_a,
        "decisions_B": dec_b,
        "C": {}, "D": {}, "C_frozen": {},
    }
    for floor in (0.3, 0.5, 0.8):
        c, _ = run(ctx, slot1_tq4=False, lock_floor_pct=floor)
        d, _ = run(ctx, slot1_tq4=True, lock_floor_pct=floor)
        cf, _ = run(ctx, slot1_tq4=False, lock_floor_pct=floor, frozen=dec_a)
        out["C"][str(floor)] = [vars(t) for t in c]
        out["D"][str(floor)] = [vars(t) for t in d]
        out["C_frozen"][str(floor)] = [vars(t) for t in cf]
        print(f"  lock floor +{floor}%: C {len(c)}건 / D {len(d)}건 / C_frozen {len(cf)}건 "
              f"(lock 발동 C={sum(1 for t in c if t.lock_fired)})")

    (OUTPUT_DIR / "raw.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"saved {OUTPUT_DIR / 'raw.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
