"""Same-path 7/23 verification: bootstrap→signal→Worker→OM with fake KIS only.

Uses saved replay bars. Does NOT change MACD formula / exits.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd

from app.trading import macd_hynix_order_manager as om
from app.trading import macd_hynix_worker as worker
from app.trading.macd_hynix_strategy import (
    DIR_DOWN,
    DIR_HOLD,
    DIR_UP,
    INVERSE_SYMBOL,
    LONG_SYMBOL,
    collect_signed_hist_two_turn_signals,
    evaluate_macd_direction,
    macd_components,
    resample_completed_3m,
    target_symbol_for_direction,
)
from app.trading.macd_pipeline import market_data as md
from app.utils.data_paths import CACHE_DIR

DAYS = ("2026-07-21", "2026-07-22", "2026-07-23")


class FakeBroker:
    mode = "mock"

    def __init__(self, cash: float = 10_000_000):
        from app.models import Position

        self.cash = cash
        self.positions: dict = {}
        self.prices = {
            "000660": 1_900_000.0,
            LONG_SYMBOL: 15_490.0,
            INVERSE_SYMBOL: 10_310.0,
        }
        self.buys: list = []
        self.sells: list = []
        self.orders: list = []
        self.account_no = "50123456"
        self._Position = Position

    def get_current_price(self, symbol: str):
        return self.prices.get(str(symbol))

    def get_positions(self):
        return list(self.positions.values())

    def get_balance(self):
        return self.cash

    def get_buyable_cash(self):
        return self.cash

    def buy(self, symbol, name, quantity, price, order_type="limit"):
        from app.models import OrderResult

        cost = float(price) * int(quantity)
        if cost > self.cash:
            return OrderResult(
                success=False, mode=self.mode, account_type="mock",
                symbol=symbol, name=name, side="buy", quantity=quantity,
                price=price, order_type=order_type, order_id="", message="insufficient cash",
            )
        self.cash -= cost
        if symbol in self.positions:
            pos = self.positions[symbol]
            total = pos.quantity + quantity
            pos.avg_price = (pos.avg_price * pos.quantity + cost) / total
            pos.quantity = total
        else:
            self.positions[symbol] = self._Position(
                symbol=symbol, name=name, quantity=quantity,
                avg_price=float(price), current_price=float(price),
            )
        self.buys.append((symbol, quantity, price))
        self.orders.append({
            "side": "BUY", "symbol": symbol, "qty": quantity, "price": price,
            "at": datetime.now().isoformat(),
        })
        return OrderResult(
            success=True, mode=self.mode, account_type="mock",
            symbol=symbol, name=name, side="buy", quantity=quantity,
            price=price, order_type=order_type, order_id=f"B{len(self.buys)}", message="ok",
        )

    def sell(self, symbol, name, quantity, price, order_type="limit"):
        from app.models import OrderResult

        pos = self.positions.get(symbol)
        if not pos or pos.quantity < quantity:
            return OrderResult(
                success=False, mode=self.mode, account_type="mock",
                symbol=symbol, name=name, side="sell", quantity=quantity,
                price=price, order_type=order_type, order_id="", message="no qty",
            )
        pos.quantity -= quantity
        self.cash += float(price) * quantity
        if pos.quantity <= 0:
            del self.positions[symbol]
        self.sells.append((symbol, quantity, price))
        self.orders.append({
            "side": "SELL", "symbol": symbol, "qty": quantity, "price": price,
            "at": datetime.now().isoformat(),
        })
        return OrderResult(
            success=True, mode=self.mode, account_type="mock",
            symbol=symbol, name=name, side="sell", quantity=quantity,
            price=price, order_type=order_type, order_id=f"S{len(self.sells)}", message="ok",
        )


# Remove obsolete FakeKis-only broker helpers below this point — FakeBroker is sufficient.


def _load(day_tag: str) -> pd.DataFrame:
    path = CACHE_DIR / f"replay_{day_tag}_hynix_1m.csv"
    df = pd.read_csv(path)
    df["datetime"] = pd.to_datetime(df["datetime"])
    return df.sort_values("datetime").reset_index(drop=True)


def _store_quotes_in_cache(quotes, mode="mock"):
    md.store_quotes(quotes, mode=mode)


worker._store_quotes_in_cache = _store_quotes_in_cache  # type: ignore[attr-defined]


def _run_day(day: str) -> dict:
    day_tag = day.replace("-", "")
    today_df = _load(day_tag)
    prior_tag = (datetime.strptime(day, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y%m%d")
    # walk back for weekday prior replay if needed
    prior = pd.DataFrame()
    for back in range(1, 5):
        tag = (datetime.strptime(day, "%Y-%m-%d") - timedelta(days=back)).strftime("%Y%m%d")
        path = CACHE_DIR / f"replay_{tag}_hynix_1m.csv"
        if path.exists():
            prior = _load(tag)
            if not prior.empty:
                break
    df_all = (
        pd.concat([prior, today_df], ignore_index=True).drop_duplicates("datetime").sort_values("datetime").reset_index(drop=True)
        if not prior.empty else today_df
    )
    now_end = datetime.strptime(day, "%Y-%m-%d").replace(hour=15, minute=30)
    bars = resample_completed_3m(df_all, now=now_end)
    closes = pd.to_numeric(bars["close"], errors="coerce").dropna()
    comps = macd_components(closes)
    hist = comps["hist"]
    bars = bars.copy()
    bars["hist"] = hist.values
    bars["close_time"] = bars["datetime"] + timedelta(minutes=3)

    onset_events = collect_signed_hist_two_turn_signals(
        [float(x) for x in hist.tolist()],
        close_times=list(bars["close_time"]),
        direction_state=None,
    )
    onsets = []
    for ev in onset_events:
        ct = pd.Timestamp(ev["close_time"]).to_pydatetime()
        bar_open = ct - timedelta(minutes=3)
        if bar_open.strftime("%Y-%m-%d") != day:
            continue
        onsets.append({
            "bar_ts": bar_open.isoformat(sep="T"),
            "bar_close_ts": ct.isoformat(sep="T"),
            "flag": ev["direction"],
            "hist_last3": [round(ev["hist_prev2"], 6), round(ev["hist_prev"], 6), round(ev["hist_curr"], 6)],
            "signal_id": f"MACD3M:{ev['direction']}:{bar_open.isoformat(sep='T')}",
            "target": target_symbol_for_direction(ev["direction"]),
        })

    live_arms = []
    last_dir = None
    last_bar = None
    today_mask = bars["datetime"].dt.strftime("%Y-%m-%d") == day
    for _, row in bars.loc[today_mask].iterrows():
        bar_ts = pd.Timestamp(row["datetime"]).to_pydatetime()
        close_ts = bar_ts + timedelta(minutes=3)
        feed = df_all[df_all["datetime"] < pd.Timestamp(close_ts)]
        ev = evaluate_macd_direction(
            feed, now=close_ts, last_signal_direction=last_dir,
            last_signal_bar_ts=last_bar, session_date=day,
        )
        if ev.get("new_signal"):
            live_arms.append({
                "bar_close_ts": close_ts.isoformat(sep="T"),
                "flag": ev.get("display_direction"),
                "signal_id": ev.get("signal_id"),
                "reason": ev.get("reason"),
                "target": target_symbol_for_direction(ev.get("display_direction")),
            })
            last_dir = ev.get("signal_direction") or ev.get("display_direction")
            last_bar = ev.get("bar_ts")

    broker = FakeBroker()
    state = om.default_state()
    state["auto_trade_on"] = True
    state["mode"] = "mock"
    state["budget"] = 1_000_000
    state["session_date"] = day
    state["opening_probe_enabled"] = False
    state["bootstrap"] = {"ok": True, "status": "OK", "received_1m_bars": int(len(df_all))}
    state["opening_probe"] = {"warmup_ready": True, "warmup_reason": "WARMUP_READY"}
    state["warmup_ready"] = True
    md.clear_quote_cache()

    worker_events = []
    latencies = []
    for arm in live_arms:
        close_ts = datetime.fromisoformat(arm["bar_close_ts"])
        now = close_ts + timedelta(seconds=5)
        feed = df_all[df_all["datetime"] < pd.Timestamp(close_ts)].copy()
        quotes = {
            "hynix": {"ok": True, "price": 1_900_000, "symbol": "000660", "updated_at": datetime.now().isoformat()},
            "long": {"ok": True, "price": 15_490, "symbol": LONG_SYMBOL, "updated_at": datetime.now().isoformat()},
            "inverse": {"ok": True, "price": 10_310, "symbol": INVERSE_SYMBOL, "updated_at": datetime.now().isoformat()},
        }
        md.store_quotes(quotes, mode="mock")
        t0 = datetime.now()
        out = worker.run_once(broker=broker, now=now, df_1m=feed, state=state)
        ms = (datetime.now() - t0).total_seconds() * 1000
        ol = state.get("order_latency_last") or state.get("order_latency") or {}
        segs = ol.get("segments_sec") or {}
        dtr = segs.get("signal_detect_to_order_request")
        if dtr is not None:
            latencies.append(float(dtr))
        worker_events.append({
            "arm": arm,
            "run_once_ms": round(ms, 1),
            "actions": out.get("actions"),
            "last_signal_id": state.get("last_signal_id"),
            "position": state.get("position"),
            "signal_detect_to_order_request_s": dtr,
            "order_requested_at": ol.get("order_requested_at") or state.get("order_requested_at"),
            "signal_detected_at": ol.get("signal_detected_at") or state.get("signal_detected_at"),
        })

    max_dtr = max(latencies) if latencies else None
    return {
        "day": day,
        "1m_bars": int(len(df_all)),
        "prior_1m_bars": int(len(prior)),
        "3m_completed": int(len(bars)),
        "onsets_collect": onsets,
        "live_arms_evaluate": live_arms,
        "worker_same_path_events": worker_events,
        "broker_orders": broker.orders,
        "latency": {
            "samples": latencies,
            "max_signal_detect_to_order_request_s": max_dtr,
            "all_under_5s": (max_dtr is None) or (max_dtr <= 5.0),
        },
        "targets": {"UP_RED": LONG_SYMBOL, "DOWN_BLUE": INVERSE_SYMBOL},
    }


def main() -> int:
    days_out = []
    for day in DAYS:
        tag = day.replace("-", "")
        if not (CACHE_DIR / f"replay_{tag}_hynix_1m.csv").exists():
            days_out.append({"day": day, "error": "missing_replay"})
            continue
        days_out.append(_run_day(day))

    report = {
        "days": days_out,
        "architecture": "MarketData→SignalEngine→Worker→OrderExecutor (fake broker)",
        "chart_vs_signed_b_note": (
            "Chart candle close times match bar_close_ts. signed-B signal_id uses bar OPEN "
            "(completed_bar_at - 3m)."
        ),
        "all_days_latency_under_5s": all(
            bool((d.get("latency") or {}).get("all_under_5s", False))
            for d in days_out if "error" not in d
        ),
    }
    out_path = ROOT / "data" / "state" / "macd_jul21_23_same_path_report.json"
    out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(json.dumps({
        "days": [
            {
                "day": d.get("day"),
                "arms": len(d.get("live_arms_evaluate") or []),
                "orders": len(d.get("broker_orders") or []),
                "max_dtr_s": (d.get("latency") or {}).get("max_signal_detect_to_order_request_s"),
                "under_5s": (d.get("latency") or {}).get("all_under_5s"),
                "error": d.get("error"),
            }
            for d in days_out
        ],
        "all_days_latency_under_5s": report["all_days_latency_under_5s"],
        "wrote": str(out_path),
    }, indent=2, ensure_ascii=False))
    return 0 if report["all_days_latency_under_5s"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
