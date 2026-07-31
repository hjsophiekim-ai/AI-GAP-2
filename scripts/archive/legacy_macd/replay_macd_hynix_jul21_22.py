"""Jul21+Jul22 replay for isolated MACD Hynix Strategy B (signed hist 2-turn).

Comparison economics (match old A–F B `delay_1m_cons` for MATCH):
  - completed 3m bars only; shared `evaluate_macd_direction`
  - fill: first 1m open STRICTLY after signal minute (+ adverse)
  - flat RT cost 0.05% of entry notional (not TradeCostEngine)
  - force exit 15:15 / entry cutoff 14:50

Live worker still flattens at 15:00 (product rule) — documented separately.
Never places broker orders.
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
    DIR_DOWN,
    DIR_UP,
    INVERSE_SYMBOL,
    LONG_SYMBOL,
    SIGNAL_SYMBOL,
    evaluate_macd_direction,
    resample_completed_3m,
)

CACHE = ROOT / "data" / "cache"
STATE = ROOT / "data" / "state"
INITIAL_CASH = 10_000_000.0
# Match old A–F B compare clocks (live uses 15:00 flatten).
ENTRY_CUTOFF_HM = (14, 50)
FORCE_HM = (15, 15)
RT_COST_PCT = 0.05  # flat % of entry notional, applied on close (old A–F B)


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
    delay_label: str


@dataclass
class DayResult:
    day: str
    delay_label: str
    trades: list[Trade] = field(default_factory=list)
    signals: list[dict] = field(default_factory=list)
    net_pnl: float = 0.0
    ret_pct: float = 0.0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    mdd_pct: float = 0.0
    round_trips: int = 0
    long_trades: int = 0
    inverse_trades: int = 0
    signal_to_order_delays_sec: list[float] = field(default_factory=list)


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


def _rt_cost(entry_price: float, qty: int) -> float:
    return float(entry_price) * int(qty) * (RT_COST_PCT / 100.0)


def _fill_price(
    df: pd.DataFrame,
    signal_close_ts: datetime,
    side: str,
    delay_min: int,
    adverse_pct: float,
) -> tuple[Optional[datetime], Optional[float]]:
    """Match old A–F `resolve_fill`: next 1m open STRICTLY after signal minute."""
    sig_min = signal_close_ts.replace(second=0, microsecond=0)
    if delay_min <= 0:
        row = df[df["datetime"] == sig_min]
        if row.empty:
            prev = df[df["datetime"] <= sig_min]
            if prev.empty:
                return None, None
            px = float(prev.iloc[-1]["close"])
            ts = pd.Timestamp(prev.iloc[-1]["datetime"]).to_pydatetime()
        else:
            px = float(row.iloc[0]["close"])
            ts = sig_min
    else:
        if delay_min <= 1:
            target = sig_min + timedelta(minutes=1)
        else:
            target = sig_min + timedelta(minutes=delay_min)
        sub = df[df["datetime"] >= target]
        if sub.empty:
            return None, None
        row = sub.iloc[0]
        ts = pd.Timestamp(row["datetime"]).to_pydatetime()
        px = float(row["open"])
        if ts <= sig_min:
            sub2 = df[df["datetime"] >= sig_min + timedelta(minutes=1)]
            if sub2.empty:
                return None, None
            row = sub2.iloc[0]
            ts = pd.Timestamp(row["datetime"]).to_pydatetime()
            px = float(row["open"])
    if px is None:
        return None, None
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


def replay_day(
    day: str,
    *,
    delay_min: int = 1,
    adverse_pct: float = 0.05,
    delay_label: str = "delay_1m_cons",
    entry_cutoff_hm: tuple[int, int] = ENTRY_CUTOFF_HM,
    force_hm: tuple[int, int] = FORCE_HM,
) -> DayResult:
    data = _load_day(day)
    hynix = data[SIGNAL_SYMBOL]
    long_df = data[LONG_SYMBOL]
    inv_df = data[INVERSE_SYMBOL]
    result = DayResult(day=day, delay_label=delay_label)

    last_dir = None
    last_bar = None
    position = None  # dict
    # Match old A–F cash: costs hit reported net only, not cash balance.
    cash = INITIAL_CASH

    all_3m = resample_completed_3m(hynix, now=hynix["datetime"].iloc[-1] + timedelta(minutes=3))
    for i in range(len(all_3m)):
        bar_ts = pd.Timestamp(all_3m.iloc[i]["datetime"]).to_pydatetime()
        close_ts = bar_ts + timedelta(minutes=3)
        hist_1m = hynix[hynix["datetime"] < close_ts]
        ev = evaluate_macd_direction(
            hist_1m,
            now=close_ts,
            last_signal_direction=last_dir,
            last_signal_bar_ts=last_bar,
        )
        if not ev.get("ok"):
            continue

        def _close_position(exit_reason: str) -> None:
            nonlocal position, cash
            if position is None:
                return
            etf_exit = long_df if position["symbol"] == LONG_SYMBOL else inv_df
            xts, xpx = _fill_price(etf_exit, close_ts, "SELL", delay_min, adverse_pct)
            if xpx is None:
                xpx = float(position["entry_price"])
                xts = close_ts
            qty = position["qty"]
            gross = (xpx - position["entry_price"]) * qty
            cost = _rt_cost(position["entry_price"], qty)
            net = gross - cost
            cash += qty * xpx  # old A–F: cost not deducted from cash
            result.trades.append(Trade(
                day=day, direction=position["direction"], symbol=position["symbol"],
                signal_time=position["signal_time"], entry_time=position["entry_time"],
                entry_price=position["entry_price"], exit_time=str(xts), exit_price=xpx,
                qty=qty, gross_pnl=gross, cost=cost,
                net_pnl=net, exit_reason=exit_reason, delay_label=delay_label,
            ))
            # Keep direction_state (last_dir) after flatten — no same-dir re-entry.
            position = None

        if not ev.get("new_signal"):
            continue

        direction = ev["signal_direction"]
        last_dir = direction
        last_bar = ev.get("bar_ts")
        result.signals.append({
            "time": close_ts.isoformat(),
            "direction": direction,
            "signal_id": ev.get("signal_id"),
            "hist_last3": ev.get("hist_last3"),
        })

        force_dt = close_ts.replace(
            hour=force_hm[0], minute=force_hm[1], second=0, microsecond=0
        )
        if close_ts >= force_dt:
            continue

        target = LONG_SYMBOL if direction == DIR_UP else INVERSE_SYMBOL
        etf_df = long_df if target == LONG_SYMBOL else inv_df

        # Opposite switch allowed after entry cutoff (old A–F); only new entries blocked.
        if position is not None and position["symbol"] != target:
            _close_position(f"SWITCH_TO_{direction}")

        cutoff_dt = close_ts.replace(
            hour=entry_cutoff_hm[0], minute=entry_cutoff_hm[1], second=0, microsecond=0
        )
        if close_ts > cutoff_dt:
            continue

        if position is not None and position["symbol"] == target:
            continue  # same direction no add

        ets, epx = _fill_price(etf_df, close_ts, "BUY", delay_min, adverse_pct)
        if epx is None or epx <= 0:
            continue
        qty = int(cash // epx)
        if qty < 1:
            continue
        cash -= qty * epx
        delay_sec = max(0.0, (ets - close_ts).total_seconds()) if ets else float(delay_min * 60)
        result.signal_to_order_delays_sec.append(delay_sec)
        position = {
            "symbol": target,
            "direction": direction,
            "qty": qty,
            "entry_price": epx,
            "entry_time": str(ets),
            "signal_time": close_ts.isoformat(),
        }

    # End-of-day flatten if still held (compare force clock 15:15)
    if position is not None:
        close_ts = datetime.strptime(
            f"{day} {force_hm[0]:02d}:{force_hm[1]:02d}:00", "%Y-%m-%d %H:%M:%S"
        )
        etf = long_df if position["symbol"] == LONG_SYMBOL else inv_df
        xts, xpx = _fill_price(etf, close_ts, "SELL", delay_min, adverse_pct)
        if xpx is None:
            xpx = float(position["entry_price"])
            xts = close_ts
        qty = position["qty"]
        gross = (xpx - position["entry_price"]) * qty
        cost = _rt_cost(position["entry_price"], qty)
        net = gross - cost
        cash += qty * xpx
        result.trades.append(Trade(
            day=day, direction=position["direction"], symbol=position["symbol"],
            signal_time=position["signal_time"], entry_time=position["entry_time"],
            entry_price=position["entry_price"], exit_time=str(xts), exit_price=xpx,
            qty=qty, gross_pnl=gross, cost=cost,
            net_pnl=net, exit_reason=f"{force_hm[0]:02d}:{force_hm[1]:02d}_FORCE",
            delay_label=delay_label,
        ))
        position = None

    m = _metrics(result.trades)
    result.net_pnl = m["net"]
    result.ret_pct = m["ret"]
    result.win_rate = m["wr"]
    result.profit_factor = m["pf"]
    result.mdd_pct = m["mdd"]
    result.round_trips = len(result.trades)
    result.long_trades = sum(1 for t in result.trades if t.symbol == LONG_SYMBOL)
    result.inverse_trades = sum(1 for t in result.trades if t.symbol == INVERSE_SYMBOL)
    return result


def _summary(dr: DayResult) -> dict[str, Any]:
    delays = dr.signal_to_order_delays_sec
    by_dir = {"UP_RED": 0.0, "DOWN_BLUE": 0.0}
    for t in dr.trades:
        by_dir[t.direction] = by_dir.get(t.direction, 0.0) + t.net_pnl
    return {
        "day": dr.day,
        "delay_label": dr.delay_label,
        "signals": len(dr.signals),
        "round_trips": dr.round_trips,
        "long_trades": dr.long_trades,
        "inverse_trades": dr.inverse_trades,
        "win_rate_pct": dr.win_rate,
        "profit_factor": dr.profit_factor,
        "net_pnl": dr.net_pnl,
        "ret_pct": dr.ret_pct,
        "mdd_pct": dr.mdd_pct,
        "avg_signal_to_fill_sec": round(sum(delays) / len(delays), 1) if delays else None,
        "pnl_by_direction": by_dir,
        "signal_list": dr.signals,
        "trades": [asdict(t) for t in dr.trades],
        "economics": {
            "fill": "strict_next_1m_open_after_signal_minute",
            "rt_cost_pct": RT_COST_PCT,
            "force_exit": f"{FORCE_HM[0]:02d}:{FORCE_HM[1]:02d}",
            "entry_cutoff": f"{ENTRY_CUTOFF_HM[0]:02d}:{ENTRY_CUTOFF_HM[1]:02d}",
            "live_force_note": "Live worker flattens at 15:00 (product rule); this replay uses 15:15 to match old A–F B.",
        },
    }


def main() -> None:
    scenarios = [
        ("delay_1m_cons", 1, 0.05),
        ("delay_1m", 1, 0.0),
        ("delay_2m_stress", 2, 0.10),
    ]
    days = ["2026-07-21", "2026-07-22"]
    report: dict[str, Any] = {
        "generated_at": datetime.now().isoformat(),
        "strategy": "B: signed hist 2-turn (shared)",
        "scenarios": {},
    }
    print("=" * 72)
    print("MACD Hynix Strategy B replay (signed 2-turn, old A–F economics)")
    print("=" * 72)
    for label, delay, adv in scenarios:
        report["scenarios"][label] = {}
        print(f"\n## {label} (delay={delay}m, adverse={adv}%)")
        for day in days:
            try:
                dr = replay_day(day, delay_min=delay, adverse_pct=adv, delay_label=label)
            except FileNotFoundError as exc:
                print(f"  {day}: MISSING CACHE {exc}")
                continue
            s = _summary(dr)
            report["scenarios"][label][day] = s
            print(
                f"  {day}: signals={s['signals']} RT={s['round_trips']} "
                f"L={s['long_trades']} I={s['inverse_trades']} "
                f"WR={s['win_rate_pct']}% PF={s['profit_factor']} "
                f"Net={s['net_pnl']:,.0f} Ret={s['ret_pct']}% MDD={s['mdd_pct']}%"
            )
            for t in dr.trades:
                print(
                    f"    {t.signal_time[11:16]} {t.direction} {t.symbol} "
                    f"entry={t.entry_price:.0f} exit={t.exit_price:.0f} "
                    f"net={t.net_pnl:,.0f} ({t.exit_reason})"
                )

    out = STATE / "macd_hynix_jul21_22_replay.json"
    STATE.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nWrote {out}")


if __name__ == "__main__":
    main()
