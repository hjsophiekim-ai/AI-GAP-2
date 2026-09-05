"""READ-ONLY 연구 2/2 단계2 (2026-09-04): AFTERNOON RUNNER FILTER A/B/C 를
TRAIN 40일에서만 설계·동결하고, OOS 20일에서 재튜닝 없이 검증한 뒤,
"기존 TW2 3-SLOT + 조기익절 + 추가 1회 진입" 전략으로 full-chain 비교.

■ TRAIN 에서 고른 설명 가능한 상위 피처 (feature_rank.txt 기준, 상위 5개)
   1. day_extreme_margin_pct  구분력 0.616 (AUC 0.192)  낮을수록 BIG WIN
   2. price_vs_vwap_pct       구분력 0.568 (AUC 0.216)  낮을수록 BIG WIN
   3. ema20_slope_pct         구분력 0.525 (AUC 0.238)  낮을수록 BIG WIN
   4. teg_passed              구분력 0.422 (AUC 0.289)  낮을수록 BIG WIN
   5. tq (Trend Quality)      구분력 0.393 (AUC 0.304)  낮을수록 BIG WIN
   다섯 개가 전부 같은 방향을 가리킨다 — 오후/슬롯소진 이후의 BIG WIN 은
   "추세 계속"이 아니라 **당일 극값에서 멀리 떨어진 역추세 스냅백**이다.

■ 임계값은 전부 TRAIN 분위수에서만 뽑아 동결했다(아래 상수). OOS 는 그대로
   검증만 한다. 대조군으로 정반대 방향(강신호) 필터 S 도 함께 본다.

Production 코드는 수정하지 않는다. 피처/게이트/청산은 전부 연구 1단계
(_tmp_20260904_runner_dataset.py) 의 production 함수 재사용 경로를 그대로 쓴다.
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Callable, Optional

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
BASE = PROJECT_ROOT / "data" / "validation" / "afternoon_runner"

# ── TRAIN 에서 동결한 임계값 (OOS 에서 절대 재튜닝하지 않음) ───────────
THR_EXTREME = -4.362      # day_extreme_margin_pct TRAIN q25 근방 컷
THR_VWAP = -1.167         # price_vs_vwap_pct   BIG+SUPER q75
THR_SLOPE = 0.003         # ema20_slope_pct     BIG+SUPER q75
THR_TEG_STRONG = 7        # teg_passed 만점 (대조군 S)


def filt_A(f: dict) -> bool:
    """A 단일조건: 당일 극값에서 충분히 멀다(역추세 깊이)."""
    v = f.get("day_extreme_margin_pct")
    return v is not None and v <= THR_EXTREME


def filt_B(f: dict) -> bool:
    """B = A + VWAP 역방향 이격."""
    v = f.get("price_vs_vwap_pct")
    return filt_A(f) and v is not None and v <= THR_VWAP


def filt_C(f: dict) -> bool:
    """C = B + EMA20 기울기도 역방향."""
    s = f.get("ema20_slope_pct")
    return filt_B(f) and s is not None and s <= THR_SLOPE


def filt_S(f: dict) -> bool:
    """대조군: 정반대(강신호) — TEGv2 전 조건 통과."""
    v = f.get("teg_passed")
    return v is not None and v >= THR_TEG_STRONG


def filt_TEG(f: dict) -> bool:
    """직전 연구의 기준선: TEGv2 approved."""
    return bool(f.get("teg_approved"))


FILTERS: dict[str, Callable[[dict], bool]] = {
    "A": filt_A, "B": filt_B, "C": filt_C, "S": filt_S, "TEG": filt_TEG,
}
FILTER_DESC = {
    "A": f"day_extreme_margin_pct <= {THR_EXTREME}",
    "B": f"A AND price_vs_vwap_pct <= {THR_VWAP}",
    "C": f"B AND ema20_slope_pct <= {THR_SLOPE}",
    "S": f"[대조군] teg_passed >= {THR_TEG_STRONG} (TEGv2 만점)",
    "TEG": "[기준선] TEGv2 approved",
}


# ── 1) 후보 데이터셋 위에서 필터 성능 (TRAIN 설계 / OOS 검증) ──────────
def eval_filter(df: pd.DataFrame, name: str, fn) -> dict:
    mask = df.apply(lambda r: fn(r.to_dict()), axis=1)
    sel = df[mask]
    big_all = df["label"].isin(["BIG_WIN", "SUPER_WIN"]).sum()
    los_all = (df["label"] == "LOSER").sum()
    if len(sel) == 0:
        return {"filter": name, "n": 0, "big_capture_pct": 0.0,
                "loser_block_pct": 100.0 if los_all else None}
    wins = sel[sel["net_pct"] > 0]
    losses = sel[sel["net_pct"] < 0]
    gp = wins["net_pct"].sum()
    gl = -losses["net_pct"].sum()
    big_sel = int(sel["label"].isin(["BIG_WIN", "SUPER_WIN"]).sum())
    los_sel = int((sel["label"] == "LOSER").sum())
    return {
        "filter": name,
        "n": len(sel),
        "pass_rate_pct": round(len(sel) / len(df) * 100, 2),
        "win_rate_pct": round(len(wins) / len(sel) * 100, 2),
        "avg_pct": round(float(sel["net_pct"].mean()), 4),
        "total_pct": round(float(sel["net_pct"].sum()), 4),
        "pf": round(float(gp / gl), 4) if gl > 0 else None,
        "big_wins": big_sel,
        "big_total": int(big_all),
        "big_capture_pct": round(big_sel / big_all * 100, 1) if big_all else None,
        "losers": los_sel,
        "loser_total": int(los_all),
        "loser_block_pct": round((1 - los_sel / los_all) * 100, 1) if los_all else None,
        "max_pct": round(float(sel["net_pct"].max()), 3),
        "min_pct": round(float(sel["net_pct"].min()), 3),
    }


# ── 2) full-chain 전략: 3슬롯 소진 후 필터통과 후보에 하루 1회 추가진입 ──
@dataclass
class Trade:
    date: str
    slot_number: Optional[int]
    session: Optional[str]
    direction: str
    decision_idx: int
    is_extra: bool = False
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
    switched_from: Optional[str] = None


def run_chain(ctx, pre, *, gate=None) -> list:
    """gate=None -> 현행 A. gate(features)->bool 이면 3슬롯 소진 후 하루 1회 추가진입."""
    bars = ctx.hynix_bars_3m
    flags_by_idx = ctx.flags_by_idx
    etf_close = ctx.etf_close
    etf_1m_close = ctx.etf_1m_close
    date_set = set(ctx.dates)

    trades: list = []
    position = None
    pending = None
    slots_used_today = 0
    morning_count = afternoon_count = 0
    extra_used_today = False
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
            extra_used_today = False
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
                is_extra = False

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
                        if (gate is not None and not extra_used_today
                                and sd.reject_reason == config.TW2_3SLOT_REJECT_SLOT_CAP):
                            feats = ds.build_features(bars, pre, idx, p_direction,
                                                      bar_close_at, bars_slice, flag_bar_dt)
                            if gate(feats):
                                is_extra = True
                                final_approved = True
                                final_reason = config.TW_APPROVED
                                slot_number = 4
                                session = sd.session
                    else:
                        if sd.requires_quality_gate:
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
                            if is_extra:
                                extra_used_today = True
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
                                        decision_idx=idx, is_extra=is_extra,
                                        entry_time=bar_close_at.isoformat(),
                                        entry_symbol=target, entry_price=fill,
                                        entry_bar_idx=idx, entry_chop=entry_chop,
                                        switched_from=switched_from)
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
        last_idx = len(bars) - 1
        last_dt = pd.Timestamp(bars["datetime"].iloc[last_idx]).to_pydatetime() + timedelta(minutes=3)
        close = etf_close[position["symbol"]].get(bars["datetime"].iloc[last_idx],
                                                  position["rec"].entry_price)
        close_trade(last_dt, close, "END_OF_DATA", last_idx)
    return trades


def strategy_metrics(trades: list, dates: list) -> dict:
    ts = [vars(t) if not isinstance(t, dict) else t for t in trades]
    ts = [t for t in ts if t["date"] in set(dates)]
    if not ts:
        return {}
    df = pd.DataFrame(ts).sort_values("exit_time").reset_index(drop=True)
    n = len(df)
    wins = df[df["net_pct"] > 0]
    losses = df[df["net_pct"] < 0]
    eq = (1 + df["net_pct"] / 100).cumprod()
    gp = wins["net_pct"].sum()
    gl = -losses["net_pct"].sum()
    dd = (eq / eq.cummax() - 1) * 100
    daily = df.groupby("date")["net_pct"].sum()
    return {
        "trades": n, "extra_trades": int(df.get("is_extra", pd.Series([False]*n)).sum()),
        "avg_trades_per_day": round(n / len(dates), 3),
        "win_rate_pct": round(len(wins) / n * 100, 2),
        "simple_pct": round(float(df["net_pct"].sum()), 4),
        "compound_pct": round(float((eq.iloc[-1] - 1) * 100), 4),
        "avg_pct": round(float(df["net_pct"].mean()), 4),
        "pf": round(float(gp / gl), 4) if gl > 0 else None,
        "mdd_pct": round(float(dd.min()), 4),
        "profit_days": int((daily > 0).sum()), "loss_days": int((daily < 0).sum()),
        "trades_ge_3pct": int((df["net_pct"] >= 3.0).sum()),
        "trades_ge_5pct": int((df["net_pct"] >= 5.0).sum()),
    }


def main() -> int:
    ctx = ds.build_ctx(use_cache=True)
    pre = ds.precompute(ctx.hynix_bars_3m)
    dates = ctx.dates
    train_dates, oos_dates = dates[:ds.N_TRAIN], dates[ds.N_TRAIN:]
    df = pd.read_csv(BASE / "candidates.csv")

    L: list[str] = []
    add = L.append
    add(f"기간 {dates[0]}~{dates[-1]} (60영업일)  TRAIN {train_dates[0]}~{train_dates[-1]} (40) / "
        f"OOS {oos_dates[0]}~{oos_dates[-1]} (20)")
    add("")
    add("=" * 104)
    add("[D] 필터 정의 (임계값은 TRAIN 분위수에서만 산출, OOS 재튜닝 없음)")
    add("=" * 104)
    for k in FILTERS:
        add(f"  RUNNER FILTER {k}: {FILTER_DESC[k]}")
    add("")

    # ── 후보 데이터셋 기준 필터 성능 ──
    add("=" * 104)
    add("[E] 후보 데이터셋 기준 필터 성능 (섀도우 진입 결과)")
    add("=" * 104)
    perf = {}
    for split, d in (("TRAIN", df[df["split"] == "TRAIN"]),
                     ("OOS", df[df["split"] == "OOS"]),
                     ("ALL", df)):
        add(f"-- {split} (후보 {len(d)}건, BIG {int(d['label'].isin(['BIG_WIN','SUPER_WIN']).sum())}, "
            f"LOSER {int((d['label']=='LOSER').sum())})")
        add(f"   {'필터':<6}{'통과':>6}{'통과율':>8}{'승률':>8}{'평균%':>9}{'합계%':>9}"
            f"{'PF':>8}{'BIG포착':>9}{'LOSER차단':>10}{'최고%':>8}{'최저%':>8}")
        base = eval_filter(d, "무필터", lambda f: True)
        for name, fn in [("무필터", lambda f: True)] + list(FILTERS.items()):
            r = eval_filter(d, name, fn)
            perf[(split, name)] = r
            if r["n"] == 0:
                add(f"   {name:<6}{0:>6}  (통과 없음)")
                continue
            add(f"   {name:<6}{r['n']:>6}{r['pass_rate_pct']:>8.1f}{r['win_rate_pct']:>8.1f}"
                f"{r['avg_pct']:>+9.3f}{r['total_pct']:>+9.2f}"
                f"{(r['pf'] if r['pf'] is not None else float('nan')):>8.3f}"
                f"{(r['big_capture_pct'] if r['big_capture_pct'] is not None else 0):>8.1f}%"
                f"{(r['loser_block_pct'] if r['loser_block_pct'] is not None else 0):>9.1f}%"
                f"{r['max_pct']:>+8.2f}{r['min_pct']:>+8.2f}")
        add("")

    # ── full-chain 전략 비교 ──
    add("=" * 104)
    add("[F] full-chain 전략 비교 — 현행(baseline) vs 3슬롯 소진 후 필터통과 1회 추가")
    add("=" * 104)
    chains = {"BASE": run_chain(ctx, pre, gate=None)}
    for k, fn in FILTERS.items():
        chains[k] = run_chain(ctx, pre, gate=fn)
    strat = {}
    for scope, dd in (("60일 전체", dates), ("TRAIN 40일", train_dates), ("OOS 20일", oos_dates)):
        add(f"-- {scope}")
        add(f"   {'전략':<7}{'거래':>6}{'추가':>6}{'승률':>8}{'단순%':>9}{'복리%':>9}"
            f"{'평균%':>9}{'PF':>8}{'MDD%':>9}{'수익일':>7}{'손실일':>7}{'+3%':>6}{'+5%':>6}")
        for k, tr in chains.items():
            m = strategy_metrics(tr, dd)
            strat[(scope, k)] = m
            if not m:
                continue
            add(f"   {k:<7}{m['trades']:>6}{m['extra_trades']:>6}{m['win_rate_pct']:>8.2f}"
                f"{m['simple_pct']:>+9.3f}{m['compound_pct']:>+9.3f}{m['avg_pct']:>+9.4f}"
                f"{(m['pf'] if m['pf'] is not None else float('nan')):>8.4f}"
                f"{m['mdd_pct']:>9.3f}{m['profit_days']:>7}{m['loss_days']:>7}"
                f"{m['trades_ge_3pct']:>6}{m['trades_ge_5pct']:>6}")
        add("")

    # ── 각 필터의 추가거래 목록 ──
    add("=" * 104)
    add("[G] 각 필터가 실제로 만든 추가거래")
    add("=" * 104)
    extras_by = {}
    for k in FILTERS:
        ex = [vars(t) for t in chains[k] if t.is_extra]
        extras_by[k] = ex
        tot = sum(t["net_pct"] for t in ex)
        w = sum(1 for t in ex if t["net_pct"] > 0)
        add(f"-- 필터 {k}: {len(ex)}건, 합계 {tot:+.3f}%, 승 {w}/{len(ex)}"
            + (f", 평균 {tot/len(ex):+.3f}%" if ex else ""))
        for t in ex:
            split = "TRAIN" if t["date"] in set(train_dates) else "OOS"
            add(f"     [{split}] {t['date']} {t['entry_time'][11:16]}->"
                f"{(t['exit_time'] or '')[11:16]} {t['direction']:<10} "
                f"{t['net_pct']:+.3f}% (MFE {t['peak_net_pct']:+.2f}%, {t['exit_reason']})")
        add("")

    # ── 놓친 러너 ──
    add("=" * 104)
    add("[H] 놓친 +3% / +5% 러너 (후보 데이터셋 기준)")
    add("=" * 104)
    runners = df[df["label"].isin(["BIG_WIN", "SUPER_WIN"])].sort_values("net_pct", ascending=False)
    add(f"전체 후보 379건 중 +3% 이상 {len(runners)}건 / +5% 이상 "
        f"{int((df['net_pct']>=5).sum())}건")
    add(f"{'날짜':<10}{'판정':<7}{'split':<7}{'방향':<11}{'손익%':>8}{'MFE%':>8}"
        f"{'TEG':>5}{'TQ':>4}{'극값이격':>9}{'VWAP':>8}{'slope':>8}  통과필터")
    add("-" * 104)
    for _, r in runners.iterrows():
        f = r.to_dict()
        passed = [k for k, fn in FILTERS.items() if fn(f)]
        add(f"{r['date']:<10}{r['decision_at'][11:16]:<7}{r['split']:<7}{r['direction']:<11}"
            f"{r['net_pct']:>+8.3f}{r['peak_net_pct']:>+8.2f}"
            f"{int(r['teg_passed']):>5}{int(r['tq']):>4}"
            f"{r['day_extreme_margin_pct']:>+9.2f}{r['price_vs_vwap_pct']:>+8.2f}"
            f"{r['ema20_slope_pct']:>+8.3f}  {','.join(passed) or '(없음)'}")
    add("")
    for k, fn in FILTERS.items():
        missed = [r for _, r in runners.iterrows() if not fn(r.to_dict())]
        add(f"-- 필터 {k} 가 놓친 +3% 러너: {len(missed)}/{len(runners)}건  "
            + (", ".join(f"{r['date']} {r['decision_at'][11:16]} {r['net_pct']:+.2f}%"
                         for r in missed) or "(없음)"))
    add("")

    (BASE / "filters_report.txt").write_text("\n".join(L), encoding="utf-8")
    (BASE / "filters_report.json").write_text(json.dumps({
        "thresholds": {"THR_EXTREME": THR_EXTREME, "THR_VWAP": THR_VWAP,
                       "THR_SLOPE": THR_SLOPE, "THR_TEG_STRONG": THR_TEG_STRONG},
        "filter_desc": FILTER_DESC,
        "candidate_perf": {f"{s}|{n}": v for (s, n), v in perf.items()},
        "strategy": {f"{s}|{k}": v for (s, k), v in strat.items()},
        "extras": extras_by,
    }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"saved {BASE / 'filters_report.txt'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
