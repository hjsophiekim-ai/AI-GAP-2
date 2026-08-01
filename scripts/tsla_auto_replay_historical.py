#!/usr/bin/env python
"""Replay TSLA_AUTO on historical KIS overseas 1m candles.

This is a dry-run diagnostic: it fetches historical 1m candles with KIS
inquire-time-itemchartprice pagination, caches them under data/cache/tsla_auto,
then runs the real TSLA_AUTO worker path with FakeBroker only.
"""
from __future__ import annotations

import csv
import json
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.data_sources.kis_overseas_minute import TR_OVERSEAS_MINUTE, _auth_headers
from app.trading.tsla_auto import config, ledger, market_data as market_data_module, state_store
from app.trading.tsla_auto.kis_overseas_adapter import _get_base_url
from app.trading.tsla_auto.market_data import MarketDataService
from app.trading.tsla_auto.models import QuoteSnapshot
from app.trading.tsla_auto.worker import initialize_strategy_session, run_once
from tests.tsla_auto.fake_broker import FakeBroker

ET = config.ET
CACHE_DIR = ROOT / "data" / "cache" / "tsla_auto"


@contextmanager
def _isolated_paths():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        orig = (
            state_store.STATE_DIR_PATH,
            state_store.STATE_PATH,
            ledger.LOGS_DIR_PATH,
            ledger.SIGNAL_LEDGER_PATH,
            ledger.EXECUTION_LEDGER_PATH,
            market_data_module.CACHE_DIR,
        )
        state_store.STATE_DIR_PATH = tmp_path
        state_store.STATE_PATH = tmp_path / "tsla_auto_runtime.json"
        ledger.LOGS_DIR_PATH = tmp_path
        ledger.SIGNAL_LEDGER_PATH = tmp_path / "tsla_auto_signal_ledger.csv"
        ledger.EXECUTION_LEDGER_PATH = tmp_path / "tsla_auto_execution_ledger.csv"
        market_data_module.CACHE_DIR = tmp_path / "cache"
        try:
            yield
        finally:
            (
                state_store.STATE_DIR_PATH,
                state_store.STATE_PATH,
                ledger.LOGS_DIR_PATH,
                ledger.SIGNAL_LEDGER_PATH,
                ledger.EXECUTION_LEDGER_PATH,
                market_data_module.CACHE_DIR,
            ) = orig


def _cache_path(symbol: str, trading_date: date) -> Path:
    return CACHE_DIR / f"{symbol}_{trading_date:%Y%m%d}_1m.csv"


def _parse_rows(rows: list[dict[str, Any]]) -> pd.DataFrame:
    out = []
    for item in rows:
        xymd = str(item.get("xymd") or "")
        xhms = str(item.get("xhms") or "")
        kymd = str(item.get("kymd") or "")
        khms = str(item.get("khms") or "")
        if xymd and xhms:
            dt = datetime.strptime(xymd + xhms, "%Y%m%d%H%M%S").replace(tzinfo=ET)
        elif kymd and khms:
            dt = datetime.strptime(kymd + khms, "%Y%m%d%H%M%S").replace(tzinfo=config.KST).astimezone(ET)
        else:
            continue
        close = float(str(item.get("last") or "0").replace(",", ""))
        if close <= 0:
            continue
        out.append({
            "datetime": dt,
            "open": float(str(item.get("open") or close).replace(",", "")),
            "high": float(str(item.get("high") or close).replace(",", "")),
            "low": float(str(item.get("low") or close).replace(",", "")),
            "close": close,
            "volume": int(float(item.get("evol") or 0)),
        })
    if not out:
        return pd.DataFrame(columns=["datetime", "open", "high", "low", "close", "volume"])
    return pd.DataFrame(out).drop_duplicates(subset=["datetime"], keep="last").sort_values("datetime").reset_index(drop=True)


def _read_cache(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    df["datetime"] = pd.to_datetime(df["datetime"]).dt.tz_convert(ET)
    return df


def _write_cache(path: Path, df: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False, quoting=csv.QUOTE_MINIMAL)


def fetch_symbol_day(symbol: str, exchange: str, trading_date: date, *, mode: str = "real", max_pages: int = 80) -> pd.DataFrame:
    path = _cache_path(symbol, trading_date)
    if path.exists():
        return _read_cache(path)

    url = _get_base_url(mode) + "/uapi/overseas-price/v1/quotations/inquire-time-itemchartprice"
    keyb = ""
    frames = []
    seen_keyb = set()
    oldest_seen: datetime | None = None
    target_start = datetime.combine(trading_date, config.SESSION_OPEN, tzinfo=ET)

    for page in range(max_pages):
        params = {
            "AUTH": "",
            "EXCD": exchange,
            "SYMB": symbol,
            "NMIN": "1",
            "PINC": "1",
            "NEXT": "1" if keyb else "",
            "NREC": "120",
            "FILL": "",
            "KEYB": keyb,
        }
        resp = requests.get(url, headers=_auth_headers(mode, TR_OVERSEAS_MINUTE), params=params, timeout=15)
        resp.raise_for_status()
        body = resp.json()
        rows = body.get("output2") or []
        if not rows:
            break
        df_page = _parse_rows(rows)
        if df_page.empty:
            break
        frames.append(df_page)
        oldest_seen = df_page["datetime"].iloc[0].to_pydatetime() if oldest_seen is None else min(oldest_seen, df_page["datetime"].iloc[0].to_pydatetime())
        oldest_row = df_page["datetime"].iloc[0]
        newest_row = df_page["datetime"].iloc[-1]
        next_keyb = f"{oldest_row:%Y%m%d%H%M%S}"
        if next_keyb in seen_keyb:
            break
        seen_keyb.add(next_keyb)
        keyb = next_keyb
        if oldest_seen <= target_start - timedelta(minutes=5):
            break
        time.sleep(0.12)

    if not frames:
        raise RuntimeError(f"no KIS candles fetched for {symbol}")
    raw = pd.concat(frames, ignore_index=True).drop_duplicates(subset=["datetime"], keep="last").sort_values("datetime").reset_index(drop=True)
    d = raw["datetime"].dt.tz_convert(ET)
    minutes = d.dt.hour * 60 + d.dt.minute
    open_min = config.SESSION_OPEN.hour * 60 + config.SESSION_OPEN.minute
    close_min = config.REGULAR_CLOSE.hour * 60 + config.REGULAR_CLOSE.minute
    day = d.dt.date == trading_date
    regular = raw.loc[day & (minutes >= open_min) & (minutes < close_min)].reset_index(drop=True)
    if regular.empty:
        raise RuntimeError(f"no regular-session candles for {symbol} {trading_date:%Y%m%d}; raw range {raw['datetime'].min()}..{raw['datetime'].max()}")
    _write_cache(path, regular)
    return regular


def _price_at(df: pd.DataFrame, now: datetime) -> float:
    rows = df[df["datetime"] <= pd.Timestamp(now)]
    if rows.empty:
        return float(df["close"].iloc[0])
    return float(rows["close"].iloc[-1])


def _load_history(target_day: date) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    previous_day = target_day - timedelta(days=1)
    tsla_prev = fetch_symbol_day(config.SIGNAL_SYMBOL, config.QUOTE_EXCHANGE_BY_SYMBOL[config.SIGNAL_SYMBOL], previous_day)
    tsla_day = fetch_symbol_day(config.SIGNAL_SYMBOL, config.QUOTE_EXCHANGE_BY_SYMBOL[config.SIGNAL_SYMBOL], target_day)
    tsll_day = fetch_symbol_day(config.LONG_SYMBOL, config.QUOTE_EXCHANGE_BY_SYMBOL[config.LONG_SYMBOL], target_day)
    tslz_day = fetch_symbol_day(config.INVERSE_SYMBOL, config.QUOTE_EXCHANGE_BY_SYMBOL[config.INVERSE_SYMBOL], target_day)
    tsla = pd.concat([tsla_prev, tsla_day], ignore_index=True).drop_duplicates(subset=["datetime"], keep="last").sort_values("datetime").reset_index(drop=True)
    return tsla, tsll_day, tslz_day


def main() -> int:
    target_day = date(2026, 7, 30)
    budget = float(config.DEFAULT_BUDGET_USD)
    tsla, tsll, tslz = _load_history(target_day)

    with _isolated_paths():
        svc = MarketDataService(mode="MOCK", fetch_minute_candles=lambda *a: (tsla, {}), fetch_quote=lambda mode, symbol: (None, None))
        svc.bootstrap(now=datetime.combine(target_day, config.SESSION_OPEN, tzinfo=ET))
        state = state_store.default_state()
        state.auto_trade_on = True
        state.budget_usd = budget
        state.strategy_name = config.STRATEGY_NAME
        state.strategy_version = config.STRATEGY_VERSION
        state.signal_rule = config.SIGNAL_RULE
        broker = FakeBroker(cash_usd=budget, quotes={config.LONG_SYMBOL: _price_at(tsll, datetime.combine(target_day, config.SESSION_OPEN, tzinfo=ET)), config.INVERSE_SYMBOL: _price_at(tslz, datetime.combine(target_day, config.SESSION_OPEN, tzinfo=ET))})

        start = datetime.combine(target_day, config.SESSION_OPEN, tzinfo=ET)
        initialize_strategy_session(state, svc, now=start, worker_instance_id="historical-replay")
        now = start + timedelta(minutes=3)
        end = datetime.combine(target_day, config.REGULAR_CLOSE, tzinfo=ET)
        actions = []
        while now <= end:
            prices = {
                config.SIGNAL_SYMBOL: _price_at(tsla, now),
                config.LONG_SYMBOL: _price_at(tsll, now),
                config.INVERSE_SYMBOL: _price_at(tslz, now),
            }
            for symbol, price in prices.items():
                svc._quotes[symbol] = QuoteSnapshot(
                    symbol=symbol,
                    price=price,
                    fetched_at=datetime.now(ET),
                    age_sec=0.0,
                    source=f"historical_at_{now.isoformat()}",
                )
                if symbol in config.TRADE_SYMBOLS:
                    broker.set_quote(symbol, price)
            result = run_once(broker=broker, market_data=svc, state=state, now=now)
            if result.actions:
                actions.append({"at_et": now.isoformat(), "actions": list(result.actions), "position": repr(state.position)})
            now += timedelta(minutes=3)

        signal_rows = ledger.load_signal_ledger(limit=10_000)
        execution_rows = ledger.load_execution_ledger(limit=10_000)
        summary = {
            "target_day": f"{target_day:%Y%m%d}",
            "budget_usd": budget,
            "tsla_rows": len(tsla[tsla["datetime"].dt.date == target_day]),
            "tsll_rows": len(tsll),
            "tslz_rows": len(tslz),
            "signals": [
                {
                    "signal_id": r.get("signal_id"),
                    "direction": r.get("direction"),
                    "completed_bar_at": r.get("completed_bar_at"),
                    "bar_start_at_et": r.get("bar_start_at_et"),
                    "order_result": r.get("order_result"),
                    "block_reason": r.get("block_reason"),
                    "broker_order_id": r.get("broker_order_id"),
                    "final_qty": r.get("final_qty"),
                    "order_price": r.get("order_price"),
                    "expected_notional_usd": r.get("expected_notional_usd"),
                }
                for r in signal_rows
            ],
            "executions": execution_rows,
            "actions": actions,
            "final_cash_usd": round(float(broker.get_cash()), 4),
            "final_position": None if state.position is None else {
                "symbol": state.position.symbol,
                "quantity": state.position.quantity,
                "avg_price": state.position.avg_price,
            },
        }
        out_path = CACHE_DIR / f"replay_{target_day:%Y%m%d}_summary.json"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        print(f"summary_path={out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
