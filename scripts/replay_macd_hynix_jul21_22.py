"""Read-only Jul21+Jul22 replay for isolated MACD Hynix Strategy B.

Fill model (recommendation basis):
  - completed 3m bars only
  - next 1m open after signal bar close
  - 0.05% adverse slippage
  - TradeCostEngine round-trip costs
  - no broker orders

Stress: 1m and 2m delay variants.
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
    DIR_HOLD,
    DIR_UP,
    INVERSE_SYMBOL,
    LONG_SYMBOL,
    SIGNAL_SYMBOL,
    evaluate_macd_direction,
    resample_completed_3m,
)
from app.trading.trading_cost_engine import TradeCostEngine  # noqa: E402

CACHE = ROOT / "data" / "cache"
STATE = ROOT / "data" / "state"
INITIAL_CASH = 10_000_000.0
ENTRY_CUTOFF_HM = (14, 55)
FORCE_HM = (15, 0)


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


def _price_at(df: pd.DataFrame, ts: datetime, field: str = "open") -> Optional[float]:
    sub = df[df["datetime"] >= ts]
    if sub.empty:
        sub = df[df["datetime"] <= ts]
        if sub.empty:
            return None
        return float(sub.iloc[-1][field])
    return float(sub.iloc[0][field])


def _fill_price(df: pd.DataFrame, signal_close_ts: datetime, side: str, delay_min: int, adverse_pct: float) -> tuple[Optional[datetime], Optional[float]]:
    target = signal_close_ts + timedelta(minutes=max(0, delay_min - 1)) if delay_min > 0 else signal_close_ts
    # next 1m open at or after signal_close + (delay_min) concept:
    # delay_min=1 → first bar with datetime >= signal_close_ts (next minute open)
    if delay_min <= 0:
        ts = signal_close_ts
        px = _price_at(df, ts, "close")
    else:
        ts_gate = signal_close_ts + timedelta(minutes=delay_min - 1)
        # find first 1m bar starting at/after signal_close for delay=1
        gate = signal_close_ts if delay_min == 1 else signal_close_ts + timedelta(minutes=delay_min - 1)
        sub = df[df["datetime"] >= gate]
        if sub.empty:
            return None, None
        row = sub.iloc[0]
        ts = pd.Timestamp(row["datetime"]).to_pydatetime()
        px = float(row["open"])
    if px is None:
        return None, None
    # adverse: buy higher, sell lower
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
) -> DayResult:
    data = _load_day(day)
    hynix = data[SIGNAL_SYMBOL]
    long_df = data[LONG_SYMBOL]
    inv_df = data[INVERSE_SYMBOL]
    cost_engine = TradeCostEngine()
    result = DayResult(day=day, delay_label=delay_label)

    last_dir = None
    last_bar = None
    position = None  # dict
    realized = 0.0

    # Iterate on each completed 3m close time
    all_3m = resample_completed_3m(hynix, now=hynix["datetime"].iloc[-1] + timedelta(minutes=3))
    for i in range(len(all_3m)):
        bar_ts = pd.Timestamp(all_3m.iloc[i]["datetime"]).to_pydatetime()
        close_ts = bar_ts + timedelta(minutes=3)
        # Use only bars known at close_ts
        hist_1m = hynix[hynix["datetime"] < close_ts]
        ev = evaluate_macd_direction(
            hist_1m,
            now=close_ts,
            last_signal_direction=last_dir,
            last_signal_bar_ts=last_bar,
        )
        if not ev.get("ok"):
            continue

        hm = (close_ts.hour, close_ts.minute)
        equity = INITIAL_CASH + realized

        def _close_position(exit_reason: str) -> None:
            nonlocal position, realized
            if position is None:
                return
            etf_exit = long_df if position["symbol"] == LONG_SYMBOL else inv_df
            xts, xpx = _fill_price(etf_exit, close_ts, "SELL", delay_min, adverse_pct)
            if xpx is None:
                xpx = float(position["entry_price"])
                xts = close_ts
            qty = position["qty"]
            breakdown = cost_engine.compute_net_pnl(
                position["symbol"], position["entry_price"], xpx, qty,
                buy_order_type="market", sell_order_type="market",
            )
            result.trades.append(Trade(
                day=day, direction=position["direction"], symbol=position["symbol"],
                signal_time=position["signal_time"], entry_time=position["entry_time"],
                entry_price=position["entry_price"], exit_time=str(xts), exit_price=xpx,
                qty=qty, gross_pnl=breakdown["gross_pnl"], cost=breakdown["total_cost"],
                net_pnl=breakdown["net_pnl"], exit_reason=exit_reason, delay_label=delay_label,
            ))
            realized += breakdown["net_pnl"]
            position = None

        # Force liquidate
        if hm >= FORCE_HM and position is not None:
            _close_position("15:00_FORCE")
            continue

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

        if hm >= ENTRY_CUTOFF_HM:
            continue

        target = LONG_SYMBOL if direction == DIR_UP else INVERSE_SYMBOL
        etf_df = long_df if target == LONG_SYMBOL else inv_df

        if position is not None and position["symbol"] != target:
            _close_position(f"SWITCH_TO_{direction}")
            equity = INITIAL_CASH + realized

        if position is not None and position["symbol"] == target:
            continue  # same direction no add

        ets, epx = _fill_price(etf_df, close_ts, "BUY", delay_min, adverse_pct)
        if epx is None or epx <= 0:
            continue
        qty = int(equity // epx)
        if qty < 1:
            continue
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

    # End-of-day flatten if still held
    if position is not None:
        close_ts = datetime.strptime(f"{day} 15:00:00", "%Y-%m-%d %H:%M:%S")
        etf = long_df if position["symbol"] == LONG_SYMBOL else inv_df
        xts, xpx = _fill_price(etf, close_ts, "SELL", delay_min, adverse_pct)
        if xpx is None:
            xpx = float(position["entry_price"])
            xts = close_ts
        qty = position["qty"]
        breakdown = cost_engine.compute_net_pnl(
            position["symbol"], position["entry_price"], xpx, qty,
            buy_order_type="market", sell_order_type="market",
        )
        result.trades.append(Trade(
            day=day, direction=position["direction"], symbol=position["symbol"],
            signal_time=position["signal_time"], entry_time=position["entry_time"],
            entry_price=position["entry_price"], exit_time=str(xts), exit_price=xpx,
            qty=qty, gross_pnl=breakdown["gross_pnl"], cost=breakdown["total_cost"],
            net_pnl=breakdown["net_pnl"], exit_reason="EOD_FLAT", delay_label=delay_label,
        ))
        realized += breakdown["net_pnl"]
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
    }


def main() -> None:
    scenarios = [
        ("delay_1m_cons", 1, 0.05),
        ("delay_1m", 1, 0.0),
        ("delay_2m_stress", 2, 0.10),
    ]
    days = ["2026-07-21", "2026-07-22"]
    report: dict[str, Any] = {"generated_at": datetime.now().isoformat(), "scenarios": {}}
    print("=" * 72)
    print("MACD Hynix Strategy B replay (read-only)")
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
