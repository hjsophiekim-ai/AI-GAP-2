"""READ-ONLY head-to-head (2026-09-04 사용자 요청): 현행 **조기익절 필터** vs
**CHOP 적응형 +1.5% 즉시 전량익절**. 하루 최대 3회 유지, 진입 로직 동일.

두 안은 같은 아이디어("난타전이면 빨리 익절")의 서로 다른 구현인데 지금까지
직접 맞대결한 적이 없다 — chop_adaptive_exit 연구의 베이스라인은 조기익절
도입(43e7623) **이전** 이었기 때문이다. 여기서 같은 창·같은 하니스로 붙인다.

  N  = 공통 베이스라인: TP1/TP2/trailing/SL 만 (두 필터 모두 OFF)
  A  = 현행 — 조기익절 필터 ON
       production early_take_profit: 진입시점 entry_chop 고정,
       peak >= 1.5% 도달 시 arm, 완성봉 종가가 <= 0.8% 로 내려오면 전량청산
  B  = CHOP 적응형 — 봉마다 CHOP 재판정(한 번 CHOP이면 그 포지션 동안 latch),
       latch 상태에서 순수익 >= 1.5% 도달 즉시 전량익절(틱 단위도 인정)
       CHOP 판정식/파라미터는 chop_adaptive_exit 연구가 TRAIN 20일에서
       고른 것을 그대로 사용: cross>=1 | flip>=3 | score>=3

■ 두 안의 구조적 차이 (이번 비교의 본질)
  판정시점 : A = 진입 시점 1회 고정   /  B = 매 완성봉 재판정 + latch
  발동조건 : A = 고점 1.5% 찍고 0.8%로 되돌아오면(되돌림 확인)
             B = 1.5% 닿는 즉시(되돌림 안 기다림)

■ 두 가지 체인 모드
  frozen  : A 체인의 승인/거절 결과를 그대로 재생 → 진입집합 완전동일.
            청산 변경이 진입을 바꾸는 오염 없이 "순수 청산 효과"만 비교.
  free    : full-chain 독립 재생 → 실제 배포 시 효과(청산이 빨라져 포지션이
            일찍 비면 없던 진입이 생기는 부작용까지 포함).

Production 코드 무수정. 판단은 전부 production 함수:
  evaluate_time_window_entry / evaluate_tw2_extra_vetoes / resolve_slot /
  evaluate_trend_quality / evaluate_teg / evaluate_whipsaw_watch /
  evaluate_take_profit_immediate / evaluate_position /
  early_take_profit.evaluate_entry_chop / .evaluate / _net_return_pct
CHOP 판정 재료도 production 지표 함수로 만든 것을 재사용
(_tmp_20260903_chop_adaptive_exit_train_oos.build_chop_features / chop_conditions).
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
OUT = PROJECT_ROOT / "data" / "validation" / "earlytp_vs_choptp"
OUT.mkdir(parents=True, exist_ok=True)

CHOP_CFG = ce.ChopConfig(cross_min=1, flip_min=3, score_min=3)
CHOP_TP_PCT = 1.5
EXIT_CHOP_TP = ce.EXIT_CHOP_FULL_TP

MODE_NONE, MODE_EARLY, MODE_CHOP = "NONE", "EARLY_TP", "CHOP_TP"


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
    net_pct: Optional[float] = None
    peak_net_pct: float = 0.0
    entry_chop: bool = False
    chop_latched: bool = False
    lock_fired: bool = False
    hold_bars: int = 0


def run(ctx, feat, *, mode: str, frozen: Optional[dict] = None,
        record_decisions: bool = False) -> tuple[list, dict]:
    bars = ctx.hynix_bars_3m
    flags_by_idx = ctx.flags_by_idx
    etf_close = ctx.etf_close
    etf_1m_close = ctx.etf_1m_close
    date_set = set(ctx.dates)

    trades: list = []
    decisions: dict = {}
    position = None
    pending = None
    slots_used_today = morning_count = afternoon_count = 0
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

    def maybe_latch_chop(idx: int) -> None:
        """완성봉 idx 정보로 CHOP 판정. 한 번 CHOP이면 그 포지션 동안 유지."""
        if position is None or position["chop_latched"]:
            return
        d = bt._direction_for_symbol(position["symbol"])
        if d is None:
            return
        c = ce.chop_conditions(feat, idx, d, CHOP_CFG)
        if c is not None and c["is_chop"]:
            position["chop_latched"] = True
            position["rec"].chop_latched = True

    for idx in range(len(bars)):
        bar_ts = bars["datetime"].iloc[idx]
        bar_start = pd.Timestamp(bar_ts).to_pydatetime()
        day_key = bar_start.strftime("%Y%m%d")
        if day_key not in date_set:
            continue
        if day_key != current_day:
            current_day = day_key
            slots_used_today = morning_count = afternoon_count = 0
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
                dec_key = f"{idx}|{p_direction.value}"

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
                    slot_number = session = None
                    base_decision = twf.evaluate_time_window_entry(
                        bars_slice, p_direction, flag_bar_dt, bar_close_at,
                        position_direction=position_direction(),
                        morning_entry_count=0, afternoon_entry_count=0, daily_entry_count=0)
                    tw2_cleared = bool(base_decision.approved)
                    base_reason = base_decision.block_reason
                    if tw2_cleared:
                        vetoed, vr = twf.evaluate_tw2_extra_vetoes(
                            bars_slice, p_direction, flag_bar_dt, bar_close_at)
                        if vetoed:
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
                    if record_decisions:
                        decisions[dec_key] = {
                            "approved": bool(final_approved), "reason": str(final_reason),
                            "slot_number": slot_number, "session": session}

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
                                        decision_idx=idx, entry_time=bar_close_at.isoformat(),
                                        entry_symbol=target, entry_price=fill,
                                        entry_bar_idx=idx, entry_chop=entry_chop)
                            position = {"symbol": target, "entry_idx": idx,
                                        "entry_time": bar_close_at, "tp1_done": False,
                                        "peak": 0.0, "session": session, "rec": rec,
                                        "qty_frac": 1.0, "realized": 0.0,
                                        "chop_latched": False}
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

        # ── 틱(1분 종가) ──
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
                # B: latch 상태면 1.5% 닿는 즉시 전량익절 (TP 래더보다 우선)
                if mode == MODE_CHOP and position["chop_latched"] and net >= CHOP_TP_PCT:
                    position["rec"].lock_fired = True
                    close_trade(tick, price, EXIT_CHOP_TP, idx)
                    position = None
                    whipsaw_watch = None
                    break
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

        # ── 완성봉: (B) CHOP 재판정 → 청산 래더 → (A) 조기익절 ──
        if position is not None and mode == MODE_CHOP:
            maybe_latch_chop(idx)
        if position is not None and idx > position["entry_idx"]:
            close = etf_close[position["symbol"]].get(bar_ts)
            if close is not None:
                net = net_at(close)
                position["rec"].peak_net_pct = max(position["rec"].peak_net_pct, net)
                if mode == MODE_CHOP and position["chop_latched"] and net >= CHOP_TP_PCT:
                    position["rec"].lock_fired = True
                    close_trade(bar_close_at, close, EXIT_CHOP_TP, idx)
                    position = None
                    whipsaw_watch = None
                    continue
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
                    if mode == MODE_EARLY and position["rec"].entry_chop:
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
    return trades, decisions


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
        "trades": n, "win_rate_pct": round(len(w) / n * 100, 2),
        "simple_pct": round(float(df.net_pct.sum()), 4),
        "compound_pct": round(float((eq.iloc[-1] - 1) * 100), 4),
        "avg_pct": round(float(df.net_pct.mean()), 4),
        "pf": round(float(gp / gl), 4) if gl > 0 else None,
        "mdd_pct": round(float(dd.min()), 4),
        "top10_excl_compound_pct": round(float(t10c), 4),
        "profit_days": int((daily > 0).sum()), "loss_days": int((daily < 0).sum()),
        "trades_ge_3pct": int((df.net_pct >= 3.0).sum()),
        "fired": int(df.lock_fired.sum()) if "lock_fired" in df else 0,
    }


def main() -> int:
    ctx = ds.build_ctx(use_cache=True)
    feat = ce.build_chop_features(ctx.hynix_bars_3m)
    d60 = ctx.dates
    d30 = d60[-30:]
    print(f"ctx {d60[0]}~{d60[-1]} (60일) / 30일창 {d30[0]}~{d30[-1]}")
    print(f"CHOP cfg: {CHOP_CFG.key()},  CHOP TP={CHOP_TP_PCT}%")

    # free chain
    free = {}
    free[MODE_EARLY], dec_a = run(ctx, feat, mode=MODE_EARLY, record_decisions=True)
    free[MODE_CHOP], _ = run(ctx, feat, mode=MODE_CHOP)
    free[MODE_NONE], _ = run(ctx, feat, mode=MODE_NONE)
    # frozen (A 진입집합 고정)
    froz = {}
    for m in (MODE_EARLY, MODE_CHOP, MODE_NONE):
        froz[m], _ = run(ctx, feat, mode=m, frozen=dec_a)

    A = [vars(t) for t in free[MODE_EARLY]]
    prev = PROJECT_ROOT / "data" / "validation" / "slot23_tq_fullchain" / "raw.json"
    if prev.exists():
        old = json.loads(prev.read_text(encoding="utf-8"))["A"]
        k = lambda t: (t["date"], t["entry_time"], t["direction"],
                       round(t["net_pct"], 6), t["exit_reason"])
        print(f"현행(A) 재현 검증(30일창): "
              f"{ {k(t) for t in old} == {k(t) for t in A if t['date'] in set(d30)} }")

    L: list[str] = []
    add = L.append
    add(f"기간 60일 {d60[0]}~{d60[-1]} / 30일창 {d30[0]}~{d30[-1]}")
    add(f"CHOP 판정: {CHOP_CFG.key()} (chop_adaptive_exit TRAIN 20일 선정값 그대로)")
    add(f"CHOP 익절 임계값: 순수익 >= {CHOP_TP_PCT}% 즉시 전량")
    add("")
    NAME = {MODE_NONE: "N 필터없음", MODE_EARLY: "A 현행 조기익절", MODE_CHOP: "B CHOP+1.5%"}
    res = {}
    for chain_label, chain in (("frozen(진입 동결 — 순수 청산효과)", froz),
                               ("free(full-chain — 실배포 효과)", free)):
        add("=" * 96)
        add(f"[{chain_label}]")
        add("=" * 96)
        for scope, dd in (("30일", d30), ("60일", d60),
                          ("TRAIN 1-40일", d60[:40]), ("OOS 41-60일", d60[40:])):
            add(f"-- {scope}")
            add(f"   {'안':<18}{'거래':>6}{'발동':>6}{'승률':>8}{'단순%':>10}{'복리%':>10}"
                f"{'평균%':>9}{'PF':>9}{'MDD%':>9}{'T10제외복리%':>13}{'+3%':>6}")
            for m in (MODE_NONE, MODE_EARLY, MODE_CHOP):
                mm = metrics([vars(t) for t in chain[m]], dd)
                res[f"{chain_label}|{scope}|{m}"] = mm
                if not mm:
                    continue
                add(f"   {NAME[m]:<18}{mm['trades']:>6}{mm['fired']:>6}"
                    f"{mm['win_rate_pct']:>8.2f}{mm['simple_pct']:>+10.3f}"
                    f"{mm['compound_pct']:>+10.3f}{mm['avg_pct']:>+9.4f}"
                    f"{mm['pf']:>9.4f}{mm['mdd_pct']:>9.3f}"
                    f"{mm['top10_excl_compound_pct']:>+13.3f}{mm['trades_ge_3pct']:>6}")
            add("")

    # B 발동 거래 목록 (frozen 기준, A와 1:1 비교)
    add("=" * 96)
    add("[B(CHOP+1.5%) 발동 거래 — frozen 기준 A와 1:1 대조]")
    add("=" * 96)
    a_map = {(t.decision_idx, t.direction): t for t in froz[MODE_EARLY]}
    n_map = {(t.decision_idx, t.direction): t for t in froz[MODE_NONE]}
    diffs = []
    for t in froz[MODE_CHOP]:
        if not t.lock_fired:
            continue
        key = (t.decision_idx, t.direction)
        at, nt = a_map.get(key), n_map.get(key)
        scope = "30일창" if t.date in set(d30) else "60일전용"
        diffs.append({"date": t.date, "scope": scope, "entry": t.entry_time,
                      "B_net": t.net_pct, "B_exit": t.exit_reason,
                      "A_net": at.net_pct if at else None,
                      "A_exit": at.exit_reason if at else None,
                      "N_net": nt.net_pct if nt else None,
                      "N_exit": nt.exit_reason if nt else None})
        add(f"   [{scope}] {t.date} {t.entry_time[11:16]} {t.direction:<10} "
            f"B {t.net_pct:+.3f}%({t.exit_reason})  "
            f"A {at.net_pct:+.3f}%({at.exit_reason})  "
            f"N {nt.net_pct:+.3f}%({nt.exit_reason})" if at and nt else "")
    add("")
    for scope, dd in (("30일", d30), ("60일", d60)):
        sub = [d for d in diffs if d["date"] in set(dd) and d["A_net"] is not None]
        if sub:
            add(f"   {scope}: B발동 {len(sub)}건, B합계 {sum(d['B_net'] for d in sub):+.3f}% "
                f"vs 같은거래 A합계 {sum(d['A_net'] for d in sub):+.3f}% "
                f"-> 차이 {sum(d['B_net'] - d['A_net'] for d in sub):+.3f}%")
    add("")
    # A 발동 거래
    add("[A(조기익절) 발동 거래 수 — frozen]")
    for scope, dd in (("30일", d30), ("60일", d60)):
        fa = [t for t in froz[MODE_EARLY] if t.lock_fired and t.date in set(dd)]
        fb = [t for t in froz[MODE_CHOP] if t.lock_fired and t.date in set(dd)]
        add(f"   {scope}: A 발동 {len(fa)}건 / B 발동 {len(fb)}건")
    add("")

    (OUT / "report.txt").write_text("\n".join(L), encoding="utf-8")
    (OUT / "result.json").write_text(json.dumps({
        "dates30": d30, "dates60": d60, "chop_cfg": CHOP_CFG.key(),
        "chop_tp_pct": CHOP_TP_PCT, "metrics": res, "B_fired_vs_A": diffs,
        "frozen": {m: [vars(t) for t in froz[m]] for m in froz},
        "free": {m: [vars(t) for t in free[m]] for m in free},
    }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"saved {OUT / 'report.txt'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
