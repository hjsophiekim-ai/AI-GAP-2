"""Jul21+Jul22 A vs B replay: TP/SL with/without continuation re-entry.

Conservative fills: next 1m open after signal, 0.05% adverse, all TradeCostEngine costs.

A: TP +3% / SL -1.5% net, no continuation re-entry
B: same + one CONTINUATION_REENTRY when gates pass

Does not place broker orders. Writes JSON under data/state/.
"""
from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402

from app.trading.macd_hynix_strategy import (  # noqa: E402
    CHASE_MAX_PCT,
    DIR_DOWN,
    DIR_HOLD,
    DIR_UP,
    ENTRY_CONTINUATION,
    ENTRY_INITIAL,
    EXIT_OPPOSITE,
    EXIT_SL,
    EXIT_TP,
    INVERSE_SYMBOL,
    LONG_SYMBOL,
    SIGNAL_SYMBOL,
    SL_NET_PCT,
    TP_NET_PCT,
    check_tp_sl,
    evaluate_continuation_reentry,
    evaluate_macd_direction,
    make_direction_episode_id,
    net_pnl_pct_vs_entry,
    resample_completed_3m,
    snapshot_tp_context,
)
from app.trading.trading_cost_engine import TradeCostEngine  # noqa: E402

CACHE = ROOT / "data" / "cache"
STATE = ROOT / "data" / "state"
INITIAL_CASH = 10_000_000.0
ENTRY_CUTOFF_HM = (14, 55)
FORCE_HM = (15, 0)
ADVERSE_PCT = 0.05
DELAY_MIN = 1


@dataclass
class Trade:
    day: str
    direction: str
    symbol: str
    signal_time: str
    entry_time: str
    entry_price: float
    exit_time: str
    exit_price: float
    qty: int
    gross_pnl: float
    cost: float
    net_pnl: float
    exit_reason: str
    entry_kind: str
    episode_id: str
    variant: str
    chase_flag: bool = False


@dataclass
class VariantResult:
    variant: str
    days: list[str] = field(default_factory=list)
    trades: list[Trade] = field(default_factory=list)
    bad_reentries: list[dict] = field(default_factory=list)


def _load_day(day: str) -> dict[str, pd.DataFrame]:
    tag = day.replace("-", "")
    files = {
        SIGNAL_SYMBOL: CACHE / f"replay_{tag}_hynix_1m.csv",
        LONG_SYMBOL: CACHE / f"replay_{tag}_long_1m.csv",
        INVERSE_SYMBOL: CACHE / f"replay_{tag}_inverse_1m.csv",
    }
    out = {}
    for sym, path in files.items():
        if not path.exists():
            raise FileNotFoundError(path)
        df = pd.read_csv(path)
        df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
        df = df.dropna(subset=["datetime"]).sort_values("datetime").reset_index(drop=True)
        out[sym] = df
    return out


def _fill_price(
    df: pd.DataFrame,
    signal_close_ts: datetime,
    side: str,
    delay_min: int = DELAY_MIN,
    adverse_pct: float = ADVERSE_PCT,
) -> tuple[Optional[datetime], Optional[float]]:
    if delay_min <= 0:
        ts = signal_close_ts
        sub = df[df["datetime"] <= ts]
        if sub.empty:
            return None, None
        px = float(sub.iloc[-1]["close"])
    else:
        gate = signal_close_ts if delay_min == 1 else signal_close_ts + timedelta(minutes=delay_min - 1)
        sub = df[df["datetime"] >= gate]
        if sub.empty:
            return None, None
        row = sub.iloc[0]
        ts = pd.Timestamp(row["datetime"]).to_pydatetime()
        px = float(row["open"])
    if side == "BUY":
        px = px * (1.0 + adverse_pct / 100.0)
    else:
        px = px * (1.0 - adverse_pct / 100.0)
    return ts, float(px)


def _metrics(trades: list[Trade], cash0: float = INITIAL_CASH) -> dict[str, float]:
    if not trades:
        return {"net": 0.0, "ret": 0.0, "wr": 0.0, "pf": 0.0, "mdd": 0.0}
    nets = [t.net_pnl for t in trades]
    wins = [n for n in nets if n > 0]
    losses = [n for n in nets if n < 0]
    equity = cash0
    peak = cash0
    mdd = 0.0
    for n in nets:
        equity += n
        peak = max(peak, equity)
        dd = (peak - equity) / peak * 100.0 if peak else 0.0
        mdd = max(mdd, dd)
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    pf = (gross_win / gross_loss) if gross_loss > 0 else (999.0 if gross_win > 0 else 0.0)
    return {
        "net": round(sum(nets), 2),
        "ret": round(sum(nets) / cash0 * 100.0, 3),
        "wr": round(len(wins) / len(nets) * 100.0, 2),
        "pf": round(pf, 3),
        "mdd": round(mdd, 3),
    }


def _etf_price_at(df: pd.DataFrame, ts: datetime) -> Optional[float]:
    sub = df[df["datetime"] <= ts]
    if sub.empty:
        return None
    return float(sub.iloc[-1]["close"])


def replay_variant(
    days: list[str],
    *,
    allow_continuation: bool,
    variant: str,
) -> VariantResult:
    cost_engine = TradeCostEngine()
    out = VariantResult(variant=variant, days=list(days))

    for day in days:
        data = _load_day(day)
        hynix = data[SIGNAL_SYMBOL]
        long_df = data[LONG_SYMBOL]
        inv_df = data[INVERSE_SYMBOL]

        last_dir = None
        last_bar = None
        position = None
        episode: dict[str, Any] = {}
        realized = 0.0

        # Minute loop for TP/SL checks; signals evaluated on completed 3m closes
        minutes = list(hynix["datetime"].tolist())
        # Also ensure we hit 15:00
        day_force = datetime.strptime(f"{day} 15:00:00", "%Y-%m-%d %H:%M:%S")

        completed_closes: set[str] = set()

        for raw_ts in minutes:
            ts = pd.Timestamp(raw_ts).to_pydatetime()
            hm = (ts.hour, ts.minute)

            # Force liquidate
            if hm >= FORCE_HM and position is not None:
                etf = long_df if position["symbol"] == LONG_SYMBOL else inv_df
                xts, xpx = _fill_price(etf, day_force, "SELL")
                if xpx is None:
                    xpx = float(position["entry_price"])
                    xts = day_force
                bd = cost_engine.compute_net_pnl(
                    position["symbol"], position["entry_price"], xpx, position["qty"],
                    buy_order_type="market", sell_order_type="market",
                )
                out.trades.append(Trade(
                    day=day, direction=position["direction"], symbol=position["symbol"],
                    signal_time=position["signal_time"], entry_time=position["entry_time"],
                    entry_price=position["entry_price"], exit_time=str(xts), exit_price=xpx,
                    qty=position["qty"], gross_pnl=bd["gross_pnl"], cost=bd["total_cost"],
                    net_pnl=bd["net_pnl"], exit_reason="15:00_FORCE",
                    entry_kind=position["entry_kind"], episode_id=position["episode_id"],
                    variant=variant, chase_flag=position.get("chase_flag", False),
                ))
                realized += bd["net_pnl"]
                position = None
                episode = {}
                continue

            # Intrabar TP/SL using ETF close at this minute
            if position is not None:
                etf = long_df if position["symbol"] == LONG_SYMBOL else inv_df
                cur = _etf_price_at(etf, ts)
                if cur is not None:
                    hit = check_tp_sl(
                        position["symbol"], position["entry_price"], cur, position["qty"],
                    )
                    if hit:
                        xts, xpx = _fill_price(etf, ts, "SELL")
                        if xpx is None:
                            xpx = cur
                            xts = ts
                        bd = cost_engine.compute_net_pnl(
                            position["symbol"], position["entry_price"], xpx, position["qty"],
                            buy_order_type="market", sell_order_type="market",
                        )
                        out.trades.append(Trade(
                            day=day, direction=position["direction"], symbol=position["symbol"],
                            signal_time=position["signal_time"], entry_time=position["entry_time"],
                            entry_price=position["entry_price"], exit_time=str(xts), exit_price=xpx,
                            qty=position["qty"], gross_pnl=bd["gross_pnl"], cost=bd["total_cost"],
                            net_pnl=bd["net_pnl"], exit_reason=hit,
                            entry_kind=position["entry_kind"], episode_id=position["episode_id"],
                            variant=variant, chase_flag=position.get("chase_flag", False),
                        ))
                        realized += bd["net_pnl"]
                        if hit == EXIT_TP:
                            ctx = snapshot_tp_context(
                                hynix[hynix["datetime"] < ts + timedelta(minutes=1)],
                                now=ts,
                            )
                            episode = {
                                **episode,
                                "tp_at": ts.isoformat(),
                                "tp_bar_ts": ctx.get("tp_bar_ts"),
                                "tp_hist_max_abs": ctx.get("tp_hist_max_abs") or 0.0,
                                "tp_hynix_price": ctx.get("tp_hynix_price"),
                                "tp_pivot_price": (
                                    ctx.get("tp_pivot_low")
                                    if episode.get("direction") == DIR_DOWN
                                    else ctx.get("tp_pivot_high")
                                ) or ctx.get("tp_hynix_price"),
                                "last_exit_reason": EXIT_TP,
                            }
                        elif hit == EXIT_SL:
                            episode = {
                                **episode,
                                "sl_lock": True,
                                "tp_at": None,
                                "last_exit_reason": EXIT_SL,
                            }
                        position = None
                        # fall through for re-entry / signals on same minute

            # Evaluate MACD only when a 3m bar has just completed
            # bar window [t, t+3) completes at t+3
            close_candidate = ts
            # Use bars known strictly before this minute's close-check: treat minute
            # timestamps that align to 3m boundaries as close times.
            if ts.minute % 3 != 0:
                # Still allow continuation eval when flat after TP
                if (
                    allow_continuation
                    and position is None
                    and episode.get("tp_at")
                    and not episode.get("sl_lock")
                    and not episode.get("continuation_reentry_used")
                    and hm < ENTRY_CUTOFF_HM
                ):
                    hist_1m = hynix[hynix["datetime"] <= ts]
                    cont = evaluate_continuation_reentry(
                        hist_1m,
                        direction=str(episode.get("direction") or ""),
                        episode=episode,
                        now=ts,
                        enabled=True,
                    )
                    if cont.get("eligible") and cont.get("signal_id"):
                        direction = str(episode["direction"])
                        target = LONG_SYMBOL if direction == DIR_UP else INVERSE_SYMBOL
                        etf_df = long_df if target == LONG_SYMBOL else inv_df
                        equity = INITIAL_CASH + realized
                        ets, epx = _fill_price(etf_df, ts, "BUY")
                        if epx and epx > 0:
                            qty = int(equity // epx)
                            if qty >= 1:
                                chase = False
                                # Mark chasey if entry is near chase limit
                                pivot = float(episode.get("tp_pivot_price") or 0) or epx
                                hx = float(
                                    (hynix[hynix["datetime"] <= ts].iloc[-1]["close"])
                                    if not hynix[hynix["datetime"] <= ts].empty
                                    else epx
                                )
                                if direction == DIR_UP and pivot > 0:
                                    chase = hx > pivot * (1.0 + (CHASE_MAX_PCT * 0.8) / 100.0)
                                elif direction == DIR_DOWN and pivot > 0:
                                    chase = hx < pivot * (1.0 - (CHASE_MAX_PCT * 0.8) / 100.0)
                                position = {
                                    "symbol": target,
                                    "direction": direction,
                                    "qty": qty,
                                    "entry_price": epx,
                                    "entry_time": str(ets),
                                    "signal_time": ts.isoformat(),
                                    "entry_kind": ENTRY_CONTINUATION,
                                    "episode_id": episode.get("id") or "",
                                    "chase_flag": chase,
                                }
                                episode["continuation_reentry_used"] = True
                                episode["tp_at"] = None
                                if chase or True:
                                    # Track all re-entries; flag chasey ones
                                    pass
                                if chase:
                                    out.bad_reentries.append({
                                        "day": day,
                                        "time": ts.isoformat(),
                                        "direction": direction,
                                        "reason": "NEAR_CHASE_LIMIT",
                                        "entry_price": epx,
                                    })
                continue

            close_key = ts.isoformat()
            if close_key in completed_closes:
                continue
            # Only treat as 3m close if resample would include a bar ending here
            hist_1m = hynix[hynix["datetime"] < ts]
            bars = resample_completed_3m(hist_1m, now=ts)
            if bars.empty:
                continue
            last_bar_ts = pd.Timestamp(bars.iloc[-1]["datetime"]).to_pydatetime()
            if last_bar_ts + timedelta(minutes=3) != ts:
                # Not exactly a fresh completion aligned to ts
                # Still accept if bar close == ts
                if last_bar_ts + timedelta(minutes=3) > ts:
                    continue
            completed_closes.add(close_key)

            ev = evaluate_macd_direction(
                hist_1m,
                now=ts,
                last_signal_direction=last_dir,
                last_signal_bar_ts=last_bar,
            )
            if not ev.get("ok"):
                continue

            if episode.get("sl_lock") and ev.get("display_direction") == DIR_HOLD:
                last_dir = DIR_HOLD

            # Continuation at 3m close
            if (
                allow_continuation
                and position is None
                and episode.get("tp_at")
                and not episode.get("sl_lock")
                and not episode.get("continuation_reentry_used")
                and hm < ENTRY_CUTOFF_HM
            ):
                cont = evaluate_continuation_reentry(
                    hist_1m,
                    direction=str(episode.get("direction") or ""),
                    episode=episode,
                    now=ts,
                    enabled=True,
                )
                if cont.get("eligible") and cont.get("signal_id"):
                    direction = str(episode["direction"])
                    target = LONG_SYMBOL if direction == DIR_UP else INVERSE_SYMBOL
                    etf_df = long_df if target == LONG_SYMBOL else inv_df
                    equity = INITIAL_CASH + realized
                    ets, epx = _fill_price(etf_df, ts, "BUY")
                    if epx and epx > 0:
                        qty = int(equity // epx)
                        if qty >= 1:
                            pivot = float(episode.get("tp_pivot_price") or 0) or epx
                            hx = float(bars.iloc[-1]["close"])
                            chase = False
                            if direction == DIR_UP and pivot > 0:
                                chase = hx > pivot * (1.0 + (CHASE_MAX_PCT * 0.8) / 100.0)
                            elif direction == DIR_DOWN and pivot > 0:
                                chase = hx < pivot * (1.0 - (CHASE_MAX_PCT * 0.8) / 100.0)
                            position = {
                                "symbol": target,
                                "direction": direction,
                                "qty": qty,
                                "entry_price": epx,
                                "entry_time": str(ets),
                                "signal_time": ts.isoformat(),
                                "entry_kind": ENTRY_CONTINUATION,
                                "episode_id": episode.get("id") or "",
                                "chase_flag": chase,
                            }
                            episode["continuation_reentry_used"] = True
                            episode["tp_at"] = None
                            if chase:
                                out.bad_reentries.append({
                                    "day": day,
                                    "time": ts.isoformat(),
                                    "direction": direction,
                                    "reason": "NEAR_CHASE_LIMIT",
                                    "entry_price": epx,
                                })

            if not ev.get("new_signal"):
                continue

            direction = ev["signal_direction"]
            last_dir = direction
            last_bar = ev.get("bar_ts")

            if hm >= ENTRY_CUTOFF_HM:
                continue

            target = LONG_SYMBOL if direction == DIR_UP else INVERSE_SYMBOL
            etf_df = long_df if target == LONG_SYMBOL else inv_df

            if position is not None and position["symbol"] != target:
                exit_etf = long_df if position["symbol"] == LONG_SYMBOL else inv_df
                xts, xpx = _fill_price(exit_etf, ts, "SELL")
                if xpx is None:
                    xpx = float(position["entry_price"])
                    xts = ts
                bd = cost_engine.compute_net_pnl(
                    position["symbol"], position["entry_price"], xpx, position["qty"],
                    buy_order_type="market", sell_order_type="market",
                )
                out.trades.append(Trade(
                    day=day, direction=position["direction"], symbol=position["symbol"],
                    signal_time=position["signal_time"], entry_time=position["entry_time"],
                    entry_price=position["entry_price"], exit_time=str(xts), exit_price=xpx,
                    qty=position["qty"], gross_pnl=bd["gross_pnl"], cost=bd["total_cost"],
                    net_pnl=bd["net_pnl"], exit_reason=EXIT_OPPOSITE,
                    entry_kind=position["entry_kind"], episode_id=position["episode_id"],
                    variant=variant, chase_flag=position.get("chase_flag", False),
                ))
                realized += bd["net_pnl"]
                position = None
                episode = {}

            if position is not None and position["symbol"] == target:
                continue

            equity = INITIAL_CASH + realized
            ets, epx = _fill_price(etf_df, ts, "BUY")
            if epx is None or epx <= 0:
                continue
            qty = int(equity // epx)
            if qty < 1:
                continue
            ep_id = make_direction_episode_id(direction, ev.get("bar_ts"))
            episode = {
                "id": ep_id,
                "direction": direction,
                "started_at": ts.isoformat(),
                "initial_entry_used": True,
                "continuation_reentry_used": False,
                "sl_lock": False,
                "tp_at": None,
                "tp_bar_ts": None,
                "tp_hist_max_abs": 0.0,
                "tp_hynix_price": None,
                "tp_pivot_price": None,
                "last_exit_reason": None,
            }
            position = {
                "symbol": target,
                "direction": direction,
                "qty": qty,
                "entry_price": epx,
                "entry_time": str(ets),
                "signal_time": ts.isoformat(),
                "entry_kind": ENTRY_INITIAL,
                "episode_id": ep_id,
                "chase_flag": False,
            }

        if position is not None:
            etf = long_df if position["symbol"] == LONG_SYMBOL else inv_df
            xts, xpx = _fill_price(etf, day_force, "SELL")
            if xpx is None:
                xpx = float(position["entry_price"])
                xts = day_force
            bd = cost_engine.compute_net_pnl(
                position["symbol"], position["entry_price"], xpx, position["qty"],
                buy_order_type="market", sell_order_type="market",
            )
            out.trades.append(Trade(
                day=day, direction=position["direction"], symbol=position["symbol"],
                signal_time=position["signal_time"], entry_time=position["entry_time"],
                entry_price=position["entry_price"], exit_time=str(xts), exit_price=xpx,
                qty=position["qty"], gross_pnl=bd["gross_pnl"], cost=bd["total_cost"],
                net_pnl=bd["net_pnl"], exit_reason="EOD_FLAT",
                entry_kind=position["entry_kind"], episode_id=position["episode_id"],
                variant=variant, chase_flag=position.get("chase_flag", False),
            ))

    return out


def _summarize(vr: VariantResult) -> dict[str, Any]:
    m = _metrics(vr.trades)
    initial = [t for t in vr.trades if t.entry_kind == ENTRY_INITIAL]
    reentry = [t for t in vr.trades if t.entry_kind == ENTRY_CONTINUATION]
    re_nets = [t.net_pnl for t in reentry]
    re_wins = [n for n in re_nets if n > 0]
    by_day: dict[str, Any] = {}
    for day in vr.days:
        day_trades = [t for t in vr.trades if t.day == day]
        by_day[day] = {
            **_metrics(day_trades),
            "round_trips": len(day_trades),
            "initial_pnl": round(sum(t.net_pnl for t in day_trades if t.entry_kind == ENTRY_INITIAL), 2),
            "reentry_pnl": round(sum(t.net_pnl for t in day_trades if t.entry_kind == ENTRY_CONTINUATION), 2),
        }
    return {
        "variant": vr.variant,
        "round_trips": len(vr.trades),
        "initial_trades": len(initial),
        "reentry_trades": len(reentry),
        "initial_pnl": round(sum(t.net_pnl for t in initial), 2),
        "reentry_pnl": round(sum(t.net_pnl for t in reentry), 2),
        "reentry_win_rate_pct": round(len(re_wins) / len(re_nets) * 100.0, 2) if re_nets else None,
        "net_pnl": m["net"],
        "ret_pct": m["ret"],
        "profit_factor": m["pf"],
        "mdd_pct": m["mdd"],
        "win_rate_pct": m["wr"],
        "by_day": by_day,
        "bad_reentries": vr.bad_reentries,
        "trades": [asdict(t) for t in vr.trades],
    }


def decide_adopt(sum_a: dict[str, Any], sum_b: dict[str, Any]) -> str:
    """ADOPT B only if Net improves and MDD does not worsen meaningfully."""
    net_ok = sum_b["net_pnl"] > sum_a["net_pnl"]
    mdd_a = float(sum_a["mdd_pct"] or 0)
    mdd_b = float(sum_b["mdd_pct"] or 0)
    # Allow tiny float noise; block if MDD rises by > 0.25pp absolute
    mdd_ok = mdd_b <= mdd_a + 0.25
    bad = len(sum_b.get("bad_reentries") or [])
    # Error / chasey trades: more than 1 chasey re-entry is meaningful worsening
    errors_ok = bad <= 1
    if net_ok and mdd_ok and errors_ok:
        return "ADOPT"
    return "DO_NOT_ADOPT"


def main() -> None:
    days = ["2026-07-21", "2026-07-22"]
    print("=" * 72)
    print("MACD TP/SL + continuation re-entry A vs B (conservative fills)")
    print(f"TP={TP_NET_PCT}% SL={SL_NET_PCT}% adverse={ADVERSE_PCT}% delay={DELAY_MIN}m")
    print("=" * 72)

    a = replay_variant(days, allow_continuation=False, variant="A_TPSL_ONLY")
    b = replay_variant(days, allow_continuation=True, variant="B_TPSL_REENTRY")
    sum_a = _summarize(a)
    sum_b = _summarize(b)
    decision = decide_adopt(sum_a, sum_b)

    for label, s in (("A", sum_a), ("B", sum_b)):
        print(f"\n## {label}: {s['variant']}")
        print(
            f"  RT={s['round_trips']} init={s['initial_trades']} re={s['reentry_trades']} "
            f"Net={s['net_pnl']:,.0f} PF={s['profit_factor']} MDD={s['mdd_pct']}% "
            f"initPnL={s['initial_pnl']:,.0f} rePnL={s['reentry_pnl']:,.0f} "
            f"reWR={s['reentry_win_rate_pct']}"
        )
        for day, d in s["by_day"].items():
            print(
                f"  {day}: RT={d['round_trips']} Net={d['net']:,.0f} "
                f"MDD={d['mdd']}% init={d['initial_pnl']:,.0f} re={d['reentry_pnl']:,.0f}"
            )
        for t in a.trades if label == "A" else b.trades:
            print(
                f"    {t.day} {str(t.signal_time)[11:16]} {t.entry_kind} {t.direction} "
                f"net={t.net_pnl:,.0f} ({t.exit_reason})"
            )

    delta = {
        "net": round(sum_b["net_pnl"] - sum_a["net_pnl"], 2),
        "mdd": round(sum_b["mdd_pct"] - sum_a["mdd_pct"], 3),
        "pf": round(sum_b["profit_factor"] - sum_a["profit_factor"], 3),
    }
    print(f"\nDelta B-A: Net={delta['net']:,.0f} MDD={delta['mdd']} PF={delta['pf']}")
    print(f"Bad/chasey re-entries (B): {len(sum_b['bad_reentries'])}")
    for br in sum_b["bad_reentries"]:
        print(f"  {br}")
    print(f"\n>>> DECISION: {decision}")

    report = {
        "generated_at": datetime.now().isoformat(),
        "fill_model": {
            "delay_min": DELAY_MIN,
            "adverse_pct": ADVERSE_PCT,
            "tp_net_pct": TP_NET_PCT,
            "sl_net_pct": SL_NET_PCT,
            "chase_max_pct": CHASE_MAX_PCT,
        },
        "A": sum_a,
        "B": sum_b,
        "delta_b_minus_a": delta,
        "decision": decision,
        "note": (
            "Live CONTINUATION_REENTRY_ENABLED follows decision: "
            "ADOPT → True, DO_NOT_ADOPT → False (feature remains implemented)."
        ),
    }
    out = STATE / "macd_hynix_tpsl_reentry_compare_jul21_22.json"
    STATE.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
