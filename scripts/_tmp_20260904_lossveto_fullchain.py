"""READ-ONLY full-chain (2026-09-04): Loss-Veto A = `Slot1 AND entry_chop` 를
60영업일 시간순 재생으로 검증. 정적 제거가 아니라 세 전략을 각각 처음부터 재생한다.

  A. 현행           — TW2 3-SLOT + 조기익절, PRE15 OFF, 하루 최대 3회
  B. Loss-Veto + 슬롯 소비
       Slot1 후보가 entry_chop=True 면 진입 차단. 단 Slot1 은 사용한 것으로
       처리(slots_used_today += 1, 해당 세션 카운트 += 1) → 이후 후보는
       Slot2 → Slot3 순서로 진행.
  C. Loss-Veto + 슬롯 미소비
       동일 조건으로 차단하되 슬롯을 소비하지 않음 → 다음 플래그도 다시 Slot1
       후보로 평가되고, 실제 체결된 뒤에만 슬롯 카운트가 증가한다.

■ 새 임계값/새 점수식 없음
  entry_chop 은 production ``early_take_profit.evaluate_entry_chop`` 의 반환값을
  그대로 쓴다(현행 조기익절 필터가 이미 매 진입마다 계산해 청산에 쓰는 바로 그 값).
  나머지 MACD/T+3/TW2/TEGv2/whipsaw/조기익절/TP1/TP2/trailing/SL 은 전부 그대로.

■ 차단 시점의 포지션
  slot_number == 1 은 곧 slots_used_today == 0 이고, 당일 진입이 아직 없으므로
  그 시점 포지션은 항상 flat 이다(전일 포지션은 15:00 강제청산으로 정리됨).
  따라서 이 veto 는 보유 포지션의 청산에 영향을 주지 않는다 — 순수 진입 차단.

■ B 의 카운터 처리
  slots_used_today 와 세션 카운트(morning/afternoon)는 증가시킨다.
  ``last_afternoon_direction`` 은 증가시키지 않는다 — 그 값은 production 에서
  "이미 완전히 청산된 오후 포지션의 방향"을 뜻하는데 차단된 후보에는 포지션
  자체가 없기 때문(resolve_slot 의 2번째 오후후보 동일방향 금지 규칙 대상 아님).

체인 골격은 이미 검증된 _tmp_20260904_snapback_fullchain.run_chain 과 동일하며
(A 재현이 30일 70거래로 직전 검증엔진과 일치함을 매번 재확인), 새로 추가된 것은
위 veto 분기뿐이다. production 코드 무수정.
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
import _tmp_20260904_runner_dataset as ds  # noqa: E402

KST = config.KST
OUT = PROJECT_ROOT / "data" / "validation" / "lossveto_fullchain"
OUT.mkdir(parents=True, exist_ok=True)
N_TRAIN = 40

VETO_OFF, VETO_CONSUME, VETO_PRESERVE = "OFF", "CONSUME", "PRESERVE"
REJECT_VETO = "SLOT1_ENTRY_CHOP_VETO"


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
    lock_fired: bool = False
    hold_bars: int = 0


@dataclass
class Blocked:
    date: str
    direction: str
    decision_idx: int
    decision_at: str
    session: Optional[str]
    flag_ordinal: int
    chop_score: Optional[int]


def run_chain(ctx, *, veto: str, blocked_out: Optional[list] = None) -> list:
    bars = ctx.hynix_bars_3m
    flags_by_idx = ctx.flags_by_idx
    etf_close = ctx.etf_close
    etf_1m_close = ctx.etf_1m_close
    date_set = set(ctx.dates)

    trades: list = []
    blocked: list = []
    position = None
    pending = None
    slots_used_today = morning_count = afternoon_count = 0
    flag_ordinal_today = 0
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
            flag_ordinal_today = 0
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
                flag_ordinal_today += 1
                flag_ord = flag_ordinal_today
                flag_bar_dt = pd.Timestamp(p_bar_ts).to_pydatetime()
                bars_slice = bars.iloc[: idx + 1]
                slot_number = session = None

                base_decision = twf.evaluate_time_window_entry(
                    bars_slice, p_direction, flag_bar_dt, bar_close_at,
                    position_direction=position_direction(),
                    morning_entry_count=0, afternoon_entry_count=0, daily_entry_count=0)
                tw2_cleared = bool(base_decision.approved)
                base_reason = base_decision.block_reason
                if tw2_cleared:
                    vetoed_x, vr = twf.evaluate_tw2_extra_vetoes(
                        bars_slice, p_direction, flag_bar_dt, bar_close_at)
                    if vetoed_x:
                        tw2_cleared, base_reason = False, vr

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

                # ── Loss-Veto: 승인된 Slot1 후보가 entry_chop 이면 차단 ──
                if (veto != VETO_OFF and final_approved and slot_number == 1):
                    cd = etp.evaluate_entry_chop(bars_slice, p_direction, bar_close_at)
                    if (not cd.insufficient_data) and bool(cd.is_chop):
                        final_approved = False
                        final_reason = REJECT_VETO
                        blocked.append(Blocked(
                            date=current_day, direction=p_direction.value,
                            decision_idx=idx, decision_at=bar_close_at.isoformat(),
                            session=session, flag_ordinal=flag_ord,
                            chop_score=int(cd.score)))
                        if veto == VETO_CONSUME:
                            # Slot1 을 사용한 것으로 처리 -> 이후 Slot2 -> Slot3
                            slots_used_today += 1
                            if session == tw3.SESSION_MORNING:
                                morning_count += 1
                            else:
                                afternoon_count += 1

                target = order_executor.target_symbol_for_direction(p_direction)
                if not final_approved:
                    if position is not None and position["symbol"] != target:
                        if final_reason in config.TW_WHIPSAW_REJECT_REASONS:
                            whipsaw_watch = {"direction": p_direction,
                                             "last_gap": float("-inf"),
                                             "last_ema_spread": float("-inf")}
                        else:
                            cn = etf_close[position["symbol"]].get(
                                bar_ts, position["rec"].entry_price)
                            close_trade(bar_close_at, cn, config.EXIT_OPPOSITE_SIGNAL, idx)
                            position = None
                else:
                    fill = etf_close[target].get(bar_ts)
                    if fill is not None:
                        if position is not None and position["symbol"] != target:
                            cn = etf_close[position["symbol"]].get(
                                bar_ts, position["rec"].entry_price)
                            close_trade(bar_close_at, cn, config.EXIT_OPPOSITE_SIGNAL, idx)
                            position = None
                        if position is None:
                            slots_used_today += 1
                            if session == tw3.SESSION_MORNING:
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
                                        decision_idx=idx, flag_ordinal=flag_ord,
                                        entry_time=bar_close_at.isoformat(),
                                        entry_symbol=target, entry_price=fill,
                                        entry_bar_idx=idx, entry_chop=entry_chop)
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
        li = len(bars) - 1
        ldt = pd.Timestamp(bars["datetime"].iloc[li]).to_pydatetime() + timedelta(minutes=3)
        close = etf_close[position["symbol"]].get(bars["datetime"].iloc[li],
                                                  position["rec"].entry_price)
        close_trade(ldt, close, "END_OF_DATA", li)

    if blocked_out is not None:
        blocked_out.extend(blocked)
    return trades


def metrics(trades, dates) -> dict:
    ts = [t for t in trades if t["date"] in set(dates)]
    if not ts:
        return {}
    df = pd.DataFrame(ts).sort_values("exit_time").reset_index(drop=True)
    n = len(df)
    w, l = df[df.net_pct > 0], df[df.net_pct < 0]
    eq = (1 + df.net_pct / 100).cumprod()
    gp, gl = w.net_pct.sum(), -l.net_pct.sum()
    dd = (eq / eq.cummax() - 1) * 100
    daily = df.groupby("date")["net_pct"].sum()
    top10 = df.nlargest(min(10, n), "net_pct")
    rest = df.drop(top10.index)
    t10c = (((1 + rest.sort_values("exit_time").net_pct / 100).cumprod().iloc[-1] - 1) * 100
            if len(rest) else 0.0)
    return {
        "trades": n, "avg_per_day": round(n / len(dates), 3),
        "win_rate_pct": round(len(w) / n * 100, 2),
        "simple_pct": round(float(df.net_pct.sum()), 4),
        "compound_pct": round(float((eq.iloc[-1] - 1) * 100), 4),
        "avg_pct": round(float(df.net_pct.mean()), 4),
        "pf": round(float(gp / gl), 4) if gl > 0 else None,
        "mdd_pct": round(float(dd.min()), 4),
        "top10_excl_simple_pct": round(float(rest.net_pct.sum()), 4),
        "top10_excl_compound_pct": round(float(t10c), 4),
        "big_wins": int((df.net_pct >= 3.0).sum()),
        "big_losses": int((df.net_pct <= -1.0).sum()),
        "profit_days": int((daily > 0).sum()), "loss_days": int((daily < 0).sum()),
    }


def main() -> int:
    ctx = ds.build_ctx(use_cache=True)
    d60 = ctx.dates
    train, oos = d60[:N_TRAIN], d60[N_TRAIN:]
    print(f"ctx {d60[0]}~{d60[-1]} (60일)  TRAIN {train[0]}~{train[-1]} / "
          f"OOS {oos[0]}~{oos[-1]}")

    blk_b: list = []
    blk_c: list = []
    tA = run_chain(ctx, veto=VETO_OFF)
    tB = run_chain(ctx, veto=VETO_CONSUME, blocked_out=blk_b)
    tC = run_chain(ctx, veto=VETO_PRESERVE, blocked_out=blk_c)
    A, B, C = ([vars(t) for t in x] for x in (tA, tB, tC))

    prev = PROJECT_ROOT / "data" / "validation" / "slot23_tq_fullchain" / "raw.json"
    if prev.exists():
        old = json.loads(prev.read_text(encoding="utf-8"))["A"]
        k = lambda t: (t["date"], t["entry_time"], t["direction"],
                       round(t["net_pct"], 6), t["exit_reason"])
        d30 = set(d60[-30:])
        print(f"A 재현 검증(30일창): "
              f"{ {k(t) for t in old} == {k(t) for t in A if t['date'] in d30} }")
    print(f"A {len(A)} / B {len(B)} (차단 {len(blk_b)}) / C {len(C)} (차단 {len(blk_c)})")

    L: list[str] = []
    add = L.append
    add(f"기간 60영업일 {d60[0]}~{d60[-1]}  |  TRAIN 40일 {train[0]}~{train[-1]}  |  "
        f"OOS 20일 {oos[0]}~{oos[-1]}")
    add("A 현행 / B Loss-Veto+슬롯소비 / C Loss-Veto+슬롯미소비")
    add("veto 조건: 승인된 Slot1 후보의 entry_chop=True (production evaluate_entry_chop)")
    add("")

    res = {}
    add("=" * 104)
    add("[1] 성과 지표")
    add("=" * 104)
    for scope, dd in (("TRAIN 40일", train), ("OOS 20일", oos), ("전체 60일", d60)):
        add(f"-- {scope}")
        add(f"   {'안':<4}{'거래':>6}{'일평균':>8}{'승률%':>8}{'단순%':>10}{'복리%':>10}"
            f"{'평균%':>9}{'PF':>9}{'MDD%':>9}{'T10제외단순%':>13}{'T10제외복리%':>13}"
            f"{'BW':>4}{'BL':>4}")
        for nm, tr in (("A", A), ("B", B), ("C", C)):
            m = metrics(tr, dd)
            res[f"{scope}|{nm}"] = m
            add(f"   {nm:<4}{m['trades']:>6}{m['avg_per_day']:>8.3f}"
                f"{m['win_rate_pct']:>8.2f}{m['simple_pct']:>+10.3f}"
                f"{m['compound_pct']:>+10.3f}{m['avg_pct']:>+9.4f}{m['pf']:>9.4f}"
                f"{m['mdd_pct']:>9.3f}{m['top10_excl_simple_pct']:>+13.3f}"
                f"{m['top10_excl_compound_pct']:>+13.3f}"
                f"{m['big_wins']:>4}{m['big_losses']:>4}")
        add("")

    # ── 차단된 Slot1 CHOP 거래 ──
    akey = {(t["decision_idx"], t["direction"]): t for t in A}
    add("=" * 104)
    add("[2] 차단된 Slot1 CHOP 후보 전부 + A 기준 원래 손익")
    add("=" * 104)
    for nm, blk in (("B", blk_b), ("C", blk_c)):
        add(f"-- {nm}: {len(blk)}건")
        add(f"   {'split':<6}{'날짜':<10}{'판정':<7}{'방향':<11}{'세션':<10}"
            f"{'#':>3}{'CHOP':>5}{'A손익%':>9}{'A MFE%':>9}  A청산")
        tot = 0.0
        for b in blk:
            bd = vars(b)
            at = akey.get((bd["decision_idx"], bd["direction"]))
            sp = "TRAIN" if bd["date"] in set(train) else "OOS"
            if at:
                tot += at["net_pct"]
            add(f"   {sp:<6}{bd['date']:<10}{bd['decision_at'][11:16]:<7}"
                f"{bd['direction']:<11}{str(bd['session']):<10}{bd['flag_ordinal']:>3}"
                f"{bd['chop_score']:>5}"
                + (f"{at['net_pct']:>9.3f}{at['peak_net_pct']:>9.2f}  {at['exit_reason']}"
                   if at else f"{'-':>9}{'-':>9}  (A에도 없음)"))
        add(f"   A기준 차단분 합계 {tot:+.3f}%")
        for sp, dd in (("TRAIN", train), ("OOS", oos)):
            s = [vars(b) for b in blk if b.date in set(dd)]
            st = sum(akey[(x["decision_idx"], x["direction"])]["net_pct"]
                     for x in s if (x["decision_idx"], x["direction"]) in akey)
            add(f"     {sp}: {len(s)}건, A기준 합계 {st:+.3f}%")
        add("")

    # ── C 에서 새로 생긴 거래 ──
    add("=" * 104)
    add("[3] 차단 후 새로 생긴 후속 거래 (A 에 없던 진입)")
    add("=" * 104)
    news = {}
    for nm, T in (("B", B), ("C", C)):
        new = [t for t in T if (t["decision_idx"], t["direction"]) not in akey]
        news[nm] = new
        add(f"-- {nm}: {len(new)}건" + (f", 합계 {sum(t['net_pct'] for t in new):+.3f}%"
                                        if new else ""))
        for t in new:
            sp = "TRAIN" if t["date"] in set(train) else "OOS"
            add(f"   [{sp}] {t['date']} {t['entry_time'][11:16]}->"
                f"{(t['exit_time'] or '')[11:16]} Slot{t['slot_number']} "
                f"#{t['flag_ordinal']} {t['direction']:<10} {t['net_pct']:+.3f}% "
                f"(MFE {t['peak_net_pct']:+.2f}%, {t['exit_reason']})")
        if new:
            for sp, dd in (("TRAIN", train), ("OOS", oos)):
                s = [t for t in new if t["date"] in set(dd)]
                if s:
                    add(f"     {sp}: {len(s)}건 합계 {sum(t['net_pct'] for t in s):+.3f}%")
            from collections import Counter
            add("     슬롯 분포: " + str(dict(sorted(Counter(t["slot_number"]
                                                          for t in new).items()))))
        add("")

    # ── BIG_WIN 보존 / BIG_LOSS 차단 ──
    add("=" * 104)
    add("[4] BIG_WIN(+3%) 보존 / BIG_LOSS(-1%) 차단")
    add("=" * 104)
    add(f"   {'안':<4}{'구간':<12}{'A의BW':>7}{'놓친BW':>8}{'보존율%':>9}"
        f"{'A의BL':>7}{'막은BL':>8}{'차단율%':>9}")
    cons = {}
    for nm, T in (("B", B), ("C", C)):
        tkey = {(t["decision_idx"], t["direction"]) for t in T}
        for scope, dd in (("TRAIN 40일", train), ("OOS 20일", oos), ("전체 60일", d60)):
            abw = [t for t in A if t["date"] in set(dd) and t["net_pct"] >= 3.0]
            abl = [t for t in A if t["date"] in set(dd) and t["net_pct"] <= -1.0]
            miss = [t for t in abw if (t["decision_idx"], t["direction"]) not in tkey]
            blkd = [t for t in abl if (t["decision_idx"], t["direction"]) not in tkey]
            keep = (len(abw) - len(miss)) / len(abw) * 100 if abw else 100.0
            cons[f"{nm}|{scope}"] = {"A_bw": len(abw), "missed": len(miss),
                                     "keep_pct": round(keep, 1),
                                     "A_bl": len(abl), "blocked_bl": len(blkd)}
            add(f"   {nm:<4}{scope:<12}{len(abw):>7}{len(miss):>8}{keep:>9.1f}"
                f"{len(abl):>7}{len(blkd):>8}"
                f"{(len(blkd)/len(abl)*100 if abl else 0):>9.1f}")
            for t in miss:
                add(f"        놓친 BIG_WIN: {t['date']} {t['entry_time'][11:16]} "
                    f"{t['direction']} {t['net_pct']:+.3f}%")
        add("")

    # ── 최근 10영업일 타임라인 ──
    add("=" * 104)
    add("[5] 최근 10영업일 날짜별 A/B/C 타임라인")
    add("=" * 104)
    for d in d60[-10:]:
        add(f"### {d}")
        for nm, T in (("A", A), ("B", B), ("C", C)):
            ts = [t for t in T if t["date"] == d]
            add(f"  {nm}: {len(ts)}건 합계 {sum(t['net_pct'] for t in ts):+.3f}%")
            for t in ts:
                add(f"      {t['entry_time'][11:16]}->{(t['exit_time'] or '')[11:16]} "
                    f"S{t['slot_number']} #{t['flag_ordinal']} {t['direction'][:4]} "
                    f"{t['net_pct']:+.2f}% ({t['exit_reason']})")
            blk = [vars(b) for b in (blk_b if nm == "B" else blk_c if nm == "C" else [])
                   if b.date == d]
            for b in blk:
                add(f"      [차단] {b['decision_at'][11:16]} #{b['flag_ordinal']} "
                    f"{b['direction'][:4]} CHOP={b['chop_score']}")
        da = sum(t["net_pct"] for t in A if t["date"] == d)
        db = sum(t["net_pct"] for t in B if t["date"] == d)
        dc = sum(t["net_pct"] for t in C if t["date"] == d)
        add(f"  => B-A {db - da:+.3f}%  /  C-A {dc - da:+.3f}%")
        add("")

    # ── 최종 판정 ──
    add("=" * 104)
    add("[6] OOS 채택 판정 (복리↑ + PF↑ + MDD 비악화 + BIG_WIN 보존율 100%)")
    add("=" * 104)
    verdict = {}
    ao = res["OOS 20일|A"]
    for nm in ("B", "C"):
        m = res[f"OOS 20일|{nm}"]
        c = cons[f"{nm}|OOS 20일"]
        c1 = m["compound_pct"] > ao["compound_pct"]
        c2 = m["pf"] > ao["pf"]
        c3 = m["mdd_pct"] >= ao["mdd_pct"] - 1e-9
        c4 = c["keep_pct"] >= 100.0
        ok = c1 and c2 and c3 and c4
        verdict[nm] = ok
        add(f"-- {nm}")
        add(f"   복리   {ao['compound_pct']:+.3f}% -> {m['compound_pct']:+.3f}%  "
            f"({'PASS' if c1 else 'FAIL'})")
        add(f"   PF     {ao['pf']:.4f} -> {m['pf']:.4f}  ({'PASS' if c2 else 'FAIL'})")
        add(f"   MDD    {ao['mdd_pct']:.3f}% -> {m['mdd_pct']:.3f}%  "
            f"({'PASS' if c3 else 'FAIL'})")
        add(f"   BIG_WIN 보존율 {c['keep_pct']:.1f}%  ({'PASS' if c4 else 'FAIL'})")
        add(f"   => {'채택 후보' if ok else '탈락'}")
        add("")
    add("최종: " + (", ".join(nm for nm, v in verdict.items() if v) or "채택 가능한 안 없음"))

    (OUT / "report.txt").write_text("\n".join(L), encoding="utf-8")
    (OUT / "raw.json").write_text(json.dumps({
        "dates60": d60, "train": train, "oos": oos,
        "A": A, "B": B, "C": C,
        "blocked_B": [vars(b) for b in blk_b], "blocked_C": [vars(b) for b in blk_c],
        "new_entries": news, "metrics": res, "bw_bl": cons, "verdict": verdict,
    }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"saved {OUT / 'report.txt'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
