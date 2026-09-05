"""READ-ONLY full-chain backtest (2026-09-04 사용자 요청 #2):
현행 TW2 3-SLOT + 조기익절필터(A) 대비, 3슬롯 소진 후 오후 TEGv2 강신호에
한해 하루 1회 추가 진입을 허용하는 안(B)을 최근 30영업일 실데이터로 비교.

  A. 현행 — TW2 3-SLOT + 조기익절 필터, PRE15 OFF, 하루 최대 3회
  B. A + 오후 TEGv2 추가 1회
       - 기존 3개 슬롯 판정은 A와 완전히 동일(코드 경로 자체가 동일).
       - resolve_slot 이 TW2_3SLOT_REJECT_DAILY_SLOT_CAP(=3슬롯 소진)으로
         거절했고, 그 시점 세션이 AFTERNOON(=11:00 이후) 인 후보에 한해
         production TEGv2(teg_gate.evaluate_teg)를 그대로 호출하여
         통과하면 4번째 진입을 1회만 허용한다.
       - 상한 시각은 별도 상수를 새로 만들지 않는다. resolve_slot 이
         moment >= TW2_3SLOT_AFTERNOON_WINDOW_END(14:50) 인 후보에는
         REJECT_SLOT_CAP 이 아니라 REJECT_OUTSIDE_WINDOW 를 돌려주므로
         "11:00 이후 ~ 신규진입 마감 전" 창은 production 상수만으로 그대로
         결정된다(플래그 등록 자체도 NEW_ENTRY_CUTOFF=14:55 로 이미 제한됨).
       - 추가 진입도 T+3 재확인 / TW2 기본+추가veto / TEGv2 / whipsaw /
         조기익절 / TP1 / TP2 / trailing / SL / 15:00 강제청산을 전부
         동일하게 적용받는다(세션=AFTERNOON 으로 래더 진입).
       - 4번째가 반대신호 스위치이면 production 그대로 기존 포지션을
         OPPOSITE_SIGNAL 로 청산한 뒤 반대 ETF 에 진입한다.

TEGv2 는 재정의하지 않는다 — production 이 오후 슬롯에서 쓰는 것과 완전히
동일한 teg_gate.evaluate_teg 함수/임계값(TEG_HIST_DELTA_FLOOR 등)을 그대로
호출한다.

full-chain: A 거래에 한 건 덧붙이는 정적 계산이 아니라, B 를 처음부터 끝까지
시간순으로 독립 재생한다. 추가 1회가 체결된 뒤의 보유 포지션 상태 / 청산 /
그 이후 플래그 처리(스위치·whipsaw·강제청산)까지 전부 체인에 반영된다.

판단은 전부 production 순수함수 호출(신규 구현 없음):
  MACD zero-cross      signal_engine.calculate_macd / evaluate_macd_crossover
  T+3 재확인/TW2        time_window_filter.evaluate_time_window_entry
  TW2 추가 veto         time_window_filter.evaluate_tw2_extra_vetoes
  슬롯/세션/일한도       time_window_3slot.resolve_slot
  Trend Quality        time_window_3slot.evaluate_trend_quality
  TEGv2                teg_gate.evaluate_teg
  whipsaw              time_window_filter.evaluate_whipsaw_watch
  조기익절 필터          early_take_profit.evaluate_entry_chop / .evaluate
  TP1/TP2/trailing/SL  time_window_position_manager.
                       evaluate_take_profit_immediate / evaluate_position
  수수료/순수익          worker._net_return_pct (TradeCostEngine)
  방향→종목             order_executor.target_symbol_for_direction

데이터/ctx 는 _tmp_20260904_slot23_tq_fullchain.build_ctx() 재사용
(최근 30영업일 20260722~20260904, 하이닉스 + LONG/INVERSE ETF 실 1분봉).

참고용 민감도: B_STRICT — 사용자 스펙(창+TEGv2+1회) 에 더해 production 오후
슬롯의 "2번째 오후후보 flat + 동일방향 금지" 규칙까지 4번째에 적용한 변형.
스펙에는 없지만 production 오후 규칙을 최대한 보수적으로 재현했을 때 결과가
달라지는지 확인하기 위해 함께 돌린다.
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
from app.trading.macd2 import early_take_profit as etp  # noqa: E402
from app.trading.macd2 import time_window_3slot as tw3  # noqa: E402
from app.trading.macd2 import time_window_filter as twf  # noqa: E402
from app.trading.macd2 import time_window_position_manager as twpm  # noqa: E402
from app.trading.macd2.worker import _net_return_pct  # noqa: E402

import backtest_time_window_filter as bt  # noqa: E402
import _tmp_20260903_chop_adaptive_exit_train_oos as ce  # noqa: E402
import _tmp_20260904_slot23_tq_fullchain as base  # noqa: E402

KST = config.KST
OUTPUT_DIR = PROJECT_ROOT / "data" / "validation" / "teg4th_fullchain"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

EXTRA_SLOT_NUMBER = 4
REJECT_EXTRA_TEG = "TW2_3SLOT_EXTRA4_TEG_REJECT"
REJECT_EXTRA_USED = "TW2_3SLOT_EXTRA4_ALREADY_USED"
REJECT_EXTRA_SAME_DIR = "TW2_3SLOT_EXTRA4_SAME_DIRECTION_AFTERNOON"


@dataclass
class Trade:
    date: str
    slot_number: Optional[int]
    session: Optional[str]
    direction: str
    decision_idx: int
    flag_ordinal: int = 0
    is_extra: bool = False          # 오후 TEGv2 추가 1회로 들어온 4번째 거래
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
    hold_bars: int = 0
    switched_from: Optional[str] = None   # 스위치로 진입한 경우 직전 보유 심볼


@dataclass
class ExtraCandidate:
    """3슬롯 소진 후 오후에 도달한 4번째 후보의 TEGv2 판정 기록."""
    date: str
    direction: str
    decision_idx: int
    decision_at: str
    flag_ordinal: int
    teg_approved: bool
    teg_reject_reasons: list
    entered: bool
    reject_reason: Optional[str] = None
    position_before: Optional[str] = None


def run(
    ctx,
    *,
    extra_teg_entry: bool,
    extra_same_direction_rule: bool = False,
    extras_out: Optional[list] = None,
) -> list:
    bars = ctx.hynix_bars_3m
    flags_by_idx = ctx.flags_by_idx
    etf_close = ctx.etf_close
    etf_1m_close = ctx.etf_1m_close
    date_set = set(ctx.dates)

    trades: list = []
    extras: list = []
    position: Optional[dict] = None
    pending = None

    slots_used_today = 0
    morning_count = 0
    afternoon_count = 0
    flag_ordinal_today = 0
    extra_used_today = False
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
            extra_used_today = False
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
                is_extra = False
                extra_rec: Optional[ExtraCandidate] = None

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
                        # ── B 전용: 3슬롯 소진 + 오후 → TEGv2 강신호 1회 추가 ──
                        if (
                            extra_teg_entry
                            and slot_decision.reject_reason == config.TW2_3SLOT_REJECT_SLOT_CAP
                            and slot_decision.session == tw3.SESSION_AFTERNOON
                        ):
                            is_extra = True
                            t = teg_gate.evaluate_teg(bars_slice, p_direction,
                                                      flag_bar_dt, bar_close_at)
                            extra_rec = ExtraCandidate(
                                date=current_day, direction=p_direction.value,
                                decision_idx=idx, decision_at=bar_close_at.isoformat(),
                                flag_ordinal=flag_ord, teg_approved=bool(t.approved),
                                teg_reject_reasons=list(t.reject_reasons),
                                entered=False,
                                position_before=position["symbol"] if position else None,
                            )
                            same_dir_blocked = (
                                extra_same_direction_rule
                                and afternoon_count >= 1
                                and position is None
                                and last_afternoon_direction is not None
                                and p_direction.value == last_afternoon_direction
                            )
                            if extra_used_today:
                                extra_rec.reject_reason = REJECT_EXTRA_USED
                                final_reason = config.TW2_3SLOT_REJECT_SLOT_CAP
                            elif same_dir_blocked:
                                extra_rec.reject_reason = REJECT_EXTRA_SAME_DIR
                                final_reason = config.TW2_3SLOT_REJECT_SLOT_CAP
                            elif not t.approved:
                                extra_rec.reject_reason = REJECT_EXTRA_TEG
                                final_reason = config.TW2_3SLOT_REJECT_TEG
                            else:
                                final_approved = True
                                final_reason = config.TW_APPROVED
                                slot_number = EXTRA_SLOT_NUMBER
                                session = tw3.SESSION_AFTERNOON
                            extras.append(extra_rec)
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
                        switched_from = None
                        if position is not None and position["symbol"] != target:
                            switched_from = position["symbol"]
                            close_now = etf_close[position["symbol"]].get(
                                bar_ts, position["rec"].entry_price)
                            close_trade(bar_close_at, close_now, config.EXIT_OPPOSITE_SIGNAL, idx)
                            position = None
                        if position is None:
                            slots_used_today += 1
                            if is_extra:
                                extra_used_today = True
                                afternoon_count += 1
                                last_afternoon_direction = p_direction.value
                                if extra_rec is not None:
                                    extra_rec.entered = True
                            elif session == tw3.SESSION_MORNING:
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
                                flag_ordinal=flag_ord, is_extra=is_extra,
                                entry_time=bar_close_at.isoformat(),
                                entry_symbol=target, entry_price=fill, entry_bar_idx=idx,
                                entry_chop=entry_chop, chop_score_entry=chop_score,
                                switched_from=switched_from,
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
                if position["rec"].entry_chop:
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
                    if position["rec"].entry_chop:
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

    if extras_out is not None:
        extras_out.extend(extras)
    return trades


def main() -> int:
    ctx = base.build_ctx(use_cache=True)
    print(f"기간 {ctx.dates[0]}~{ctx.dates[-1]} ({len(ctx.dates)}영업일) "
          f"3분봉 {len(ctx.hynix_bars_3m)} 확정플래그 {len(ctx.flags_by_idx)}")

    extras_b: list = []
    extras_s: list = []
    trades_a = run(ctx, extra_teg_entry=False)
    trades_b = run(ctx, extra_teg_entry=True, extras_out=extras_b)
    trades_s = run(ctx, extra_teg_entry=True, extra_same_direction_rule=True,
                   extras_out=extras_s)

    # 검증: A 가 직전 검증된 엔진(slot23_tq_fullchain 의 A)과 동일한지
    prev = PROJECT_ROOT / "data" / "validation" / "slot23_tq_fullchain" / "raw.json"
    if prev.exists():
        old_a = json.loads(prev.read_text(encoding="utf-8"))["A"]
        def k(t):
            return (t["date"], t["entry_time"], t["direction"],
                    round(t["net_pct"], 6), t["exit_reason"])
        same = {k(t) for t in old_a} == {k(vars(t)) for t in trades_a}
        print(f"A 재현 검증 (직전 검증엔진과 동일): {same}  "
              f"[old {len(old_a)}건 / new {len(trades_a)}건]")

    n_extra_entered = sum(1 for t in trades_b if t.is_extra)
    print(f"A {len(trades_a)}건 / B {len(trades_b)}건 (그중 추가 {n_extra_entered}건) "
          f"/ B_STRICT {len(trades_s)}건")
    print(f"오후 4번째 후보 도달 {len(extras_b)}건, TEGv2 통과 "
          f"{sum(1 for e in extras_b if e.teg_approved)}건, 실제 진입 {n_extra_entered}건")
    same_bs = ([vars(t) for t in trades_b] == [vars(t) for t in trades_s])
    print(f"B == B_STRICT: {same_bs}")

    out = {
        "dates": ctx.dates,
        "A": [vars(t) for t in trades_a],
        "B": [vars(t) for t in trades_b],
        "B_STRICT": [vars(t) for t in trades_s],
        "extra_candidates_B": [vars(e) for e in extras_b],
        "extra_candidates_STRICT": [vars(e) for e in extras_s],
        "B_equals_STRICT": same_bs,
    }
    (OUTPUT_DIR / "raw.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"saved {OUTPUT_DIR / 'raw.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
