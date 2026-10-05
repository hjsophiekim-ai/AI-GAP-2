"""TEMP (실행 후 즉시 삭제) — 주간 연구: 하루(D)를 전일(P) 선재생 후 production worker.run_once 로 재생. 모든 쓰기 tmp."""
from __future__ import annotations
import json, logging, os, pickle
from datetime import datetime, timedelta
from pathlib import Path
import pandas as pd
from app.trading.macd2 import chop_regime, config, ledger, p3_stack, premarket_shadow, strategy_mode, state_store, worker
from app.trading.macd2.market_data import MarketDataService
from app.trading.macd2.models import QuoteSnapshot
from tests.macd2.fake_broker import FakeBroker

SP = Path(os.environ.get("REPLAY_SP", ""))
KST = config.KST

def _d(s):
    return datetime.strptime(s, "%Y%m%d").replace(tzinfo=KST)

def test_wk_replay(monkeypatch, tmp_path):
    D, P, P2 = os.environ["WK_D"], os.environ["WK_P"], os.environ["WK_P2"]
    monkeypatch.setattr(chop_regime, "LEDGER_DIR_PATH", tmp_path)
    monkeypatch.setattr(chop_regime, "LEDGER_PATH", tmp_path / "shadow.json")
    monkeypatch.setattr(premarket_shadow, "PREMARKET_CARRY_DIR", tmp_path / "pm")
    base = pickle.load(open(SP / "base_d83.pkl", "rb"))["trades"]
    rows = []
    for t in base:
        if t["date"] >= P:
            continue
        rows.append(chop_regime.ShadowTrade(
            shadow_trade_id=chop_regime.make_shadow_trade_id(t["entry_time"], t["direction"], t.get("slot_number") or 1),
            trading_date=t["date"], entry_time=pd.Timestamp(t["entry_time"]).isoformat(),
            exit_time=pd.Timestamp(t["exit_time"]).isoformat(), direction=t["direction"],
            slot=int(t.get("slot_number") or 1), entry_price=float(t["entry_price"]),
            exit_price=float(t["exit_price"]), net_pct=float(t["net_pct"]),
            h50_intervened=bool(t["h50_held"]), tp1_hit=bool(t["tp1_hit"])))
    chop_regime.save_ledger(rows, source="replay-seed")
    rd = lambda n: pd.read_csv(SP / "data" / n, parse_dates=["datetime"])
    hy = pd.concat([rd(f"replay_{x}_hynix_1m.csv") for x in (P2, P, D)]).drop_duplicates("datetime", keep="last")
    hy["datetime"] = hy["datetime"].dt.tz_localize(KST)
    hy = hy.sort_values("datetime").reset_index(drop=True)
    etf = {}
    for sym, tag in ((config.LONG_SYMBOL, "long"), (config.INVERSE_SYMBOL, "inverse")):
        d = pd.concat([rd(f"replay_{x}_{tag}_1m.csv") for x in (P, D)]); d["datetime"] = d["datetime"].dt.tz_localize(KST)
        etf[sym] = d.sort_values("datetime").reset_index(drop=True)
    etf[config.WATCH_SYMBOL] = hy[hy["datetime"] >= pd.Timestamp(_d(P).replace(hour=8))].reset_index(drop=True)
    svc = MarketDataService(mode="mock", fetch_minute_candles=lambda *a, **k: (pd.DataFrame(columns=hy.columns), {}),
                            fetch_quote=lambda mode, symbol: (None, None))
    clock = {"now": _d(P).replace(hour=9)}
    def px(sym, now):
        d = etf[sym]; done = d[d["datetime"] + pd.Timedelta(minutes=1) <= pd.Timestamp(now)]
        return float(done["close"].iloc[-1]) if len(done) else None
    svc.get_quote = lambda symbol: (None if px(symbol, clock["now"]) is None else QuoteSnapshot(
        symbol=symbol, price=px(symbol, clock["now"]), fetched_at=clock["now"], age_sec=0.0, source="replay", error=None))
    svc.refresh_quotes = lambda *a, **k: None
    broker = FakeBroker(cash=50_000_000.0, quotes={config.LONG_SYMBOL: 10_000.0, config.INVERSE_SYMBOL: 6_000.0})
    state = state_store.default_state()
    state.auto_trade_on = True; state.mode = "mock"; state.budget = 10_000_000.0
    strategy_mode.apply(state, getattr(strategy_mode, os.environ.get("WK_MODE", "MODE_P3")))
    FDT = type("FDT", (datetime,), {"now": classmethod(lambda c, tz=None: clock["now"].astimezone(tz) if tz else clock["now"])})
    monkeypatch.setattr(worker, "datetime", FDT)
    orders, acts = [], []
    t = clock["now"]; end = _d(D).replace(hour=15, minute=21)
    hyend = hy["datetime"].iloc[-1].to_pydatetime() + timedelta(minutes=1)
    end = min(end, hyend)
    while t <= end:
        if _d(P).replace(hour=15, minute=21) < t < _d(D).replace(hour=8):
            t = _d(D).replace(hour=8); continue
        clock["now"] = t
        with svc._history_lock:
            svc._df_1m = hy[hy["datetime"] + pd.Timedelta(minutes=1) <= pd.Timestamp(t)].reset_index(drop=True)
        for sym in (config.LONG_SYMBOL, config.INVERSE_SYMBOL):
            p = px(sym, t)
            if p is not None:
                broker.set_quote(sym, p)
        n0 = len(broker.orders)
        r = worker.run_once(broker=broker, market_data=svc, state=state, now=t)
        for o in broker.orders[n0:]:
            if o.success and t.strftime("%Y%m%d") == D:
                orders.append(dict(t=t.isoformat(), side=o.side, sym=o.symbol, qty=int(o.executed_qty), px=float(o.executed_price)))
        if t.strftime("%Y%m%d") == D and r.actions:
            acts.append(dict(t=t.isoformat(), a=list(r.actions), mode=p3_stack.position_mode(state) if state.position else "-",
                             regime=state.p3_last_regime))
        t += timedelta(seconds=20)
    sig = []
    for r in ledger.load_signal_ledger(limit=0):
        if str(r.get("trading_date")) == D:
            sig.append({k: (str(v) if v is not None else None) for k, v in r.items()
                        if k in ("signal_id", "completed_bar_at", "order_result", "block_reason", "final_qty", "order_price", "detected_at", "direction")})
    ex = []
    for r in ledger.load_execution_ledger(limit=0):
        ex.append({k: (str(v) if v is not None else None) for k, v in r.items()
                   if k in ("side", "symbol", "executed_qty", "executed_price", "exit_reason", "net_pnl", "signal_id", "timestamp")})
    (SP / "out" / f"wk_{D}{os.environ.get('WK_SFX', '')}.json").write_text(json.dumps(dict(orders=orders, acts=acts, sig=sig, ex=ex), ensure_ascii=False, indent=1), encoding="utf-8")
